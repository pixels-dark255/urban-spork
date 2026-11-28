# The same ideas on hourly bars

All 25 ideas on NSE 1-hour bars instead of 5-minute ones. Run `4-hourly`,
fingerprint `a167d2e71779`.

**268 training sessions, 2024-11-28 to 2025-12-26.** 116 sessions
(2025-12-29 to 2026-06-19) were split off before gate 1 and never loaded,
which is the 70/30 split AGENTS.md §2 fixes for hourly bars. Gate 7 was
not run and the locked window is unspent.

Hourly is the first bar size that clears `min_months_for_funnel: 12`.
Nineteen months of history, against sixty days for 5m and 15m, which is
why those two are labelled UNPROVEN and this one is not.

**25 ideas in, 0 survivors.** Every one died at gate 2.

## Per trade, which is the only fair comparison

The two runs cover different spans, 40 sessions against 268, so totals
cannot be set side by side. Loss per trade can.

| idea | 5m trades | 5m loss/trade | 1h trades | 1h loss/trade |
| --- | ---: | ---: | ---: | ---: |
| bb_breakout | 70 | -15.25 | 57 | -18.81 |
| ema_cross | 87 | -20.72 | 62 | **-9.13** |
| gap_fade | 7 | -26.32 | 23 | **-1.38** |
| orb | 71 | -18.80 | 267 | -26.45 |
| orb_both | 115 | -29.46 | 537 | -25.62 |
| rsi_reversion | 53 | -28.95 | 112 | -25.96 |
| supertrend | 109 | -18.98 | 75 | -29.88 |
| trend_pullback | 127 | -18.52 | 119 | -21.72 |
| vwap_reclaim | 156 | -12.71 | 469 | -24.58 |
| **all** | **795** | **-19.77** | **1,721** | **-24.26** |

**Hourly is worse per trade, by 23%.** That is the finding, and it is the
opposite of what the bar size was tried for. The idea was that a longer
bar holds a bigger move, so a trade would have more room to cover the
0.206% round trip. Each hourly idea does trade less often, 0.71 times a
session against 2.21, and loses more on each one.

Two ideas improved sharply, `ema_cross` and `gap_fade`, and neither is
close to profitable. On 23 trades `gap_fade` is noise either way.

## Seven bars

An NSE session is 09:15 to 15:30. On hourly bars that is **seven bars**,
and a rule written for intraday structure has almost nothing to work
with. Two ideas took no trades at all, and the funnel now says which kind
of nothing each one was:

- `intraday_momentum`: **212 signals, every one dropped as `no_next_bar`.**
  It fires in the last hour by construction, which on hourly bars is the
  final bar of the session, and there is no next open to fill at. Not a
  bug: §1.3 refusing to fill a decision at the bar that produced it. The
  rule is structurally impossible at this bar size.
- `vwap_bands`: **22 signals in 268 sessions, every one refused** because
  the target could not beat the round trip twice over. At 5m this rule is
  the busiest in the funnel at 232 trades in 40 sessions. On hourly bars
  it barely speaks, and nothing it says is worth acting on.

A session-expanding standard deviation, which is what the VWAP bands are
built on, has between three and seven observations on hourly bars. The
floor was raised to three so the rule could run at all, and three points
is thin evidence. Read `patient_vwap_bands`'s 30 trades with that in mind.

## What the run exposed in the code

Three faults, all of which had been invisible at 5m and all fixed before
these numbers were taken:

1. The engine truncates a book's stored trades at 300 to bound a Postgres
   row. That was being applied to backtests too, so `orb_both` reported
   300 trades and -9,357 when the truth was 537 trades and -13,756. A
   replay now keeps every trade; the live book still caps.
2. A rule that took no trades was reported as "net +0.00 after costs over
   0 trades", which reads as breaking even.
3. The first fix for (2) then claimed "the rule never fired", which was
   false for `vwap_bands`: it fires and the engine refuses it. Gate 2 now
   reads the engine's skip counters and says which it was.

## A gap in the data

The hourly feed returned 2024-11-28 to 2026-06-19 against a two-year
request made on 2026-11-28, so the most recent five months are missing.
The split is still an honest hold-out, but the locked window is the last
30% of what the source served, not the last 30% up to today. Worth
checking before gate 7 is ever spent on it.
