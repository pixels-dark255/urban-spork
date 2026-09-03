"""
Prediction history and accuracy analytics.

Every recommendation the engine commits to is stored with everything needed
to grade it later: entry, stop, target, confidence, regime, indicators and
each component's vote. Once the horizon passes, `resolve_due()` finds out
what actually happened and writes the outcome back.

Resolution walks the bars printed between the prediction and its target time
rather than only comparing the final price, so "target hit then price came
back" is recorded as a win, which is what a real trade with a target order
would have been.

The accuracy numbers this produces are the platform's own report card, and
the only reason to trust (or distrust) its confidence scores. They are
broken down by timeframe, stock, regime and recommendation, because an
overall hit rate hides the thing worth knowing: which conditions this engine
is actually any good in.
"""
from __future__ import annotations

import datetime as dt

import ensemble
import market_data
import market_store


def _outcome_from_bars(pred: dict) -> tuple[float, str] | None:
    """Did the target or the stop trade first, between made_at and target_at?"""
    try:
        made_at = dt.datetime.fromisoformat(pred["made_at"])
        target_at = dt.datetime.fromisoformat(pred["target_at"])
    except (TypeError, ValueError):
        return None
    made_ts = int(made_at.replace(tzinfo=dt.timezone.utc).timestamp())
    target_ts = int(target_at.replace(tzinfo=dt.timezone.utc).timestamp())

    bars = market_data.get_bars(pred["symbol"], pred["exchange"], pred["timeframe"])
    if bars is None or bars.empty:
        return None

    stop, target = pred.get("stop_loss"), pred.get("target_price")
    long_side = (pred.get("direction") or 0) > 0
    last_close = None
    for idx, row in bars.iterrows():
        ts = idx.timestamp() if hasattr(idx, "timestamp") else None
        if ts is None or ts <= made_ts:
            continue
        if ts > target_ts:
            break
        high, low, close = float(row["High"]), float(row["Low"]), float(row["Close"])
        last_close = close
        if stop is not None and ((low <= stop) if long_side else (high >= stop)):
            return float(stop), "stop"
        if target is not None and ((high >= target) if long_side else (low <= target)):
            return float(target), "target"
    if last_close is None:
        return None
    return last_close, "expired"


def resolve_due(limit: int = 200) -> dict:
    """Grade every prediction whose horizon has passed. Runs on every tick,
    including outside market hours - a horizon that expired on Friday
    evening deserves grading before Monday, otherwise nothing ever learns
    across a weekend."""
    now_iso = dt.datetime.utcnow().isoformat()
    due = market_store.due_predictions(now_iso, limit=limit)
    resolved, skipped = 0, 0
    for pred in due:
        try:
            outcome = _outcome_from_bars(pred)
            if outcome is None:
                skipped += 1
                continue
            actual_price, reason = outcome
            entry = pred["entry_price"]
            move_pct = ((actual_price - entry) / entry * 100) if entry else 0.0
            direction = pred.get("direction") or 0
            actual_direction = 1 if actual_price > entry else (-1 if actual_price < entry else 0)
            correct = None if (direction == 0 or actual_direction == 0) else (direction == actual_direction)

            market_store.resolve_prediction(
                pred["id"], round(actual_price, 2), round(move_pct, 3), correct,
                reason if reason != "expired" else ("flat" if actual_direction == 0 else "expired"),
            )
            ensemble.record_component_outcomes(
                pred.get("components"), pred.get("market_regime"), pred["timeframe"], actual_direction
            )
            resolved += 1
        except Exception as e:
            print(f"[warn] could not resolve prediction {pred.get('id')}: {e}")
            skipped += 1
    return {"due": len(due), "resolved": resolved, "skipped": skipped}


def _bucket_stats(rows: list[dict]) -> dict:
    graded = [r for r in rows if r.get("correct_direction") is not None]
    hits = [r for r in graded if r["correct_direction"]]
    target_hits = [r for r in rows if r.get("outcome") == "target"]
    stop_hits = [r for r in rows if r.get("outcome") == "stop"]
    moves = [r["move_pct"] for r in rows if r.get("move_pct") is not None]
    gains = [m for m in moves if m > 0]
    drops = [m for m in moves if m < 0]
    return {
        "predictions": len(rows),
        "graded": len(graded),
        "directional_accuracy_pct": round(100 * len(hits) / len(graded), 1) if graded else None,
        "target_hit_pct": round(100 * len(target_hits) / len(rows), 1) if rows else None,
        "stop_hit_pct": round(100 * len(stop_hits) / len(rows), 1) if rows else None,
        "avg_move_pct": round(sum(moves) / len(moves), 3) if moves else None,
        "avg_gain_pct": round(sum(gains) / len(gains), 3) if gains else None,
        "avg_loss_pct": round(sum(drops) / len(drops), 3) if drops else None,
        "avg_confidence": round(sum(r["confidence"] for r in rows) / len(rows), 3) if rows else None,
    }


def accuracy(client_id: str | None = None, limit: int = 1000) -> dict:
    """The report card, sliced the ways that actually inform a decision."""
    rows = [r for r in market_store.list_predictions(client_id=client_id, limit=limit, resolved=True)]
    if not rows:
        return {
            "resolved_predictions": 0,
            "note": ("No predictions have been graded yet. Accuracy appears here once "
                     "recommendations have had time to play out - which is the only way "
                     "to know whether the confidence scores mean anything."),
        }

    by_timeframe, by_symbol, by_regime, by_recommendation, by_confidence = {}, {}, {}, {}, {}
    for row in rows:
        by_timeframe.setdefault(row["timeframe"], []).append(row)
        by_symbol.setdefault(row["symbol"], []).append(row)
        by_regime.setdefault(row.get("market_regime") or "UNKNOWN", []).append(row)
        by_recommendation.setdefault(row["recommendation"], []).append(row)
        # Confidence calibration buckets: does a 70% call actually land 70%
        # of the time? This is the honest test of the confidence number.
        bucket = f"{int((row['confidence'] or 0) * 10) * 10}-{int((row['confidence'] or 0) * 10) * 10 + 10}%"
        by_confidence.setdefault(bucket, []).append(row)

    return {
        "resolved_predictions": len(rows),
        "overall": _bucket_stats(rows),
        "by_timeframe": {k: _bucket_stats(v) for k, v in sorted(by_timeframe.items())},
        "by_symbol": dict(sorted(
            ((k, _bucket_stats(v)) for k, v in by_symbol.items()),
            key=lambda kv: -kv[1]["predictions"])[:20]),
        "by_market_regime": {k: _bucket_stats(v) for k, v in sorted(by_regime.items())},
        "by_recommendation": {k: _bucket_stats(v) for k, v in sorted(by_recommendation.items())},
        "confidence_calibration": {k: _bucket_stats(v) for k, v in sorted(by_confidence.items())},
        "component_performance": market_store.component_performance(),
        "note": ("Directional accuracy counts a prediction as correct if price moved the way it "
                 "said by the horizon. Target/stop rates use the actual high-low path, so a "
                 "target touched and then given back still counts as reached."),
    }


def history(client_id: str | None = None, symbol: str | None = None,
            timeframe: str | None = None, limit: int = 200) -> list[dict]:
    return market_store.list_predictions(client_id=client_id, symbol=symbol,
                                         timeframe=timeframe, limit=limit)
