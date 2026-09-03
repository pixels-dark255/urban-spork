"""
Model registry: train, validate, persist, serve.

The rule this module exists to enforce: a model is only allowed to influence
a live recommendation if it has beaten a naive baseline on data it never saw
during training. Anything else gets stored with `usable: false` and is
ignored by the ensemble. An unvalidated model that quietly votes is worse
than no model, because its votes look identical to a good one's.

Validation is a chronological hold-out, never a random split - shuffling
time series lets the model learn from its own future, which produces
gorgeous validation scores and a system that loses money.

Training data comes from the local bar database, so the longer the platform
runs and collects, the more there is to learn from. Models are retrained on
demand or on a slow schedule, never after every trade: retraining on each
new outcome is how you overfit to the last hour of noise.
"""
from __future__ import annotations

import os
import json
import time
import threading
import datetime as dt

import numpy as np
import pandas as pd

try:
    import joblib
except Exception:  # pragma: no cover - joblib ships with scikit-learn
    joblib = None

import config
import features
import market_store
import market_data
from ml import models

MIN_TRAINING_ROWS = 400        # below this, any model is memorising noise
VALIDATION_FRACTION = 0.3
MIN_EDGE_OVER_BASELINE = 0.02  # must beat "always predict the majority class" by 2pp

_cache_lock = threading.Lock()
_cache: dict[str, dict] = {}


def available_backends() -> list[str]:
    return models.available_backends()


def _key(symbol: str, exchange: str, timeframe: str) -> str:
    return f"{symbol.upper()}_{exchange.upper()}_{timeframe.replace('.', '_')}"


def _model_path(symbol: str, exchange: str, timeframe: str) -> str:
    return os.path.join(config.MODEL_DIR, f"{_key(symbol, exchange, timeframe)}.joblib")


def _meta_path(symbol: str, exchange: str, timeframe: str) -> str:
    return os.path.join(config.MODEL_DIR, f"{_key(symbol, exchange, timeframe)}.json")


def _directional_accuracy(y_true: np.ndarray, y_pred: np.ndarray) -> float | None:
    """Accuracy over the bars where the model actually committed to a
    direction. Predicting 'neutral' forever is not skill, so those rows are
    excluded rather than counted as correct."""
    mask = y_pred != 0
    if mask.sum() == 0:
        return None
    return float((y_true[mask] == y_pred[mask]).mean())


def _baseline_accuracy(y_train: np.ndarray, y_val: np.ndarray) -> float:
    """What you'd score by always guessing the training set's most common
    directional class. The bar every model has to clear."""
    directional = y_train[y_train != 0]
    if len(directional) == 0:
        return 0.5
    majority = 1 if (directional == 1).sum() >= (directional == -1).sum() else -1
    val_directional = y_val[y_val != 0]
    if len(val_directional) == 0:
        return 0.5
    return float((val_directional == majority).mean())


def train_symbol(symbol: str, exchange: str = "NSE", timeframe: str = "5m",
                 bars: pd.DataFrame | None = None, force: bool = False) -> dict:
    """Train and validate a model for one symbol/timeframe. Returns a report
    whether or not a model was saved - a refusal is a result, with a reason."""
    tf = market_data.TIMEFRAMES.get(timeframe)
    if tf is None:
        return {"trained": False, "reason": f"unknown timeframe '{timeframe}'"}
    horizon_bars = market_data.HOLD_BARS.get(timeframe, 10)

    if bars is None:
        bars = market_data.get_bars(symbol, exchange, timeframe, persist=True)
    if bars is None or len(bars) < MIN_TRAINING_ROWS:
        have = 0 if bars is None else len(bars)
        return {
            "trained": False,
            "reason": (f"only {have} bars stored for {symbol} {timeframe}; "
                       f"{MIN_TRAINING_ROWS} needed. Let the collector run for longer."),
            "bars_available": have,
        }

    X, y = features.build_training_set(bars, horizon_bars)
    if len(X) < MIN_TRAINING_ROWS:
        return {"trained": False, "reason": f"only {len(X)} usable feature rows after cleaning",
                "bars_available": len(bars)}

    split = int(len(X) * (1 - VALIDATION_FRACTION))
    X_train, X_val = X.iloc[:split], X.iloc[split:]
    y_train, y_val = y.iloc[:split].values, y.iloc[split:].values
    if len(np.unique(y_train)) < 2 or len(X_val) < 50:
        return {"trained": False, "reason": "not enough class variety or validation rows to judge a model"}

    baseline = _baseline_accuracy(y_train, y_val)
    results = []
    best = None
    for kind in models.available_backends():
        try:
            estimator = models.fit(kind, models.build(kind), X_train.values, y_train)
            preds = models.predict_labels(kind, estimator, X_val.values)
            acc = _directional_accuracy(y_val, preds)
            coverage = float((preds != 0).mean())
            results.append({"model": kind, "val_directional_accuracy": None if acc is None else round(acc, 4),
                            "coverage": round(coverage, 4)})
            if acc is None or coverage < 0.05:
                continue
            if best is None or acc > best["accuracy"]:
                best = {"kind": kind, "estimator": estimator, "accuracy": acc, "coverage": coverage}
        except Exception as e:
            results.append({"model": kind, "error": str(e)})

    if best is None:
        return {"trained": False, "reason": "no model produced usable directional predictions",
                "candidates": results, "baseline_accuracy": round(baseline, 4)}

    usable = best["accuracy"] >= baseline + MIN_EDGE_OVER_BASELINE
    meta = {
        "symbol": symbol.upper(),
        "exchange": exchange.upper(),
        "timeframe": timeframe,
        "model": best["kind"],
        "horizon_bars": horizon_bars,
        "trained_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(),
        "training_rows": int(len(X_train)),
        "validation_rows": int(len(X_val)),
        "val_directional_accuracy": round(best["accuracy"], 4),
        "val_coverage": round(best["coverage"], 4),
        "baseline_accuracy": round(baseline, 4),
        "edge_over_baseline": round(best["accuracy"] - baseline, 4),
        "usable": bool(usable),
        "candidates": results,
        "feature_importances": models.feature_importances(best["kind"], best["estimator"], list(X.columns)),
        "features": list(X.columns),
    }
    if not usable:
        meta["note"] = (
            "Trained but NOT used: it did not beat the majority-class baseline by enough "
            "to be worth trusting. The ensemble ignores it and falls back to the "
            "technical and statistical components."
        )

    if joblib is not None:
        try:
            joblib.dump({"kind": best["kind"], "estimator": best["estimator"],
                         "features": list(X.columns)},
                        _model_path(symbol, exchange, timeframe))
        except Exception as e:
            meta["persist_error"] = str(e)
    with open(_meta_path(symbol, exchange, timeframe), "w") as f:
        json.dump(meta, f, indent=2)

    with _cache_lock:
        _cache.pop(_key(symbol, exchange, timeframe), None)

    return {"trained": True, **meta}


def _load(symbol: str, exchange: str, timeframe: str) -> dict | None:
    key = _key(symbol, exchange, timeframe)
    path, meta_path = _model_path(symbol, exchange, timeframe), _meta_path(symbol, exchange, timeframe)
    if not (os.path.exists(path) and os.path.exists(meta_path)):
        return None
    mtime = os.path.getmtime(path)
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached.get("mtime") == mtime:
            return cached
    if joblib is None:
        return None
    try:
        bundle = joblib.load(path)
        with open(meta_path) as f:
            meta = json.load(f)
    except Exception as e:
        print(f"[warn] could not load model {key}: {e}")
        return None
    entry = {"mtime": mtime, "bundle": bundle, "meta": meta}
    with _cache_lock:
        _cache[key] = entry
    return entry


def predict(symbol: str, exchange: str, timeframe: str, bars: pd.DataFrame) -> dict:
    """ML view on the next `horizon_bars`. Always returns a dict; check
    `available` before believing `score`."""
    entry = _load(symbol, exchange, timeframe)
    if entry is None:
        return {"available": False, "reason": "no trained model for this symbol/timeframe yet"}
    meta = entry["meta"]
    if not meta.get("usable"):
        return {"available": False, "reason": meta.get("note", "model did not beat its baseline"),
                "model": meta.get("model"), "val_directional_accuracy": meta.get("val_directional_accuracy")}

    row = features.latest_feature_row(bars)
    if row is None:
        return {"available": False, "reason": "not enough recent bars to build features"}
    expected = meta.get("features") or list(row.columns)
    try:
        row = row[expected]
    except KeyError:
        return {"available": False, "reason": "feature set changed since training - retrain needed"}

    bundle = entry["bundle"]
    try:
        proba = models.predict_proba(bundle["kind"], bundle["estimator"], row.values)
    except Exception as e:
        return {"available": False, "reason": f"model inference failed: {e}"}

    score = proba["up"] - proba["down"]          # -1..+1
    conviction = max(proba["up"], proba["down"])
    return {
        "available": True,
        "model": meta.get("model"),
        "score": round(float(score), 4),
        "p_up": round(proba["up"], 4),
        "p_down": round(proba["down"], 4),
        "p_neutral": round(proba["neutral"], 4),
        "conviction": round(float(conviction), 4),
        "val_directional_accuracy": meta.get("val_directional_accuracy"),
        "trained_at": meta.get("trained_at"),
        "horizon_bars": meta.get("horizon_bars"),
        "top_features": meta.get("feature_importances"),
    }


def model_status(symbol: str | None = None, exchange: str = "NSE") -> list[dict]:
    """Every trained model on disk, with its validation record."""
    out = []
    if not os.path.isdir(config.MODEL_DIR):
        return out
    for fname in sorted(os.listdir(config.MODEL_DIR)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(config.MODEL_DIR, fname)) as f:
                meta = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if symbol and meta.get("symbol") != symbol.upper():
            continue
        meta.pop("candidates", None)
        out.append(meta)
    return out


def train_all_tracked(timeframes: list[str] | None = None, limit: int = 20) -> list[dict]:
    """Retrain models for every symbol that has enough stored history.
    Called on a slow schedule (see scheduler.py) - not per trade."""
    timeframes = timeframes or ["5m", "15m"]
    reports = []
    with market_store.cursor() as conn:
        rows = conn.execute(
            "SELECT symbol, exchange, COUNT(*) c FROM bars GROUP BY symbol, exchange "
            "ORDER BY c DESC LIMIT ?", (limit,)
        ).fetchall()
    for row in rows:
        for tf in timeframes:
            try:
                reports.append(train_symbol(row["symbol"], row["exchange"], tf))
            except Exception as e:
                reports.append({"trained": False, "symbol": row["symbol"], "timeframe": tf,
                                "reason": str(e)})
    return reports
