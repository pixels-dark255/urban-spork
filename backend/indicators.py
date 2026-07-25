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
