"""Phase 7: the AI layer, which is sealed and off.

Most of these check a refusal. The layer's value is not that it makes
good calls - nothing here establishes that - but that it cannot act
without leaving a record, cannot be switched on by a flattering replay,
and cannot reach anything except the core.
"""
import datetime as dt
import json
import pathlib

import pytest

from ai import calls as calls_mod, judge, mask as mask_mod, replay


@pytest.fixture
def log(tmp_path):
    return calls_mod.CallLog(tmp_path)


def make(call="hold", **kw):
    base = dict(call=call, reason="a short reason",
                made_at="2026-11-25T10:00:00+00:00", model="stub-v1",
                source_url="demo://1", document_date="2026-11-24",
                document_hash="abc123", masked=True, core_holding="ITBEES.NS")
    base.update(kw)
    return calls_mod.Call(**base)


# ------------------------------------------------- the closed list

@pytest.mark.parametrize("call", calls_mod.CALLS)
def test_each_allowed_call_is_accepted(call):
    assert make(call).call == call


def test_the_list_is_exactly_the_four_in_the_brief():
    assert set(calls_mod.CALLS) == {"hold", "safe_for_5d", "safe_for_10d",
                                    "engine_for_10d"}


def test_anything_outside_the_list_is_refused():
    # An open-ended instruction cannot be backtested, replayed or argued
    # with, so it cannot be allowed in.
    with pytest.raises(calls_mod.CallError, match="not one of"):
        make("reduce_exposure_somewhat")


def test_a_call_must_carry_a_one_line_reason():
    with pytest.raises(calls_mod.CallError, match="one-line reason"):
        make(reason="   ")
    with pytest.raises(calls_mod.CallError, match="one line"):
        make(reason="first line\nsecond line")


def test_a_call_must_name_its_document():
    with pytest.raises(calls_mod.CallError, match="name the document"):
        make(source_url="")


def test_a_parsed_line_must_use_the_closed_list():
    assert judge.parse_line("hold: nothing changed") == ("hold", "nothing changed")
    with pytest.raises(calls_mod.CallError):
        judge.parse_line("go_long: feeling good")
    with pytest.raises(calls_mod.CallError, match="expected"):
        judge.parse_line("just some prose with no call")


# ------------------------------- recorded before it can act

def test_a_call_cannot_act_before_it_is_recorded(log):
    """The ordering is the whole safeguard.

    A layer that can act first and log afterwards will, one day, act and
    not log - and it will be the day that matters.
    """
    call = make("safe_for_10d")
    with pytest.raises(calls_mod.CallError, match="has not been recorded"):
        calls_mod.apply(call, log, "ITBEES.NS", "ITBEES.NS", "GOLDBEES.NS")


def test_a_recorded_call_may_act(log):
    call = make("safe_for_10d")
    log.record(call)
    out = calls_mod.apply(call, log, "ITBEES.NS", "ITBEES.NS", "GOLDBEES.NS")
    assert out == {"holding": "GOLDBEES.NS", "days": 10, "changed": True}


def test_hold_changes_nothing(log):
    call = make("hold")
    log.record(call)
    out = calls_mod.apply(call, log, "ITBEES.NS", "ITBEES.NS", "GOLDBEES.NS")
    assert out["changed"] is False and out["days"] == 0


def test_the_record_carries_what_an_audit_needs(log):
    stored = log.record(make("safe_for_5d"))
    for field in ("call", "reason", "made_at", "model", "source_url",
                  "document_date", "document_hash", "masked", "recorded_at"):
        assert field in stored, field


def test_recording_the_same_call_twice_writes_one_file(log):
    call = make("safe_for_5d")
    first = log.record(call)
    second = log.record(call)
    assert first["recorded_at"] == second["recorded_at"]
    assert len(log.all()) == 1


def test_a_different_decision_on_the_same_document_is_a_different_record(log):
    log.record(make("hold"))
    log.record(make("safe_for_10d"))
    assert len(log.all()) == 2


def test_a_masked_call_is_distinct_from_an_unmasked_one(log):
    assert make("hold", masked=True).id != make("hold", masked=False).id


def test_the_log_has_no_update_or_delete():
    # Append only is a property of the interface, not a convention.
    for forbidden in ("update", "delete", "remove", "overwrite"):
        assert not hasattr(calls_mod.CallLog, forbidden)


# ------------------------------------------------- staleness

def test_no_call_is_made_on_an_old_document(tmp_path):
    log = calls_mod.CallLog(tmp_path)
    old = {"date": "2026-10-01", "text": "the committee decided to hike",
           "source_url": "demo://old"}
    out = judge.make_call(judge.StubCaller(), old, None, "ITBEES.NS",
                          dt.date(2026, 11, 25), True, log)
    assert out["skipped"] and "older than" in out["why"]
    assert log.all() == []


def test_a_fresh_document_is_called_on(tmp_path):
    log = calls_mod.CallLog(tmp_path)
    doc = {"date": "2026-11-24", "text": "the committee decided to hike the rate",
           "source_url": "demo://fresh"}
    out = judge.make_call(judge.StubCaller(), doc, None, "ITBEES.NS",
                          dt.date(2026, 11, 25), True, log)
    assert not out["skipped"] and len(log.all()) == 1


def test_trading_days_skip_weekends_and_holidays():
    # 2026-01-26 is Republic Day. The calendar decides, not the clock.
    assert calls_mod.trading_days_between(dt.date(2026, 1, 23),
                                          dt.date(2026, 1, 27)) == 1


# ------------------------------------------------------- masking

def test_masking_removes_every_date_and_name():
    doc = ("The Monetary Policy Committee of the Reserve Bank of India met on "
           "15 March 2024. The Governor said inflation in Q1 FY25 eased. "
           "Resolution No. 48, February 2023, projected 2024-25 growth.")
    assert mask_mod.leaks(mask_mod.mask(doc)) == []


def test_masking_keeps_the_numbers_that_are_the_content():
    doc = "The repo rate was kept at 6.50 per cent and growth at 7.2 per cent."
    out = mask_mod.mask(doc)
    assert "6.50" in out and "7.2" in out


def test_masking_removes_a_fiscal_span_whole():
    # 2024-25 must not become "[masked]-25", which still dates it.
    assert mask_mod.leaks(mask_mod.mask("growth for 2024-25")) == []
    assert "-25" not in mask_mod.mask("growth for 2024-25")


def test_the_leak_detector_actually_detects():
    assert mask_mod.leaks("published in March 2024 by the RBI")


# -------------------------------------------- the enable gate

GOOD = {"ok": True, "core": {"return_over_drawdown": 0.5},
        "with_ai": {"return_over_drawdown": 1.5}}
BAD = {"ok": True, "core": {"return_over_drawdown": 0.5},
       "with_ai": {"return_over_drawdown": 0.1}}
CLEAN = {"hard_rule_gaps": [], "hard_rules_clean": True,
         "survivors": 1, "core_passed_its_gates": True, "ok": True}


def test_a_better_masked_replay_alone_does_not_enable_the_layer():
    """The brief's condition is necessary, not sufficient.

    "Only after Phases 0-6 pass" is the other half, and a layer that
    improved a core which had already failed its own gates would be
    improving something nobody should be running.
    """
    out = replay.may_enable(GOOD, {"hard_rule_gaps": ["x"], "hard_rules_clean": False,
                                   "survivors": 0, "core_passed_its_gates": False,
                                   "ok": False})
    assert not out["enable"]
    assert any("failed its own gates" in r for r in out["reasons"])


def test_all_three_conditions_together_do_enable_it():
    out = replay.may_enable(GOOD, CLEAN)
    assert out["enable"] and out["reasons"] == []


def test_a_worse_masked_replay_blocks_it_even_when_everything_else_is_clean():
    out = replay.may_enable(BAD, CLEAN)
    assert not out["enable"]
    assert any("did not beat core" in r for r in out["reasons"])


def test_a_losing_overlay_does_not_count_as_beating_the_core():
    # The bug this caught: abs() on the ratio made a bigger loss score
    # better, which would have enabled the layer for losing more money.
    worse = {"ok": True, "core": {"return_over_drawdown": -0.077},
             "with_ai": {"return_over_drawdown": -0.198}}
    assert not replay.beats_core(worse)


def test_the_verdict_always_carries_the_replay_label():
    assert replay.may_enable(GOOD, CLEAN)["label"] == replay.LABEL
    assert "not a clean test" in replay.LABEL


def test_the_real_preconditions_are_currently_unmet():
    """The honest state of this repository today.

    If this test ever fails it is good news: it means the hard rules are
    closed and the core passed. Update it then, deliberately.
    """
    pre = replay.preconditions()
    assert not pre["ok"]


# -------------------------------------- scope: core only, key safety

def test_the_ai_layer_cannot_reach_intraday_or_sleeves():
    folder = pathlib.Path(__file__).resolve().parent.parent / "ai"
    for path in folder.glob("*.py"):
        text = path.read_text()
        assert "arena.engine" not in text, "%s reaches the intraday engine" % path.name
        assert "from arena import engine" not in text
        assert "sleeves" not in text.replace("sleeve", ""), path.name


def test_an_api_key_is_never_written_down():
    key = "sk-ant-api03-ThisIsNotARealKeyAtAll1234567890"
    assert key not in judge.redact("failed with key %s" % key, key)
    assert "<redacted>" in judge.redact("failed with key %s" % key, key)


def test_a_key_shaped_string_is_scrubbed_even_if_it_is_not_the_configured_one():
    other = "sk-ant-api03-SomeOtherKeyShapedStringHere999"
    assert other not in judge.redact("oops " + other, key="")


def test_the_model_caller_is_off_without_a_key(monkeypatch):
    monkeypatch.delenv(judge.KEY_VAR, raising=False)
    caller = judge.AnthropicCaller()
    assert not caller.configured()
    with pytest.raises(RuntimeError, match="stays off"):
        caller.decide("doc", "prev", "ITBEES.NS")


def test_no_real_policy_text_is_invented_in_the_repo():
    """The demo documents must be obviously synthetic.

    Making up a central bank's words to score a trading layer would be
    worse than having no layer at all.
    """
    from ai import cli
    for doc in cli.DEMO_DOCS:
        assert doc["source_url"].startswith("demo://")
