"""Phase 8: keeping it working, and the guards that stop it drifting.

The two that matter most: an unexplained gap between paper and backtest
is a bug rather than a curiosity, and a strategy is never retired on one
bad week.
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from adapt import killlines as kl, reconcile as rc, review as rv, rotate as rot, status as st


def trade(ticker="A", qty=10, price=100.0, at="09:20", side="BUY",
          date="2026-11-25", strategy="s"):
    return {"date": date, "ticker": ticker, "strategy": strategy, "side": side,
            "qty": qty, "price": price, "at": at}


# ------------------------------------------------- 1. reconciliation

def test_identical_books_have_no_gaps():
    out = rc.compare([trade()], [trade()])
    assert out["clean"] and out["gaps"] == [] and out["matched"] == 1


def test_a_price_difference_is_a_fill_gap():
    out = rc.compare([trade(price=100.0)], [trade(price=103.0)])
    assert out["counts"][rc.FILLS] == 1 and out["clean"]


def test_a_tiny_price_difference_is_not_a_gap_at_all():
    out = rc.compare([trade(price=100.0)], [trade(price=100.1)])
    assert out["gaps"] == []


def test_a_different_bar_is_a_timing_gap():
    out = rc.compare([trade(at="09:20")], [trade(at="09:25")])
    assert out["counts"][rc.TIMING] == 1 and out["clean"]


def test_one_share_apart_is_rounding():
    out = rc.compare([trade(qty=10)], [trade(qty=11)])
    assert out["counts"][rc.ROUNDING] == 1 and out["clean"]


def test_a_big_size_difference_is_a_bug_unless_a_limit_explains_it():
    out = rc.compare([trade(qty=2)], [trade(qty=10)])
    assert out["counts"][rc.RULES] == 1 and not out["clean"]
    explained = rc.compare([trade(qty=2)], [trade(qty=10)],
                           limited_keys={rc.key_of(trade())})
    assert explained["counts"][rc.LIMITS] == 1 and explained["clean"]


def test_a_missing_bar_explains_a_one_sided_trade():
    out = rc.compare([trade()], [], missing_bars={("2026-11-25", "A")})
    assert out["counts"][rc.DATA] == 1 and out["clean"]


def test_an_unexplained_one_sided_trade_is_a_bug():
    """The residual bucket is the whole point of classifying.

    A classifier with a "miscellaneous" bin that quietly absorbs the
    unexplained would let a rule bug live in it forever.
    """
    out = rc.compare([trade()], [])
    assert out["counts"][rc.RULES] == 1
    assert not out["clean"] and out["bugs"]


def test_the_summary_names_the_bugs_loudly():
    text = rc.summary(rc.compare([trade()], []))
    assert "nothing benign explains" in text


# ----------------------------------------------------- 2. kill lines

def test_a_kill_line_is_a_multiple_of_the_tested_drawdown():
    assert kl.kill_line(-10.0, 2.0) == -20.0
    assert kl.kill_line(10.0, 2.0) == -20.0        # sign of the input is ignored


def test_crossing_is_at_or_past_the_line():
    assert kl.crossed(-20.0, -20.0)
    assert kl.crossed(-20.1, -20.0)
    assert not kl.crossed(-19.9, -20.0)


def test_half_way_is_half_the_line():
    assert kl.half_way(-10.0, -20.0)
    assert not kl.half_way(-9.9, -20.0)


def test_a_strategy_with_no_tested_drawdown_has_no_line():
    out = kl.evaluate([{"id": "x", "worst_tested_drawdown_pct": 0,
                        "live_drawdown_pct": -50}], 2.0)[0]
    assert not out["measurable"] and not out["crossed"]


def test_the_core_uses_a_tighter_multiple_and_only_blocks_sleeves():
    out = kl.evaluate_core(-45.0, -28.52)
    assert out["kill_line_pct"] == pytest.approx(-42.78)
    assert out["crossed"] and out["stop_new_sleeves"]
    assert "human decision" in out["action"]


def test_the_core_below_its_line_does_nothing():
    assert not kl.evaluate_core(-12.0, -28.52)["crossed"]


def test_the_changelog_is_append_only(tmp_path):
    path = tmp_path / "CHANGELOG_rules.md"
    entry = {"id": "a", "live_drawdown_pct": -25.0, "kill_line_pct": -20.0,
             "worst_tested_drawdown_pct": -10.0, "multiple": 2.0}
    kl.write_changelog([entry], path)
    first = path.read_text()
    kl.write_changelog([dict(entry, id="b")], path)
    second = path.read_text()
    assert second.startswith(first)          # nothing was rewritten
    assert "a" in second and "b" in second


def test_the_changelog_says_why(tmp_path):
    path = tmp_path / "log.md"
    kl.write_changelog([{"id": "a", "live_drawdown_pct": -25.0,
                         "kill_line_pct": -20.0,
                         "worst_tested_drawdown_pct": -10.0,
                         "multiple": 2.0}], path)
    text = path.read_text()
    assert "switched off" in text and "-25.00%" in text and "-20.00%" in text


# -------------------------------------------------------- 3. status

def row(**kw):
    base = {"id": "x", "ticker": "X.NS", "worst_tested_drawdown_pct": -10,
            "live_drawdown_pct": -2, "live_trades": 40, "live_days": 60,
            "tested_trades": 50, "tested_days": 60}
    base.update(kw)
    return base


def status_of(**kw):
    r = row(**kw)
    kill = kl.evaluate([r], 2.0)[0]
    return st.classify(r, kill)


def test_a_normal_strategy_is_kept():
    assert status_of()["status"] == st.KEEP


def test_too_few_live_trades_is_a_watch():
    assert status_of(live_trades=4)["status"] == st.WATCH


def test_past_half_the_kill_line_is_a_watch():
    assert status_of(live_drawdown_pct=-12)["status"] == st.WATCH


def test_trading_far_too_rarely_is_a_review_after_three_months():
    out = status_of(live_trades=40, live_days=80, tested_trades=200)
    assert out["status"] == st.REVIEW


def test_frequency_is_not_judged_before_three_months():
    out = status_of(live_trades=40, live_days=40, tested_trades=200)
    assert out["status"] != st.REVIEW


def test_crossing_the_kill_line_with_evidence_is_out():
    out = status_of(live_drawdown_pct=-25, live_trades=40, live_days=60)
    assert out["status"] == st.OUT and out["switched_off"]


def test_a_strategy_is_never_retired_on_one_bad_week():
    """Rule 6, and the reason `out` and `switched_off` are separate.

    Crossing the line stops it trading immediately, because that is
    safety. Writing it out is a judgement, and a judgement needs more
    than four days of evidence.
    """
    out = status_of(live_drawdown_pct=-40, live_trades=4, live_days=5)
    assert out["status"] == st.WATCH
    assert out["switched_off"] is True
    assert "not enough to retire it on" in out["why"]


def test_the_retirement_thresholds_are_what_stops_it():
    assert st.MIN_TRADES_TO_RETIRE > st.MIN_LIVE_TRADES
    assert st.MIN_DAYS_TO_RETIRE >= 20


def test_frequency_ratio_is_per_day_not_per_trade():
    assert st.frequency_ratio(50, 50, 50, 50) == 1.0
    assert st.frequency_ratio(25, 50, 50, 50) == 0.5
    assert st.frequency_ratio(0, 0, 10, 10) is None


# -------------------------------------------------------- 4. rotate

def series(mu, n=300, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-01", periods=n)
    return pd.Series(rng.normal(mu, 0.004, n), index=idx)


def core_curve(n=300, seed=2):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-01", periods=n)
    return pd.Series(100000 * np.cumprod(1 + rng.normal(0.0004, 0.008, n)), index=idx)


def cand(cid, mu, ticker=None, seed=3):
    return {"id": cid, "ticker": ticker or (cid.upper() + ".NS"),
            "returns": series(mu, seed=seed)}


def test_a_candidate_on_a_ticker_already_held_is_refused():
    keeping = [cand("keep", 0.0003, "HELD.NS", seed=7)]
    out = rot.propose("dead", [cand("new", 0.001, "HELD.NS", seed=8)],
                      keeping, core_curve(), 100000.0)
    assert not out["rotate"]
    assert "already in the book" in out["rejected"][0]["why"]


def test_a_candidate_too_correlated_to_the_core_is_refused():
    curve = core_curve()
    twin = {"id": "twin", "ticker": "T.NS", "returns": curve.pct_change().fillna(0.0)}
    out = rot.propose("dead", [twin], [], curve, 100000.0)
    assert not out["rotate"]
    assert "correlation" in out["rejected"][0]["why"]


def test_a_candidate_that_makes_the_account_worse_is_refused():
    out = rot.propose("dead", [cand("poor", -0.0012, seed=9)], [], core_curve(),
                      100000.0)
    assert not out["rotate"]
    assert "no candidate leaves the account no worse" in out["why"]


def test_an_empty_slot_is_left_empty_rather_than_filled_for_its_own_sake():
    out = rot.propose("dead", [cand("poor", -0.002, seed=11)], [], core_curve(),
                      100000.0)
    assert out["chosen"] is None
    assert "for its own sake" in out["why"]


def test_a_good_candidate_is_taken():
    out = rot.propose("dead", [cand("good", 0.0009, seed=12)], [], core_curve(),
                      100000.0)
    assert out["rotate"] and out["chosen"]["id"] == "good"


def test_every_candidate_considered_is_named_even_when_one_wins():
    out = rot.propose("dead", [cand("good", 0.0009, seed=12),
                               cand("poor", -0.0012, seed=9)],
                      [], core_curve(), 100000.0)
    assert out["rotate"]
    assert any(r["id"] == "poor" for r in out["rejected"])


def test_a_refusal_is_logged_as_well_as_a_rotation(tmp_path):
    path = tmp_path / "log.md"
    path.write_text("# log\n")
    out = rot.propose("dead", [cand("poor", -0.002, seed=11)], [], core_curve(),
                      100000.0)
    rot.log_rotation(out, dry_run=True, path=path)
    text = path.read_text()
    assert "no rotation for dead" in text and "[dry run]" in text


# -------------------------------------------------------- 5. review

def test_the_weekly_report_lands_on_saturday():
    assert rv.WEEKLY in rv.due(dt.date(2026, 11, 28))
    assert rv.WEEKLY not in rv.due(dt.date(2026, 11, 25))


def test_the_monthly_report_lands_at_month_end():
    assert rv.MONTHLY in rv.due(dt.date(2026, 11, 30))
    assert rv.MONTHLY not in rv.due(dt.date(2026, 11, 29))


def test_the_quarterly_proposal_lands_only_at_a_quarter_end():
    assert rv.QUARTERLY in rv.due(dt.date(2026, 12, 31))
    assert rv.QUARTERLY not in rv.due(dt.date(2026, 11, 30))


def test_a_review_may_fix_a_crash():
    assert not rv.touches_rules({"target": "harness/runner.py",
                                 "note": "fix a crash"})


@pytest.mark.parametrize("target", ["rules.yaml", "arena/strategies.py",
                                    "funnel/gates.py threshold",
                                    "bench.yaml", "kill_line"])
def test_a_review_may_never_touch_a_rule(target):
    """A review that can edit a rule will, one day, edit the losing one."""
    assert rv.touches_rules({"target": target})


def test_an_explicit_rule_change_is_caught_whatever_the_file():
    assert rv.touches_rules({"target": "somewhere.py", "changes_rule": True})


def test_the_quarterly_proposal_changes_nothing_until_a_human_says_yes():
    out = rv.quarterly_proposal([{"from": "A", "to": "B"}], dt.date(2026, 12, 31))
    assert not out["applied"] and out["waiting_for"] == rv.APPROVE_FLAG


def test_only_the_first_three_lines_go_to_telegram():
    head = rv.telegram_head(["one", "two", "three", "four", "five"])
    assert head.splitlines() == ["one", "two", "three"]


def test_the_weekly_report_leads_with_the_bugs():
    report = rv.weekly_report(rc.compare([trade()], []), [], {}, dt.date(2026, 11, 28))
    assert "nothing benign explains" in rv.telegram_head(report)
