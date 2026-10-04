"""
Glue between the outside world (scheduler, API) and the pure engine.

Network fetches always happen OUTSIDE the store lock: bars are downloaded
first, then the state is re-read, advanced and saved under the lock. The
previous version of this app held its storage lock across Yahoo calls and
that stalled every request.
"""
from __future__ import annotations

import datetime as dt

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


def _fetch_enriched(symbols: list[str], period: str | None = None) -> dict:
    out = {}
    for sym in symbols:
        try:
            raw = (data_sources._yf_download_cached(sym, period, "5m") if period
                   else data_sources.fetch_intraday_bars(sym, "5m"))
            out[sym] = enrich(raw)
        except Exception as e:  # one bad symbol must not stop the others
            print(f"[warn] arena: bars failed for {sym}: {e}")
    return out


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


def tick_client(ip: str, now: dt.datetime | None = None) -> None:
    now = now or timeutil.utc_now()
    ist = _ist(now)
    today, minute = ist.date().isoformat(), ist.hour * 60 + ist.minute
    trading_today = market_calendar.is_trading_day(ist.date())

    state = store.load(ip)
    if not state or not state["config"].get("enabled"):
        return
    unsettled = bool(state["day"]) and not state["day_settled"]
    should_open = trading_today and minute >= OPEN_MINUTE and state["day"] != today
    if not (unsettled or should_open):
        return

    bars = _fetch_enriched(state["config"]["symbols"])

    with store.lock:
        state = store.load(ip)
        if not state:
            return
        # Catch up a day the server slept through, then settle it.
        if state["day"] and not state["day_settled"] and state["day"] != today:
            engine.process_bars(state, bars, now=None)
            engine.settle_day(state)
        if trading_today and minute >= OPEN_MINUTE and state["day"] != today:
            engine.start_day(state, today)
        if state["day"] == today and not state["day_settled"]:
            engine.process_bars(state, bars, now=now)
            if minute >= SETTLE_MINUTE:
                engine.settle_day(state)
        store.save(ip, state)


# ---------------------------------------------------------------------------
# API operations
# ---------------------------------------------------------------------------

def view(ip: str) -> dict:
    state = load_or_create(ip)
    return {
        "config": state["config"],
        "strategies": catalog(),
        "market": market_calendar.market_status(),
        "day": state["day"],
        "day_settled": state["day_settled"],
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
    out = []
    for book_id, book in state["books"].items():
        if sid and book_id != sid:
            continue
        out.extend(book["trades"])
    out.sort(key=lambda t: t["exit_at"], reverse=True)
    return out[:max(1, min(limit, 1000))]


def run_backtest(ip: str, days: int = 30) -> dict:
    state = load_or_create(ip)
    bars = _fetch_enriched(state["config"]["symbols"], REPLAY_PERIOD)
    bars = {k: v for k, v in bars.items() if v is not None and not v.empty}
    if not bars:
        return {"ok": False, "reason": "No historical 5-minute data could be fetched for these stocks."}
    result = engine.replay(state["config"], bars, days=max(1, min(int(days), 60)))
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
        "recent_trades": sorted(
            (t for b in result["books"].values() for t in b["trades"]),
            key=lambda t: t["exit_at"], reverse=True)[:60],
    }
