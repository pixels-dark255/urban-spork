"""CHECK for Phase 7: the two replays, and whether the layer may be enabled."""
import argparse
import datetime as dt
import json
import os
import pathlib
import sys

import numpy as np
import pandas as pd

import fingerprint as fp
import settings

from . import calls as calls_mod, judge, mask as mask_mod, replay

DOCUMENTS = calls_mod.ARCHIVE / "documents.json"

# Clearly synthetic, and labelled wherever they are used. Real RBI
# statements are not invented here: making up a central bank's words to
# score a trading layer would be worse than having no layer.
DEMO_DOCS = [
    {"date": "2024-02-08", "source_url": "demo://synthetic/1",
     "text": "The committee decided to keep the policy rate unchanged. "
             "Inflation is projected to moderate. Growth remains resilient."},
    {"date": "2024-04-05", "source_url": "demo://synthetic/2",
     "text": "The committee decided to hike the policy rate and to continue "
             "the withdrawal of accommodation. Inflation risks have risen."},
    {"date": "2024-06-07", "source_url": "demo://synthetic/3",
     "text": "The committee decided to cut the policy rate. The stance is "
             "accommodative as inflation has eased durably."},
]


def load_documents(path: pathlib.Path) -> tuple:
    if path.exists():
        docs = json.loads(path.read_text())
        return sorted(docs, key=lambda d: d["date"]), False
    return DEMO_DOCS, True


def _bars(source, tickers, years=4):
    if source == "synthetic":
        rng = np.random.default_rng(20260325)
        idx = pd.bdate_range(dt.date.today() - dt.timedelta(days=int(365.25 * years)),
                             dt.date.today())
        out = {}
        for i, t in enumerate(sorted(tickers)):
            v = 100 * np.cumprod(1 + rng.normal(0.0005 - 0.0003 * i,
                                                0.009 - 0.003 * i, len(idx)))
            out[t] = pd.DataFrame({"Open": v, "High": v * 1.004,
                                   "Low": v * 0.996, "Close": v}, index=idx)
        return out
    import data_sources
    out = {}
    for t in sorted(tickers):
        f = data_sources.fetch_daily_history(t, period="%dy" % years, timeout=45.0)
        if f is not None and not f.empty:
            out[t] = f
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=["synthetic", "yahoo"], default="synthetic")
    ap.add_argument("--documents", type=pathlib.Path, default=DOCUMENTS)
    ap.add_argument("--engine", default="ITBEES.NS")
    ap.add_argument("--safe", default="GOLDBEES.NS")
    args = ap.parse_args(argv)

    cfg = settings.load()
    documents, demo = load_documents(args.documents)
    log = calls_mod.CallLog()
    caller = judge.StubCaller()

    anthropic = judge.AnthropicCaller()
    lines = ["Phase 7: the AI layer", "",
             "Caller: %s%s" % (caller.name,
                               "" if not anthropic.configured()
                               else " (a model key IS configured; the stub is "
                                    "still used until the layer is enabled)"),
             "Documents: %d%s" % (len(documents),
                                  "  *** SYNTHETIC DEMO DOCUMENTS ***" if demo else ""),
             ""]
    if demo:
        lines += ["  No real documents were supplied, so these are invented ones",
                  "  that exercise the pipeline. They say nothing about any real",
                  "  policy decision. Put real statements in %s to replay them."
                  % args.documents.name, ""]

    bars = _bars(args.source, [args.engine, args.safe])
    if args.engine not in bars:
        print("\n".join(lines + ["No bars for %s." % args.engine]), file=sys.stderr)
        return 3

    start, end = cfg["train_window_daily"]

    # ------------------------------------------------- the two replays
    runs = {}
    for masked in (False, True):
        runs[masked] = replay.run_once(
            bars, args.engine, args.safe, cfg["swing_capital_inr"],
            documents, caller, masked, log, start, end)

    lines += ["The replay, run twice   (%s)" % replay.LABEL, "-" * 70,
              "  %-24s %10s %10s %10s" % ("", "RETURN", "MAX DD", "RET/DD")]
    base = runs[True].get("core") or {}
    lines.append("  %-24s %9s%% %9s%% %10s"
                 % ("core alone", base.get("return_pct"),
                    base.get("max_drawdown_pct"),
                    base.get("return_over_drawdown")))
    for masked in (False, True):
        r = runs[masked]
        w = r.get("with_ai") or {}
        lines.append("  %-24s %9s%% %9s%% %10s"
                     % ("+ AI, dates visible" if not masked else "+ AI, dates MASKED",
                        w.get("return_pct"), w.get("max_drawdown_pct"),
                        w.get("return_over_drawdown")))
    lines.append("")
    lines.append("  Only the masked run counts. With dates visible the model may be")
    lines.append("  recalling what the market did next rather than reading the text.")
    lines.append("")

    for masked in (False, True):
        r = runs[masked]
        tag = "masked" if masked else "visible"
        for d in r.get("decisions", []):
            lines.append("    %-8s %s  %-15s %s"
                         % (tag, d["effective_from"], d["call"], d["reason"][:44]))
        for s in r.get("skipped", []):
            lines.append("    %-8s %s  skipped: %s"
                         % (tag, s.get("document_date"), s["why"]))
    lines.append("")

    # ------------------------------------------------- masking proof
    sample = documents[-1]["text"]
    leaked = mask_mod.leaks(mask_mod.mask(sample))
    lines += ["Masking", "-" * 70,
              "  anything left that could date the document: %s"
              % (", ".join(leaked) if leaked else "nothing"), ""]

    # ------------------------------------------------- the enable gate
    verdict = replay.may_enable(runs[True])
    lines += ["May the layer be turned on?", "-" * 70,
              "  masked replay beat core alone: %s" % verdict["masked_beat_core"],
              "  ENABLE: %s" % ("yes" if verdict["enable"] else "NO")]
    for reason in verdict["reasons"]:
        lines.append("    - %s" % reason)
    lines.append("")
    lines.append("  Recorded calls on disk: %d (append only)" % len(log.all()))

    values = {
        "documents": len(documents), "demo_documents": demo,
        "caller": caller.name,
        "masked_beat_core": verdict["masked_beat_core"],
        "enable": verdict["enable"],
        "reasons": len(verdict["reasons"]),
        "hard_rules_clean": verdict["preconditions"]["hard_rules_clean"],
        "core_passed": verdict["preconditions"]["core_passed_its_gates"],
        "mask_leaks": len(leaked),
        "calls_visible": len(runs[False].get("decisions", [])),
        "calls_masked": len(runs[True].get("decisions", [])),
    }
    text = "\n".join(lines)
    print(text)
    sha = fp.fingerprint(values)
    print(fp.line(7, values))

    try:
        import research_store
        research_store.save("7", sha, {
            "fingerprint": sha, "label": replay.LABEL,
            "demo_documents": demo, "documents": len(documents),
            "caller": caller.name,
            "core": base,
            "visible": (runs[False].get("with_ai") or {}),
            "masked": (runs[True].get("with_ai") or {}),
            "decisions": runs[True].get("decisions", []),
            "skipped": runs[True].get("skipped", []),
            "verdict": verdict, "mask_leaks": leaked,
            "recorded_calls": len(log.all()),
        })
    except Exception as exc:
        print("[warn] could not store the AI check: %s" % exc, file=sys.stderr)

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("## Phase 7: the AI layer\n\n```\n%s\n```\n" % text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
