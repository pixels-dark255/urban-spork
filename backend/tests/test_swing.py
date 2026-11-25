"""Phase 5: the swing account.

The core rotation is one decision repeated, which makes it easy to check
by construction: on a market that only rises, the rule should hold the
engine and nothing else.
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from arena import costs
from swing import core, picker, sleeves


def frame(values, start="2020-01-01"):
    idx = pd.bdate_range(start, periods=len(values))
    v = np.asarray(values, dtype=float)
    return pd.DataFrame({"Open": v, "High": v * 1.004,
                         "Low": v * 0.996, "Close": v}, index=idx)


def rising(n=600, step=0.3):
    return [100.0 + i * step for i in range(n)]


def falling(n=600, step=0.2):
    return [200.0 - i * step for i in range(n)]


# ------------------------------------------------------- delivery costs

def test_delivery_costs_more_than_intraday():
    """Swing holds overnight, so it pays CNC rates, not MIS.

    STT alone goes from 0.025% on one leg to 0.1% on both. A rotation
    charged intraday rates would look better than it is.
    """
    t = 100000.0
    intra = sum(costs.leg_charges(s, t, costs.DEFAULT_COSTS)["total"]
                for s in ("BUY", "SELL"))
    deliv = sum(costs.leg_charges(s, t, costs.DELIVERY_COSTS)["total"]
                for s in ("BUY", "SELL"))
    assert deliv > intra * 2


def test_intraday_still_pays_no_stt_on_the_buy():
    assert costs.leg_charges("BUY", 100000.0, costs.DEFAULT_COSTS)["stt"] == 0.0
    assert costs.leg_charges("BUY", 100000.0, costs.DELIVERY_COSTS)["stt"] > 0


# ------------------------------------------------------- the rotation

def test_a_rising_engine_is_held_throughout():
    bars = {"E": frame(rising()), "S": frame([50.0] * 600)}
    out = core.run(bars, "E", "S", 100000.0)
    assert out["ok"]
    # One switch: cash into the engine once the average exists.
    assert out["switches"] == 1
    assert out["return_pct"] > 0


def test_a_falling_engine_is_never_held():
    bars = {"E": frame(falling()), "S": frame([50.0] * 600)}
    out = core.run(bars, "E", "S", 100000.0)
    assert out["return_pct"] <= 0.01     # the safe asset is flat


def test_cash_as_the_safe_asset_loses_nothing_while_waiting():
    bars = {"E": frame(falling())}
    out = core.run(bars, "E", core.CASH, 100000.0)
    assert out["ok"]
    assert out["return_pct"] == pytest.approx(0.0, abs=0.001)


def test_the_warm_up_is_sat_out_rather_than_guessed():
    # Before a 200-day average exists there is no signal, and assuming
    # the engine is fine would be a position taken on nothing.
    bars = {"E": frame(rising(400)), "S": frame([50.0] * 400)}
    out = core.run(bars, "E", "S", 100000.0)
    assert float(out["equity"].iloc[0]) == pytest.approx(100000.0)


def test_every_switch_pays_both_legs():
    bars = {"E": frame(rising()), "S": frame([50.0] * 600)}
    out = core.run(bars, "E", "S", 100000.0)
    assert out["charges"] > 0


def test_doubling_costs_lowers_the_return():
    bars = {"E": frame(rising()), "S": frame([50.0] * 600)}
    one = core.run(bars, "E", "S", 100000.0)
    two = core.run(bars, "E", "S", 100000.0, cost_multiple=2.0)
    assert two["return_pct"] < one["return_pct"]


def test_only_whole_shares_are_bought():
    """A share near 7,000 against 10,000 of capital buys one, not 1.4.

    The leftover stays in cash, which on a small account is a real drag
    rather than a rounding note: a third of the capital does no work.
    """
    bars = {"E": frame([7000.0 + i for i in range(600)])}
    out = core.run(bars, "E", core.CASH, 10000.0)
    assert out["ok"] and out["switches"] >= 1
    held = out["equity"].iloc[-1]
    # One share of a ~7,500 stock plus idle cash, never 1.4 shares' worth.
    assert held < 10000.0 + 600.0


def test_a_slot_too_small_for_one_share_is_reported_not_hidden():
    bars = {"E": frame([float(i) for i in rising(600, step=50)])}
    out = core.run(bars, "E", core.CASH, 500.0)
    assert out["ok"]
    assert out["could_not_buy"], "a slot that cannot buy a share must be named"


def test_missing_bars_are_refused_rather_than_assumed():
    assert not core.run({}, "E", "S", 1000.0)["ok"]
    assert not core.run({"E": frame(rising())}, "E", "MISSING", 1000.0)["ok"]


# ---------------------------------------------------------- the metrics

def test_max_drawdown_is_a_percentage_of_the_peak():
    equity = pd.Series([100.0, 120.0, 60.0, 90.0])
    assert core.max_drawdown(equity) == pytest.approx(-50.0)


def test_a_curve_that_only_rises_has_no_drawdown():
    assert core.max_drawdown(pd.Series([1.0, 2.0, 3.0])) == 0.0


def test_a_curve_with_no_drawdown_has_no_ratio_rather_than_an_infinite_one():
    # Dividing by a zero drawdown is undefined, not excellent. Reporting a
    # huge number here is how a curve that never dipped because it never
    # traded ends up at the top of a leaderboard.
    out = core.summarise(pd.Series(np.linspace(100, 110, 252)), 100.0)
    assert out["max_drawdown_pct"] == 0.0
    assert out["return_over_drawdown"] is None


def test_return_over_drawdown_rewards_a_calmer_curve():
    def dip(depth):
        half = np.linspace(100, 100 - depth, 126)
        return pd.Series(np.concatenate([half, np.linspace(100 - depth, 112, 126)]))
    calm = core.summarise(dip(5), 100.0)
    wild = core.summarise(dip(40), 100.0)
    assert calm["return_over_drawdown"] > wild["return_over_drawdown"]


# ------------------------------------------------------- the trend rules

@pytest.mark.parametrize("rule,label", [
    (core.sma_rule(100), "SMA100"), (core.ema_rule(200), "EMA200"),
    (core.cross_rule(50, 200), "50/200 cross")])
def test_each_neighbour_rule_is_labelled(rule, label):
    assert rule.label == label


def test_an_ema_rule_waits_for_its_span():
    # An EMA has a value from bar one, which is not the same as having
    # seen enough bars to mean anything.
    out = core.ema_rule(200)(pd.Series(rising(300)))
    assert out.iloc[:199].isna().all()
    assert out.iloc[199:].notna().all()


def test_the_brief_s_five_neighbours_are_all_present():
    labels = {r.label for r in core.NEIGHBOUR_RULES}
    assert labels == {"SMA100", "SMA150", "SMA250", "EMA200", "50/200 cross"}


# ------------------------------------------------------------ the picker

def seeded_bars(seed=5, n=1500):
    rng = np.random.default_rng(seed)
    out = {}
    for i, name in enumerate(["E1.NS", "E2.NS", "S1.NS", "S2.NS"]):
        v = 100 * np.cumprod(1 + rng.normal(0.0006 - 0.0003 * i, 0.004 + 0.002 * i, n))
        out[name] = frame(v, start="2019-01-01")
    return out


def test_the_lineup_does_not_change_when_the_future_is_removed():
    """The whole point of a quarterly backtest.

    A picker that reads ahead looks exactly like a correct one until the
    data after the pick date is taken away.
    """
    bars = seeded_bars()
    out = picker.verify_causal(bars, dt.date(2023, 12, 31),
                               ["E1.NS", "E2.NS"], ["S1.NS", "S2.NS", "CASH"])
    assert out["match"], (out["full"], out["truncated"])


def test_nothing_after_the_pick_date_is_even_supplied():
    bars = seeded_bars()
    cut = picker.truncate(bars, dt.date(2022, 6, 30))
    for frame_ in cut.values():
        assert frame_.index.max().date() <= dt.date(2022, 6, 30)


def test_the_engine_with_the_largest_trend_gap_wins():
    bars = seeded_bars()
    out = picker.pick(bars, dt.date(2023, 12, 31), ["E1.NS", "E2.NS"],
                      ["S1.NS", "CASH"])
    gaps = {e["ticker"]: e["trend_gap"] for e in out["engines"] if e["usable"]}
    assert out["engine"] == max(gaps, key=gaps.get)


def test_a_too_correlated_safe_asset_is_refused():
    bars = seeded_bars()
    bars["COPY.NS"] = bars["E2.NS"].copy()
    out = picker.pick(bars, dt.date(2023, 12, 31), ["E2.NS"], ["COPY.NS", "CASH"])
    rejected = next(s for s in out["safes"] if s["ticker"] == "COPY.NS")
    assert not rejected["usable"] and "correlation" in rejected["why"]
    assert out["safe"] == core.CASH


def test_cash_is_the_fallback_not_a_failure():
    bars = seeded_bars()
    out = picker.pick(bars, dt.date(2023, 12, 31), ["E2.NS"], ["CASH"])
    assert out["ok"] and out["safe"] == core.CASH


def test_a_date_with_no_history_refuses_to_pick():
    bars = seeded_bars()
    out = picker.pick(bars, dt.date(2019, 2, 1), ["E2.NS"], ["CASH"])
    assert not out["ok"] and "enough history" in out["reason"]


def test_quarter_ends_are_the_four_calendar_quarters():
    out = picker.quarter_ends(dt.date(2023, 1, 1), dt.date(2023, 12, 31))
    assert [d.isoformat() for d in out] == ["2023-03-31", "2023-06-30",
                                            "2023-09-30", "2023-12-31"]


def test_the_trade_minimums_are_the_relaxed_ten_and_four():
    assert picker.MIN_TRADES["reversion"] == 10
    assert picker.MIN_TRADES["breakout"] == 4


# ------------------------------------------------------------ sleeves

def sleeve(i, mu, seed, n=500, price=500.0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=n)
    return {"id": "s%d" % i, "ticker": "T%d.NS" % i, "max_price": price,
            "returns": pd.Series(rng.normal(mu, 0.004, n), index=idx)}


def core_curve(n=500, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=n)
    return pd.Series(100000 * np.cumprod(1 + rng.normal(0.0004, 0.008, n)), index=idx)


def test_a_sleeve_that_does_not_improve_the_account_is_refused():
    out = sleeves.select(core_curve(), [sleeve(1, -0.0005, 11)], 100000.0)
    assert out["chosen"] == []
    assert "does not improve" in out["rejected"][0]["why"]


def test_at_most_three_sleeves_are_taken():
    cands = [sleeve(i, 0.0006, 20 + i) for i in range(6)]
    out = sleeves.select(core_curve(), cands, 100000.0)
    assert len(out["chosen"]) <= sleeves.MAX_SLEEVES


def test_a_slot_that_cannot_buy_one_share_is_rejected_with_the_price():
    out = sleeves.select(core_curve(), [sleeve(1, 0.001, 9, price=50000.0)], 100000.0)
    assert out["chosen"] == []
    assert "cannot buy one share" in out["rejected"][0]["why"]


def test_a_sleeve_too_correlated_to_the_core_is_refused():
    curve = core_curve()
    twin = {"id": "twin", "ticker": "X.NS", "max_price": 100.0,
            "returns": curve.pct_change().fillna(0.0)}
    out = sleeves.select(curve, [twin], 100000.0)
    assert out["chosen"] == []
    assert "correlation" in out["rejected"][0]["why"]


def test_one_sleeve_per_ticker():
    a = sleeve(1, 0.0008, 31)
    b = dict(sleeve(2, 0.0008, 32), ticker=a["ticker"])
    out = sleeves.select(core_curve(), [a, b], 100000.0)
    tickers = [c["ticker"] for c in out["chosen"]]
    assert len(tickers) == len(set(tickers))


def test_each_sleeve_takes_a_fifth_of_the_account():
    assert sleeves.SLOT_FRACTION == 0.20


def test_sleeves_take_their_money_from_the_core():
    """Adding a sleeve must move capital, not conjure it.

    With a flat sleeve, a 20% slot should leave the account slightly
    below the core alone rather than above it.
    """
    curve = core_curve()
    flat = pd.Series(0.0, index=curve.index)
    account = sleeves.account_equity(curve, [flat], 100000.0)
    assert float(account.iloc[-1]) < float(curve.iloc[-1])


def test_half_size_is_applied_on_volatile_days():
    idx = pd.bdate_range("2022-01-03", periods=6)
    returns = pd.Series([0.01] * 6, index=idx)
    state = pd.Series([regime.VOL_CALM if i < 3 else regime.VOL_VOLATILE
                       for i in range(6)], index=idx)
    out = sleeves.half_size_on_volatile(returns, state)
    # The state is read from the previous session, so the change lands
    # one day after the regime turns.
    assert out.iloc[2] == pytest.approx(0.01)
    assert out.iloc[-1] == pytest.approx(0.005)


from signals import regime      # noqa: E402  (used above)


def test_an_engine_that_never_dips_has_no_trend_gap_rather_than_a_made_up_one():
    """A gap needs both buckets.

    If a ticker never closes below its own 200-day average there is no
    "below" sample, and reporting a gap anyway would compare an average
    to nothing.
    """
    bars = seeded_bars()
    out = picker.pick(bars, dt.date(2023, 12, 31), ["E1.NS", "E2.NS"],
                      ["S1.NS", "CASH"])
    e1 = next(e for e in out["engines"] if e["ticker"] == "E1.NS")
    assert e1["trend_gap"] is None and not e1["usable"]
    assert out["engine"] == "E2.NS"


# ------------------------------------- the two accounts stay separate

def test_the_swing_account_uses_its_own_capital_setting():
    """daily_capital_inr is the Arena's. swing_capital_inr is this one's.

    Mixing them would put an intraday day's budget behind an overnight
    position, or report a swing return against an intraday base.
    """
    import settings
    cfg = settings.load()
    assert cfg["daily_capital_inr"] != cfg["swing_capital_inr"]
    source = pathlib.Path(__file__).resolve().parent.parent / "swing" / "cli.py"
    text = source.read_text()
    assert "swing_capital_inr" in text
    assert "daily_capital_inr" not in text, (
        "the swing account must never read the Arena's intraday budget")


def test_the_swing_package_does_not_touch_the_arena_engine():
    """No shared state, so an intraday result cannot land in this book.

    The two share a cost model and a fill rule and nothing else.
    """
    import pathlib as _p
    folder = _p.Path(__file__).resolve().parent.parent / "swing"
    for path in folder.glob("*.py"):
        text = path.read_text()
        assert "arena.engine" not in text and "from arena import engine" not in text, (
            "%s imports the Arena engine" % path.name)


import pathlib      # noqa: E402  (used above)
