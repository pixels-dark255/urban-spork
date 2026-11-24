"""Phase 2: the regime layer, on data whose answer is known by construction.

The point of synthetic data here is that a rising line IS an uptrend and a
noisy stretch IS volatile, so a wrong classification has nowhere to hide.
"""
import numpy as np
import pandas as pd
import pytest

from signals import regime


def frame(closes):
    return pd.DataFrame({"Close": closes},
                        index=pd.bdate_range("2021-01-04", periods=len(closes)))


def rising(n=400, step=1.0):
    return [100.0 + i * step for i in range(n)]


def falling(n=400, step=1.0):
    return [100.0 + n * step - i * step for i in range(n)]


# -------------------------------------------------------------- the trend

def test_a_rising_line_is_an_uptrend_once_warmed_up():
    out = regime.classify(frame(rising()))
    assert (out["trend"].iloc[250:] == regime.TREND_UP).all()


def test_a_falling_line_is_a_downtrend_once_warmed_up():
    out = regime.classify(frame(falling()))
    assert (out["trend"].iloc[250:] == regime.TREND_DOWN).all()


def test_the_warm_up_is_unknown_not_calm_or_up():
    # 199 bars cannot support a 200-day average. Calling the warm-up "up"
    # would let a trend filter pass on evidence that does not exist.
    out = regime.classify(frame(rising()))
    assert out["trend"].iloc[:199].isna().all()
    assert out["trend"].iloc[199:].notna().all()


def test_price_exactly_on_the_average_counts_as_down():
    # A flat line sits exactly on its own average. The boundary must fall
    # one way deliberately rather than by floating-point luck.
    out = regime.classify(frame([100.0] * 300))
    assert (out["trend"].iloc[250:] == regime.TREND_DOWN).all()


def test_a_turn_is_picked_up_after_the_cross():
    up_then_down = rising(260) + falling(200)
    out = regime.classify(frame(up_then_down))
    assert out["trend"].iloc[259] == regime.TREND_UP
    assert out["trend"].iloc[-1] == regime.TREND_DOWN


# ---------------------------------------------------------------- the vol

def test_a_noisy_stretch_is_volatile_once_it_starts():
    rng = np.random.default_rng(0)
    calm = 100 + np.cumsum(rng.normal(0, 0.1, 400))
    wild = calm[-1] + np.cumsum(rng.normal(0, 3.0, 200))
    out = regime.classify(frame(list(calm) + list(wild)))
    # The 40 sessions after the shift, once the 20-day vol has filled with
    # wild bars and before the trailing median has absorbed them.
    just_after = out["vol"].iloc[420:460]
    assert (just_after == regime.VOL_VOLATILE).mean() > 0.9


def test_a_sustained_shift_becomes_the_new_normal():
    """This measure is relative, and that has a consequence worth stating.

    "Volatile" means louder than this ticker's own past year, not louder
    than some absolute bar. After a few hundred wild sessions the trailing
    median is itself wild, and the same behaviour reads as calm again.

    That is the definition working, not a bug - but a strategy that halves
    its size on `volatile` will quietly go back to full size in a
    permanently rougher market, and whoever relies on that rule should
    know it before it happens rather than after.
    """
    rng = np.random.default_rng(0)
    calm = 100 + np.cumsum(rng.normal(0, 0.1, 400))
    wild = calm[-1] + np.cumsum(rng.normal(0, 3.0, 200))
    out = regime.classify(frame(list(calm) + list(wild)))
    assert out["vol"].iloc[425] == regime.VOL_VOLATILE
    assert out["vol"].iloc[-1] == regime.VOL_CALM


def test_vol_is_unknown_until_there_is_a_window():
    out = regime.classify(frame(rising(30)))
    assert out["vol"].iloc[:19].isna().all()


def test_the_median_is_not_dragged_by_one_crisis():
    # A mean would be pulled up by a single violent month, and the quiet
    # months after it would then read as "calm" against an inflated bar.
    rng = np.random.default_rng(1)
    quiet = list(100 + np.cumsum(rng.normal(0, 0.2, 300)))
    crisis = [quiet[-1] * (1 + 0.08 * (-1) ** i) for i in range(20)]
    after = list(crisis[-1] + np.cumsum(rng.normal(0, 0.2, 60)))
    out = regime.classify(frame(quiet + crisis + after))
    assert out["vol"].iloc[-1] == regime.VOL_CALM


# ------------------------------------------------------------- the sizing

def test_half_size_when_volatile():
    assert regime.size_multiple(regime.VOL_CALM) == 1.0
    assert regime.size_multiple(regime.VOL_VOLATILE) == 0.5


def test_an_unknown_regime_is_sized_as_if_volatile():
    # Not knowing is not the same as calm. Being half-sized in a quiet
    # market is the cheaper mistake.
    assert regime.size_multiple(None) == 0.5
    assert regime.size_multiple("anything else") == 0.5


# ------------------------------------------------------------ the filter

def test_no_filter_passes_everything():
    assert regime.passes(None, {"trend": None, "vol": None})
    assert regime.passes("none", {"trend": "down", "vol": "volatile"})


def test_a_trend_filter_only_passes_that_trend():
    assert regime.passes({"trend": "up"}, {"trend": "up", "vol": "calm"})
    assert not regime.passes({"trend": "up"}, {"trend": "down", "vol": "calm"})


def test_a_filter_fails_on_an_unknown_regime():
    # A filter nobody can evaluate must not quietly pass.
    assert not regime.passes({"trend": "up"}, {"trend": None, "vol": "calm"})


def test_both_parts_of_a_filter_must_hold():
    want = {"trend": "up", "vol": "calm"}
    assert regime.passes(want, {"trend": "up", "vol": "calm"})
    assert not regime.passes(want, {"trend": "up", "vol": "volatile"})


# ------------------------------------------------------------- lookup

def test_the_regime_is_read_at_a_finished_session():
    out = regime.classify(frame(rising()))
    when = out.index[300]
    assert regime.at(out, when)["trend"] == regime.TREND_UP


def test_an_unknown_date_returns_no_regime_rather_than_raising():
    out = regime.classify(frame(rising(250)))
    assert regime.at(out, pd.Timestamp("1999-01-01")) == {"trend": None, "vol": None}


# ------------------------------------------------------------- India VIX

def test_vix_is_not_wired_into_the_classifier():
    # The brief allows India VIX only if a free reliable source exists.
    # It has not been verified from this environment, so classify() must
    # not pretend to know it.
    out = regime.classify(frame(rising()))
    assert "vix" not in out.columns


def test_the_vix_helper_still_works_if_a_source_is_ever_confirmed():
    series = pd.Series([10.0] * 100 + [30.0] * 50,
                       index=pd.bdate_range("2022-01-03", periods=150))
    state = regime.vix_state(series)
    assert state.iloc[-1] == "high"
    assert state.iloc[50] == "low"
