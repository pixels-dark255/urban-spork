"""Kill lines: the drawdown past which a strategy stops trading.

A strategy was tested through some worst drawdown. Past a multiple of it,
the thing in front of you is no longer the thing that was tested, and the
honest response is to stop rather than to hope.

    sleeve / intraday   2x its own worst tested drawdown  -> switched off
    core                1.5x its worst                    -> alert, and no
                                                             new sleeves

The core is treated differently on purpose. Switching the core off is a
decision about the whole account, so it stays human-only: crossing its
line raises an alert and blocks new sleeves, and changes nothing else.

Switching off is separated from being written out. Crossing the line
always stops new entries immediately, because that is safety. Being
marked `out` - eligible for replacement - needs a minimum amount of live
evidence as well, so that one dreadful week cannot retire a strategy
(the brief's rule 6, and status.py enforces the other half).
"""
from __future__ import annotations

import datetime as dt
import pathlib

CORE_MULTIPLE = 1.5
CHANGELOG = pathlib.Path(__file__).resolve().parent.parent.parent / "CHANGELOG_rules.md"


def kill_line(worst_tested_drawdown_pct: float, multiple: float) -> float:
    """The drawdown that stops it, as a negative percentage."""
    return -abs(float(worst_tested_drawdown_pct)) * float(multiple)


def crossed(live_drawdown_pct: float, line_pct: float) -> bool:
    return float(live_drawdown_pct) <= float(line_pct)


def half_way(live_drawdown_pct: float, line_pct: float) -> bool:
    return float(live_drawdown_pct) <= float(line_pct) / 2.0


def evaluate(strategies: list[dict], multiple: float) -> list[dict]:
    """Each strategy against its own line. Sorted, for a stable report."""
    out = []
    for row in sorted(strategies, key=lambda r: r["id"]):
        worst = abs(float(row.get("worst_tested_drawdown_pct") or 0.0))
        live = float(row.get("live_drawdown_pct") or 0.0)
        line = kill_line(worst, multiple)
        out.append({
            "id": row["id"], "worst_tested_drawdown_pct": -worst,
            "live_drawdown_pct": live, "kill_line_pct": line,
            "crossed": crossed(live, line) if worst else False,
            "half_way": half_way(live, line) if worst else False,
            # Without a tested drawdown there is no line to cross, which
            # is itself worth saying rather than defaulting to "fine".
            "measurable": bool(worst),
        })
    return out


def evaluate_core(live_drawdown_pct: float, worst_tested_drawdown_pct: float) -> dict:
    worst = abs(float(worst_tested_drawdown_pct or 0.0))
    line = kill_line(worst, CORE_MULTIPLE)
    hit = crossed(live_drawdown_pct, line) if worst else False
    return {"live_drawdown_pct": float(live_drawdown_pct),
            "worst_tested_drawdown_pct": -worst,
            "kill_line_pct": line, "crossed": hit,
            "stop_new_sleeves": hit,
            "action": ("alert and stop new sleeves; the core itself is a "
                       "human decision" if hit else "none"),
            "measurable": bool(worst)}


def write_changelog(entries: list[dict], path: pathlib.Path | None = None,
                    today: dt.date | None = None) -> pathlib.Path:
    """Append why a strategy was switched off. Append only.

    The log exists so that "why is this off?" has an answer six months
    later. Rewriting an entry would make it a story rather than a record.
    """
    path = pathlib.Path(path or CHANGELOG)
    today = today or dt.date.today()
    if not path.exists():
        path.write_text(
            "# Rule changelog\n\n"
            "Append only. Every line is something that stopped trading, or\n"
            "was rotated, and why. Nothing here is ever edited: a record\n"
            "that can be rewritten is a story.\n\n"
            "A kill line is a multiple of the worst drawdown a strategy was\n"
            "tested through. Past it, the thing running is no longer the\n"
            "thing that was tested.\n")
    lines = []
    for entry in entries:
        lines.append(
            "- **%s** %s switched off: live drawdown %.2f%% past its kill "
            "line of %.2f%% (%.1fx the %.2f%% it was tested through).%s"
            % (today.isoformat(), entry["id"], entry["live_drawdown_pct"],
               entry["kill_line_pct"], entry.get("multiple", 2.0),
               abs(entry["worst_tested_drawdown_pct"]),
               ("  " + entry["note"]) if entry.get("note") else ""))
    if lines:
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n" + "\n".join(lines) + "\n")
    return path
