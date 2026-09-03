"""End-to-end tests for the recommendation pipeline, prediction grading and
paper trading. All data is synthetic and stored locally, so nothing here
touches the network."""
import datetime as dt

import pytest

from conftest import make_bars, store_bars
import ensemble
import indicators
import intraday_engine
import market_store
import paper_trading
import predictions as prediction_analytics
import regime
import risk


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No network in tests: the provider always fails, so every engine runs
    off the local bar store - which is also the real-world offline path."""
    class DeadProvider:
        name = "offline"
        def get_bars(self, *a, **k):
            raise ConnectionError("offline in tests")
        def get_quote(self, *a, **k):
            return None
    monkeypatch.setattr("providers.get_provider", lambda *a, **k: DeadProvider())
    monkeypatch.setattr("market_overview.market_context",
                        lambda: {"available": False, "reason": "offline in tests"})


def _seed(symbol, **kwargs):
    df = make_bars(**kwargs)
    store_bars(symbol, df, timeframe="5m")
    return df


def test_engine_refuses_when_there_is_not_enough_data():
    _seed("THIN", n=20, seed=30)
    result = intraday_engine.analyze("THIN", "NSE", "5m", client_id="t", record=False)
    assert result["recommendation"] == "NO_TRADE"
    assert result["data_available"] is False
    assert "bars available" in result["reason"]


def test_engine_rejects_an_unknown_timeframe():
    result = intraday_engine.analyze("ANY", "NSE", "3.7m", client_id="t", record=False)
    assert result["ok"] is False
    assert "30s" in result["supported"]


def test_clean_trend_produces_a_complete_actionable_plan():
    _seed("TRENDY", n=420, trend=120, noise=0.3, volume_trend=True, seed=31)
    result = intraday_engine.analyze("TRENDY", "NSE", "5m", client_id="plan", record=False)
    assert result["recommendation"] in ("BUY", "STRONG_BUY")
    plan = result["trade_plan"]
    # Every field the spec asks a recommendation to carry.
    for field in ("entry_price", "stop_loss", "target_price", "position_size",
                  "risk_amount", "reward_amount", "risk_reward"):
        assert plan[field] is not None, field
    assert plan["stop_loss"] < plan["entry_price"] < plan["target_price"]
    assert 0 < result["confidence"] <= 0.92


def test_recommendation_is_explainable():
    _seed("EXPLAIN", n=420, trend=120, noise=0.3, seed=32)
    result = intraday_engine.analyze("EXPLAIN", "NSE", "5m", client_id="x", record=False)
    assert len(result["explanation"]) >= 3
    assert result["indicators"]["rsi"] is not None
    assert set(result["components"]) == set(ensemble.COMPONENTS)
    # Each contributing component discloses its weight in the final answer.
    assert abs(sum(w["weight"] for w in result["weights"].values()) - 1.0) < 1e-6


def test_noise_does_not_generate_a_trade():
    _seed("NOISE", n=420, noise=3.0, ranging=True, seed=33)
    result = intraday_engine.analyze("NOISE", "NSE", "5m", client_id="n", record=False)
    assert result["recommendation"] in ("NO_TRADE", "HOLD")
    assert result["reason"]


def test_confidence_floor_is_enforced():
    _seed("FLOOR", n=420, trend=120, noise=0.3, seed=34)
    risk.save_settings("strict", {"min_confidence": 0.95})
    result = intraday_engine.analyze("FLOOR", "NSE", "5m", client_id="strict", record=False)
    assert result["recommendation"] == "NO_TRADE"


def test_all_timeframes_run_without_error():
    df = make_bars(n=900, trend=60, noise=0.5, freq="1min", seed=35)
    store_bars("MULTITF", df, timeframe="1m")
    store_bars("MULTITF", df, timeframe="5m")
    for tf in ("30s", "1m"):
        result = intraday_engine.analyze("MULTITF", "NSE", tf, client_id="m", record=False)
        assert result["ok"] and "recommendation" in result


def test_predictions_are_recorded_and_deduplicated():
    _seed("RECORD", n=420, trend=120, noise=0.3, seed=36)
    before = len(market_store.list_predictions(client_id="rec"))
    intraday_engine.analyze("RECORD", "NSE", "5m", client_id="rec", record=True)
    intraday_engine.analyze("RECORD", "NSE", "5m", client_id="rec", record=True)
    after = market_store.list_predictions(client_id="rec")
    # Two runs inside one bar's cooldown must not write two rows.
    assert len(after) - before <= 1


def test_prediction_resolution_grades_a_target_hit():
    """A long whose target trades before its stop must resolve as 'target'."""
    df = make_bars(n=200, trend=40, noise=0.3, seed=37)
    store_bars("RESOLVE", df, timeframe="5m")
    entry = float(df["Close"].iloc[0])
    pred_id = market_store.save_prediction({
        "client_id": "res", "symbol": "RESOLVE", "exchange": "NSE", "timeframe": "5m",
        "made_at": (df.index[0].tz_convert("UTC").tz_localize(None)).isoformat(),
        "horizon_minutes": 50,
        "target_at": (df.index[30].tz_convert("UTC").tz_localize(None)).isoformat(),
        "recommendation": "BUY", "direction": 1, "entry_price": entry,
        "stop_loss": entry - 50, "target_price": entry + 1.0,
        "confidence": 0.7, "market_regime": "UPTREND",
    })
    prediction_analytics.resolve_due()
    row = [p for p in market_store.list_predictions(client_id="res") if p["id"] == pred_id][0]
    assert row["resolved"] is True
    assert row["outcome"] == "target"
    assert row["correct_direction"] is True


def test_accuracy_report_breaks_results_down_by_slice():
    report = prediction_analytics.accuracy(client_id="res")
    assert report["resolved_predictions"] >= 1
    for key in ("by_timeframe", "by_symbol", "by_market_regime",
                "by_recommendation", "confidence_calibration"):
        assert key in report


def test_paper_trade_opens_and_closes_at_the_stop():
    entry = 1000.0
    # Price path that dips through the stop, with the trade opened just
    # before the first of those bars printed.
    df = make_bars(n=100, trend=-60, noise=0.4, start=entry, seed=38)
    store_bars("PAPER", df, timeframe="5m")
    opened_at = (df.index[0].tz_convert("UTC").tz_localize(None) - dt.timedelta(minutes=5))
    trade_id = market_store.open_trade({
        "client_id": "paper", "symbol": "PAPER", "exchange": "NSE", "timeframe": "5m",
        "side": "BUY", "quantity": 10, "entry_price": entry,
        "stop_loss": entry - 5, "target_price": entry + 50,
        "opened_at": opened_at.isoformat(),
    })
    paper_trading.mark_to_market("paper")
    trade = market_store.get_trade(trade_id)
    assert trade["status"] == "CLOSED"
    assert trade["exit_reason"] == "stop"
    assert trade["pnl"] < 0


def test_paper_summary_reports_expectancy():
    summary = paper_trading.summary("paper")
    assert summary["closed_trades"] >= 1
    assert summary["win_rate_pct"] is not None
    assert "expectancy_per_trade" in summary


def test_daily_loss_limit_blocks_further_trades():
    risk.save_settings("blocked", {"capital": 10000.0, "max_daily_loss_pct": 0.5})
    market_store.open_trade({
        "client_id": "blocked", "symbol": "X", "exchange": "NSE", "timeframe": "5m",
        "side": "BUY", "quantity": 1, "entry_price": 100.0, "opened_at": dt.datetime.utcnow().isoformat(),
    })
    trade = market_store.list_trades("blocked", status="OPEN")[0]
    market_store.close_trade(trade["id"], 20.0, "stop", -500.0, -80.0)
    status = risk.daily_loss_status("blocked")
    assert status["breached"] is True
    assert risk.check_trade_allowed("blocked", 0.9)["allowed"] is False


def test_ensemble_needs_agreement_for_confidence():
    regime_info = {"primary": "UPTREND", "flags": [], "confidence": 0.8}
    agreeing = {name: {"available": True, "score": 0.6, "confidence": 0.6}
                for name in ensemble.COMPONENTS}
    conflicting = dict(agreeing)
    conflicting["technical"] = {"available": True, "score": 0.6, "confidence": 0.6}
    conflicting["statistical"] = {"available": True, "score": -0.6, "confidence": 0.6}
    conflicting["volume"] = {"available": True, "score": -0.5, "confidence": 0.6}
    a = ensemble.fuse(agreeing, regime_info, "5m")
    b = ensemble.fuse(conflicting, regime_info, "5m")
    assert a["agreement"] == pytest.approx(1.0)
    assert a["confidence"] > b["confidence"]


def test_missing_components_reduce_confidence():
    regime_info = {"primary": "UPTREND", "flags": [], "confidence": 0.8}
    full = {name: {"available": True, "score": 0.6, "confidence": 0.6}
            for name in ensemble.COMPONENTS}
    partial = {name: ({"available": True, "score": 0.6, "confidence": 0.6}
                      if name in ("technical", "volume") else {"available": False})
               for name in ensemble.COMPONENTS}
    assert ensemble.fuse(full, regime_info, "5m")["confidence"] > \
           ensemble.fuse(partial, regime_info, "5m")["confidence"]


def test_high_volatility_and_thin_volume_cost_confidence():
    calm = {"primary": "UPTREND", "flags": [], "confidence": 0.8}
    rough = {"primary": "UPTREND", "flags": ["HIGH_VOLATILITY", "LOW_VOLUME"], "confidence": 0.8}
    comps = {name: {"available": True, "score": 0.6, "confidence": 0.6}
             for name in ensemble.COMPONENTS}
    assert ensemble.fuse(comps, calm, "5m")["confidence"] > \
           ensemble.fuse(comps, rough, "5m")["confidence"]


def test_classify_respects_the_confidence_floor():
    assert ensemble.classify(0.9, 0.3, 0.55) == "NO_TRADE"
    assert ensemble.classify(0.6, 0.8, 0.55) == "STRONG_BUY"
    assert ensemble.classify(-0.6, 0.8, 0.55) == "STRONG_SELL"
    assert ensemble.classify(0.2, 0.8, 0.55) == "BUY"
    assert ensemble.classify(0.02, 0.8, 0.55) == "HOLD"


def test_component_performance_feeds_back_into_weights():
    for _ in range(30):
        market_store.record_component_outcome("technical", "UPTREND", "5m", hit=True)
        market_store.record_component_outcome("volume", "UPTREND", "5m", hit=False)
    multipliers = ensemble.performance_multipliers("UPTREND", "5m")["multipliers"]
    assert multipliers["technical"] > 1.0 > multipliers["volume"]
    # Bounded, so one hot streak can never hand a component the whole vote.
    assert multipliers["technical"] <= ensemble.PERFORMANCE_MULTIPLIER_RANGE[1]
