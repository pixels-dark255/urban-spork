"""
The long view: which strategy is actually earning, over weeks rather than
over one session.

The leaderboard answers "where does everything stand right now", which is
the wrong question for deciding what to trust with real money. One good
Tuesday flatters a strategy that loses on four days in five, and a single
bad day buries one that grinds out small wins. What matters is the record:
how often it finishes a day up, how often down, the size of its best day
against the size of its worst, and whether it beats the benchmark often
enough to be worth the risk.

Everything here is read out of `book["day_history"]`, which the engine has
been writing one row per settled session per strategy all along - up to 250
sessions. Nothing new is recorded and nothing is recomputed from trades, so
these numbers are the same ones the arena settled with on the day.

Three readings it is built to support, in plain terms:
  - which strategy made the most money over the period
  - which strategy won the most DAYS, which is a different question and
    often a different strategy
  - which strategy lost on the most days, because that is the one to stop
"""
from __future__ import annotations

from arena import engine
from arena.strategies import STRATEGIES

# Offered as period buttons. Sessions rather than calendar days: an arena
# has no record of a day it did not trade, and "the last 30 days" across a
# holiday week would silently mean something different per market.
PERIODS = [
    {"id": "5", "sessions": 5, "label": "last 5"},
    {"id": "10", "sessions": 10, "label": "last 10"},
    {"id": "30", "sessions": 30, "label": "last 30"},
    {"id": "90", "sessions": 90, "label": "last 90"},
    {"id": "all", "sessions": None, "label": "all time"},
]
DEFAULT_PERIOD = "30"
SPARK_DAYS = 12


def _period(period_id: str | None) -> dict:
    for p in PERIODS:
        if p["id"] == str(period_id):
            return p
    return next(p for p in PERIODS if p["id"] == DEFAULT_PERIOD)


def _rows_for(book: dict, dates: list[str]) -> list[dict]:
    wanted = set(dates)
    return [d for d in book.get("day_history", []) if d.get("date") in wanted]


def _session_dates(state: dict, sessions: int | None) -> list[str]:
    """Every settled session this arena has, newest last, optionally the
    most recent N. Taken from the arena-level history so a strategy that
    joined late does not shorten the period for everyone else."""
    dates = sorted({h["date"] for h in state.get("history", []) if h.get("date")})
    if not dates:
        # An arena whose per-strategy records outlive the arena summary.
        seen = set()
        for book in state.get("books", {}).values():
            seen.update(d["date"] for d in book.get("day_history", []) if d.get("date"))
        dates = sorted(seen)
    return dates[-sessions:] if sessions else dates


def _streaks(nets: list[float]) -> dict:
    """Current and longest runs of up days and down days.

    A flat day (exactly zero, almost always "did not trade") breaks a
    streak rather than extending either one: calling a day the strategy sat
    out a winning day would be flattery.
    """
    current = 0
    for net in reversed(nets):
        if net > 0 and current >= 0:
            current += 1
        elif net < 0 and current <= 0:
            current -= 1
        else:
            break
    longest_up = longest_down = run = 0
    for net in nets:
        if net > 0:
            run = run + 1 if run > 0 else 1
        elif net < 0:
            run = run - 1 if run < 0 else -1
        else:
            run = 0
        longest_up = max(longest_up, run)
        longest_down = min(longest_down, run)
    return {"current": current, "longest_green": longest_up,
            "longest_red": -longest_down}


def strategy_record(state: dict, sid: str, dates: list[str]) -> dict | None:
    book = state["books"].get(sid)
    strat = STRATEGIES.get(sid)
    if not book or not strat:
        return None
    rows = _rows_for(book, dates)
    nets = [float(r.get("net") or 0.0) for r in rows]
    traded = [r for r in rows if (r.get("trades") or 0) > 0]
    green = [r for r in rows if float(r.get("net") or 0) > 0]
    red = [r for r in rows if float(r.get("net") or 0) < 0]

    # Rupee totals are summed only where every day in the period had a
    # recorded rate. A partial sum would quietly mean "most of the period",
    # which is worse than saying nothing.
    inr_values = [r.get("net_inr") for r in rows]
    inr_known = bool(rows) and all(v is not None for v in inr_values)
    inr_total = sum(v for v in inr_values if v is not None) if inr_known else None

    best = max(rows, key=lambda r: float(r.get("net") or 0), default=None)
    worst = min(rows, key=lambda r: float(r.get("net") or 0), default=None)

    def day(row):
        if not row:
            return None
        return {"date": row["date"], "net": round(float(row.get("net") or 0), 4),
                "net_inr": row.get("net_inr"), "trades": row.get("trades", 0),
                "caught_up": bool(row.get("caught_up"))}

    total = round(sum(nets), 4)
    return {
        "id": sid,
        "name": strat.display_name(engine.market_of(state).has_volume),
        "benchmark": strat.benchmark,
        "status": book.get("status", "active"),
        "status_reason": book.get("status_reason"),
        "sessions": len(rows),
        "days_traded": len(traded),
        "green_days": len(green),
        "red_days": len(red),
        # Days it neither made nor lost money - overwhelmingly days it
        # found no signal at all, which is not the same as a flat result.
        "quiet_days": len(rows) - len(green) - len(red),
        "day_win_rate": (round(100 * len(green) / (len(green) + len(red)), 1)
                         if (green or red) else None),
        "net": total,
        "net_inr": round(inr_total, 2) if inr_total is not None else None,
        "avg_day": round(total / len(rows), 4) if rows else 0.0,
        "trades": sum(int(r.get("trades") or 0) for r in rows),
        "best_day": day(best),
        "worst_day": day(worst),
        "streaks": _streaks(nets),
        # Newest last, for a sparkline.
        "recent": [{"date": r["date"], "net": round(float(r.get("net") or 0), 4)}
                   for r in rows[-SPARK_DAYS:]],
        "daily_wins": 0,            # filled in by scoreboard()
        "beat_benchmark_days": 0,
    }


def scoreboard(state: dict, period_id: str | None = None) -> dict:
    """The whole record for one arena over one period."""
    period = _period(period_id)
    dates = _session_dates(state, period["sessions"])
    market = engine.market_of(state)
    cfg = state["config"]

    records = []
    for sid in cfg["strategies"]:
        rec = strategy_record(state, sid, dates)
        if rec:
            records.append(rec)
    by_id = {r["id"]: r for r in records}
    contenders = [r for r in records if not r["benchmark"]]

    # Per-day: who won, who lost, and how the yardstick did. The daily
    # winner is the question "which one would I have wanted that day", so
    # the benchmark is excluded - it is what they are all being measured
    # against, not a competitor.
    daily, summaries = [], {h["date"]: h for h in state.get("history", [])}
    for date in dates:
        per_strategy = {}
        for rec in records:
            book = state["books"].get(rec["id"]) or {}
            row = next((d for d in book.get("day_history", []) if d.get("date") == date), None)
            if row is not None:
                per_strategy[rec["id"]] = row
        if not per_strategy:
            continue
        bench_id = next((r["id"] for r in records if r["benchmark"]), None)
        bench_net = (float(per_strategy[bench_id].get("net") or 0)
                     if bench_id in per_strategy else None)
        active = {sid: float(row.get("net") or 0) for sid, row in per_strategy.items()
                  if sid != bench_id}
        # A day nobody traded has no winner - awarding one would make a
        # strategy look like it won on a day it never placed an order.
        movers = {sid: net for sid, net in active.items() if net != 0}
        winner = max(movers, key=movers.get) if movers else None
        loser = min(movers, key=movers.get) if movers else None
        if winner and movers[winner] > 0:
            by_id[winner]["daily_wins"] += 1
        if bench_net is not None:
            for sid, net in active.items():
                if net > bench_net:
                    by_id[sid]["beat_benchmark_days"] += 1
        daily.append({
            "date": date,
            "winner": winner, "winner_name": by_id[winner]["name"] if winner else None,
            "winner_net": round(movers[winner], 4) if winner else None,
            "loser": loser, "loser_name": by_id[loser]["name"] if loser else None,
            "loser_net": round(movers[loser], 4) if loser else None,
            "benchmark_net": round(bench_net, 4) if bench_net is not None else None,
            "traded": sum(1 for row in per_strategy.values() if (row.get("trades") or 0) > 0),
            "caught_up": bool((summaries.get(date) or {}).get("caught_up")),
        })

    records.sort(key=lambda r: (r["benchmark"], -r["net"]))
    return {
        "period": {
            "id": period["id"], "label": period["label"],
            "sessions": len(dates),
            "from": dates[0] if dates else None,
            "to": dates[-1] if dates else None,
        },
        "periods": PERIODS,
        "currency": {"code": market.quote_currency, "symbol": market.currency_symbol,
                     "fractional": market.fractional},
        "market": cfg.get("market"),
        "strategies": records,
        "daily": list(reversed(daily)),      # newest first, for reading
        "highlights": _highlights(contenders, daily),
        "note": ("Days are settled sessions, not calendar days. A strategy with a high "
                 "total but few winning days is living on one or two good sessions; one "
                 "that wins most days for a small total is doing something more "
                 "repeatable. Neither is proof - both are a record."),
    }


def _highlights(contenders: list[dict], daily: list[dict]) -> dict:
    """The handful of answers worth putting above the table."""
    if not contenders:
        return {}

    # Only strategies that actually placed trades can hold any of these
    # titles. A strategy that found no signal all month sits at exactly
    # zero, which beats every strategy that lost money - so without this
    # filter "most profitable" goes to the one that never traded, which is
    # true arithmetic and a useless answer.
    traded_any = [r for r in contenders if r["trades"]]

    def pick(key, rows=None, reverse=True):
        rows = [r for r in (rows if rows is not None else traded_any) if r["sessions"]]
        if not rows:
            return None
        best = (max if reverse else min)(rows, key=key)
        return {"id": best["id"], "name": best["name"], "net": best["net"],
                "net_inr": best["net_inr"], "daily_wins": best["daily_wins"],
                "green_days": best["green_days"], "red_days": best["red_days"],
                "trades": best["trades"], "sessions": best["sessions"]}
    best_single = max((r["best_day"] | {"strategy": r["name"], "id": r["id"]}
                       for r in traded_any if r["best_day"]),
                      key=lambda d: d["net"], default=None)
    worst_single = min((r["worst_day"] | {"strategy": r["name"], "id": r["id"]}
                        for r in traded_any if r["worst_day"]),
                       key=lambda d: d["net"], default=None)
    winners = [r for r in traded_any if r["daily_wins"]]
    losers = [r for r in traded_any if r["red_days"]]
    return {
        "most_profitable": pick(lambda r: r["net"]),
        # Deliberately a different question from "made the most money": a
        # strategy can top the table on one enormous day and still lose on
        # four days in five.
        "most_winning_days": pick(lambda r: (r["daily_wins"], r["net"]), rows=winners),
        "most_losing_days": pick(lambda r: (r["red_days"], -r["net"]), rows=losers),
        "best_single_day": best_single if best_single and best_single["net"] > 0 else None,
        "worst_single_day": worst_single if worst_single and worst_single["net"] < 0 else None,
        # Said out loud, because an arena where nothing trades looks
        # identical to one where everything loses.
        "idle": [{"id": r["id"], "name": r["name"]} for r in contenders if not r["trades"]],
    }


def to_inr_totals(board: dict, fx: dict | None) -> dict:
    """Convenience for callers that want the rupee column filled in from a
    current rate when the per-day rates were not recorded. Kept separate
    and explicit: it restates history at today's rate, which is fine for a
    rough total and wrong for anything else."""
    rate = (fx or {}).get("inr_per_unit")
    if not rate:
        return board
    for rec in board.get("strategies", []):
        if rec.get("net_inr") is None:
            rec["net_inr_estimated"] = round(rec["net"] * float(rate), 2)
    return board
