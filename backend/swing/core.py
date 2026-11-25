"""Core rotation: hold the engine while it is in an uptrend, else the safe.

The whole account is one decision repeated: at each daily close, is the
engine above its trend line? If yes hold the engine, if no hold the safe
asset. The next session's open is where that decision is acted on, never
the close that produced it (AGENTS.md §1.3).

Three things here are easy to get wrong and are done explicitly:

  delivery costs  Anything held overnight pays CNC charges, not MIS.
                  STT alone is eight times higher and is charged on both
                  legs. A rotation that looks fine on intraday rates can
                  be a loser on the real ones.

  whole shares    You cannot buy 0.4 of a share on the NSE. The leftover
                  stays in cash and is reported, because on a small
                  account the uninvested remainder is a real drag rather
                  than a rounding note.

  no free switch  Every rotation sells one holding and buys another, and
                  pays both legs. A rule that flips often is punished by
                  the arithmetic rather than by an opinion about it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from arena import costs

CASH = "CASH"


# ---------------------------------------------------------- trend rules

def sma_rule(window: int):
    def rule(close: pd.Series) -> pd.Series:
        line = close.rolling(window, min_periods=window).mean()
        return (close > line).where(line.notna())
    rule.label = "SMA%d" % window
    return rule


def ema_rule(span: int):
    def rule(close: pd.Series) -> pd.Series:
        line = close.ewm(span=span, adjust=False).mean()
        # An EMA has a value from bar one, which is not the same as having
        # `span` bars behind it. Held back so it cannot sign off on a
        # trend it has not seen.
        line = line.where(np.arange(len(close)) >= span - 1)
        return (close > line).where(line.notna())
    rule.label = "EMA%d" % span
    return rule


def cross_rule(fast: int, slow: int):
    def rule(close: pd.Series) -> pd.Series:
        f = close.rolling(fast, min_periods=fast).mean()
        s = close.rolling(slow, min_periods=slow).mean()
        return (f > s).where(f.notna() & s.notna())
    rule.label = "%d/%d cross" % (fast, slow)
    return rule


BASE_RULE = sma_rule(200)

# The neighbours the brief names. If the edge only exists at SMA200 it is
# not an edge, it is the one window that happened to be looked at.
NEIGHBOUR_RULES = [sma_rule(100), sma_rule(150), sma_rule(250),
                   ema_rule(200), cross_rule(50, 200)]


# ------------------------------------------------------------- the book

def _buy(cash: float, price: float, cfg: dict) -> tuple:
    """Spend cash on whole shares. Returns (qty, cash_left, charges)."""
    fill = costs.fill_price("BUY", price, cfg)
    if fill <= 0:
        return 0, cash, 0.0
    qty = int(cash // fill)
    while qty > 0:
        spend = qty * fill
        charge = costs.leg_charges("BUY", spend, cfg)["total"]
        if spend + charge <= cash:
            return qty, cash - spend - charge, charge
        qty -= 1          # charges pushed it over; buy one fewer
    return 0, cash, 0.0


def _sell(qty: int, price: float, cfg: dict) -> tuple:
    fill = costs.fill_price("SELL", price, cfg)
    proceeds = qty * fill
    charge = costs.leg_charges("SELL", proceeds, cfg)["total"]
    return proceeds - charge, charge


def run(bars: dict[str, pd.DataFrame], engine: str, safe: str,
        capital: float, rule=BASE_RULE, cost_multiple: float = 1.0,
        start=None, end=None) -> dict:
    """The rotation, day by day.

    `bars` maps ticker to a daily OHLC frame. `safe` may be CASH, which
    means holding rupees: no position, no cost, no return.
    """
    cfg = costs.delivery()
    if cost_multiple != 1.0:
        cfg = costs.delivery({"slippage_pct": cfg["slippage_pct"] * cost_multiple,
                              "brokerage_pct": cfg["brokerage_pct"] * cost_multiple,
                              "stt_buy_pct": cfg["stt_buy_pct"] * cost_multiple,
                              "stt_sell_pct": cfg["stt_sell_pct"] * cost_multiple,
                              "stamp_buy_pct": cfg["stamp_buy_pct"] * cost_multiple,
                              "exchange_pct": cfg["exchange_pct"] * cost_multiple})

    engine_bars = bars.get(engine)
    if engine_bars is None or engine_bars.empty:
        return {"ok": False, "reason": "no bars for engine %s" % engine}
    safe_bars = None if safe == CASH else bars.get(safe)
    if safe != CASH and (safe_bars is None or safe_bars.empty):
        return {"ok": False, "reason": "no bars for safe asset %s" % safe}

    index = engine_bars.index
    if safe_bars is not None:
        index = index.intersection(safe_bars.index)
    index = _window(index, start, end)
    if len(index) < 2:
        return {"ok": False, "reason": "not enough overlapping sessions"}

    hold_engine = rule(engine_bars["Close"]).reindex(index)

    holding, qty, cash = CASH, 0, float(capital)
    equity, switches, charges_paid, idle = [], 0, 0.0, []
    # What was held, and when. The equity curve alone cannot tell you
    # whether a flat stretch was a bad engine or a long spell in cash.
    bands: list[dict] = []

    for i, day in enumerate(index):
        if i > 0:
            want = hold_engine.iloc[i - 1]
            # An unknown trend (warm-up) is not a signal. Sit in the safe
            # asset rather than guessing the engine is fine.
            target = CASH if pd.isna(want) else (engine if want else safe)
            if target != holding:
                opens = _open_of(bars, holding, day)
                if holding != CASH and qty and opens is not None:
                    proceeds, charge = _sell(qty, opens, cfg)
                    cash += proceeds
                    charges_paid += charge
                    qty = 0
                buy_open = _open_of(bars, target, day)
                if target != CASH and buy_open:
                    qty, cash, charge = _buy(cash, buy_open, cfg)
                    charges_paid += charge
                    if qty == 0:
                        idle.append({"date": str(day.date()), "ticker": target,
                                     "price": round(float(buy_open), 2),
                                     "cash": round(cash, 2)})
                holding = target
                switches += 1
            if not bands or bands[-1]["ticker"] != holding:
                bands.append({"ticker": holding, "start": str(day.date()),
                              "end": str(day.date())})
            else:
                bands[-1]["end"] = str(day.date())

        mark = _close_of(bars, holding, day)
        value = cash + (qty * mark if holding != CASH and mark else 0.0)
        equity.append(value)

    series = pd.Series(equity, index=index)
    return {"ok": True, "engine": engine, "safe": safe,
            "rule": getattr(rule, "label", "rule"),
            "sessions": len(index), "switches": switches,
            "charges": round(charges_paid, 2),
            "equity": series, "cost_multiple": cost_multiple,
            "could_not_buy": idle, "holdings": bands,
            **summarise(series, capital)}


def _window(index, start, end):
    if start is not None:
        index = index[index.date >= start]
    if end is not None:
        index = index[index.date <= end]
    return index


def _open_of(bars, ticker, day):
    if ticker == CASH or ticker not in bars or day not in bars[ticker].index:
        return None
    return float(bars[ticker].loc[day, "Open"])


def _close_of(bars, ticker, day):
    if ticker == CASH or ticker not in bars or day not in bars[ticker].index:
        return None
    return float(bars[ticker].loc[day, "Close"])


# ------------------------------------------------------------- metrics

TRADING_DAYS = 252


def max_drawdown(equity: pd.Series) -> float:
    """Worst peak-to-trough fall of the equity curve, as a percentage."""
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float(((equity - peak) / peak).min() * 100.0)


def summarise(equity: pd.Series, capital: float) -> dict:
    if equity.empty:
        return {"return_pct": 0.0, "cagr_pct": 0.0, "max_drawdown_pct": 0.0,
                "return_over_drawdown": None, "final": capital}
    final = float(equity.iloc[-1])
    total = 100.0 * (final / capital - 1.0)
    years = len(equity) / TRADING_DAYS
    cagr = (100.0 * ((final / capital) ** (1 / years) - 1.0)) if years > 0 and final > 0 else 0.0
    dd = max_drawdown(equity)
    return {
        "return_pct": round(total, 3),
        "cagr_pct": round(cagr, 3),
        "max_drawdown_pct": round(dd, 3),
        # The number that matters: return earned per unit of pain. A
        # strategy returning 20% with a 40% drawdown is worse than one
        # returning 10% with a 5% drawdown, whatever the headline says.
        "return_over_drawdown": round(abs(cagr / dd), 3) if dd else None,
        "final": round(final, 2),
    }
