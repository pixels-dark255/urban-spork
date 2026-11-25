"""Audit that every entry really filled at the next bar's open.

AGENTS.md §1.3 is the one rule a backtest can break without leaving a
mark: a fill at the signal bar's close looks exactly like a fill at the
next open on the leaderboard, and it is worth roughly the size of one bar
per trade in free money. This recomputes each entry from the bars and
compares it with what the engine booked, so the rule is measured rather
than asserted.

It also reports what the rule cost: the gap between the price the engine
paid and the signal bar's close it would have paid before Phase 9.
"""
import pandas as pd

from arena import engine


def _entry_leg(side: str) -> str:
    return "SELL" if side == "SHORT" else "BUY"


def audit(config: dict, enriched: dict[str, pd.DataFrame], days: int = 30) -> dict:
    """Replay, then check every entry price against the bar it claims."""
    state = engine.replay(config, enriched, days=days)
    market = engine.market_of(state)
    ccfg = market.costs.merged(state["config"]["costs"])

    checked = 0
    mismatches = []
    moved = 0
    deltas = []
    for sid, book in sorted(state["books"].items()):
        for trade in book["trades"]:
            frame = enriched.get(trade["symbol"])
            if frame is None or frame.empty:
                continue
            at = pd.Timestamp(trade["entry_at"])
            if at not in frame.index:
                mismatches.append({"strategy": sid, "at": trade["entry_at"],
                                   "why": "entry timestamp is not a bar"})
                continue
            where = frame.index.get_loc(at)
            if where == 0:
                # The first bar of the data has no previous bar to have
                # decided on it, so it cannot be an honest entry.
                mismatches.append({"strategy": sid, "at": trade["entry_at"],
                                   "why": "entered on the first bar of the data"})
                continue
            bar = frame.iloc[where]
            prev = frame.iloc[where - 1]
            expected = market.costs.fill_price(
                _entry_leg(trade["side"]), float(bar["Open"]), ccfg,
                trade["symbol"], engine._scale_for(state, trade["symbol"]))
            checked += 1
            if abs(float(trade["entry_price"]) - expected) > 0.01:
                mismatches.append({
                    "strategy": sid, "at": trade["entry_at"],
                    "why": "paid %.4f, next open is %.4f" % (trade["entry_price"], expected)})
                continue
            # What the old rule would have paid: the close of the bar that
            # produced the signal, which is the bar before this one.
            old = market.costs.fill_price(
                _entry_leg(trade["side"]), float(prev["Close"]), ccfg,
                trade["symbol"], engine._scale_for(state, trade["symbol"]))
            if abs(old - expected) > 0.01:
                moved += 1
                deltas.append(100.0 * (expected - old) / old)

    skipped = engine.fill_rule(state)["skipped"]
    return {
        "sessions": len(state["replay"]["sessions"]),
        "checked": checked,
        "mismatches": mismatches,
        "ok": checked > 0 and not mismatches,
        "moved": moved,
        "avg_move_pct": (sum(deltas) / len(deltas)) if deltas else 0.0,
        "worst_move_pct": (max(deltas, key=abs) if deltas else 0.0),
        "no_next_bar": skipped["no_next_bar"],
        "gapped_through": skipped["gapped_through"],
    }


# The planted-edge generator opens each bar exactly at the previous close,
# so on those bars the next-open rule and the old signal-close rule agree
# to the paisa and the audit measures nothing. Real bars gap. This puts a
# seeded gap on every bar so the rule has something to be different about.
GAP_PCT = 0.25


def with_gaps(frame: pd.DataFrame, seed: int = 20260325,
              gap_pct: float = GAP_PCT) -> pd.DataFrame:
    """The same bars, with a seeded gap between each close and the next open."""
    import numpy as np

    out = frame.copy()
    rng = np.random.default_rng(seed)
    shift = rng.normal(0.0, gap_pct / 100.0, len(out))
    # Only the open moves. The high and low are widened to keep the bar
    # valid rather than being shifted with it, so the gap is a gap and not
    # a different bar.
    opens = out["Open"].to_numpy() * (1.0 + shift)
    out["Open"] = opens
    out["High"] = out[["High", "Open"]].max(axis=1)
    out["Low"] = out[["Low", "Open"]].min(axis=1)
    return out


def last_bar_scenario() -> dict:
    """A signal on the last tradeable bar, which has no next open.

    The replay above cannot be relied on to produce one - whether any
    strategy signals in the final five minutes is an accident of the
    fixture - so the case is driven directly.
    """
    from arena import strategies as strat_mod

    class Always(strat_mod.Strategy):
        id = "orb"

        def entry(self, row, prev):
            return {"stop": row["Close"] * 0.97, "target": row["Close"] * 1.05,
                    "reason": "last-bar scenario"}

    cfg = {"market": "nse", "symbols": ["T.NS"], "strategies": ["orb"]}
    state = engine.new_state(cfg)
    engine.start_day(state, "2026-09-01")
    real = strat_mod.STRATEGIES["orb"]
    strat_mod.STRATEGIES["orb"] = Always()
    try:
        late = pd.Series({"Open": 100.0, "High": 100.5, "Low": 99.5, "Close": 100.0,
                          "minute": 15 * 60 + 10, "bar_no": 74})
        at = pd.Timestamp("2026-09-01 15:10", tz="Asia/Kolkata")
        engine.process_bar(state, "T.NS", at, late, late, 1)
        engine.settle_day(state)
    finally:
        strat_mod.STRATEGIES["orb"] = real
    book = state["books"]["orb"]
    return {"positions": len(book["positions"]),
            "no_next_bar": book["day_history"][-1]["skipped"]["no_next_bar"]}
