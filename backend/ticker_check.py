"""Does every ticker in the settings block actually resolve?

A ticker that silently returns nothing is the worst kind of input error:
the run still finishes, the table still has rows, and one instrument is
just quietly missing from every number. So this reports every symbol it
could not resolve and never drops one on the floor.

Fetching goes through data_sources, which is the only module allowed to
talk to Yahoo (AGENTS.md §3).
"""
import datetime

import pandas as pd

import data_sources
import settings

# Enough history to tell "this symbol exists" from "Yahoo had a bad second".
PROBE_PERIOD = "1mo"
MIN_BARS = 3

# Every probe is bounded. An unanswered request is indistinguishable from a
# slow one until it has already cost the whole job: the first archive run
# sat on a single symbol for fifteen minutes and was killed by the step
# timeout, having checked nothing.
PROBE_TIMEOUT = 20.0


def probe(ticker: str) -> dict:
    """One symbol's result: resolved, bars seen, last close, and why not."""
    row = {"ticker": ticker, "ok": False, "bars": 0,
           "last_close": None, "last_date": None, "reason": ""}
    try:
        frame = data_sources.fetch_daily_history(
            ticker, period=PROBE_PERIOD, timeout=PROBE_TIMEOUT)
    except Exception as exc:                      # network, parse, anything
        row["reason"] = "%s: %s" % (type(exc).__name__, exc)
        return row
    if frame is None or frame.empty:
        row["reason"] = "no data returned"
        return row
    if "Close" not in frame.columns:
        row["reason"] = "no Close column (columns: %s)" % list(frame.columns)[:4]
        return row
    closes = frame["Close"].dropna()
    row["bars"] = int(len(closes))
    if closes.empty:
        row["reason"] = "every close was blank"
        return row
    if row["bars"] < MIN_BARS:
        row["reason"] = "only %d bar(s) in %s - too thin to trust" % (
            row["bars"], PROBE_PERIOD)
        return row
    last = closes.index[-1]
    row["last_close"] = float(closes.iloc[-1])
    row["last_date"] = (last.date().isoformat()
                        if isinstance(last, pd.Timestamp) else str(last))
    row["ok"] = True
    return row


def check(cfg: dict | None = None, probe_fn=probe) -> dict:
    """Probe every ticker the settings block names, in sorted order."""
    cfg = cfg or settings.load()
    rows = [probe_fn(t) for t in settings.all_tickers(cfg)]
    unresolved = [r for r in rows if not r["ok"]]
    return {
        "checked_at": datetime.date.today().isoformat(),
        "rows": rows,
        "resolved": [r["ticker"] for r in rows if r["ok"]],
        "unresolved": [r["ticker"] for r in unresolved],
        "reasons": {r["ticker"]: r["reason"] for r in unresolved},
        "excluded": [settings.CASH] if settings.CASH in
                    cfg["safe_candidates"] else [],
    }


def report(result: dict) -> str:
    """The CHECK block a human reads."""
    lines = ["Tickers (%d checked, %d unresolved)"
             % (len(result["rows"]), len(result["unresolved"]))]
    for r in result["rows"]:
        if r["ok"]:
            lines.append("  OK   %-16s %5d bars  last %s @ %.2f"
                         % (r["ticker"], r["bars"], r["last_date"], r["last_close"]))
        else:
            lines.append("  FAIL %-16s %s" % (r["ticker"], r["reason"]))
    for name in result["excluded"]:
        lines.append("  --   %-16s not a ticker: holding rupees" % name)
    if result["unresolved"]:
        lines.append("")
        lines.append("UNRESOLVED: %s" % ", ".join(result["unresolved"]))
        lines.append("These are NOT dropped. Fix the symbol or remove it from "
                     "the settings block in AGENTS.md before relying on a run.")
    return "\n".join(lines)
