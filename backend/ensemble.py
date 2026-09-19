"""
Adaptive ensemble / fusion engine.

               Market Data
                    |
            Feature Engineering
                    |
   Technical  +  ML  +  Statistical  +  Volume  +  Market Context
                    |
             Fusion (this module)
                    |
              Final Prediction

Five components each vote in [-1, +1]. They are combined with weights that
are *not* fixed:

 1. Base weights - a sane starting point.
 2. Regime bias - trend-following matters more in a trend, mean-reversion
    more in a range (regime.strategy_bias), applied inside the technical
    component and to the component mix.
 3. Measured performance - each component's historical hit rate for this
    regime and timeframe, from the model_performance table. Only applied
    once there are enough resolved samples to mean anything, and bounded,
    so one lucky streak can't hand a component the whole vote.

The final confidence is deliberately hard to earn: it starts from the size
of the combined score, is scaled by how much the components actually agree
(five weak agreeing votes beat one strong lone one), and is then penalised
for thin volume, extreme volatility, an uncertain regime read, and
approximated data. The engine is expected to return NO TRADE often. That is
the design, not a shortfall - see the confidence floor in config.
"""
from __future__ import annotations

import numpy as np

import market_store
import regime as regime_mod

COMPONENTS = ["technical", "ml", "statistical", "volume", "market_context"]

BASE_WEIGHTS = {
    "technical": 0.32,
    "ml": 0.24,
    "statistical": 0.18,
    "volume": 0.14,
    "market_context": 0.12,
}

MIN_SAMPLES_FOR_ADAPTATION = 20
PERFORMANCE_MULTIPLIER_RANGE = (0.6, 1.4)


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

def technical_component(snapshot: dict, regime_info: dict) -> dict:
    """Classic technical analysis, split into three strategy families whose
    relative importance depends on the regime."""
    bias = regime_mod.strategy_bias(regime_info)
    price = snapshot["price"]
    reasons = []

    # --- trend following ---
    trend = 0.0
    if snapshot["ema9"] > snapshot["ema21"]:
        trend += 0.4
        reasons.append("EMA9 above EMA21 (short-term trend up)")
    else:
        trend -= 0.4
        reasons.append("EMA9 below EMA21 (short-term trend down)")
    if price > snapshot["ema50"]:
        trend += 0.25
    else:
        trend -= 0.25
    macd_hist = snapshot["macd_hist"]
    if macd_hist > 0:
        trend += 0.2
        reasons.append("MACD histogram positive")
    elif macd_hist < 0:
        trend -= 0.2
        reasons.append("MACD histogram negative")
    di_spread = snapshot["plus_di"] - snapshot["minus_di"]
    trend += float(np.clip(di_spread / 40.0, -0.3, 0.3))
    if snapshot["adx"] >= 25:
        reasons.append(f"ADX {snapshot['adx']:.0f} confirms a real trend, not chop")

    # --- mean reversion ---
    mean_rev = 0.0
    rsi_val = snapshot["rsi"]
    if rsi_val <= 30:
        mean_rev += 0.6
        reasons.append(f"RSI {rsi_val:.0f} oversold")
    elif rsi_val >= 70:
        mean_rev -= 0.6
        reasons.append(f"RSI {rsi_val:.0f} overbought")
    else:
        mean_rev += (50 - rsi_val) / 100.0
    pct_b = snapshot["bollinger"]["percent_b"]
    if pct_b <= 0.05:
        mean_rev += 0.4
        reasons.append("Price at/below the lower Bollinger band")
    elif pct_b >= 0.95:
        mean_rev -= 0.4
        reasons.append("Price at/above the upper Bollinger band")

    # --- breakout / location ---
    breakout = 0.0
    levels = snapshot.get("levels") or {}
    resistance = levels.get("nearest_resistance")
    support = levels.get("nearest_support")
    if resistance and price >= resistance:
        breakout += 0.5
        reasons.append(f"Price broke above resistance at {resistance:,.2f}")
    if support and price <= support:
        breakout -= 0.5
        reasons.append(f"Price broke below support at {support:,.2f}")
    vwap_val = snapshot.get("vwap")
    if vwap_val:
        vwap_gap = (price - vwap_val) / price * 100
        breakout += float(np.clip(vwap_gap / 1.0, -0.4, 0.4))
        reasons.append(f"Price {'above' if vwap_gap >= 0 else 'below'} VWAP by {abs(vwap_gap):.2f}%")

    weights = {
        "trend_following": bias["trend_following"],
        "mean_reversion": bias["mean_reversion"],
        "breakout": bias["breakout"],
    }
    total = sum(weights.values())
    score = (trend * weights["trend_following"]
             + mean_rev * weights["mean_reversion"]
             + breakout * weights["breakout"]) / total

    return {
        "available": True,
        "score": float(np.clip(score, -1, 1)),
        "confidence": 0.6,
        "reasons": reasons,
        "detail": {
            "trend_score": round(float(np.clip(trend, -1, 1)), 3),
            "mean_reversion_score": round(float(np.clip(mean_rev, -1, 1)), 3),
            "breakout_score": round(float(np.clip(breakout, -1, 1)), 3),
            "regime_bias": bias,
        },
    }


def statistical_component(snapshot: dict, bars) -> dict:
    """Distribution-based view: how stretched is price relative to its own
    recent behaviour, and does the recent drift stand out from the noise?"""
    close = bars["Close"].dropna()
    if len(close) < 30:
        return {"available": False, "reason": "not enough bars", "score": 0.0, "confidence": 0.0,
                "reasons": []}
    reasons = []
    sma20 = snapshot["sma20"]
    atr_val = snapshot["atr"] or (float(close.std()) or 1.0)
    z = (snapshot["price"] - sma20) / atr_val if atr_val else 0.0
    # Stretched far from the mean in ATR units -> statistical pull back.
    mean_reversion_score = float(np.clip(-z / 3.0, -1, 1))
    if abs(z) >= 2:
        reasons.append(f"Price is {abs(z):.1f} ATR {'above' if z > 0 else 'below'} its 20-bar mean")

    returns = close.pct_change().dropna()
    recent = returns.tail(10)
    drift = float(recent.mean())
    noise = float(returns.tail(60).std() or 0.0)
    # t-like statistic: drift measured in standard errors, so a big move in a
    # wild stock counts for less than the same move in a calm one.
    t_stat = drift / (noise / np.sqrt(len(recent))) if noise else 0.0
    momentum_score = float(np.clip(t_stat / 3.0, -1, 1))
    if abs(t_stat) >= 2:
        reasons.append(f"Recent drift is {abs(t_stat):.1f} standard errors from flat")

    score = 0.5 * momentum_score + 0.5 * mean_reversion_score
    return {
        "available": True,
        "score": float(np.clip(score, -1, 1)),
        "confidence": float(np.clip(abs(score), 0.1, 0.8)),
        "reasons": reasons,
        "detail": {"z_score_atr": round(float(z), 3), "drift_t_stat": round(float(t_stat), 3),
                   "momentum_score": round(momentum_score, 3),
                   "mean_reversion_score": round(mean_reversion_score, 3)},
    }


def volume_component(snapshot: dict) -> dict:
    vol = snapshot.get("volume") or {}
    if not vol.get("available"):
        return {"available": False, "reason": "no volume data", "score": 0.0, "confidence": 0.0,
                "reasons": []}
    reasons = []
    obv_slope = vol.get("obv_slope", 0.0) or 0.0
    avg_vol = vol.get("avg_volume") or 1.0
    normalised = float(np.clip(obv_slope / avg_vol, -1, 1)) if avg_vol else 0.0
    score = normalised * 0.8
    if vol.get("spike"):
        # A volume spike amplifies whatever direction OBV says; it does not
        # have a direction of its own.
        score *= 1.25
        reasons.append(f"Volume spike: {vol['volume_ratio']:.1f}x the 20-bar average")
    if vol.get("dry_up"):
        score *= 0.5
        reasons.append("Volume has dried up - moves here are easy to fake")
    if vol.get("confirms_price"):
        reasons.append("Volume is confirming the price move")
    else:
        score *= 0.6
        reasons.append("Volume is diverging from price - the move lacks participation")
    return {
        "available": True,
        "score": float(np.clip(score, -1, 1)),
        "confidence": 0.5 if vol.get("confirms_price") else 0.3,
        "reasons": reasons,
        "detail": {"volume_ratio": vol.get("volume_ratio"), "obv_slope": obv_slope},
    }


def market_context_component(context: dict | None) -> dict:
    """The broader market. A long in a stock that looks perfect while NIFTY
    is falling apart is still a long into a falling market."""
    if not context or not context.get("available"):
        return {"available": False, "reason": "market context unavailable", "score": 0.0,
                "confidence": 0.0, "reasons": []}
    score = float(np.clip(context.get("score", 0.0), -1, 1))
    reasons = context.get("reasons") or []
    return {
        "available": True,
        "score": score,
        "confidence": float(context.get("confidence", 0.4)),
        "reasons": reasons,
        "detail": context.get("detail"),
    }


# ---------------------------------------------------------------------------
# Adaptive weighting
# ---------------------------------------------------------------------------

def performance_multipliers(regime_primary: str, timeframe: str) -> dict:
    """Turn each component's measured hit rate into a bounded weight
    multiplier. Below MIN_SAMPLES_FOR_ADAPTATION resolved predictions the
    multiplier stays 1.0 - adapting off six samples is superstition."""
    rows = market_store.component_performance(regime=regime_primary, timeframe=timeframe)
    multipliers = {c: 1.0 for c in COMPONENTS}
    detail = {}
    lo, hi = PERFORMANCE_MULTIPLIER_RANGE
    for row in rows:
        comp = row["component"]
        if comp not in multipliers:
            continue
        if row["samples"] < MIN_SAMPLES_FOR_ADAPTATION or row["hit_rate"] is None:
            detail[comp] = {"samples": row["samples"], "hit_rate": row["hit_rate"], "multiplier": 1.0}
            continue
        # 50% hit rate -> 1.0; every point above/below moves the multiplier
        # linearly, clamped to the allowed range.
        mult = float(np.clip(1.0 + (row["hit_rate"] - 0.5) * 2.0, lo, hi))
        multipliers[comp] = mult
        detail[comp] = {"samples": row["samples"], "hit_rate": row["hit_rate"],
                        "multiplier": round(mult, 3)}
    return {"multipliers": multipliers, "detail": detail}


def fuse(components: dict, regime_info: dict, timeframe: str,
         approximated: bool = False) -> dict:
    """Combine component votes into one direction + confidence."""
    perf = performance_multipliers(regime_info.get("primary", "SIDEWAYS"), timeframe)
    multipliers = perf["multipliers"]

    effective, contributions = {}, {}
    for name in COMPONENTS:
        comp = components.get(name) or {}
        if not comp.get("available"):
            continue
        weight = BASE_WEIGHTS[name] * multipliers.get(name, 1.0)
        effective[name] = weight

    total_weight = sum(effective.values())
    if total_weight <= 0:
        return {
            "score": 0.0, "direction": 0, "confidence": 0.0,
            "recommendation": "NO_TRADE",
            "reason": "No component had usable data.",
            "components": components, "weights": {}, "agreement": 0.0,
        }

    combined = 0.0
    for name, weight in effective.items():
        normalised_weight = weight / total_weight
        contribution = components[name]["score"] * normalised_weight
        combined += contribution
        contributions[name] = {
            "weight": round(normalised_weight, 4),
            "score": round(float(components[name]["score"]), 4),
            "contribution": round(float(contribution), 4),
            "performance_multiplier": round(multipliers.get(name, 1.0), 3),
        }

    # Agreement: the share of weight voting the same way as the result. Five
    # components pulling in opposite directions should not produce a
    # confident answer just because they happen to net out to a big number.
    direction = 1 if combined > 0 else (-1 if combined < 0 else 0)
    if direction != 0:
        agreeing = sum(effective[n] for n in effective
                       if np.sign(components[n]["score"]) == direction)
        agreement = agreeing / total_weight
    else:
        agreement = 0.0

    # Confidence calibration. Two honest caveats, stated here because this
    # number drives whether a trade happens at all:
    #  - it is a heuristic, NOT a calibrated probability. Whether 70% here
    #    means 70% in reality is answered by the Predictions tab's measured
    #    hit rate, not by this formula.
    #  - component scores rarely approach +/-1 even when a setup is textbook,
    #    so |combined| is measured against 0.5 rather than 1.0. Anchoring on
    #    1.0 made a unanimous read in a confirmed trend score ~0.4 and the
    #    engine never traded at all, which is a broken instrument rather
    #    than a cautious one.
    strength = float(np.clip(abs(combined) / 0.5, 0.0, 1.0))
    confidence = (0.35 + 0.5 * strength) * (0.55 + 0.45 * agreement)

    penalties = []
    flags = regime_info.get("flags") or []
    if "LOW_VOLUME" in flags:
        confidence *= 0.8
        penalties.append("thin volume")
    if "HIGH_VOLATILITY" in flags:
        confidence *= 0.85
        penalties.append("elevated volatility")
    if regime_info.get("confidence", 0.5) < 0.4:
        confidence *= 0.9
        penalties.append("unclear market regime")
    if approximated:
        confidence *= 0.9
        penalties.append("timeframe is approximated from coarser bars")
    if len(effective) < 4:
        # Fewer voices, less corroboration. Scales with how many are missing
        # rather than being a single cliff at three.
        coverage_multiplier = min(1.0, 0.6 + 0.1 * len(effective))
        confidence *= coverage_multiplier
        penalties.append(f"only {len(effective)} of {len(COMPONENTS)} components had data")

    confidence = float(np.clip(confidence, 0.0, 0.92))

    return {
        "score": round(float(combined), 4),
        "direction": direction,
        "confidence": round(confidence, 4),
        "agreement": round(float(agreement), 4),
        "weights": contributions,
        "performance_detail": perf["detail"],
        "penalties": penalties,
        "components": components,
    }


def classify(score: float, confidence: float, min_confidence: float) -> str:
    """Map the fused score to the label set the UI shows."""
    if confidence < min_confidence:
        return "NO_TRADE"
    if score >= 0.45:
        return "STRONG_BUY"
    if score >= 0.15:
        return "BUY"
    if score <= -0.45:
        return "STRONG_SELL"
    if score <= -0.15:
        return "SELL"
    return "HOLD"


def record_component_outcomes(components_json: dict | None, regime_primary: str,
                              timeframe: str, actual_direction: int):
    """After a prediction resolves, score each component that took a side.
    This is what makes the weights adaptive over time."""
    if not components_json or actual_direction == 0:
        return
    for name in COMPONENTS:
        comp = components_json.get(name)
        if not comp or not comp.get("available"):
            continue
        score = comp.get("score", 0.0)
        if abs(score) < 0.05:
            continue  # took no real side - nothing to grade
        component_direction = 1 if score > 0 else -1
        market_store.record_component_outcome(
            name, regime_primary or "SIDEWAYS", timeframe,
            hit=(component_direction == actual_direction),
        )
