"""The fill audit, and the counters the dashboard reads.

The audit's job is to catch a fill that did not happen at the next bar's
open. A test that only shows it passing on honest bars proves nothing, so
the control here breaks a fill on purpose and checks the audit says so.
"""
import pytest

from arena import engine
from arena import markets as markets_mod
from arena.strategies import enrich
from validation import arena_guard, fills

SEED = 20260325
CFG = {"market": "nse", "symbols": ["TEST.NS"]}


@pytest.fixture(scope="module")
def enriched():
    bars = arena_guard.intraday_bars(sessions=8, seed=SEED)
    return {"TEST.NS": enrich(bars, market=markets_mod.get("nse"))}


def test_every_entry_lands_on_the_next_bars_open(enriched):
    report = fills.audit(CFG, enriched, days=8)
    assert report["checked"] > 0
    assert report["mismatches"] == []
    assert report["ok"]


def test_the_audit_catches_a_fill_that_did_not_happen_at_the_open(enriched, monkeypatch):
    """The control. Without it the test above is a tautology."""
    real = engine.replay

    def cheating_replay(*args, **kwargs):
        state = real(*args, **kwargs)
        for book in state["books"].values():
            for trade in book["trades"]:
                trade["entry_price"] = round(float(trade["entry_price"]) * 0.97, 6)
        return state

    monkeypatch.setattr(engine, "replay", cheating_replay)
    report = fills.audit(CFG, enriched, days=8)
    assert not report["ok"]
    assert report["mismatches"]
    assert "next open is" in report["mismatches"][0]["why"]


def test_gapped_bars_make_the_two_rules_disagree(enriched):
    """On bars that open where the last one closed the rule is invisible."""
    bars = arena_guard.intraday_bars(sessions=8, seed=SEED)
    flat = fills.audit(CFG, enriched, days=8)
    gapped = fills.audit(
        CFG, {"TEST.NS": enrich(fills.with_gaps(bars, seed=SEED),
                                market=markets_mod.get("nse"))}, days=8)
    assert flat["moved"] == 0
    assert gapped["moved"] == gapped["checked"] > 0
    assert gapped["ok"]


def test_with_gaps_keeps_every_bar_valid():
    bars = arena_guard.intraday_bars(sessions=4, seed=SEED)
    out = fills.with_gaps(bars, seed=SEED)
    assert (out["High"] >= out[["Open", "Close"]].max(axis=1) - 1e-9).all()
    assert (out["Low"] <= out[["Open", "Close"]].min(axis=1) + 1e-9).all()
    # The close is untouched, so this is a gap and not different bars.
    assert out["Close"].equals(bars["Close"])


def test_a_last_bar_signal_is_counted_and_not_taken():
    scenario = fills.last_bar_scenario()
    assert scenario == {"positions": 0, "no_next_bar": 1}


# ---------------------------------------------------------------------------
# engine.fill_rule - what the dashboard shows
# ---------------------------------------------------------------------------

def test_fill_rule_counts_today_and_the_stored_days():
    state = engine.new_state({"market": "nse", "symbols": ["T.NS"],
                              "strategies": ["orb", "benchmark"]})
    engine.start_day(state, "2026-09-02")          # books are made per session
    book = state["books"]["orb"]
    book["stats"]["trades"] = 4
    book["day"]["skipped"]["gapped_through"] = 1
    book["day_history"].append({"date": "2026-09-01",
                                "skipped": {"no_next_bar": 2, "gapped_through": 3}})
    engine._park(state, "orb", "T.NS", {"kind": "entry", "signal": {}})

    report = engine.fill_rule(state)
    assert report["filled"] == 4
    assert report["parked_now"] == 1
    assert report["skipped"]["gapped_through"] == 4
    assert report["skipped"]["no_next_bar"] == 2
    assert report["slippage_multiples"] == engine.SLIPPAGE_MULTIPLES


def test_fill_rule_ignores_a_book_the_user_unticked():
    state = engine.new_state({"market": "nse", "symbols": ["T.NS"],
                              "strategies": ["orb", "benchmark"]})
    engine.start_day(state, "2026-09-02")
    state["books"]["ema_cross"] = engine._new_book("ema_cross", 10000.0)
    state["books"]["ema_cross"]["stats"]["trades"] = 99
    assert engine.fill_rule(state)["filled"] == 0


def test_a_day_history_row_without_skipped_does_not_break_the_count():
    """Rows stored before Phase 9 have no skipped block at all."""
    state = engine.new_state({"market": "nse", "symbols": ["T.NS"],
                              "strategies": ["orb", "benchmark"]})
    engine.start_day(state, "2026-09-02")
    state["books"]["orb"]["day_history"].append({"date": "2026-08-01", "net": 12.0})
    assert engine.fill_rule(state)["skipped"]["no_next_bar"] == 0


# ---------------------------------------------------------------------------
# The funnel says what it ran on, so data drift cannot pass for a rule change
# ---------------------------------------------------------------------------

def test_the_funnel_reports_the_bars_it_got_and_the_ones_it_did_not():
    from funnel import cli as funnel_cli

    bars = arena_guard.intraday_bars(sessions=3, seed=SEED)
    enriched = {"A.NS": enrich(bars, market=markets_mod.get("nse"))}
    summary = funnel_cli.bars_summary(enriched, ["A.NS", "B.NS"])

    assert summary["missing"] == ["B.NS"]
    assert summary["symbols"][0]["symbol"] == "A.NS"
    assert summary["symbols"][0]["bars"] == len(bars)
    assert summary["first"] <= summary["last"]
    assert "MISSING: B.NS" in funnel_cli.bars_table(summary)


def test_the_funnel_fingerprint_moves_when_the_download_does():
    """Same code, fewer bars, different fingerprint. Phase 4 on Yahoo is not
    a fixed input, so the input belongs in the hash."""
    from funnel import cli as funnel_cli

    base = {"counts": {"ideas_in": 1, "survivors": 0},
            "bar_size": "5m", "sessions": 40, "rows": [],
            "bars": {"symbols": [{"symbol": "A.NS", "bars": 3120}]}}
    for key in funnel_cli.gates.ORDER:
        base["counts"][key] = {"reached": 0, "passed": 0, "failed": 0}

    import copy
    fewer = copy.deepcopy(base)
    fewer["bars"]["symbols"][0]["bars"] = 3110
    assert funnel_cli.fingerprint_values(base) != funnel_cli.fingerprint_values(fewer)
