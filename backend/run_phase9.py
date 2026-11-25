"""Phase 9 CHECK: the next-open fill rule, measured rather than asserted.

AGENTS.md §1.3 and §1.4 were both marked "not yet true" until this phase.
This prints the evidence that they are now: every entry price recomputed
from the bars, the two drop reasons only a next-open rule can produce, and
the slippage sweep that the 2x gate reads its top multiple from.
"""
import argparse
import os
import sys

import fingerprint as fp
from arena import engine
from arena import markets as markets_mod
from arena.strategies import enrich
from funnel import gates
from validation import arena_guard, fills

SEED = 20260325


def _verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sessions", type=int, default=12)
    args = ap.parse_args(argv)

    failures = []
    lines = ["Phase 9: the next-open fill rule", ""]

    bars = arena_guard.intraday_bars(sessions=args.sessions, seed=SEED)
    market = markets_mod.get("nse")
    cfg = {"market": "nse", "symbols": ["TEST.NS"]}

    flat = fills.audit(cfg, {"TEST.NS": enrich(bars, market=market)}, days=args.sessions)
    gapped_bars = fills.with_gaps(bars, seed=SEED)
    gapped = fills.audit(cfg, {"TEST.NS": enrich(gapped_bars, market=market)},
                         days=args.sessions)

    both_ok = flat["ok"] and gapped["ok"]
    failures += [] if both_ok else ["fill audit"]

    lines += [
        "Every entry price recomputed from the bars it claims to have filled on",
        "-" * 92,
        "%-22s %-12s %-14s %s" % ("BARS", "ENTRIES", "MISMATCHES", "RESULT"),
        "%-22s %-12d %-14d %s" % ("as generated", flat["checked"],
                                  len(flat["mismatches"]), _verdict(flat["ok"])),
        "%-22s %-12d %-14d %s" % ("with seeded gaps", gapped["checked"],
                                  len(gapped["mismatches"]), _verdict(gapped["ok"])),
        "",
        "  The generator opens each bar at the previous close, so on those bars the",
        "  old rule and the new one agree to the paisa (%d fills moved). Gapping the"
        % flat["moved"],
        "  opens by %.2f%% moves every one of them: average %+.4f%%, worst %+.3f%%."
        % (fills.GAP_PCT, gapped["avg_move_pct"], gapped["worst_move_pct"]),
    ]

    # The two drop reasons. gapped_through falls out of the gapped replay;
    # whether any strategy signals in the last five minutes is an accident
    # of the fixture, so no_next_bar is driven directly.
    last_bar = fills.last_bar_scenario()
    gap_ok = gapped["gapped_through"] > 0
    last_ok = last_bar["no_next_bar"] == 1 and last_bar["positions"] == 0
    failures += [] if gap_ok else ["gapped_through"]
    failures += [] if last_ok else ["no_next_bar"]

    lines += [
        "",
        "What a next-open rule drops that a same-bar rule cannot",
        "-" * 92,
        "%-22s %-40s %s" % ("gapped_through", "%d setups, open past their own stop"
                            % gapped["gapped_through"], _verdict(gap_ok)),
        "%-22s %-40s %s" % ("no_next_bar", "%d signal on the last tradeable bar, %d taken"
                            % (last_bar["no_next_bar"], last_bar["positions"]),
                            _verdict(last_ok)),
    ]

    # §1.4: the sweep and the gate must read the same number.
    sweep = list(engine.SLIPPAGE_MULTIPLES)
    sweep_ok = 2.0 in sweep and gates.DOUBLE_COST == max(sweep)
    failures += [] if sweep_ok else ["slippage sweep"]
    lines += [
        "",
        "Costs are always on, and always also at 2x (AGENTS.md §1.4)",
        "-" * 92,
        "%-22s %-40s %s" % ("engine sweep", ", ".join("%gx" % m for m in sweep), ""),
        "%-22s %-40s %s" % ("funnel gate 6", "reads %gx from the sweep" % gates.DOUBLE_COST,
                            _verdict(sweep_ok)),
    ]

    values = {
        "flat_checked": flat["checked"],
        "flat_mismatches": len(flat["mismatches"]),
        "flat_moved": flat["moved"],
        "gapped_checked": gapped["checked"],
        "gapped_mismatches": len(gapped["mismatches"]),
        "gapped_moved": gapped["moved"],
        "gapped_avg_move_pct": round(gapped["avg_move_pct"], 6),
        "gapped_worst_move_pct": round(gapped["worst_move_pct"], 6),
        "gapped_through": gapped["gapped_through"],
        "no_next_bar": last_bar["no_next_bar"],
        "last_bar_positions": last_bar["positions"],
        "slippage_multiples": sweep,
        "gate6_multiple": gates.DOUBLE_COST,
    }
    lines += ["", fp.line(9, values)]
    text = "\n".join(lines)
    print(text)

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("## Phase 9: the next-open fill rule\n\n```\n%s\n```\n" % text)

    if failures:
        print("\nFAILED: %s" % ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
