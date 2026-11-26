# AGENTS.md: house rules for TickerBoard

Read this before writing code. It binds humans and agents equally.

The project is a paper-trading research system for Indian intraday equities:
a FastAPI backend, a vanilla-JS PWA dashboard, and a Strategy Arena that runs
many strategies side by side on simulated money.

---

## 1. Hard rules

These are not preferences. A change that breaks one of them is wrong even if
the tests pass and the numbers look good.

### 1.1 Paper trading only
No live broker order placement, ever. No order-routing library, no broker
session, no "just a dry-run flag that could be flipped". The simulator may
produce an order *ticket* as data; nothing may send one.

Never read, print, or log `.env`. Secrets are typed into `.env` by a human and
by nobody else. `.env` is git-ignored and stays that way. `.env.example`
carries the variable names with empty values, and nothing else.

### 1.2 One signal module
The backtest and the live paper runner import the SAME signal functions from
`backend/arena/strategies.py`. There is no second copy of a rule. If a
backtest needs a rule the runner does not have, the rule goes in the shared
module and both use it.

Today this holds: `engine.replay()` drives `start_day` / `process_bars` /
`settle_day`, which is the identical code path the live runner takes. Keep it
that way. A backtest that re-implements a rule is a bug, not an optimisation.

### 1.3 Next-open fills
A strategy decides on a **completed** bar and fills at the **next** bar's
open. Never fill at the bar that produced the signal.

**True since Phase 9.** `_maybe_enter` parks the signal and `_fill_pending`
fills it at the next bar's open. Stops, targets and the square-off keep
their intrabar logic, because those are events rather than decisions, and
a gap through a stop fills at the open.

Two consequences worth knowing. A signal on the session's last tradeable
bar has no next open, so it is not taken and is counted as `no_next_bar`.
An open beyond the signal's own stop invalidates the setup, so it is
dropped as `gapped_through` rather than booked as a trade that was stopped
at birth.

### 1.4 Costs are always on
Every fill pays the Indian cost model in `backend/arena/costs.py`: brokerage
(0.03% capped at ₹20), STT, exchange transaction, SEBI, GST, stamp duty, plus
slippage. Non-Indian markets use their own `CostModel` in
`backend/arena/markets.py`.

There is no "costs off" mode and no result quoted gross. **Every result is
also reported at 2x cost.** The sweep lives in `engine.SLIPPAGE_MULTIPLES`.

**True since Phase 9.** `SLIPPAGE_MULTIPLES = [0.0, 0.5, 1.0, 2.0]`, and the
funnel's gate 6 reads the top of that list rather than carrying its own
number, so the two cannot drift apart.

### 1.5 No rule changes because of recent results
Losing last week is not a reason to touch a rule. Tuning a threshold because
of what just happened is how a backtest becomes a memory of the past.

A rule change is a **new idea** with a new id, and it goes through the full
funnel. The old rule keeps running until the new one has earned its place.

### 1.6 The locked window is run ONCE
Each idea gets exactly one run on the locked test window, after every choice
about it has already been made. If it fails, the idea is out. There is no
"tweak and rerun": that is the same as having no test at all.

Every locked-window run is appended to `results/locked_test_log.csv`
(`idea_id, date_run, result, notes`) so that a rerun is visible to anyone
reading the file. An idea appearing twice is a red flag, not a detail.

### 1.7 Deterministic output
Same input, same bytes out. Fixed seeds, sorted iteration over every dict and
set that reaches a result, no reliance on insertion order or wall-clock time
inside a computation.

Each phase prints a **fingerprint**: a short hash of its key result numbers,
so two runs can be compared at a glance.

### 1.8 What finishes a phase
A phase is done when all three are true:
1. tests pass (backend and frontend),
2. a small table or chart for it exists on the dashboard,
3. a one-paragraph plain-English summary says what was built, what was
   chosen, and what was deliberately left undone.

---

## 2. My settings

Every later phase reads these values from this block. Nothing hardcodes a
capital figure, a window or a ticker list. `backend/settings.py` parses it;
`python3.13 -m settings` prints what it parsed and checks every ticker.

```yaml
market: NSE
timezone: Asia/Kolkata
session: 09:15-15:30 IST, intraday square-off by 15:15
bar_sizes: [1d, 1h, 15m, 5m]          # daily is the default for proof work
default_bar_size: 1d
study_tickers: [RELIANCE.NS, HDFCBANK.NS, ICICIBANK.NS, INFY.NS, TCS.NS,
                SBIN.NS, AXISBANK.NS, ITC.NS, BHARTIARTL.NS, TATAMOTORS.NS,
                TATASTEEL.NS, HINDUNILVR.NS]
engine_candidates: [NIFTYBEES.NS, JUNIORBEES.NS, BANKBEES.NS, ITBEES.NS]
safe_candidates: [GOLDBEES.NS, LIQUIDBEES.NS, GILT5YBEES.NS, CASH]
daily_capital_inr: 10000              # intraday paper budget per day
swing_capital_inr: 100000             # core+sleeve paper account
train_window_daily: 2022-01-01 .. 2024-12-31
locked_window_daily: 2025-01-01 .. today
cost_model: existing Indian cost legs + slippage; stress at 2x
kill_line_multiple: 2.0               # x worst tested drawdown

# Data-length rules. Yahoo keeps ~2 years of 1h bars and only ~60 days of
# 5m/15m, which is far too little to prove anything intraday.
hourly_train_fraction: 0.70           # 1h: first 70% train, last 30% locked
unproven_bar_sizes: [15m, 5m]         # labelled UNPROVEN on the dashboard
min_months_for_funnel: 12             # stored history before a bar size qualifies
```

`study_tickers` is the Arena's current NSE set. The per-client watchlist
lives in Postgres rather than in the repo, so it cannot be pinned here; add
a ticker to this list when you want it studied.

`CASH` in `safe_candidates` is deliberate and is not a ticker. It means
holding rupees, and it never resolves on a data source.

**The check.** `cd backend && python3.13 -m settings` prints the parsed
settings, resolves every ticker, lists the open hard rules and prints the
phase fingerprint. Add `--offline` to skip the ticker probe where there is
no network. The same probe runs in CI on the Bar archive workflow, which is
the only place with reliable access to the data source.

---

## 3. Repo map

Reuse these. Do not create a parallel copy of any of them.

| Role | Module |
| --- | --- |
| Data download | `backend/data_sources.py`: every external fetch (yfinance, news), caching, symbol search, `_yf_download` |
| | `backend/arena/universe.py`: daily bars for screening (`fetch_daily`), per-process cache |
| Indicators | `backend/indicators.py`: SMA, EMA, RSI, MACD, Bollinger, volatility (pure pandas) |
| **Signals** | `backend/arena/strategies.py`: `enrich()` plus every `Strategy` class and `catalog()`. **The one signal module** (§1.2) |
| Arena strategies | same file: ORB both ways, SuperTrend flip, VWAP band reversion, EMA pullback, gap fade, intraday momentum, VWAP reclaim, EMA crossover, RSI reversion, Bollinger breakout, legacy score, buy-at-open benchmark |
| Cost model | `backend/arena/costs.py` (India) · `backend/arena/markets.py` (`CostModel`, `USCostModel`, `ForexCostModel`, `CryptoCostModel`) |
| Paper fills | `backend/arena/engine.py`: `_maybe_enter`, `_exit_price`, `process_bar(s)`, position sizing, square-off |
| Backtest | `backend/arena/engine.py::replay`: the live path over past sessions. (`backend/backtest.py` is **not** this: it is the watchlist predictor's 90-day weight refinement) |
| Persistence | `backend/arena/store.py`: arena state per client id, Postgres with a JSON-file fallback · `backend/storage.py`: watchlist and intraday |
| Scheduler | `.github/workflows/arena-runner.yml` + `backend/run_daily.py`: the scheduled runner that actually advances the arena · `backend/scheduler.py`: the in-process APScheduler · `backend/arena/runner.py`: which of the two is in charge (`ARENA_RUNNER`) |
| Market calendar | `backend/market_calendar.py` + `backend/nse_holidays.json` (NSE) · `backend/arena/markets.py` (other venues) · `backend/timeutil.py` (one source of truth for time) |
| Settings | `backend/settings.py` reads §2 above. `python3.13 -m settings` prints and checks it |
| Ticker check | `backend/ticker_check.py`: resolves every ticker; flags, never drops |
| History archive | `backend/bar_archive.py`, `backend/archive_bars.py`, `.github/workflows/bar-archive.yml` |
| Fingerprint | `backend/fingerprint.py`: one comparable line per phase |
| Regime | `backend/signals/regime.py`: trend and vol per bar; one definition, shared with the study |
| Rule spec | `rules.yaml` + `backend/signals/rules.py`: the 7-part schema. `python3.13 -m signals.rules` prints the table |
| Adapt | `backend/adapt/` + `CHANGELOG_rules.md`: Phase 8. `python -m adapt.cli` prints the kill lines, the status table and a dry-run rotate. The scheduled review may fix a code bug, never a rule |
| AI layer | `backend/ai/` + `ai_calls/`: Phase 7, **off**. `python -m ai.cli` runs the two replays and prints the enable verdict. Core only; four calls; every call recorded before it can act |
| Harness | `backend/harness/` + `.github/workflows/harness.yml`: Phase 6. `python -m harness.cli` prints the dry run, the risk limits and the rerun check. Paper only; the trust ladder's top rung writes a paper fill and nothing else |
| Swing account | `backend/swing/`: Phase 5. `python -m swing.cli` runs the core rotation, the quarterly picker and the sleeves. Delivery costs, separate from the Arena's intraday book |
| Funnel | `backend/funnel/` + `bench.yaml`: Phase 4's seven gates. `python -m funnel.cli` runs them all; `python -m tools.test_idea <idea.yaml>` runs one |
| Asset labels | `results/asset_labels.json`: Phase 1's output, committed so gate 1 is reproducible |
| Fill rule | `backend/validation/fills.py` + `backend/run_phase9.py`: Phase 9. `python3.13 run_phase9.py` recomputes every entry from the bars and prints the two drop reasons and the slippage sweep |
| Proof | `backend/validation/` + `backend/run_proof.py`: Phase 3. `python3.13 run_proof.py` prints the three synthetic results against the bands, the guard per strategy, and the fingerprint |
| Dashboard | `frontend/arena.js`, `frontend/app.js`, `frontend/index.html`, `frontend/style.css` |
| API | `backend/main.py`: FastAPI routes, including `/api/arena/*` |

---

## 4. Conventions

- **Tests.** Backend: `cd backend && python3.13 -m pytest -q`. Frontend:
  `cd frontend && node --test app.helpers.test.js arena.helpers.test.js`.
  Both must pass before a commit.
- **Costs.** Intraday pays `costs.DEFAULT_COSTS` (MIS). Anything held
  overnight pays `costs.DELIVERY_COSTS` (CNC), where STT is 0.1% on
  **both** legs rather than 0.025% on the sell. A swing result charged
  intraday rates is wrong by roughly 0.19% per round trip.
- **Money.** Indian results are in rupees (`₹`). A market with another quote
  currency carries its own symbol and an INR conversion; never mix them in
  one column without labelling.
- **Time.** Always IST for the NSE, via `timeutil` / `market_calendar`. Never
  `datetime.now()` without a zone.
- **Holidays.** `backend/nse_holidays.json` is the single source. Anything
  needing the NSE calendar reads it rather than hard-coding dates.
- **Comments** explain *why*, not *what*. The repo's existing comments are the
  standard to match.
- **Alert credentials.** `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` come
  from the environment, filled in by a human through `.env`. The token is
  never logged: Telegram puts it in the request URL, so
  `harness/alerts.py::redact` scrubs it from anything heading for a log.
  With neither set the harness runs and sends nothing; an unconfigured
  alerter must never be able to stop a run.
- **No secrets in source**, in commits, or in logs.

---

## 5. Known rule violations

Open gaps against §1, recorded so they are fixed deliberately rather than
discovered by surprise.

| Rule | Gap | Impact |
| --- | --- | --- |
| AI layer cannot be enabled | Phase 7 (fingerprint `e8a5000d1c16`). Three conditions must hold and none does: six §1 rules are open, the core failed its own gates, and the masked replay did not beat core alone | The layer is built, tested and sealed off. `replay.may_enable` refuses even when the replay is flattering, because improving a core that failed its gates proves nothing worth acting on |
| Core rotation fails its gates | Phase 5 on real bars (fingerprint `caaf2427ec36`): engine `ITBEES.NS`, safe `GOLDBEES.NS`, SMA200 returns **+0.70%/yr against a −28.52% drawdown** (ret/dd 0.03). **Neighbours FAIL** (SMA150 loses 8.06% while SMA100 makes 28% and SMA250 makes 59%), and **2x cost FAILS** at −5.73% | The locked window was deliberately **not** spent: the idea is already out, and §1.6 allows one run per idea. Switching to SMA250 because it scored best is forbidden by §1.5 and would be a new idea needing the full funnel |
| Trading less is not the answer either | `results/patient_family.md`. Widening a target also changes WHICH trades are taken, because the engine's `min_reward_to_cost` stops rejecting the marginal ones: two of the twelve variants traded MORE than their base and lost more | Any future variant that widens a target has to be read with that in mind, and a drop in total loss means nothing on its own. Loss per trade is the number to watch |
| Slippage is not the problem | The `4-sweep` run on real bars (fingerprint `e23d1fb22a79`) gives every idea perfect fills, and all eleven still lose. Free fills recover Rs8,884 of a Rs21,892 loss and nothing crosses zero; before statutory charges the whole set makes about Rs1 a trade on a Rs12,500 position | `results/slippage_sweep.md`. The cost model is not hiding an edge. Relaxing costs to make a strategy pass would be relaxing the only part of this that is measured |
| Nothing has earned capital | Phase 4 on real NSE 5m bars (40 sessions, fingerprint `c09d0ecb4972`): **25 ideas in, 0 survivors**, every one dead at gate 2. That includes the twelve patient variants built to trade less: they cut the loss 28% by taking 24% fewer trades, while the loss per trade moved only from Rs20.59 to Rs19.64 | `bench.yaml` has an empty `survivors` list. Every strategy is observe-only, with no capital, until one clears the gates |
| Gate 5 is partial | Indicator windows (the 9 and 21 in `ema9`/`ema21`, the 20 in the Bollinger bands) are computed once in `enrich()` for every strategy at once, so varying one per strategy would mean re-enriching per variant. Gate 5 varies the rule parameters only | A single-spike optimum hiding in an indicator window would not be caught |
| Regime not wired in | `signals/regime.py` computes trend and volatility, and `rules.yaml` declares the half-size rule, but `engine.py` consults neither. Every filter reads `none` | The regime layer is specified and tested but decorative until the engine asks |
| India VIX unverified | The brief allows it only if a free reliable source exists. This container's egress policy blocks the data source, so `^INDIAVIX` could not be resolved | `vix_state` exists, pure and tested, but `classify()` does not call it and no rule may filter on vix |
| `TATAMOTORS.NS` is dead | Yahoo answers "No data found, symbol may be delisted". It is still in `study_tickers` AND in the Arena's `default_symbols` in `arena/markets.py` | Phase 1 studied 11 tickers, not 12. The live Arena has been trying to trade a symbol that returns nothing. Not replaced by guesswork: the successor symbol is a deliberate choice |
| Overlapping events | A new 50-day high can occur on consecutive days, so one trend contributes many "events". Phase 1's counts are not independent observations | A label with 86 events may rest on far fewer distinct episodes, so the 6 / 12 thresholds are weaker evidence than they look |
| Folder naming | `backend/data/` holds **live** JSON state despite the name, which is backwards from the `data/` (frozen research) vs `data_live/` convention | Confusing; moving it would restart the running store, so it is flagged rather than renamed mid-flight |
| Top-level `tests/` | Not created. `backend/tests/` already holds the suite; a second empty `tests/` at the root would be a trap for whoever adds the next test | None, as long as nobody adds one |
| Arena is UNPROVEN | The Arena trades 5m bars, and Yahoo keeps ~60 days of those | Every Arena result is labelled UNPROVEN until the archive has 12 months |
| §1.7 determinism | `fingerprint.py` exists and Phase 0 prints a line, but the simulation declares no seed and emits no fingerprint | Two Arena runs still cannot be compared mechanically |
| §1.7 on real bars | The funnel's Yahoo input is not fixed: the 5-minute window rolls and a symbol can fail to download on one run and not the next. Two Phase 4 runs five minutes apart differed on six of eleven ideas | Since Phase 9 the run prints the bars per symbol and hashes them into the fingerprint, so data drift is visible. It is not removed: only the bar archive can do that |
