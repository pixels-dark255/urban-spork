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


# --- the fill-cost ladder ---------------------------------------------------

def test_the_sweep_runs_every_idea_at_every_multiple(monkeypatch):
    """0x is the row that matters: slippage is the biggest single cost leg
    and the only one nobody has measured."""
    from arena import engine
    from funnel import cli as funnel_cli, run as run_mod

    seen = []

    def fake_run(strategy, symbols, enriched, days, cost_multiple=1.0, capital=None):
        seen.append((strategy.id, cost_multiple))
        return {"ok": True, "trades": 5, "net": -100.0 * cost_multiple,
                "profit_factor": 0.5, "charges": 20.0, "before_charges": -80.0}

    monkeypatch.setattr(run_mod.backtest, "run", fake_run)
    ideas = [i for i in run_mod.ideas_from_rules() if not i.observe_only][:2]
    multiples = [0.0, 1.0, 2.0]
    # Gate 1 decides which assets an idea may trade, and the sweep must use
    # the same ones the funnel does or it is a different experiment.
    labels = funnel_cli.gates.load_labels()
    enriched = {sym: object() for sym in labels["labels"]}
    rows = funnel_cli.sweep(ideas, enriched, labels, 10, multiples)

    assert rows, "the sweep should produce rows for ideas that clear gate 1"
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)
    for row in rows:
        for m in multiples:
            assert (row["id"], m) in seen
        # Never the whole universe: an idea trades what it is entitled to.
        entitled = funnel_cli.gates.gate_fits_asset(
            next(i for i in ideas if i.id == row["id"]), labels)["symbols"]
        assert row["symbols"] == [s for s in entitled if s in enriched]
    # Each row carries a net per multiple, and 0x is not the same run as 1x.
    row = rows[0]
    assert row["at"]["0.0"]["net"] == 0.0
    assert row["at"]["2.0"]["net"] == -200.0
    assert engine.SLIPPAGE_MULTIPLES[0] == 0.0


def test_the_sweep_table_counts_how_many_survive_each_multiple():
    from funnel import cli as funnel_cli

    rows = [
        {"id": "wins_at_zero", "trades": 9, "charges": 10.0,
         "at": {"0.0": {"net": 50.0}, "1.0": {"net": -20.0}}},
        {"id": "loses_always", "trades": 4, "charges": 10.0,
         "at": {"0.0": {"net": -5.0}, "1.0": {"net": -40.0}}},
    ]
    text = funnel_cli.sweep_table(rows, [0.0, 1.0])
    assert "net @ 0x" in text and "net @ 1x" in text
    assert "1 of 2" in text        # one idea is profitable with no slippage
    assert "0 of 2" in text        # none is once slippage is charged
    # The statutory legs are never switched off, and the header says so.
    assert "Statutory charges stay on" in text


def test_before_charges_is_not_called_gross():
    """Slippage is paid in the fill price, not in charges, so a number that
    only adds charges back is not a gross result and must not claim to be."""
    source = pathlib.Path(__file__).resolve().parent.parent / "funnel" / "backtest.py"
    text = source.read_text()
    assert '"before_charges"' in text
    assert '"gross"' not in text


# --- bar sizes --------------------------------------------------------------

def test_the_warm_ups_mean_the_same_at_five_minutes_as_the_bar_counts_did():
    """They were bar counts that silently meant 5-minute bars. In minutes
    they must resolve to exactly the same bars at 5m, or this is a rule
    change wearing a refactor's clothes."""
    from arena import markets as markets_mod
    from arena.strategies import enrich, OPENING_RANGE_BARS, OPENING_RANGE_MINUTES
    from validation import arena_guard

    df = enrich(arena_guard.intraday_bars(sessions=3, seed=7),
                market=markets_mod.get("nse"))
    assert df["bar_minutes"].iloc[0] == 5.0
    # The opening range becomes known on the same bar as before.
    assert int(df.loc[df["or_high"].notna(), "bar_no"].min()) == OPENING_RANGE_BARS - 1
    # And the half-hour close on the bar that closes at 09:45.
    assert int(df.loc[df["close_0945"].notna(), "bar_no"].min()) == 5
    assert OPENING_RANGE_MINUTES == 5 * OPENING_RANGE_BARS


def test_bar_minutes_ignores_the_gap_between_sessions():
    """Overnight is 18 hours. Counting it as a bar would make every warm-up
    a single bar and every opening range the first bar of the day."""
    from arena import markets as markets_mod
    from arena.strategies import enrich, bar_minutes
    from validation import arena_guard

    df = enrich(arena_guard.intraday_bars(sessions=4, seed=7),
                market=markets_mod.get("nse"))
    assert bar_minutes(df) == 5.0


def test_a_bar_longer_than_the_opening_range_uses_one_bar_not_a_fraction():
    """A 15-minute range cannot be measured on hourly bars. Taking the
    first bar and saying so beats inventing the other 45 minutes."""
    import pandas as pd
    from arena import markets as markets_mod
    from arena.strategies import enrich

    idx = pd.date_range("2026-09-01 09:15", periods=6, freq="60min", tz="Asia/Kolkata")
    frame = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0,
                          "Close": 100.5, "Volume": 1000.0}, index=idx)
    df = enrich(frame, market=markets_mod.get("nse"))
    assert df["bar_minutes"].iloc[0] == 60.0
    # Known from the very first bar, because that first bar IS the range.
    assert bool(df["or_high"].notna().iloc[0])
    # And the half-hour close is that same bar, rather than nothing at all.
    assert bool(df["close_0945"].notna().iloc[0])


def test_the_train_locked_split_cuts_every_symbol_at_the_same_date():
    import pandas as pd
    from funnel import cli as funnel_cli

    def frame(sessions):
        return pd.DataFrame({"session": sessions, "Close": range(len(sessions))})

    days = ["2026-01-%02d" % i for i in range(1, 11)]
    enriched = {"A.NS": frame(days), "B.NS": frame(days[2:5])}
    train_bars, train, locked = funnel_cli.split_train_locked(enriched, 0.7)
    assert train == days[:7]
    assert locked == days[7:]
    # B's short history does not move A's cut, and B keeps only its own.
    assert sorted(train_bars["B.NS"]["session"].unique()) == days[2:5]
    assert days[7] not in set(train_bars["A.NS"]["session"])


def test_the_locked_sessions_never_reach_training():
    """The whole point of §1.6: gates 1-6 must not see the held-back bars."""
    import pandas as pd
    from funnel import cli as funnel_cli

    enriched = {"A.NS": pd.DataFrame({"session": ["2026-01-%02d" % i for i in range(1, 11)],
                                      "Close": range(10)})}
    train_bars, train, locked = funnel_cli.split_train_locked(enriched, 0.7)
    seen = set(train_bars["A.NS"]["session"])
    assert not seen & set(locked)
    assert len(train) + len(locked) == 10


def test_a_rule_that_never_fired_is_not_reported_as_one_that_lost():
    """"net +0.00 over 0 trades" reads as a strategy that broke even.
    It never fired, which on a new bar size is the finding itself."""
    from funnel import gates

    silent = gates.gate_training_profit({"ok": True, "net": 0.0, "trades": 0})
    assert silent["pass"] is False
    assert silent["no_trades"] is True
    assert "never signalled" in silent["why"]
    assert "+0.00" not in silent["why"]

    # A rule the engine refused on every signal is the opposite finding,
    # and reading it as "never signalled" is how vwap_bands came back as
    # silent while the variant wrapping it traded thirty times.
    refused = gates.gate_training_profit(
        {"ok": True, "net": 0.0, "trades": 0, "skipped": {"costs_exceed_target": 412}})
    assert "412 signals" in refused["why"]
    assert "never signalled" not in refused["why"]
    assert refused["skipped"]["costs_exceed_target"] == 412

    lost = gates.gate_training_profit({"ok": True, "net": -12.5, "trades": 3})
    assert lost["pass"] is False
    assert not lost.get("no_trades")
    assert "-12.50 after costs over 3 trades" in lost["why"]


def test_a_backtest_reports_why_signals_did_not_become_trades():
    from arena import markets as markets_mod
    from arena.strategies import enrich, STRATEGIES
    from funnel import backtest
    from validation import arena_guard

    market = markets_mod.get("nse")
    bars = {"A.NS": enrich(arena_guard.intraday_bars(sessions=10, seed=11), market=market)}
    result = backtest.run(STRATEGIES["vwap_bands"], ["A.NS"], bars, days=10)
    assert "skipped" in result
    assert isinstance(result["skipped"], dict)
    # Only non-zero reasons, so the block is a finding rather than a form.
    assert all(v for v in result["skipped"].values())
