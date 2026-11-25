"""The scheduled review: weekly, monthly, quarterly.

    weekly (Saturday)   reconciliation and kill lines
    monthly             the status table
    quarterly           a re-pick proposal, which waits for a human yes

The review may fix a code bug. It may never change a rule. That boundary
is the reason this exists at all: a review that could edit a rule would,
sooner or later, edit the rule that was losing, and the whole funnel
behind it would become decoration. `touches_rules` is the check, and the
quarterly proposal is a proposal rather than an action for the same
reason.

The first three lines of every report go to Telegram. Three, because a
report nobody reads is not a report, and the part that matters has to
survive a phone notification.
"""
from __future__ import annotations

import datetime as dt

WEEKLY, MONTHLY, QUARTERLY = "weekly", "monthly", "quarterly"
TELEGRAM_LINES = 3

APPROVE_FLAG = "REPICK_APPROVED"

# A review may touch these. Anything under rules.yaml or a Strategy's
# parameters is off limits, and the list is explicit so that widening it
# is a visible change rather than a habit.
MAY_FIX = ("a crash", "a wrong number in a report", "a stale cache",
           "a mis-formatted alert", "a failing job")
MAY_NEVER = ("a rule", "a threshold", "a parameter", "a kill line",
             "which strategies hold capital")


def due(today: dt.date) -> list[str]:
    """Which reports are due. Saturday weekly; month end monthly."""
    out = []
    if today.weekday() == 5:
        out.append(WEEKLY)
    tomorrow = today + dt.timedelta(days=1)
    if tomorrow.day == 1:
        out.append(MONTHLY)
        if today.month in (3, 6, 9, 12):
            out.append(QUARTERLY)
    return out


def touches_rules(change: dict) -> bool:
    """True if a proposed change would alter a rule rather than fix a bug."""
    target = str(change.get("target", "")).lower()
    for marker in ("rules.yaml", "strategies.py", "params", "neighbours",
                   "kill_line", "bench.yaml", "threshold"):
        if marker in target:
            return True
    return bool(change.get("changes_rule"))


def weekly_report(reconciliation: dict, kills: list[dict],
                  core_kill: dict, today: dt.date) -> list[str]:
    bugs = len(reconciliation.get("bugs", []))
    off = [k["id"] for k in kills if k["crossed"]]
    lines = [
        "TickerBoard weekly %s" % today.isoformat(),
        "Paper vs backtest: %s" % (
            "clean" if not bugs else "%d gap(s) nothing benign explains" % bugs),
        "Kill lines: %s" % (
            "none crossed" if not off else "crossed by %s" % ", ".join(off)),
    ]
    if core_kill.get("crossed"):
        lines.append("Core past %.1fx its worst tested drawdown: no new sleeves."
                     % 1.5)
    counts = reconciliation.get("counts", {})
    lines.append("Gaps: " + (", ".join("%d %s" % (v, k)
                                       for k, v in sorted(counts.items()) if v)
                             or "none"))
    return lines


def monthly_report(statuses: list[dict], today: dt.date) -> list[str]:
    from .status import counts as count_statuses
    counts = count_statuses(statuses)
    lines = [
        "TickerBoard monthly %s" % today.isoformat(),
        "Status: %s" % ", ".join("%d %s" % (v, k) for k, v in counts.items() if v),
        "Out and replaceable: %s" % (
            ", ".join(s["id"] for s in statuses if s["status"] == "out") or "none"),
    ]
    for row in statuses:
        lines.append("  %-18s %-7s %s" % (row["id"], row["status"], row["why"]))
    return lines


def quarterly_proposal(picks: list[dict], today: dt.date,
                       approved: bool = False) -> dict:
    """A proposal, not an action. It waits for a human yes."""
    lines = ["TickerBoard quarterly re-pick %s" % today.isoformat(),
             "This is a proposal and changes nothing until you say yes.",
             "Proposed: %s" % (", ".join(
                 "%s -> %s" % (p.get("from", "-"), p.get("to", "-"))
                 for p in picks) or "no change")]
    return {"lines": lines, "applied": False, "approved": approved,
            "waiting_for": None if approved else APPROVE_FLAG,
            "picks": picks}


def telegram_head(lines: list[str]) -> str:
    return "\n".join(lines[:TELEGRAM_LINES])
