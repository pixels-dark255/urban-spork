"""The ML layer's contract: a model only votes if it earned the right to."""
import numpy as np
import pandas as pd

from conftest import make_bars
import features
from ml import models, registry


def _learnable_series(n=1600, seed=5) -> pd.DataFrame:
    """A deliberately predictable series (mean-reverting around a slow
    trend) so the 'a good model is accepted' path is actually exercised.
    Real markets are nothing like this - that is the point of the test."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-05-01 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    base = 1000 + np.linspace(0, 40, n)
    deviation = np.zeros(n)
    for i in range(1, n):
        deviation[i] = deviation[i - 1] * 0.82 + rng.normal(0, 1.5)
    prices = base + deviation
    return pd.DataFrame({"Open": prices, "High": prices + 0.6, "Low": prices - 0.6,
                         "Close": prices, "Volume": rng.integers(1000, 9000, n).astype(float)},
                        index=idx)


def test_feature_frame_has_no_lookahead_columns():
    df = make_bars(n=300, seed=40)
    frame = features.build_feature_frame(df)
    assert list(frame.columns) == features.FEATURE_COLUMNS
    # Every feature for bar i must be computable from bars <= i: truncating
    # the series must not change the features of the bars that remain.
    truncated = features.build_feature_frame(df.iloc[:-20])
    common = frame.index.intersection(truncated.index)
    assert len(common) > 100
    pd.testing.assert_frame_equal(frame.loc[common], truncated.loc[common], atol=1e-9)


def test_labels_use_a_neutral_band():
    df = make_bars(n=300, noise=0.5, seed=41)
    labels = features.build_labels(df, horizon_bars=10).dropna()
    assert set(labels.unique()) <= {-1, 0, 1}
    assert (labels == 0).sum() > 0     # tiny moves are not called "buy"


def test_training_refuses_when_history_is_too_short():
    report = registry.train_symbol("TOOSHORT", "NSE", "5m", bars=make_bars(n=100, seed=42))
    assert report["trained"] is False
    assert "bars" in report["reason"]


def test_a_model_that_beats_its_baseline_is_accepted_and_serves_predictions():
    report = registry.train_symbol("LEARNABLE", "NSE", "5m", bars=_learnable_series())
    assert report["trained"] is True
    assert report["usable"] is True
    assert report["val_directional_accuracy"] > report["baseline_accuracy"]
    prediction = registry.predict("LEARNABLE", "NSE", "5m", _learnable_series())
    assert prediction["available"] is True
    assert -1 <= prediction["score"] <= 1
    assert prediction["top_features"]      # explainable, not a black box


def test_a_model_that_cannot_beat_its_baseline_is_never_used():
    """A pure random walk holds no learnable signal. Anything claiming to
    have found one there is overfitting, and must not be allowed to vote."""
    noise = make_bars(n=1600, noise=1.2, seed=43)
    report = registry.train_symbol("RANDOMWALK", "NSE", "5m", bars=noise)
    assert report["trained"] is True
    assert report["usable"] is False
    prediction = registry.predict("RANDOMWALK", "NSE", "5m", noise)
    assert prediction["available"] is False


def test_prediction_is_unavailable_for_an_untrained_symbol():
    assert registry.predict("NEVERTRAINED", "NSE", "5m", make_bars(n=300))["available"] is False


def test_validation_split_is_chronological_not_shuffled():
    """Shuffling a time series lets a model learn from its own future. The
    source is asserted here because it is the single easiest way to make
    this whole layer silently worthless."""
    import inspect
    source = inspect.getsource(registry.train_symbol)
    assert "iloc[:split]" in source and "iloc[split:]" in source
    assert "shuffle" not in source


def test_available_backends_always_include_a_dependency_free_option():
    backends = models.available_backends()
    assert "random_forest" in backends and "gradient_boosting" in backends
