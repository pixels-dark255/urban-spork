# Urban Spork

An AI-assisted analysis and intraday-trading platform for Indian equities
(NSE + BSE). It searches the full stock universe, analyses a stock across
ten intraday timeframes, and produces a complete, risk-checked trade idea —
entry, stop loss, target, position size, risk, reward, R:R and confidence —
or an explicit refusal, with its reasoning shown either way.

**Read this first — an honest framing.** Stock prices cannot be reliably
predicted, by this software or anyone else's. This platform combines
transparent, well-understood methods (technical analysis, statistics,
market regime, volume, and machine learning where it has *earned* the
right to vote), records every call it makes, and grades itself afterwards
so you can see what its confidence numbers are actually worth. It makes no
claim of guaranteed profits or accuracy. The design goal is fewer, better
signals — not more of them.

---

## 1. What it does

| Tab | What's there |
| --- | --- |
| **Search** | Fuzzy search across the whole NSE + BSE universe. Works with the market shut and with NSE/BSE unreachable. |
| **Intraday** | The recommendation engine. Pick a stock and one of ten timeframes; get a full trade plan or a reasoned "no trade". |
| **Market** | NIFTY / SENSEX / BANK NIFTY / India VIX, market breadth, top movers, sector performance — also fed back into every prediction. |
| **Paper** | Simulated trades with a live scorecard: win rate, average win vs average loss, profit factor, expectancy. |
| **More → Predictions** | Every recommendation ever made and how it turned out, sliced by timeframe, stock, regime, and confidence bucket. |
| **More → Settings** | Capital, risk per trade, daily loss cap, minimum R:R, confidence floor. |
| **More → Watchlist / Analysis** | The original multi-day forecast screens, with their 90-day backtest and tracked accuracy. |
| **More → Auto-simulation** | The original rule-based paper-trading bot, still running on 5m/15m/30m. |

---

## 2. How a recommendation is built

```
             Market data (provider layer)
                        |
                 Feature engineering
                        |
      Technical + ML + Statistical + Volume + Market context
                        |
             Adaptive ensemble (fusion)
                        |
               Risk management engine
                        |
        BUY / SELL / HOLD / NO TRADE + full plan
```

**1. Market data.** `providers/` is a swappable layer — Yahoo Finance today,
Kotak Neo stubbed for later. The prediction engine never imports a broker
SDK, so changing data source is a config change, not a rewrite.

**2. Indicators.** RSI, MACD, EMA (9/21/50), SMA, Bollinger Bands, ATR, ADX
with +DI/−DI, VWAP, OBV, volume profile, and clustered swing-pivot
support/resistance. All pure pandas — no TA library to install or go stale.

**3. Market regime.** Before predicting anything, classify the environment:
`STRONG_UPTREND / UPTREND / SIDEWAYS / DOWNTREND / STRONG_DOWNTREND`, plus
flags for high volatility, thin volume, and gap up/down. This matters
because the right strategy in each is different and mutually contradictory
— buying a breakout is correct in a strong trend and wrong in a range.
Trend-following, mean-reversion and breakout signals are re-weighted
accordingly.

**4. Five components vote**, each in [−1, +1] with its own stated reasons:
technical, ML, statistical, volume, and market context.

**5. Adaptive ensemble.** Weights are not fixed. They start from a sane
baseline, are tilted by the regime, and are then scaled by each component's
*measured* hit rate for this regime and timeframe — but only once there are
at least 20 resolved predictions to judge by, and always within bounds, so
one lucky streak can never hand a component the whole vote.

**6. Confidence** starts from the size of the combined score, is scaled by
how much the components actually agree, and is then penalised for thin
volume, extreme volatility, an uncertain regime read, missing components,
and approximated data.

**7. Risk management has the final word.** It sizes an ATR-based stop
(widened in high volatility, tightened to structure when a real level sits
closer), sets a target from the minimum R:R or the next real level,
computes position size from your capital and risk-per-trade, and **vetoes
the trade** if the R:R doesn't clear your floor, the size rounds to zero, a
level sits squarely in the path, your daily loss limit is spent, or
confidence is below your threshold. A veto is a first-class result with a
reason, not an error.

---

## 3. Timeframes

30s · 1m · 2m · 5m · 10m · 15m · 30m · 1h · 1.5h · 2h

Where the data source doesn't serve a timeframe natively (10m, 1.5h, 2h),
bars are rolled up from finer ones, aligned to each session's 09:15 open so
a 10-minute candle never splices two sessions together.

**30s is approximated.** No free provider serves sub-minute Indian equity
bars, so it runs on 1-minute data. It is flagged as approximated in the API
and marked with an asterisk in the UI, and its confidence is penalised —
rather than pretending to a resolution the data doesn't have.

---

## 4. Machine learning: the rule that matters

A model is allowed to influence a live recommendation **only if it beat a
majority-class baseline on data it never saw during training.** Everything
else is stored with `usable: false` and ignored by the ensemble. An
unvalidated model that quietly votes is worse than no model, because its
votes look identical to a good one's.

- Validation is a **chronological hold-out**, never a shuffled split.
  Shuffling a time series lets a model learn from its own future, which
  produces beautiful validation scores and a system that loses money.
- Labels are three-way (up / down / **neutral**), where neutral is any move
  smaller than a noise band scaled to that stock's own bar range. Training
  on raw sign teaches a model to call a 0.02% drift a "buy".
- Backends: XGBoost and LightGBM when installed, otherwise scikit-learn's
  RandomForest / GradientBoosting / logistic regression. The platform must
  run on a free tier, and a RandomForest that exists beats an XGBoost that
  doesn't.
- Retraining runs **daily after the close**, not after every trade. A model
  retrained on each new outcome chases the last hour of noise.
- No LSTM yet — deliberately. Adding a sequential model before the local
  bar database holds months of history would be theatre. The registry takes
  any classifier with `fit`/`predict_proba`, so one can be added without
  touching anything else.

Models report their top features, so a recommendation is explainable rather
than a black box.

---

## 5. Historical data

Every bar the platform fetches is written to a local SQLite database and
**never deleted** — not at the close, not over the weekend, not on restart.
The background collector adds to it every few minutes during market hours
for everything you track. That growing dataset is what makes ML training
possible at all, and it doubles as the offline fallback: when the market is
shut or the data source is unreachable, the app serves stored history
instead of an error.

SQLite by default, plain SQL with no ORM, so moving to PostgreSQL later is
a driver swap rather than a rewrite.

---

## 6. Running it

```bash
cd backend
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # optional - edit as needed
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000`. The backend serves the frontend, so it's one
process and one URL. On first run the stock universe is seeded from a
bundled file (search works instantly, with no network at all) and a live
NSE/BSE refresh runs in the background.

### Tests

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

The suite runs entirely on synthetic data with the network stubbed out, so
it passes at weekends, offline, and under a rate limit.

### Deploying to Render

Render picks up `backend/render.yaml`. Set `NEWS_API_KEY` (optional) and
`DATABASE_URL` (optional) in the Environment tab.

Two caveats on the free tier: services spin down after ~15 minutes idle, so
the collector and scheduler pause while asleep; and the disk resets on
redeploy, which takes the collected bar database with it. For continuous
collection, either use a paid tier with a persistent disk or point
`MARKET_DB_PATH` at one.

### Install on a phone

Open the URL in Chrome → **⋮** → **Add to Home screen**. It runs full-screen
as a PWA.

---

## 7. API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/stocks/search?q=` | Fuzzy search the NSE + BSE universe |
| `GET /api/timeframes` | Supported timeframes and their horizons |
| `GET /api/intraday/analyze?symbol=&timeframe=` | Full recommendation + trade plan |
| `POST /api/intraday/scan` | Same, across up to 25 symbols |
| `GET/POST /api/settings` | Risk configuration |
| `GET /api/paper/positions`, `POST /api/paper/open`, `POST /api/paper/close/{id}` | Paper trading |
| `GET /api/predictions`, `GET /api/predictions/accuracy` | History and the report card |
| `GET /api/market/overview` | Indices, breadth, movers, sectors |
| `GET /api/models`, `POST /api/models/train` | ML registry |
| `GET /api/data/status`, `POST /api/data/collect` | Historical bar store |
| `GET /api/platform/status` | Providers, universe, data and model counts |

The original watchlist and analysis endpoints (`/api/watchlist`,
`/api/stocks/{symbol}/analyze`, `/api/intraday/stocks`) are unchanged.

---

## 8. Secrets

Broker credentials, API keys and database URLs are read from environment
variables only, via `config.py`. `.env` is git-ignored. Nothing sensitive
belongs in source, and `providers/kotak_neo.py` contains **no order-placement
code at all** — that is Phase 6 of the roadmap, gated behind extensive
validation and a manual-approval default. An untested order path is the
most expensive kind of bug this project could ship.

---

## 9. Roadmap

- **Phase 1 — done.** Full NSE/BSE search, persistent universe, improved
  prediction engine.
- **Phase 2 — done.** Intraday tab, all ten timeframes, stop loss, target,
  position sizing, risk/reward.
- **Phase 3 — done.** Historical intraday database, collection pipeline,
  paper trading, prediction grading. (A dedicated backtesting screen for the
  intraday engine is still to come; the 90-day backtest currently covers the
  multi-day watchlist engine only.)
- **Phase 4 — in progress.** Hybrid ML, ensemble fusion, regime detection
  and adaptive weighting are all live. What they need now is *data*: the
  models get useful only once the collector has built up months of local
  history.
- **Phase 5 — pending.** Kotak Neo integration for live prices, historical
  data and WebSocket streaming.
- **Phase 6 — pending.** Optional real execution, manual approval by
  default, automated only after extensive validation.

---

## 10. What this software will not do

- Claim guaranteed profits or guaranteed accuracy.
- Let an unvalidated model vote on a live recommendation.
- Emit a trade it cannot give a sane stop, a worthwhile target and a
  survivable size.
- Trade past your daily loss limit.
- Hide its reasoning.

It will say **NO TRADE** often. Most market moments do not contain a good
trade, and an engine that always finds one is not being clever.
