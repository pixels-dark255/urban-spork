"""rules.yaml must describe the strategies that actually run.

A rule file that drifts from the code is worse than none: it is read as a
specification and believed, while the engine does something else.
"""
import pytest
import yaml

from arena.strategies import STRATEGIES
from signals import rules


DOC = rules.load()


def test_every_running_strategy_has_a_rule():
    drift = rules.check_against_catalog(DOC)
    assert drift["undocumented"] == [], (
        "these strategies run but are not in rules.yaml: %s" % drift["undocumented"])


def test_no_rule_describes_a_strategy_that_does_not_run():
    drift = rules.check_against_catalog(DOC)
    assert drift["orphaned"] == []


def test_rule_names_match_the_running_strategies():
    by_id = rules.by_id(DOC)
    for sid, strategy in STRATEGIES.items():
        assert by_id[sid]["name"] == strategy.name, (
            "rules.yaml calls %s %r, the code calls it %r"
            % (sid, by_id[sid]["name"], strategy.name))


@pytest.mark.parametrize("rule", DOC["strategies"], ids=lambda r: r["id"])
def test_every_rule_has_all_seven_parts(rule):
    for part in rules.PARTS:
        assert rule.get(part) not in (None, ""), "%s has no %s" % (rule["id"], part)


@pytest.mark.parametrize("rule", DOC["strategies"], ids=lambda r: r["id"])
def test_a_rule_that_is_not_testable_says_why(rule):
    if not rule["testable"]:
        assert rule["not_testable_because"].strip()


@pytest.mark.parametrize("rule", DOC["strategies"], ids=lambda r: r["id"])
def test_testable_means_the_stop_is_a_percentage(rule):
    """The schema asks for a stop in percent. That is the whole test."""
    pct = rule["stop"].get("pct")
    assert rule["testable"] == (pct is not None), (
        "%s is marked testable=%s but its stop pct is %r"
        % (rule["id"], rule["testable"], pct))


@pytest.mark.parametrize("rule", DOC["strategies"], ids=lambda r: r["id"])
def test_every_entry_is_next_open(rule):
    # AGENTS.md §1.3. The engine does not do this yet, which is the known
    # gap; the rules must still state the intent, or there is nothing to
    # hold the engine to.
    assert "next session open" in rule["entry"]


@pytest.mark.parametrize("rule", DOC["strategies"], ids=lambda r: r["id"])
def test_every_rule_offers_the_volatile_half_size(rule):
    assert "halved when volatile" in rule["size"]


def test_the_defaults_carry_the_half_size_multiple():
    from signals import regime
    assert DOC["defaults"]["size"]["volatile_multiple"] == regime.VOLATILE_SIZE_MULTIPLE


REGIME_WORDS = ("regime", "trend", "volatil", "calm", "choppy", "vix")


def test_no_rule_filters_on_a_regime_yet():
    """No rule consults the regime, and that is a finding, not a choice.

    When the regime layer is wired into the engine this test should fail
    and be replaced, which is the point: it marks the moment the filters
    stop being decorative.

    It checks for a REGIME filter rather than for the literal string
    "none", because a filter can be a real condition without being a
    market-state one: the patient family filters on whether the move it is
    aiming for covers the round trip, which the engine does execute.
    """
    for rule in DOC["strategies"]:
        text = str(rule["filter"]).lower()
        found = [w for w in REGIME_WORDS if w in text]
        assert not found, "%s filters on %s: %s" % (rule["id"], found, rule["filter"])


def test_a_filter_that_is_not_none_is_one_the_engine_actually_runs():
    """The gap this file records is that rules.yaml can describe things the
    engine ignores. A filter that is written down must not join them."""
    from arena.strategies import STRATEGIES

    for rule in DOC["strategies"]:
        if str(rule["filter"]).lower() == "none":
            continue
        strategy = STRATEGIES[rule["id"]]
        # Only the patient family has a filter so far, and it is enforced
        # in its own entry() rather than being a description of an
        # intention.
        assert hasattr(strategy, "min_move_mult") or "min_move_mult" in strategy.params


def test_no_rule_filters_on_vix():
    # India VIX has not been confirmed to have a free reliable source.
    for rule in DOC["strategies"]:
        assert "vix" not in str(rule["filter"]).lower()


# ------------------------------------------------------- loader guards

def write(tmp_path, doc):
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


def minimal(**over):
    rule = {"id": "x", "name": "X", "testable": True, "market": "nse",
            "setup": "s", "filter": "none", "entry": "next session open",
            "exit": "e", "stop": {"rule": "r", "pct": 1.0, "kind": "percent"},
            "size": "equal slot share, halved when volatile"}
    rule.update(over)
    return {"schema_version": 1, "strategies": [rule]}


def test_a_missing_part_is_refused(tmp_path):
    doc = minimal()
    del doc["strategies"][0]["exit"]
    with pytest.raises(rules.RulesError, match="missing: exit"):
        rules.load(write(tmp_path, doc))


def test_a_duplicate_id_is_refused(tmp_path):
    doc = minimal()
    doc["strategies"].append(dict(doc["strategies"][0]))
    with pytest.raises(rules.RulesError, match="twice"):
        rules.load(write(tmp_path, doc))


def test_not_testable_without_a_reason_is_refused(tmp_path):
    with pytest.raises(rules.RulesError, match="gives no reason"):
        rules.load(write(tmp_path, minimal(testable=False)))


def test_the_table_prints_all_seven_parts_for_every_strategy():
    text = rules.table(DOC)
    for rule in DOC["strategies"]:
        assert rule["id"] in text
    for part in rules.PARTS:
        assert part + ":" in text
    assert "NOT TESTABLE YET" in text


def test_the_fingerprint_moves_when_a_stop_changes():
    import copy
    import fingerprint as fp
    other = copy.deepcopy(DOC)
    other["strategies"][0]["stop"]["pct"] = 99.0
    assert (fp.fingerprint(rules.fingerprint_values(DOC))
            != fp.fingerprint(rules.fingerprint_values(other)))
