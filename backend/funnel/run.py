"""Drive every idea through the seven gates and report what happened.

The output is deliberately shaped around failure. Which gate each idea
died at is the finding; the survivor list is usually short and sometimes
empty, and an empty survivor list after seven honest gates is a result,
not a malfunction.
"""
from __future__ import annotations

import csv
import dataclasses
import pathlib

import yaml

from arena.strategies import STRATEGIES
from signals import rules as rules_mod

from . import backtest, gates

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
FUNNEL_CSV = ROOT / "results" / "funnel.csv"
BENCH_YAML = ROOT / "bench.yaml"
LOCKED_LOG = ROOT / "results" / "locked_test_log.csv"


@dataclasses.dataclass
class Idea:
    id: str
    name: str
    kind: str
    bar_size: str
    strategy: object
    neighbours: dict

    @property
    def observe_only(self) -> bool:
        return self.kind == "benchmark"


def ideas_from_rules(bar_size: str = "5m") -> list[Idea]:
    doc = rules_mod.load()
    out = []
    for rule in doc["strategies"]:
        strategy = STRATEGIES.get(rule["id"])
        if strategy is None:
            continue
        out.append(Idea(id=rule["id"], name=rule["name"],
                        kind=rule.get("kind", "mixed"), bar_size=bar_size,
                        strategy=strategy,
                        neighbours=dict(getattr(strategy, "NEIGHBOURS", {}))))
    return sorted(out, key=lambda i: i.id)


def _neighbour_runs(idea: Idea, symbols, enriched, days) -> list[dict]:
    """Every parameter, one step down and one step up."""
    out = []
    defaults = idea.strategy.params
    for name in sorted(idea.neighbours):
        for value in idea.neighbours[name]:
            if value == defaults.get(name):
                continue
            variant = idea.strategy.variant(**{name: value})
            result = backtest.run(variant, symbols, enriched, days)
            result["label"] = "%s=%g" % (name, value) if isinstance(value, (int, float)) \
                else "%s=%s" % (name, value)
            out.append(result)
    return out


def run_idea(idea: Idea, enriched: dict, labels: dict, days: int,
             locked: dict | None = None) -> dict:
    """One idea through the gates, stopping at the first failure."""
    row = {"id": idea.id, "name": idea.name, "kind": idea.kind,
           "bar_size": idea.bar_size, "gates": {}, "failed_at": None,
           "survivor": False, "training": None, "double": None,
           "locked": None, "neighbours": []}

    fits = gates.gate_fits_asset(idea, labels)
    row["gates"]["fits_asset"] = fits
    if not fits["pass"]:
        row["failed_at"] = "fits_asset"
        return row
    symbols = fits["symbols"]

    training = backtest.run(idea.strategy, symbols, enriched, days)
    row["training"] = training
    for key, verdict in (("training_profit", gates.gate_training_profit(training)),
                         ("enough_trades", gates.gate_enough_trades(idea, training)),
                         ("profit_factor", gates.gate_profit_factor(training))):
        row["gates"][key] = verdict
        if not verdict["pass"]:
            row["failed_at"] = key
            return row

    neighbours = _neighbour_runs(idea, symbols, enriched, days)
    row["neighbours"] = [{"label": n["label"], "net": n["net"]} for n in neighbours]
    verdict = gates.gate_neighbours(neighbours)
    row["gates"]["neighbours"] = verdict
    if not verdict["pass"]:
        row["failed_at"] = "neighbours"
        return row

    doubled = backtest.run(idea.strategy, symbols, enriched, days,
                           cost_multiple=gates.DOUBLE_COST)
    row["double"] = doubled
    verdict = gates.gate_double_cost(doubled)
    row["gates"]["double_cost"] = verdict
    if not verdict["pass"]:
        row["failed_at"] = "double_cost"
        return row

    locked_result = None
    if locked is not None:
        locked_result = backtest.run(idea.strategy, symbols, locked["enriched"],
                                     locked["days"])
        row["locked"] = locked_result
        # Logged the moment it runs, before the verdict is known, so that a
        # second run on the same idea is visible to anyone reading the file
        # (AGENTS.md §1.6). An idea appearing twice is the red flag.
        log_locked(idea.id, locked_result, locked.get("note", ""))
    verdict = gates.gate_locked(locked_result)
    row["gates"]["locked"] = verdict
    if not verdict["pass"]:
        row["failed_at"] = "locked"
        return row

    row["survivor"] = True
    return row


def run_all(enriched: dict, labels: dict, days: int, bar_size: str = "5m",
            locked: dict | None = None) -> dict:
    ideas = ideas_from_rules(bar_size)
    rows = [run_idea(i, enriched, labels, days, locked) for i in ideas]

    counts = {}
    remaining = len(rows)
    counts["ideas_in"] = remaining
    for key in gates.ORDER:
        reached = [r for r in rows if key in r["gates"]]
        passed = [r for r in reached if r["gates"][key]["pass"]]
        counts[key] = {"reached": len(reached), "passed": len(passed),
                       "failed": len(reached) - len(passed)}
    counts["survivors"] = sum(1 for r in rows if r["survivor"])
    return {"rows": rows, "counts": counts, "bar_size": bar_size,
            "sessions": days, "locked_run": locked is not None}


# ------------------------------------------------------------- outputs

CSV_FIELDS = ["id", "name", "kind", "bar_size", "survivor", "failed_at",
              "trades", "net", "profit_factor", "max_drawdown", "return_pct",
              "net_2x", "neighbours_tested", "neighbours_losing", "why"]


def to_csv(report: dict, path: pathlib.Path | None = None) -> pathlib.Path:
    path = path or FUNNEL_CSV
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in report["rows"]:
            t = row["training"] or {}
            n = row["gates"].get("neighbours", {})
            failed = row["failed_at"]
            writer.writerow({
                "id": row["id"], "name": row["name"], "kind": row["kind"],
                "bar_size": row["bar_size"],
                "survivor": "yes" if row["survivor"] else "no",
                "failed_at": gates.TITLES.get(failed, failed or ""),
                "trades": t.get("trades", ""), "net": t.get("net", ""),
                "profit_factor": t.get("profit_factor", ""),
                "max_drawdown": t.get("max_drawdown", ""),
                "return_pct": t.get("return_pct", ""),
                "net_2x": (row["double"] or {}).get("net", ""),
                "neighbours_tested": n.get("tested", ""),
                "neighbours_losing": len(n.get("losers", []) or []),
                "why": row["gates"].get(failed, {}).get("why", "") if failed else "survived",
            })
    return path


def to_bench(report: dict, path: pathlib.Path | None = None) -> pathlib.Path:
    """Survivors get capital. Everything else stays in the Arena with none."""
    path = path or BENCH_YAML
    survivors, observe = [], []
    for row in report["rows"]:
        entry = {"id": row["id"], "name": row["name"], "kind": row["kind"]}
        if row["survivor"]:
            t = row["training"]
            entry.update({"trades": t["trades"], "net": t["net"],
                          "profit_factor": t["profit_factor"],
                          "max_drawdown": t["max_drawdown"],
                          "symbols": t["symbols"]})
            survivors.append(entry)
        else:
            entry["failed_at"] = gates.TITLES.get(row["failed_at"], row["failed_at"])
            entry["why"] = row["gates"].get(row["failed_at"], {}).get("why", "")
            observe.append(entry)

    doc = {
        "generated_from": {"bar_size": report["bar_size"],
                           "sessions": report["sessions"],
                           "locked_test_run": report["locked_run"]},
        "survivors": survivors,
        "observe_only": observe,
    }
    header = (
        "# Which ideas have earned capital, and which have not.\n"
        "#\n"
        "# Survivors cleared all seven funnel gates. Everything under\n"
        "# observe_only keeps running in the Arena so its behaviour stays\n"
        "# visible, and gets no capital: a rule that failed a gate is not\n"
        "# deleted, because knowing it still loses is worth the screen space.\n"
        "#\n"
        "# Generated by `python -m funnel.cli`. Do not hand-edit.\n")
    path.write_text(header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    return path


def log_locked(idea_id: str, result: dict, note: str = "",
               path: pathlib.Path | None = None) -> None:
    """Append one locked-window run to the log.

    Append only. A row is never rewritten: the whole value of the log is
    that a rerun shows up as a second line rather than replacing the
    first one.
    """
    import datetime

    path = path or LOCKED_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or not path.read_text().strip()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["idea_id", "date_run", "result", "notes"])
        verdict = "pass" if result.get("ok") and result.get("net", 0) > 0 else "fail"
        writer.writerow([
            idea_id,
            datetime.date.today().isoformat(),
            verdict,
            note or "net %+.2f over %d trades"
                    % (result.get("net", 0.0), result.get("trades", 0)),
        ])


def locked_runs(path: pathlib.Path | None = None) -> dict:
    """How many times each idea has used its one locked-window run."""
    path = path or LOCKED_LOG
    if not path.exists():
        return {}
    counts: dict = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            idea = (row.get("idea_id") or "").strip()
            if idea:
                counts[idea] = counts.get(idea, 0) + 1
    return counts
