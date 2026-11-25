"""`python -m tools.test_idea` must refuse a malformed idea clearly.

A new idea gets no easier a ride than the ones already running, and that
starts with not silently accepting a file that means something other than
what its author thought.
"""
import pathlib

import pytest
import yaml

from tools import test_idea


def write(tmp_path, **doc):
    base = {"id": "x", "name": "X", "kind": "reversion", "base": "rsi_reversion"}
    base.update(doc)
    base = {k: v for k, v in base.items() if v is not None}
    path = tmp_path / "idea.yaml"
    path.write_text(yaml.safe_dump(base))
    return path


def test_a_valid_idea_loads_with_its_overrides(tmp_path):
    idea = test_idea.load_idea(write(tmp_path, params={"rsi_floor": 25.0}))
    assert idea.strategy.p("rsi_floor") == 25.0
    assert idea.strategy.p("rsi_exit") == 55.0        # untouched default


def test_a_missing_field_is_named(tmp_path):
    with pytest.raises(test_idea.IdeaError, match="kind"):
        test_idea.load_idea(write(tmp_path, kind=None))


def test_an_unknown_base_lists_what_is_available(tmp_path):
    with pytest.raises(test_idea.IdeaError, match="rsi_reversion"):
        test_idea.load_idea(write(tmp_path, base="not_a_strategy"))


def test_a_new_rule_is_directed_to_the_one_signal_module(tmp_path):
    with pytest.raises(test_idea.IdeaError, match=r"AGENTS.md §1.2"):
        test_idea.load_idea(write(tmp_path, base="my_brand_new_rule"))


def test_a_typo_in_a_parameter_name_is_refused(tmp_path):
    # Silently ignoring it would run the base rule unchanged and report a
    # result for an idea nobody tested.
    with pytest.raises(test_idea.IdeaError, match="no parameter"):
        test_idea.load_idea(write(tmp_path, params={"rsi_flor": 25.0}))


def test_an_unknown_kind_is_refused(tmp_path):
    with pytest.raises(test_idea.IdeaError, match="kind"):
        test_idea.load_idea(write(tmp_path, kind="vibes"))


def test_neighbours_must_name_real_parameters(tmp_path):
    with pytest.raises(test_idea.IdeaError, match="unknown parameter"):
        test_idea.load_idea(write(tmp_path, neighbours={"nope": [1, 2, 3]}))


def test_neighbours_default_to_the_base_rules_own(tmp_path):
    idea = test_idea.load_idea(write(tmp_path))
    from arena.strategies import STRATEGIES
    assert idea.neighbours == STRATEGIES["rsi_reversion"].NEIGHBOURS


def test_the_shipped_example_is_valid():
    path = pathlib.Path(__file__).resolve().parents[2] / "ideas" / "example_rsi_patient.yaml"
    idea = test_idea.load_idea(path)
    assert idea.kind == "reversion"
    assert idea.strategy.p("rsi_floor") == 25.0


def test_the_locked_gate_is_never_spent_from_this_command():
    """It may be run once per idea (AGENTS.md §1.6).

    Spending it from a command built for quick iteration is exactly how a
    locked window stops meaning anything, so run_idea is called with
    locked=None and the gate reports a skip.
    """
    source = pathlib.Path(test_idea.__file__).read_text()
    assert "locked=None" in source


def test_the_funnel_cli_refuses_a_locked_run_on_synthetic_bars():
    from funnel import cli
    assert cli.main(["--locked", "--source", "synthetic"]) == 2
