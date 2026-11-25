"""Paper against backtest, trade by trade, with every gap classified.

The runner and the backtest are supposed to be the same code over the
same bars (AGENTS.md §1.2, proved in Phase 3). They still drift: a fill
lands a paisa off, a bar arrives late, a risk limit cuts a size. Most of
that is benign and explainable.

The point of classifying is to find what is NOT. Six buckets, and five of
them are excuses:

    fills      same trade, different price
    timing     same trade, different bar
    rounding   same trade, a share or two apart
    data       a trade on one side only, because the bars differed
    limits     a trade the risk gate cut or refused
    rules      everything else

`rules` is the residual, and that is deliberate. Anything that cannot be
explained by a known benign cause is a bug in the rule, not a curiosity,
and it is reported as one. A classifier with a "miscellaneous" bucket
that quietly absorbs the unexplained would defeat the whole exercise.
"""
from __future__ import annotations

import dataclasses

# A fill within this fraction of the price is slippage, not a different
# decision. Wider than a tick, far narrower than a bar.
PRICE_TOLERANCE = 0.002
ROUNDING_SHARES = 1

FILLS, TIMING, ROUNDING, DATA, LIMITS, RULES = (
    "fills", "timing", "rounding", "data", "limits", "rules")
BUCKETS = (FILLS, TIMING, ROUNDING, DATA, LIMITS, RULES)


def key_of(trade: dict) -> tuple:
    return (str(trade.get("date")), str(trade.get("ticker")),
            str(trade.get("strategy")), str(trade.get("side", "")).upper())


@dataclasses.dataclass
class Gap:
    bucket: str
    key: tuple
    detail: str

    def to_dict(self) -> dict:
        return {"bucket": self.bucket, "key": "|".join(self.key),
                "detail": self.detail}


def _index(trades: list[dict]) -> dict:
    out: dict = {}
    for trade in trades:
        out.setdefault(key_of(trade), []).append(trade)
    return out


def compare(paper: list[dict], backtest: list[dict],
            limited_keys: set | None = None,
            missing_bars: set | None = None) -> dict:
    """Line the two up and classify every difference.

    `limited_keys` are trades the risk gate is known to have touched, and
    `missing_bars` the (date, ticker) pairs where one side had no data.
    Both are supplied rather than guessed: inferring "it must have been a
    limit" from the shape of a gap is how a rule bug gets excused.
    """
    limited_keys = limited_keys or set()
    missing_bars = missing_bars or set()
    left, right = _index(paper), _index(backtest)
    gaps: list[Gap] = []
    matched = 0

    for key in sorted(set(left) | set(right)):
        here, there = left.get(key, []), right.get(key, [])
        if here and not there or there and not here:
            only = "paper" if here else "backtest"
            if key in limited_keys:
                gaps.append(Gap(LIMITS, key, "only in %s; the risk gate "
                                             "touched this trade" % only))
            elif (key[0], key[1]) in missing_bars:
                gaps.append(Gap(DATA, key, "only in %s; one side had no bars "
                                           "for this session" % only))
            else:
                gaps.append(Gap(RULES, key,
                                "only in %s, with no limit and no missing "
                                "bar to explain it" % only))
            continue

        for a, b in zip(here, there):
            matched += 1
            qty_a, qty_b = int(a.get("qty", 0)), int(b.get("qty", 0))
            price_a = float(a.get("price") or 0.0)
            price_b = float(b.get("price") or 0.0)
            at_a, at_b = str(a.get("at", "")), str(b.get("at", ""))

            if qty_a != qty_b:
                if abs(qty_a - qty_b) <= ROUNDING_SHARES:
                    gaps.append(Gap(ROUNDING, key, "%d vs %d shares"
                                    % (qty_a, qty_b)))
                elif key in limited_keys:
                    gaps.append(Gap(LIMITS, key, "%d vs %d shares; the risk "
                                                 "gate cut this" % (qty_a, qty_b)))
                else:
                    gaps.append(Gap(RULES, key, "%d vs %d shares, which is "
                                                "more than rounding and no "
                                                "limit applied" % (qty_a, qty_b)))
                continue
            if at_a and at_b and at_a != at_b:
                gaps.append(Gap(TIMING, key, "%s vs %s" % (at_a, at_b)))
                continue
            if price_b and abs(price_a - price_b) / price_b > PRICE_TOLERANCE:
                gaps.append(Gap(FILLS, key, "%.4f vs %.4f" % (price_a, price_b)))

    counts = {b: 0 for b in BUCKETS}
    for gap in gaps:
        counts[gap.bucket] += 1
    return {
        "matched": matched,
        "paper_trades": len(paper), "backtest_trades": len(backtest),
        "counts": counts,
        "gaps": [g.to_dict() for g in gaps],
        "bugs": [g.to_dict() for g in gaps if g.bucket == RULES],
        "clean": counts[RULES] == 0,
    }


def summary(result: dict) -> str:
    counts = result["counts"]
    parts = ["%d trades lined up" % result["matched"]]
    for bucket in BUCKETS:
        if counts[bucket]:
            parts.append("%d %s" % (counts[bucket], bucket))
    line = ", ".join(parts)
    if counts[RULES]:
        line += ("  <- %d gap(s) nothing benign explains; these are bugs"
                 % counts[RULES])
    return line
