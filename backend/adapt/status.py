"""What each strategy is actually doing, said once a month.

    keep     doing what it was tested to do
    watch    past half its kill line, or fewer than 5 live trades
    review   live trade frequency under 0.5x or over 2x the tested rate,
             once it has had three months to settle
    out      past its kill line, with enough live evidence to mean it

`out` is the only status that leads to replacement, and it deliberately
needs more than a bad run. The brief's rule 6 - never remove a strategy
for one bad week - is enforced here: a strategy that crosses its kill
line is switched off immediately for safety, but it is not written out
until it has traded enough for the crossing to mean something. Until
then it is `watch`, off and observed.
"""
from __future__ import annotations

KEEP, WATCH, REVIEW, OUT = "keep", "watch", "review", "out"

MIN_LIVE_TRADES = 5          # fewer than this and nothing is established
SETTLE_MONTHS = 3            # frequency is not judged before this
FREQ_LOW, FREQ_HIGH = 0.5, 2.0

# Rule 6. A kill line crossed on a handful of trades switches a strategy
# off, but does not retire it.
MIN_TRADES_TO_RETIRE = 10
MIN_DAYS_TO_RETIRE = 20


def frequency_ratio(live_trades: int, live_days: int,
                    tested_trades: int, tested_days: int) -> float | None:
    """Live trades per day against tested trades per day."""
    if not live_days or not tested_days or not tested_trades:
        return None
    live_rate = live_trades / live_days
    tested_rate = tested_trades / tested_days
    if tested_rate <= 0:
        return None
    return round(live_rate / tested_rate, 3)


def classify(row: dict, kill: dict) -> dict:
    """One strategy's status, with the reason that decided it."""
    trades = int(row.get("live_trades") or 0)
    days = int(row.get("live_days") or 0)
    months = days / 21.0

    ratio = frequency_ratio(trades, days,
                            int(row.get("tested_trades") or 0),
                            int(row.get("tested_days") or 0))

    if kill.get("crossed"):
        enough = trades >= MIN_TRADES_TO_RETIRE and days >= MIN_DAYS_TO_RETIRE
        if enough:
            return {"status": OUT,
                    "why": "live drawdown %.2f%% is past its kill line of "
                           "%.2f%%, over %d trades and %d days"
                           % (kill["live_drawdown_pct"], kill["kill_line_pct"],
                              trades, days),
                    "frequency_ratio": ratio, "switched_off": True}
        return {"status": WATCH,
                "why": "past its kill line and switched off, but only %d "
                       "trade(s) over %d day(s): not enough to retire it on"
                       % (trades, days),
                "frequency_ratio": ratio, "switched_off": True}

    if trades < MIN_LIVE_TRADES:
        return {"status": WATCH,
                "why": "%d live trade(s), fewer than %d"
                       % (trades, MIN_LIVE_TRADES),
                "frequency_ratio": ratio, "switched_off": False}

    if kill.get("half_way"):
        return {"status": WATCH,
                "why": "live drawdown %.2f%% is past half its kill line "
                       "(%.2f%%)" % (kill["live_drawdown_pct"],
                                     kill["kill_line_pct"] / 2.0),
                "frequency_ratio": ratio, "switched_off": False}

    if months >= SETTLE_MONTHS and ratio is not None and (
            ratio < FREQ_LOW or ratio > FREQ_HIGH):
        return {"status": REVIEW,
                "why": "trading at %.2fx its tested rate after %.1f months"
                       % (ratio, months),
                "frequency_ratio": ratio, "switched_off": False}

    return {"status": KEEP, "why": "within its tested behaviour",
            "frequency_ratio": ratio, "switched_off": False}


def table(rows: list[dict], kills: list[dict]) -> list[dict]:
    by_id = {k["id"]: k for k in kills}
    out = []
    for row in sorted(rows, key=lambda r: r["id"]):
        kill = by_id.get(row["id"], {})
        verdict = classify(row, kill)
        out.append({"id": row["id"], "ticker": row.get("ticker"), **verdict,
                    "live_trades": int(row.get("live_trades") or 0),
                    "live_days": int(row.get("live_days") or 0),
                    "live_drawdown_pct": kill.get("live_drawdown_pct"),
                    "kill_line_pct": kill.get("kill_line_pct")})
    return out


def counts(rows: list[dict]) -> dict:
    out = {KEEP: 0, WATCH: 0, REVIEW: 0, OUT: 0}
    for row in rows:
        out[row["status"]] += 1
    return out
