"""
Market regime detection.

Before predicting anything, classify the environment the stock is trading
in. This matters because the strategies that work in each regime are
different, and mutually contradictory: buying a breakout is right in a
strong trend and wrong in a range, where fading extremes is right instead.
A model that ignores the regime is averaging those two together and getting
neither.

Output has two parts:
 - `primary`: one of STRONG_UPTREND / UPTREND / SIDEWAYS / DOWNTREND /
   STRONG_DOWNTREND. This is the key the adaptive ensemble stores and looks
   up its per-regime weights under.
 - `flags`: HIGH_VOLATILITY / LOW_VOLUME / GAP_UP / GAP_DOWN - conditions
   that can coexist with any trend and that mostly act as confidence
   penalties rather than direction signals.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import atr, adx

STRONG_UPTREND = "STRONG_UPTREND"
UPTREND = "UPTREND"
SIDEWAYS = "SIDEWAYS"
DOWNTREND = "DOWNTREND"
STRONG_DOWNTREND = "STRONG_DOWNTREND"

PRIMARY_REGIMES = [STRONG_UPTREND, UPTREND, SIDEWAYS, DOWNTREND, STRONG_DOWNTREND]

HIGH_VOLATILITY = "HIGH_VOLATILITY"
LOW_VOLUME = "LOW_VOLUME"
GAP_UP = "GAP_UP"
GAP_DOWN = "GAP_DOWN"

# ADX thresholds. 20/25 are the conventional "trend exists" line; 40 is where
# a trend is strong enough that fading it is usually a losing idea.
ADX_TREND = 22.0
ADX_STRONG = 40.0


def _session_gap(df: pd.DataFrame) -> tuple[str | None, float | None]:
    """Compare today's first bar's open with the previous session's close."""
    if df is None or len(df) < 2:
        return None, None
    try:
        dates = pd.Index([pd.Timestamp(i).date() for i in df.index])
    except (TypeError, ValueError):
        return None, None
    unique_days = sorted(set(dates))
    if len(unique_days) < 2:
        return None, None
    today, prev_day = unique_days[-1], unique_days[-2]
    today_open = float(df["Open"].values[dates == today][0])
    prev_close = float(df["Close"].values[dates == prev_day][-1])
    if not prev_close:
        return None, None
    gap_pct = (today_open - prev_close) / prev_close * 100
    if gap_pct >= 1.0:
        return GAP_UP, round(gap_pct, 3)
    if gap_pct <= -1.0:
        return GAP_DOWN, round(gap_pct, 3)
    return None, round(gap_pct, 3)


def detect(df: pd.DataFrame, snapshot: dict | None = None) -> dict:
    """Classify the regime from a bar series. `snapshot` is an optional
    already-computed indicator_snapshot, reused to avoid recomputing ADX/ATR."""
    if df is None or len(df) < 30:
        return {
            "primary": SIDEWAYS,
            "flags": [],
            "confidence": 0.2,
            "detail": {"reason": "not enough bars to classify - defaulting to sideways"},
        }

    close = df["Close"].dropna()
    if snapshot:
        adx_val = float(snapshot.get("adx") or 0.0)
        plus_di = float(snapshot.get("plus_di") or 0.0)
        minus_di = float(snapshot.get("minus_di") or 0.0)
        atr_pct = float(snapshot.get("atr_pct") or 0.0)
        vol_info = snapshot.get("volume") or {}
        ema9, ema21 = snapshot.get("ema9"), snapshot.get("ema21")
    else:
        adx_s, plus_s, minus_s = adx(df)
        adx_val = float(adx_s.iloc[-1]) if not adx_s.dropna().empty else 0.0
        plus_di = float(plus_s.iloc[-1]) if not plus_s.dropna().empty else 0.0
        minus_di = float(minus_s.iloc[-1]) if not minus_s.dropna().empty else 0.0
        atr_series = atr(df)
        atr_val = float(atr_series.iloc[-1]) if not atr_series.dropna().empty else 0.0
        atr_pct = atr_val / float(close.iloc[-1]) * 100 if len(close) else 0.0
        vol_info = {}
        ema9 = float(close.ewm(span=9, adjust=False).mean().iloc[-1])
        ema21 = float(close.ewm(span=21, adjust=False).mean().iloc[-1])

    adx_val = 0.0 if np.isnan(adx_val) else adx_val
    directional = plus_di - minus_di
    ema_bias = 1 if (ema9 or 0) > (ema21 or 0) else -1

    if adx_val >= ADX_STRONG and directional > 0:
        primary = STRONG_UPTREND
    elif adx_val >= ADX_STRONG and directional < 0:
        primary = STRONG_DOWNTREND
    elif adx_val >= ADX_TREND and directional > 0:
        primary = UPTREND
    elif adx_val >= ADX_TREND and directional < 0:
        primary = DOWNTREND
    else:
        primary = SIDEWAYS

    # ADX lags. When it is just under the trend line but price and both EMAs
    # agree strongly, call the weaker trend rather than "sideways".
    if primary == SIDEWAYS and adx_val >= ADX_TREND - 5:
        recent = float(close.pct_change(10).iloc[-1] * 100) if len(close) > 10 else 0.0
        if ema_bias > 0 and recent > 0.5:
            primary = UPTREND
        elif ema_bias < 0 and recent < -0.5:
            primary = DOWNTREND

    flags = []
    # Volatility is judged against this stock's own recent history, not an
    # absolute number - 1% ATR is calm for a smallcap and wild for an index.
    atr_series = atr(df)
    atr_pct_series = (atr_series / df["Close"]).dropna() * 100
    vol_percentile = None
    if len(atr_pct_series) >= 30:
        vol_percentile = float((atr_pct_series <= atr_pct).mean())
        if vol_percentile >= 0.85:
            flags.append(HIGH_VOLATILITY)

    if vol_info.get("available") and vol_info.get("volume_ratio") is not None:
        if vol_info["volume_ratio"] <= 0.5:
            flags.append(LOW_VOLUME)

    gap_flag, gap_pct = _session_gap(df)
    if gap_flag:
        flags.append(gap_flag)

    # How much to trust the classification itself: a decisive ADX far from
    # the thresholds is a confident call; sitting right on 22 is not.
    distance = min(abs(adx_val - ADX_TREND), abs(adx_val - ADX_STRONG))
    confidence = float(np.clip(0.45 + distance / 40.0, 0.2, 0.95))
    if primary == SIDEWAYS and adx_val < 15:
        confidence = min(0.9, confidence + 0.15)

    return {
        "primary": primary,
        "flags": flags,
        "confidence": round(confidence, 3),
        "detail": {
            "adx": round(adx_val, 2),
            "plus_di": round(plus_di, 2),
            "minus_di": round(minus_di, 2),
            "atr_pct": round(atr_pct, 3),
            "volatility_percentile": round(vol_percentile, 3) if vol_percentile is not None else None,
            "gap_pct": gap_pct,
            "ema_bias": "bullish" if ema_bias > 0 else "bearish",
        },
    }


def describe(regime: dict) -> str:
    """Plain-English one-liner for the UI."""
    primary = regime.get("primary", SIDEWAYS)
    text = {
        STRONG_UPTREND: "Strong uptrend - trend-following signals carry the most weight here.",
        UPTREND: "Uptrend - pullback entries in the direction of the trend are favoured.",
        SIDEWAYS: "Sideways/range - mean-reversion is favoured and breakouts often fail.",
        DOWNTREND: "Downtrend - rallies tend to be sold; long setups need extra evidence.",
        STRONG_DOWNTREND: "Strong downtrend - fading it is the most expensive mistake available.",
    }.get(primary, primary)
    flags = regime.get("flags") or []
    extra = {
        HIGH_VOLATILITY: "Volatility is unusually high, so stops must be wider and size smaller.",
        LOW_VOLUME: "Volume is thin, which makes every signal here less reliable.",
        GAP_UP: "The session gapped up, so the opening range matters more than usual.",
        GAP_DOWN: "The session gapped down, so the opening range matters more than usual.",
    }
    return " ".join([text] + [extra[f] for f in flags if f in extra])


def strategy_bias(regime: dict) -> dict:
    """Which family of signals should dominate in this regime. The ensemble
    multiplies its component weights by these before normalising."""
    primary = regime.get("primary", SIDEWAYS)
    if primary in (STRONG_UPTREND, STRONG_DOWNTREND):
        return {"trend_following": 1.5, "mean_reversion": 0.5, "breakout": 1.3}
    if primary in (UPTREND, DOWNTREND):
        return {"trend_following": 1.25, "mean_reversion": 0.8, "breakout": 1.1}
    return {"trend_following": 0.7, "mean_reversion": 1.4, "breakout": 0.8}
