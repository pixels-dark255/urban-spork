"""CHECK for Phase 8: kill lines, the status table, and a dry-run rotate."""
import argparse
import datetime as dt
import os
import sys

import numpy as np
import pandas as pd
import yaml

import fingerprint as fp
import settings
from funnel import run as funnel_run

from . import killlines as kl, reconcile as rc, review as rv, rotate as rot, status as st

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def live_rows(bench_doc: dict) -> list[dict]:
    """What the book is actually running, with its live record.

    With no survivors there is nothing live, and the honest table is an
    empty one that says so rather than a demo pretending otherwise.
    """
    return [{"id": s["id"], "ticker": (s.get("symbols") or [None])[0],
             "worst_tested_drawdown_pct": s.get("max_drawdown_pct") or 0.0,
             "live_drawdown_pct": 0.0, "live_trades": 0, "live_days": 0,
             "tested_trades": s.get("trades") or 0, "tested_days": 0}
            for s in (bench_doc.get("survivors") or [])]


def demo_rows() -> list[dict]:
    """Clearly-labelled examples, so the machinery can be seen working."""
    return [
        {"id": "demo_keep", "ticker": "D1.NS", "worst_tested_drawdown_pct": -10,
         "live_drawdown_pct": -3, "live_trades": 50, "live_days": 60,
         "tested_trades": 50, "tested_days": 60},
        {"id": "demo_watch_halfway", "ticker": "D2.NS",
         "worst_tested_drawdown_pct": -10, "live_drawdown_pct": -12,
         "live_trades": 40, "live_days": 60, "tested_trades": 50,
         "tested_days": 60},
        {"id": "demo_watch_thin", "ticker": "D3.NS",
         "worst_tested_drawdown_pct": -10, "live_drawdown_pct": -25,
         "live_trades": 3, "live_days": 5, "tested_trades": 50,
         "tested_days": 60},
        {"id": "demo_review_freq", "ticker": "D4.NS",
         "worst_tested_drawdown_pct": -10, "live_drawdown_pct": -2,
         "live_trades": 40, "live_days": 80, "tested_trades": 200,
         "tested_days": 60},
        {"id": "demo_out", "ticker": "D5.NS", "worst_tested_drawdown_pct": -10,
         "live_drawdown_pct": -25, "live_trades": 40, "live_days": 60,
         "tested_trades": 50, "tested_days": 60},
    ]


def bar(value: float, line: float, width: int = 24) -> str:
    """A text kill-line chart: how far along its rope a strategy is."""
    if not line:
        return "(no tested drawdown, so no line)"
    share = max(0.0, min(1.5, abs(value) / abs(line)))
    filled = int(round(share * width))
    marker = "|"
    cells = ["#" if i < filled else "." for i in range(width + 8)]
    cells[width] = marker
    return "".join(cells[:width + 8]) + "  %.0f%% of the line" % (share * 100)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo", action="store_true",
                    help="use labelled example strategies (the live book is empty)")
    args = ap.parse_args(argv)

    cfg = settings.load()
    multiple = cfg["kill_line_multiple"]
    today = dt.datetime.now(IST).date()

    bench = {}
    if funnel_run.BENCH_YAML.exists():
        bench = yaml.safe_load(funnel_run.BENCH_YAML.read_text()) or {}
    rows = live_rows(bench)
    demo = args.demo or not rows
    if demo:
        rows = demo_rows()

    lines = ["Phase 8: adapt", "",
             "Kill line: %.1fx a strategy's own worst tested drawdown; "
             "core at %.1fx." % (multiple, kl.CORE_MULTIPLE)]
    if demo:
        lines += ["",
                  "  *** The live book is empty: bench.yaml has no survivors, so",
                  "  *** nothing is actually running. The rows below are labelled",
                  "  *** examples that exercise every branch of the machinery."]
    lines.append("")

    # ------------------------------------------------- kill lines
    kills = kl.evaluate(rows, multiple)
    lines += ["Kill lines", "-" * 74,
              "  %-20s %9s %9s  %s" % ("STRATEGY", "LIVE DD", "LINE", "")]
    for k in kills:
        lines.append("  %-20s %8.2f%% %8.2f%%  %s"
                     % (k["id"], k["live_drawdown_pct"], k["kill_line_pct"],
                        bar(k["live_drawdown_pct"], k["kill_line_pct"])))
    core_kill = kl.evaluate_core(-12.0, -28.52)
    lines += ["", "  core: live %.2f%%, line %.2f%% -> %s"
              % (core_kill["live_drawdown_pct"], core_kill["kill_line_pct"],
                 core_kill["action"]), ""]

    # ------------------------------------------------- status table
    statuses = st.table(rows, kills)
    counts = st.counts(statuses)
    lines += ["Status", "-" * 74,
              "  %s" % ", ".join("%d %s" % (v, k) for k, v in counts.items() if v)]
    for row in statuses:
        lines.append("  %-20s %-7s %s" % (row["id"], row["status"], row["why"][:52]))
    lines.append("")

    # ------------------------------------------------- dry-run rotate
    idx = pd.bdate_range(today - dt.timedelta(days=400), today)
    rng = np.random.default_rng(20260325)
    core_equity = pd.Series(
        cfg["swing_capital_inr"] * np.cumprod(1 + rng.normal(0.0004, 0.008, len(idx))),
        index=idx)
    keeping = [{"id": r["id"], "ticker": r["ticker"],
                "returns": pd.Series(rng.normal(0.0002, 0.004, len(idx)), index=idx)}
               for r in statuses if r["status"] == st.KEEP]
    candidates = [
        {"id": "cand_good", "ticker": "C1.NS",
         "returns": pd.Series(rng.normal(0.0008, 0.004, len(idx)), index=idx)},
        {"id": "cand_poor", "ticker": "C2.NS",
         "returns": pd.Series(rng.normal(-0.0009, 0.004, len(idx)), index=idx)},
    ]
    if keeping:
        candidates.append({"id": "cand_same_ticker", "ticker": keeping[0]["ticker"],
                           "returns": pd.Series(rng.normal(0.001, 0.004, len(idx)),
                                                index=idx)})
    out_ids = [r["id"] for r in statuses if r["status"] == st.OUT]
    lines += ["Rotate (dry run)", "-" * 74]
    proposal = None
    if not out_ids:
        lines.append("  nothing is `out`, so there is nothing to replace")
    else:
        proposal = rot.propose(out_ids[0], candidates, keeping, core_equity,
                               cfg["swing_capital_inr"])
        lines.append("  %s %s" % ("WOULD ROTATE:" if proposal["rotate"] else "NO ROTATION:",
                                  proposal["why"][:74]))
        for r in proposal["rejected"]:
            lines.append("    rejected %-18s %s" % (r["id"], r["why"][:50]))
        lines.append("  nothing was written: this is a dry run")
    lines.append("")

    # ------------------------------------------------- reconciliation
    empty = rc.compare([], [])
    lines += ["Paper vs backtest", "-" * 74,
              "  %s" % (rc.summary(empty) if not demo else
                        "no live trades to reconcile yet (the book is empty)"), ""]

    # ------------------------------------------------- the reports
    weekly = rv.weekly_report(empty, kills, core_kill, today)
    monthly = rv.monthly_report(statuses, today)
    lines += ["Reports due today (%s): %s" % (today, rv.due(today) or "none"),
              "-" * 74, "  First %d lines go to Telegram:" % rv.TELEGRAM_LINES]
    for line in rv.telegram_head(weekly).splitlines():
        lines.append("    | %s" % line)
    lines.append("")

    values = {
        "demo": demo, "strategies": len(rows),
        "kill_multiple": multiple, "core_multiple": kl.CORE_MULTIPLE,
        "crossed": sum(1 for k in kills if k["crossed"]),
        "core_crossed": core_kill["crossed"],
        "rotate": bool(proposal and proposal["rotate"]),
        "rotate_chosen": (proposal or {}).get("chosen", {}) and
                         (proposal or {}).get("chosen", {}).get("id") or "none",
        "reconcile_bugs": len(empty.get("bugs", [])),
        **{"status:%s" % k: v for k, v in counts.items()},
    }
    text = "\n".join(lines)
    print(text)
    sha = fp.fingerprint(values)
    print(fp.line(8, values))

    try:
        import research_store
        research_store.save("8", sha, {
            "fingerprint": sha, "demo": demo, "kills": kills,
            "core_kill": core_kill, "statuses": statuses, "counts": counts,
            "rotate": proposal, "weekly": weekly, "monthly": monthly,
            "reports_due": rv.due(today), "kill_multiple": multiple,
        })
    except Exception as exc:
        print("[warn] could not store: %s" % exc, file=sys.stderr)

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("## Phase 8: adapt\n\n```\n%s\n```\n" % text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
