"""Phase 1 runner: fetch daily bars, label every study ticker, store it.

Run from CI, where the data source is reachable. Prints the CHECK block
(a table of tickers against labels) and the phase fingerprint.
"""
import argparse
import datetime
import os
import sys

import asset_study
import data_sources
import fingerprint as fp
import research_store
import settings

PHASE = "1"
EXIT_NO_DATA = 3

# Daily bars back far enough to cover the training window plus the 200-day
# warm-up the labels need before the window even opens.
FETCH_PERIOD = "5y"


def fetch(tickers: list[str], period: str = FETCH_PERIOD) -> tuple[dict, dict]:
    frames, failed = {}, {}
    for ticker in sorted(tickers):
        try:
            frame = data_sources.fetch_daily_history(ticker, period=period)
        except Exception as exc:
            failed[ticker] = "%s: %s" % (type(exc).__name__, exc)
            continue
        if frame is None or frame.empty or "Close" not in frame.columns:
            failed[ticker] = "no data returned"
            continue
        frames[ticker] = frame
    return frames, failed


def slice_window(frames: dict, start, end, warmup_days: int = 420) -> dict:
    """Cut to the training window, keeping a warm-up tail before it.

    The 200-day average and the 50-day high must be computed from real bars
    before the window opens, or the first year of the window has no usable
    state and the labels quietly lose a third of their events.
    """
    out = {}
    begin = start - datetime.timedelta(days=warmup_days)
    for name, frame in frames.items():
        idx = frame.index
        local = idx.tz_convert(None) if getattr(idx, "tz", None) else idx
        mask = (local.date >= begin) & (local.date <= end)
        cut = frame[mask]
        if not cut.empty:
            out[name] = cut
    return out


def table(result: dict) -> str:
    """The CHECK block: tickers against labels."""
    rows = result["rows"]
    if not rows:
        return "No tickers could be studied."
    head = "%-16s %-12s %-12s %-6s %9s %9s %7s" % (
        "TICKER", "KEEPS GOING", "SNAPS BACK", "SAFE", "TREND GAP",
        "CALM/VOL", "CORR")
    lines = [head, "-" * len(head)]
    for row in rows:
        gap = row["trend_gap"]["gap"]
        ratio = row["calm_vs_volatile"]["ratio"]
        corr = row["safety"]["max_correlation"]
        lines.append("%-16s %-12s %-12s %-6s %9s %9s %7s" % (
            row["ticker"],
            "yes (%d)" % row["momentum"]["events"] if row["momentum"]["labelled"]
            else "no (%d)" % row["momentum"]["events"],
            "yes (%d)" % row["reversion"]["events"] if row["reversion"]["labelled"]
            else "no (%d)" % row["reversion"]["events"],
            "yes" if row["safety"]["labelled"] else "no",
            "n/a" if gap is None else "%+.3f%%" % (gap * 100),
            "n/a" if ratio is None else "%.2fx" % ratio,
            "n/a" if corr is None else "%.2f" % corr))
    if result["missing"]:
        lines.append("")
        lines.append("MISSING (no data, not dropped silently): %s"
                     % ", ".join(result["missing"]))
    lines.append("")
    lines.append("In plain English:")
    for row in rows:
        lines.append("  " + row["summary"])
    return "\n".join(lines)


def summary_markdown(result: dict, cfg: dict) -> str:
    out = ["## Phase 1: asset study", "",
           "Training window %s .. %s, daily bars."
           % (cfg["train_window_daily"][0], cfg["train_window_daily"][1]), "",
           "| Ticker | Keeps going | Snaps back | Safe | Trend gap | Calm/vol | Corr |",
           "| --- | --- | --- | --- | ---: | ---: | ---: |"]
    for row in result["rows"]:
        gap = row["trend_gap"]["gap"]
        ratio = row["calm_vs_volatile"]["ratio"]
        corr = row["safety"]["max_correlation"]
        out.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            row["ticker"],
            "**yes** (%d)" % row["momentum"]["events"] if row["momentum"]["labelled"]
            else "no (%d)" % row["momentum"]["events"],
            "**yes** (%d)" % row["reversion"]["events"] if row["reversion"]["labelled"]
            else "no (%d)" % row["reversion"]["events"],
            "**yes**" if row["safety"]["labelled"] else "no",
            "n/a" if gap is None else "%+.3f%%" % (gap * 100),
            "n/a" if ratio is None else "%.2fx" % ratio,
            "n/a" if corr is None else "%.2f" % corr))
    if result["missing"]:
        out += ["", "**Missing:** %s" % ", ".join(result["missing"])]
    out += ["", "### In plain English", ""]
    out += ["- " + row["summary"] for row in result["rows"]]
    return "\n".join(out)


def write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--period", default=FETCH_PERIOD)
    ap.add_argument("--no-store", action="store_true",
                    help="compute and print without writing the result")
    args = ap.parse_args(argv)

    cfg = settings.load()
    start, end = cfg["train_window_daily"]
    wanted = sorted(set(cfg["study_tickers"]) | set(cfg["engine_candidates"]))

    print("Phase 1: asset study")
    print("Training window %s .. %s, %d tickers" % (start, end, len(wanted)))
    print()

    frames, failed = fetch(wanted, args.period)
    if failed:
        print("Could not fetch:")
        for name, why in sorted(failed.items()):
            print("  %-16s %s" % (name, why))
        print()
    if not frames:
        print("[error] no daily bars at all", file=sys.stderr)
        write_summary("## Phase 1\n\n**No daily bars could be fetched.**\n")
        return EXIT_NO_DATA

    # Warm-up bars are kept so the 200-day average exists on day one of
    # the window; asset_study.study cuts them away after computing, and
    # supplies nothing at all after `end` so the locked window stays sealed.
    windowed = slice_window(frames, start, end)
    result = asset_study.study(windowed, cfg["study_tickers"],
                               cfg["engine_candidates"], window=(start, end))
    result["window"] = {"start": start.isoformat(), "end": end.isoformat()}
    result["fetch_failed"] = failed

    text = table(result)
    print(text)

    values = asset_study.fingerprint_values(result)
    values["window"] = "%s..%s" % (start, end)
    sha = fp.fingerprint(values)
    result["fingerprint"] = sha

    if not args.no_store:
        where = research_store.save(PHASE, sha, result)
        print()
        print("Stored to %s" % where)

    write_summary(summary_markdown(result, cfg))
    write_summary("\n`%s`\n" % fp.line(PHASE, values))
    print()
    print(fp.line(PHASE, values))
    return 0


if __name__ == "__main__":
    sys.exit(main())
