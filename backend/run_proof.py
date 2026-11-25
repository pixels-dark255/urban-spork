"""Phase 3 CHECK: the three synthetic results, the guard, and the proof.

Prints each measured number against the band that was written before any
of it was run, so a reader can see which way the comparison went rather
than being told it passed.
"""
import argparse
import os
import sys

import fingerprint as fp
import research_store
from arena import markets as markets_mod
from arena.strategies import enrich
from validation import arena_guard, lookahead, samecode, synthetic

# The same bands as tests/test_backtester_proof.py, which committed them
# before the implementation existed.
PLANTED_MIN, PLANTED_MAX = 0.6, 1.2
NOISE_TOLERANCE = 0.15
SEED = 20260325


def _verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sessions", type=int, default=12)
    ap.add_argument("--dates", type=int, default=30)
    ap.add_argument("--no-store", action="store_true")
    args = ap.parse_args(argv)

    failures = []
    lines = ["Phase 3: proving the backtester", ""]

    # ---------------------------------------------------- 1 and 2
    planted = synthetic.run_planted_edge(1.0, seed=SEED)
    noise = synthetic.run_planted_edge(0.0, seed=SEED)

    planted_ok = PLANTED_MIN <= planted["avg_net_pct"] <= PLANTED_MAX
    expected_noise = -noise["cost_pct"]
    noise_ok = (abs(noise["avg_net_pct"] - expected_noise) <= NOISE_TOLERANCE
                and noise["avg_net_pct"] < 0)
    failures += [] if planted_ok else ["planted edge"]
    failures += [] if noise_ok else ["pure noise"]

    lines += [
        "%-16s %-34s %-26s %s" % ("TEST", "MEASURED", "BAND (written first)", "RESULT"),
        "-" * 92,
        "%-16s %-34s %-26s %s" % (
            "planted 1.0%",
            "net %+.3f%% (gross %+.3f%%, %d trades)"
            % (planted["avg_net_pct"], planted["avg_gross_pct"], planted["trades"]),
            "%.1f%% to %.1f%%" % (PLANTED_MIN, PLANTED_MAX),
            _verdict(planted_ok)),
        "%-16s %-34s %-26s %s" % (
            "pure noise",
            "net %+.3f%% (gross %+.3f%%, %d trades)"
            % (noise["avg_net_pct"], noise["avg_gross_pct"], noise["trades"]),
            "%+.3f%% +/- %.2f%%" % (expected_noise, NOISE_TOLERANCE),
            _verdict(noise_ok)),
    ]

    # ---------------------------------------------------------- 3
    frame = synthetic.random_walk(600, seed=SEED)
    honest = lookahead.check(synthetic.honest_signal, frame, args.dates, SEED)
    cheat = lookahead.check(synthetic.cheating_signal, frame, args.dates, SEED)
    guard_ok = honest["ok"] and not cheat["ok"]
    failures += [] if guard_ok else ["look-ahead guard"]
    lines.append("%-16s %-34s %-26s %s" % (
        "look-ahead",
        "honest %d/%d clean, cheat %d/%d caught"
        % (honest["checked"] - len(honest["mismatches"]), honest["checked"],
           len(cheat["mismatches"]), cheat["checked"]),
        "honest clean, cheat caught", _verdict(guard_ok)))

    # ---------------------------------------------------------- 4
    report = arena_guard.run(sessions=args.sessions, dates=args.dates, seed=SEED)
    control_ok = report["control"]["caught"]
    strategies_ok = not report["failed"] and report["regime"]["ok"] and control_ok
    failures += [] if strategies_ok else ["strategy guard"]

    lines += ["", "Look-ahead guard, every Arena strategy", "-" * 92]
    for row in report["strategies"]:
        lines.append("  %-18s %-5s %d dates checked%s" % (
            row["id"], _verdict(row["ok"]), row["checked"],
            "" if row["ok"] else "   DISABLED: %d mismatches" % row["mismatches"]))
    lines.append("  %-18s %-5s %d dates checked" % (
        "regime layer", _verdict(report["regime"]["ok"]), report["regime"]["checked"]))
    lines.append("  %-18s %-5s %d of %d dates caught (the control; if this "
                 "passes the guard proves nothing)" % (
                     "deliberate cheat", _verdict(control_ok),
                     report["control"]["mismatches"], report["control"]["checked"]))
    if report["failed"]:
        lines.append("")
        lines.append("  DISABLED (reads the future): %s" % ", ".join(report["failed"]))

    # ---------------------------------------------------------- 5
    bars = arena_guard.intraday_bars(sessions=args.sessions, seed=SEED)
    enriched = {"TEST.NS": enrich(bars, market=markets_mod.get("nse"))}
    proof = samecode.compare({"market": "nse", "symbols": ["TEST.NS"]},
                             enriched, days=args.sessions)
    same_ok = proof["match"] and proof["replay_trades"] > 0
    failures += [] if same_ok else ["same-code proof"]
    lines += ["", "Same-code proof (replay vs the live drip-feed)", "-" * 92,
              "  %d sessions, %d trades from replay and %d from live: %s"
              % (proof["sessions"], proof["replay_trades"], proof["live_trades"],
                 "identical" if proof["match"] else "DIFFERENT")]
    if proof["differences"]:
        for diff in proof["differences"][:3]:
            lines.append("    %r" % diff)

    values = {
        "planted_net": round(planted["avg_net_pct"], 4),
        "planted_gross": round(planted["avg_gross_pct"], 4),
        "planted_trades": planted["trades"],
        "noise_net": round(noise["avg_net_pct"], 4),
        "noise_gross": round(noise["avg_gross_pct"], 4),
        "guard_failed": ",".join(report["failed"]) or "none",
        "guard_control_caught": control_ok,
        "regime_ok": report["regime"]["ok"],
        "samecode_match": proof["match"],
        "samecode_trades": proof["replay_trades"],
    }
    for row in report["strategies"]:
        values["guard:" + row["id"]] = row["ok"]

    sha = fp.fingerprint(values)
    lines += ["", fp.line(3, values)]
    text = "\n".join(lines)
    print(text)

    if not args.no_store:
        payload = {
            "fingerprint": sha,
            "bands": {"planted_min": PLANTED_MIN, "planted_max": PLANTED_MAX,
                      "noise_tolerance": NOISE_TOLERANCE},
            "planted": planted, "noise": noise,
            "noise_expected": expected_noise,
            "results": {"planted_ok": planted_ok, "noise_ok": noise_ok,
                        "guard_ok": guard_ok, "strategies_ok": strategies_ok,
                        "samecode_ok": same_ok},
            "guard": report, "samecode": proof,
        }
        try:
            research_store.save("3", sha, payload)
        except Exception as exc:
            print("[warn] could not store the proof: %s" % exc, file=sys.stderr)

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("## Phase 3: proving the backtester\n\n```\n%s\n```\n" % text)

    if failures:
        print("\nFAILED: %s" % ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
