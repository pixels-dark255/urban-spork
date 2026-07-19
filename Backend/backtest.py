"""
90-day backtest + adaptive weight refinement, run once when a stock is added
to the watchlist.

What this actually is (see predictor.nudge_weights for the honest framing):
online learning, not code-rewriting. It walks backward through the last ~90
trading days, and for each one:
  1. Builds only the data that would genuinely have been available the day
     before (no lookahead).
  2. Predicts that day's close.
  3. Compares to what actually happened.
  4. Nudges each signal's weight up or down depending on whether its
     direction was right (predictor.nudge_weights).
The final refined weights are then used for this stock's live predictions
going forward, and keep adapting every time a live prediction resolves.

Known limitations (stated here so the numbers don't quietly overclaim):
- Daily-bar data only. Yahoo Finance doesn't provide months of free
  historical intraday (minute/hour) data, so the short-horizon technical
  windows used in live predictions aren't exercised during the backtest.
- No historical news or weather is replayed - there's no free source for
  "sentiment as of 63 days ago" - so those two signals start neutral here
  and are only tuned afterward by live results.
"""
from data_sources import fetch_daily_history
from predictor import predict_price, nudge_weights, DEFAULT_WEIGHTS

WINDOW_TRADING_DAYS = {"1mo": 21, "2mo": 42, "3mo": 63, "6mo": 126, "1y": 252}
BACKTEST_DAYS = 90


def run_backtest_and_refine(yf_symbol: str) -> dict:
    df = fetch_daily_history(yf_symbol, period="2y")
    if df is None or df.empty or "Close" not in df.columns or len(df) < 60:
        return {
            "backtest_history": [],
            "refined_weights": dict(DEFAULT_WEIGHTS),
            "summary": None,
            "note": "Not enough historical daily data to run a backtest for this symbol yet.",
        }

    weights = dict(DEFAULT_WEIGHTS)
    history = []
    n = len(df)

    # Start as far back as 90 trading days, but never before we have enough
    # history for the longest (1y) window.
    start_idx = max(min(WINDOW_TRADING_DAYS.values()) + 1, n - BACKTEST_DAYS)
    start_idx = min(start_idx, n - 1) if n > 1 else 0

    for i in range(start_idx, n):
        timeframe_data = {}
        for label, length in WINDOW_TRADING_DAYS.items():
            lo = max(0, i - length)
            slice_df = df.iloc[lo:i]
            if not slice_df.empty:
                timeframe_data[label] = slice_df
        if not timeframe_data:
            continue

        current_price = float(df["Close"].iloc[i - 1])
        actual_price = float(df["Close"].iloc[i])
        date_label = df.index[i].strftime("%Y-%m-%d")

        result = predict_price(
            timeframe_data=timeframe_data,
            current_price=current_price,
            horizon_minutes=1440,  # one trading day
            news_articles=[],
            weather_json={},
            weights=weights,
        )
        predicted_price = result["predicted_price"]
        raw_signals = result["signals"]
        error_pct = (
            round((actual_price - predicted_price) / predicted_price * 100, 3)
            if predicted_price else None
        )

        history.append({
            "date": date_label,
            "current_price": current_price,
            "predicted_price": predicted_price,
            "actual_price": actual_price,
            "error_pct": error_pct,
            "weights_used": dict(weights),
        })

        actual_direction = 1 if actual_price > current_price else (-1 if actual_price < current_price else 0)
        weights = nudge_weights(weights, raw_signals, actual_direction)

    return {
        "backtest_history": history,
        "refined_weights": weights,
        "summary": _summarize(history),
    }


def _summarize(history: list[dict]) -> dict | None:
    resolved = [h for h in history if h["error_pct"] is not None]
    if not resolved:
        return None
    n = len(resolved)
    third = max(1, n // 3)
    early_chunk = resolved[:third]
    recent_chunk = resolved[-third:]

    def avg_abs_err(chunk):
        errs = [abs(h["error_pct"]) for h in chunk]
        return round(sum(errs) / len(errs), 3) if errs else None

    early = avg_abs_err(early_chunk)
    recent = avg_abs_err(recent_chunk)
    return {
        "total_days_backtested": n,
        "avg_abs_error_pct_overall": avg_abs_err(resolved),
        "avg_abs_error_pct_early_period": early,
        "avg_abs_error_pct_recent_period": recent,
        "improved": (early is not None and recent is not None and recent < early),
    }
