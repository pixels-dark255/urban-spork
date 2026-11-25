"""CHECK for Phase 5: core alone against core plus sleeves."""
import argparse
import datetime as dt
import os
import sys

import numpy as np
import pandas as pd
import yaml

import fingerprint as fp
import research_store
import settings
from funnel import run as funnel_run
from signals import regime

from . import core, picker, sleeves

BENCH = funnel_run.BENCH_YAML


def load_daily(source: str, tickers: list[str], years: int = 6) -> dict:
    if source == "synthetic":
        rng = np.random.default_rng(20260325)
        idx = pd.bdate_range(dt.date.today() - dt.timedelta(days=int(365.25 * years)),
                             dt.date.today())
        out = {}
        for i, ticker in enumerate(sorted(tickers)):
            drift = 0.0006 - 0.0002 * i
            vol = 0.004 + 0.003 * (i % 3)
            v = 100 * np.cumprod(1 + rng.normal(drift, vol, len(idx)))
            out[ticker] = pd.DataFrame(
                {"Open": v, "High": v * 1.004, "Low": v * 0.996, "Close": v,
                 "Volume": 100000.0}, index=idx)
        return out

    import data_sources
    out = {}
    for ticker in sorted(tickers):
        frame = data_sources.fetch_daily_history(ticker, period="%dy" % years,
                                                 timeout=45.0)
        if frame is not None and not frame.empty and "Close" in frame.columns:
            out[ticker] = frame
    return out


def survivors() -> list[dict]:
    if not BENCH.exists():
        return []
    doc = yaml.safe_load(BENCH.read_text()) or {}
    return doc.get("survivors") or []


def _vol_state(frame: pd.DataFrame) -> pd.Series:
    return regime.classify(frame)["vol"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=["synthetic", "yahoo"], default="synthetic")
    ap.add_argument("--locked", action="store_true",
                    help="run the locked window. ONCE (AGENTS.md §1.6)")
    ap.add_argument("--no-store", action="store_true")
    args = ap.parse_args(argv)

    if args.locked and args.source != "yahoo":
        print("[error] the locked window may be run once and must not be "
              "spent on synthetic bars.", file=sys.stderr)
        return 2

    cfg = settings.load()
    engines, safes = cfg["engine_candidates"], cfg["safe_candidates"]
    capital = cfg["swing_capital_inr"]
    train_start, train_end = cfg["train_window_daily"]
    locked_start, locked_end = cfg["locked_window_daily"]

    wanted = sorted(set(engines) | {s for s in safes if s != core.CASH})
    bars = load_daily(args.source, wanted)
    missing = [t for t in wanted if t not in bars]

    lines = ["Phase 5: core + sleeves (swing account)", "",
             "Capital \u20b9{:,.0f}, delivery costs, next-open fills.".format(capital), ""]
    if missing:
        lines += ["UNRESOLVED (not dropped silently): %s" % ", ".join(missing), ""]
    if not bars:
        print("\n".join(lines + ["No daily bars at all."]), file=sys.stderr)
        return 3

    # ------------------------------------------------- the pick
    chosen = picker.pick(bars, train_end, engines, safes)
    if not chosen["ok"]:
        print("\n".join(lines + [chosen["reason"]]), file=sys.stderr)
        return 4
    engine, safe = chosen["engine"], chosen["safe"]
    lines += ["Pick as of %s" % train_end, "-" * 70]
    for row in chosen["engines"]:
        gap = row["trend_gap"]
        lines.append("  engine  %-16s trend gap %s%s"
                     % (row["ticker"],
                        "n/a" if gap is None else "%+.4f%%" % (gap * 100),
                        "   <- chosen" if row["ticker"] == engine else ""))
    for row in chosen["safes"]:
        lines.append("  safe    %-16s %s%s"
                     % (row["ticker"], row["why"],
                        "   <- chosen" if row["ticker"] == safe else ""))
    if safe == core.CASH:
        lines.append("  safe    CASH             no candidate qualified; holding rupees")
    lines.append("")

    # ------------------------------------------------- core, neighbours, 2x
    base = core.run(bars, engine, safe, capital, core.BASE_RULE,
                    start=train_start, end=train_end)
    if not base.get("ok"):
        print("\n".join(lines + [base["reason"]]), file=sys.stderr)
        return 5

    neighbours = []
    for rule in core.NEIGHBOUR_RULES:
        result = core.run(bars, engine, safe, capital, rule,
                          start=train_start, end=train_end)
        neighbours.append({"rule": getattr(rule, "label", "?"),
                           "ok": bool(result.get("ok")),
                           "return_pct": result.get("return_pct"),
                           "max_drawdown_pct": result.get("max_drawdown_pct")})
    losers = [n for n in neighbours if not n["ok"] or (n["return_pct"] or 0) <= 0]

    doubled = core.run(bars, engine, safe, capital, core.BASE_RULE,
                       cost_multiple=2.0, start=train_start, end=train_end)

    lines += ["Core rotation, training window %s .. %s" % (train_start, train_end),
              "-" * 70,
              "  %-14s return %+8.2f%%  CAGR %+7.2f%%  maxDD %7.2f%%  r/dd %s  switches %d"
              % (base["rule"], base["return_pct"], base["cagr_pct"],
                 base["max_drawdown_pct"],
                 "n/a" if base["return_over_drawdown"] is None else "%.2f" % base["return_over_drawdown"],
                 base["switches"]),
              ""]
    lines.append("  Neighbours (all must be profitable)")
    for n in neighbours:
        lines.append("    %-14s %s" % (n["rule"],
                     "n/a" if n["return_pct"] is None else "%+8.2f%%" % n["return_pct"]))
    lines.append("    verdict: %s" % ("PASS" if not losers else
                 "FAIL - %s" % ", ".join(n["rule"] for n in losers)))
    lines += ["", "  2x cost: %s"
              % ("n/a" if not doubled.get("ok") else "%+.2f%% (%s)"
                 % (doubled["return_pct"],
                    "PASS" if doubled["return_pct"] > 0 else "FAIL")), ""]

    if base["could_not_buy"]:
        lines.append("  Slots that could not buy one share: %d (first: %r)"
                     % (len(base["could_not_buy"]), base["could_not_buy"][0]))
        lines.append("")

    # ------------------------------------------------- sleeves
    bench = survivors()
    vol = _vol_state(bars[engine])
    sleeve_result = sleeves.select(base["equity"], [], capital, vol)
    lines += ["Sleeves", "-" * 70]
    if not bench:
        lines.append("  bench.yaml has no survivors, so there is nothing to add.")
        lines.append("  The account is the core alone. This is Phase 4's result,")
        lines.append("  not a failure here: no idea has earned a slot.")
    else:
        lines.append("  %d survivor(s) available; %d chosen."
                     % (len(bench), len(sleeve_result["chosen"])))
    lines.append("")

    # ------------------------------------------------- causality + quarters
    causal = picker.verify_causal(bars, train_end, engines, safes)
    quarters = picker.backtest_picks(bars, engines, safes,
                                     train_start, min(locked_end, dt.date.today()))
    lines += ["Quarterly re-pick", "-" * 70,
              "  look-ahead check at %s: full %s, truncated %s -> %s"
              % (causal["as_of"], causal["full"], causal["truncated"],
                 "MATCH" if causal["match"] else "MISMATCH"),
              "  %d quarter ends backtested" % len(quarters)]
    for row in quarters[-8:]:
        lines.append("    %s  engine %-14s safe %s"
                     % (row["as_of"], row["engine"] or "-", row["safe"] or "-"))
    lines.append("")

    # ------------------------------------------------- locked window
    locked = None
    if args.locked:
        locked = core.run(bars, engine, safe, capital, core.BASE_RULE,
                          start=locked_start, end=locked_end)
        funnel_run.log_locked(
            "swing_core_%s_%s" % (engine, safe), 
            {"ok": locked.get("ok"), "net": (locked.get("final", capital) - capital),
             "trades": locked.get("switches", 0)},
            note="core rotation %s/%s, %s" % (engine, safe, base["rule"]))

    lines += ["CHECK: core alone vs full account", "-" * 70,
              "  %-22s %10s %10s %10s" % ("", "RETURN/YR", "MAX DD", "RET/DD")]
    for label, result in (("core alone", base),
                          ("core + sleeves", sleeve_result if bench else base)):
        lines.append("  %-22s %9.2f%% %9.2f%% %10s"
                     % (label, result.get("cagr_pct", 0.0),
                        result.get("max_drawdown_pct", 0.0),
                        "n/a" if result.get("return_over_drawdown") is None
                        else "%.2f" % result["return_over_drawdown"]))
    if locked and locked.get("ok"):
        lines.append("  %-22s %9.2f%% %9.2f%% %10s   (locked window %s .. %s)"
                     % ("locked, core alone", locked["cagr_pct"],
                        locked["max_drawdown_pct"],
                        "n/a" if locked["return_over_drawdown"] is None
                        else "%.2f" % locked["return_over_drawdown"],
                        locked_start, locked_end))
    elif args.locked:
        lines.append("  locked window: %s" % (locked or {}).get("reason", "no result"))
    else:
        lines.append("  locked window: not run (once per idea; --locked with real bars)")

    values = {
        "engine": engine, "safe": safe, "source": args.source,
        "rule": base["rule"], "switches": base["switches"],
        "core_return": base["return_pct"], "core_cagr": base["cagr_pct"],
        "core_maxdd": base["max_drawdown_pct"],
        "core_r_over_dd": base["return_over_drawdown"],
        "neighbours_failed": len(losers),
        "double_cost_return": doubled.get("return_pct"),
        "sleeves": len(sleeve_result["chosen"]),
        "survivors_available": len(bench),
        "causal": causal["match"], "quarters": len(quarters),
        "unresolved": ",".join(missing) or "none",
    }
    if locked and locked.get("ok"):
        values["locked_cagr"] = locked["cagr_pct"]
        values["locked_maxdd"] = locked["max_drawdown_pct"]

    text = "\n".join(lines)
    print(text)
    sha = fp.fingerprint(values)
    print()
    print(fp.line(5, values))

    if not args.no_store:
        payload = {
            "fingerprint": sha, "source": args.source,
            "pick": chosen, "core": _plain(base), "doubled": _plain(doubled),
            "neighbours": neighbours, "quarters": quarters, "causal": causal,
            "sleeves": {"chosen": sleeve_result["chosen"],
                        "rejected": sleeve_result["rejected"],
                        "history": sleeve_result["history"],
                        "survivors_available": len(bench)},
            "equity": _curve(base["equity"]),
            "holdings": base.get("holdings", []),
            "engine": engine, "safe": safe,
            "train_window": [str(train_start), str(train_end)],
            "account_equity": _curve(sleeve_result["equity"]) if bench else _curve(base["equity"]),
            "locked": _plain(locked) if locked else None,
            "capital": capital, "unresolved": missing,
        }
        try:
            research_store.save("5", sha, payload)
        except Exception as exc:
            print("[warn] could not store: %s" % exc, file=sys.stderr)

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("## Phase 5: core + sleeves\n\n```\n%s\n```\n" % text)
    return 0


def _plain(result: dict | None) -> dict:
    if not result:
        return {}
    return {k: v for k, v in result.items() if not isinstance(v, pd.Series)}


def _curve(series: pd.Series, points: int = 400) -> list:
    """Thinned for the dashboard: a chart does not need every session."""
    if series is None or series.empty:
        return []
    step = max(1, len(series) // points)
    thin = series.iloc[::step]
    return [[str(pd.Timestamp(d).date()), round(float(v), 2)]
            for d, v in thin.items()]


if __name__ == "__main__":
    sys.exit(main())
