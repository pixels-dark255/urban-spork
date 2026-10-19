# Tickerboard — NSE/BSE stock analyser & predictor (personal use)

A local-ish personal app: a Python backend that gathers NSE/BSE stock data,
news, and a weather feed, runs an ensemble prediction, and tracks its own
accuracy over time — plus a phone-installable web app (PWA) frontend.

**Read this first — an honest framing:** stock prices cannot be reliably
predicted, by this app or anyone else's. This tool gives you a transparent,
data-backed *estimate* with a visible confidence range, and it grades its own
past predictions so you can see for yourself how good they actually are for a
given stock and horizon. Treat it as a research aid, not a trading signal.

---

## 1. What's inside

```
stock-analyzer/
  render.yaml        Render Blueprint (must stay at the repo root)
  backend/          FastAPI app — data fetching, prediction engine, watchlist scheduler
    main.py
    data_sources.py   NSE/BSE list, yfinance prices, news, weather
    indicators.py     RSI / MACD / Bollinger / volatility
    predictor.py       the ensemble prediction engine
    scheduler.py       background job that keeps watchlist predictions live
    storage.py         watchlist + prediction persistence (JSON file or Postgres)
    market_calendar.py NSE session hours + trading holidays (one shared rule)
    us_holidays.json   NYSE closures + half days (2026-2027)
    timeutil.py        timezone-aware UTC helpers
    jsonsafe.py        strips NaN/inf before JSON or storage
    nse_holidays.json  trading-holiday dates (see "Trading holidays" below)
    arena/             Strategy Arena: strategies competing with simulated money
      markets.py       market profiles: sessions, costs, currencies, windows
    requirements.txt
    tests/             pytest suite (no network required)
    .env.example
  frontend/          Mobile PWA (installs to your phone's home screen)
    index.html / style.css / app.js / arena.js
    manifest.json / sw.js
    icons/
```

The backend also **serves the frontend** — one deployment, one URL.

---

## 2. Get your free API key (5 minutes)

The app works without this (falls back to keyless Google News RSS), but a
real key gives better news coverage:

1. Go to https://newsapi.org/register — sign up free (100 requests/day).
2. Copy your API key.
3. You'll paste it into Render's environment variables in step 4.

Weather (Open-Meteo) and price data (Yahoo Finance via `yfinance`) need **no
key at all**.

---

## 3. Test it locally first (optional but recommended)

You'll need Python 3.11, 3.12 or 3.13.

(numpy and pandas are version *ranges* rather than exact pins: the previous
`numpy==1.26.4` / `pandas==2.2.3` have no Python 3.13 wheels, so `pip
install` either failed outright or spent minutes compiling them.)

```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then edit .env and paste your NEWS_API_KEY
uvicorn main:app --reload --port 8000
```

Open `http://localhost:8000` in your browser (or on your phone if it's on
the same wifi, using your computer's local IP instead of `localhost`).

Run the tests with:

```bash
pip install pytest pyflakes httpx
python -m pytest tests/ -q          # 223 backend tests
python -m pyflakes .
cd ../frontend && node --test app.helpers.test.js arena.helpers.test.js   # 46 frontend tests
```

The suite stubs the market data source, so it needs no network and passes
at weekends and offline.

To click around the real PWA without a live data source, there's a dev
server that serves the app with synthetic market data:

```bash
cd backend && TZ=Asia/Kolkata python tests/devserver.py --port 8131
```

---

## 4. Deploy to Render (free) so it runs continuously

`render.yaml` sits at the **repository root**, which is the only place
Render looks for a Blueprint. It sets `rootDir: backend`, so the build and
start commands run from `backend/` while the blueprint stays where Render
can find it.

### First deploy

1. Go to https://render.com and sign up (free).
2. **New → Blueprint**, connect this GitHub repository, pick the `main`
   branch. Render reads `render.yaml` and proposes a `tickerboard` web
   service — no manual build/start commands needed.
3. Fill in the two env vars marked `sync: false`:
   - `DATABASE_URL` — see below. Set this.
   - `NEWS_API_KEY` — optional; without it news falls back to keyless
     Google News RSS.
4. **Apply**. First build takes a few minutes. Render gives you a URL like
   `https://tickerboard-xxxx.onrender.com`.

After that, **every push to `main` redeploys automatically** — that is the
deploy mechanism, so there is nothing else to run.

Health check: `/api/health`, already wired into the blueprint.

### Set DATABASE_URL — it matters more than it used to

Without it, the watchlist, the intraday paper portfolios and the Strategy
Arena are all JSON files under `backend/data/`, and the free tier wipes
that disk on every redeploy *and* every spin-down. Point it at a free
Postgres from [supabase.com](https://supabase.com) or
[neon.tech](https://neon.tech) and all three persist (tables
`watchlist_store`, `intraday_store`, `arena_store`). See `.env.example`.

### Free tier sleeps — what that costs, feature by feature

Free web services spin down after ~15 minutes without traffic and take
30-60s to wake. The scheduler is asleep for that whole time, which affects
the three features differently:

- **Watchlist predictions** resolve against the price *at* their target
  time, so a nap costs nothing — they are graded correctly whenever the
  service next wakes.
- **Intraday paper trading** reads live 5-minute bars and keeps no
  historical catch-up, so trades that would have triggered during a nap
  simply do not happen.
- **The Strategy Arena** replays what it slept through. On the first
  request after a wake it refetches Yahoo's 5-minute bars and runs every
  missed session through the same engine, in order, before touching today
  — see the next section.

### Running the Arena live on Render free tier

The Arena is built to survive the free tier rather than fight it. You need
one thing, and one more is nice to have:

1. **Set `DATABASE_URL`** (free Postgres from [neon.tech](https://neon.tech)
   or [supabase.com](https://supabase.com)). This is not optional for the
   Arena: without it the state is a JSON file on a disk Render wipes on
   every spin-down, so each wake starts a brand-new arena with no history
   to catch up to. The Arena tab says so in a banner when it is running on
   ephemeral storage.

2. **Optional, to watch trades appear during the day:** point a free cron
   pinger (e.g. [cron-job.org](https://cron-job.org)) at
   `https://your-app.onrender.com/api/health` every 10 minutes, Mon-Fri,
   09:00-15:45 IST. The service then stays awake through the session and
   the Arena trades bar by bar as they print, exactly like a paid instance.

3. **Minimum:** one ping after 15:40 IST on trading days is enough. The
   catch-up fills in the entire day from Yahoo's 5-minute bars and settles
   it. Opening the Arena tab in a browser does the same thing — any request
   that wakes the service starts a tick in the background.

What the catch-up does and does not promise:

- It replays up to **60 sessions** — as far back as Yahoo serves 5-minute
  bars. A gap longer than that loses the sessions beyond it, permanently.
- Trades filled from a replay are tagged **"caught up"** in the trade list,
  and the day's summary records it, so a replayed day is never silently
  passed off as a live one. The fills are identical either way (the engine
  only ever reads completed bars), but the tag is there to be audited.
- It is idempotent. Pinging ten times on the same evening settles each
  session once.
- The Arena tab shows when the last tick ran, and warns if the market is
  open but nothing has ticked for 30 minutes — i.e. the service is asleep
  and nobody is pinging it.

---

## 5. Install it on your phone as an app

1. Open your Render URL in **Chrome** on your Android phone.
2. Tap the **⋮** menu → **Add to Home screen** (or you may see an automatic
   "Install app" banner).
3. It now opens full-screen from an icon on your home screen, no browser
   bar — a real app-like experience, just not a compiled `.apk`.

---

## 6. How the prediction actually works

For each stock, the backend pulls history across every window you asked for
(5y, 1y, 6mo, 3mo, 2mo, 1mo, 4wk, 3wk, 2wk, 1wk, 4d, 3d, 2d, 1d, and recent
intraday), then blends:

- **Multi-timeframe trend** — weighted average drift, reweighted by how far
  ahead you're predicting (a 15-minute prediction leans on the last few
  days; a 3-month prediction leans on years of history).
- **Momentum** — RSI overbought/oversold + MACD histogram direction.
- **Volatility-based projection** — a Geometric Brownian Motion model
  (the standard stochastic model for price paths) turns the drift + volatility
  into a predicted price *and* a genuine confidence band, not just a point guess.
- **News sentiment** — VADER sentiment score across recent headlines nudges
  the drift up or down.
- **Seasonality** — average historical return for this calendar month,
  computed from the stock's own 5-year history (the honest, data-backed
  version of "does the season matter").
- **Weather** — a small, explicitly experimental nudge. There's no strong
  general evidence that weather predicts stock prices outside a few sectors
  (agriculture, power demand, travel), so this is deliberately capped small.

Every prediction is logged with a target time. Once that time passes, the
scheduler fetches the real price and computes the error — this is what
powers the "tracked accuracy" number on your watchlist, so the app's
track record is always visible, not just its guesses.

---

## 6b. Things worth knowing

**Intraday positions are squared off before the close.** From 15:15 IST any
open simulated position is force-exited (`exit_reason: eod_square_off`,
shown as "EOD" on the chart) and no new ones are opened. An "intraday"
strategy that holds overnight is quietly taking gap risk it never measured,
and a real broker would auto-square-off an MIS position anyway.

**Trading holidays.** Session state comes from `/api/market-status`, which
knows about weekends, session hours (09:15-15:30 IST) and NSE trading
holidays. The dates live in `backend/nse_holidays.json`.

**2026 is populated** (16 trading holidays). Those dates were transcribed
from secondary sources rather than read from nseindia.com directly, so
please spot-check them against the official list before relying on this:
<https://www.nseindia.com/resources/exchange-communication-holidays>
(Trading Holidays tab, Equities segment; also served as JSON at
`https://www.nseindia.com/api/holiday-master?type=trading`).

**2027 is empty on purpose.** The 2027 list available at the time was
checked and rejected - five of its dates fell on a weekend, which means it
was a festival calendar rather than a trading calendar, and it omitted
Diwali, Dussehra, Guru Nanak Jayanti and Christmas. Fill it in from the
official circular when NSE publishes it. Until a year is populated,
`/api/market-status` reports `holidays_known_for_year: false` and every
weekday looks like a trading day - the behaviour the app already had, now
visible instead of implied.

Weekends are handled in code and must not be listed. The loader refuses any
weekend date outright and says so, and `tests/test_holidays.py` fails the
build on one - that check is what caught the bad 2027 list.

**The watchlist shows a last price, not a live one.** It is the price from
the most recent scheduler tick, labelled with the time it was taken. The
scheduler only runs during market hours, and on Render's free tier it stops
entirely while the service is asleep.

**One tracked prediction per stock at a time.** The scheduler still refreshes
every 5 minutes, but overlapping forecasts of the same move are not each
graded - the newest is shown as an interim preview and only the tracked one
counts towards accuracy and weight learning.

---

## 6c. Strategy Arena (simulated money)

Open the **Arena** tab (desktop: the ⚔ Arena button). Several intraday
strategies paper-trade the same stocks on the same 5-minute bars, each with
its own copy of your daily capital (default ₹10,000), so the leaderboard
answers one question: **which one actually makes money after costs?**

- **Strategies:** opening-range breakout, VWAP reclaim, EMA 9/21 crossover,
  RSI oversold bounce, Bollinger volume breakout, the app's original
  4-signal score - plus a **benchmark** (buy every stock at the open, sell at
  the close). A strategy that can't beat the benchmark isn't adding anything.
- **Realistic costs:** every trade pays brokerage, STT, exchange fees, SEBI
  fee, stamp duty, GST and slippage (`backend/arena/costs.py` - edit the
  rates there if your broker differs). Trades whose target can't cover
  costs are skipped.
- **Pessimistic fills:** stops are checked against the bar's low; if a bar
  touches both stop and target, the stop counts. Gaps fill at the open.
- **Survival rules:** a strategy that loses its daily limit (default 2%)
  stops for the day. Once it has 20+ trades, it is **benched** if its
  recent trades lose money after costs, or if its drawdown passes 10%.
  Benched strategies keep trading in *shadow* and are reinstated
  automatically when they start earning again. If every strategy is
  benched, the arena says so - a live account would sit in cash.
- **Daily cycle:** opens at 09:15 IST, no new entries after 14:45, everything
  squared off at 15:15, settled after 15:30. A day the server slept through
  is caught up and settled from Yahoo's bars on the next tick.
- **Backtest:** replays the last 10-60 sessions of real 5-minute bars (Yahoo
  keeps about 60 days) through the same engine, so you see results today
  instead of after weeks. Treat a short backtest as a hint, not proof.

Nothing in the arena places a real order. No strategy, rule or setting can
guarantee a profit; the arena exists to find out which ones earn and to
stop the ones that don't before any real money is involved.

---

## 6d. Global markets (US, forex, crypto) and your own hours

The NSE session is 09:15-15:30 IST, which is the working day. The Arena
therefore also runs **three other markets**, each as a **separate arena**
with its own capital, symbols, strategies, history and currency - pick one
from the tabs at the top of the Arena tab. Nothing here places a real
order either.

### The markets and when they run, in IST

| Market | Symbols | Session in IST | Session locally | Volume? |
|---|---|---|---|---|
| **India (NSE)** | `RELIANCE`, `INFY` … | 09:15-15:30, Mon-Fri | same | yes |
| **US stocks** | `AAPL`, `MSFT`, `SPY` … | **19:00-01:30** (Mar-Oct)<br>**20:00-02:30** (Nov-Mar) | 09:30-16:00 New York | yes |
| **Forex** | `EURUSD=X`, `USDJPY=X`, `USDINR=X` … | 24×5 — **Mon 02:30 to Sat 02:30** (Mar-Oct)<br>**Mon 03:30 to Sat 03:30** (Nov-Mar) | Sun 17:00 to Fri 17:00 New York | **no** |
| **Crypto** | `BTC-USD`, `ETH-USD` … | 24×7 | — | yes |

**The US hours move by an hour twice a year.** The US observes daylight
saving and India does not, so the New York session lands at 19:00-01:30 IST
from March to early November and at 20:00-02:30 IST for the rest of the
year. Everything is computed with `zoneinfo` from the market's own clock,
so this happens by itself — but it is worth knowing before you set an alarm.
Forex's weekly open and close move with it for the same reason.

Holidays: NSE closures come from `backend/nse_holidays.json`, NYSE
closures and its 13:00 half days from `backend/us_holidays.json`
(2026-2027, from the NYSE Group calendar). Forex and crypto have no
holidays — forex just shuts for the weekend.

### Your trading window

Each arena has a **window**: the hours you actually want it trading, set in
IST, with the days of the week you want. The arena trades only when the
market is open **and** your window is active; that overlap is one
"session", and it is what opens, settles and gets caught up.

- The default window is the market's full session, except forex and crypto
  where it is **19:00-23:30 IST** — an evening, not a 24-hour day.
- **A window that ends before it starts runs past midnight** (22:00-02:00)
  and counts as one session, filed under the day it began.
- **The entry cutoff and square-off follow your window's end**, not a clock
  time: no new entries in the last 30 minutes, everything closed 15 minutes
  before the end. The NSE arena keeps its original 14:45 and 15:15.
- A window that never overlaps the market has no session that day, and the
  arena says so rather than sitting silently empty.

### Money

Capital is always set in **rupees** (default ₹10,000). At each session's
start it is converted to the market's currency at a Yahoo FX rate, and that
rate is **recorded in the session's own record**, so a settled session is
never restated when the rupee moves. P&L is shown in the market's currency
with the rupee figure underneath.

> **₹10,000 is about $113, which will not buy one share of most US
> mega-caps.** US and Indian arenas trade whole shares, so at the default
> capital a US arena skips most entries and the leaderboard stays empty —
> the arena says exactly that instead of looking broken. Raise the capital
> for that arena (₹50,000+ is a reasonable start) or pick lower-priced
> symbols. Forex and crypto trade fractional units, so they are fine at
> ₹10,000.

Leverage is available on forex and crypto only, capped at 5×, and off by
default. It multiplies the position a book may hold, never the money it
risks per trade — and it loses just as much faster, which the UI says in
red whenever it is on.

### What each market charges

Edit any of these in the arena's settings; the defaults are:

- **India** — brokerage 0.03% capped at ₹20, STT 0.025% on sells, exchange
  0.00297%, SEBI ₹10/crore, stamp 0.003% on buys, GST 18%, slippage 0.05%
  a side.
- **US** — $0 commission (configurable: "free" is a routing arrangement,
  not a law), SEC fee 0.00278% on sells, FINRA TAF, slippage 0.02% a side.
- **Forex** — no commission; the cost is the **spread**, in pips, half
  charged each side: EUR/USD 1.0, GBP/USD 1.5, USD/JPY 1.0, AUD/USD 1.2,
  USD/INR 2.0, anything else 1.5. A pip is 0.0001 on a major and 0.01 on a
  yen pair.
- **Crypto** — 0.10% taker fee plus 0.05% slippage, each side.

The backtest's sensitivity table replays the same sessions at **0×, 0.5×
and 1×** the market's own fill cost. A strategy that loses at 0× has no
edge at all; one that wins only at 0× cannot survive real fills.

### Forex has no volume

Yahoo reports zero volume for every FX pair. Rather than let that quietly
disable half the strategies, the arena substitutes a session **TWAP** (the
running mean of the typical price) for VWAP, and marks the volume average
**undefined** rather than zero — zero would make "volume above 1.5× average"
trivially true and fire a breakout on every bar. The two volume-gated
strategies trade without their volume confirmation and are labelled
**"· no-volume variant"** in the leaderboard, because they are not quite the
same strategy any more.

### Before you think about real money

> Simulation only. For real money in India: forex is legal only in INR
> pairs and RBI-permitted crosses on NSE/BSE through a SEBI-registered
> broker. Offshore forex apps are illegal. US stocks go through the LRS
> route. Crypto is taxed at 30% plus 1% TDS.

This note also sits under every leaderboard in the app. It is a summary,
not advice, and none of it is a substitute for checking the current rules
yourself — they change.

---

## 7. Extending it later

- Swap the ensemble for a trained ML model (XGBoost/LightGBM) once you've
  collected enough of your own resolved-prediction history to train on.
- Add more exchanges/asset types by extending `to_yf_symbol()` in
  `data_sources.py`.
- Add push notifications (e.g. via a free service like Pushover) when a
  watchlist prediction resolves.
