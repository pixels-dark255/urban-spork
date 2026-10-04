"""Group A: the two crash classes (items 1-2), plus the safety net under them."""
import math

import numpy as np
import pandas as pd
import pytest

from conftest import make_frame
import indicators
import jsonsafe
import predictor


# --- item 1: NaN volatility -------------------------------------------------

def test_two_bar_timeframe_gives_finite_volatility():
    """A 2-bar frame yields exactly one return, and the sample stddev of one
    observation is NaN. This is the 09:15 case that produced 500s."""
    summary = indicators.summarize_timeframe(make_frame(n=2))
    assert summary is not None
    assert math.isfinite(summary["volatility"])
    assert math.isfinite(summary["mean_return"])


def test_predict_price_survives_every_timeframe_having_two_bars():
    two_bar = {label: make_frame(n=2) for label in predictor.BASE_TIMEFRAME_WEIGHTS}
    result = predictor.predict_price(
        timeframe_data=two_bar, current_price=100.0, horizon_minutes=1440,
        news_articles=[], weather_json={},
    )
    for key in ("predicted_price", "confidence", "mu_annualized", "sigma_annualized"):
        assert math.isfinite(result[key]), key
    for bound in (*result["band_68"], *result["band_95"]):
        assert math.isfinite(bound)


def test_a_single_poisoned_timeframe_cannot_contaminate_the_blend():
    """NaN propagates through a weighted sum, so one bad timeframe used to
    take every other timeframe down with it."""
    frames = {label: make_frame(n=60) for label in predictor.BASE_TIMEFRAME_WEIGHTS}
    poisoned = make_frame(n=60)
    poisoned.iloc[10, poisoned.columns.get_loc("Close")] = np.nan
    poisoned.iloc[11, poisoned.columns.get_loc("Close")] = np.inf
    frames["1mo"] = poisoned
    result = predictor.predict_price(
        timeframe_data=frames, current_price=100.0, horizon_minutes=1440,
        news_articles=[], weather_json={},
    )
    assert math.isfinite(result["predicted_price"])


def test_predict_price_guards_non_finite_inputs_directly():
    empty = {label: pd.DataFrame() for label in predictor.BASE_TIMEFRAME_WEIGHTS}
    result = predictor.predict_price(
        timeframe_data=empty, current_price=100.0, horizon_minutes=15,
        news_articles=[], weather_json={},
    )
    assert math.isfinite(result["sigma_annualized"])
    assert math.isfinite(result["predicted_price"])


def test_gbm_path_guards_non_finite_mu_and_sigma():
    path = predictor.gbm_path(100.0, float("nan"), float("nan"), 60)
    assert len(path) == 13
    for point in path:
        for key in ("mid", "low_68", "high_68", "low_95", "high_95"):
            assert math.isfinite(point[key]), key


# --- item 2: NaN RSI --------------------------------------------------------

def test_rsi_is_100_when_there_are_no_down_bars():
    rising = pd.Series(np.arange(100, 140, dtype=float))
    assert indicators.rsi(rising).iloc[-1] == 100.0


def test_rsi_is_50_on_a_flat_window():
    assert indicators.rsi(pd.Series([100.0] * 40)).iloc[-1] == 50.0


def test_rsi_is_0_when_there_are_no_up_bars():
    falling = pd.Series(np.arange(140, 100, -1, dtype=float))
    assert indicators.rsi(falling).iloc[-1] == 0.0


def test_rsi_keeps_nan_while_the_window_is_incomplete():
    """An unfilled window is missing data, not a degenerate case - callers
    rely on None there rather than a fabricated 50."""
    rising = pd.Series(np.arange(100, 140, dtype=float))
    assert math.isnan(indicators.rsi(rising).iloc[5])


def test_intraday_signal_is_finite_on_strictly_rising_prices():
    import intraday
    signal = intraday.compute_signal(make_frame(n=60, step=1.0))
    assert signal is not None
    for key, value in signal.items():
        if isinstance(value, float):
            assert math.isfinite(value), key
    assert signal["rsi"] == 100.0


def test_intraday_signal_treats_non_finite_rsi_as_neutral(monkeypatch):
    import intraday
    monkeypatch.setattr(intraday, "rsi_series",
                        lambda *a, **k: pd.Series([float("nan")] * 60))
    signal = intraday.compute_signal(make_frame(n=60))
    assert signal["rsi"] == 50.0


# --- the safety net ---------------------------------------------------------

@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_jsonsafe_replaces_non_finite_floats_with_none(value):
    assert jsonsafe.clean(value) is None
    assert jsonsafe.clean({"a": [value]}) == {"a": [None]}


def test_jsonsafe_leaves_good_values_alone():
    payload = {"a": 1.5, "b": [1, "x", True, None], "c": {"d": 0.0}}
    assert jsonsafe.clean(payload) == payload


def test_jsonsafe_handles_numpy_scalars():
    assert jsonsafe.clean(np.float64("nan")) is None
    assert jsonsafe.clean(np.float64(2.5)) == 2.5
    assert jsonsafe.clean(np.int64(7)) == 7


def test_response_class_renders_nan_as_null():
    """Starlette serialises with allow_nan=False, so without the net this
    raises instead of returning a body."""
    import main
    rendered = main.SafeJSONResponse(content={"x": float("nan")}).body
    assert b"null" in rendered
    assert b"NaN" not in rendered


def test_storage_never_writes_nan(isolated_storage):
    import json
    import storage

    storage._save({"1.2.3.4": [{"id": 1, "bad": float("nan")}]})
    with open(storage.STORE_PATH) as f:
        raw = f.read()
    assert "NaN" not in raw
    assert json.loads(raw)["1.2.3.4"][0]["bad"] is None
