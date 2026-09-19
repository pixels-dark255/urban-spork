import numpy as np
import pytest

from conftest import make_bars
import indicators
import regime
import risk


def test_atr_is_positive_and_tracks_range():
    df = make_bars(n=200, noise=2.0, seed=1)
    atr = indicators.atr(df).dropna()
    assert len(atr) > 100
    assert (atr > 0).all()
    calm = indicators.atr(make_bars(n=200, noise=0.2, seed=1)).dropna().iloc[-1]
    wild = indicators.atr(make_bars(n=200, noise=4.0, seed=1)).dropna().iloc[-1]
    assert wild > calm


def test_adx_higher_in_a_trend_than_in_chop():
    trending, _, _ = indicators.adx(make_bars(n=250, trend=120, noise=0.5, seed=2))
    choppy, _, _ = indicators.adx(make_bars(n=250, trend=0, noise=0.5, seed=2))
    assert float(trending.iloc[-1]) > float(choppy.iloc[-1])


def test_support_resistance_brackets_price():
    df = make_bars(n=200, noise=2.0, seed=3)
    levels = indicators.support_resistance(df)
    price = float(df["Close"].iloc[-1])
    assert levels["nearest_support"] < price < levels["nearest_resistance"]


def test_indicator_snapshot_refuses_thin_data():
    assert indicators.indicator_snapshot(make_bars(n=10)) is None


def test_snapshot_has_every_documented_field():
    snap = indicators.indicator_snapshot(make_bars(n=200, seed=4))
    for key in ("price", "ema9", "ema21", "ema50", "rsi", "macd_hist", "atr", "adx",
                "vwap", "bollinger", "volume", "levels"):
        assert key in snap, key


@pytest.mark.parametrize("trend,expected", [(150, "UP"), (-150, "DOWN")])
def test_regime_detects_trend_direction(trend, expected):
    df = make_bars(n=250, trend=trend, noise=0.6, seed=5)
    result = regime.detect(df, indicators.indicator_snapshot(df))
    assert expected in result["primary"]


def test_regime_calls_flat_market_sideways():
    df = make_bars(n=250, noise=1.0, seed=6, ranging=True)
    assert regime.detect(df, indicators.indicator_snapshot(df))["primary"] == "SIDEWAYS"


def test_regime_bias_favours_mean_reversion_in_a_range():
    ranging = regime.strategy_bias({"primary": "SIDEWAYS"})
    trending = regime.strategy_bias({"primary": "STRONG_UPTREND"})
    assert ranging["mean_reversion"] > ranging["trend_following"]
    assert trending["trend_following"] > trending["mean_reversion"]


def test_trade_plan_long_has_stop_below_and_target_above():
    plan = risk.build_trade_plan(1, 1000.0, 8.0, "5m",
                                 levels={"nearest_support": 980.0, "nearest_resistance": 1050.0})
    assert plan["ok"]
    assert plan["stop_loss"] < plan["entry_price"] < plan["target_price"]
    assert plan["risk_reward"] >= 1.5
    assert plan["position_size"] >= 1


def test_trade_plan_short_is_mirrored():
    plan = risk.build_trade_plan(-1, 1000.0, 8.0, "15m",
                                 levels={"nearest_support": 930.0, "nearest_resistance": 1060.0})
    assert plan["ok"]
    assert plan["target_price"] < plan["entry_price"] < plan["stop_loss"]


def test_position_size_respects_the_risk_budget():
    settings = dict(risk.DEFAULT_SETTINGS, capital=100000.0, risk_per_trade_pct=1.0)
    plan = risk.build_trade_plan(1, 1000.0, 10.0, "5m", settings=settings,
                                 levels={"nearest_support": 950.0, "nearest_resistance": 1100.0})
    assert plan["risk_amount"] <= 1000.0 + 1e-6   # never more than 1% of capital


def test_high_volatility_widens_the_stop():
    calm = risk.build_trade_plan(1, 1000.0, 8.0, "5m")
    wild = risk.build_trade_plan(1, 1000.0, 8.0, "5m", regime_flags=["HIGH_VOLATILITY"])
    assert wild["stop_distance"] > calm["stop_distance"]
    assert wild["position_size"] < calm["position_size"]


def test_trade_is_rejected_when_a_level_blocks_the_target():
    """Resistance sitting squarely between entry and the minimum R:R target
    means the trade only pays by breaking through it - that is a reject."""
    plan = risk.build_trade_plan(1, 1000.0, 8.0, "5m",
                                 levels={"nearest_support": 990.0, "nearest_resistance": 1010.0,
                                         "resistance_is_pivot": True, "support_is_pivot": True})
    assert plan["ok"] is False
    assert "1,010" in plan["reason"]


def test_a_level_price_is_already_testing_does_not_block():
    """A pivot two rupees above a 1,000 entry is where price already trades;
    clearing it is the setup, not an obstacle to it. Blocking on those would
    veto every breakout entry the engine could ever take."""
    plan = risk.build_trade_plan(1, 1000.0, 8.0, "5m",
                                 levels={"nearest_support": 990.0, "nearest_resistance": 1002.0,
                                         "resistance_is_pivot": True, "support_is_pivot": True})
    assert plan["ok"] is True


def test_a_fallback_level_at_a_fresh_high_never_blocks():
    """At a fresh high there is no pivot resistance, so support_resistance()
    falls back to the window extreme. That is not structure and must not
    veto the trade."""
    plan = risk.build_trade_plan(1, 1000.0, 8.0, "5m",
                                 levels={"nearest_support": 990.0, "nearest_resistance": 1004.0,
                                         "resistance_is_pivot": False, "support_is_pivot": True})
    assert plan["ok"] is True


def test_no_direction_is_never_a_trade():
    assert risk.build_trade_plan(0, 1000.0, 8.0, "5m")["ok"] is False
