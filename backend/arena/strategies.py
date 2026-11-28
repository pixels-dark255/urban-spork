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

# The opening range, the warm-ups and the "30 minutes in" close are all
# periods of TIME. They used to be counted in bars, which silently meant
# 5-minute bars: on hourly bars `bar_no < 3` is three hours of warm-up and
# the 25-29 minute window for the half-hour close matches no bar at all,
# so two strategies would never fire and nobody would be told. Stated in
# minutes they mean the same thing at any bar size, and at 5m they resolve
# to exactly the bar counts they replaced.
OPENING_RANGE_MINUTES = 15
HALF_HOUR_MINUTES = 30
OPENING_RANGE_BARS = 3          # 3 x 5m, kept for anything still counting bars
LAST_ENTRY_MINUTE = 14 * 60 + 45  # no new entries after 14:45 IST


def bar_minutes(df) -> float:
    """How long one bar is, measured from the bars themselves.

    Taken from the gaps WITHIN a session, so the overnight gap between
    15:30 and the next 09:15 cannot be mistaken for a bar length. The
    median shrugs off a missing bar or a half-day.
    """
    if df is None or len(df) < 2:
        return 5.0
    gaps = df.index.to_series().diff().dt.total_seconds().div(60.0)
    if "session" in df:
        gaps = gaps.where(df["session"].eq(df["session"].shift(1)))
    gaps = gaps[gaps > 0]
    if gaps.empty:
        return 5.0
    return float(gaps.median())


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
    # The opening range is the first OPENING_RANGE_MINUTES of the session,
    # which on 5m bars is bars 0-2 exactly as before. On a bar longer than
    # the range itself it is the first bar, and the range is then that
    # bar's length rather than 15 minutes: a bar size cannot be finer than
    # its bars, and pretending otherwise is how a backtest invents data.
    minutes = bar_minutes(df)
    df["bar_minutes"] = minutes
    in_or = df["sess_minute"] < max(OPENING_RANGE_MINUTES, minutes)
    or_high = high.where(in_or).groupby(df["session"]).transform("max")
    or_low = low.where(in_or).groupby(df["session"]).transform("min")
    known = df["sess_minute"] >= max(OPENING_RANGE_MINUTES, minutes) - minutes
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
    # The last bar that closes at or before the half-hour mark, so on 5m
    # bars this is the 09:40-09:45 bar exactly as before. On a bar longer
    # than half an hour it is the first bar of the session.
    mark = max(HALF_HOUR_MINUTES - minutes, 0.0)
    bar30 = (df["sess_minute"] >= mark) & (df["sess_minute"] < mark + minutes)
    c30 = close.where(bar30).groupby(df["session"]).transform("max")
    df["close_0945"] = c30.where(df["sess_minute"] >= mark)
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

def _early(row, minutes: float) -> bool:
    """True while the session is younger than `minutes`.

    The warm-ups were written as bar counts, which meant 5-minute bars
    without ever saying so. In minutes they survive a change of bar size:
    "six bars" becomes "half an hour", which is what it always meant.
    """
    return float(row["sess_minute"]) < minutes


class Strategy:
    # Rule parameters, and the three values the funnel's neighbour gate
    # steps through (down, default, up). Defaults must equal the literals
    # they replaced: a parameterised strategy that trades differently from
    # the one whose results are on the leaderboard is a new strategy.
    #
    # Indicator windows (the 9 and 21 in ema9/ema21, the 20 in the
    # Bollinger bands) are NOT here. They are computed once in enrich() for
    # every strategy at once, so varying one per strategy would mean
    # re-enriching per variant. Gate 5 therefore covers the rule
    # parameters and not the indicator windows; see AGENTS.md §5.
    PARAMS: dict = {}
    NEIGHBOURS: dict = {}

    id = "base"
    name = "Base"
    description = ""
    benchmark = False          # benchmarks are never benched, only compared
    # Whether a running arena switches this on by itself. False means the
    # strategy is available to tick but is not pushed into books that are
    # already going.
    arena_default = True
    max_trades_per_symbol_per_day = 2
    # True when the strategy normally requires a volume confirmation. On a
    # market whose feed carries no volume it still trades, without that
    # confirmation, and says so in its name rather than pretending the two
    # are the same strategy.
    volume_gated = False

    def __init__(self, **overrides):
        unknown = sorted(set(overrides) - set(self.PARAMS))
        if unknown:
            raise ValueError("%s has no parameter(s): %s"
                             % (self.id, ", ".join(unknown)))
        self.params = {**self.PARAMS, **overrides}

    def p(self, name):
        return self.params[name]

    def variant(self, **overrides):
        """The same rule with one or more parameters moved."""
        return type(self)(**{**{k: v for k, v in self.params.items()
                                if k != "__defaults__"}, **overrides})

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
    PARAMS = {"stop_cap_pct": 0.015, "target_r": 2.0}
    NEIGHBOURS = {"stop_cap_pct": [0.010, 0.015, 0.020],
                  "target_r": [1.5, 2.0, 2.5]}
    id = "orb"
    name = "Opening-range breakout"
    description = ("Buys when price closes above the high of the first 15 minutes. "
                   "Stop under the range low (max 1.5%), target 2x the risk.")
    max_trades_per_symbol_per_day = 1

    def entry(self, row, prev):
        if not _ok(row["or_high"], row["or_low"], prev["Close"]):
            return None
        if _early(row, max(OPENING_RANGE_MINUTES, row["bar_minutes"])) or row["Close"] <= row["or_high"] or prev["Close"] > row["or_high"]:
            return None
        price = row["Close"]
        stop = max(row["or_low"], price * (1 - self.p("stop_cap_pct")))
        if stop >= price:
            return None
        return {"stop": stop,
                "target": price + self.p("target_r") * (price - stop),
                "reason": "closed above opening range"}


class VwapReclaim(Strategy):
    PARAMS = {"stop_atr": 0.5, "target_r": 2.0, "exit_slack": 0.998}
    NEIGHBOURS = {"stop_atr": [0.25, 0.5, 1.0], "target_r": [1.5, 2.0, 2.5],
                  "exit_slack": [0.996, 0.998, 0.999]}
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
        if _early(row, 15) or not _ok(row["vwap"], prev["vwap"], row["atr14"]):
            return None
        if not no_vol and not _ok(row["vol_avg20"]):
            return None
        crossed = prev["Close"] <= prev["vwap"] and row["Close"] > row["vwap"]
        if not crossed:
            return None
        if not no_vol and row["Volume"] < row["vol_avg20"]:
            return None
        price = row["Close"]
        stop = min(row["Low"], row["vwap"]) - self.p("stop_atr") * row["atr14"]
        if stop >= price:
            return None
        return {"stop": stop,
                "target": price + self.p("target_r") * (price - stop),
                "reason": "reclaimed TWAP" if no_vol else "reclaimed VWAP on volume"}

    def exit(self, row, prev, position):
        if _ok(row["vwap"]) and row["Close"] < row["vwap"] * self.p("exit_slack"):
            return "lost VWAP"
        return None


class EmaCrossover(Strategy):
    PARAMS = {"stop_atr": 1.5, "target_atr": 3.0}
    NEIGHBOURS = {"stop_atr": [1.0, 1.5, 2.0], "target_atr": [2.0, 3.0, 4.0]}
    id = "ema_cross"
    name = "EMA 9/21 crossover"
    description = "Trend-following: buys when the 9-EMA crosses above the 21-EMA, exits on the cross back."

    def entry(self, row, prev):
        if _early(row, 10) or not _ok(row["ema9"], row["ema21"], prev["ema9"], prev["ema21"]):
            return None
        if prev["ema9"] <= prev["ema21"] and row["ema9"] > row["ema21"]:
            stop, target = self.atr_levels(row["Close"], row["atr14"],
                                           self.p("stop_atr"), self.p("target_atr"))
            return {"stop": stop, "target": target, "reason": "EMA9 crossed above EMA21"}
        return None

    def exit(self, row, prev, position):
        if _ok(row["ema9"], row["ema21"]) and row["ema9"] < row["ema21"]:
            return "EMA9 crossed back below EMA21"
        return None


class RsiReversion(Strategy):
    PARAMS = {"rsi_floor": 30.0, "rsi_exit": 55.0,
              "stop_atr": 1.5, "target_atr": 2.0}
    NEIGHBOURS = {"rsi_floor": [25.0, 30.0, 35.0], "rsi_exit": [50.0, 55.0, 60.0],
                  "stop_atr": [1.0, 1.5, 2.0], "target_atr": [1.5, 2.0, 2.5]}
    id = "rsi_reversion"
    name = "RSI oversold bounce"
    description = ("Mean-reversion: buys when RSI(14) climbs back above 30 after being oversold, "
                   "exits once RSI recovers past 55.")

    def entry(self, row, prev):
        if _early(row, 15) or not _ok(row["rsi14"], prev["rsi14"]):
            return None
        floor = self.p("rsi_floor")
        if prev["rsi14"] < floor <= row["rsi14"]:
            stop, target = self.atr_levels(row["Close"], row["atr14"],
                                           self.p("stop_atr"), self.p("target_atr"))
            return {"stop": stop, "target": target,
                    "reason": "RSI back above %g" % floor}
        return None

    def exit(self, row, prev, position):
        if _ok(row["rsi14"]) and row["rsi14"] > self.p("rsi_exit"):
            return "RSI recovered above %g" % self.p("rsi_exit")
        return None


class BollingerBreakout(Strategy):
    PARAMS = {"vol_mult": 1.5, "stop_atr": 1.5, "target_atr": 3.0}
    NEIGHBOURS = {"vol_mult": [1.2, 1.5, 2.0], "stop_atr": [1.0, 1.5, 2.0],
                  "target_atr": [2.0, 3.0, 4.0]}
    id = "bb_breakout"
    name = "Bollinger volume breakout"
    description = ("Momentum: buys a close above the upper Bollinger band on 1.5x average volume, "
                   "exits when price falls back to the middle band.")

    volume_gated = True

    def entry(self, row, prev):
        no_vol = bool(row.get("no_volume", False))
        if _early(row, 15) or not _ok(row["bb_up"]):
            return None
        if not no_vol and not _ok(row["vol_avg20"]):
            return None
        if row["Close"] <= row["bb_up"]:
            return None
        if not no_vol and row["Volume"] < self.p("vol_mult") * row["vol_avg20"]:
            return None
        stop, target = self.atr_levels(row["Close"], row["atr14"],
                                       self.p("stop_atr"), self.p("target_atr"))
        return {"stop": stop, "target": target,
                "reason": "breakout above upper band" if no_vol
                          else "volume breakout above upper band"}

    def exit(self, row, prev, position):
        if _ok(row["bb_mid"]) and row["Close"] < row["bb_mid"]:
            return "back below middle band"
        return None


class LegacyScore(Strategy):
    PARAMS = {"entry_score": 2, "exit_score": -2,
              "stop_pct": 0.015, "target_pct": 0.025}
    NEIGHBOURS = {"entry_score": [1, 2, 3], "exit_score": [-1, -2, -3],
                  "stop_pct": [0.010, 0.015, 0.020],
                  "target_pct": [0.020, 0.025, 0.030]}
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
        want = self.p("entry_score")
        if s is None or s < want or (p is not None and p >= want):
            return None  # act on the transition into >=want, not every bar it stays there
        price = row["Close"]
        return {"stop": price * (1 - self.p("stop_pct")),
                "target": price * (1 + self.p("target_pct")),
                "reason": f"score {s:+d}"}

    def exit(self, row, prev, position):
        s = self.score(row)
        if s is not None and s <= self.p("exit_score"):
            return f"score {s:+d}"
        return None


class BuyAtOpenBenchmark(Strategy):
    PARAMS = {"stop_pct": 0.10, "target_pct": 0.50}
    NEIGHBOURS = {}
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
            return {"stop": price * (1 - self.p("stop_pct")),
                    "target": price * (1 + self.p("target_pct")),
                    "reason": "day open"}
        return None


# ---------------------------------------------------------------------------
# Second wave: both directions, wider stops, fewer trades. The first six
# lost mostly because a long-only book can't make money in a falling market
# and because 5-minute noise kept hitting tight stops.
# ---------------------------------------------------------------------------

class OrbBothWays(Strategy):
    PARAMS = {"stop_cap_pct": 0.015, "target_r": 2.0}
    NEIGHBOURS = {"stop_cap_pct": [0.010, 0.015, 0.020],
                  "target_r": [1.5, 2.0, 2.5]}
    id = "orb_both"
    name = "Opening-range breakout (long + short)"
    description = ("Buys a close above the first-15-minute high, or shorts a close below its low. "
                   "Stop at the other side of the range (max 1.5%), target 2x the risk.")
    max_trades_per_symbol_per_day = 1

    def entry(self, row, prev):
        if _early(row, max(OPENING_RANGE_MINUTES, row["bar_minutes"])) \
                or not _ok(row["or_high"], row["or_low"], prev["Close"]):
            return None
        price = row["Close"]
        cap, r = self.p("stop_cap_pct"), self.p("target_r")
        if price > row["or_high"] >= prev["Close"]:
            stop = max(row["or_low"], price * (1 - cap))
            return {"stop": stop, "target": price + r * (price - stop),
                    "reason": "broke above opening range"}
        if price < row["or_low"] <= prev["Close"]:
            stop = min(row["or_high"], price * (1 + cap))
            return {"side": "SHORT", "stop": stop, "target": price - r * (stop - price),
                    "reason": "broke below opening range"}
        return None


class SuperTrendFlip(Strategy):
    PARAMS = {"target_r": 2.0}
    NEIGHBOURS = {"target_r": [1.5, 2.0, 2.5]}
    id = "supertrend"
    name = "SuperTrend (10, 3) flip"
    description = ("Trend-following with a 3-ATR trailing line: goes long when the trend flips up, "
                   "short when it flips down. The line is the stop; exits on the opposite flip.")

    def entry(self, row, prev):
        if _early(row, 10) or not _ok(row["st_line"], row["atr14"]) or prev["st_dir"] == row["st_dir"]:
            return None
        price = row["Close"]
        if row["st_dir"] == 1 and row["st_line"] < price:
            stop = row["st_line"]
            return {"stop": stop,
                    "target": price + self.p("target_r") * (price - stop),
                    "reason": "SuperTrend flipped up"}
        if row["st_dir"] == -1 and row["st_line"] > price:
            stop = row["st_line"]
            return {"side": "SHORT", "stop": stop,
                    "target": price - self.p("target_r") * (stop - price),
                    "reason": "SuperTrend flipped down"}
        return None

    def exit(self, row, prev, position):
        want = -1 if position.get("side") == "SHORT" else 1
        if _ok(row["st_dir"]) and row["st_dir"] == -want:
            return "SuperTrend flipped against the trade"
        return None


class VwapBandReversion(Strategy):
    PARAMS = {"sd_mult": 2.0, "rsi_long": 35.0, "rsi_short": 65.0,
              "stop_atr": 0.5}
    NEIGHBOURS = {"sd_mult": [1.5, 2.0, 2.5], "rsi_long": [30.0, 35.0, 40.0],
                  "rsi_short": [60.0, 65.0, 70.0], "stop_atr": [0.25, 0.5, 1.0]}
    id = "vwap_bands"
    name = "VWAP band mean-reversion"
    description = ("Fades stretched moves: buys when price is 2 standard deviations below VWAP and turns up "
                   "(RSI under 35), shorts the mirror case. Target is VWAP itself.")

    def entry(self, row, prev):
        if _early(row, 30) or not _ok(row["vwap"], row["vwap_sd"], row["rsi14"], row["atr14"]) \
                or row["vwap_sd"] <= 0:
            return None
        price, vwap, sd = row["Close"], row["vwap"], row["vwap_sd"]
        mult = self.p("sd_mult")
        if (prev["Close"] < vwap - mult * sd and price > prev["Close"]
                and row["rsi14"] < self.p("rsi_long")):
            stop = min(row["Low"], prev["Low"]) - self.p("stop_atr") * row["atr14"]
            if stop < price < vwap:
                return {"stop": stop, "target": vwap, "reason": "2σ below VWAP, turning up"}
        if (prev["Close"] > vwap + mult * sd and price < prev["Close"]
                and row["rsi14"] > self.p("rsi_short")):
            stop = max(row["High"], prev["High"]) + self.p("stop_atr") * row["atr14"]
            if stop > price > vwap:
                return {"side": "SHORT", "stop": stop, "target": vwap, "reason": "2σ above VWAP, turning down"}
        return None


class TrendPullback(Strategy):
    PARAMS = {"target_r": 2.0}
    NEIGHBOURS = {"target_r": [1.5, 2.0, 2.5]}
    id = "trend_pullback"
    name = "Trend pullback to EMA 21"
    description = ("Only trades with the day's trend (price vs VWAP, EMA 21 vs EMA 50): waits for a dip "
                   "to the 21-EMA and buys the bounce, or shorts the rally in a downtrend. Stop beyond the "
                   "last swing, target 2x risk.")

    def entry(self, row, prev):
        if _early(row, 30) \
                or not _ok(row["ema21"], row["ema50"], row["vwap"], row["swing_low5"], row["swing_high5"]):
            return None
        price = row["Close"]
        up = price > row["vwap"] and row["ema21"] > row["ema50"]
        down = price < row["vwap"] and row["ema21"] < row["ema50"]
        if up and prev["Low"] <= prev["ema21"] and price > row["ema21"] and price > prev["High"]:
            stop = min(row["swing_low5"], row["Low"])
            if stop < price:
                return {"stop": stop,
                        "target": price + self.p("target_r") * (price - stop),
                        "reason": "bounced off EMA21 in uptrend"}
        if down and prev["High"] >= prev["ema21"] and price < row["ema21"] and price < prev["Low"]:
            stop = max(row["swing_high5"], row["High"])
            if stop > price:
                return {"side": "SHORT", "stop": stop,
                        "target": price - self.p("target_r") * (stop - price),
                        "reason": "rejected at EMA21 in downtrend"}
        return None


class GapFade(Strategy):
    PARAMS = {"gap_pct": 0.006, "stop_atr": 0.2, "max_bar": 9}
    NEIGHBOURS = {"gap_pct": [0.004, 0.006, 0.008], "stop_atr": [0.1, 0.2, 0.4],
                  "max_bar": [7, 9, 12]}
    id = "gap_fade"
    name = "Gap fade (gap-fill)"
    description = ("When a stock opens 0.6%+ away from yesterday's close and the first 15 minutes fail to "
                   "extend the gap, trades back toward yesterday's close. Stop beyond the day's extreme.")
    max_trades_per_symbol_per_day = 1

    def entry(self, row, prev):
        if not (OPENING_RANGE_BARS <= row["bar_no"] <= self.p("max_bar")):
            return None
        if not _ok(row["prev_session_close"], row["session_open"], row["or_high"], row["or_low"], row["atr14"]):
            return None
        pc, op, price = row["prev_session_close"], row["session_open"], row["Close"]
        gap = (op - pc) / pc
        threshold = self.p("gap_pct")
        if gap >= threshold and price < row["or_low"]:
            stop = row["day_high"] + self.p("stop_atr") * row["atr14"]
            if stop > price > pc:
                return {"side": "SHORT", "stop": stop, "target": pc, "reason": f"gap up {gap:.1%} failing"}
        if gap <= -threshold and price > row["or_high"]:
            stop = row["day_low"] - self.p("stop_atr") * row["atr14"]
            if stop < price < pc:
                return {"stop": stop, "target": pc, "reason": f"gap down {gap:.1%} recovering"}
        return None


class IntradayMomentum(Strategy):
    PARAMS = {"early_pct": 0.005, "stop_atr": 1.5}
    NEIGHBOURS = {"early_pct": [0.003, 0.005, 0.008], "stop_atr": [1.0, 1.5, 2.0]}
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
        threshold = self.p("early_pct")
        if early >= threshold and price > row["vwap"]:
            stop = min(row["vwap"], price - self.p("stop_atr") * row["atr14"])
            return {"stop": stop, "target": price + 3 * (price - stop), "reason": f"first 30 min {early:+.1%}"}
        if early <= -threshold and price < row["vwap"]:
            stop = max(row["vwap"], price + self.p("stop_atr") * row["atr14"])
            return {"side": "SHORT", "stop": stop, "target": price - 3 * (stop - price),
                    "reason": f"first 30 min {early:+.1%}"}
        return None


# ---------------------------------------------------------------------------
# The patient family
#
# Measured, not guessed at: a round trip on an NSE intraday position costs
# 0.206% of it (results/slippage_sweep.md). The sweep then showed that with
# slippage switched off entirely the eleven existing ideas still make about
# Rs1 a trade on Rs12,500, which is 0.008%. They are not losing an edge to
# costs; they are taking trades too small to pay for themselves.
#
# So this family takes the SAME signals and refuses most of them. A setup is
# taken only when the move it is aiming for is a multiple of what the round
# trip costs, and it aims further before getting out. Fewer trades, each one
# worth making.
#
# It is a wrapper, not a copy. Every rule still comes from the base
# strategy's own entry() (AGENTS.md §1.2), and each variant is a NEW id
# going through the full funnel rather than a threshold moved on an old one
# (AGENTS.md §1.5).
# ---------------------------------------------------------------------------

# One NSE round trip, as a percentage of the position: brokerage 0.060, STT
# 0.025, GST 0.012, exchange 0.006, stamp 0.003, SEBI 0.000, slippage 0.100.
ROUND_TRIP_PCT = 0.206


class Patient(Strategy):
    """A base rule, taken only when it is aiming far enough to be worth it.

    `min_move_mult` is in round trips, not in rupees or percent, because
    that is the thing the move has to beat. At the default of 4 a setup
    must be aiming for 0.824% before it is taken at all.

    `target_r` widens the base's own target. This is the bet being tested
    and it is not a free one: a target further away is hit less often, so
    the family wins only if the bigger wins outweigh the lower hit rate.
    The funnel decides that, not this comment.
    """

    PARAMS = {"min_move_mult": 4.0, "target_r": 3.0}
    NEIGHBOURS = {"min_move_mult": [3.0, 4.0, 6.0],
                  "target_r": [2.0, 3.0, 4.0]}
    # One a day per symbol: the whole point is to stop paying the toll
    # repeatedly for moves that never cover it.
    max_trades_per_symbol_per_day = 1
    # Available to tick on, but it does not join a running arena by itself.
    # Twelve new books appearing unasked in someone's live arena is not an
    # improvement.
    arena_default = False
    base_id = "base"

    @property
    def base(self):
        return STRATEGIES[self.base_id]

    def entry(self, row, prev):
        sig = self.base.entry(row, prev)
        if not sig:
            return None
        price = row["Close"]
        stop = float(sig["stop"])
        short = sig.get("side") == "SHORT"
        risk = (stop - price) if short else (price - stop)
        if not _ok(price, stop) or risk <= 0 or price <= 0:
            return None
        target = (price - self.p("target_r") * risk) if short \
            else (price + self.p("target_r") * risk)
        if target <= 0:
            return None
        move_pct = 100.0 * abs(target - price) / price
        if move_pct < self.p("min_move_mult") * ROUND_TRIP_PCT:
            return None
        out = {"stop": stop, "target": target,
               "reason": "%s, aiming %.2f%%" % (sig.get("reason", "signal"), move_pct)}
        if short:
            out["side"] = "SHORT"
        return out

    def exit(self, row, prev, position):
        # The way out is the base's. Only the decision to come in at all,
        # and how far it aims, belong to this family.
        return self.base.exit(row, prev, position)


def _patient(base: Strategy) -> type:
    """One patient variant of `base`, named after it."""
    return type("Patient" + type(base).__name__, (Patient,), {
        "id": "patient_" + base.id,
        "base_id": base.id,
        "name": base.name + " (patient)",
        "description": ("%s Taken only when it is aiming at least %gx the %.3f%% round "
                        "trip, at %gx the risk, once a day per stock."
                        % (base.description, Patient.PARAMS["min_move_mult"],
                           ROUND_TRIP_PCT, Patient.PARAMS["target_r"])),
        "volume_gated": base.volume_gated,
    })


STRATEGIES: dict[str, Strategy] = {s.id: s for s in [
    OpeningRangeBreakout(), VwapReclaim(), EmaCrossover(), RsiReversion(),
    BollingerBreakout(), LegacyScore(),
    OrbBothWays(), SuperTrendFlip(), VwapBandReversion(), TrendPullback(), GapFade(), IntradayMomentum(),
    BuyAtOpenBenchmark(),
]}

# A patient variant of every real idea. Every one of them, deliberately:
# picking which bases to wrap by how they scored would be choosing a rule
# on recent results, which is the thing AGENTS.md §1.5 exists to stop. The
# funnel decides which survive, not this list.
for _base in [s for s in list(STRATEGIES.values()) if not s.benchmark]:
    _variant = _patient(_base)()
    STRATEGIES[_variant.id] = _variant
del _base, _variant

# The strategies that existed before the second wave. Arenas created back
# then have these in their config; anything newer is switched on for them
# once (see engine.adopt_new_strategies) - after that, the user's choice wins.
ORIGINAL_STRATEGY_IDS = ["orb", "vwap_reclaim", "ema_cross", "rsi_reversion", "bb_breakout", "score4", "benchmark"]


def catalog(has_volume: bool = True) -> list[dict]:
    return [{"id": s.id, "name": s.display_name(has_volume), "description": s.description,
             "benchmark": s.benchmark, "volume_gated": s.volume_gated,
             "both_sides": s.id not in ORIGINAL_STRATEGY_IDS}
            for s in STRATEGIES.values()]
