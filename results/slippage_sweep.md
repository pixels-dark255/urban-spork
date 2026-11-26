# What the fills cost, measured

Phase 4's eleven ideas on real NSE 5-minute bars, 40 sessions, each on the
assets gate 1 entitles it to, run at four fill-cost assumptions. Statutory
charges (brokerage, STT, GST, exchange, SEBI, stamp duty) stay on at every
multiple. Only the slippage assumption moves.

Run `4-sweep`, fingerprint `e23d1fb22a79`. The 1x column reproduces the
funnel's gate 2 number for all eleven ideas, which is how we know the
sweep and the funnel are measuring the same thing.

| idea | trades | net @ 0x | net @ 0.5x | net @ 1x | net @ 2x |
| --- | ---: | ---: | ---: | ---: | ---: |
| bb_breakout | 70 | -368.24 | -863.19 | -1067.75 | -376.09 |
| ema_cross | 87 | -1057.52 | -1834.85 | -1802.67 | -1543.79 |
| gap_fade | 7 | -207.76 | -392.42 | -184.22 | **+207.46** |
| intraday_momentum | 36 | -476.10 | -703.84 | -868.13 | -1173.64 |
| orb | 71 | -499.99 | -917.36 | -1334.72 | -2163.16 |
| orb_both | 115 | -2033.97 | -2710.71 | -3387.45 | -4734.64 |
| rsi_reversion | 53 | -2804.54 | -2554.77 | -1534.11 | -1070.16 |
| supertrend | 109 | -782.37 | -1425.61 | -2068.85 | -3350.32 |
| trend_pullback | 127 | -1309.46 | -2189.07 | -2351.43 | -1528.44 |
| vwap_bands | 232 | -3070.70 | -4371.01 | -5309.13 | -6329.39 |
| vwap_reclaim | 156 | -396.70 | -1346.61 | -1983.34 | -1740.67 |
| **profitable** | | **0 of 11** | **0 of 11** | **0 of 11** | **1 of 11** |
| **total** | 1,063 | **-13,007** | | **-21,892** | |

## What it settles

**Slippage is not the reason these lose.** Hand every idea perfect fills,
no spread and no impact, and all eleven still lose money. Free fills
recover Rs8,884 of a Rs21,892 loss and not one idea crosses zero.

Removing slippage leaves about Rs1,077 across 1,063 trades before
statutory charges, which is roughly Rs1 a trade on a Rs12,500 position:
0.008%. Four of the eleven are negative even there. There is no edge
being eaten by costs, because there is almost no edge.

This replaces an earlier estimate that reconstructed a "gross" figure by
adding a modelled cost back onto each trade and concluded nine of eleven
had a positive gross edge. That arithmetic was wrong, and measuring
rather than reconstructing is what showed it. Charging less does not only
return money: it changes which trades the engine takes at all, because
`min_reward_to_cost` stops rejecting the marginal ones.

## Two rows that read backwards

`gap_fade` is the only profitable cell in the table, at 2x, on **7
trades** against gate 3's 40-trade minimum. It is noise with a plus sign.

`rsi_reversion` and `gap_fade` lose less as costs rise, which looks like
nonsense and is not. A higher assumed cost makes `min_reward_to_cost`
reject more setups, so the book takes fewer trades and skips some losers.
That is a selection effect of the filter, not an edge, and a strategy
whose results improve when you charge it more is a strategy with nothing
to charge for.

## What this does not say

Slippage at 1x is 0.05% per leg. The sweep shows what happens if that
assumption is wrong in the optimistic direction, which answers "are we
being too harsh". It cannot tell you what your real fills would be,
because nobody has measured them. That needs a broker's fill reports
against the signal prices, and no such data exists here.
