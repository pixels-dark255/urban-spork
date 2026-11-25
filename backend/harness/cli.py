"""CHECK for Phase 6: a dry run, the three risk limits, and double-run safety."""
import argparse
import copy
import datetime as dt
import os
import sys

import pandas as pd
import yaml

import fingerprint as fp
import settings
from funnel import run as funnel_run

from . import alerts as alerts_mod, risk, runner, store as store_mod

IST = runner.IST


def _bars_for(tickers, session: dt.date, source: str) -> dict:
    """Current-session bars. Synthetic ones are stamped with today's date,
    because the point of step 4 is to catch a stale stamp."""
    if source == "synthetic":
        idx = pd.DatetimeIndex([dt.datetime.combine(session, dt.time(15, 0))])
        return {t: pd.DataFrame({"Open": [100.0], "High": [101.0], "Low": [99.0],
                                 "Close": [100.5], "Volume": [1000.0]}, index=idx)
                for t in sorted(tickers)}
    import data_sources
    out = {}
    for ticker in sorted(tickers):
        frame = data_sources._yf_download(ticker, "5d", "5m", timeout=45.0)
        if frame is not None and not frame.empty:
            out[ticker] = frame
    return out


def plan_from_bench() -> tuple:
    """Target orders. With nothing in bench.yaml there is nothing to buy."""
    if not funnel_run.BENCH_YAML.exists():
        return [], "bench.yaml does not exist yet"
    doc = yaml.safe_load(funnel_run.BENCH_YAML.read_text()) or {}
    survivors = doc.get("survivors") or []
    if not survivors:
        return [], ("no idea has earned capital: bench.yaml has no survivors, "
                    "so the harness plans nothing and holds cash")
    orders = []
    for row in survivors[:3]:
        for ticker in (row.get("symbols") or [])[:1]:
            orders.append({"ticker": ticker, "strategy": row["id"],
                           "side": "BUY", "qty": 1, "target_qty": 1})
    return orders, "%d survivor(s) in bench.yaml" % len(survivors)


def _limit_demo(label, orders, ctx_kwargs, peak, expect) -> dict:
    s = store_mod.MemoryStore()
    ctx = risk.Context(account="swing", positions=[],
                       now_ist=dt.datetime(2026, 11, 25, 10, 0, tzinfo=IST),
                       **ctx_kwargs)
    out = risk.apply(copy.deepcopy(orders), ctx, peak, s)
    return {"limit": label, "orders_in": len(orders),
            "orders_out": len(out["orders"]),
            "rules": [a["rule"] for a in out["actions"]],
            "paused": out["paused"], "expected": expect,
            "qty_out": [o["qty"] for o in out["orders"]],
            "violations": risk.check_invariant(orders, out["orders"])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=["synthetic", "yahoo"], default="synthetic")
    ap.add_argument("--live-store", action="store_true",
                    help="use Postgres instead of an in-memory store")
    ap.add_argument("--no-dry-run", action="store_true",
                    help="write paper fills (still paper only; §1.1)")
    ap.add_argument("--missed-check", action="store_true",
                    help="alert if the end-of-day run has not happened")
    args = ap.parse_args(argv)

    if args.missed_check:
        store = store_mod.open_store()
        sink = alerts_mod.open_sink(store)
        out = runner.check_missed_run(store, sink, dt.datetime.now(IST))
        print("Missed-run check: %s" % out)
        if not sink.configured():
            print("Telegram is not configured, so nothing was sent.")
        return 0

    cfg = settings.load()
    session = dt.datetime.now(IST)
    store = store_mod.open_store() if args.live_store else store_mod.MemoryStore()
    sink = alerts_mod.open_sink(store)

    orders, reason = plan_from_bench()
    tickers = sorted({o["ticker"] for o in orders}) or cfg["engine_candidates"][:1]
    bars = _bars_for(tickers, session.date(), args.source)

    lines = ["Phase 6: the harness", "",
             "Alerts: %s" % ("Telegram configured" if sink.configured()
                             else "not configured, so nothing is sent"),
             "Trust ladder: %s (paper only; live trading is out of scope)"
             % runner.DEFAULT_LADDER,
             "Store: %s" % type(store).__name__, ""]

    # ---------------------------------------------------- the dry run
    ctx = runner.Context(store=store, sink=sink, now_ist=session,
                         dry_run=not args.no_dry_run,
                         equity=cfg["swing_capital_inr"],
                         day_start_equity=cfg["swing_capital_inr"],
                         worst_backtest_drawdown_pct=28.52,
                         peak_equity=cfg["swing_capital_inr"],
                         bars=bars, planned=copy.deepcopy(orders),
                         plan_reason=reason)
    first = runner.run(ctx)

    lines += ["Dry run for %s" % session.date(), "-" * 68]
    for step in first["steps"]:
        mark = "ok" if step["ok"] else ("STOP" if step["stop"] else "warn")
        note = step["detail"].get("why") or step["detail"].get("reason") or ""
        lines.append("  %-13s %-5s %s" % (step["name"], mark, str(note)[:48]))
    lines.append("  stopped at: %s" % (first["stopped_at"] or "nothing; ran to the end"))
    lines.append("  plan: %s" % reason)
    lines.append("")

    # ------------------------------------------- the three risk limits
    demo_orders = [{"ticker": "DEMO.NS", "strategy": "demo", "side": "BUY",
                    "qty": 10, "target_qty": 10},
                   {"ticker": "DEMO.NS", "strategy": "demo", "side": "SELL",
                    "qty": 4}]
    base = {"equity": 100000.0, "day_start_equity": 100000.0,
            "worst_backtest_drawdown_pct": 28.52}
    demos = [
        _limit_demo("none (baseline)", demo_orders, base, 100000.0,
                    "both orders through"),
        _limit_demo("daily loss > 3%", demo_orders,
                    {**base, "equity": 96500.0}, 100000.0,
                    "entries dropped, exit still runs"),
        _limit_demo("daily loss > 5%", demo_orders,
                    {**base, "equity": 94000.0}, 100000.0,
                    "PAUSE set, exits only"),
        _limit_demo("drawdown past worst tested", demo_orders,
                    {**base, "equity": 65000.0, "day_start_equity": 65000.0},
                    100000.0, "new size halved"),
    ]
    lines += ["The risk limits, each toggled on", "-" * 68,
              "  %-28s %-9s %-9s %s" % ("LIMIT", "ORDERS", "QTY", "WHAT CHANGED")]
    for d in demos:
        lines.append("  %-28s %d -> %-4d %-9s %s"
                     % (d["limit"], d["orders_in"], d["orders_out"],
                        ",".join(str(q) for q in d["qty_out"]) or "-",
                        ", ".join(d["rules"]) or "nothing"))
    bad = [d for d in demos if d["violations"]]
    lines.append("  gate may only reduce exposure: %s"
                 % ("PASS" if not bad else "FAIL %s" % bad))
    lines.append("")

    # --------------------------------------------- double-run safety
    twice_store = store_mod.MemoryStore()
    twice_sink = alerts_mod.Sink(twice_store)
    placed = []
    for _ in range(2):
        c = runner.Context(store=twice_store, sink=twice_sink, now_ist=session,
                           dry_run=False, equity=100000.0,
                           day_start_equity=100000.0, bars=bars,
                           planned=[{"ticker": "DEMO.NS", "strategy": "demo",
                                     "side": "BUY", "qty": 1, "target_qty": 1}])
        out = runner.run(c)
        step = next((s for s in out["steps"] if s["name"] == "record"), None)
        placed.append((step or {}).get("detail", {}).get("placed", 0))
    stale_alert_sent = len(twice_sink.sent)
    for _ in range(2):
        twice_sink.send("same-key", "the same alert twice")
    lines += ["Running twice", "-" * 68,
              "  orders placed on run 1: %d, on run 2: %d" % tuple(placed),
              "  the same alert sent twice: %d delivery(ies)"
              % (len(twice_sink.sent) - stale_alert_sent),
              "  verdict: %s" % ("PASS - a rerun creates nothing new"
                                 if placed[1] == 0 and
                                 (len(twice_sink.sent) - stale_alert_sent) == 1
                                 else "FAIL"), ""]

    values = {
        "stopped_at": first["stopped_at"] or "none",
        "steps": len(first["steps"]),
        "plan_orders": len(orders),
        "trust": runner.DEFAULT_LADDER,
        "alerts_configured": sink.configured(),
        "second_run_orders": placed[1],
        "gate_violations": sum(len(d["violations"]) for d in demos),
        "soft_loss_pct": risk.SOFT_LOSS_PCT,
        "hard_loss_pct": risk.HARD_LOSS_PCT,
        "drawdown_multiple": risk.DRAWDOWN_HALVE,
    }
    for d in demos:
        values["limit:%s" % d["limit"]] = ",".join(d["rules"]) or "none"

    text = "\n".join(lines)
    print(text)
    sha = fp.fingerprint(values)
    print(fp.line(6, values))

    try:
        import research_store
        research_store.save("6", sha, {
            "fingerprint": sha,
            "steps": first["steps"], "stopped_at": first["stopped_at"],
            "plan_reason": reason, "limits": demos,
            "trust": runner.DEFAULT_LADDER,
            "alerts_configured": sink.configured(),
            "double_run": {"first": placed[0], "second": placed[1]},
            "soft_loss_pct": risk.SOFT_LOSS_PCT,
            "hard_loss_pct": risk.HARD_LOSS_PCT,
            "ran_at": session.isoformat(),
        })
    except Exception as exc:
        print("[warn] could not store the harness check: %s" % exc, file=sys.stderr)

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("## Phase 6: the harness\n\n```\n%s\n```\n" % text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
