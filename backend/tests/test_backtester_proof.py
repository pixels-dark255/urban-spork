"""Phase 3: prove the backtester before trusting it.

THE PASS RANGES IN THIS FILE WERE WRITTEN BEFORE ANY OF IT WAS RUN.

That ordering is the whole point. A range chosen after seeing the number is
not a test, it is a description, and it will accept whatever the code
happens to do - including the bug it happens to have. So the numbers below
come from arithmetic done in advance:

  A planted +1.0% edge, entered at the next open and exited at that day's
  close, captures the bump and pays one round trip of Indian costs. Those
  costs are roughly 0.21% - 0.10% slippage over two legs, ~0.06%
  brokerage, 0.025% STT, 0.003% stamp, ~0.006% exchange and GST on top -
  so the expected net is about 0.79%. The brief's band of 0.6% to 1.2%
  comfortably contains that while still rejecting a backtester that pays
  no costs (1.0%+) or double-counts the bump (2.0%).

  With the bump removed, the same trades are a coin flip minus the same
  costs, so the expected net is about -0.21% and anything positive is a
  bug. The generator is sized so this is a real test rather than a
  coin toss: ~375 trades at a per-leg sigma of 0.707% gives a standard
  error near 0.036%, so the +/-0.15% band is about four standard errors
  wide and will not fail by luck. Determinism (AGENTS.md §1.7) does the
  rest: one fixed seed, one answer, every run.
"""
import pytest

pytest.importorskip("validation.synthetic")

from validation import lookahead, synthetic           # noqa: E402

# --------------------------------------------------------------------------
# The pass ranges. Written first. Do not widen one to make a run go green:
# a failure here means the backtester is wrong, not that the band is.
# --------------------------------------------------------------------------

PLANTED_BUMP_PCT = 1.0

PLANTED_NET_MIN_PCT = 0.6
PLANTED_NET_MAX_PCT = 1.2

NOISE_TOLERANCE_PCT = 0.15

# Below this the experiment has no power and a pass would mean nothing.
MIN_TRADES = 200

# The brief's guard: 30 truncation points.
GUARD_DATES = 30

SEED = 20260325


@pytest.fixture(scope="module")
def planted():
    return synthetic.run_planted_edge(bump_pct=PLANTED_BUMP_PCT, seed=SEED)


@pytest.fixture(scope="module")
def noise():
    return synthetic.run_planted_edge(bump_pct=0.0, seed=SEED)


# ---------------------------------------------------- 1. the planted edge

def test_the_experiment_has_enough_trades_to_mean_anything(planted):
    assert planted["trades"] >= MIN_TRADES


def test_a_planted_edge_is_recovered_after_costs(planted):
    net = planted["avg_net_pct"]
    assert PLANTED_NET_MIN_PCT <= net <= PLANTED_NET_MAX_PCT, (
        "a planted %.1f%% edge came back as %.3f%% net, outside the %.1f-%.1f%% "
        "band written before this was run"
        % (PLANTED_BUMP_PCT, net, PLANTED_NET_MIN_PCT, PLANTED_NET_MAX_PCT))


def test_the_planted_edge_costs_something(planted):
    # Gross above net, by about the round trip. A backtester that reports
    # the same number for both is not charging costs at all (§1.4).
    assert planted["avg_gross_pct"] > planted["avg_net_pct"]
    assert planted["cost_pct"] > 0


def test_the_recovered_edge_is_not_larger_than_the_one_planted(planted):
    # Gross above the bump would mean the harness is capturing something
    # the generator did not put there: double-counting, or look-ahead.
    assert planted["avg_gross_pct"] <= PLANTED_BUMP_PCT + 0.15


# ----------------------------------------------------------- 2. pure noise

def test_noise_pays_costs_and_earns_nothing(noise):
    expected = -noise["cost_pct"]
    net = noise["avg_net_pct"]
    assert abs(net - expected) <= NOISE_TOLERANCE_PCT, (
        "with no edge planted the average trade should sit near %.3f%% "
        "(costs alone); it came back %.3f%%" % (expected, net))


def test_profit_on_pure_noise_is_a_bug(noise):
    assert noise["avg_net_pct"] < 0, (
        "the backtester made %.3f%% a trade on data with no edge in it"
        % noise["avg_net_pct"])


def test_noise_is_flat_before_costs(noise):
    assert abs(noise["avg_gross_pct"]) <= NOISE_TOLERANCE_PCT


def test_the_only_difference_between_the_two_runs_is_the_bump(planted, noise):
    # Same seed, same flags, same trade count. If these differ, the two
    # runs are not comparable and neither result means anything.
    assert planted["trades"] == noise["trades"]
    assert planted["flag_days"] == noise["flag_days"]


# ------------------------------------------------------ 3. look-ahead guard

def test_an_honest_signal_survives_truncation():
    report = lookahead.check(synthetic.honest_signal,
                             synthetic.random_walk(600, seed=SEED),
                             dates=GUARD_DATES, seed=SEED)
    assert report["checked"] == GUARD_DATES
    assert report["mismatches"] == [], report["mismatches"]
    assert report["ok"]


def test_a_signal_that_peeks_at_tomorrow_is_caught():
    report = lookahead.check(synthetic.cheating_signal,
                             synthetic.random_walk(600, seed=SEED),
                             dates=GUARD_DATES, seed=SEED)
    assert not report["ok"]
    assert report["mismatches"], "a signal reading tomorrow's close went undetected"


def test_the_guard_raises_on_a_cheat_when_asked_to_assert():
    with pytest.raises(lookahead.LookAheadError):
        lookahead.assert_causal(synthetic.cheating_signal,
                                synthetic.random_walk(600, seed=SEED),
                                dates=GUARD_DATES, seed=SEED)


def test_the_guard_is_deterministic():
    kwargs = dict(dates=GUARD_DATES, seed=SEED)
    frame = synthetic.random_walk(600, seed=SEED)
    first = lookahead.check(synthetic.cheating_signal, frame, **kwargs)
    second = lookahead.check(synthetic.cheating_signal, frame, **kwargs)
    assert first["mismatches"] == second["mismatches"]
