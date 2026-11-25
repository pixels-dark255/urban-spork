"""Phase 4: the gates that decide which ideas get capital.

The gates exist to say no. These tests mostly check that they say no for
the right reason, because a funnel that passes everything is decoration
and a funnel that fails everything for the wrong reason is worse.
"""
import pathlib

import pytest
import yaml

from arena.strategies import STRATEGIES
from funnel import backtest, gates, run as run_mod


class FakeIdea:
    def __init__(self, kind="reversion", bar_size="5m"):
        self.kind = kind
        self.bar_size = bar_size


LABELS = gates.load_labels()


# ------------------------------------------------------- 1. fits the asset

def test_a_breakout_idea_may_only_trade_keeps_going_tickers():
    out = gates.gate_fits_asset(FakeIdea(kind="breakout"), LABELS)
    assert out["pass"]
    assert out["symbols"] == ["BHARTIARTL.NS", "INFY.NS", "RELIANCE.NS"]


def test_a_reversion_idea_gets_the_snaps_back_tickers():
    out = gates.gate_fits_asset(FakeIdea(kind="reversion"), LABELS)
    assert "TCS.NS" in out["symbols"] and "SBIN.NS" not in out["symbols"]


def test_momentum_is_treated_as_keeps_going():
    a = gates.gate_fits_asset(FakeIdea(kind="momentum"), LABELS)["symbols"]
    b = gates.gate_fits_asset(FakeIdea(kind="breakout"), LABELS)["symbols"]
    assert a == b


def test_a_mixed_idea_fails_rather_than_being_assigned_a_side():
    # score4 adds a trend signal to an RSI signal to a VWAP signal. There
    # is no asset label that suits it, and guessing one would be the
    # funnel deciding something it has no evidence for.
    out = gates.gate_fits_asset(FakeIdea(kind="mixed"), LABELS)
    assert not out["pass"] and "matches no asset label" in out["why"]


def test_the_benchmark_is_never_given_capital():
    out = gates.gate_fits_asset(FakeIdea(kind="benchmark"), LABELS)
    assert not out["pass"] and "yardstick" in out["why"]


# --------------------------------------------------------- 2, 3, 4

def test_a_losing_training_run_fails():
    assert not gates.gate_training_profit(
        {"ok": True, "net": -1.0, "trades": 30})["pass"]


def test_exactly_break_even_is_not_profitable():
    assert not gates.gate_training_profit(
        {"ok": True, "net": 0.0, "trades": 30})["pass"]


@pytest.mark.parametrize("kind,bar,need", [
    ("reversion", "1d", 15), ("breakout", "1d", 6), ("momentum", "1d", 6),
    ("reversion", "1h", 40), ("breakout", "1h", 15),
    ("reversion", "5m", 40), ("breakout", "15m", 15),
])
def test_trade_minimums_follow_the_brief(kind, bar, need):
    assert gates.minimum_trades(kind, bar) == need


def test_one_trade_short_of_the_minimum_fails():
    idea = FakeIdea(kind="breakout", bar_size="1d")
    assert not gates.gate_enough_trades(idea, {"trades": 5})["pass"]
    assert gates.gate_enough_trades(idea, {"trades": 6})["pass"]


def test_a_profit_factor_just_under_the_bar_fails():
    assert not gates.gate_profit_factor({"profit_factor": 1.29})["pass"]
    assert gates.gate_profit_factor({"profit_factor": 1.30})["pass"]


def test_no_losing_trades_is_undefined_not_excellent():
    # The old code called this 99.0 and the UI printed "99", which reads
    # as a measured result. It means too few trades to have met a loss.
    out = gates.gate_profit_factor({"profit_factor": None, "no_losses": True})
    assert not out["pass"] and "undefined rather than excellent" in out["why"]


# -------------------------------------------------------- 5. neighbours

def test_a_single_spike_optimum_fails():
    out = gates.gate_neighbours([
        {"ok": True, "net": 10.0, "label": "rsi_floor=25"},
        {"ok": True, "net": -5.0, "label": "rsi_floor=35"},
    ])
    assert not out["pass"]
    assert out["losers"] == ["rsi_floor=35"]


def test_all_neighbours_profitable_passes():
    out = gates.gate_neighbours([
        {"ok": True, "net": 3.0, "label": "a=1"},
        {"ok": True, "net": 1.0, "label": "a=3"},
    ])
    assert out["pass"] and out["tested"] == 2


def test_an_idea_with_no_varyable_parameters_cannot_clear_the_gate():
    # Not an oversight: with nothing to vary, a single-spike optimum
    # cannot be ruled out, so the gate must not pass by default.
    out = gates.gate_neighbours([])
    assert not out["pass"] and "cannot be ruled out" in out["why"]


def test_a_break_even_neighbour_counts_as_a_loser():
    out = gates.gate_neighbours([{"ok": True, "net": 0.0, "label": "a=1"}])
    assert not out["pass"]


# ------------------------------------------------------ 6 and 7

def test_doubling_costs_must_still_leave_a_profit():
    assert gates.gate_double_cost({"ok": True, "net": 1.0})["pass"]
    assert not gates.gate_double_cost({"ok": True, "net": -1.0})["pass"]


def test_an_unrun_locked_test_is_a_skip_that_does_not_pass():
    out = gates.gate_locked(None)
    assert not out["pass"] and out["skipped"]
    assert "ONCE per idea" in out["why"]


# --------------------------------------------------- ordering and counts

def test_gates_run_in_the_order_the_brief_gives():
    assert gates.ORDER == ["fits_asset", "training_profit", "enough_trades",
                           "profit_factor", "neighbours", "double_cost", "locked"]


def test_an_idea_stops_at_its_first_failure():
    """A later gate's number is meaningless once an earlier one failed.

    A profit factor computed from four trades is not a profit factor, so
    it must not appear in the report at all.
    """
    labels = {"labels": {}}
    idea = run_mod.Idea(id="x", name="X", kind="reversion", bar_size="5m",
                        strategy=STRATEGIES["rsi_reversion"], neighbours={})
    row = run_mod.run_idea(idea, {}, labels, days=5)
    assert row["failed_at"] == "fits_asset"
    assert set(row["gates"]) == {"fits_asset"}


# ----------------------------------------------------------- drawdown

def test_max_drawdown_is_peak_to_trough():
    assert backtest.max_drawdown(
        [{"net": 10}, {"net": -30}, {"net": 5}, {"net": -20}]) == -45.0


def test_a_run_that_only_rises_has_no_drawdown():
    assert backtest.max_drawdown([{"net": 5}, {"net": 5}]) == 0.0


def test_no_trades_has_no_drawdown():
    assert backtest.max_drawdown([]) == 0.0


# ------------------------------------------------- the variant registry

def test_a_variant_is_removed_from_the_registry_afterwards():
    before = set(STRATEGIES)
    with backtest.registered(STRATEGIES["orb"], "funnel__tmp") as clone:
        assert "funnel__tmp" in STRATEGIES and clone.id == "funnel__tmp"
    assert set(STRATEGIES) == before


def test_the_registry_is_restored_even_if_the_run_raises():
    before = set(STRATEGIES)
    with pytest.raises(RuntimeError):
        with backtest.registered(STRATEGIES["orb"], "funnel__boom"):
            raise RuntimeError("boom")
    assert set(STRATEGIES) == before


def test_a_variant_cannot_shadow_a_real_strategy():
    with pytest.raises(ValueError, match="already a strategy id"):
        with backtest.registered(STRATEGIES["orb"], "rsi_reversion"):
            pass


def test_varying_a_parameter_does_not_touch_the_original():
    base = STRATEGIES["rsi_reversion"]
    variant = base.variant(rsi_floor=25.0)
    assert variant.p("rsi_floor") == 25.0
    assert base.p("rsi_floor") == 30.0


def test_an_unknown_parameter_is_refused():
    with pytest.raises(ValueError, match="no parameter"):
        STRATEGIES["orb"].variant(nonsense=1)


# ------------------------------------------------------------- outputs

def test_the_csv_names_the_gate_each_idea_died_at(tmp_path):
    report = {"rows": [{"id": "x", "name": "X", "kind": "reversion",
                        "bar_size": "5m", "survivor": False,
                        "failed_at": "enough_trades",
                        "gates": {"enough_trades": {"pass": False, "why": "2 trades"}},
                        "training": {"trades": 2, "net": 1.0, "profit_factor": None,
                                     "max_drawdown": 0.0, "return_pct": None},
                        "double": None, "neighbours": []}],
              "counts": {}, "bar_size": "5m", "sessions": 10, "locked_run": False}
    path = run_mod.to_csv(report, tmp_path / "funnel.csv")
    text = path.read_text()
    assert "3. Enough trades" in text and "2 trades" in text


def test_non_survivors_go_to_observe_only_with_no_capital(tmp_path):
    report = {"rows": [
        {"id": "keep", "name": "K", "kind": "reversion", "bar_size": "5m",
         "survivor": True, "failed_at": None, "gates": {},
         "training": {"trades": 50, "net": 100.0, "profit_factor": 1.5,
                      "max_drawdown": -10.0, "symbols": ["TCS.NS"]},
         "double": None, "neighbours": []},
        {"id": "drop", "name": "D", "kind": "breakout", "bar_size": "5m",
         "survivor": False, "failed_at": "double_cost",
         "gates": {"double_cost": {"pass": False, "why": "net -1.00 at 2x cost"}},
         "training": None, "double": None, "neighbours": []}],
        "counts": {}, "bar_size": "5m", "sessions": 10, "locked_run": False}
    path = run_mod.to_bench(report, tmp_path / "bench.yaml")
    doc = yaml.safe_load(path.read_text())
    assert [s["id"] for s in doc["survivors"]] == ["keep"]
    assert [o["id"] for o in doc["observe_only"]] == ["drop"]
    assert "no capital" in path.read_text()


def test_every_rule_becomes_an_idea():
    ideas = run_mod.ideas_from_rules()
    assert {i.id for i in ideas} == set(STRATEGIES)
    assert all(i.kind for i in ideas)


# ------------------------------------------------- 7. the locked-test log

def test_a_locked_run_is_logged_with_its_verdict(tmp_path):
    log = tmp_path / "locked.csv"
    run_mod.log_locked("my_idea", {"ok": True, "net": 50.0, "trades": 30}, path=log)
    text = log.read_text()
    assert "idea_id,date_run,result,notes" in text
    assert "my_idea" in text and "pass" in text


def test_a_losing_locked_run_is_logged_as_a_fail(tmp_path):
    log = tmp_path / "locked.csv"
    run_mod.log_locked("x", {"ok": True, "net": -5.0, "trades": 9}, path=log)
    assert ",fail," in log.read_text()


def test_a_second_run_appends_rather_than_replacing(tmp_path):
    """The whole value of the log is that a rerun is visible.

    An idea appearing twice is the red flag §1.6 exists to raise, so the
    second row must never overwrite the first.
    """
    log = tmp_path / "locked.csv"
    run_mod.log_locked("x", {"ok": True, "net": 1.0, "trades": 20}, path=log)
    run_mod.log_locked("x", {"ok": True, "net": 2.0, "trades": 20}, path=log)
    assert run_mod.locked_runs(log) == {"x": 2}
    assert log.read_text().count("idea_id,") == 1       # one header


def test_an_absent_log_reports_no_runs(tmp_path):
    assert run_mod.locked_runs(tmp_path / "missing.csv") == {}
