"""
Glue between the outside world (scheduler, API) and the pure engine.

Network fetches always happen OUTSIDE the store lock: bars are downloaded
first, then the state is re-read, advanced and saved under the lock. The
previous version of this app held its storage lock across Yahoo calls and
that stalled every request.
"""
from __future__ import annotations

import datetime as dt
import threading

import data_sources
import market_calendar
import timeutil
from arena import engine, store
from arena import costs as costs_mod
from arena.strategies import enrich, catalog

BACKTEST_PREFIX = "bt::"
REPLAY_PERIOD = "60d"     # Yahoo serves 5-minute bars for about 60 days
OPEN_MINUTE = 9 * 60 + 15
SETTLE_MINUTE = engine.SESSION_END_MINUTE + engine.BAR_MINUTES   # last bar complete


def _ist(now: dt.datetime | None):
    return (now or timeutil.utc_now()).astimezone(timeutil.IST)


def _fetch_enriched(symbols: list[str], period: str | None = None) -> tuple[dict, dict]:
    """Returns (bars_by_symbol, diagnostics).

    The diagnostics exist because a silent data failure and a sleeping
    server look identical from the UI - both just stop updating. Recording
    which symbols returned bars, which failed, and how recent the newest bar
    is lets the header say which it was.
    """
    out, ok, failed, latest = {}, [], [], None
    for sym in symbols:
        try:
            raw = (data_sources._yf_download_cached(sym, period, "5m") if period
                   else data_sources.fetch_intraday_bars(sym, "5m"))
            df = enrich(raw)
            if df is None or df.empty:
                failed.append(sym)
                continue
            out[sym] = df
            ok.append(sym)
            last_ts = df.index[-1]
            if latest is None or last_ts > latest:
                latest = last_ts
        except Exception as e:  # one bad symbol must not stop the others
            failed.append(sym)
            print(f"[warn] arena: bars failed for {sym}: {e}")
    diagnostics = {
        "at": timeutil.iso_now(),
        "symbols_ok": ok,
        "symbols_failed": failed,
        "latest_bar_at": timeutil.to_iso(latest.to_pydatetime()) if latest is not None else None,
    }
    return out, diagnostics


# One background tick per client at a time. Without this, each poll of a
# slow-loading page would start another catch-up over the same sessions.
_bg_locks: dict[str, threading.Lock] = {}
_bg_locks_guard = threading.Lock()


def _bg_lock(ip: str) -> threading.Lock:
    with _bg_locks_guard:
        return _bg_locks.setdefault(ip, threading.Lock())


def is_due(ip: str, now: dt.datetime | None = None) -> bool:
    """Whether a tick would actually do something: a session that should be
    open, or past days still unsettled. Cheap - reads state, no network."""
    state = store.load(ip)
    if not state or not state["config"].get("enabled"):
        return False
    now = now or timeutil.utc_now()
    ist = _ist(now)
    today, minute = ist.date().isoformat(), ist.hour * 60 + ist.minute
    if _missed_sessions(state, ist):
        return True
    if market_calendar.is_trading_day(ist.date()) and minute >= OPEN_MINUTE:
        return state["day"] != today or not state["day_settled"]
    return False


def tick_in_background(ip: str, now: dt.datetime | None = None) -> bool:
    """Kick off a tick without blocking the caller.

    The arena page is often the first thing to touch a just-woken server, so
    the request that wakes it is exactly the one that should start the
    catch-up - but it must not wait for a 6-symbol Yahoo fetch to render.
    The page's 60-second poll picks up the result.
    """
    if not is_due(ip, now):
        return False
    lock = _bg_lock(ip)
    if not lock.acquire(blocking=False):
        return False                    # one is already running

    def run():
        try:
            tick_client(ip, now)
        except Exception as e:
            print(f"[warn] arena background tick failed for {ip}: {e}")
        finally:
            lock.release()

    threading.Thread(target=run, name=f"arena-tick-{ip[:12]}", daemon=True).start()
    return True


def load_or_create(ip: str) -> dict:
    with store.lock:
        state = store.load(ip)
        if state is None:
            state = engine.new_state()
            store.save(ip, state)
        return state


# ---------------------------------------------------------------------------
# Scheduler entry points
# ---------------------------------------------------------------------------

def tick_all(now: dt.datetime | None = None) -> None:
    for ip in store.all_ids():
        if ip.startswith(BACKTEST_PREFIX):
            continue
        try:
            tick_client(ip, now)
        except Exception as e:
            print(f"[warn] arena tick failed for {ip}: {e}")


MAX_CATCHUP_SESSIONS = 60       # Yahoo serves ~60 days of 5-minute bars
SHORT_GAP_SESSIONS = 4          # beyond this, the 5d fetch can't cover it


def _missed_sessions(state: dict, ist: dt.datetime) -> list[str]:
    """Trading days that should have been settled but weren't.

    Walks forward from last_settled_day rather than looking only at the one
    day left open. On a free Render instance the server can be asleep for a
    week; the old code settled whichever single day was still open and
    silently skipped every session between it and today.
    """
    today = ist.date()
    anchor = state.get("last_settled_day") or state.get("day")
    if not anchor:
        return []                       # brand-new arena: start from today
    try:
        cursor = dt.date.fromisoformat(anchor)
    except (TypeError, ValueError):
        return []

    # The open-but-unsettled day is itself a missed session.
    include_anchor = bool(state.get("day")) and not state.get("day_settled")
    out = [state["day"]] if include_anchor else []

    cursor += dt.timedelta(days=1)
    while cursor <= today and len(out) < MAX_CATCHUP_SESSIONS:
        if market_calendar.is_trading_day(cursor):
            iso = cursor.isoformat()
            if iso not in out:
                out.append(iso)
        cursor += dt.timedelta(days=1)
    return out[:MAX_CATCHUP_SESSIONS]


def tick_client(ip: str, now: dt.datetime | None = None) -> None:
    now = now or timeutil.utc_now()
    ist = _ist(now)
    today, minute = ist.date().isoformat(), ist.hour * 60 + ist.minute
    trading_today = market_calendar.is_trading_day(ist.date())

    state = store.load(ip)
    if not state or not state["config"].get("enabled"):
        return

    missed = _missed_sessions(state, ist)
    # A session only counts as missed once it is over; today is handled below.
    finished_missed = [d for d in missed if d < today or minute >= SETTLE_MINUTE]
    should_open = trading_today and minute >= OPEN_MINUTE and state["day"] != today
    today_live = state["day"] == today and not state["day_settled"]
    if not (finished_missed or should_open or today_live):
        # Nothing to do - but still record that a tick happened. Without
        # this, last_tick_at keeps the timestamp of the last tick that had
        # work, and the UI reports "the server was asleep" about a server
        # that is wide awake with a settled day behind it. The stale catch-up
        # count would likewise keep claiming days were replayed.
        with store.lock:
            state = store.load(ip)
            if state:
                state["last_tick_at"] = timeutil.iso_now()
                state["caught_up_last"] = 0
                store.save(ip, state)
        return

    # A long gap needs more history than the 5-day intraday window holds.
    gap = len([d for d in finished_missed if d != today])
    period = REPLAY_PERIOD if gap > SHORT_GAP_SESSIONS else None
    bars, diagnostics = _fetch_enriched(state["config"]["symbols"], period)

    with store.lock:
        state = store.load(ip)
        if not state:
            return
        caught_up = 0
        # Replay each missed session in date order, exactly as the live path
        # would have: same engine, same bars, same functions.
        for date in _missed_sessions(state, ist):
            if date == today and minute < SETTLE_MINUTE:
                break                   # today is still running; handled below
            if state["day"] != date:
                engine.start_day(state, date, caught_up=True)
            engine.process_bars(state, bars, now=None)
            engine.settle_day(state, caught_up=True)
            caught_up += 1

        if trading_today and minute >= OPEN_MINUTE and state["day"] != today:
            engine.start_day(state, today)
        if state["day"] == today and not state["day_settled"]:
            engine.process_bars(state, bars, now=now)
            if minute >= SETTLE_MINUTE:
                engine.settle_day(state)

        state["last_tick_at"] = timeutil.iso_now()
        state["last_tick"] = diagnostics
        state["caught_up_last"] = caught_up
        store.save(ip, state)


# ---------------------------------------------------------------------------
# API operations
# ---------------------------------------------------------------------------

STALE_TICK_MINUTES = 30


def freshness(state: dict, now: dt.datetime | None = None) -> dict:
    """How current the arena's data is, and why it might not be.

    The UI needs to tell three situations apart that all look like "nothing
    is happening": the server was asleep, the data source failed, or the
    market is simply shut.
    """
    now = now or timeutil.utc_now()
    last = timeutil.parse_utc(state.get("last_tick_at"))
    age_minutes = None if last is None else round((now - last).total_seconds() / 60.0, 1)
    market_open = market_calendar.is_market_open(now)
    tick = state.get("last_tick") or {}
    return {
        "last_tick_at": state.get("last_tick_at"),
        "age_minutes": age_minutes,
        "stale": bool(market_open and (age_minutes is None or age_minutes > STALE_TICK_MINUTES)),
        "caught_up_last": state.get("caught_up_last", 0),
        "last_settled_day": state.get("last_settled_day"),
        "symbols_ok": tick.get("symbols_ok", []),
        "symbols_failed": tick.get("symbols_failed", []),
        "latest_bar_at": tick.get("latest_bar_at"),
    }


def view(ip: str) -> dict:
    state = load_or_create(ip)
    return {
        "config": state["config"],
        "strategies": catalog(),
        "market": market_calendar.market_status(),
        "day": state["day"],
        "day_settled": state["day_settled"],
        "storage": {
            "backend": store.backend_name(),
            # True means results live in a file Render wipes on every sleep
            # or redeploy - worth a banner, not a footnote.
            "ephemeral": store.backend_name() == "json_file",
        },
        "freshness": freshness(state),
        "verdict": engine.verdict(state),
        "leaderboard": engine.leaderboard(state),
        "open_positions": engine.open_positions(state),
        "history": state["history"][-30:],
        "presets": {"liquid_nifty": engine.LIQUID_NIFTY},
        "default_costs": costs_mod.DEFAULT_COSTS,
        "default_risk": engine.DEFAULT_RISK,
    }


def update_config(ip: str, raw: dict) -> dict:
    with store.lock:
        state = store.load(ip) or engine.new_state()
        old = state["config"]
        new = engine.normalise_config(raw, base=old)
        in_session = bool(state["day"]) and not state["day_settled"]
        if in_session:
            # Close positions the new config no longer covers, at their mark,
            # rather than leaving them unmanaged until the close.
            for sid, book in state["books"].items():
                for sym in list(book["positions"]):
                    if sid not in new["strategies"] or sym not in new["symbols"]:
                        mark = state["marks"].get(sym, {}).get("price") or book["positions"][sym]["entry_price"]
                        engine._close(state, book, sym, mark, "config_changed",
                                      state["marks"].get(sym, {}).get("at"))
        state["config"] = new
        if in_session:
            # New strategies join the running day with today's capital
            # (capital changes themselves apply from the next session).
            for sid in new["strategies"]:
                if sid not in state["books"]:
                    book = engine._new_book(sid, old["daily_capital"])
                    state["books"][sid] = book
        store.save(ip, state)
        return state["config"]


def reset(ip: str) -> None:
    with store.lock:
        state = store.load(ip)
        store.save(ip, engine.new_state(state["config"] if state else None))


def set_status(ip: str, sid: str, status: str) -> bool:
    with store.lock:
        state = store.load(ip)
        if not state or sid not in state["books"] or status not in ("active", "benched"):
            return False
        book = state["books"][sid]
        if book["status"] != status:
            book.update(status=status, status_reason=f"set to {status} manually",
                        status_changed=timeutil.iso_now())
        store.save(ip, state)
        return True


def trades(ip: str, sid: str | None = None, limit: int = 100) -> list[dict]:
    state = store.load(ip)
    if not state:
        return []
    # Which sessions were replayed after the fact. Tagged here rather than
    # stored on each trade: it is a property of the day, and deriving it
    # keeps trades written before this existed correct too.
    caught_up_days = {h["date"] for h in state.get("history", []) if h.get("caught_up")}
    out = []
    for book_id, book in state["books"].items():
        if sid and book_id != sid:
            continue
        for trade in book["trades"]:
            exit_day = (trade.get("exit_at") or "")[:10]
            out.append({**trade, "caught_up": exit_day in caught_up_days})
    out.sort(key=lambda t: t["exit_at"], reverse=True)
    return out[:max(1, min(limit, 1000))]


def run_backtest(ip: str, days: int = 30, slippage_sweep: bool = False) -> dict:
    state = load_or_create(ip)
    bars, _diag = _fetch_enriched(state["config"]["symbols"], REPLAY_PERIOD)
    bars = {k: v for k, v in bars.items() if v is not None and not v.empty}
    if not bars:
        return {"ok": False, "reason": "No historical 5-minute data could be fetched for these stocks."}
    days = max(1, min(int(days), 60))
    result = engine.replay(state["config"], bars, days=days)
    if slippage_sweep:
        result["slippage_sweep"] = engine.replay_slippage_sweep(
            state["config"], bars, days=days)
    with store.lock:
        store.save(BACKTEST_PREFIX + ip, result)
    return backtest_view(result)


def last_backtest(ip: str) -> dict | None:
    result = store.load(BACKTEST_PREFIX + ip)
    return backtest_view(result) if result else None


def backtest_view(result: dict) -> dict:
    return {
        "ok": True,
        "replay": result.get("replay"),
        "verdict": engine.verdict(result),
        "leaderboard": engine.leaderboard(result),
        "history": result["history"],
        "slippage_sweep": result.get("slippage_sweep"),
        "recent_trades": sorted(
            (t for b in result["books"].values() for t in b["trades"]),
            key=lambda t: t["exit_at"], reverse=True)[:60],
    }
