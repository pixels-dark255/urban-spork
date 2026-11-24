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
    {"role": "Regime",
     "modules": ["signals/regime.py"],
     "note": "Trend and volatility per bar. One definition, shared with "
             "asset_study.py so a filter cannot disagree with the study."},
    {"role": "Rule spec",
     "modules": ["rules.yaml", "signals/rules.py"],
     "note": "The 7-part schema. Wraps arena/strategies.py; a test refuses "
             "to let the two drift apart."},
    {"role": "Settings",
     "modules": ["settings.py", "AGENTS.md"],
     "note": "Parsed from AGENTS.md §2. No phase hardcodes a number."},
    {"role": "History archive",
     "modules": ["bar_archive.py", "archive_bars.py",
                 ".github/workflows/bar-archive.yml"],
     "note": "Appends intraday bars to Postgres nightly, because Yahoo's "
             "60-day intraday window slides."},
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
    {"id": "regime-wired", "rule": "Strategies filter on the regime",
     "status": GAP,
     "detail": "signals/regime.py computes trend and volatility, but no "
               "strategy consults it: every filter in rules.yaml is "
               "`none`, and the engine never asks."},
    {"id": "half-size-volatile", "rule": "Half size on volatile bars",
     "status": GAP,
     "detail": "regime.size_multiple is written and tested, and every rule "
               "declares it, but engine sizing does not call it yet."},
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


# Which part of the system runs on which bar size. The Arena is the one
# that matters here: it trades 5-minute bars exclusively, so under the
# settings block every Arena result is UNPROVEN until enough history is
# stored. That is uncomfortable and it is the point - it is the difference
# between a result and a result you can rely on.
USED_BY = {"5m": "Strategy Arena", "1d": "Watchlist backtest"}

UNPROVEN_LABEL = "UNPROVEN (insufficient history)"


def bars() -> list[dict]:
    """Per bar size: is a result on it provable yet, and why not."""
    import settings

    cfg = settings.load()
    try:
        import bar_archive

        cover = bar_archive.coverage()
    except Exception:
        # No database configured, or it is unreachable. The eligibility
        # rules still apply; only the stored-months figure is unknown.
        cover = {}
    try:
        import bar_archive

        verdicts = bar_archive.eligible(cfg, cover)
    except Exception:
        verdicts = {}

    out = []
    for bar_size in cfg["bar_sizes"]:
        verdict = verdicts.get(bar_size, {})
        unproven = settings.is_unproven(cfg, bar_size) and not verdict.get("eligible")
        out.append({
            "bar_size": bar_size,
            "unproven": unproven,
            "label": UNPROVEN_LABEL if unproven else "proven",
            "months": verdict.get("months"),
            "why": verdict.get("why", ""),
            "used_by": USED_BY.get(bar_size, ""),
        })
    return out


def settings_summary() -> dict:
    import settings

    cfg = settings.load()
    train, locked = cfg["train_window_daily"], cfg["locked_window_daily"]
    return {
        "market": cfg["market"],
        "session": "%s-%s IST, square-off %s"
                   % (cfg["session_open"], cfg["session_close"], cfg["square_off"]),
        "daily_capital_inr": cfg["daily_capital_inr"],
        "swing_capital_inr": cfg["swing_capital_inr"],
        "train_window": "%s .. %s" % (train[0], train[1]),
        "locked_window": "%s .. %s" % (locked[0], locked[1]),
        "kill_line_multiple": cfg["kill_line_multiple"],
        "study_tickers": cfg["study_tickers"],
        "engine_candidates": cfg["engine_candidates"],
        "safe_candidates": cfg["safe_candidates"],
    }


def rule_spec() -> dict:
    """The 7-part strategy table, for the dashboard."""
    from signals import rules as rules_mod

    doc = rules_mod.load()
    return {
        "strategies": [
            {"id": r["id"], "name": r["name"], "testable": bool(r["testable"]),
             "why": r.get("not_testable_because", ""),
             "setup": r["setup"], "filter": str(r["filter"]),
             "entry": r["entry"], "exit": r["exit"],
             "stop": r["stop"]["rule"] if isinstance(r["stop"], dict) else str(r["stop"]),
             "stop_pct": r["stop"].get("pct") if isinstance(r["stop"], dict) else None,
             "size": r["size"]}
            for r in doc["strategies"]],
        "testable": len(rules_mod.testable(doc)),
        "not_testable": len(rules_mod.not_testable(doc)),
    }


def view() -> dict:
    out = {"modules": MODULES, "rules": RULES, "compliance": compliance()}
    try:
        out["rule_spec"] = rule_spec()
    except Exception as exc:
        out["rule_spec_error"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        out["settings"] = settings_summary()
        out["bars"] = bars()
    except Exception as exc:
        # The panel is diagnostics; a broken settings block must not take
        # the dashboard down with it. It says so instead.
        out["settings_error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


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
