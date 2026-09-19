"""Technical indicator calculations - pure pandas/numpy, no extra deps."""
import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=max(1, window // 2)).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window).mean()
    avg_loss = loss.rolling(window).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger_bands(series: pd.Series, window: int = 20, num_std: float = 2.0):
    mid = sma(series, window)
    std = series.rolling(window).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def annualized_volatility(returns: pd.Series, periods_per_year: int) -> float:
    if returns.dropna().empty:
        return 0.0
    return float(returns.std() * np.sqrt(periods_per_year))


def summarize_timeframe(df: pd.DataFrame) -> dict | None:
    """Compute a compact feature summary for one timeframe's OHLC data."""
    if df is None or df.empty or "Close" not in df.columns or len(df) < 2:
        return None
    close = df["Close"].dropna()
    if len(close) < 2:
        return None
    returns = close.pct_change().dropna()

    start_price = float(close.iloc[0])
    end_price = float(close.iloc[-1])
    pct_change = (end_price - start_price) / start_price * 100 if start_price else 0.0

    r = rsi(close).iloc[-1] if len(close) >= 15 else np.nan
    macd_line, signal_line, hist = macd(close)
    macd_hist_last = float(hist.iloc[-1]) if not hist.dropna().empty else 0.0

    return {
        "start_price": start_price,
        "end_price": end_price,
        "pct_change": pct_change,
        "mean_return": float(returns.mean()) if not returns.empty else 0.0,
        "volatility": float(returns.std()) if not returns.empty else 0.0,
        "rsi": float(r) if not np.isnan(r) else None,
        "macd_hist": macd_hist_last,
        "n_points": int(len(close)),
    }


# ---------------------------------------------------------------------------
# Extended indicator set used by the intraday engine, regime detector and the
# ML feature builder. Everything below is pure pandas/numpy - no TA library
# dependency, so there is nothing to install and nothing to go stale.
# ---------------------------------------------------------------------------

def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1)
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Average True Range - the volatility unit stop losses are sized in.
    Wilder's smoothing (an EMA with alpha = 1/window), which is what every
    charting package means by 'ATR(14)'."""
    tr = true_range(df["High"], df["Low"], df["Close"])
    return tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


def adx(df: pd.DataFrame, window: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Average Directional Index plus +DI/-DI. ADX measures how *strong* a
    trend is without saying which way; +DI vs -DI says which way. Together
    they are the backbone of the regime classifier."""
    high, low, close = df["High"], df["Low"], df["Close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)

    tr = true_range(high, low, close)
    alpha = 1.0 / window
    atr_s = tr.ewm(alpha=alpha, adjust=False, min_periods=window).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=window).mean() / atr_s.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=window).mean() / atr_s.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_s = dx.ewm(alpha=alpha, adjust=False, min_periods=window).mean()
    return adx_s, plus_di, minus_di


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP. Callers pass a single session's bars; passing more than
    one day smears the average across sessions and means nothing."""
    typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
    vol = df["Volume"].fillna(0)
    cum_vol = vol.cumsum()
    cum_pv = (typical * vol).cumsum()
    return (cum_pv / cum_vol.replace(0, np.nan)).fillna(df["Close"])


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume - cumulative volume signed by the day's direction."""
    direction = np.sign(df["Close"].diff().fillna(0.0))
    return (direction * df["Volume"].fillna(0)).cumsum()


def volume_profile(df: pd.DataFrame, window: int = 20) -> dict:
    """Is the current bar's volume unusual, and is volume confirming price?"""
    if "Volume" not in df.columns or df["Volume"].fillna(0).sum() == 0:
        return {"available": False}
    vol = df["Volume"].fillna(0)
    avg = float(vol.rolling(window, min_periods=max(2, window // 2)).mean().iloc[-1])
    last = float(vol.iloc[-1])
    ratio = (last / avg) if avg else 1.0
    recent_ret = float(df["Close"].pct_change().tail(window).sum())
    obv_series = obv(df)
    obv_slope = float(obv_series.diff().tail(window).mean())
    return {
        "available": True,
        "last_volume": last,
        "avg_volume": round(avg, 2),
        "volume_ratio": round(ratio, 3),
        "spike": bool(ratio >= 2.0),
        "dry_up": bool(ratio <= 0.4),
        "obv_slope": round(obv_slope, 2),
        # Volume confirms a move when it rises with price in the same
        # direction; divergence is the classic early warning of a fake move.
        "confirms_price": bool((recent_ret > 0 and obv_slope > 0) or (recent_ret < 0 and obv_slope < 0)),
    }


def support_resistance(df: pd.DataFrame, lookback: int = 60, levels: int = 3) -> dict:
    """Swing-pivot support/resistance. A bar is a pivot high if its high is
    the highest of the 2 bars either side of it (and vice versa); pivots are
    then clustered so five touches of "roughly 1,000" become one level
    rather than five near-identical ones."""
    if df is None or len(df) < 10:
        return {"support": [], "resistance": [], "nearest_support": None, "nearest_resistance": None}
    window = df.tail(lookback)
    highs, lows, closes = window["High"].values, window["Low"].values, window["Close"].values
    last = float(closes[-1])

    pivot_highs, pivot_lows = [], []
    for i in range(2, len(window) - 2):
        if highs[i] == max(highs[i - 2:i + 3]):
            pivot_highs.append(float(highs[i]))
        if lows[i] == min(lows[i - 2:i + 3]):
            pivot_lows.append(float(lows[i]))

    def cluster(values: list[float]) -> list[float]:
        if not values:
            return []
        tolerance = max(last * 0.002, 0.01)  # 0.2% - two levels closer than this are one level
        values = sorted(values)
        groups, current = [], [values[0]]
        for v in values[1:]:
            if v - current[-1] <= tolerance:
                current.append(v)
            else:
                groups.append(current)
                current = [v]
        groups.append(current)
        return [round(float(np.mean(g)), 2) for g in groups]

    resistance = [lvl for lvl in cluster(pivot_highs) if lvl > last]
    support = [lvl for lvl in cluster(pivot_lows) if lvl < last]

    # Fall back to the window's extremes when no pivot sits on the right side
    # of price - which is what happens at a fresh high or low. A level is
    # still needed for targeting, but it must be labelled: "the highest tick
    # in the last hour" is not structure, and treating it as an obstacle
    # would veto every breakout entry, which is exactly the setup that most
    # deserves one.
    resistance_is_pivot = bool(resistance)
    support_is_pivot = bool(support)
    if not resistance:
        resistance = [round(float(window["High"].max()), 2)]
    if not support:
        support = [round(float(window["Low"].min()), 2)]

    nearest_resistance = min(resistance) if resistance else None
    nearest_support = max(support) if support else None
    return {
        "support": sorted(support, reverse=True)[:levels],
        "resistance": sorted(resistance)[:levels],
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "resistance_is_pivot": resistance_is_pivot,
        "support_is_pivot": support_is_pivot,
    }


def bollinger_position(close: pd.Series, window: int = 20, num_std: float = 2.0) -> dict:
    upper, mid, lower = bollinger_bands(close, window, num_std)
    last = float(close.iloc[-1])
    u, m, l = float(upper.iloc[-1]), float(mid.iloc[-1]), float(lower.iloc[-1])
    width = (u - l)
    pct_b = (last - l) / width if width else 0.5
    return {
        "upper": round(u, 2), "middle": round(m, 2), "lower": round(l, 2),
        "percent_b": round(float(pct_b), 3),
        "bandwidth": round(float(width / m), 4) if m else None,
        "squeeze": bool(m and (width / m) < 0.02),
    }


def indicator_snapshot(df: pd.DataFrame, session_df: pd.DataFrame | None = None) -> dict | None:
    """One call, every indicator the engines need, computed once and shared.
    Returns None when there simply isn't enough data to compute anything
    honest - callers treat that as 'no trade', not as a neutral signal."""
    if df is None or len(df) < 25 or "Close" not in df.columns:
        return None
    close = df["Close"].dropna()
    if len(close) < 25:
        return None

    last = float(close.iloc[-1])
    ema9 = float(ema(close, 9).iloc[-1])
    ema21 = float(ema(close, 21).iloc[-1])
    ema50 = float(ema(close, 50).iloc[-1]) if len(close) >= 50 else ema21
    sma20 = float(sma(close, 20).iloc[-1])
    rsi_val = float(rsi(close, 14).iloc[-1]) if len(close) >= 15 else 50.0
    macd_line, signal_line, hist = macd(close)
    atr_series = atr(df)
    atr_val = float(atr_series.iloc[-1]) if not atr_series.dropna().empty else float(close.std())
    adx_s, plus_di, minus_di = adx(df)

    def _f(series, default=0.0):
        try:
            v = float(series.iloc[-1])
            return default if np.isnan(v) else v
        except (IndexError, TypeError, ValueError):
            return default

    session = session_df if (session_df is not None and not session_df.empty) else df
    vwap_val = float(vwap(session).iloc[-1])

    returns = close.pct_change().dropna()
    return {
        "price": round(last, 2),
        "ema9": round(ema9, 2),
        "ema21": round(ema21, 2),
        "ema50": round(ema50, 2),
        "sma20": round(sma20, 2),
        "rsi": round(rsi_val, 2) if not np.isnan(rsi_val) else 50.0,
        "macd": round(_f(macd_line), 4),
        "macd_signal": round(_f(signal_line), 4),
        "macd_hist": round(_f(hist), 4),
        "atr": round(atr_val, 3),
        "atr_pct": round(atr_val / last * 100, 3) if last else None,
        "adx": round(_f(adx_s, 0.0), 2),
        "plus_di": round(_f(plus_di, 0.0), 2),
        "minus_di": round(_f(minus_di, 0.0), 2),
        "vwap": round(vwap_val, 2),
        "bollinger": bollinger_position(close),
        "volume": volume_profile(df),
        "levels": support_resistance(df),
        "return_pct_20": round(float(close.pct_change(20).iloc[-1] * 100), 3) if len(close) > 20 else None,
        "volatility_pct": round(float(returns.tail(20).std() * 100), 4) if len(returns) >= 5 else None,
        "bars_used": int(len(close)),
    }
