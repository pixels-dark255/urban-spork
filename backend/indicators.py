"""Technical indicator calculations - pure pandas/numpy, no extra deps."""
import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=max(1, window // 2)).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """Relative Strength Index.

    The division by avg_loss has two degenerate cases that the textbook
    formula leaves as NaN, and a NaN here propagates all the way into a JSON
    response (Starlette serialises with allow_nan=False and raises) and into
    Postgres JSONB (which rejects NaN outright):

      - no down bars in the window  -> RSI is 100 by definition
      - a perfectly flat window     -> neither side has strength; 50

    Rows where the rolling window is not yet full stay NaN on purpose: that
    is missing data, not a degenerate case, and callers already treat it as
    "no reading yet".
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window).mean()
    avg_loss = loss.rolling(window).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))

    # NaN comparisons are False, so incomplete windows are untouched by both.
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    out = out.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    return out


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

    # Sample standard deviation needs at least two observations. With a single
    # return - which is exactly what a 2-bar timeframe gives, and what every
    # timeframe gives in the first minutes after the 09:15 open - pandas
    # returns NaN, and that NaN survives max(nan, 0.05) in the predictor and
    # takes the whole request down with a 500.
    volatility = float(returns.std()) if len(returns) >= 2 else 0.0
    mean_return = float(returns.mean()) if not returns.empty else 0.0

    summary = {
        "start_price": start_price,
        "end_price": end_price,
        "pct_change": pct_change,
        "mean_return": mean_return,
        "volatility": volatility,
        "rsi": float(r) if np.isfinite(r) else None,
        "macd_hist": macd_hist_last,
        "n_points": int(len(close)),
    }

    # Belt and braces: if anything above is still non-finite, this timeframe
    # has nothing trustworthy to contribute. Returning None drops it from the
    # blend instead of poisoning the whole weighted average.
    for key, value in summary.items():
        if isinstance(value, float) and not np.isfinite(value):
            return None
    return summary
