"""Sleeves: survivors that have earned a slot beside the core.

Added greedily, one at a time, each round taking whichever candidate most
improves the whole account's return over max drawdown. Greedy rather than
exhaustive on purpose: with a handful of candidates the optimum found by
searching every combination is mostly a search artefact, and the greedy
path is the one a person would actually follow.

Three refusals, all of them about not holding the same bet twice:

  correlation   Above 0.6 to the core or to a sleeve already chosen, it
                is turned down however good it looks alone. Two copies of
                one bet double the drawdown without doubling the return.
  one per ticker  The same instrument cannot occupy two slots.
  three sleeves   A hard stop. A fourth slot is another 20% of capital
                chasing a smaller improvement.

Half size on volatile days comes from signals/regime.py, so "volatile"
means the same thing here as everywhere else.
"""
from __future__ import annotations

import pandas as pd

from signals import regime

SLOT_FRACTION = 0.20
MAX_SLEEVES = 3
MAX_CORRELATION = 0.6


def daily_returns_from_trades(trades: list[dict], index: pd.DatetimeIndex,
                              slot_capital: float) -> pd.Series:
    """A strategy's trades collapsed into a daily return on its slot."""
    if slot_capital <= 0:
        return pd.Series(0.0, index=index)
    by_day: dict = {}
    for trade in trades:
        stamp = trade.get("exit_at") or trade.get("entry_at")
        if not stamp:
            continue
        day = pd.Timestamp(stamp).tz_localize(None).normalize()
        by_day[day] = by_day.get(day, 0.0) + float(trade.get("net", 0.0))
    naive = index.tz_convert(None) if getattr(index, "tz", None) else index
    values = [by_day.get(pd.Timestamp(d).normalize(), 0.0) / slot_capital
              for d in naive]
    return pd.Series(values, index=index)


def half_size_on_volatile(returns: pd.Series, vol_state: pd.Series) -> pd.Series:
    """Phase 2's sizing rule, applied to a sleeve's daily return.

    The regime is taken from the PREVIOUS session: today's volatility is
    not known when today's size is chosen.
    """
    multiple = vol_state.shift(1).map(regime.size_multiple)
    return returns * multiple.reindex(returns.index).fillna(
        regime.VOLATILE_SIZE_MULTIPLE)


def _correlation(a: pd.Series, b: pd.Series) -> float | None:
    joined = pd.concat([a, b], axis=1).dropna()
    if len(joined) < 30 or joined.iloc[:, 0].std() == 0 or joined.iloc[:, 1].std() == 0:
        return None
    value = float(joined.iloc[:, 0].corr(joined.iloc[:, 1]))
    return None if pd.isna(value) else value


def can_afford_one_share(slot_capital: float, price: float) -> bool:
    return bool(price > 0 and slot_capital >= price)


def account_equity(core_equity: pd.Series, sleeve_returns: list[pd.Series],
                   capital: float) -> pd.Series:
    """Core plus sleeves, each sleeve on its own 20% slot.

    The core keeps whatever the sleeves do not take, so adding a sleeve
    moves money out of the core rather than conjuring it.
    """
    core_fraction = 1.0 - SLOT_FRACTION * len(sleeve_returns)
    core_norm = core_equity / float(core_equity.iloc[0])
    total = core_norm * core_fraction
    for returns in sleeve_returns:
        growth = (1.0 + returns.reindex(core_equity.index).fillna(0.0)).cumprod()
        total = total + growth * SLOT_FRACTION
    return total * capital


def select(core_equity: pd.Series, candidates: list[dict], capital: float,
           vol_state: pd.Series | None = None) -> dict:
    """Greedily fill up to three slots. Returns the chosen sleeves and why.

    `candidates` are dicts with: id, ticker, returns (daily Series),
    max_price (for the whole-share check).
    """
    from .core import summarise

    slot_capital = capital * SLOT_FRACTION
    core_returns = core_equity.pct_change().fillna(0.0)

    chosen, rejected = [], []
    pool = []
    for cand in candidates:
        if not can_afford_one_share(slot_capital, cand.get("max_price", 0.0)):
            rejected.append({**_brief(cand),
                             "why": "a %.0f slot cannot buy one share at %.2f"
                                    % (slot_capital, cand.get("max_price", 0.0))})
            continue
        returns = cand["returns"]
        if vol_state is not None:
            returns = half_size_on_volatile(returns, vol_state)
        corr = _correlation(returns, core_returns)
        if corr is not None and corr > MAX_CORRELATION:
            rejected.append({**_brief(cand),
                             "why": "correlation %.2f to the core is above %.1f"
                                    % (corr, MAX_CORRELATION)})
            continue
        pool.append({**cand, "returns": returns, "core_correlation": corr})

    baseline = summarise(core_equity, capital)
    best_score = baseline["return_over_drawdown"] or 0.0
    history = [{"sleeves": 0, "return_over_drawdown": best_score,
                **{k: baseline[k] for k in ("return_pct", "cagr_pct",
                                            "max_drawdown_pct")}}]

    while pool and len(chosen) < MAX_SLEEVES:
        scored = []
        for cand in pool:
            if any(c["ticker"] == cand["ticker"] for c in chosen):
                continue
            clash = next((c for c in chosen
                          if (_correlation(cand["returns"], c["returns"]) or 0) > MAX_CORRELATION),
                         None)
            if clash:
                continue
            equity = account_equity(core_equity,
                                    [c["returns"] for c in chosen] + [cand["returns"]],
                                    capital)
            metrics = summarise(equity, capital)
            scored.append((metrics["return_over_drawdown"] or 0.0, cand, metrics))
        if not scored:
            break
        scored.sort(key=lambda row: (row[0], row[1]["id"]), reverse=True)
        score, cand, metrics = scored[0]
        if score <= best_score:
            rejected.append({**_brief(cand),
                             "why": "does not improve return over drawdown "
                                    "(%.3f vs %.3f)" % (score, best_score)})
            break
        chosen.append(cand)
        pool = [c for c in pool if c["id"] != cand["id"]]
        best_score = score
        history.append({"sleeves": len(chosen), "added": cand["id"],
                        "return_over_drawdown": score,
                        **{k: metrics[k] for k in ("return_pct", "cagr_pct",
                                                   "max_drawdown_pct")}})

    equity = account_equity(core_equity, [c["returns"] for c in chosen], capital)
    return {"chosen": [_brief(c) for c in chosen], "rejected": rejected,
            "history": history, "equity": equity,
            "slot_capital": round(slot_capital, 2),
            **summarise(equity, capital)}


def _brief(cand: dict) -> dict:
    return {"id": cand["id"], "ticker": cand["ticker"],
            "core_correlation": cand.get("core_correlation")}
