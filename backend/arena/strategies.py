"""
The competing intraday strategies.

Each strategy looks at ONE completed 5-minute bar (plus the bar before it)
whose indicator columns were precomputed by `enrich()`, and answers:
  - flat:      BUY (with a stop and a target) or nothing
  - in a trade: EXIT or nothing
Stops, targets, the end-of-day square-off and the daily loss limit are
enforced by the engine, not here, so no strategy can skip them.

All indicators are causal (they only use bars up to and including the
current one), so the historical replay has no lookahead.

Long-only for this first version: shorting intraday is allowed in MIS, but
it adds borrow/auction edge cases that would muddy a first comparison.
Add short variants once the long-only results are understood.

Adding a strategy: subclass Strategy, implement entry() (and exit() if it
has a signal-based exit), add it to STRATEGIES. Nothing else changes.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

OPENING_RANGE_BARS = 3          # 3 x 5m = the first 15 minutes
LAST_ENTRY_MINUTE = 14 * 60 + 45  # no new entries after 14:45 IST


# ---------------------------------------------------------------------------
# Indicators, computed once per symbol per refresh
# ---------------------------------------------------------------------------

def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    out = out.where(~((loss == 0) & (gain > 0)), 100.0)
    out = out.where(~((loss == 0) & (gain == 0)), 50.0)
    return out


def enrich(bars: pd.DataFrame) -> pd.DataFrame:
    """Add every indicator column the strategies read. Input: OHLCV with a
    tz-aware index (Yahoo returns Asia/Kolkata for NSE/BSE)."""
    if bars is None or bars.empty:
        return pd.DataFrame()
    df = bars[["Open", "High", "Low", "Close", "Volume"]].copy().dropna(subset=["Close"])
    if df.empty:
        return df
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize("Asia/Kolkata")
    df.index = idx.tz_convert("Asia/Kolkata")

    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"].fillna(0)
    df["session"] = [t.date().isoformat() for t in df.index]
    df["minute"] = [t.hour * 60 + t.minute for t in df.index]

    df["ema9"] = close.ewm(span=9, adjust=False).mean()
    df["ema21"] = close.ewm(span=21, adjust=False).mean()
    df["sma5"] = close.rolling(5, min_periods=5).mean()
    df["sma20"] = close.rolling(20, min_periods=20).mean()
    df["rsi14"] = _rsi(close, 14)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.rolling(14, min_periods=5).mean()
    mid = close.rolling(20, min_periods=20).mean()
    sd = close.rolling(20, min_periods=20).std()
    df["bb_mid"], df["bb_up"] = mid, mid + 2 * sd
    df["vol_avg20"] = vol.rolling(20, min_periods=10).mean()

    g = df.groupby("session", sort=False)
    df["bar_no"] = g.cumcount()
    pv = (close * vol).groupby(df["session"]).cumsum()
    cv = vol.groupby(df["session"]).cumsum()
    df["vwap"] = (pv / cv.replace(0, np.nan)).fillna(close)
    # Opening range only becomes known once its last bar has closed - before
    # that the columns are NaN so no strategy can peek at it.
    in_or = df["bar_no"] < OPENING_RANGE_BARS
    or_high = high.where(in_or).groupby(df["session"]).transform("max")
    or_low = low.where(in_or).groupby(df["session"]).transform("min")
    known = df["bar_no"] >= OPENING_RANGE_BARS - 1
    df["or_high"] = or_high.where(known)
    df["or_low"] = or_low.where(known)
    return df


def _ok(*vals) -> bool:
    return all(v is not None and isinstance(v, (int, float)) and math.isfinite(v) for v in vals)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

class Strategy:
    id = "base"
    name = "Base"
    description = ""
    benchmark = False          # benchmarks are never benched, only compared
    max_trades_per_symbol_per_day = 2

    def entry(self, row, prev) -> dict | None:
        """Return {"stop", "target", "reason"} to buy at this bar's close."""
        return None

    def exit(self, row, prev, position) -> str | None:
        """Return a reason string to exit at this bar's close."""
        return None

    @staticmethod
    def atr_levels(price, atr, stop_mult, target_mult):
        if not _ok(price, atr) or atr <= 0:
            atr = price * 0.004
        return price - stop_mult * atr, price + target_mult * atr


class OpeningRangeBreakout(Strategy):
    id = "orb"
    name = "Opening-range breakout"
    description = ("Buys when price closes above the high of the first 15 minutes. "
                   "Stop under the range low (max 1.5%), target 2x the risk.")
    max_trades_per_symbol_per_day = 1

    def entry(self, row, prev):
        if not _ok(row["or_high"], row["or_low"], prev["Close"]):
            return None
        if row["bar_no"] < OPENING_RANGE_BARS or row["Close"] <= row["or_high"] or prev["Close"] > row["or_high"]:
            return None
        price = row["Close"]
        stop = max(row["or_low"], price * (1 - 0.015))
        if stop >= price:
            return None
        return {"stop": stop, "target": price + 2 * (price - stop), "reason": "closed above opening range"}


class VwapReclaim(Strategy):
    id = "vwap_reclaim"
    name = "VWAP reclaim"
    description = ("Buys when price crosses back above the day's VWAP with above-average volume. "
                   "Exits if it falls back below VWAP. Target 2x risk.")

    def entry(self, row, prev):
        if row["bar_no"] < 3 or not _ok(row["vwap"], prev["vwap"], row["vol_avg20"], row["atr14"]):
            return None
        crossed = prev["Close"] <= prev["vwap"] and row["Close"] > row["vwap"]
        if not crossed or row["Volume"] < row["vol_avg20"]:
            return None
        price = row["Close"]
        stop = min(row["Low"], row["vwap"]) - 0.5 * row["atr14"]
        if stop >= price:
            return None
        return {"stop": stop, "target": price + 2 * (price - stop), "reason": "reclaimed VWAP on volume"}

    def exit(self, row, prev, position):
        if _ok(row["vwap"]) and row["Close"] < row["vwap"] * 0.998:
            return "lost VWAP"
        return None


class EmaCrossover(Strategy):
    id = "ema_cross"
    name = "EMA 9/21 crossover"
    description = "Trend-following: buys when the 9-EMA crosses above the 21-EMA, exits on the cross back."

    def entry(self, row, prev):
        if row["bar_no"] < 2 or not _ok(row["ema9"], row["ema21"], prev["ema9"], prev["ema21"]):
            return None
        if prev["ema9"] <= prev["ema21"] and row["ema9"] > row["ema21"]:
            stop, target = self.atr_levels(row["Close"], row["atr14"], 1.5, 3.0)
            return {"stop": stop, "target": target, "reason": "EMA9 crossed above EMA21"}
        return None

    def exit(self, row, prev, position):
        if _ok(row["ema9"], row["ema21"]) and row["ema9"] < row["ema21"]:
            return "EMA9 crossed back below EMA21"
        return None


class RsiReversion(Strategy):
    id = "rsi_reversion"
    name = "RSI oversold bounce"
    description = ("Mean-reversion: buys when RSI(14) climbs back above 30 after being oversold, "
                   "exits once RSI recovers past 55.")

    def entry(self, row, prev):
        if row["bar_no"] < 3 or not _ok(row["rsi14"], prev["rsi14"]):
            return None
        if prev["rsi14"] < 30 <= row["rsi14"]:
            stop, target = self.atr_levels(row["Close"], row["atr14"], 1.5, 2.0)
            return {"stop": stop, "target": target, "reason": "RSI back above 30"}
        return None

    def exit(self, row, prev, position):
        if _ok(row["rsi14"]) and row["rsi14"] > 55:
            return "RSI recovered above 55"
        return None


class BollingerBreakout(Strategy):
    id = "bb_breakout"
    name = "Bollinger volume breakout"
    description = ("Momentum: buys a close above the upper Bollinger band on 1.5x average volume, "
                   "exits when price falls back to the middle band.")

    def entry(self, row, prev):
        if row["bar_no"] < 3 or not _ok(row["bb_up"], row["vol_avg20"]):
            return None
        if row["Close"] > row["bb_up"] and row["Volume"] >= 1.5 * row["vol_avg20"]:
            stop, target = self.atr_levels(row["Close"], row["atr14"], 1.5, 3.0)
            return {"stop": stop, "target": target, "reason": "volume breakout above upper band"}
        return None

    def exit(self, row, prev, position):
        if _ok(row["bb_mid"]) and row["Close"] < row["bb_mid"]:
            return "back below middle band"
        return None


class LegacyScore(Strategy):
    id = "score4"
    name = "4-signal score (original)"
    description = ("The app's original intraday rule: MA trend + RSI + VWAP + opening range, "
                   "buy at score >= +2, exit at <= -2. Stop -1.5%, target +2.5%.")

    @staticmethod
    def score(row) -> int | None:
        if not _ok(row["sma5"], row["sma20"], row["rsi14"], row["vwap"]):
            return None
        s = 1 if row["sma5"] > row["sma20"] else -1
        s += 1 if row["rsi14"] < 30 else (-1 if row["rsi14"] > 70 else 0)
        s += 1 if row["Close"] > row["vwap"] else -1
        if _ok(row["or_high"], row["or_low"]):
            s += 1 if row["Close"] > row["or_high"] else (-1 if row["Close"] < row["or_low"] else 0)
        return s

    def entry(self, row, prev):
        s, p = self.score(row), self.score(prev)
        if s is None or s < 2 or (p is not None and p >= 2):
            return None  # act on the transition into >=2, not every bar it stays there
        price = row["Close"]
        return {"stop": price * 0.985, "target": price * 1.025, "reason": f"score {s:+d}"}

    def exit(self, row, prev, position):
        s = self.score(row)
        if s is not None and s <= -2:
            return f"score {s:+d}"
        return None


class BuyAtOpenBenchmark(Strategy):
    id = "benchmark"
    name = "Benchmark: buy first bar, hold to close"
    description = ("Not a strategy - the yardstick. Buys every selected stock on the first bar and "
                   "sells at square-off. A strategy that can't beat this isn't adding anything.")
    benchmark = True
    max_trades_per_symbol_per_day = 1

    def entry(self, row, prev):
        if row["bar_no"] == 0:
            price = row["Close"]
            # Wide catastrophe stop only; the point is to hold the day.
            return {"stop": price * 0.9, "target": price * 1.5, "reason": "day open"}
        return None


STRATEGIES: dict[str, Strategy] = {s.id: s for s in [
    OpeningRangeBreakout(), VwapReclaim(), EmaCrossover(), RsiReversion(),
    BollingerBreakout(), LegacyScore(), BuyAtOpenBenchmark(),
]}


def catalog() -> list[dict]:
    return [{"id": s.id, "name": s.name, "description": s.description, "benchmark": s.benchmark}
            for s in STRATEGIES.values()]
