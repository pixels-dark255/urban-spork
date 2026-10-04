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
from arena import costs as costs_mod
from arena.strategies import STRATEGIES, LAST_ENTRY_MINUTE

SQUARE_OFF_MINUTE = 15 * 60 + 15   # 15:15 IST
SESSION_END_MINUTE = 15 * 60 + 30
BAR_MINUTES = 5
MAX_TRADES_KEPT = 300
MAX_DAYS_KEPT = 250

LIQUID_NIFTY = [
    "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS", "TCS.NS",
    "SBIN.NS", "AXISBANK.NS", "ITC.NS", "BHARTIARTL.NS", "TATASTEEL.NS",
]

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

DEFAULT_CONFIG = {
    "enabled": True,
    "daily_capital": 10_000.0,
    "capital_mode": "reset",        # "reset" daily, or "compound"
    "symbols": LIQUID_NIFTY[:6],
    "strategies": list(STRATEGIES.keys()),
    "risk": DEFAULT_RISK,
    "costs": {},
}


# ---------------------------------------------------------------------------
# Config + state
# ---------------------------------------------------------------------------

def normalise_config(raw: dict | None, base: dict | None = None) -> dict:
    """Validate and fill a config. Unknown keys are dropped; bad values fall
    back to the current/base value rather than raising mid-trading-day."""
    cfg = {**DEFAULT_CONFIG, **(base or {})}
    cfg["risk"] = {**DEFAULT_RISK, **(base or {}).get("risk", {})}
    raw = raw or {}

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
            if isinstance(s, str) and s.strip():
                s = s.strip().upper()
                if not s.endswith((".NS", ".BO")):
                    s += ".NS"
                if s not in syms:
                    syms.append(s)
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
        cfg["costs"] = {k: float(v) for k, v in raw["costs"].items()
                        if k in costs_mod.DEFAULT_COSTS and isinstance(v, (int, float)) and v >= 0}
    return cfg


def new_state(config: dict | None = None) -> dict:
    return {
        "config": normalise_config(config),
        "books": {},
        "day": None,
        "day_settled": True,
        "last_bar": {},       # symbol -> ISO time of last processed bar
        "marks": {},          # symbol -> {"price", "at"}
        "history": [],        # one arena-level summary per settled day
        "created_at": timeutil.iso_now(),
    }


def _new_book(sid: str, capital: float) -> dict:
    return {
        "id": sid,
        "status": "active",
        "status_reason": None,
        "status_changed": None,
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


# ---------------------------------------------------------------------------
# Day lifecycle
# ---------------------------------------------------------------------------

def start_day(state: dict, date: str) -> None:
    if state["day"] and not state["day_settled"]:
        settle_day(state)
    cfg = state["config"]
    capital = cfg["daily_capital"]
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


def settle_day(state: dict) -> dict | None:
    """Close leftovers at their last mark, record the day, judge every book."""
    if not state["day"] or state["day_settled"]:
        return None
    cfg, risk = state["config"], state["config"]["risk"]
    summary = {"date": state["day"], "books": {}}
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
        })
        book["day_history"] = book["day_history"][-MAX_DAYS_KEPT:]
        _judge(book, risk, cfg["daily_capital"], state["day"])
        summary["books"][sid] = {"net": day_net, "trades": book["day"]["trades"], "status": book["status"]}
    state["day_settled"] = True
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
    dd_limit = capital * risk["max_drawdown_pct"] / 100
    current_dd = book["stats"]["peak_net"] - book["stats"]["net"]

    if book["status"] == "active":
        reason = None
        if current_dd >= dd_limit:
            reason = f"drawdown ₹{current_dd:.0f} hit the {risk['max_drawdown_pct']:g}% limit"
        elif enough and (m["expectancy"] < 0 or (m["profit_factor"] is not None and m["profit_factor"] < 1)):
            reason = (f"last {m['trades']} trades lose money after costs "
                      f"(₹{m['expectancy']:.2f}/trade, PF {m['profit_factor']})")
        if reason:
            book.update(status="benched", status_reason=reason, status_changed=date)
    elif book["status"] == "benched":
        pf = m["profit_factor"]
        recovered = (m["trades"] >= min(risk["min_trades_to_judge"], risk["judge_window"])
                     and m["expectancy"] > 0 and pf is not None and pf >= risk["reinstate_profit_factor"]
                     and current_dd < dd_limit)
        if recovered:
            book.update(status="active",
                        status_reason=f"reinstated: shadow trades earning again (PF {pf})",
                        status_changed=date)


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


def process_bar(state: dict, sym: str, ts, row, prev, n_symbols: int) -> None:
    cfg, risk = state["config"], state["config"]["risk"]
    ccfg = costs_mod.merged(cfg["costs"])
    at = timeutil.to_iso(ts.to_pydatetime())
    state["marks"][sym] = {"price": float(row["Close"]), "at": at}
    minute = int(row["minute"])

    for sid in cfg["strategies"]:
        book = state["books"].get(sid)
        strat = STRATEGIES.get(sid)
        if book is None or strat is None:
            continue
        pos = book["positions"].get(sym)
        if pos:
            price, reason = _exit_decision(strat, row, prev, pos, minute)
            if reason:
                _close(state, book, sym, price, reason, at, ccfg)
        elif not book["day"]["stopped"]:
            _maybe_enter(book, strat, sym, row, prev, minute, at, risk, ccfg, n_symbols)
        _check_daily_loss(state, book, risk, at, ccfg)


def _exit_decision(strat, row, prev, pos, minute):
    o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
    stop, target = pos["stop"], pos["target"]
    if o <= stop:
        return o, "stop_loss_gap"
    if l <= stop:
        return stop, "stop_loss"          # checked before target: pessimistic
    if o >= target:
        return o, "target"
    if h >= target:
        return target, "target"
    if minute >= SQUARE_OFF_MINUTE:
        return c, "eod_square_off"
    why = strat.exit(row, prev, pos)
    if why:
        return c, f"signal: {why}"
    return None, None


def _maybe_enter(book, strat, sym, row, prev, minute, at, risk, ccfg, n_symbols):
    if minute > LAST_ENTRY_MINUTE or minute >= SQUARE_OFF_MINUTE:
        return
    day = book["day"]
    if (len(book["positions"]) >= risk["max_open_positions"] and not strat.benchmark) \
            or day["trades"] >= risk["max_trades_per_day"] \
            or day["per_symbol"].get(sym, 0) >= strat.max_trades_per_symbol_per_day:
        return
    sig = strat.entry(row, prev)
    if not sig:
        return

    fill = costs_mod.fill_price("BUY", float(row["Close"]), ccfg)
    stop, target = float(sig["stop"]), float(sig["target"])
    if not (math.isfinite(stop) and math.isfinite(target)) or stop >= fill or target <= fill:
        return
    equity = book["day_start_equity"]
    if strat.benchmark:
        qty = int((equity / n_symbols) // fill)
    else:
        qty_risk = int((equity * risk["risk_per_trade_pct"] / 100) // (fill - stop))
        qty_cap = int((equity * risk["max_position_pct"] / 100) // fill)
        qty = min(qty_risk, qty_cap)
    est_buy_charges = costs_mod.leg_charges("BUY", fill * max(qty, 1), ccfg)["total"]
    qty = min(qty, int((book["cash"] - est_buy_charges) // fill))
    if qty < 1:
        day["skipped"]["too_expensive"] += 1
        return
    if not strat.benchmark:
        reward = (target - fill) * qty
        if reward < risk["min_reward_to_cost"] * costs_mod.round_trip_cost_estimate(fill, qty, ccfg):
            day["skipped"]["costs_exceed_target"] += 1
            return

    charges = costs_mod.leg_charges("BUY", fill * qty, ccfg)
    book["cash"] -= fill * qty + charges["total"]
    book["positions"][sym] = {
        "symbol": sym, "qty": qty, "entry_price": round(fill, 4), "entry_at": at,
        "stop": round(stop, 4), "target": round(target, 4),
        "buy_charges": charges["total"], "entry_reason": sig.get("reason", ""),
    }
    day["trades"] += 1
    day["per_symbol"][sym] = day["per_symbol"].get(sym, 0) + 1


def _close(state, book, sym, price, reason, at, ccfg=None):
    ccfg = ccfg or costs_mod.merged(state["config"]["costs"])
    pos = book["positions"].pop(sym, None)
    if not pos:
        return None
    fill = costs_mod.fill_price("SELL", float(price), ccfg)
    proceeds = fill * pos["qty"]
    charges = costs_mod.leg_charges("SELL", proceeds, ccfg)
    book["cash"] += proceeds - charges["total"]
    gross = (fill - pos["entry_price"]) * pos["qty"]
    total_charges = pos["buy_charges"] + charges["total"]
    net = gross - charges["total"] - pos["buy_charges"]
    trade = {
        "strategy": book["id"], "symbol": sym, "qty": pos["qty"],
        "entry_at": pos["entry_at"], "entry_price": round(pos["entry_price"], 2),
        "exit_at": at or timeutil.iso_now(), "exit_price": round(fill, 2),
        "gross": round(gross, 2), "charges": round(total_charges, 2), "net": round(net, 2),
        "entry_reason": pos.get("entry_reason", ""), "exit_reason": reason,
        "shadow": book["status"] == "benched",
    }
    book["trades"].append(trade)
    book["trades"] = book["trades"][-MAX_TRADES_KEPT:]
    st = book["stats"]
    st["trades"] += 1
    st["net"] = round(st["net"] + net, 2)
    st["charges"] = round(st["charges"] + total_charges, 2)
    if net > 0:
        st["wins"] += 1
        st["gross_win"] = round(st["gross_win"] + net, 2)
    else:
        st["gross_loss"] = round(st["gross_loss"] - net, 2)
    st["peak_net"] = max(st["peak_net"], st["net"])
    st["max_drawdown"] = round(max(st["max_drawdown"], st["peak_net"] - st["net"]), 2)
    book["day"]["net"] = round(book["day"]["net"] + net, 2)
    return trade


def _equity(state, book) -> float:
    eq = book["cash"]
    for sym, pos in book["positions"].items():
        mark = state["marks"].get(sym, {}).get("price") or pos["entry_price"]
        eq += mark * pos["qty"]
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
                "profit_factor": None, "avg_win": None, "avg_loss": None}
    wins = [t["net"] for t in trades if t["net"] > 0]
    losses = [-t["net"] for t in trades if t["net"] <= 0]
    gw, gl = sum(wins), sum(losses)
    return {
        "trades": n,
        "net": round(sum(t["net"] for t in trades), 2),
        "win_rate": round(100 * len(wins) / n, 1),
        "expectancy": round(sum(t["net"] for t in trades) / n, 2),
        "profit_factor": round(gw / gl, 2) if gl > 0 else (None if gw == 0 else 99.0),
        "avg_win": round(gw / len(wins), 2) if wins else None,
        "avg_loss": round(gl / len(losses), 2) if losses else None,
    }


def leaderboard(state: dict) -> list[dict]:
    cfg = state["config"]
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
        rows.append({
            "id": sid, "name": strat.name, "benchmark": strat.benchmark,
            "status": book["status"], "status_reason": book["status_reason"],
            "net_total": st["net"], "charges_total": st["charges"],
            "trades": st["trades"],
            "win_rate": round(100 * st["wins"] / st["trades"], 1) if st["trades"] else None,
            "expectancy": round(st["net"] / st["trades"], 2) if st["trades"] else None,
            "profit_factor": allm["profit_factor"] if st["trades"] <= MAX_TRADES_KEPT else (
                round(st["gross_win"] / st["gross_loss"], 2) if st["gross_loss"] else None),
            "max_drawdown": st["max_drawdown"],
            "days": st["days"], "green_days": st["green_days"],
            "recent": recent,
            "today": {"net": book["day"]["net"], "trades": book["day"]["trades"],
                      "open_positions": len(book["positions"]),
                      "equity": round(_equity(state, book), 2),
                      "stopped": book["day"]["stop_reason"]},
            "vs_benchmark": (round(st["net"] - bench_net, 2)
                             if bench_net is not None and not strat.benchmark else None),
        })
    rows.sort(key=lambda r: (r["benchmark"], -r["net_total"]))
    return rows


def verdict(state: dict) -> dict:
    """One-line answer to 'is anything working?'."""
    board = [r for r in leaderboard(state) if not r["benchmark"]]
    if not board or all(r["trades"] == 0 for r in board):
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
                "text": f"{best['name']} leads: ₹{best['net_total']:,.0f} net after costs over "
                        f"{best['trades']} trades, beating the benchmark by ₹{best['vs_benchmark']:,.0f}."}
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
            out.append({**pos, "strategy": sid, "mark": mark,
                        "unrealised": round((mark - pos["entry_price"]) * pos["qty"], 2) if mark else None})
    return out


# ---------------------------------------------------------------------------
# Historical replay
# ---------------------------------------------------------------------------

def replay(config: dict, enriched: dict[str, pd.DataFrame], days: int = 30) -> dict:
    """Run the exact live code path over past sessions. Each session: start,
    feed every bar, settle. Returns a fresh state (never touches the live one)."""
    state = new_state(config)
    state["config"]["enabled"] = True
    sessions = sorted({s for df in enriched.values() if df is not None and not df.empty
                       for s in df["session"].unique()})
    sessions = sessions[-int(days):]
    for s in sessions:
        start_day(state, s)
        process_bars(state, enriched, now=None)
        settle_day(state)
    state["replay"] = {"sessions": sessions, "symbols": sorted(enriched.keys()),
                       "ran_at": timeutil.iso_now()}
    return state
