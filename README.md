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
  backend/          FastAPI app — data fetching, prediction engine, watchlist scheduler
    main.py
    data_sources.py   NSE/BSE list, yfinance prices, news, weather
    indicators.py     RSI / MACD / Bollinger / volatility
    predictor.py       the ensemble prediction engine
    scheduler.py       background job that keeps watchlist predictions live
    db.py              SQLite models (watchlist + prediction history)
    requirements.txt
    render.yaml         one-click Render deployment config
    .env.example
  frontend/          Mobile PWA (installs to your phone's home screen)
    index.html / style.css / app.js
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

You'll need Python 3.11+ installed on your computer.

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

---

## 4. Deploy to Render (free) so it runs continuously

1. Push this whole `stock-analyzer` folder to a new **GitHub repository**
   (Render deploys from a git repo — create one at github.com if you don't
   have it there yet, `git init`, `git add .`, `git commit`, `git push`).
2. Go to https://render.com, sign up free, click **New → Web Service**.
3. Connect your GitHub repo. Render will detect `backend/render.yaml`
   automatically — if it asks for a root directory, set it to `backend`.
4. In the **Environment** tab, add:
   - `NEWS_API_KEY` = your key from step 2
   - (leave `DATABASE_URL` unset to use SQLite, or see note below)
5. Click **Deploy**. Render gives you a URL like
   `https://tickerboard-xxxx.onrender.com`.

**Free tier note:** Render's free web services spin down after ~15 minutes
of no traffic, and spin back up (takes ~30-60s) on the next request. This
means the watchlist scheduler pauses while asleep and resumes once you open
the app again — fine for personal use, just not truly 24/7. If you want real
24/7 tracking, either upgrade to Render's paid tier, or set up a free
uptime-pinger (e.g. UptimeRobot hitting `/api/health` every 10 minutes) to
keep it awake during market hours.

**Persistent storage note:** Render's free tier disk resets on every
redeploy. Your watchlist/prediction history will survive restarts but not
redeploys, unless you point `DATABASE_URL` at a free Postgres from
[supabase.com](https://supabase.com) or [neon.tech](https://neon.tech) — copy
their connection string into the `DATABASE_URL` env var (see `.env.example`).

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

## 7. Extending it later

- Swap the ensemble for a trained ML model (XGBoost/LightGBM) once you've
  collected enough of your own resolved-prediction history to train on.
- Add more exchanges/asset types by extending `to_yf_symbol()` in
  `data_sources.py`.
- Add push notifications (e.g. via a free service like Pushover) when a
  watchlist prediction resolves.
