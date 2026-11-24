"""What this system is made of, and where it does not yet obey its own rules.

Two jobs. It answers "which module does X?" so that nobody writes a second
copy of something that already exists, and it states plainly which of the
hard rules in AGENTS.md are not met yet.

The gaps are checked against the real code by tests/test_system_map.py, so
fixing one makes a test fail until this file is updated too. A compliance
table that can drift out of date is worse than none: it gets believed.
"""

# --------------------------------------------------------------- the map

MODULES = [
    {"role": "Data download",
     "modules": ["data_sources.py", "arena/universe.py"],
     "note": "Every external fetch. Nothing else calls yfinance directly."},
    {"role": "Indicators",
     "modules": ["indicators.py"],
     "note": "SMA, EMA, RSI, MACD, Bollinger, volatility. Pure pandas."},
    {"role": "Signals",
     "modules": ["arena/strategies.py"],
     "note": "The one signal module. Backtest and live runner both import it."},
    {"role": "Cost model",
     "modules": ["arena/costs.py", "arena/markets.py"],
     "note": "Indian legs: brokerage, STT, exchange, SEBI, GST, stamp, slippage."},
    {"role": "Paper fills",
     "modules": ["arena/engine.py"],
     "note": "Sizing, entries, exits, square-off. No live orders anywhere."},
    {"role": "Backtest",
     "modules": ["arena/engine.py::replay"],
     "note": "Replays the live code path. backtest.py is the watchlist "
             "predictor, a different thing."},
    {"role": "Persistence",
     "modules": ["arena/store.py", "storage.py"],
     "note": "Postgres, with a JSON file fallback the runner refuses."},
    {"role": "Scheduler",
     "modules": [".github/workflows/arena-runner.yml", "run_daily.py",
                 "scheduler.py", "arena/runner.py"],
     "note": "The scheduled job advances the arena; ARENA_RUNNER picks which."},
    {"role": "Market calendar",
     "modules": ["market_calendar.py", "nse_holidays.json", "timeutil.py"],
     "note": "One source of truth for sessions, holidays and time."},
    {"role": "Dashboard",
     "modules": ["frontend/arena.js", "frontend/app.js"],
     "note": "View only. It never advances a simulation."},
]

# ------------------------------------------------------------- the rules

OK = "ok"
GAP = "gap"

RULES = [
    {"id": "paper-only", "rule": "Paper trading only", "status": OK,
     "detail": "No broker-order module exists; nothing can place an order."},
    {"id": "one-signal-module", "rule": "Backtest and live share signal code",
     "status": OK,
     "detail": "engine.replay() drives the same start_day / process_bars / "
               "settle_day path the live runner uses."},
    {"id": "next-open-fills", "rule": "Fill on the next bar's open",
     "status": GAP,
     "detail": "Entries fill at the signal bar's close, so every result "
               "carries up to one bar of look-ahead and reads optimistic."},
    {"id": "costs-on", "rule": "Costs always on", "status": OK,
     "detail": "Every fill pays the full Indian cost model plus slippage."},
    {"id": "double-cost", "rule": "Every result also reported at 2x cost",
     "status": GAP,
     "detail": "The slippage sweep runs 0x, 0.5x and 1x. No 2x case."},
    {"id": "determinism", "rule": "Deterministic output with a fingerprint",
     "status": GAP,
     "detail": "fingerprint.py exists and Phase 0 prints one, but the "
               "simulation declares no seed and emits no fingerprint."},
]


def compliance() -> dict:
    """Counts for the dashboard header."""
    return {"ok": sum(1 for r in RULES if r["status"] == OK),
            "gap": sum(1 for r in RULES if r["status"] == GAP),
            "total": len(RULES)}


def view() -> dict:
    return {"modules": MODULES, "rules": RULES, "compliance": compliance()}


def fingerprint_values() -> dict:
    """The numbers that define this phase's result."""
    values = {"modules": len(MODULES), "rules": len(RULES)}
    values.update(compliance())
    for rule in RULES:                      # sorted inside fingerprint()
        values["rule:" + rule["id"]] = rule["status"]
    return values


if __name__ == "__main__":
    import fingerprint as _fp

    for entry in MODULES:
        print("%-16s %s" % (entry["role"], " ".join(entry["modules"])))
    print()
    for rule in RULES:
        print("%-4s %-42s %s" % (rule["status"].upper(), rule["rule"], rule["detail"]))
    print()
    print(_fp.line(0, fingerprint_values()))
