# The patient family, through the funnel

Twelve new ideas, one per existing rule: the same signal, taken only when
the move it is aiming for is at least 4x the 0.206% round trip, aiming 3x
the risk, at most one trade a day per stock.

Real NSE 5-minute bars, 40 sessions, each idea on the assets gate 1
entitles it to. Run `4`, fingerprint `c09d0ecb4972`.

**25 ideas in, 0 survivors.** Every patient variant died at gate 2, the
same gate as its base. `patient_score4` died at gate 1 for the same
reason `score4` does: kind `mixed` matches no asset label.

| idea | base trades | base net | patient trades | patient net | change |
| --- | ---: | ---: | ---: | ---: | ---: |
| bb_breakout | 70 | -1,068 | 41 | -348 | **+720** |
| ema_cross | 87 | -1,803 | 45 | -1,140 | +663 |
| gap_fade | 7 | -184 | 23 | -1,304 | **-1,120** |
| intraday_momentum | 36 | -868 | 26 | -830 | +38 |
| orb | 71 | -1,335 | 71 | -985 | +350 |
| orb_both | 115 | -3,387 | 115 | -3,035 | +353 |
| rsi_reversion | 53 | -1,534 | 72 | -1,715 | **-181** |
| supertrend | 109 | -2,069 | 92 | -1,886 | +183 |
| trend_pullback | 127 | -2,351 | 86 | -1,492 | +860 |
| vwap_bands | 232 | -5,309 | 117 | -1,774 | **+3,535** |
| vwap_reclaim | 156 | -1,983 | 116 | -1,286 | +698 |
| **total** | **1,063** | **-21,892** | **804** | **-15,794** | **+6,098** |

## The verdict

Nine of eleven lose less and the total loss falls 28%. That reads like
progress and is not, because of one number: **the loss per trade barely
moves, from -Rs20.59 to -Rs19.64.**

The family cut the loss by taking 24% fewer trades, not by taking better
ones. Doing less of something unprofitable is not an edge, it is a
smaller position in the same mistake. A rule that genuinely found the
bigger moves would show a smaller loss per trade, or a profit; this shows
the same loss, 259 times less often.

`vwap_bands` is the clearest case. Halving its trades from 232 to 117
recovered Rs3,535, which is most of the family's entire improvement, and
its loss per trade went from -Rs22.88 to -Rs15.16. Better, and still a
loss on every trade on average.

## Two that traded MORE, which is the interesting failure

`gap_fade` went from 7 trades to 23 and lost Rs1,120 more. `rsi_reversion`
went from 53 to 72. A family called "patient" trading more often needs
explaining, and the explanation is a real mechanism rather than a bug.

The engine already refuses a setup whose target cannot beat the round
trip by `min_reward_to_cost` (2x). Widening the target from the base's
multiple to 3x the risk makes setups that USED to fail that check pass
it. So the wider target let through trades the engine had been rejecting,
and those trades were bad.

This is the same effect that broke my first reading of the slippage
sweep: changing a cost or a target does not only change the arithmetic on
the trades taken, it changes which trades are taken at all. Any future
variant that widens a target has to be read with that in mind.

## What was not done

The locked window was not touched. Nothing reached gate 7, and under
AGENTS.md §1.6 it is spent once per idea after every choice is made.
`results/locked_test_log.csv` is unchanged.

No parameter was retuned to make a variant pass. `min_move_mult` 4 and
`target_r` 3 were chosen before the run from the cost arithmetic, not
after it from the results. Moving them now because of what this run
showed is what §1.5 forbids, and would make a new idea needing its own
run through all seven gates.
