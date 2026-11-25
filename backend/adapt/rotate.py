"""Replacing a dead strategy, only when the replacement is not worse.

Three conditions, all of them, before anything is swapped:

  * the candidate's ticker is not already in the book - one instrument
    per slot, or the account holds the same bet twice;
  * its correlation to the core and to every surviving sleeve is at most
    0.6, for the same reason;
  * the account's return over drawdown with the swap is NOT WORSE than
    without it.

The third is the one that stops a rotation from being motion for its own
sake. A slot falling empty is uncomfortable, and the urge to fill it with
the least-bad candidate is exactly how a book fills up with strategies
nobody chose.

Every proposal is logged with the date, the reason, and the measured
comparison that justified it - including the refusals, because a rotation
that was considered and rejected is as much a decision as one that
happened.
"""
from __future__ import annotations

import datetime as dt
import pathlib

import pandas as pd

from swing import core, sleeves

MAX_CORRELATION = sleeves.MAX_CORRELATION          # 0.6
LOG = pathlib.Path(__file__).resolve().parent.parent.parent / "CHANGELOG_rules.md"


def _correlation(a: pd.Series, b: pd.Series) -> float | None:
    return sleeves._correlation(a, b)


def propose(out_id: str, candidates: list[dict], keeping: list[dict],
            core_equity: pd.Series, capital: float) -> dict:
    """The best legal replacement for `out_id`, or a refusal with reasons.

    `candidates` and `keeping` carry id, ticker and a daily return series.
    """
    used = {row["ticker"] for row in keeping}
    core_returns = core_equity.pct_change().fillna(0.0)

    before = core.summarise(
        sleeves.account_equity(core_equity, [k["returns"] for k in keeping],
                               capital), capital)
    baseline = before["return_over_drawdown"]

    considered, rejected = [], []
    for candidate in sorted(candidates, key=lambda c: c["id"]):
        if candidate["id"] == out_id:
            continue
        if candidate["ticker"] in used:
            rejected.append({**_brief(candidate),
                             "why": "%s is already in the book"
                                    % candidate["ticker"]})
            continue
        core_corr = _correlation(candidate["returns"], core_returns)
        if core_corr is not None and core_corr > MAX_CORRELATION:
            rejected.append({**_brief(candidate),
                             "why": "correlation %.2f to the core is above %.1f"
                                    % (core_corr, MAX_CORRELATION)})
            continue
        clash = next((k for k in keeping
                      if (_correlation(candidate["returns"], k["returns"]) or 0)
                      > MAX_CORRELATION), None)
        if clash:
            rejected.append({**_brief(candidate),
                             "why": "correlation above %.1f to %s, which is "
                                    "staying" % (MAX_CORRELATION, clash["id"])})
            continue

        after = core.summarise(
            sleeves.account_equity(
                core_equity, [k["returns"] for k in keeping] + [candidate["returns"]],
                capital), capital)
        score = after["return_over_drawdown"]
        considered.append({**_brief(candidate), "core_correlation": core_corr,
                           "return_over_drawdown": score,
                           "baseline": baseline,
                           "not_worse": score is not None and baseline is not None
                                        and score >= baseline,
                           "after": {k: after[k] for k in
                                     ("return_pct", "cagr_pct",
                                      "max_drawdown_pct")}})

    viable = [c for c in considered if c["not_worse"]]

    # Every candidate that was looked at and passed over is named, whether
    # or not a viable one was found. A rotation log that only lists the
    # ones that failed on a technicality hides the ones that simply were
    # not good enough, which is the more common case.
    for c in considered:
        if not c["not_worse"]:
            rejected.append({**{k: c[k] for k in ("id", "ticker")},
                             "why": "account return over drawdown would go "
                                    "from %s to %s"
                                    % (baseline, c["return_over_drawdown"])})
    if not viable:
        return {"rotate": False, "out": out_id, "chosen": None,
                "baseline": baseline, "considered": considered,
                "rejected": rejected,
                "why": "no candidate leaves the account no worse; the slot "
                       "stays empty rather than being filled for its own sake"}

    viable.sort(key=lambda c: (c["return_over_drawdown"], c["id"]), reverse=True)
    chosen = viable[0]
    return {"rotate": True, "out": out_id, "chosen": chosen,
            "baseline": baseline, "considered": considered,
            "rejected": rejected,
            "why": "return over drawdown %s -> %s with %s in place of %s"
                   % (baseline, chosen["return_over_drawdown"],
                      chosen["id"], out_id)}


def _brief(row: dict) -> dict:
    return {"id": row["id"], "ticker": row["ticker"]}


def log_rotation(proposal: dict, dry_run: bool, path: pathlib.Path | None = None,
                 today: dt.date | None = None) -> pathlib.Path:
    """Append the decision, taken or not. Refusals are decisions too."""
    path = pathlib.Path(path or LOG)
    today = today or dt.date.today()
    head = "%s %s" % (today.isoformat(), "[dry run]" if dry_run else "")
    if proposal["rotate"]:
        line = ("- **%s** rotate: %s out, %s (%s) in. %s"
                % (head.strip(), proposal["out"], proposal["chosen"]["id"],
                   proposal["chosen"]["ticker"], proposal["why"]))
    else:
        line = ("- **%s** no rotation for %s. %s"
                % (head.strip(), proposal["out"], proposal["why"]))
    if proposal["rejected"]:
        line += "\n" + "\n".join(
            "    - rejected %s: %s" % (r["id"], r["why"])
            for r in proposal["rejected"])
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return path
