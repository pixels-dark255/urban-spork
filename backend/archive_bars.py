"""Entry point for the scheduled bar-archive job.

Separate from bar_archive.py so the module stays importable and testable
without argument parsing, exactly as run_daily.py is separate from the
engine. Writes a summary to $GITHUB_STEP_SUMMARY when running in Actions.
"""
import argparse
import os
import re
import sys

import bar_archive
import settings

EXIT_NO_DATABASE_URL = 2
EXIT_FETCH_FAILED = 3


def _redact(text) -> str:
    """Never let a connection string reach a log or a run summary."""
    return re.sub(r"(?i)(postgres(?:ql)?://)[^@\s]*@", r"\1<redacted>@", str(text))


def summary(result: dict, cfg: dict) -> str:
    lines = ["## Bar archive", ""]
    written = result["written"]
    lines.append("| Bar size | Rows written | Stored months | In the funnel |")
    lines.append("| --- | ---: | ---: | --- |")
    for bar_size in sorted(set(written) | set(result["eligible"])):
        el = result["eligible"].get(bar_size, {})
        months = el.get("months")
        lines.append("| %s | %s | %s | %s |" % (
            bar_size,
            written.get(bar_size, "-"),
            "-" if months is None else "%.1f" % months,
            "yes" if el.get("eligible") else "no - %s" % el.get("why", "")))
    if result["failed"]:
        lines += ["", "### Symbols that failed", ""]
        for key, why in sorted(result["failed"].items()):
            lines.append("- `%s` %s" % (key, _redact(why)))
    lines += ["", "_Unproven bar sizes: %s. A result on one of these is "
              "labelled UNPROVEN until %d months are stored._"
              % (", ".join(cfg["unproven_bar_sizes"]),
                 cfg["min_months_for_funnel"])]
    return "\n".join(lines)


def write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bar-sizes", default="",
                    help="comma separated; default is every intraday size")
    ap.add_argument("--check-tickers", action="store_true",
                    help="also resolve every ticker in the settings block")
    args = ap.parse_args(argv)

    cfg = settings.load()
    print(settings.report(cfg))
    print()

    failed_tickers = 0
    if args.check_tickers:
        import ticker_check

        result = ticker_check.check(cfg)
        print(ticker_check.report(result))
        print()
        failed_tickers = len(result["unresolved"])
        write_summary("## Tickers\n\n```\n%s\n```\n"
                      % ticker_check.report(result))

    sizes = [s.strip() for s in args.bar_sizes.split(",") if s.strip()] or None
    try:
        result = bar_archive.run(cfg, sizes)
    except bar_archive.NoDatabase as exc:
        message = _redact(exc)
        print("[error] %s" % message, file=sys.stderr)
        write_summary("## Bar archive\n\n**Refused to run.** %s\n" % message)
        return EXIT_NO_DATABASE_URL

    text = summary(result, cfg)
    print(text)
    write_summary(text)

    import fingerprint as fp

    values = settings.fingerprint_values(cfg)
    values["unresolved"] = failed_tickers
    for bar_size, count in sorted(result["written"].items()):
        values["written:" + bar_size] = count
    print()
    print(fp.line(0, values))

    if result["failed"] and not result["written"]:
        return EXIT_FETCH_FAILED
    return 0


if __name__ == "__main__":
    sys.exit(main())
