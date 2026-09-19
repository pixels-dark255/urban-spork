"""
Model zoo. Every model implements the same tiny interface, so adding
LightGBM or swapping in a sequential model later touches this file only.

Gradient boosting (XGBoost/LightGBM) is used when installed; otherwise
scikit-learn's own ensembles do the job. That degradation is intentional:
the platform must run on a free tier where a 200MB wheel may not be
installable, and a RandomForest that exists beats an XGBoost that doesn't.

A note on LSTM/sequential models: they are not here yet, and adding one
before the local bar database has months of history in it would be
theatre - a sequential model trained on a few thousand bars overfits
comprehensively. The registry's interface takes any classifier with
fit/predict_proba, so it can be added without touching anything else.
"""
from __future__ import annotations

import numpy as np

from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

CLASSES = [-1, 0, 1]


def _xgboost_available() -> bool:
    try:
        import xgboost  # noqa: F401
        return True
    except Exception:
        return False


def _lightgbm_available() -> bool:
    try:
        import lightgbm  # noqa: F401
        return True
    except Exception:
        return False


def available_backends() -> list[str]:
    backends = ["random_forest", "gradient_boosting", "logistic"]
    if _xgboost_available():
        backends.insert(0, "xgboost")
    if _lightgbm_available():
        backends.insert(0, "lightgbm")
    return backends


def build(kind: str, random_state: int = 42):
    """Construct an unfitted estimator. Hyperparameters are deliberately
    conservative (shallow trees, few estimators, strong regularisation):
    with a few thousand noisy bars, a deep model memorises the noise and
    reports a wonderful training score that means nothing."""
    if kind == "xgboost":
        from xgboost import XGBClassifier
        return XGBClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, reg_lambda=2.0,
            objective="multi:softprob", num_class=3, tree_method="hist",
            random_state=random_state, eval_metric="mlogloss",
        )
    if kind == "lightgbm":
        from lightgbm import LGBMClassifier
        return LGBMClassifier(
            n_estimators=200, max_depth=5, num_leaves=15, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, reg_lambda=2.0,
            random_state=random_state, verbose=-1,
        )
    if kind == "random_forest":
        return RandomForestClassifier(
            n_estimators=250, max_depth=6, min_samples_leaf=20,
            class_weight="balanced_subsample", random_state=random_state, n_jobs=-1,
        )
    if kind == "gradient_boosting":
        return GradientBoostingClassifier(
            n_estimators=150, max_depth=3, learning_rate=0.05,
            subsample=0.8, random_state=random_state,
        )
    if kind == "logistic":
        return Pipeline([
            ("scale", StandardScaler()),
            ("clf", LogisticRegression(max_iter=1000, C=0.5, class_weight="balanced")),
        ])
    raise ValueError(f"unknown model kind '{kind}'")


def _label_encode(y: np.ndarray) -> np.ndarray:
    """XGBoost insists on labels 0..n-1; everything else is happy either way,
    so encode uniformly and decode on the way out."""
    return np.array([CLASSES.index(int(v)) for v in y])


def fit(kind: str, estimator, X, y):
    if kind == "xgboost":
        estimator.fit(X, _label_encode(np.asarray(y)))
    else:
        estimator.fit(X, np.asarray(y))
    return estimator


def predict_proba(kind: str, estimator, X) -> dict:
    """Returns {down, neutral, up} probabilities for the last row of X."""
    proba = estimator.predict_proba(X)[-1]
    if kind == "xgboost":
        classes = CLASSES
    else:
        classes = [int(c) for c in estimator.classes_]
    mapping = {-1: 0.0, 0: 0.0, 1: 0.0}
    for cls, p in zip(classes, proba):
        mapping[int(cls)] = float(p)
    return {"down": mapping[-1], "neutral": mapping[0], "up": mapping[1]}


def predict_labels(kind: str, estimator, X) -> np.ndarray:
    raw = estimator.predict(X)
    if kind == "xgboost":
        return np.array([CLASSES[int(v)] for v in raw])
    return np.asarray(raw).astype(int)


def feature_importances(kind: str, estimator, columns: list[str]) -> dict | None:
    """Which features the model actually leans on - the difference between an
    explainable model and a black box."""
    est = estimator
    if isinstance(est, Pipeline):
        est = est.named_steps.get("clf", est)
    importances = getattr(est, "feature_importances_", None)
    if importances is None:
        coef = getattr(est, "coef_", None)
        if coef is None:
            return None
        importances = np.abs(coef).mean(axis=0)
    importances = np.asarray(importances, dtype=float)
    if importances.sum() <= 0 or len(importances) != len(columns):
        return None
    importances = importances / importances.sum()
    ranked = sorted(zip(columns, importances), key=lambda x: -x[1])[:10]
    return {name: round(float(val), 4) for name, val in ranked}
