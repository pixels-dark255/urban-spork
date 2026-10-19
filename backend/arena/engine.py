"""
Arena engine - the simulation itself.

State is a plain JSON-able dict (so it can be stored as-is). Every strategy
gets its OWN book with the same starting capital and trades the same stocks
on the same bars, so the comparison is fair: the only difference between
two books is the strategy.

Daily life of a book:
  start_day      fresh capital (or carried equity in "compound" mode)
  process_bar    for each completed 5m bar, in time order across stocks:
                   1. if holding: stop / target / square-off / strategy exit
                   2. if flat: maybe enter (sized by risk, costs checked)
                   3. daily loss limit -> close everything, done for the day
  settle_day     close leftovers, record the day, judge the strategy

Survival rules (the realistic version of "only keep running if it makes
money"):
  - Daily loss limit: a book that loses N% in a day stops for that day.
  - Benching: once a strategy has enough trades to judge, it is benched if
    its recent trades lose money after costs (expectancy < 0 or profit
    factor < 1), or if its drawdown from peak exceeds the limit.
  - Benched strategies keep paper-trading in SHADOW mode, so a strategy
    that starts working again is reinstated on evidence, not on hope.
  - If every real strategy is benched, the arena reports that nothing
    currently earns - i.e. the would-be-live account would stay in cash.
Nothing here can guarantee a profit; these rules limit how much a bad
strategy can lose before it is taken out of rotation.

Fill model (stated so the numbers don't flatter):
  - entries fill at the signal bar's close, plus slippage
  - stops/targets are checked against the NEXT bars' high/low; if one bar
    touches both, the stop is assumed to have hit first (pessimistic)
  - a bar that opens through the stop fills at the open, not the stop
  - every leg pays brokerage, STT, exchange, SEBI, stamp duty and GST
"""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd

import timeutil
from arena import markets as markets_mod
from arena.strategies import STRATEGIES, ORIGINAL_STRATEGY_IDS

# NSE clock times, kept because other modules still read them and because
# they document the shape the arena was born with. The engine itself no
# longer uses them: cutoffs come from the market profile, measured against
# the end of whatever the active session turns out to be.
SQUARE_OFF_MINUTE = 15 * 60 + 15   # 15:15 IST
SESSION_END_MINUTE = 15 * 60 + 30
BAR_MINUTES = 5
MAX_TRADES_KEPT = 300
MAX_DAYS_KEPT = 250

LIQUID_NIFTY = [
    "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", "TCS.NS",
    "SBIN.NS", "AXISBANK.NS", "ITC.NS", "BHARTIARTL.NS", "TATASTEEL.NS",
]

def market_of(state_or_cfg: dict):
    """The market profile this arena trades. Defaults to the NSE, so a
    state written before markets existed keeps behaving exactly as it did."""
    cfg = state_or_cfg.get("config", state_or_cfg)
    return markets_mod.get(cfg.get("market"))


def _window_of(cfg: dict) -> dict:
    return cfg.get("window") or market_of(cfg).default_window()


def _scale_for(state: dict, sym: str) -> float:
    """What a quoted price was multiplied by to put it in the book's
    currency. Needed by the forex cost model, whose spread is quoted in
    pips of the native pair."""
    fx = state.get("fx") or {}
    val = (fx.get("symbol_scale") or {}).get(sym)
    try:
        val = float(val)
    except (TypeError, ValueError):
        return 1.0
    return val if val > 0 else 1.0


def quantise(market, qty: float) -> float:
    """Round a size DOWN to what the market actually trades: whole shares
    for equities, fractional units for forex and crypto."""
    step = float(getattr(market, "qty_step", 1.0) or 1.0)
    if qty <= 0:
        return 0.0
    if step >= 1:
        return float(int(qty // step) * step)
    # Floating point: 0.3/1e-6 is 299999.99..., which would floor a hair low.
    units = math.floor(round(qty / step, 6))
    return round(units * step, 10)


DEFAULT_RISK = {
    "risk_per_trade_pct": 1.0,      # of the day's starting equity
    "max_position_pct": 50.0,       # max capital in one position
    "max_open_positions": 2,
    "max_trades_per_day": 10,
    "daily_loss_limit_pct": 2.0,
    "max_drawdown_pct": 10.0,       # of capital, from the book's peak
    "min_trades_to_judge": 20,
    "judge_window": 30,             # most recent N trades are judged
    "reinstate_profit_factor": 1.1,
    "min_reward_to_cost": 2.0,      # skip trades whose target can't beat costs
}

INT_RISK_KEYS = {"max_open_positions", "max_trades_per_day", "min_trades_to_judge", "judge_window"}

MAX_LEVERAGE = 5.0

DEFAULT_CONFIG = {
    "enabled": True,
    "market": markets_mod.DEFAULT_MARKET,
    "daily_capital": 10_000.0,      # always in INR, whatever the market
    "capital_mode": "reset",        # "reset" daily, or "compound"
    "symbols": LIQUID_NIFTY[:6],
    "strategies": list(STRATEGIES.keys()),
    "risk": DEFAULT_RISK,
    "costs": {},
    "window": None,                 # None means the market's own session
    "leverage": 1.0,
}


# ---------------------------------------------------------------------------
# Config + state
# ---------------------------------------------------------------------------

def normalise_config(raw: dict | None, base: dict | None = None,
                     market_id: str | None = None) -> dict:
    """Validate and fill a config. Unknown keys are dropped; bad values fall
    back to the current/base value rather than raising mid-trading-day."""
    cfg = {**DEFAULT_CONFIG, **(base or {})}
    cfg["risk"] = {**DEFAULT_RISK, **(base or {}).get("risk", {})}
    raw = raw or {}

    # The market is fixed when the arena is created - one arena per market,
    # so switching it would mean reinterpreting a whole history of trades
    # in the wrong currency and on the wrong calendar. An explicit
    # `market_id` (which the API always passes, taken from the storage key)
    # therefore outranks anything in the request body; `raw` is only
    # consulted when a state is being built from scratch.
    chosen = market_id or raw.get("market") or cfg.get("market") or markets_mod.DEFAULT_MARKET
    cfg["market"] = chosen if markets_mod.is_market_id(chosen) else markets_mod.DEFAULT_MARKET
    market = markets_mod.get(cfg["market"])
    if not (base or {}).get("symbols") and not raw.get("symbols"):
        cfg["symbols"] = list(market.default_symbols)
    # Same rule for the stake: a market's own default applies only when the
    # arena is being created. Once the user has set a figure it is theirs,
    # and a later change to the default must not overwrite it.
    if not (base or {}).get("daily_capital") and raw.get("daily_capital") is None:
        cfg["daily_capital"] = float(market.default_capital_inr)
    cfg["window"] = markets_mod.normalise_window(
        raw.get("window"), market, base=(base or {}).get("window"))
    lev = raw.get("leverage", cfg.get("leverage", 1.0))
    if isinstance(lev, (int, float)) and not isinstance(lev, bool):
        cfg["leverage"] = min(MAX_LEVERAGE, max(1.0, float(lev))) if market.allow_leverage else 1.0
    else:
        cfg["leverage"] = 1.0

    if isinstance(raw.get("enabled"), bool):
        cfg["enabled"] = raw["enabled"]
    cap = raw.get("daily_capital")
    if isinstance(cap, (int, float)) and 500 <= cap <= 10_000_000:
        cfg["daily_capital"] = float(cap)
    if raw.get("capital_mode") in ("reset", "compound"):
        cfg["capital_mode"] = raw["capital_mode"]
    if isinstance(raw.get("symbols"), list):
        syms = []
        for s in raw["symbols"]:
            if isinstance(s, str):
                norm = market.normalise_symbol(s)
                if norm and norm not in syms:
                    syms.append(norm)
        if syms:
            cfg["symbols"] = syms[:25]
    if isinstance(raw.get("strategies"), list):
        ids = [s for s in raw["strategies"] if s in STRATEGIES]
        if "benchmark" not in ids:
            ids.append("benchmark")   # always compare against the yardstick
        cfg["strategies"] = ids
    for k, v in (raw.get("risk") or {}).items():
        if k in DEFAULT_RISK and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            cfg["risk"][k] = max(1, int(v)) if k in INT_RISK_KEYS else float(v)
    if isinstance(raw.get("costs"), dict):
        allowed = market.costs.defaults
        cfg["costs"] = {k: float(v) for k, v in raw["costs"].items()
                        if k in allowed and isinstance(v, (int, float))
                        and not isinstance(v, bool) and v >= 0}
    return cfg


def new_state(config: dict | None = None) -> dict:
    state = _new_state(config)
    state["config"].setdefault("known_strategies", list(STRATEGIES))
    return state


def _new_state(config: dict | None = None) -> dict:
    return {
        "config": normalise_config(config),
        "books": {},
        "day": None,
        "day_settled": True,
        "last_bar": {},       # symbol -> ISO time of last processed bar
        "marks": {},          # symbol -> {"price", "at"}
        "history": [],        # one arena-level summary per settled day
        # How far the arena has been settled. The catch-up walks forward from
        # here, so a server that slept for days still replays every session
        # it missed instead of skipping straight to today.
        "last_settled_day": None,
        "last_tick_at": None,
        # What the last tick actually saw: which symbols returned bars and
        # which failed. Without this, a Yahoo outage and a sleeping server
        # look identical from the UI - both just stop updating.
        "last_tick": None,
        "caught_up_last": 0,
        # FX rates captured at the start of the current session. Recorded
        # rather than looked up live, so the rupee figures for a session
        # never drift after the fact.
        "fx": None,
        "created_at": timeutil.iso_now(),
    }


def _new_book(sid: str, capital: float) -> dict:
    return {
        "id": sid,
        "status": "active",
        "status_reason": None,
        "status_changed": None,
        # stats.net at the moment of benching. Further drawdown is measured
        # from here, not from the all-time peak - see _judge.
        "bench_net": None,
        "cash": capital,
        "day_start_equity": capital,
        "positions": {},
        "day": _blank_day(),
        "trades": [],
        "stats": {"trades": 0, "wins": 0, "net": 0.0, "gross_win": 0.0, "gross_loss": 0.0,
                  "charges": 0.0, "peak_net": 0.0, "max_drawdown": 0.0,
                  "days": 0, "green_days": 0},
        "day_history": [],
    }


def _blank_day() -> dict:
    return {"trades": 0, "net": 0.0, "stopped": False, "stop_reason": None,
            "per_symbol": {}, "skipped": {"too_expensive": 0, "costs_exceed_target": 0}}


def inr_per_unit(state: dict) -> float | None:
    """What one unit of the book's currency is worth in rupees."""
    market = market_of(state)
    if market.quote_currency == "INR":
        return 1.0
    fx = state.get("fx") or {}
    rate = fx.get("inr_per_unit")
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return None
    return rate if rate > 0 else None


def capital_native(state: dict) -> float:
    """The arena's rupee capital expressed in the book's currency.

    With no usable FX rate the rupee figure is used unconverted and the
    session records `fx.incomplete`, so the UI can say the rupee column is
    an assumption. Refusing to trade would be worse: the alternative is an
    arena that silently does nothing whenever Yahoo's FX feed hiccups.
    """
    rupees = float(state["config"]["daily_capital"])
    rate = inr_per_unit(state)
    return rupees / rate if rate else rupees


# ---------------------------------------------------------------------------
# Day lifecycle
# ---------------------------------------------------------------------------

def adopt_new_strategies(state: dict) -> list[str]:
    """Switch on strategies added to the app since this arena was created -
    once. `known_strategies` remembers what the user has already been
    offered, so unticking one later sticks. Returns the ids switched on."""
    cfg = state["config"]
    known = cfg.get("known_strategies")
    if known is None:
        known = list(ORIGINAL_STRATEGY_IDS)
    added = []
    for sid in STRATEGIES:
        if sid not in known:
            known.append(sid)
            if sid not in cfg["strategies"]:
                cfg["strategies"].append(sid)
                added.append(sid)
    cfg["known_strategies"] = known
    # Keep the yardstick last so it reads as the last row everywhere.
    if "benchmark" in cfg["strategies"]:
        cfg["strategies"] = [x for x in cfg["strategies"] if x != "benchmark"] + ["benchmark"]
    if added and state.get("day") and not state.get("day_settled"):
        # Mid-session: new strategies join with today's capital.
        for sid in added:
            state["books"].setdefault(sid, _new_book(sid, cfg["daily_capital"]))
    return added


def start_day(state: dict, date: str, caught_up: bool = False,
              fx: dict | None = None) -> None:
    if state["day"] and not state["day_settled"]:
        settle_day(state, caught_up=caught_up)
    adopt_new_strategies(state)
    cfg = state["config"]
    if fx is not None:
        state["fx"] = fx
    # Capital is always chosen in rupees; the book trades in the market's
    # currency, so it is converted once, at the session's recorded rate.
    capital = capital_native(state)
    for sid in cfg["strategies"]:
        book = state["books"].setdefault(sid, _new_book(sid, capital))
        equity = book["cash"]  # positions are always flat between days
        if cfg["capital_mode"] == "reset" or equity <= 0:
            equity = capital
        book["cash"] = equity
        book["day_start_equity"] = equity
        book["positions"] = {}
        book["day"] = _blank_day()
    state["day"] = date
    state["day_settled"] = False


def settle_day(state: dict, caught_up: bool = False) -> dict | None:
    """Close leftovers at their last mark, record the day, judge every book.

    `caught_up` marks a session the server slept through and replayed after
    the fact. The engine and the bars are identical either way, so the
    numbers are the same - but the UI says so rather than implying someone
    watched it happen.
    """
    if not state["day"] or state["day_settled"]:
        return None
    cfg, risk = state["config"], state["config"]["risk"]
    fx = state.get("fx") or {}
    summary = {"date": state["day"], "books": {}, "caught_up": bool(caught_up),
               "market": cfg.get("market", markets_mod.DEFAULT_MARKET),
               # The rate this session's rupee figures were computed at.
               # Kept per day so an old session is never restated at
               # today's rate.
               "fx_rate": fx.get("inr_per_unit"),
               "currency": market_of(state).quote_currency}
    for sid, book in state["books"].items():
        if sid not in cfg["strategies"]:
            continue
        for sym in list(book["positions"]):
            mark = state["marks"].get(sym, {}).get("price") or book["positions"][sym]["entry_price"]
            _close(state, book, sym, mark, "eod_square_off", state["marks"].get(sym, {}).get("at"))
        day_net = round(book["cash"] - book["day_start_equity"], 2)
        traded = book["day"]["trades"] > 0
        st = book["stats"]
        if traded:
            st["days"] += 1
            st["green_days"] += 1 if day_net > 0 else 0
        book["day_history"].append({
            "date": state["day"], "net": day_net, "trades": book["day"]["trades"],
            "equity_end": round(book["cash"], 2), "status": book["status"],
            "stopped": book["day"]["stop_reason"], "skipped": book["day"]["skipped"],
            "caught_up": bool(caught_up),
            "net_inr": markets_mod.to_inr(day_net, fx),
            "fx_rate": fx.get("inr_per_unit"),
        })
        book["day_history"] = book["day_history"][-MAX_DAYS_KEPT:]
        _judge(book, risk, capital_native(state), state["day"])
        summary["books"][sid] = {"net": day_net, "trades": book["day"]["trades"],
                                 "status": book["status"],
                                 "net_inr": markets_mod.to_inr(day_net, fx)}
    state["day_settled"] = True
    state["last_settled_day"] = state["day"]
    state["history"].append(summary)
    state["history"] = state["history"][-MAX_DAYS_KEPT:]
    return summary


def _judge(book: dict, risk: dict, capital: float, date: str) -> None:
    """Bench losers, reinstate recovered shadows. Benchmarks are exempt."""
    strat = STRATEGIES.get(book["id"])
    if strat is None or strat.benchmark:
        return
    m = window_metrics(book["trades"][-int(risk["judge_window"]):])
    enough = book["stats"]["trades"] >= risk["min_trades_to_judge"]
    # `capital` arrives in the book's own currency - see settle_day - so the
    # drawdown limit is compared like with like.
    dd_limit = capital * risk["max_drawdown_pct"] / 100
    current_dd = book["stats"]["peak_net"] - book["stats"]["net"]

    if book["status"] == "active":
        reason = None
        if current_dd >= dd_limit:
            reason = (f"drawdown {current_dd:,.2f} hit the "
                      f"{risk['max_drawdown_pct']:g}% limit")
        elif enough and (m["expectancy"] < 0 or (m["profit_factor"] is not None and m["profit_factor"] < 1)):
            reason = (f"last {m['trades']} trades lose money after costs "
                      f"({m['expectancy']:.2f}/trade, PF {m['profit_factor']})")
        if reason:
            # Remember where the book stood when it was benched. Reinstatement
            # measures further decline from HERE, not from the all-time peak:
            # a book benched for drawdown is by definition still near its
            # trough, so requiring peak - net < limit would have demanded it
            # win back most of the loss while confined to shadow trading.
            # That is a bench it could essentially never return from.
            book.update(status="benched", status_reason=reason, status_changed=date,
                        bench_net=book["stats"]["net"])
    elif book["status"] == "benched":
        pf = m["profit_factor"]
        bench_net = book.get("bench_net")
        dd_since_bench = ((bench_net - book["stats"]["net"]) if bench_net is not None
                          else current_dd)
        # A window with no losses has an undefined profit factor (None), not
        # a bad one. Requiring `pf >= threshold` would then refuse to
        # reinstate the best possible recovery - every shadow trade a winner.
        pf_ok = m.get("no_losses") or (pf is not None and pf >= risk["reinstate_profit_factor"])
        recovered = (m["trades"] >= min(risk["min_trades_to_judge"], risk["judge_window"])
                     and m["expectancy"] > 0 and pf_ok
                     and dd_since_bench < dd_limit)
        if recovered:
            book.update(status="active",
                        status_reason=("reinstated: shadow trades earning again "
                                       f"(PF {pf if pf is not None else 'no losses'})"),
                        status_changed=date, bench_net=None)


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------

def process_bars(state: dict, enriched: dict[str, pd.DataFrame],
                 now: dt.datetime | None = None) -> int:
    """Feed every not-yet-processed, completed bar of the current day, in
    time order across all stocks. Returns how many bars were processed."""
    day = state["day"]
    if not day or state["day_settled"] or not state["config"]["enabled"]:
        return 0
    cutoff = None
    if now is not None:
        cutoff = timeutil.ensure_aware(now) - dt.timedelta(minutes=BAR_MINUTES)

    queue = []
    for sym, df in enriched.items():
        if df is None or df.empty or sym not in state["config"]["symbols"]:
            continue
        last = timeutil.parse_utc(state["last_bar"].get(sym))
        sess = (df["session"] == day).to_numpy()
        positions = [i for i in range(len(df)) if sess[i]]
        for i in positions:
            ts = df.index[i]
            if last is not None and ts <= last:
                continue
            if cutoff is not None and ts > cutoff:
                continue          # still forming - Yahoo's last bar is live
            queue.append((ts, sym, i))
    queue.sort(key=lambda x: (x[0], x[1]))

    n_symbols = max(1, len(state["config"]["symbols"]))
    for ts, sym, i in queue:
        df = enriched[sym]
        row = df.iloc[i]
        prev = df.iloc[i - 1] if i > 0 else row
        process_bar(state, sym, ts, row, prev, n_symbols)
        state["last_bar"][sym] = timeutil.to_iso(ts.to_pydatetime())
    return len(queue)


def _mins_left(row) -> float:
    """Minutes until this session's square-off deadline. NaN-safe: a bar
    with no session end behaves as if the close is far away."""
    try:
        left = float(row["mins_left"])
    except (KeyError, TypeError, ValueError):
        return float("inf")
    return left if left == left else float("inf")


def process_bar(state: dict, sym: str, ts, row, prev, n_symbols: int) -> None:
    cfg, risk = state["config"], state["config"]["risk"]
    market = market_of(state)
    ccfg = market.costs.merged(cfg["costs"])
    at = timeutil.to_iso(ts.to_pydatetime())
    state["marks"][sym] = {"price": float(row["Close"]), "at": at}
    left = _mins_left(row)

    for sid in cfg["strategies"]:
        book = state["books"].get(sid)
        strat = STRATEGIES.get(sid)
        if book is None or strat is None:
            continue
        pos = book["positions"].get(sym)
        if pos:
            price, reason = _exit_decision(strat, row, prev, pos, left, market)
            if reason:
                _close(state, book, sym, price, reason, at, ccfg)
        elif not book["day"]["stopped"]:
            _maybe_enter(book, strat, sym, row, prev, left, at, risk, ccfg,
                         n_symbols, market, state)
        _check_daily_loss(state, book, risk, at, ccfg)


def _exit_decision(strat, row, prev, pos, mins_left, market):
    o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
    stop, target = pos["stop"], pos["target"]
    if pos.get("side", "LONG") == "SHORT":
        # Mirror image: the stop sits ABOVE the entry, the target below.
        if o >= stop:
            return o, "stop_loss_gap"
        if h >= stop:
            return stop, "stop_loss"      # checked before target: pessimistic
        if o <= target:
            return o, "target"
        if l <= target:
            return target, "target"
    else:
        if o <= stop:
            return o, "stop_loss_gap"
        if l <= stop:
            return stop, "stop_loss"      # checked before target: pessimistic
        if o >= target:
            return o, "target"
        if h >= target:
            return target, "target"
    if mins_left <= market.square_off_before_end:
        return c, "eod_square_off"
    why = strat.exit(row, prev, pos)
    if why:
        return c, f"signal: {why}"
    return None, None


def _maybe_enter(book, strat, sym, row, prev, mins_left, at, risk, ccfg,
                 n_symbols, market, state):
    # No new entries in the run-up to the close, and none at all once the
    # square-off has begun. Both are measured from the end of THIS session,
    # so a market closing at 02:30 IST gets the same treatment as the NSE.
    if mins_left < market.entry_cutoff_before_end or mins_left <= market.square_off_before_end:
        return
    day = book["day"]
    if (len(book["positions"]) >= risk["max_open_positions"] and not strat.benchmark) \
            or day["trades"] >= risk["max_trades_per_day"] \
            or day["per_symbol"].get(sym, 0) >= strat.max_trades_per_symbol_per_day:
        return
    sig = strat.entry(row, prev)
    if not sig:
        return

    # Shorts (intraday MIS) sell first and buy back later. Margin is kept at
    # 1x - the full notional is set aside - so a short can't size bigger
    # than a long would, and leverage never flatters the comparison.
    side = "SHORT" if sig.get("side") == "SHORT" else "LONG"
    entry_leg = "SELL" if side == "SHORT" else "BUY"
    scale = _scale_for(state, sym)
    fill = market.costs.fill_price(entry_leg, float(row["Close"]), ccfg, sym, scale)
    stop, target = float(sig["stop"]), float(sig["target"])
    if not (math.isfinite(stop) and math.isfinite(target)):
        return
    if side == "LONG" and (stop >= fill or target <= fill):
        return
    if side == "SHORT" and (stop <= fill or target >= fill):
        return
    risk_per_share = abs(fill - stop)
    if risk_per_share <= 0 or fill <= 0:
        return
    equity = book["day_start_equity"]
    # Leverage (forex/crypto only, capped) multiplies the notional a book
    # may commit, never the risk budget: a leveraged book takes the same
    # money per trade, it is just allowed to hold more of the instrument.
    leverage = float(state["config"].get("leverage", 1.0) or 1.0)
    buying_power = equity * leverage
    if strat.benchmark:
        qty = buying_power / max(1, n_symbols) / fill
    else:
        qty_risk = (equity * risk["risk_per_trade_pct"] / 100) / risk_per_share
        qty_cap = (buying_power * risk["max_position_pct"] / 100) / fill
        qty = min(qty_risk, qty_cap)
    qty = quantise(market, qty)
    step = float(getattr(market, "qty_step", 1.0) or 1.0)
    est_charges = market.costs.leg_charges(entry_leg, fill * max(qty, step), ccfg, sym)["total"]
    qty = quantise(market, min(qty, (book["cash"] * leverage - est_charges) / fill))
    if qty < step:
        day["skipped"]["too_expensive"] += 1
        return
    if not strat.benchmark:
        reward = abs(target - fill) * qty
        round_trip = market.costs.round_trip_cost_estimate(fill, qty, ccfg, sym, scale)
        if reward < risk["min_reward_to_cost"] * round_trip:
            day["skipped"]["costs_exceed_target"] += 1
            return

    charges = market.costs.leg_charges(entry_leg, fill * qty, ccfg, sym)
    # The full notional leaves the cash balance even when leverage allows a
    # bigger one; `cash` can go negative in that case, which is exactly what
    # borrowing to hold a position looks like.
    book["cash"] -= fill * qty + charges["total"]
    book["positions"][sym] = {
        "symbol": sym, "side": side, "qty": qty, "entry_price": round(fill, 6), "entry_at": at,
        "stop": round(stop, 6), "target": round(target, 6),
        # name kept for stored state: it means "charges paid on the entry leg"
        "buy_charges": charges["total"], "entry_reason": sig.get("reason", ""),
    }
    day["trades"] += 1
    day["per_symbol"][sym] = day["per_symbol"].get(sym, 0) + 1


def _close(state, book, sym, price, reason, at, ccfg=None):
    market = market_of(state)
    ccfg = ccfg or market.costs.merged(state["config"]["costs"])
    pos = book["positions"].pop(sym, None)
    if not pos:
        return None
    side = pos.get("side", "LONG")
    exit_leg = "BUY" if side == "SHORT" else "SELL"
    scale = _scale_for(state, sym)
    fill = market.costs.fill_price(exit_leg, float(price), ccfg, sym, scale)
    charges = market.costs.leg_charges(exit_leg, fill * pos["qty"], ccfg, sym)
    if side == "SHORT":
        gross = (pos["entry_price"] - fill) * pos["qty"]
        # release the set-aside notional plus the short's profit/loss
        book["cash"] += pos["entry_price"] * pos["qty"] + gross - charges["total"]
    else:
        gross = (fill - pos["entry_price"]) * pos["qty"]
        book["cash"] += fill * pos["qty"] - charges["total"]
    total_charges = pos["buy_charges"] + charges["total"]
    net = gross - charges["total"] - pos["buy_charges"]
    trade = {
        "strategy": book["id"], "symbol": sym, "side": side, "qty": pos["qty"],
        "entry_at": pos["entry_at"], "entry_price": _price_round(market, pos["entry_price"]),
        "exit_at": at or timeutil.iso_now(), "exit_price": _price_round(market, fill),
        "gross": round(gross, 4), "charges": round(total_charges, 4), "net": round(net, 4),
        "entry_reason": pos.get("entry_reason", ""), "exit_reason": reason,
        "shadow": book["status"] == "benched",
        "currency": market.quote_currency,
        "net_inr": markets_mod.to_inr(net, state.get("fx")),
    }
    book["trades"].append(trade)
    book["trades"] = book["trades"][-MAX_TRADES_KEPT:]
    st = book["stats"]
    st["trades"] += 1
    # Rounded to 4dp, not 2: a forex book trades in dollars where a whole
    # session's profit can be a few cents, and 2dp would quantise it away.
    st["net"] = round(st["net"] + net, 4)
    st["charges"] = round(st["charges"] + total_charges, 4)
    if net > 0:
        st["wins"] += 1
        st["gross_win"] = round(st["gross_win"] + net, 4)
    else:
        st["gross_loss"] = round(st["gross_loss"] - net, 4)
    st["peak_net"] = max(st["peak_net"], st["net"])
    st["max_drawdown"] = round(max(st["max_drawdown"], st["peak_net"] - st["net"]), 4)
    book["day"]["net"] = round(book["day"]["net"] + net, 4)
    return trade


def _price_round(market, value: float) -> float:
    """FX quotes need 5 decimals; a rupee share price does not."""
    return round(float(value), 5 if getattr(market, "fractional", False) else 2)


def _position_value(pos: dict, mark: float) -> float:
    """What an open position is worth to the book at `mark`."""
    if pos.get("side", "LONG") == "SHORT":
        return pos["entry_price"] * pos["qty"] + (pos["entry_price"] - mark) * pos["qty"]
    return mark * pos["qty"]


def _unrealised(pos: dict, mark: float) -> float:
    sign = -1 if pos.get("side", "LONG") == "SHORT" else 1
    return sign * (mark - pos["entry_price"]) * pos["qty"]


def _equity(state, book) -> float:
    eq = book["cash"]
    for sym, pos in book["positions"].items():
        mark = state["marks"].get(sym, {}).get("price") or pos["entry_price"]
        eq += _position_value(pos, mark)
    return eq


def _check_daily_loss(state, book, risk, at, ccfg):
    if book["day"]["stopped"] or STRATEGIES[book["id"]].benchmark:
        return  # the yardstick holds the day by definition
    limit = book["day_start_equity"] * risk["daily_loss_limit_pct"] / 100
    if book["day_start_equity"] - _equity(state, book) >= limit:
        for sym in list(book["positions"]):
            _close(state, book, sym, state["marks"][sym]["price"], "daily_loss_limit", at, ccfg)
        book["day"]["stopped"] = True
        book["day"]["stop_reason"] = f"daily loss limit ({risk['daily_loss_limit_pct']:g}%) hit"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def window_metrics(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0, "net": 0.0, "win_rate": None, "expectancy": 0.0,
                "profit_factor": None, "no_losses": False,
                "avg_win": None, "avg_loss": None}
    wins = [t["net"] for t in trades if t["net"] > 0]
    losses = [-t["net"] for t in trades if t["net"] <= 0]
    gw, gl = sum(wins), sum(losses)
    # Profit factor is gross win / gross loss. With no losses that is a
    # division by zero, not a very large number: the old code returned 99.0
    # and the UI printed "99", which reads like a measured result rather
    # than "undefined". None plus a flag lets the UI show "-" honestly.
    return {
        "trades": n,
        "net": round(sum(t["net"] for t in trades), 4),
        "win_rate": round(100 * len(wins) / n, 1),
        "expectancy": round(sum(t["net"] for t in trades) / n, 2),
        "profit_factor": round(gw / gl, 2) if gl > 0 else None,
        "no_losses": gl == 0 and gw > 0,
        "avg_win": round(gw / len(wins), 2) if wins else None,
        "avg_loss": round(gl / len(losses), 2) if losses else None,
    }


def _today_row(state: dict, book: dict) -> dict:
    """Today's figures, with open positions marked to market."""
    equity = _equity(state, book)
    realised = book["day"]["net"]
    net = round(equity - book["day_start_equity"], 4)
    return {
        "net": net,                       # realised + unrealised
        "realised": round(realised, 4),
        "unrealised": round(net - realised, 4),
        "entries": book["day"]["trades"],  # positions OPENED today
        "trades": book["day"]["trades"],   # kept for older frontends
        "open_positions": len(book["positions"]),
        "equity": round(equity, 2),
        "stopped": book["day"]["stop_reason"],
    }


def leaderboard(state: dict) -> list[dict]:
    cfg = state["config"]
    market = market_of(state)
    fx = state.get("fx")
    bench = state["books"].get("benchmark")
    bench_net = bench["stats"]["net"] if bench else None
    rows = []
    for sid in cfg["strategies"]:
        book = state["books"].get(sid)
        strat = STRATEGIES.get(sid)
        if not book or not strat:
            continue
        st = book["stats"]
        allm = window_metrics(book["trades"])
        recent = window_metrics(book["trades"][-int(cfg["risk"]["judge_window"]):])
        today = _today_row(state, book)
        rows.append({
            "id": sid,
            # On a feed with no volume the volume-gated strategies trade
            # without their volume confirmation, so they are not quite the
            # same strategy and do not carry the same name.
            "name": strat.display_name(market.has_volume),
            "benchmark": strat.benchmark,
            "currency": market.quote_currency,
            "currency_symbol": market.currency_symbol,
            "net_total_inr": markets_mod.to_inr(book["stats"]["net"], fx),
            "status": book["status"], "status_reason": book["status_reason"],
            "net_total": st["net"], "charges_total": st["charges"],
            "trades": st["trades"],
            "win_rate": round(100 * st["wins"] / st["trades"], 1) if st["trades"] else None,
            "expectancy": round(st["net"] / st["trades"], 2) if st["trades"] else None,
            "profit_factor": allm["profit_factor"] if st["trades"] <= MAX_TRADES_KEPT else (
                round(st["gross_win"] / st["gross_loss"], 2) if st["gross_loss"] else None),
            "no_losses": allm.get("no_losses", False) if st["trades"] <= MAX_TRADES_KEPT else (
                st["gross_loss"] == 0 and st["gross_win"] > 0),
            "max_drawdown": st["max_drawdown"],
            "days": st["days"], "green_days": st["green_days"],
            "recent": recent,
            # today.net marks open positions to market. Using realised P&L
            # alone reported the benchmark - which holds all day by design -
            # as flat ₹0 with five positions open, which is simply not what
            # the book is worth. `entries` counts positions opened today;
            # `trades` at the top level counts CLOSED trades, and the two
            # were previously both labelled "trades" in the UI.
            "today": {**today, "net_inr": markets_mod.to_inr(today["net"], fx)},
            "vs_benchmark": (round(st["net"] - bench_net, 4)
                             if bench_net is not None and not strat.benchmark else None),
        })
    rows.sort(key=lambda r: (r["benchmark"], -r["net_total"]))
    return rows


def _money(state: dict, amount: float) -> str:
    """An amount in the book's currency, with the rupee equivalent beside
    it when the book is not already kept in rupees."""
    market = market_of(state)
    native = f"{market.currency_symbol}{amount:,.2f}" if market.fractional \
        else f"{market.currency_symbol}{amount:,.0f}"
    rupees = markets_mod.to_inr(amount, state.get("fx"))
    if market.quote_currency == "INR" or rupees is None:
        return native
    return f"{native} (₹{rupees:,.0f})"


def skipped_unaffordable(state: dict) -> int:
    """How many entry signals were dropped because the book could not buy a
    whole unit. On a market that trades whole shares this is the difference
    between "no signals" and "no money"."""
    total = 0
    for sid, book in state["books"].items():
        if sid not in state["config"]["strategies"]:
            continue
        total += book["day"]["skipped"].get("too_expensive", 0)
        for day in book["day_history"][-30:]:
            total += (day.get("skipped") or {}).get("too_expensive", 0)
    return total


def verdict(state: dict) -> dict:
    """One-line answer to 'is anything working?'."""
    board = [r for r in leaderboard(state) if not r["benchmark"]]
    if not board or all(r["trades"] == 0 for r in board):
        blocked = skipped_unaffordable(state)
        market = market_of(state)
        if blocked and not market.fractional:
            # The common way a US arena looks broken: ₹10,000 is about
            # $115, and one share of a mega-cap costs more than that. The
            # arena is working exactly as told; it just cannot afford a
            # single share. Saying so beats an empty leaderboard.
            native = capital_native(state)
            return {"level": "waiting",
                    "text": (f"No trades yet: {blocked} entry signals were skipped because "
                             f"₹{state['config']['daily_capital']:,.0f} "
                             f"({market.currency_symbol}{native:,.0f}) will not buy a whole share "
                             f"at these prices. Raise the capital for this arena, or pick "
                             f"lower-priced symbols.")}
        return {"level": "waiting", "text": "No trades yet - results appear after the first session."}
    active = [r for r in board if r["status"] == "active"]
    judged = [r for r in board if r["trades"] >= state["config"]["risk"]["min_trades_to_judge"]]
    winners = [r for r in judged if r["net_total"] > 0 and (r["vs_benchmark"] or 0) > 0]
    if not active:
        return {"level": "halted",
                "text": "Every strategy is benched: none is earning after costs right now. "
                        "A live account following these rules would be sitting in cash."}
    if winners:
        best = max(winners, key=lambda r: r["net_total"])
        return {"level": "good",
                "text": f"{best['name']} leads: {_money(state, best['net_total'])} net after costs "
                        f"over {best['trades']} trades, beating the benchmark by "
                        f"{_money(state, best['vs_benchmark'])}."}
    if not judged:
        return {"level": "early",
                "text": f"Too early to judge - each strategy needs "
                        f"{state['config']['risk']['min_trades_to_judge']} trades before its numbers mean anything."}
    return {"level": "none",
            "text": "No strategy has beaten the benchmark after costs yet."}


def open_positions(state: dict) -> list[dict]:
    out = []
    for sid, book in state["books"].items():
        if sid not in state["config"]["strategies"]:
            continue
        for sym, pos in book["positions"].items():
            mark = state["marks"].get(sym, {}).get("price")
            unreal = round(_unrealised(pos, mark), 4) if mark else None
            out.append({**pos, "strategy": sid, "mark": mark,
                        "unrealised": unreal,
                        "unrealised_inr": markets_mod.to_inr(unreal, state.get("fx")),
                        "currency": market_of(state).quote_currency})
    return out


# ---------------------------------------------------------------------------
# Historical replay
# ---------------------------------------------------------------------------

def replay(config: dict, enriched: dict[str, pd.DataFrame], days: int = 30,
           fx: dict | None = None) -> dict:
    """Run the exact live code path over past sessions. Each session: start,
    feed every bar, settle. Returns a fresh state (never touches the live one).

    The sessions come from the bars themselves, which `enrich` has already
    cut to the market's hours and the user's window - so a backtest replays
    exactly the sessions the live arena would have traded, no more.

    One FX rate is used for the whole replay rather than each session's
    own: Yahoo does not serve historical intraday FX alongside the bars,
    and silently inventing per-day rates would make a currency bet look
    like a strategy result.
    """
    state = new_state(config)
    state["config"]["enabled"] = True
    if fx is not None:
        state["fx"] = fx
    sessions = sorted({s for df in enriched.values() if df is not None and not df.empty
                       for s in df["session"].unique() if s})
    sessions = sessions[-int(days):]
    for s in sessions:
        start_day(state, s)
        process_bars(state, enriched, now=None)
        settle_day(state)
    state["replay"] = {"sessions": sessions, "symbols": sorted(enriched.keys()),
                       "market": state["config"].get("market"),
                       "currency": market_of(state).quote_currency,
                       "fx_rate": (fx or {}).get("inr_per_unit"),
                       "ran_at": timeutil.iso_now()}
    return state


# The fill penalty is assumed, not measured, and on a small intraday target
# it eats a large share of the move. Running the same replay at a fraction
# of it separates "has an edge" from "has an edge only if fills are free".
# Expressed as multiples of the market's own assumption, because that cost
# is a percentage on equities and crypto but a spread in pips on forex.
SLIPPAGE_MULTIPLES = [0.0, 0.5, 1.0]


def _scaled_cost_overrides(market, base_costs: dict, multiple: float) -> dict:
    """The market's fill-cost settings scaled by `multiple`.

    Equities and crypto have one slippage percentage. Forex has none - its
    cost IS the spread - so every per-pair spread is scaled instead, which
    is the same experiment expressed in that market's units.
    """
    costs = {**(base_costs or {})}
    defaults = market.costs.defaults
    key = market.costs.slippage_key
    if key and key in defaults:
        costs[key] = float(costs.get(key, defaults[key])) * multiple
    else:
        for k, v in defaults.items():
            if k.startswith("spread_pips_"):
                costs[k] = float(costs.get(k, v)) * multiple
    return costs


def replay_slippage_sweep(config: dict, enriched: dict[str, pd.DataFrame],
                          days: int = 30,
                          multiples: list[float] | None = None,
                          fx: dict | None = None) -> dict:
    """The same sessions replayed at each fill-cost assumption."""
    multiples = multiples if multiples is not None else SLIPPAGE_MULTIPLES
    market = markets_mod.get((config or {}).get("market"))
    rows: dict[str, dict] = {}
    order: list[dict] = []
    for multiple in multiples:
        cfg = {**config,
               "costs": _scaled_cost_overrides(market, config.get("costs"), multiple)}
        result = replay(cfg, enriched, days=days, fx=fx)
        label = f"{multiple:g}×"
        order.append({"multiple": multiple, "label": label,
                      "describes": _sweep_label(market, multiple)})
        for row in leaderboard(result):
            entry = rows.setdefault(row["id"], {"id": row["id"], "name": row["name"],
                                                "benchmark": row["benchmark"], "by_slippage": {}})
            entry["by_slippage"][label] = {"net": row["net_total"], "trades": row["trades"],
                                           "net_inr": row.get("net_total_inr")}
    return {
        "levels": order,
        "currency": market.quote_currency,
        "rows": sorted(rows.values(), key=lambda r: (r["benchmark"], r["name"])),
        "note": ("Same bars and the same engine at each level - only the assumed "
                 "fill cost changes. A strategy that loses at 0× has no edge at all; "
                 "one that wins only at 0× cannot survive real fills."),
    }


def _sweep_label(market, multiple: float) -> str:
    if multiple == 0:
        return "perfect fills, no cost"
    key = market.costs.slippage_key
    if key and key in market.costs.defaults:
        return f"{market.costs.defaults[key] * 100 * multiple:g}% a side"
    base = market.costs.defaults.get("spread_pips_default", 1.5)
    return f"{base * multiple:g} pips typical spread"
