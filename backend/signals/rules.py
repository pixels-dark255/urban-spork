"""rules.yaml, loaded and checked against the strategies that actually run.

This wraps arena/strategies.py rather than replacing it. The Python there
is still the one signal module; this states each rule in the seven parts so
it can be argued with, and refuses to let the two drift: a strategy with no
rule entry, or a rule entry naming a strategy that no longer exists, is an
error rather than a quiet omission.
"""
from __future__ import annotations

import pathlib

import yaml

RULES_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / "rules.yaml"

PARTS = ("market", "setup", "filter", "entry", "exit", "stop", "size")


class RulesError(ValueError):
    pass


def load(path: pathlib.Path | None = None) -> dict:
    doc = yaml.safe_load((path or RULES_PATH).read_text())
    if not isinstance(doc, dict) or "strategies" not in doc:
        raise RulesError("rules.yaml has no `strategies` list")
    seen = set()
    for rule in doc["strategies"]:
        missing = [p for p in PARTS if p not in rule or rule[p] in (None, "")]
        if missing:
            raise RulesError("%s is missing: %s"
                             % (rule.get("id", "?"), ", ".join(missing)))
        if rule["id"] in seen:
            raise RulesError("%s appears twice in rules.yaml" % rule["id"])
        seen.add(rule["id"])
        if not rule.get("testable") and not rule.get("not_testable_because"):
            raise RulesError("%s is not testable but gives no reason" % rule["id"])
    return doc


def by_id(doc: dict | None = None) -> dict:
    doc = doc or load()
    return {r["id"]: r for r in doc["strategies"]}


def check_against_catalog(doc: dict | None = None) -> dict:
    """Every running strategy must have a rule, and vice versa."""
    from arena.strategies import STRATEGIES

    rules = by_id(doc)
    running = set(STRATEGIES)
    return {
        "undocumented": sorted(running - set(rules)),
        "orphaned": sorted(set(rules) - running),
    }


def testable(doc: dict | None = None) -> list[str]:
    return sorted(r["id"] for r in (doc or load())["strategies"] if r["testable"])


def not_testable(doc: dict | None = None) -> list[dict]:
    return [{"id": r["id"], "why": r["not_testable_because"]}
            for r in (doc or load())["strategies"] if not r["testable"]]


def _short(value, width: int) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= width else text[:width - 1] + "…"


def table(doc: dict | None = None, width: int = 46) -> str:
    """The CHECK block: the seven parts for every Arena strategy."""
    doc = doc or load()
    lines = []
    for rule in doc["strategies"]:
        mark = "" if rule["testable"] else "   [NOT TESTABLE YET]"
        lines.append("%s  (%s)%s" % (rule["name"], rule["id"], mark))
        if not rule["testable"]:
            lines.append("    %-8s %s" % ("why:", rule["not_testable_because"]))
        for part in PARTS:
            value = rule[part]
            if part == "stop" and isinstance(value, dict):
                pct = value.get("pct")
                value = "%s  [%s]" % (value["rule"],
                                      "%.1f%%" % pct if pct is not None else value["kind"])
            lines.append("    %-8s %s" % (part + ":", _short(value, 400)))
        lines.append("")
    counts = "%d strategies: %d testable, %d not testable yet" % (
        len(doc["strategies"]), len(testable(doc)), len(not_testable(doc)))
    lines.append(counts)
    return "\n".join(lines)


def fingerprint_values(doc: dict | None = None) -> dict:
    doc = doc or load()
    values = {"strategies": len(doc["strategies"])}
    for rule in doc["strategies"]:
        stop = rule["stop"]
        values[rule["id"] + ":testable"] = bool(rule["testable"])
        values[rule["id"] + ":stop_kind"] = stop.get("kind") if isinstance(stop, dict) else "?"
        values[rule["id"] + ":stop_pct"] = stop.get("pct") if isinstance(stop, dict) else None
        values[rule["id"] + ":filter"] = str(rule["filter"])
    return values


if __name__ == "__main__":
    import fingerprint as fp

    doc = load()
    print(table(doc))
    drift = check_against_catalog(doc)
    if drift["undocumented"] or drift["orphaned"]:
        print()
        if drift["undocumented"]:
            print("RUNNING BUT UNDOCUMENTED: %s" % ", ".join(drift["undocumented"]))
        if drift["orphaned"]:
            print("DOCUMENTED BUT NOT RUNNING: %s" % ", ".join(drift["orphaned"]))
    else:
        print("Every running strategy has a rule, and every rule a strategy.")
    print()
    print(fp.line(2, fingerprint_values(doc)))
