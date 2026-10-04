"""Items 4-9: wrong data, and the storage/scheduler restructure behind it."""
import datetime as dt
import json

import pytest

from conftest import make_frame, make_daily_frame
import intraday
import predictor
import storage
import timeutil


# --- item 5: equity with no quote -------------------------------------------

def test_open_position_is_marked_at_entry_when_no_price_is_available():
    """A missing quote used to look exactly like the stock going to zero."""
    portfolio = {
        "cash": 80000.0,
        "position": {"qty": 100, "entry_price": 200.0, "entry_at": timeutil.iso_now(),
                     "timeframe": "5m"},
        "trade_log": [],
    }
    summary = intraday.portfolio_summary(portfolio, None)
    assert summary["equity"] == pytest.approx(80000.0 + 100 * 200.0)
    assert summary["total_pnl"] == pytest.approx(0.0)


def test_open_position_uses_the_live_price_when_there_is_one():
    portfolio = {
        "cash": 80000.0,
        "position": {"qty": 100, "entry_price": 200.0, "entry_at": timeutil.iso_now(),
                     "timeframe": "5m"},
        "trade_log": [],
    }
    summary = intraday.portfolio_summary(portfolio, 210.0)
    assert summary["equity"] == pytest.approx(80000.0 + 100 * 210.0)
    assert summary["total_pnl"] == pytest.approx(1000.0)


def test_flat_portfolio_is_unaffected():
    summary = intraday.portfolio_summary(
        {"cash": 100000.0, "position": None, "trade_log": []}, None)
    assert summary["equity"] == pytest.approx(100000.0)


# --- item 7: band width -----------------------------------------------------

def test_short_horizons_use_trading_minutes():
    assert predictor.horizon_in_years(15) == pytest.approx(15 / (252 * 375))


def test_horizons_of_a_day_or_more_stay_on_the_calendar_basis():
    assert predictor.horizon_in_years(1440) == pytest.approx(1440 / (365 * 24 * 60))


def test_intraday_band_is_wider_than_the_old_calendar_basis():
    """The reported symptom: short-horizon bands were ~2.4x too narrow
    because the horizon used calendar minutes while volatility was
    annualised over trading periods."""
    frames = {label: make_frame(n=60) for label in predictor.BASE_TIMEFRAME_WEIGHTS}
    result = predictor.predict_price(
        timeframe_data=frames, current_price=100.0, horizon_minutes=15,
        news_articles=[], weather_json={},
    )
    width = result["band_68"][1] - result["band_68"][0]
    old_t = 15 / (365 * 24 * 60)
    new_t = predictor.horizon_in_years(15)
    assert (new_t / old_t) ** 0.5 == pytest.approx(2.358, abs=0.01)
    assert width > 0


def test_gbm_path_uses_the_same_basis_as_the_point_prediction():
    """The shaded band on the chart must be the band the number reports."""
    frames = {label: make_frame(n=60) for label in predictor.BASE_TIMEFRAME_WEIGHTS}
    result = predictor.predict_price(
        timeframe_data=frames, current_price=100.0, horizon_minutes=240,
        news_articles=[], weather_json={},
    )
    path = predictor.gbm_path(100.0, result["mu_annualized"],
                              result["sigma_annualized"], 240)
    assert path[-1]["low_68"] == pytest.approx(result["band_68"][0], abs=0.02)
    assert path[-1]["high_68"] == pytest.approx(result["band_68"][1], abs=0.02)


# --- item 6: resolve at the target time -------------------------------------

def test_price_at_time_uses_the_bar_at_the_target_not_the_latest(fake_yf):
    """The whole point: the scheduler may run hours late (Render sleeps),
    and the price then is not the price the prediction was about."""
    import data_sources

    frame = make_frame(n=60, start=100.0, step=1.0)
    fake_yf.set_default(frame)

    target = frame.index[20].tz_convert("UTC")
    made = frame.index[0].tz_convert("UTC")
    price = data_sources.price_at_time("TEST.NS", target.isoformat(), made.isoformat())

    assert price == pytest.approx(float(frame["Close"].iloc[20]))
    assert price != pytest.approx(float(frame["Close"].iloc[-1]))


def test_price_at_time_takes_the_last_bar_at_or_before_the_target(fake_yf):
    import data_sources

    frame = make_frame(n=60, start=100.0, step=1.0, freq="5min")
    fake_yf.set_default(frame)
    # Two minutes past bar 10, before bar 11 prints.
    target = frame.index[10].tz_convert("UTC") + dt.timedelta(minutes=2)
    price = data_sources.price_at_time(
        "TEST.NS", target.isoformat(), frame.index[0].tz_convert("UTC").isoformat())
    assert price == pytest.approx(float(frame["Close"].iloc[10]))


def test_price_at_time_skips_when_no_bar_exists_after_the_prediction(fake_yf):
    """Prediction made after the close on Friday, target on Saturday: the
    market has not traded since, so there is nothing to grade against."""
    import data_sources

    frame = make_frame(n=60)
    fake_yf.set_default(frame)
    made = frame.index[-1].tz_convert("UTC") + dt.timedelta(hours=1)
    target = made + dt.timedelta(hours=2)
    assert data_sources.price_at_time("TEST.NS", target.isoformat(), made.isoformat()) is None


def test_price_at_time_falls_back_to_daily_for_old_predictions(fake_yf):
    """Beyond Yahoo's intraday window, daily bars are all there is."""
    import data_sources

    daily = make_daily_frame(n=400)
    fake_yf.set_default(make_frame(n=5))
    fake_yf.set("TEST.NS", "2y", "1d", daily)
    target = daily.index[-30].tz_convert("UTC")
    made = daily.index[-60].tz_convert("UTC")
    price = data_sources.price_at_time("TEST.NS", target.isoformat(), made.isoformat())
    assert price == pytest.approx(float(daily["Close"].iloc[-30]))


def test_price_at_time_returns_none_for_an_unparseable_target(fake_yf):
    import data_sources
    assert data_sources.price_at_time("TEST.NS", "not-a-time", None) is None


# --- item 8: no network inside the storage lock -----------------------------

def _seed_item(ip="1.1.1.1", horizon=1440, predictions=None):
    storage.add_item(ip, "TEST.NS", "Test", horizon)
    if predictions:
        def mutate(data):
            data[ip][0]["predictions"] = predictions
        storage._with_store(mutate)
    return storage.get_watchlist(ip)[0]


def test_resolver_runs_outside_the_storage_lock(isolated_storage):
    """The original code fetched prices from inside the mutator, holding the
    global lock across a blocking network call for every due prediction -
    which is what stalled every other request. threading.Lock is not
    reentrant, so if the lock were still held this acquire would fail."""
    past = (timeutil.utc_now() - dt.timedelta(hours=2)).isoformat()
    _seed_item(predictions=[{
        "made_at": (timeutil.utc_now() - dt.timedelta(hours=3)).isoformat(),
        "target_at": past, "price_at_prediction": 100.0, "predicted_price": 101.0,
        "resolved": False, "tracked": True, "raw_signals": {},
    }])

    observed = {}

    def resolver(symbol, target_at, made_at):
        acquired = storage._lock.acquire(blocking=False)
        observed["lock_was_free"] = acquired
        if acquired:
            storage._lock.release()
        return 102.0

    storage.resolve_due_predictions(timeutil.iso_now(), resolver)
    assert observed["lock_was_free"] is True


def test_resolver_is_called_once_per_distinct_prediction(isolated_storage):
    now = timeutil.utc_now()
    shared_made = (now - dt.timedelta(hours=3)).isoformat()
    shared_target = (now - dt.timedelta(hours=2)).isoformat()
    _seed_item(predictions=[
        {"made_at": shared_made, "target_at": shared_target, "price_at_prediction": 100.0,
         "predicted_price": 101.0, "resolved": False, "tracked": True},
        {"made_at": shared_made, "target_at": shared_target, "price_at_prediction": 100.0,
         "predicted_price": 103.0, "resolved": False, "tracked": True},
    ])
    calls = []

    def resolver(symbol, target_at, made_at):
        calls.append((symbol, target_at, made_at))
        return 102.0

    storage.resolve_due_predictions(timeutil.iso_now(), resolver)
    assert len(calls) == 1, "identical (symbol, target, made) should be fetched once"
    assert all(p["resolved"] for p in storage.get_watchlist("1.1.1.1")[0]["predictions"])


def test_a_failing_resolver_does_not_abort_the_pass(isolated_storage):
    now = timeutil.utc_now()
    _seed_item(predictions=[{
        "made_at": (now - dt.timedelta(hours=3)).isoformat(),
        "target_at": (now - dt.timedelta(hours=2)).isoformat(),
        "price_at_prediction": 100.0, "predicted_price": 101.0, "resolved": False,
    }])

    def resolver(symbol, target_at, made_at):
        raise RuntimeError("yahoo is down")

    assert storage.resolve_due_predictions(timeutil.iso_now(), resolver) == 0
    assert not storage.get_watchlist("1.1.1.1")[0]["predictions"][0]["resolved"]


# --- item 9: one tracked prediction, previews, caps -------------------------

def _prediction(made, horizon_minutes=1440, price=100.0):
    return {
        "made_at": made.isoformat(),
        "target_at": (made + dt.timedelta(minutes=horizon_minutes)).isoformat(),
        "price_at_prediction": price, "predicted_price": price * 1.01,
        "predicted_low": price * 0.99, "predicted_high": price * 1.03,
        "confidence": 0.5, "actual_price": None, "resolved": False,
        "error_pct": None, "raw_signals": {"trend_drift_annualized": 0.1},
    }


def test_only_one_tracked_prediction_is_open_at_a_time(isolated_storage):
    item = _seed_item()
    now = timeutil.utc_now()

    modes = [storage.store_prediction("1.1.1.1", item["id"], _prediction(now + dt.timedelta(minutes=5 * i)))
             for i in range(6)]

    assert modes[0] == "tracked"
    assert set(modes[1:]) == {"preview"}

    stored = storage.get_watchlist("1.1.1.1")[0]
    open_tracked = [p for p in stored["predictions"]
                    if not p.get("resolved") and storage.is_tracked(p)]
    assert len(open_tracked) == 1
    assert stored["latest_preview"]["tracked"] is False


def test_a_new_tracked_prediction_opens_once_the_previous_resolves(isolated_storage):
    item = _seed_item()
    now = timeutil.utc_now()
    storage.store_prediction("1.1.1.1", item["id"], _prediction(now))
    assert storage.store_prediction("1.1.1.1", item["id"], _prediction(now)) == "preview"

    def mutate(data):
        data["1.1.1.1"][0]["predictions"][0]["resolved"] = True
    storage._with_store(mutate)

    assert storage.store_prediction("1.1.1.1", item["id"], _prediction(now)) == "tracked"
    assert "latest_preview" not in storage.get_watchlist("1.1.1.1")[0]


def test_weights_are_nudged_once_per_tracked_prediction(isolated_storage):
    """Overlapping 5-minute predictions all nudged the weights, so they
    saturated at the 0.1x/3.0x clamps within days."""
    item = _seed_item()
    now = timeutil.utc_now()
    for i in range(6):
        storage.store_prediction("1.1.1.1", item["id"],
                                 _prediction(now - dt.timedelta(hours=3) + dt.timedelta(minutes=i)))

    # Force everything due.
    def mutate(data):
        for pred in data["1.1.1.1"][0]["predictions"]:
            pred["target_at"] = (now - dt.timedelta(hours=1)).isoformat()
        preview = data["1.1.1.1"][0].get("latest_preview")
        if preview:
            preview["target_at"] = (now - dt.timedelta(hours=1)).isoformat()
    storage._with_store(mutate)

    nudges = []

    def updater(weights, raw_signals, direction):
        nudges.append(direction)
        return dict(weights or {})

    storage.resolve_due_predictions(timeutil.iso_now(), lambda *a: 105.0, updater)
    assert len(nudges) == 1, f"expected one nudge per tracked prediction, got {len(nudges)}"


def test_previews_are_never_graded(isolated_storage):
    item = _seed_item()
    now = timeutil.utc_now()
    storage.store_prediction("1.1.1.1", item["id"], _prediction(now - dt.timedelta(hours=3)))
    storage.store_prediction("1.1.1.1", item["id"], _prediction(now - dt.timedelta(hours=2)))

    def mutate(data):
        data["1.1.1.1"][0]["predictions"][0]["target_at"] = (now - dt.timedelta(hours=1)).isoformat()
    storage._with_store(mutate)

    storage.resolve_due_predictions(timeutil.iso_now(), lambda *a: 105.0)
    stored = storage.get_watchlist("1.1.1.1")[0]
    assert stored["predictions"][0]["resolved"] is True
    assert stored["latest_preview"].get("resolved") is False


def test_stored_predictions_are_capped(isolated_storage):
    item = _seed_item()
    now = timeutil.utc_now()
    for i in range(storage.MAX_STORED_PREDICTIONS + 25):
        storage.append_prediction("1.1.1.1", item["id"], _prediction(now, price=100.0 + i))
    predictions = storage.get_watchlist("1.1.1.1")[0]["predictions"]
    assert len(predictions) == storage.MAX_STORED_PREDICTIONS
    # The cap keeps the newest, not the oldest.
    assert predictions[-1]["price_at_prediction"] == pytest.approx(
        100.0 + storage.MAX_STORED_PREDICTIONS + 24)


# --- backward compatibility -------------------------------------------------

def test_old_format_data_still_loads_and_resolves(isolated_storage):
    """Old rows: naive timestamps with no offset, no `tracked` flag, and a
    long prediction list. All of it must keep working untouched."""
    now = timeutil.utc_now().replace(tzinfo=None)
    old_predictions = [
        {
            "made_at": (now - dt.timedelta(hours=5, minutes=i)).isoformat(),
            "target_at": (now - dt.timedelta(hours=4, minutes=i)).isoformat(),
            "price_at_prediction": 100.0, "predicted_price": 101.0,
            "confidence": 0.5, "resolved": True, "actual_price": 100.5,
            "error_pct": -0.5, "correct_direction": True,
        }
        for i in range(600)
    ]
    old_predictions.append({
        "made_at": (now - dt.timedelta(hours=3)).isoformat(),
        "target_at": (now - dt.timedelta(hours=2)).isoformat(),
        "price_at_prediction": 100.0, "predicted_price": 101.0,
        "confidence": 0.5, "resolved": False, "raw_signals": {"trend_drift_annualized": 0.2},
    })

    with open(storage.STORE_PATH, "w") as f:
        json.dump({"9.9.9.9": [{
            "id": 1, "symbol": "OLD.NS", "display_name": "Old",
            "horizon_minutes": 1440,
            "created_at": (now - dt.timedelta(days=30)).isoformat(),
            "predictions": old_predictions,
            "signal_weights": {"trend": 1.0, "momentum": 1.0, "news": 1.0,
                               "seasonality": 1.0, "weather": 1.0},
            "weights_history": [], "backtest_history": [], "backtest_summary": None,
        }]}, f)

    item = storage.get_watchlist("9.9.9.9")[0]
    assert len(item["predictions"]) == 601

    # The unresolved old row has no `tracked` key and must still be graded.
    nudges = []
    storage.resolve_due_predictions(
        timeutil.iso_now(), lambda *a: 102.0,
        lambda w, s, d: (nudges.append(d), dict(w))[1])
    assert len(nudges) == 1
    assert storage.get_watchlist("9.9.9.9")[0]["predictions"][-1]["resolved"] is True


def test_old_format_feeds_the_accuracy_trend(isolated_storage):
    import main
    now = timeutil.utc_now().replace(tzinfo=None)
    predictions = [
        {"made_at": (now - dt.timedelta(hours=i)).isoformat(),
         "target_at": (now - dt.timedelta(hours=i - 1)).isoformat(),
         "resolved": True, "correct_direction": i % 2 == 0,
         "price_at_prediction": 100.0, "predicted_price": 101.0, "error_pct": 1.0}
        for i in range(10, 0, -1)
    ]
    trend = main._directional_accuracy_trend(predictions)
    assert trend is not None
    assert trend["resolved_count"] == 10
