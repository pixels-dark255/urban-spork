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
import time
from zoneinfo import ZoneInfo

import data_sources
import market_calendar
import timeutil
from arena import engine, store
from arena import markets as markets_mod
from arena import runner as runner_mod
from arena import scoreboard as scoreboard_mod
from arena.strategies import enrich, catalog

BACKTEST_PREFIX = "bt::"
# Pause between symbols, and the waits before each retry of one symbol.
# The first entry is 0 because the first attempt is not a retry.
FETCH_GAP_SECONDS = 0.5
FETCH_BACKOFF = (0.0, 2.0, 6.0)
REPLAY_PERIOD = "60d"     # Yahoo serves 5-minute bars for about 60 days
# A session is only finished once its last bar has printed.
SETTLE_LAG = dt.timedelta(minutes=engine.BAR_MINUTES)


def _ist(now: dt.datetime | None):
    return (now or timeutil.utc_now()).astimezone(timeutil.IST)


def _market_window(state: dict):
    cfg = state["config"]
    market = engine.market_of(state)
    return market, (cfg.get("window") or market.default_window())


def _session_ended(market, window, date_iso: str, now: dt.datetime) -> bool:
    bounds = markets_mod.session_bounds(market, window, date_iso)
    return bool(bounds) and now >= bounds[1] + SETTLE_LAG


def current_session(market, window, now: dt.datetime):
    """The session this tick is responsible for: the one in progress, or the
    one that ended earlier today.

    Two window dates are tried, newest first, because a window that crosses
    midnight belongs to the date it started - at 00:30 the session under way
    is yesterday's.

    A session that ended on an earlier date is deliberately NOT current. It
    is a session the server slept through, and the catch-up settles it and
    tags it as replayed; letting the live path quietly adopt it would make a
    Friday session still look "live" on Saturday night.
    """
    tz = ZoneInfo(window["tz"])
    local = now.astimezone(tz).date()
    for cand in (local, local - dt.timedelta(days=1)):
        bounds = markets_mod.active_session(market, window, cand)
        if not bounds or now < bounds[0]:
            continue
        if now <= bounds[1] or bounds[1].astimezone(tz).date() == local:
            return cand.isoformat(), bounds
    return None, None


def fetch_fx(market, now: dt.datetime | None = None) -> dict:
    """The rates needed to state this market's money in rupees.

    Skipped entirely for the NSE, whose book is already in rupees - there
    is no reason to spend a network call converting INR to INR.
    """
    symbols = [] if market.quote_currency == "INR" else [markets_mod.FX_SYMBOLS["USDINR"]]
    if any(market.symbol_currency(s) == "JPY" for s in market.default_symbols):
        symbols.append(markets_mod.FX_SYMBOLS["USDJPY"])
    quotes = {}
    for sym in symbols:
        try:
            raw = data_sources.fetch_intraday_bars(sym, "5m")
            if raw is not None and not raw.empty:
                quotes[sym] = float(raw["Close"].dropna().iloc[-1])
        except Exception as e:
            print(f"[warn] arena: FX quote failed for {sym}: {e}")
    return markets_mod.fx_for_session(market, list(market.default_symbols), quotes)


def _fetch_enriched(symbols: list[str], period: str | None = None,
                    market=None, window: dict | None = None,
                    fx: dict | None = None) -> tuple[dict, dict]:
    """Returns (bars_by_symbol, diagnostics).

    The diagnostics exist because a silent data failure and a sleeping
    server look identical from the UI - both just stop updating. Recording
    which symbols returned bars, which failed, and how recent the newest bar
    is lets the header say which it was.
    """
    out, ok, failed, latest = {}, [], [], None
    for i, sym in enumerate(symbols):
        # Sequential, with a pause between symbols. Yahoo rate-limits by IP
        # and a scheduled job runs from a datacenter address, which is
        # exactly what gets throttled - the same reason the existing
        # curl_cffi session is kept rather than plain requests.
        if i:
            time.sleep(FETCH_GAP_SECONDS)
        df, error = None, None
        for attempt, backoff in enumerate(FETCH_BACKOFF):
            if attempt:
                time.sleep(backoff)
            try:
                raw = (data_sources._yf_download_cached(sym, period, "5m") if period
                       else data_sources.fetch_intraday_bars(sym, "5m"))
                scale = ((fx or {}).get("symbol_scale") or {}).get(sym)
                if scale is None:
                    scale = 1.0
                candidate = enrich(raw, market=market, window=window, scale=float(scale))
                if candidate is None or candidate.empty:
                    error = "no bars in this market's hours / your window"
                    continue
                df, error = candidate, None
                break
            except Exception as e:   # one bad symbol must not stop the others
                error = f"{type(e).__name__}: {e}"
        if df is None:
            failed.append(sym)
            print(f"[warn] arena: bars failed for {sym} after "
                  f"{len(FETCH_BACKOFF)} attempts: {error}")
            continue
        out[sym] = df
        ok.append(sym)
        last_ts = df.index[-1]
        if latest is None or last_ts > latest:
            latest = last_ts
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
    """Whether a tick would actually do something: a session running now, or
    a finished one still unsettled. Cheap - reads state, no network."""
    state = store.load(ip)
    if not state or not state["config"].get("enabled"):
        return False
    now = timeutil.ensure_aware(now or timeutil.utc_now())
    if _missed_sessions(state, now):
        return True
    market, window = _market_window(state)
    current, _bounds = current_session(market, window, now)
    if current:
        return state["day"] != current or not state["day_settled"]
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
    """Load this client's arena for one market, creating it on first use.

    The market comes from the storage key, so a client asking for their US
    arena gets a US arena rather than a copy of their NSE one.
    """
    _client, market_id = store.split_key(ip)
    with store.lock:
        state = store.load(ip)
        if state is None:
            state = engine.new_state({"market": market_id})
            store.save(ip, state)
        elif state["config"].get("market") != market_id:
            # Defensive: a state filed under a market key must say so, or
            # every cost and calendar decision below reads the wrong profile.
            state["config"]["market"] = market_id
            store.save(ip, state)
        return state


# ---------------------------------------------------------------------------
# Scheduler entry points
# ---------------------------------------------------------------------------

def tick_all(now: dt.datetime | None = None) -> None:
    """Advance every stored arena, across every market.

    Each key is one client's arena for one market, so a user running all
    four gets four ticks - each on its own calendar, and each a no-op
    while its own session is shut.
    """
    for ip in store.all_ids():
        if ip.startswith(BACKTEST_PREFIX):
            continue
        try:
            tick_client(ip, now)
        except Exception as e:
            print(f"[warn] arena tick failed for {ip}: {e}")


MAX_CATCHUP_SESSIONS = 60       # Yahoo serves ~60 days of 5-minute bars
SHORT_GAP_SESSIONS = 4          # beyond this, the 5d fetch can't cover it


def _missed_sessions(state: dict, now: dt.datetime) -> list[str]:
    """Active sessions that have finished but were never settled.

    Walks forward from last_settled_day over the market's own calendar and
    the user's window, rather than over calendar dates: a US arena skips
    NYSE holidays, a forex arena skips the weekend gap, and a window the
    user restricted to three evenings a week has three sessions a week.

    On a free Render instance the server can be asleep for a week, so this
    returns every missed session, not just the one left open.
    """
    market, window = _market_window(state)
    anchor = state.get("last_settled_day") or state.get("day")
    if not anchor:
        return []                       # brand-new arena: start from now
    try:
        cursor = dt.date.fromisoformat(anchor)
    except (TypeError, ValueError):
        return []

    # The session now under way (or the latest to have started) is the live
    # path's business, not the catch-up's - otherwise a brand-new arena
    # would file its very first session as "caught up", and the live and
    # replayed paths would both try to settle it.
    live_date, _bounds = current_session(market, window, now)

    out = []
    # The open-but-unsettled session is itself missed, once it has ended.
    if state.get("day") and state["day"] != live_date and not state.get("day_settled") \
            and _session_ended(market, window, state["day"], now):
        out.append(state["day"])

    wtz = ZoneInfo(window["tz"])
    # +1 day: a session that starts late and crosses midnight belongs to the
    # date it started, which may already be "yesterday" by the window's clock.
    last = now.astimezone(wtz).date() + dt.timedelta(days=1)
    for iso in markets_mod.sessions_in_range(market, window,
                                             cursor + dt.timedelta(days=1), last,
                                             limit=MAX_CATCHUP_SESSIONS * 2):
        if iso in out or iso == live_date:
            continue
        if _session_ended(market, window, iso, now):
            out.append(iso)
        if len(out) >= MAX_CATCHUP_SESSIONS:
            break
    return sorted(out)[:MAX_CATCHUP_SESSIONS]


def tick_client(ip: str, now: dt.datetime | None = None) -> None:
    now = timeutil.ensure_aware(now or timeutil.utc_now())
    state = store.load(ip)
    if not state or not state["config"].get("enabled"):
        return
    market, window = _market_window(state)

    finished_missed = _missed_sessions(state, now)
    # The session under way, or the last one to have started. None outside
    # the user's window - which is most of the day, and the whole point of
    # letting them choose it.
    current, _bounds = current_session(market, window, now)
    should_open = bool(current) and state["day"] != current
    live = bool(current) and state["day"] == current and not state["day_settled"]
    if not (finished_missed or should_open or live):
        # Nothing to do - but still record that a tick happened. Without
        # this, last_tick_at keeps the timestamp of the last tick that had
        # work, and the UI reports "the server was asleep" about a server
        # that is wide awake with a settled session behind it. The stale
        # catch-up count would likewise keep claiming sessions were replayed.
        with store.lock:
            state = store.load(ip)
            if state:
                state["last_tick_at"] = timeutil.iso_now()
                state["caught_up_last"] = 0
                store.save(ip, state)
        return

    # A long gap needs more history than the 5-day intraday window holds.
    gap = len([d for d in finished_missed if d != current])
    period = REPLAY_PERIOD if gap > SHORT_GAP_SESSIONS else None
    fx = fetch_fx(market, now)
    bars, diagnostics = _fetch_enriched(state["config"]["symbols"], period,
                                        market=market, window=window, fx=fx)

    missing = sorted(set(state["config"]["symbols"]) - set(bars))
    # Nothing downloaded at all. Settling now would record a day of zero
    # trades for every strategy and mark it done, and the real session -
    # which did happen, and whose bars Yahoo still has - would be lost for
    # good. Leave it unsettled; the next run picks it up.
    if not bars:
        with store.lock:
            state = store.load(ip)
            if state:
                state["last_tick_at"] = timeutil.iso_now()
                state["last_tick"] = {**diagnostics, "settled_nothing": True}
                state["caught_up_last"] = 0
                store.save(ip, state)
        print(f"[warn] arena {ip}: no bars for any symbol - leaving "
              f"{len(finished_missed)} session(s) unsettled for the next run")
        return

    caught_up = 0
    with store.lock:
        state = store.load(ip)
        if not state:
            return
        # Replay each missed session in order, exactly as the live path
        # would have: same engine, same bars, same functions.
        #
        # Saved after EACH one. This runs as a short-lived scheduled job
        # that can be killed by a timeout partway through a long catch-up;
        # saving once at the end would throw away every session replayed
        # so far and make the next run start the same work from scratch.
        for date in _missed_sessions(state, now):
            if state["day"] != date:
                engine.start_day(state, date, caught_up=True, fx=fx)
            engine.process_bars(state, bars, now=None)
            engine.settle_day(state, caught_up=True, missing_symbols=missing)
            caught_up += 1
            state["last_tick_at"] = timeutil.iso_now()
            state["last_tick"] = diagnostics
            state["caught_up_last"] = caught_up
            store.save(ip, state)

        if current:
            # A session that was already over when this tick began was
            # replayed, not watched - exactly like the ones above - so it
            # is tagged and counted the same way. Anything else would make
            # "caught up" mean "replayed, unless it happened to be the most
            # recent one", which is not a distinction anyone can use.
            ended = _session_ended(market, window, current, now)
            opened_late = ended and state["day"] != current
            if state["day"] != current:
                engine.start_day(state, current, caught_up=opened_late, fx=fx)
            if state["day"] == current and not state["day_settled"]:
                # `now=None` once the session is over: every bar in it is
                # complete, so none needs holding back as still forming.
                engine.process_bars(state, bars, now=None if ended else now)
                if ended:
                    engine.settle_day(state, caught_up=opened_late,
                                      missing_symbols=missing)
                    if opened_late:
                        caught_up += 1

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
    now = timeutil.ensure_aware(now or timeutil.utc_now())
    last = timeutil.parse_utc(state.get("last_tick_at"))
    age_minutes = None if last is None else round((now - last).total_seconds() / 60.0, 1)
    market, window = _market_window(state)
    # Staleness is judged against the user's ACTIVE session, not the
    # market's hours: nothing is expected to happen while the US market is
    # open but the window says the user is asleep.
    market_open = bool(markets_mod.session_date_for(market, window, now))
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
    with store.lock:
        fresh = store.load(ip) or state
        if engine.adopt_new_strategies(fresh):
            store.save(ip, fresh)
        state = fresh
    market, window = _market_window(state)
    return {
        "config": state["config"],
        "strategies": catalog(market.has_volume),
        # The NSE status block the frontend has always read, kept for the
        # India tab and for /api/market-status's contract.
        "market": market_calendar.market_status(),
        "market_profile": market.status(),
        "session": markets_mod.session_info(market, window),
        "currency": {"code": market.quote_currency, "symbol": market.currency_symbol,
                     "fractional": market.fractional},
        "fx": state.get("fx"),
        "regulatory_note": REGULATORY_NOTE,
        "day": state["day"],
        "day_settled": state["day_settled"],
        # `ephemeral` means results live in a file Render wipes on every
        # sleep or redeploy - worth a banner, not a footnote. `configured`
        # and `error` say WHICH of the two reasons it is.
        "storage": store.backend_status(),
        "freshness": freshness(state),
        # Who advances this arena, when it last did, and when it is next
        # due. The dashboard being asleep is now expected; the RUNNER
        # being asleep is the thing worth a banner.
        "runner": runner_mod.status(state),
        "verdict": engine.verdict(state),
        "leaderboard": engine.leaderboard(state),
        "open_positions": engine.open_positions(state),
        "history": state["history"][-30:],
        "presets": market.presets,
        "symbol_hint": market.symbol_hint,
        "default_costs": market.costs.defaults,
        "default_risk": engine.DEFAULT_RISK,
        "max_leverage": engine.MAX_LEVERAGE if market.allow_leverage else 1.0,
    }


# Shown under every leaderboard, on every market. The arena is a simulator,
# and the rules for actually doing any of this from India are not the same
# for the four markets - a note that only appears in the README is a note
# the person placing a real order never reads.
REGULATORY_NOTE = (
    "Simulation only. For real money in India: forex is legal only in INR pairs and "
    "RBI-permitted crosses on NSE/BSE through a SEBI-registered broker. Offshore forex "
    "apps are illegal. US stocks go through the LRS route. Crypto is taxed at 30% plus "
    "1% TDS."
)


def markets_overview(now: dt.datetime | None = None, client: str | None = None) -> dict:
    """Every market with its status, and this client's window for it."""
    now = timeutil.ensure_aware(now or timeutil.utc_now())
    out = []
    for market in markets_mod.MARKETS.values():
        row = market.status(now)
        window = market.default_window()
        if client:
            state = store.load(store.key(client, market.id))
            if state:
                window = state["config"].get("window") or window
                row["has_arena"] = True
                row["day"] = state.get("day")
                row["day_settled"] = state.get("day_settled")
        row["session"] = markets_mod.session_info(market, window, now=now)
        out.append(row)
    return {"markets": out, "default": markets_mod.DEFAULT_MARKET,
            "regulatory_note": REGULATORY_NOTE,
            "us_holidays": markets_mod.us_holiday_status()}


def scoreboard(ip: str, period: str | None = None) -> dict:
    """The long-run record for this arena: per strategy, per day."""
    state = load_or_create(ip)
    board = scoreboard_mod.scoreboard(state, period)
    # Where a day's rate was never recorded, offer a rough rupee total at
    # the last rate seen - labelled separately so it is never mistaken for
    # the figure the session actually settled at.
    return scoreboard_mod.to_inr_totals(board, state.get("fx"))


def update_config(ip: str, raw: dict) -> dict:
    _client, market_id = store.split_key(ip)
    with store.lock:
        state = store.load(ip) or engine.new_state({"market": market_id})
        old = state["config"]
        # The market is taken from the storage key, never from the request:
        # one arena per market, and a POST cannot move an arena's history
        # onto a different calendar and currency.
        new = engine.normalise_config(raw, base=old, market_id=market_id)
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
    _client, market_id = store.split_key(ip)
    with store.lock:
        state = store.load(ip)
        cfg = state["config"] if state else {"market": market_id}
        cfg = {**cfg, "market": market_id}
        store.save(ip, engine.new_state(cfg))


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
    market, window = _market_window(state)
    fx = fetch_fx(market)
    bars, _diag = _fetch_enriched(state["config"]["symbols"], REPLAY_PERIOD,
                                  market=market, window=window, fx=fx)
    bars = {k: v for k, v in bars.items() if v is not None and not v.empty}
    if not bars:
        return {"ok": False,
                "reason": ("No historical 5-minute data could be fetched for these symbols "
                           "inside your trading window.")}
    days = max(1, min(int(days), 60))
    result = engine.replay(state["config"], bars, days=days, fx=fx)
    if slippage_sweep:
        result["slippage_sweep"] = engine.replay_slippage_sweep(
            state["config"], bars, days=days, fx=fx)
    with store.lock:
        store.save(BACKTEST_PREFIX + ip, result)
    return backtest_view(result)


def last_backtest(ip: str) -> dict | None:
    result = store.load(BACKTEST_PREFIX + ip)
    return backtest_view(result) if result else None


def backtest_view(result: dict) -> dict:
    market = engine.market_of(result)
    return {
        "ok": True,
        "currency": {"code": market.quote_currency, "symbol": market.currency_symbol,
                     "fractional": market.fractional},
        "replay": result.get("replay"),
        "verdict": engine.verdict(result),
        "leaderboard": engine.leaderboard(result),
        "history": result["history"],
        "slippage_sweep": result.get("slippage_sweep"),
        "recent_trades": sorted(
            (t for b in result["books"].values() for t in b["trades"]),
            key=lambda t: t["exit_at"], reverse=True)[:60],
    }
