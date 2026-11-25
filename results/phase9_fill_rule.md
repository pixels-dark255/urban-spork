# Phase 9: what the next-open rule cost

Before Phase 9 a strategy decided on a bar and was filled on that same
bar. Now it is filled at the next bar's open (AGENTS.md §1.3). This is
the same funnel, the same eleven NSE symbols and the same 40 sessions of
real 5-minute bars, run once on each engine.

Kept here rather than only in the CI artifact, because the artifact store
is not reachable from everywhere the result is needed, and the earlier
run's profit factors and drawdowns were already lost that way.

| run | fingerprint | when | engine |
| --- | --- | --- | --- |
| before | `04dcaf3a560a` | 2026-11-25 04:27 UTC | same-bar fills |
| after | `26d138138002` | 2026-11-25 18:01 UTC | next-open fills |

## Net after costs, per idea

| idea | trades before | trades after | net before (₹) | net after (₹) | change (₹) |
| --- | ---: | ---: | ---: | ---: | ---: |
| bb_breakout | 64 | 66 | -1018.03 | -1068.15 | -50.12 |
| ema_cross | 82 | 82 | -1733.52 | -1748.75 | -15.23 |
| intraday_momentum | 35 | 34 | -741.96 | -692.68 | +49.28 |
| orb | 70 | 71 | -1427.97 | -1743.85 | -315.88 |
| orb_both | 113 | 114 | -3458.43 | -3661.12 | -202.69 |
| rsi_reversion | 54 | 53 | -1427.91 | -1534.11 | -106.20 |
| supertrend | 106 | 108 | -2195.76 | -2189.63 | +6.13 |
| trend_pullback | 123 | 125 | -1838.71 | -2000.74 | -162.03 |
| vwap_bands | 222 | 228 | -5776.35 | -5660.21 | +116.14 |
| vwap_reclaim | 147 | 152 | -1710.64 | -1905.58 | -194.94 |
| **total** | **1016** | **1033** | **-21329.28** | **-22204.82** | **-875.54** |

Seven of the ten got worse, by ₹875 in total, which is the direction a
rule that removes free money is supposed to move results.

`gap_fade` is left out of the table because it changed gate. Before, it
passed gate 2 (profitable) and died at gate 3 on 7 trades against a
40-trade minimum. After, it loses ₹115.73 over 8 trades and dies at gate
2. It was the only idea in the whole funnel that had ever been
profitable; on next-open fills, none has been.

## Profit factor and drawdown, after

Only the "after" run has these: the earlier run stored them in a CI
artifact and nothing else.

| idea | profit factor | max drawdown (₹) |
| --- | ---: | ---: |
| bb_breakout | 0.58 | -1092.60 |
| ema_cross | 0.41 | -1839.63 |
| gap_fade | 0.80 | -411.74 |
| intraday_momentum | 0.36 | -694.14 |
| orb | 0.61 | -1833.88 |
| orb_both | 0.52 | -3856.23 |
| rsi_reversion | 0.39 | -1568.49 |
| supertrend | 0.55 | -2759.38 |
| trend_pullback | 0.58 | -2000.74 |
| vwap_bands | 0.36 | -5890.25 |
| vwap_reclaim | 0.66 | -2083.57 |

Every one is below 1.0, so every one loses more than it makes.

## How much of this is the rule and how much is the data

Some of it is the data. The two runs are thirteen hours apart, so the
40-session window moved on by one session, and the trade counts move with
it. Worse, a third run of the identical commit four minutes before the
"after" run disagreed with it on six of the eleven ideas (fingerprint
`7f5c755ac920`), which is a property of the source rather than of the
engine: Yahoo's 5-minute window rolls and a symbol can fail to download
on one run and not the next.

Two later runs of the same commit then agreed exactly, so this is
occasional rather than constant. Since Phase 9 the funnel prints the bars
and sessions it got per symbol, names any symbol that came back empty,
and hashes the bar counts into its fingerprint, so next time the question
can be answered rather than estimated. The archive in
`backend/bar_archive.py` is the only thing that removes the drift.

Read the table as a direction and a rough size, not as a measurement to
the rupee.
