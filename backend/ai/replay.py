"""The replay, run twice, and the gate that decides whether to turn this on.

Twice, because the first run proves almost nothing. A model shown an RBI
statement from a date it recognises may be recalling what the market did
next rather than reading the document. So the same replay runs again with
the dates and names removed, and only the masked run counts.

Even the masked run is a replay and not a clean test, and it is labelled
that way everywhere it appears. The documents are historical, the model
was trained on a world that contains them, and masking removes the
obvious handles and not the subtle ones. Treating it as evidence of a
live edge would be exactly the mistake the rest of this system exists to
avoid.

The enable gate has three conditions and all three must hold:

  1. the hard rules in AGENTS.md §1 have no open gaps;
  2. the core itself passed its own gates, because a layer that modulates
     a failing core inherits the failure;
  3. the MASKED replay beat core alone on return over drawdown.

Condition 3 is the one the brief names. Conditions 1 and 2 are what
"only after Phases 0-6 pass" means in practice.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from swing import core

from . import calls as calls_mod, judge

LABEL = "replay, not a clean test"


def apply_calls_to_holding(index, base_hold, decisions: list[dict],
                           engine: str, safe: str):
    """Overlay the calls on the core's own signal.

    A call wins for the days it covers and the core's rule resumes
    afterwards. Overlapping calls do not stack: the later one replaces
    the earlier, because two overlapping instructions with no stated
    precedence is a rule nobody can write down.
    """
    overlay = pd.Series([None] * len(index), index=index, dtype="object")
    for decision in sorted(decisions, key=lambda d: d["effective_from"]):
        if decision["days"] <= 0:
            continue
        start = pd.Timestamp(decision["effective_from"])
        later = index[index >= start][: decision["days"]]
        for day in later:
            overlay.loc[day] = decision["holding"]
    return overlay


def run_once(bars: dict, engine: str, safe: str, capital: float,
             documents: list[dict], caller: judge.Caller, masked: bool,
             log: calls_mod.CallLog, start, end) -> dict:
    """One pass of the replay: make the calls, then price them."""
    decisions, skipped = [], []
    for i, document in enumerate(documents):
        previous = documents[i - 1] if i else None
        as_of = dt.date.fromisoformat(document["date"])
        out = judge.make_call(caller, document, previous, engine, as_of,
                              masked, log)
        if out.get("skipped"):
            skipped.append(out)
            continue
        call = out["call"]
        if call.call == "hold":
            decisions.append({"effective_from": document["date"], "days": 0,
                              "holding": engine, "call": call.call,
                              "reason": call.reason, "id": call.id})
            continue
        holding = engine if call.call == "engine_for_10d" else safe
        decisions.append({"effective_from": document["date"],
                          "days": call.hold_days(), "holding": holding,
                          "call": call.call, "reason": call.reason,
                          "id": call.id})

    base = core.run(bars, engine, safe, capital, core.BASE_RULE,
                    start=start, end=end)
    if not base.get("ok"):
        return {"ok": False, "reason": base.get("reason", "core did not run")}

    index = base["equity"].index
    overlay = apply_calls_to_holding(index, None, decisions, engine, safe)
    withai = _run_with_overlay(bars, engine, safe, capital, overlay, start, end)
    return {"ok": True, "masked": masked, "caller": caller.name,
            "decisions": decisions, "skipped": skipped,
            "core": {k: base[k] for k in ("return_pct", "cagr_pct",
                                          "max_drawdown_pct",
                                          "return_over_drawdown")},
            "with_ai": withai, "label": LABEL}


def _run_with_overlay(bars, engine, safe, capital, overlay, start, end) -> dict:
    """The core rule, with the overlay taking precedence where it is set."""
    def rule(close: pd.Series) -> pd.Series:
        signal = core.BASE_RULE(close)
        aligned = overlay.reindex(signal.index)
        forced_engine = aligned == engine
        forced_safe = aligned.notna() & (aligned != engine)
        out = signal.copy()
        out[forced_engine] = True
        out[forced_safe] = False
        return out
    rule.label = "SMA200 + AI overlay"
    result = core.run(bars, engine, safe, capital, rule, start=start, end=end)
    if not result.get("ok"):
        return {"ok": False}
    return {k: result[k] for k in ("return_pct", "cagr_pct",
                                   "max_drawdown_pct", "return_over_drawdown",
                                   "switches")}


def beats_core(result: dict) -> bool:
    """Did the overlay improve return over drawdown?"""
    if not result.get("ok") or not result["with_ai"].get("return_over_drawdown"):
        return False
    base = result["core"]["return_over_drawdown"]
    with_ai = result["with_ai"]["return_over_drawdown"]
    if base is None or with_ai is None:
        return False
    return with_ai > base


def preconditions() -> dict:
    """"Only after Phases 0-6 pass", checked rather than assumed."""
    import system_map
    import yaml
    import pathlib

    gaps = [r["rule"] for r in system_map.RULES if r["status"] == system_map.GAP]

    bench = pathlib.Path(__file__).resolve().parent.parent.parent / "bench.yaml"
    survivors = 0
    if bench.exists():
        survivors = len((yaml.safe_load(bench.read_text()) or {}).get("survivors") or [])

    # The core is the only thing this layer may touch, so a core that
    # failed its own gates makes the layer moot however well it replays.
    core_passed = False
    try:
        import research_store
        swing = research_store.load("5") or {}
        neighbours = swing.get("neighbours") or []
        doubled = swing.get("doubled") or {}
        core_passed = bool(neighbours) and all(
            (n.get("return_pct") or 0) > 0 for n in neighbours) and \
            (doubled.get("return_pct") or 0) > 0
    except Exception:
        core_passed = False

    return {
        "hard_rule_gaps": gaps,
        "hard_rules_clean": not gaps,
        "survivors": survivors,
        "core_passed_its_gates": core_passed,
        "ok": (not gaps) and core_passed,
    }


def may_enable(masked_result: dict, pre: dict | None = None) -> dict:
    """The three conditions. All of them, or the layer stays off."""
    pre = pre or preconditions()
    beat = beats_core(masked_result)
    reasons = []
    if not pre["hard_rules_clean"]:
        reasons.append("hard rules still open: %s" % ", ".join(pre["hard_rule_gaps"]))
    if not pre["core_passed_its_gates"]:
        reasons.append("the core this layer would modulate failed its own "
                       "gates, so improving it proves nothing worth acting on")
    if not beat:
        reasons.append("the masked replay did not beat core alone on return "
                       "over drawdown")
    return {"enable": not reasons, "masked_beat_core": beat,
            "reasons": reasons, "label": LABEL, "preconditions": pre}
