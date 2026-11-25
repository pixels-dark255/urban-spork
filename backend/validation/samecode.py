"""Does the backtester produce the same signals as the live runner?

AGENTS.md §1.2 says they share one code path, and by inspection they do:
`engine.replay` calls `start_day` / `process_bars` / `settle_day`, which is
what the scheduled runner calls. Inspection is not proof. The two differ in
HOW they feed bars, and that is exactly where a shared code path can still
diverge:

    replay   hands the engine a whole session at once, `now=None`
    live     drips bars in as they complete, one `now` at a time, and
             saves state in between

If anything in the engine depends on seeing the whole session - a column
read across the frame, a position opened on a bar that a cutoff would have
excluded - the two produce different trades from identical data. This runs
both over the same bars and compares the trades one for one.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from arena import engine

BAR_MINUTES = getattr(engine, "BAR_MINUTES", 5)


def _trades_of(state: dict) -> list[dict]:
    out = []
    for sid, book in sorted(state["books"].items()):
        for trade in book["trades"]:
            out.append({
                "strategy": sid,
                "symbol": trade.get("symbol"),
                "side": trade.get("side", "LONG"),
                "entry_at": str(trade.get("entry_at")),
                "exit_at": str(trade.get("exit_at")),
                "entry": round(float(trade.get("entry", 0.0)), 4),
                "exit": round(float(trade.get("exit", 0.0)), 4),
                "qty": trade.get("qty"),
            })
    out.sort(key=lambda t: (t["entry_at"], t["strategy"], t["symbol"] or ""))
    return out


def _sessions(enriched: dict[str, pd.DataFrame], days: int) -> list:
    found = set()
    for df in enriched.values():
        if df is not None and not df.empty:
            found.update(s for s in df["session"].unique() if s)
    return sorted(found)[-days:]


def run_replay(config: dict, enriched: dict, days: int) -> dict:
    return engine.replay(config, enriched, days=days)


def run_live(config: dict, enriched: dict, days: int) -> dict:
    """Drive the engine the way the scheduled runner does: bar by bar.

    `now` is advanced to just past each bar's close, because
    `process_bars` deliberately refuses a bar that has not finished yet.
    """
    state = engine.new_state(config)
    state["config"]["enabled"] = True
    for session in _sessions(enriched, days):
        engine.start_day(state, session)
        stamps = sorted({ts for df in enriched.values()
                         if df is not None and not df.empty
                         for ts in df.index[(df["session"] == session).to_numpy()]})
        for ts in stamps:
            now = ts.to_pydatetime() + dt.timedelta(minutes=BAR_MINUTES + 1)
            engine.process_bars(state, enriched, now=now)
        engine.settle_day(state)
    return state


def compare(config: dict, enriched: dict, days: int = 60) -> dict:
    """Both paths over the same bars. The trades must match one for one."""
    replayed = _trades_of(run_replay(config, enriched, days))
    live = _trades_of(run_live(config, enriched, days))

    differences = []
    for i in range(max(len(replayed), len(live))):
        a = replayed[i] if i < len(replayed) else None
        b = live[i] if i < len(live) else None
        if a != b:
            differences.append({"index": i, "replay": a, "live": b})
            if len(differences) >= 5:
                break

    return {
        "sessions": len(_sessions(enriched, days)),
        "replay_trades": len(replayed),
        "live_trades": len(live),
        "match": replayed == live,
        "differences": differences,
    }
