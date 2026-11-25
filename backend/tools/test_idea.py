"""Run any idea through the same seven gates.

    python -m tools.test_idea ideas/my_idea.yaml

The point is that a new idea gets no easier a ride than the ones already
running. Mine, Claude's, or one off YouTube: same gates, same order, same
thresholds, same locked-window rule.

An idea file declares metadata and parameters, not rule logic:

    id: rsi_patient
    name: RSI oversold bounce, patient exit
    kind: reversion            # breakout | momentum | reversion
    bar_size: 5m
    base: rsi_reversion        # a strategy in arena/strategies.py
    params:                    # overrides on that strategy's parameters
      rsi_floor: 25
      rsi_exit: 60
    neighbours:                # optional; defaults to the base's own
      rsi_floor: [20, 25, 30]

Rule logic stays in `arena/strategies.py` because AGENTS.md §1.2 allows
exactly one signal module. An idea that needs genuinely new logic adds it
there first and then points at it from here. A YAML rule DSL would be a
second place rules live, and the two would drift.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

import fingerprint as fp
from arena.strategies import STRATEGIES

from funnel import cli as funnel_cli, gates, run as run_mod

REQUIRED = ("id", "name", "kind", "base")


class IdeaError(ValueError):
    pass


def load_idea(path: pathlib.Path) -> run_mod.Idea:
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict):
        raise IdeaError("%s is not a mapping" % path)
    missing = [k for k in REQUIRED if not doc.get(k)]
    if missing:
        raise IdeaError("%s is missing: %s" % (path.name, ", ".join(missing)))

    base = STRATEGIES.get(doc["base"])
    if base is None:
        raise IdeaError("base %r is not a strategy in arena/strategies.py. "
                        "New rule logic goes there first (AGENTS.md §1.2); "
                        "known ids: %s"
                        % (doc["base"], ", ".join(sorted(STRATEGIES))))

    params = doc.get("params") or {}
    unknown = sorted(set(params) - set(base.PARAMS))
    if unknown:
        raise IdeaError("%s has no parameter(s): %s. It takes: %s"
                        % (doc["base"], ", ".join(unknown),
                           ", ".join(sorted(base.PARAMS)) or "none"))

    kind = doc["kind"]
    if kind not in gates.KIND_WANTS_LABEL:
        raise IdeaError("kind %r must be one of: %s"
                        % (kind, ", ".join(sorted(gates.KIND_WANTS_LABEL))))

    strategy = base.variant(**params) if params else base
    neighbours = doc.get("neighbours") or dict(getattr(base, "NEIGHBOURS", {}))
    bad = sorted(set(neighbours) - set(base.PARAMS))
    if bad:
        raise IdeaError("neighbours name unknown parameter(s): %s" % ", ".join(bad))

    return run_mod.Idea(id=doc["id"], name=doc["name"], kind=kind,
                        bar_size=doc.get("bar_size", "5m"),
                        strategy=strategy, neighbours=neighbours)


def report_lines(idea: run_mod.Idea, row: dict) -> list[str]:
    lines = ["Idea: %s (%s)" % (idea.name, idea.id),
             "  kind %s, %s bars, based on %s with %s"
             % (idea.kind, idea.bar_size, type(idea.strategy).__name__,
                idea.strategy.params or "default parameters"), ""]
    for key in gates.ORDER:
        verdict = row["gates"].get(key)
        if verdict is None:
            lines.append("  %-30s %-8s %s" % (gates.TITLES[key], "-", "not reached"))
            continue
        mark = "PASS" if verdict["pass"] else ("SKIP" if verdict.get("skipped") else "FAIL")
        lines.append("  %-30s %-8s %s" % (gates.TITLES[key], mark, verdict["why"]))
    lines.append("")
    if row["survivor"]:
        t = row["training"]
        lines.append("  SURVIVED: %d trades, net %+.2f, profit factor %s, max drawdown %.2f"
                     % (t["trades"], t["net"],
                        "n/a" if t["profit_factor"] is None else "%.2f" % t["profit_factor"],
                        t["max_drawdown"]))
    else:
        lines.append("  Stopped at %s." % gates.TITLES.get(row["failed_at"], row["failed_at"]))
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run one idea through the funnel.")
    ap.add_argument("idea", type=pathlib.Path)
    ap.add_argument("--source", choices=["synthetic", "yahoo"], default="synthetic")
    ap.add_argument("--sessions", type=int, default=12)
    args = ap.parse_args(argv)

    try:
        idea = load_idea(args.idea)
    except (IdeaError, yaml.YAMLError) as exc:
        print("[error] %s" % exc, file=sys.stderr)
        return 2

    labels = gates.load_labels()
    enriched = funnel_cli.load_bars(args.source, args.sessions, sorted(labels["labels"]))
    if not enriched:
        print("[error] no bars", file=sys.stderr)
        return 3

    # Gate 7 is never run here. It may be run once per idea, and spending
    # it from a command meant for quick iteration is exactly how a locked
    # window stops meaning anything.
    row = run_mod.run_idea(idea, enriched, labels, days=args.sessions, locked=None)
    print("\n".join(report_lines(idea, row)))

    values = {"id": idea.id, "kind": idea.kind,
              "failed_at": row["failed_at"] or "survived",
              "net": round((row["training"] or {}).get("net", 0.0), 2),
              "trades": (row["training"] or {}).get("trades", 0)}
    print()
    print(fp.line("4-idea", values))
    return 0 if row["survivor"] else 1


if __name__ == "__main__":
    sys.exit(main())
