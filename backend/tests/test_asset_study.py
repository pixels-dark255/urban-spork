"""Phase 1: labelling how each ticker moves.

The two things most worth pinning are the ones that would silently
flatter every label: measuring a return you could not have traded, and
calling two lucky events an edge.
"""
import numpy as np
import pandas as pd
import pytest

import asset_study as study


def frame(closes, opens=None, start="2022-01-03"):
    idx = pd.bdate_range(start, periods=len(closes))
    data = {"Close": closes, "Open": opens if opens is not None else closes}
    data["High"] = data["Close"]
    data["Low"] = data["Close"]
    return pd.DataFrame(data, index=idx)


def ramp(n, step=1.0, start=100.0):
    return [start + i * step for i in range(n)]


# ------------------------------------------------------- no look-ahead

def test_forward_return_starts_at_the_next_open():
    # Close on day 0 is the signal. The earliest fill is day 1's open, and
    # a 5-day hold exits at day 6's open. Anything measured from day 0's
    # close is a return nobody could have had.
    opens = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]
    out = study.prepare(frame(ramp(8), opens=opens))
    # day 0: entry 20 (day 1 open), exit 70 (day 6 open) -> 2.5
    assert out["fwd5"].iloc[0] == pytest.approx(70 / 20 - 1)


def test_the_last_bars_have_no_forward_return():
    out = study.prepare(frame(ramp(10)))
    assert out["fwd5"].iloc[-1] != out["fwd5"].iloc[-1]       # NaN


def test_the_200_day_average_needs_200_days():
    # indicators.sma starts after half a window on purpose, which would
    # invent an uptrend out of the warm-up and manufacture events here.
    out = study.prepare(frame(ramp(250)))
    assert out["sma200"].iloc[:199].isna().all()
    assert out["sma200"].iloc[199:].notna().all()


def test_no_momentum_event_can_fire_during_the_warm_up():
    out = study.prepare(frame(ramp(250)))
    events = (out["close"] >= out["high50"]) & out["above"]
    assert not events.iloc[:199].any()


# ----------------------------------------------------- the labelling rule

def labelled_frame(hits, forward, horizon=20):
    """A frame whose event days and forward returns are set by hand."""
    n = len(forward)
    idx = pd.bdate_range("2022-01-03", periods=n)
    f = pd.DataFrame(index=idx)
    f["fwd%d" % horizon] = forward
    events = pd.Series(hits, index=idx)
    return f, events


def test_an_edge_with_enough_events_is_labelled():
    forward = [0.01] * 20 + [0.05] * 10          # base 0.0233, hits 0.05
    hits = [False] * 20 + [True] * 10
    f, e = labelled_frame(hits, forward)
    out = study._compare(f, e, 20, 6, "keeps going")
    assert out["labelled"] and out["events"] == 10
    assert out["edge"] > 0


def test_a_real_edge_with_too_few_events_is_not_labelled():
    # Four spectacular events is a story, not evidence.
    forward = [0.0] * 26 + [0.5] * 4
    hits = [False] * 26 + [True] * 4
    f, e = labelled_frame(hits, forward)
    out = study._compare(f, e, 20, 6, "keeps going")
    assert not out["labelled"]
    assert "only 4 events, needs 6" == out["why"]


def test_many_events_with_no_edge_are_not_labelled():
    forward = [0.05] * 20 + [0.01] * 10
    hits = [False] * 20 + [True] * 10
    f, e = labelled_frame(hits, forward)
    out = study._compare(f, e, 20, 6, "keeps going")
    assert not out["labelled"] and out["edge"] < 0
    assert "worse than average" in out["why"]


def test_exactly_the_event_threshold_counts():
    forward = [0.0] * 24 + [0.1] * 6
    hits = [False] * 24 + [True] * 6
    f, e = labelled_frame(hits, forward)
    assert study._compare(f, e, 20, 6, "x")["labelled"]


def test_an_event_with_no_forward_return_is_not_counted():
    # An event in the last 20 sessions has no outcome yet. Counting it as
    # flat would drag the average toward zero in the busiest recent weeks.
    forward = [0.02] * 10 + [np.nan] * 3
    hits = [True] * 13
    f, e = labelled_frame(hits, forward)
    assert study._compare(f, e, 20, 6, "x")["events"] == 10


def test_reversion_needs_twelve_events_not_six():
    assert study.REVERSION_MIN_EVENTS == 12
    assert study.MOMENTUM_MIN_EVENTS == 6


# --------------------------------------------------------- the measures

def test_trend_gap_uses_yesterdays_state():
    # If today's close both decides "above" and supplies today's return,
    # a big up day partly defines its own bucket and the gap inflates.
    closes = ramp(260)
    out = study.prepare(frame(closes))
    gap = study.trend_gap(out)
    assert gap["days_above"] > 0
    # A pure uptrend never sits below its own average once warmed up.
    assert gap["below"] is None or gap["gap"] is not None


def test_trend_gap_is_positive_when_uptrends_pay():
    up = ramp(240)
    down = [up[-1] - i * 1.5 for i in range(120)]
    out = study.prepare(frame(up + down))
    gap = study.trend_gap(out)
    assert gap["above"] > gap["below"]
    assert gap["gap"] > 0


def test_calm_vs_volatile_sees_a_bigger_move_when_volatile():
    rng = np.random.default_rng(0)
    calm = 100 + np.cumsum(rng.normal(0, 0.2, 400))
    rough = calm[-1] + np.cumsum(rng.normal(0, 2.0, 400))
    out = study.prepare(frame(list(calm) + list(rough)))
    measure = study.calm_vs_volatile(out)
    assert measure["volatile"] > measure["calm"]
    assert measure["ratio"] > 1


def test_calm_vs_volatile_is_none_without_enough_history():
    out = study.prepare(frame(ramp(30)))
    assert study.calm_vs_volatile(out)["ratio"] is None


# ------------------------------------------------------------- safety

def engine_frames(n=400, seed=1):
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(rng.normal(0, 1.0, n))
    return study.prepare(frame(list(closes)))


def test_a_ticker_that_mirrors_the_engine_is_not_safe():
    engine = engine_frames()
    mirror = engine.copy()
    out = study.safety(mirror, {"NIFTYBEES.NS": engine})
    assert not out["labelled"]
    assert out["max_correlation"] == pytest.approx(1.0, abs=1e-6)


def test_an_uncorrelated_riser_is_safe():
    engine = engine_frames(seed=2)
    rng = np.random.default_rng(99)
    calm = 100 + np.cumsum(rng.normal(0.05, 0.2, len(engine)))
    mine = study.prepare(frame(list(calm)))
    out = study.safety(mine, {"NIFTYBEES.NS": engine})
    assert out["max_correlation"] < study.SAFE_MAX_CORRELATION
    assert out["labelled"] == (out["mean_return"] > 0)


def test_safety_without_engines_says_so():
    out = study.safety(engine_frames(), {})
    assert not out["labelled"] and "no engine candidates" in out["why"]


def test_losing_money_while_engines_fall_is_never_safe(monkeypatch):
    engine = engine_frames(seed=3)
    falling = study.prepare(frame(list(np.linspace(200, 100, len(engine)))))
    out = study.safety(falling, {"NIFTYBEES.NS": engine})
    if out["mean_return"] is not None and out["mean_return"] <= 0:
        assert not out["labelled"]


# ---------------------------------------------------------- the heatmap

def test_the_heatmap_is_square_and_one_on_the_diagonal():
    frames = {"A": engine_frames(seed=4), "B": engine_frames(seed=5)}
    heat = study.correlation_matrix(frames)
    assert heat["tickers"] == ["A", "B"]
    assert len(heat["matrix"]) == 2 and len(heat["matrix"][0]) == 2
    assert heat["matrix"][0][0] == 1.0 and heat["matrix"][1][1] == 1.0


def test_the_heatmap_is_symmetric():
    frames = {"A": engine_frames(seed=6), "B": engine_frames(seed=7)}
    heat = study.correlation_matrix(frames)
    assert heat["matrix"][0][1] == heat["matrix"][1][0]


# ------------------------------------------------------------ the study

def sample_daily():
    rng = np.random.default_rng(11)
    out = {}
    for i, name in enumerate(["AAA.NS", "BBB.NS", "NIFTYBEES.NS"]):
        closes = 100 + np.cumsum(rng.normal(0.03, 1.0, 400))
        out[name] = frame(list(closes))
    return out


def test_every_study_ticker_gets_a_row_and_a_sentence():
    result = study.study(sample_daily(), ["AAA.NS", "BBB.NS"], ["NIFTYBEES.NS"])
    assert [r["ticker"] for r in result["rows"]] == ["AAA.NS", "BBB.NS"]
    for row in result["rows"]:
        assert row["summary"].startswith(row["ticker"])
        assert row["summary"].endswith(".")


def test_a_ticker_with_no_data_is_reported_missing_not_dropped():
    result = study.study(sample_daily(), ["AAA.NS", "GONE.NS"], ["NIFTYBEES.NS"])
    assert result["missing"] == ["GONE.NS"]
    assert [r["ticker"] for r in result["rows"]] == ["AAA.NS"]


def test_the_study_is_deterministic():
    import fingerprint as fp
    a = study.study(sample_daily(), ["AAA.NS", "BBB.NS"], ["NIFTYBEES.NS"])
    b = study.study(sample_daily(), ["BBB.NS", "AAA.NS"], ["NIFTYBEES.NS"])
    assert (fp.fingerprint(study.fingerprint_values(a))
            == fp.fingerprint(study.fingerprint_values(b)))


def test_a_ticker_with_no_labels_still_says_something_useful():
    result = study.study(sample_daily(), ["AAA.NS"], ["NIFTYBEES.NS"])
    row = result["rows"][0]
    if not row["labels"]:
        assert "neither a momentum nor a snap-back edge" in row["summary"]


# ----------------------------------------------- warm-up and the window

def dated_frame(n, start="2021-01-04"):
    rng = np.random.default_rng(3)
    closes = 100 + np.cumsum(rng.normal(0.05, 1.0, n))
    return frame(list(closes), start=start)


def test_the_window_has_a_usable_average_on_its_first_day():
    # The whole point of keeping warm-up bars: without them the first 200
    # sessions of the window have no 200-day average and no events fire.
    import datetime
    data = {"AAA.NS": dated_frame(700, start="2021-01-04")}
    win = (datetime.date(2022, 1, 1), datetime.date(2023, 6, 30))
    prepared = study.restrict(study.prepare(data["AAA.NS"]), *win)
    assert prepared["sma200"].iloc[0] == prepared["sma200"].iloc[0]  # not NaN
    assert prepared["high50"].iloc[0] == prepared["high50"].iloc[0]


def test_restrict_keeps_only_rows_inside_the_window():
    import datetime
    prepared = study.prepare(dated_frame(700, start="2021-01-04"))
    win = (datetime.date(2022, 1, 1), datetime.date(2022, 12, 31))
    cut = study.restrict(prepared, *win)
    assert cut.index.min().date() >= win[0]
    assert cut.index.max().date() <= win[1]


def test_no_event_is_scored_with_data_past_the_window():
    # Nothing after the window end is supplied, so an event in the final
    # sessions has no forward return and is dropped rather than scored
    # against locked-window prices.
    import datetime
    win = (datetime.date(2022, 1, 1), datetime.date(2022, 12, 30))
    data = {"AAA.NS": dated_frame(700, start="2021-01-04")}
    cut = {k: study.restrict(v, datetime.date(2020, 1, 1), win[1])
           for k, v in {"AAA.NS": data["AAA.NS"]}.items()}
    prepared = study.prepare(cut["AAA.NS"])
    tail = study.restrict(prepared, *win).tail(study.MOMENTUM_HORIZON)
    assert tail["fwd20"].isna().all()
