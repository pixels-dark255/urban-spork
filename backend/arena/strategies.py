"""
The competing intraday strategies.

Each strategy looks at ONE completed 5-minute bar (plus the bar before it)
whose indicator columns were precomputed by `enrich()`, and answers:
  - flat:      BUY or SHORT (with a stop and a target) or nothing
  - in a trade: EXIT or nothing
Stops, targets, the end-of-day square-off and the daily loss limit are
enforced by the engine, not here, so no strategy can skip them.

All indicators are causal (they only use bars up to and including the
current one), so the historical replay has no lookahead.

Shorting: intraday (MIS) short selling is allowed in India, and a long-only
book can only lose in a falling market. A strategy shorts by returning
{"side": "SHORT", "stop": <above entry>, "target": <below entry>}. The
original six stay long-only so their history remains comparable; the newer
strategies trade both directions.

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


# 15:30 IST, in minutes. Only used when enrich() is called without a market
# profile, which is the NSE-shaped path the arena started life with.
NSE_SESSION_END_MINUTE = 15 * 60 + 30


def _sessionise(df, market, window):
    """Label each bar with the active session it belongs to, and with how
    far it is from that session's start and end.

    Without a market profile this is the original behaviour: a session is a
    calendar date in IST and the session ends at 15:30. With one, a session
    is the overlap of market hours and the user's window, which can start
    at any hour and cross midnight - so "how long until the close" has to
    be measured against that session's own end, not against a clock time.
    """
    from arena import markets as markets_mod     # imported late: markets imports us

    if market is None:
        df["session"] = [t.date().isoformat() for t in df.index]
        df["minute"] = [t.hour * 60 + t.minute for t in df.index]
        df["mins_left"] = NSE_SESSION_END_MINUTE - df["minute"]
        first = df.groupby("session")["minute"].transform("min")
        df["sess_minute"] = df["minute"] - first
        return df

    window = window or market.default_window()
    tz = market.tz
    local = df.index.tz_convert(tz)
    df["minute"] = [t.hour * 60 + t.minute for t in local]

    lo, hi = df.index[0], df.index[-1]
    import datetime as _dt
    from zoneinfo import ZoneInfo as _ZoneInfo
    wtz = _ZoneInfo(window["tz"])
    first_date = (lo.to_pydatetime().astimezone(wtz).date() - _dt.timedelta(days=2))
    last_date = (hi.to_pydatetime().astimezone(wtz).date() + _dt.timedelta(days=1))
    spans = []
    for iso in markets_mod.sessions_in_range(market, window, first_date, last_date):
        bounds = markets_mod.session_bounds(market, window, iso)
        if bounds:
            spans.append((iso, bounds[0], bounds[1]))

    sess, sess_min, left = [], [], []
    for ts in df.index:
        moment = ts.to_pydatetime()
        hit = None
        for iso, a, b in spans:
            if a <= moment <= b:
                hit = (iso, a, b)
                break
        if hit is None:
            sess.append(None)
            sess_min.append(float("nan"))
            left.append(float("nan"))
        else:
            iso, a, b = hit
            sess.append(iso)
            sess_min.append(int((moment - a).total_seconds() // 60))
            left.append(int((b - moment).total_seconds() // 60))
    df["session"] = sess
    df["sess_minute"] = sess_min
    df["mins_left"] = left
    # Bars outside every active session are dropped rather than kept with a
    # null session: nothing downstream should ever see a bar the user's
    # window excluded, and a stray one would quietly widen the opening range.
    return df[df["session"].notna()].copy()


def enrich(bars: pd.DataFrame, market=None, window: dict | None = None,
           scale: float = 1.0) -> pd.DataFrame:
    """Add every indicator column the strategies read.

    Input: OHLCV with a tz-aware index. `market` and `window` decide what a
    session is (see _sessionise); `scale` converts quoted prices into the
    book's currency, which is how a yen-quoted FX pair is traded in a
    dollar book without anything downstream knowing about FX.
    """
    if bars is None or bars.empty:
        return pd.DataFrame()
    df = bars[["Open", "High", "Low", "Close", "Volume"]].copy().dropna(subset=["Close"])
    if df.empty:
        return df
    idx = df.index
    tz_name = str(getattr(market, "tz", None) or "Asia/Kolkata")
    if idx.tz is None:
        idx = idx.tz_localize(tz_name if market is not None else "Asia/Kolkata")
    df.index = idx.tz_convert("Asia/Kolkata" if market is None else market.tz)

    if scale and scale != 1.0:
        for col in ("Open", "High", "Low", "Close"):
            df[col] = df[col] * float(scale)

    df = _sessionise(df, market, window)
    if df.empty:
        return df

    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"].fillna(0)
    # Yahoo reports zero volume for every FX pair. Treating that as "no
    # trading happened" would silently disable every volume-gated strategy
    # and hand them a VWAP of NaN; the honest reading is that the feed has
    # no volume, so the session's TWAP stands in for VWAP and the volume
    # average is explicitly undefined rather than zero.
    df["no_volume"] = bool(float(vol.sum()) <= 0)

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
    # The lower band has no consumer yet - the breakout strategy trades the
    # upper band and exits on the middle - but a band set missing a side is
    # a trap for the next mean-reversion strategy, which would reach for
    # bb_low and silently get None.
    df["bb_mid"], df["bb_up"], df["bb_low"] = mid, mid + 2 * sd, mid - 2 * sd
    no_volume = bool(df["no_volume"].iloc[0])
    # NaN, not 0: a volume average of zero would make "Volume >= 1.5x
    # average" trivially true on a feed that reports no volume at all, and
    # every volume breakout would fire on every bar.
    df["vol_avg20"] = (np.nan if no_volume else vol.rolling(20, min_periods=10).mean())

    g = df.groupby("session", sort=False)
    df["bar_no"] = g.cumcount()
    if no_volume:
        # TWAP: the running mean of the typical price so far this session.
        # It answers the same question VWAP does - "is price above or below
        # where it has been trading today" - without needing volume.
        typical = (high + low + close) / 3
        df["vwap"] = typical.groupby(df["session"]).transform(
            lambda x: x.expanding(min_periods=1).mean())
    else:
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

    # --- inputs for the second wave of strategies (all causal) ---
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    # Spread of price around VWAP so far this session (expanding std).
    dev = close - df["vwap"]
    df["vwap_sd"] = dev.groupby(df["session"]).transform(lambda x: x.expanding(min_periods=6).std())
    # Swing levels of the PREVIOUS 5 bars (excludes the current bar).
    df["swing_low5"] = low.rolling(5, min_periods=3).min().shift(1)
    df["swing_high5"] = high.rolling(5, min_periods=3).max().shift(1)
    # Previous session's last close and this session's open.
    last_close = close.groupby(df["session"], sort=False).last()
    prev_sess = pd.Series(last_close.shift(1).values, index=last_close.index)
    df["prev_session_close"] = df["session"].map(prev_sess)
    df["session_open"] = df["Open"].groupby(df["session"]).transform("first")
    df["day_high"] = high.groupby(df["session"]).cummax()
    df["day_low"] = low.groupby(df["session"]).cummin()
    # The close 30 minutes into the session, known from then on. Measured
    # from the session's own start: "09:45" means nothing to a market that
    # opens at 19:00 IST, or to a window the user moved.
    bar30 = df["sess_minute"].between(25, 29, inclusive="both")
    c30 = close.where(bar30).groupby(df["session"]).transform("max")
    df["close_0945"] = c30.where(df["sess_minute"] >= 25)
    st_line, st_dir = _supertrend(high, low, close, df["atr14"], 3.0)
    df["st_line"], df["st_dir"] = st_line, st_dir
    return df


def _supertrend(high, low, close, atr, mult):
    """Classic SuperTrend on the given ATR. Direction +1 = uptrend (line is
    support below price), -1 = downtrend (line is resistance above)."""
    n = len(close)
    line = np.full(n, np.nan)
    direction = np.zeros(n)
    hl2 = ((high + low) / 2).to_numpy()
    a = atr.to_numpy()
    c = close.to_numpy()
    upper_prev = lower_prev = np.nan
    d = 1
    for i in range(n):
        if not np.isfinite(a[i]):
            continue
        upper = hl2[i] + mult * a[i]
        lower = hl2[i] - mult * a[i]
        if np.isfinite(upper_prev) and c[i - 1] <= upper_prev:
            upper = min(upper, upper_prev)
        if np.isfinite(lower_prev) and c[i - 1] >= lower_prev:
            lower = max(lower, lower_prev)
        if np.isfinite(upper_prev):
            if d == 1 and c[i] < lower_prev:
                d = -1
            elif d == -1 and c[i] > upper_prev:
                d = 1
        line[i] = lower if d == 1 else upper
        direction[i] = d
        upper_prev, lower_prev = upper, lower
    return pd.Series(line, index=close.index), pd.Series(direction, index=close.index)


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
    # True when the strategy normally requires a volume confirmation. On a
    # market whose feed carries no volume it still trades, without that
    # confirmation, and says so in its name rather than pretending the two
    # are the same strategy.
    volume_gated = False

    def display_name(self, has_volume: bool = True) -> str:
        if self.volume_gated and not has_volume:
            return f"{self.name} · no-volume variant"
        return self.name

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

    # On a feed with no volume (every Yahoo FX pair) the VWAP is a session
    # TWAP and the volume filter simply cannot be evaluated. Dropping the
    # filter is honest - the strategy is then a plain TWAP reclaim, and the
    # leaderboard labels it as such - whereas keeping it would compare
    # volume against NaN and never trade.
    volume_gated = True

    def entry(self, row, prev):
        no_vol = bool(row.get("no_volume", False))
        if row["bar_no"] < 3 or not _ok(row["vwap"], prev["vwap"], row["atr14"]):
            return None
        if not no_vol and not _ok(row["vol_avg20"]):
            return None
        crossed = prev["Close"] <= prev["vwap"] and row["Close"] > row["vwap"]
        if not crossed:
            return None
        if not no_vol and row["Volume"] < row["vol_avg20"]:
            return None
        price = row["Close"]
        stop = min(row["Low"], row["vwap"]) - 0.5 * row["atr14"]
        if stop >= price:
            return None
        return {"stop": stop, "target": price + 2 * (price - stop),
                "reason": "reclaimed TWAP" if no_vol else "reclaimed VWAP on volume"}

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

    volume_gated = True

    def entry(self, row, prev):
        no_vol = bool(row.get("no_volume", False))
        if row["bar_no"] < 3 or not _ok(row["bb_up"]):
            return None
        if not no_vol and not _ok(row["vol_avg20"]):
            return None
        if row["Close"] <= row["bb_up"]:
            return None
        if not no_vol and row["Volume"] < 1.5 * row["vol_avg20"]:
            return None
        stop, target = self.atr_levels(row["Close"], row["atr14"], 1.5, 3.0)
        return {"stop": stop, "target": target,
                "reason": "breakout above upper band" if no_vol
                          else "volume breakout above upper band"}

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


# ---------------------------------------------------------------------------
# Second wave: both directions, wider stops, fewer trades. The first six
# lost mostly because a long-only book can't make money in a falling market
# and because 5-minute noise kept hitting tight stops.
# ---------------------------------------------------------------------------

class OrbBothWays(Strategy):
    id = "orb_both"
    name = "Opening-range breakout (long + short)"
    description = ("Buys a close above the first-15-minute high, or shorts a close below its low. "
                   "Stop at the other side of the range (max 1.5%), target 2x the risk.")
    max_trades_per_symbol_per_day = 1

    def entry(self, row, prev):
        if row["bar_no"] < OPENING_RANGE_BARS or not _ok(row["or_high"], row["or_low"], prev["Close"]):
            return None
        price = row["Close"]
        if price > row["or_high"] >= prev["Close"]:
            stop = max(row["or_low"], price * 0.985)
            return {"stop": stop, "target": price + 2 * (price - stop), "reason": "broke above opening range"}
        if price < row["or_low"] <= prev["Close"]:
            stop = min(row["or_high"], price * 1.015)
            return {"side": "SHORT", "stop": stop, "target": price - 2 * (stop - price),
                    "reason": "broke below opening range"}
        return None


class SuperTrendFlip(Strategy):
    id = "supertrend"
    name = "SuperTrend (10, 3) flip"
    description = ("Trend-following with a 3-ATR trailing line: goes long when the trend flips up, "
                   "short when it flips down. The line is the stop; exits on the opposite flip.")

    def entry(self, row, prev):
        if row["bar_no"] < 2 or not _ok(row["st_line"], row["atr14"]) or prev["st_dir"] == row["st_dir"]:
            return None
        price = row["Close"]
        if row["st_dir"] == 1 and row["st_line"] < price:
            stop = row["st_line"]
            return {"stop": stop, "target": price + 2 * (price - stop), "reason": "SuperTrend flipped up"}
        if row["st_dir"] == -1 and row["st_line"] > price:
            stop = row["st_line"]
            return {"side": "SHORT", "stop": stop, "target": price - 2 * (stop - price),
                    "reason": "SuperTrend flipped down"}
        return None

    def exit(self, row, prev, position):
        want = -1 if position.get("side") == "SHORT" else 1
        if _ok(row["st_dir"]) and row["st_dir"] == -want:
            return "SuperTrend flipped against the trade"
        return None


class VwapBandReversion(Strategy):
    id = "vwap_bands"
    name = "VWAP band mean-reversion"
    description = ("Fades stretched moves: buys when price is 2 standard deviations below VWAP and turns up "
                   "(RSI under 35), shorts the mirror case. Target is VWAP itself.")

    def entry(self, row, prev):
        if row["bar_no"] < 6 or not _ok(row["vwap"], row["vwap_sd"], row["rsi14"], row["atr14"]) or row["vwap_sd"] <= 0:
            return None
        price, vwap, sd = row["Close"], row["vwap"], row["vwap_sd"]
        if prev["Close"] < vwap - 2 * sd and price > prev["Close"] and row["rsi14"] < 35:
            stop = min(row["Low"], prev["Low"]) - 0.5 * row["atr14"]
            if stop < price < vwap:
                return {"stop": stop, "target": vwap, "reason": "2σ below VWAP, turning up"}
        if prev["Close"] > vwap + 2 * sd and price < prev["Close"] and row["rsi14"] > 65:
            stop = max(row["High"], prev["High"]) + 0.5 * row["atr14"]
            if stop > price > vwap:
                return {"side": "SHORT", "stop": stop, "target": vwap, "reason": "2σ above VWAP, turning down"}
        return None


class TrendPullback(Strategy):
    id = "trend_pullback"
    name = "Trend pullback to EMA 21"
    description = ("Only trades with the day's trend (price vs VWAP, EMA 21 vs EMA 50): waits for a dip "
                   "to the 21-EMA and buys the bounce, or shorts the rally in a downtrend. Stop beyond the "
                   "last swing, target 2x risk.")

    def entry(self, row, prev):
        if row["bar_no"] < 6 or not _ok(row["ema21"], row["ema50"], row["vwap"], row["swing_low5"], row["swing_high5"]):
            return None
        price = row["Close"]
        up = price > row["vwap"] and row["ema21"] > row["ema50"]
        down = price < row["vwap"] and row["ema21"] < row["ema50"]
        if up and prev["Low"] <= prev["ema21"] and price > row["ema21"] and price > prev["High"]:
            stop = min(row["swing_low5"], row["Low"])
            if stop < price:
                return {"stop": stop, "target": price + 2 * (price - stop), "reason": "bounced off EMA21 in uptrend"}
        if down and prev["High"] >= prev["ema21"] and price < row["ema21"] and price < prev["Low"]:
            stop = max(row["swing_high5"], row["High"])
            if stop > price:
                return {"side": "SHORT", "stop": stop, "target": price - 2 * (stop - price),
                        "reason": "rejected at EMA21 in downtrend"}
        return None


class GapFade(Strategy):
    id = "gap_fade"
    name = "Gap fade (gap-fill)"
    description = ("When a stock opens 0.6%+ away from yesterday's close and the first 15 minutes fail to "
                   "extend the gap, trades back toward yesterday's close. Stop beyond the day's extreme.")
    max_trades_per_symbol_per_day = 1

    def entry(self, row, prev):
        if not (OPENING_RANGE_BARS <= row["bar_no"] <= 9):
            return None
        if not _ok(row["prev_session_close"], row["session_open"], row["or_high"], row["or_low"], row["atr14"]):
            return None
        pc, op, price = row["prev_session_close"], row["session_open"], row["Close"]
        gap = (op - pc) / pc
        if gap >= 0.006 and price < row["or_low"]:
            stop = row["day_high"] + 0.2 * row["atr14"]
            if stop > price > pc:
                return {"side": "SHORT", "stop": stop, "target": pc, "reason": f"gap up {gap:.1%} failing"}
        if gap <= -0.006 and price > row["or_high"]:
            stop = row["day_low"] - 0.2 * row["atr14"]
            if stop < price < pc:
                return {"stop": stop, "target": pc, "reason": f"gap down {gap:.1%} recovering"}
        return None


class IntradayMomentum(Strategy):
    id = "intraday_momentum"
    name = "Intraday momentum (first 30 min → last hour)"
    description = ("Based on research that the first half-hour's move tends to continue into the close: "
                   "at 14:15, if the day is up 0.5%+ by 09:45 and price is above VWAP, buy (short the mirror "
                   "case) and hold to the square-off.")
    max_trades_per_symbol_per_day = 1

    # "14:15" was 75 minutes before the NSE close. Measured that way it
    # keeps its meaning - the last hour and a quarter - on a market that
    # closes at 02:30 IST or in a window the user shortened.
    ENTRY_MINS_LEFT = 75

    def entry(self, row, prev):
        left = row.get("mins_left")
        if left is None or not (self.ENTRY_MINS_LEFT - 2 <= left <= self.ENTRY_MINS_LEFT + 2):
            return None
        if not _ok(row["prev_session_close"], row["close_0945"], row["vwap"], row["atr14"]):
            return None
        early = (row["close_0945"] - row["prev_session_close"]) / row["prev_session_close"]
        price = row["Close"]
        if early >= 0.005 and price > row["vwap"]:
            stop = min(row["vwap"], price - 1.5 * row["atr14"])
            return {"stop": stop, "target": price + 3 * (price - stop), "reason": f"first 30 min {early:+.1%}"}
        if early <= -0.005 and price < row["vwap"]:
            stop = max(row["vwap"], price + 1.5 * row["atr14"])
            return {"side": "SHORT", "stop": stop, "target": price - 3 * (stop - price),
                    "reason": f"first 30 min {early:+.1%}"}
        return None


STRATEGIES: dict[str, Strategy] = {s.id: s for s in [
    OpeningRangeBreakout(), VwapReclaim(), EmaCrossover(), RsiReversion(),
    BollingerBreakout(), LegacyScore(),
    OrbBothWays(), SuperTrendFlip(), VwapBandReversion(), TrendPullback(), GapFade(), IntradayMomentum(),
    BuyAtOpenBenchmark(),
]}

# The strategies that existed before the second wave. Arenas created back
# then have these in their config; anything newer is switched on for them
# once (see engine.adopt_new_strategies) - after that, the user's choice wins.
ORIGINAL_STRATEGY_IDS = ["orb", "vwap_reclaim", "ema_cross", "rsi_reversion", "bb_breakout", "score4", "benchmark"]


def catalog(has_volume: bool = True) -> list[dict]:
    return [{"id": s.id, "name": s.display_name(has_volume), "description": s.description,
             "benchmark": s.benchmark, "volume_gated": s.volume_gated,
             "both_sides": s.id not in ORIGINAL_STRATEGY_IDS}
            for s in STRATEGIES.values()]
