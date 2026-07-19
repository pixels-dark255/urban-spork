"""
Ensemble prediction engine.

Honesty note (kept here in code, and surfaced in the API response):
No model can reliably predict exact future stock prices - markets are close
to a random walk over short horizons, and genuine "alpha" signals are the
hardest thing in finance to find. This engine combines several legitimate,
transparent methods into one estimate with a confidence range, and - crucially -
tracks its own historical accuracy so you can see exactly how good (or not)
its predictions have actually been for a given stock/horizon.

Methods blended:
 1. Multi-timeframe trend regression - weighted average drift across all the
    lookback windows you asked for (5y down to recent hours), longer windows
    weighted less for short horizons and vice versa.
 2. Momentum/technical tilt - RSI + MACD histogram nudge the drift up/down.
 3. Stochastic (Geometric Brownian Motion) projection - uses estimated
    drift (mu) and volatility (sigma) to project price + a confidence band,
    the same core model used in option pricing.
 4. News sentiment tilt - VADER sentiment over recent headlines nudges drift.
 5. Calendar seasonality - average historical return for this calendar
    month/day-of-week, computed from the stock's own 5y history (this is the
    honest, data-backed version of "seasons affect the stock").
 6. Weather nudge - tiny, optional, low-weight adjustment - included because
    you asked for it, but flagged as experimental/low-confidence.
"""
import datetime as dt
import numpy as np
import pandas as pd
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from indicators import summarize_timeframe, rsi, macd

_analyzer = SentimentIntensityAnalyzer()

# Per-signal weight multipliers, refined over time by the backtest (see
# backtest.py) and by live prediction outcomes (see storage.resolve_due_predictions
# + predictor.nudge_weights below). 1.0 = neutral/unrefined.
DEFAULT_WEIGHTS = {"trend": 1.0, "momentum": 1.0, "news": 1.0, "seasonality": 1.0, "weather": 1.0}

# Weight given to each timeframe's drift estimate, before horizon-based reweighting.
BASE_TIMEFRAME_WEIGHTS = {
    "5y": 0.03, "1y": 0.06, "6mo": 0.08, "3mo": 0.10, "2mo": 0.10, "1mo": 0.12,
    "4wk": 0.10, "3wk": 0.09, "2wk": 0.08, "1wk": 0.08,
    "4d": 0.06, "3d": 0.04, "2d": 0.03, "1d": 0.02, "recent_hours": 0.01,
}

# Rough trading-periods-per-year for each timeframe's interval, used to
# de-annualize / re-annualize returns consistently.
PERIODS_PER_YEAR = {
    "5y": 52, "1y": 252, "6mo": 252, "3mo": 252, "2mo": 252, "1mo": 252,
    "4wk": 252, "3wk": 252, "2wk": 252, "1wk": 252 * 7,
    "4d": 252 * 7, "3d": 252 * 7, "2d": 252 * 7, "1d": 252 * 78, "recent_hours": 252 * 78,
}


def _news_sentiment_score(articles: list[dict]) -> float:
    """Average VADER compound score across headlines+descriptions, in [-1, 1]."""
    if not articles:
        return 0.0
    scores = []
    for a in articles:
        text = f"{a.get('title','')}. {a.get('description','')}"
        if text.strip():
            scores.append(_analyzer.polarity_scores(text)["compound"])
    return float(np.mean(scores)) if scores else 0.0


def _seasonality_drift(df_5y: pd.DataFrame, target_date: dt.date) -> float:
    """Average historical daily return for this calendar month, from 5y weekly/daily data."""
    if df_5y is None or df_5y.empty or "Close" not in df_5y.columns:
        return 0.0
    close = df_5y["Close"].dropna()
    if len(close) < 10:
        return 0.0
    returns = close.pct_change().dropna()
    months = close.index.month[1:]  # aligns with returns after pct_change drop
    same_month_returns = returns[np.array(months) == target_date.month]
    if same_month_returns.empty:
        return 0.0
    return float(same_month_returns.mean())


def _weather_tilt(weather_json: dict) -> float:
    """Tiny experimental nudge. Heavy rain/extreme temps -> slight negative
    tilt for weather-sensitive consumer/agri sectors; near-zero otherwise.
    Deliberately capped small since there is no robust general evidence for this."""
    try:
        current = weather_json.get("current", {})
        precip = current.get("precipitation", 0) or 0
        temp = current.get("temperature_2m", 25) or 25
        tilt = 0.0
        if precip > 20:
            tilt -= 0.0005
        if temp > 42 or temp < 5:
            tilt -= 0.0003
        return tilt
    except Exception:
        return 0.0


def horizon_to_periods_per_year_weighting(horizon_minutes: int) -> dict:
    """Shorter requested horizons should lean on shorter timeframes' drift more."""
    weights = dict(BASE_TIMEFRAME_WEIGHTS)
    if horizon_minutes <= 60 * 6:  # intraday horizon -> lean short-term
        for k in ["1d", "recent_hours", "2d", "3d"]:
            weights[k] *= 3
        for k in ["5y", "1y", "6mo"]:
            weights[k] *= 0.3
    elif horizon_minutes <= 60 * 24 * 7:  # up to a week
        for k in ["1wk", "2wk", "3d", "4d"]:
            weights[k] *= 2
    elif horizon_minutes >= 60 * 24 * 90:  # 3mo+
        for k in ["5y", "1y", "6mo", "3mo"]:
            weights[k] *= 2.5
        for k in ["1d", "recent_hours"]:
            weights[k] *= 0.3
    total = sum(weights.values())
    return {k: v / total for k, v in weights.items()}


def predict_price(
    timeframe_data: dict,
    current_price: float,
    horizon_minutes: int,
    news_articles: list[dict] | None = None,
    weather_json: dict | None = None,
    weights: dict | None = None,
) -> dict:
    """
    timeframe_data: dict[label -> pd.DataFrame] as returned by fetch_multi_timeframe
    weights: per-signal multipliers {trend, momentum, news, seasonality, weather} -
        refined by the 90-day backtest and by live outcomes (see nudge_weights).
        Defaults to all-neutral (1.0) if not provided.
    Returns a dict with predicted_price, low/high band, confidence, and a
    breakdown of each signal so the UI can show its work.
    """
    news_articles = news_articles or []
    weather_json = weather_json or {}
    sig_w = {**DEFAULT_WEIGHTS, **(weights or {})}

    weights = horizon_to_periods_per_year_weighting(horizon_minutes)

    weighted_drift = 0.0
    weighted_vol_annual = 0.0
    total_weight_used = 0.0
    per_timeframe = {}

    for label, df in timeframe_data.items():
        summary = summarize_timeframe(df)
        if not summary:
            continue
        w = weights.get(label, 0.0)
        ppy = PERIODS_PER_YEAR.get(label, 252)
        mean_return = summary["mean_return"]
        vol = summary["volatility"]

        weighted_drift += w * mean_return * ppy  # annualized drift contribution
        weighted_vol_annual += w * vol * np.sqrt(ppy)
        total_weight_used += w
        per_timeframe[label] = summary

    if total_weight_used > 0:
        weighted_drift /= total_weight_used
        weighted_vol_annual /= total_weight_used
    else:
        weighted_drift = 0.0
        weighted_vol_annual = 0.30  # fallback ~30% annual vol if nothing usable

    # --- Momentum tilt from the most recent short timeframe available ---
    momentum_tilt = 0.0
    for label in ["1d", "2d", "3d", "1wk", "1mo"]:
        s = per_timeframe.get(label)
        if s and s.get("rsi") is not None:
            r = s["rsi"]
            if r > 70:
                momentum_tilt -= 0.02  # overbought -> slight pullback tilt
            elif r < 30:
                momentum_tilt += 0.02  # oversold -> slight bounce tilt
            if s.get("macd_hist", 0) > 0:
                momentum_tilt += 0.01
            elif s.get("macd_hist", 0) < 0:
                momentum_tilt -= 0.01
            break

    # --- News sentiment tilt ---
    sentiment = _news_sentiment_score(news_articles)
    sentiment_drift_annual = sentiment * 0.15  # cap sentiment's max annualized pull at ~15%

    # --- Seasonality (from the stock's own 5y history) ---
    target_date = (dt.datetime.utcnow() + dt.timedelta(minutes=horizon_minutes)).date()
    seasonality = _seasonality_drift(timeframe_data.get("5y"), target_date)
    seasonality_annual = seasonality * 252

    # --- Weather (small, experimental) ---
    weather_tilt = _weather_tilt(weather_json)
    weather_annual = weather_tilt * 252

    total_annual_drift = (
        weighted_drift * sig_w["trend"]
        + momentum_tilt * sig_w["momentum"]
        + sentiment_drift_annual * sig_w["news"]
        + seasonality_annual * sig_w["seasonality"]
        + weather_annual * sig_w["weather"]
    )

    # Project forward using GBM over the requested horizon
    t_years = horizon_minutes / (60 * 24 * 365)
    sigma = max(weighted_vol_annual, 0.05)
    mu = total_annual_drift

    expected_log_return = (mu - 0.5 * sigma ** 2) * t_years
    predicted_price = current_price * np.exp(expected_log_return)

    # 68% confidence band (~1 std dev) and 95% band
    band_1sigma = sigma * np.sqrt(t_years)
    low_68 = current_price * np.exp(expected_log_return - band_1sigma)
    high_68 = current_price * np.exp(expected_log_return + band_1sigma)
    low_95 = current_price * np.exp(expected_log_return - 2 * band_1sigma)
    high_95 = current_price * np.exp(expected_log_return + 2 * band_1sigma)

    # Confidence score: shrinks as horizon grows and as volatility grows.
    # This is a heuristic, not a calibrated probability.
    confidence = float(max(0.05, min(0.9, 0.9 - band_1sigma * 2)))

    return {
        "current_price": current_price,
        "predicted_price": round(float(predicted_price), 2),
        "band_68": [round(float(low_68), 2), round(float(high_68), 2)],
        "band_95": [round(float(low_95), 2), round(float(high_95), 2)],
        "confidence": round(confidence, 3),
        "horizon_minutes": horizon_minutes,
        "weights_used": sig_w,
        "signals": {
            "trend_drift_annualized": round(float(weighted_drift), 4),
            "momentum_tilt_annualized": round(float(momentum_tilt), 4),
            "news_sentiment_score": round(float(sentiment), 3),
            "news_drift_annualized": round(float(sentiment_drift_annual), 4),
            "seasonality_drift_annualized": round(float(seasonality_annual), 4),
            "weather_drift_annualized": round(float(weather_annual), 4),
            "volatility_annualized": round(float(sigma), 4),
        },
        "per_timeframe": per_timeframe,
        "disclaimer": (
            "This is a statistical estimate, not financial advice. Stock prices "
            "are close to unpredictable over short horizons - use the confidence "
            "band and this app's own tracked accuracy (see watchlist) to judge "
            "how much to trust it."
        ),
    }


def nudge_weights(weights: dict, raw_signals: dict, actual_direction: int) -> dict:
    """Online-learning weight update (multiplicative-weights / 'Hedge' style):
    each signal whose direction matched the actual price movement gets
    nudged up; each that didn't gets nudged down. Called both by the 90-day
    backtest (backtest.py) and every time a live watchlist prediction
    resolves (scheduler.py + storage.resolve_due_predictions), so refinement
    continues for as long as the stock stays on the watchlist.

    actual_direction: +1 if price went up, -1 if down, 0 if unchanged
    (unchanged days are skipped - no signal to learn from).
    """
    weights = dict(weights or DEFAULT_WEIGHTS)
    if actual_direction == 0:
        return weights
    raw_map = {
        "trend": raw_signals.get("trend_drift_annualized", 0.0),
        "momentum": raw_signals.get("momentum_tilt_annualized", 0.0),
        "news": raw_signals.get("news_drift_annualized", 0.0),
        "seasonality": raw_signals.get("seasonality_drift_annualized", 0.0),
        "weather": raw_signals.get("weather_drift_annualized", 0.0),
    }
    for name, raw_value in raw_map.items():
        if abs(raw_value) < 1e-9:
            continue  # signal made no directional claim - nothing to learn
        signal_direction = 1 if raw_value > 0 else -1
        current = weights.get(name, 1.0)
        if signal_direction == actual_direction:
            weights[name] = min(current * 1.05, 3.0)   # was right - trust it a bit more
        else:
            weights[name] = max(current * 0.95, 0.1)    # was wrong - trust it a bit less
    return weights
