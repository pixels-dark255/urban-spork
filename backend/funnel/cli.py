"""CHECK for Phase 4: funnel counts, survivors, fingerprint."""
import argparse
import os
import sys

import fingerprint as fp
import research_store
from arena import markets as markets_mod
from arena.strategies import enrich

from . import gates, run as run_mod


def load_bars(source: str, sessions: int, symbols: list[str]) -> dict:
    """Enriched bars for every symbol the funnel might trade."""
    market = markets_mod.get("nse")
    if source == "synthetic":
        from validation import arena_guard
        return {sym: enrich(arena_guard.intraday_bars(
            sessions=sessions, seed=20260325 + i), market=market)
            for i, sym in enumerate(sorted(symbols))}

    import data_sources
    out = {}
    for sym in sorted(symbols):
        frame = data_sources._yf_download(sym, "60d", "5m", timeout=45.0)
        if frame is not None and not frame.empty:
            out[sym] = enrich(frame, market=market)
    return out


def bars_summary(enriched: dict, wanted: list[str]) -> dict:
    """What the funnel actually ran on.

    Yahoo is not a fixed input: a 5-minute window rolls, and a symbol can
    fail to download on one run and succeed on the next. Two runs of the
    same code can then disagree, which looks like a changed rule. These
    counts travel with the result so a reader can tell the two apart.
    """
    rows = []
    sessions = set()
    for sym in sorted(enriched):
        frame = enriched[sym]
        days = sorted({d for d in frame["session"].unique() if d})
        sessions.update(days)
        rows.append({"symbol": sym, "bars": int(len(frame)), "sessions": len(days),
                     "first": days[0] if days else "", "last": days[-1] if days else ""})
    return {"symbols": rows,
            "missing": sorted(set(wanted) - set(enriched)),
            "first": min(sessions) if sessions else "",
            "last": max(sessions) if sessions else ""}


def bars_table(summary: dict) -> str:
    lines = ["What it ran on", "-" * 58,
             "  %d symbols, sessions %s to %s"
             % (len(summary["symbols"]), summary["first"] or "?", summary["last"] or "?")]
    for row in summary["symbols"]:
        lines.append("  %-18s %6d bars  %3d sessions  %s to %s"
                     % (row["symbol"], row["bars"], row["sessions"],
                        row["first"], row["last"]))
    if summary["missing"]:
        lines.append("  MISSING: %s (no bars came back, so these were not traded)"
                     % ", ".join(summary["missing"]))
    return "\n".join(lines)


def table(report: dict) -> str:
    counts = report["counts"]
    lines = ["Phase 4: the funnel", "",
             "%d ideas in" % counts["ideas_in"], ""]
    lines.append("%-30s %8s %8s %8s" % ("GATE", "REACHED", "PASSED", "FAILED"))
    lines.append("-" * 58)
    for key in gates.ORDER:
        c = counts[key]
        lines.append("%-30s %8d %8d %8d"
                     % (gates.TITLES[key], c["reached"], c["passed"], c["failed"]))
    lines += ["-" * 58,
              "%-30s %8s %8d" % ("SURVIVORS", "", counts["survivors"]), ""]

    lines.append("Where each idea stopped")
    lines.append("-" * 58)
    for row in sorted(report["rows"], key=lambda r: (not r["survivor"], r["id"])):
        where = "SURVIVED" if row["survivor"] else gates.TITLES.get(row["failed_at"], "?")
        why = row["gates"].get(row["failed_at"], {}).get("why", "") if row["failed_at"] else ""
        lines.append("  %-18s %-30s %s" % (row["id"], where, why[:70]))

    survivors = [r for r in report["rows"] if r["survivor"]]
    lines += ["", "Survivors"]
    lines.append("-" * 58)
    if not survivors:
        lines.append("  none. Seven honest gates and nothing cleared them, which is a "
                     "result rather than a malfunction.")
    else:
        lines.append("  %-18s %8s %10s %8s %12s" %
                     ("ID", "TRADES", "RETURN", "PF", "MAX DD"))
        for row in survivors:
            t = row["training"]
            lines.append("  %-18s %8d %9s %8s %12.2f" % (
                row["id"], t["trades"],
                "n/a" if t["return_pct"] is None else "%+.2f%%" % t["return_pct"],
                "n/a" if t["profit_factor"] is None else "%.2f" % t["profit_factor"],
                t["max_drawdown"]))
    return "\n".join(lines)


def fingerprint_values(report: dict) -> dict:
    values = {"ideas_in": report["counts"]["ideas_in"],
              "survivors": report["counts"]["survivors"],
              "bar_size": report["bar_size"], "sessions": report["sessions"]}
    # The input is part of the result. Without this two runs on different
    # downloads can share a fingerprint, or differ on it for a reason no
    # reader can see.
    for row in (report.get("bars") or {}).get("symbols", []):
        values["bars:%s" % row["symbol"]] = row["bars"]
    for key in gates.ORDER:
        c = report["counts"][key]
        values["gate:%s:passed" % key] = c["passed"]
        values["gate:%s:failed" % key] = c["failed"]
    for row in report["rows"]:
        values["%s:failed_at" % row["id"]] = row["failed_at"] or "survived"
        if row["training"]:
            values["%s:net" % row["id"]] = round(row["training"]["net"], 2)
    return values


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=["synthetic", "yahoo"], default="synthetic")
    ap.add_argument("--sessions", type=int, default=12)
    ap.add_argument("--locked", action="store_true",
                    help="run gate 7. ONCE per idea (AGENTS.md §1.6); "
                         "refused on synthetic bars")
    ap.add_argument("--no-store", action="store_true")
    args = ap.parse_args(argv)

    if args.locked and args.source != "yahoo":
        print("[error] the locked test may be run once per idea and must not "
              "be spent on synthetic bars. Use --source yahoo.", file=sys.stderr)
        return 2

    labels = gates.load_labels()
    symbols = sorted(labels["labels"])
    enriched = load_bars(args.source, args.sessions, symbols)
    if not enriched:
        print("[error] no bars", file=sys.stderr)
        return 3

    report = run_mod.run_all(enriched, labels, days=args.sessions)
    report["source"] = args.source
    report["bars"] = bars_summary(enriched, symbols)
    text = table(report) + "\n\n" + bars_table(report["bars"])
    print(text)

    csv_path = run_mod.to_csv(report)
    bench_path = run_mod.to_bench(report)
    print("\nWrote %s and %s" % (csv_path.name, bench_path.name))

    # The dashboard renders the gates in order and by name, so both travel
    # with the result rather than being duplicated in the frontend.
    report["order"] = list(gates.ORDER)
    report["titles"] = dict(gates.TITLES)

    values = fingerprint_values(report)
    sha = fp.fingerprint(values)
    report["fingerprint"] = sha
    print()
    print(fp.line(4, values))

    if not args.no_store:
        try:
            research_store.save("4", sha, report)
        except Exception as exc:
            print("[warn] could not store the funnel: %s" % exc, file=sys.stderr)

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("## Phase 4: the funnel\n\n```\n%s\n```\n" % text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
