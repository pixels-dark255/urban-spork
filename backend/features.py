"""
Feature engineering - the single definition of what the ML models see.

Kept in one module on purpose: training and inference must build features
identically, and the classic way to ship a broken ML system is to compute
them in two places that drift apart. Everything here is derived only from
bars at or before the bar being described, so there is no lookahead.

Labels are three-way by design: up / down / neutral, where "neutral" is any
forward move smaller than a cost-and-noise band. Training a two-class model
on raw sign teaches it to call a 0.02% drift a "buy", which is exactly the
kind of false signal this platform is supposed to avoid.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import ema, rsi, macd, atr, adx, bollinger_bands, obv

FEATURE_COLUMNS = [
    "ret_1", "ret_3", "ret_5", "ret_10", "ret_20",
    "ema9_dist", "ema21_dist", "ema50_dist", "ema9_21_spread",
    "rsi", "rsi_slope", "macd_hist", "macd_hist_slope",
    "atr_pct", "adx", "di_spread", "percent_b", "bandwidth",
    "vol_ratio", "obv_slope_norm", "vwap_dist",
    "range_pct", "body_pct", "upper_wick_pct", "lower_wick_pct",
    "minutes_into_session", "session_progress",
]


def _safe_div(a, b):
    return a / b.replace(0, np.nan) if isinstance(b, pd.Series) else (a / b if b else np.nan)


def build_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """One row of features per bar. Rows with insufficient history are
    dropped, not filled - an imputed ADX is a made-up number."""
    if df is None or len(df) < 60:
        return pd.DataFrame(columns=FEATURE_COLUMNS)

    close, high, low = df["Close"], df["High"], df["Low"]
    volume = df["Volume"].fillna(0) if "Volume" in df.columns else pd.Series(0.0, index=df.index)
    out = pd.DataFrame(index=df.index)

    for n in (1, 3, 5, 10, 20):
        out[f"ret_{n}"] = close.pct_change(n) * 100

    ema9, ema21, ema50 = ema(close, 9), ema(close, 21), ema(close, 50)
    out["ema9_dist"] = (close - ema9) / close * 100
    out["ema21_dist"] = (close - ema21) / close * 100
    out["ema50_dist"] = (close - ema50) / close * 100
    out["ema9_21_spread"] = (ema9 - ema21) / close * 100

    rsi_s = rsi(close, 14)
    out["rsi"] = rsi_s
    out["rsi_slope"] = rsi_s.diff(3)

    _, _, hist = macd(close)
    out["macd_hist"] = hist / close * 100
    out["macd_hist_slope"] = out["macd_hist"].diff(3)

    atr_s = atr(df)
    out["atr_pct"] = atr_s / close * 100
    adx_s, plus_di, minus_di = adx(df)
    out["adx"] = adx_s
    out["di_spread"] = plus_di - minus_di

    upper, mid, lower = bollinger_bands(close, 20)
    width = (upper - lower)
    out["percent_b"] = (close - lower) / width.replace(0, np.nan)
    out["bandwidth"] = width / mid.replace(0, np.nan)

    avg_vol = volume.rolling(20, min_periods=5).mean()
    out["vol_ratio"] = volume / avg_vol.replace(0, np.nan)
    obv_s = obv(df)
    out["obv_slope_norm"] = obv_s.diff(5) / avg_vol.replace(0, np.nan)

    typical = (high + low + close) / 3.0
    # Rolling 30-bar VWAP proxy: a true session VWAP can't be computed for
    # historical bars without knowing session boundaries for every day in the
    # window, and this tracks it closely enough to be a useful feature.
    rolling_vwap = (typical * volume).rolling(30, min_periods=5).sum() / volume.rolling(30, min_periods=5).sum().replace(0, np.nan)
    out["vwap_dist"] = (close - rolling_vwap) / close * 100

    bar_range = (high - low)
    out["range_pct"] = bar_range / close * 100
    out["body_pct"] = (close - df["Open"]).abs() / close * 100
    out["upper_wick_pct"] = (high - np.maximum(close, df["Open"])) / close * 100
    out["lower_wick_pct"] = (np.minimum(close, df["Open"]) - low) / close * 100

    # Time of day matters intraday: the open and the close behave nothing
    # like the middle of the session.
    try:
        minutes = pd.Series(
            [(t.hour * 60 + t.minute) - (9 * 60 + 15) for t in pd.DatetimeIndex(df.index)],
            index=df.index, dtype="float64",
        )
    except (TypeError, ValueError):
        minutes = pd.Series(0.0, index=df.index)
    out["minutes_into_session"] = minutes.clip(lower=0, upper=375)
    out["session_progress"] = out["minutes_into_session"] / 375.0

    out = out.replace([np.inf, -np.inf], np.nan)
    return out[FEATURE_COLUMNS]


def build_labels(df: pd.DataFrame, horizon_bars: int, neutral_band_pct: float | None = None) -> pd.Series:
    """Forward return over `horizon_bars`, bucketed to -1 / 0 / +1.

    The neutral band defaults to half the median bar range, i.e. it scales
    with how much this stock actually moves on this timeframe rather than
    being a fixed percentage that's meaningless on one and enormous on
    another."""
    close = df["Close"]
    forward = close.shift(-horizon_bars) / close - 1.0
    if neutral_band_pct is None:
        median_range = float(((df["High"] - df["Low"]) / close).median() or 0.002)
        neutral_band_pct = max(median_range * 0.5, 0.0008) * 100
    band = neutral_band_pct / 100.0
    labels = pd.Series(0, index=df.index, dtype="int64")
    labels[forward > band] = 1
    labels[forward < -band] = -1
    labels[forward.isna()] = np.nan
    return labels


def build_training_set(df: pd.DataFrame, horizon_bars: int,
                       neutral_band_pct: float | None = None) -> tuple[pd.DataFrame, pd.Series]:
    features = build_feature_frame(df)
    labels = build_labels(df, horizon_bars, neutral_band_pct)
    combined = features.join(labels.rename("label"), how="inner").dropna()
    if combined.empty:
        return pd.DataFrame(columns=FEATURE_COLUMNS), pd.Series(dtype="int64")
    return combined[FEATURE_COLUMNS], combined["label"].astype(int)


def latest_feature_row(df: pd.DataFrame) -> pd.DataFrame | None:
    """Features for the most recent complete bar, ready for model.predict."""
    frame = build_feature_frame(df)
    if frame.empty:
        return None
    row = frame.dropna().tail(1)
    return row if not row.empty else None
