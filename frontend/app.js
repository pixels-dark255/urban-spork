// Same-origin API (backend serves this frontend directly)
const API = "";

let currentStock = null;   // { symbol, exchange, name, yf_symbol }
let currentHorizon = "1d";

const HORIZONS = ["15m", "1h", "4h", "1d", "3d", "1wk", "1mo", "3mo"];

// ---------- Navigation ----------
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});
document.getElementById("backFromAnalysis").addEventListener("click", () => switchView("view-search"));

function switchView(viewId) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  document.getElementById(viewId).classList.add("active");
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === viewId));
  if (viewId === "view-watchlist") loadWatchlist();
}

// ---------- Search ----------
const searchInput = document.getElementById("searchInput");
const searchResults = document.getElementById("searchResults");
let searchDebounce = null;

searchInput.addEventListener("input", () => {
  clearTimeout(searchDebounce);
  const q = searchInput.value.trim();
  if (q.length < 1) { searchResults.innerHTML = ""; return; }
  searchDebounce = setTimeout(() => runSearch(q), 300);
});

async function runSearch(q) {
  searchResults.innerHTML = `<p class="loading">searching…</p>`;
  try {
    const res = await fetch(`${API}/api/stocks/search?q=${encodeURIComponent(q)}`);
    const data = await res.json();
    if (!data.results.length) {
      searchResults.innerHTML = `<p class="muted">No matches. Try the exact symbol, e.g. RELIANCE.</p>`;
      return;
    }
    searchResults.innerHTML = data.results.map((r) => `
      <div class="result-item" data-symbol="${r.symbol}" data-exchange="${r.exchange}" data-name="${escapeHtml(r.name)}">
        <div>
          <div class="result-symbol">${r.symbol}</div>
          <div class="result-name">${escapeHtml(r.name)}</div>
        </div>
        <div class="result-exchange">${r.exchange}</div>
      </div>
    `).join("");
    searchResults.querySelectorAll(".result-item").forEach((el) => {
      el.addEventListener("click", () => {
        openAnalysis({
          symbol: el.dataset.symbol,
          exchange: el.dataset.exchange,
          name: el.dataset.name,
        });
      });
    });
  } catch (e) {
    searchResults.innerHTML = `<p class="muted">Search failed — check the backend is reachable.</p>`;
  }
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.innerText = s;
  return d.innerHTML;
}

// ---------- Analysis ----------
async function openAnalysis(stock) {
  currentStock = stock;
  switchView("view-analysis");
  await loadAnalysis();
}

async function loadAnalysis() {
  const content = document.getElementById("analysisContent");
  content.innerHTML = `<p class="loading">gathering data across timeframes…</p>`;
  try {
    const res = await fetch(
      `${API}/api/stocks/${encodeURIComponent(currentStock.symbol)}/analyze?exchange=${currentStock.exchange}&horizon=${currentHorizon}`
    );
    if (!res.ok) throw new Error("bad response");
    const data = await res.json();
    renderAnalysis(data);
  } catch (e) {
    content.innerHTML = `<p class="muted">Could not analyse this stock right now. It may be delisted, or the data source is temporarily unavailable.</p>`;
  }
}

function renderAnalysis(data) {
  const delta = data.predicted_price - data.current_price;
  const deltaPct = (delta / data.current_price) * 100;
  const dirClass = delta >= 0 ? "up" : "down";

  const signals = data.signals;
  const signalRows = [
    ["Multi-timeframe trend", signals.trend_drift_annualized],
    ["Momentum (RSI/MACD)", signals.momentum_tilt_annualized],
    ["News sentiment", signals.news_drift_annualized],
    ["Seasonality (5y history)", signals.seasonality_drift_annualized],
    ["Weather (experimental)", signals.weather_drift_annualized],
  ].map(([label, val]) => {
    const cls = val > 0.001 ? "pos" : val < -0.001 ? "neg" : "neu";
    const sign = val > 0 ? "+" : "";
    return `<div class="signal-row"><span>${label}</span><span class="val ${cls}">${sign}${(val * 100).toFixed(2)}%/yr</span></div>`;
  }).join("");

  const newsHtml = (data.news || []).slice(0, 6).map((n) => `
    <div class="news-item">
      <a href="${n.url}" target="_blank" rel="noopener">${escapeHtml(n.title)}</a>
      <span class="news-source">${escapeHtml(n.source || "")}</span>
    </div>
  `).join("") || `<p class="muted">No recent news found.</p>`;

  const horizonPills = HORIZONS.map((h) => `
    <button class="horizon-pill ${h === currentHorizon ? "active" : ""}" data-h="${h}">${h}</button>
  `).join("");

  document.getElementById("analysisContent").innerHTML = `
    <h2 class="stock-title">${escapeHtml(currentStock.name || currentStock.symbol)}</h2>
    <div class="stock-sub">${data.yf_symbol}</div>

    <div class="price-hero">
      <span class="price-current">₹${data.current_price.toFixed(2)}</span>
    </div>

    <div class="horizon-row" id="horizonRow">${horizonPills}</div>

    <div class="prediction-card">
      <div class="label">predicted price · ${data.horizon_label} from now</div>
      <div class="predicted-price">₹${data.predicted_price.toFixed(2)}
        <span class="price-delta ${dirClass}">${delta >= 0 ? "+" : ""}${deltaPct.toFixed(2)}%</span>
      </div>
      <div class="band-row">68% range: ₹${data.band_68[0]} – ₹${data.band_68[1]}</div>
      <div class="band-row">95% range: ₹${data.band_95[0]} – ₹${data.band_95[1]}</div>
      <div class="confidence-bar-track"><div class="confidence-bar-fill" style="width:${data.confidence * 100}%"></div></div>
      <div class="band-row" style="margin-top:6px;">confidence score: ${(data.confidence * 100).toFixed(0)}/100</div>
    </div>

    <div class="section-heading"><span class="eyebrow">why</span><h2 style="font-size:17px;">Signal breakdown</h2></div>
    <div class="signal-list">${signalRows}</div>

    <div class="disclaimer">${data.disclaimer}</div>

    <button class="add-watchlist-btn" id="addWatchlistBtn">Add to watchlist &amp; track live</button>

    <div class="section-heading"><span class="eyebrow">context</span><h2 style="font-size:17px;">Recent news</h2></div>
    ${newsHtml}
  `;

  document.querySelectorAll("#horizonRow .horizon-pill").forEach((btn) => {
    btn.addEventListener("click", async () => {
      currentHorizon = btn.dataset.h;
      await loadAnalysis();
    });
  });

  document.getElementById("addWatchlistBtn").addEventListener("click", addCurrentToWatchlist);
}

async function addCurrentToWatchlist() {
  const btn = document.getElementById("addWatchlistBtn");
  btn.disabled = true;
  btn.textContent = "Adding…";
  try {
    await fetch(`${API}/api/watchlist`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol: currentStock.symbol,
        exchange: currentStock.exchange,
        display_name: currentStock.name,
        horizon: currentHorizon,
      }),
    });
    btn.textContent = "Added ✓ — see Watchlist tab";
    refreshTicker();
  } catch (e) {
    btn.textContent = "Failed — try again";
    btn.disabled = false;
  }
}

// ---------- Watchlist ----------
async function loadWatchlist() {
  const content = document.getElementById("watchlistContent");
  content.innerHTML = `<p class="loading">loading…</p>`;
  try {
    const res = await fetch(`${API}/api/watchlist`);
    const data = await res.json();
    if (!data.watchlist.length) {
      content.innerHTML = `<p class="muted">Nothing yet — add a stock from its analysis screen.</p>`;
      return;
    }
    content.innerHTML = data.watchlist.map(renderWatchCard).join("");
    content.querySelectorAll(".watch-remove").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await fetch(`${API}/api/watchlist/${btn.dataset.id}`, { method: "DELETE" });
        loadWatchlist();
        refreshTicker();
      });
    });
  } catch (e) {
    content.innerHTML = `<p class="muted">Could not load watchlist.</p>`;
  }
}

function renderWatchCard(item) {
  const lp = item.latest_prediction;
  const tr = item.track_record;
  const predRow = lp
    ? `<div class="watch-row"><span>predicted next</span><span>₹${lp.predicted_price} by ${new Date(lp.target_at).toLocaleString()}</span></div>`
    : `<div class="watch-row"><span>no prediction yet — waits for next market-hours tick</span></div>`;
  const trackRow = tr.resolved_count > 0
    ? `<div class="watch-track-record">tracked accuracy: avg ${tr.avg_abs_error_pct}% error over ${tr.resolved_count} resolved predictions</div>`
    : `<div class="watch-track-record muted">no resolved predictions yet</div>`;

  return `
    <div class="watch-card">
      <div class="watch-card-top">
        <span class="watch-symbol">${item.symbol}</span>
        <button class="watch-remove" data-id="${item.id}">remove</button>
      </div>
      <div class="watch-row"><span>${item.display_name || ""}</span><span>horizon: ${item.horizon_minutes}m</span></div>
      ${predRow}
      ${trackRow}
    </div>
  `;
}

// ---------- Ticker strip ----------
async function refreshTicker() {
  try {
    const res = await fetch(`${API}/api/watchlist`);
    const data = await res.json();
    const track = document.getElementById("tickerTrack");
    if (!data.watchlist.length) {
      track.innerHTML = `<span class="ticker-item muted">add stocks to your watchlist to see them scroll here →</span>`;
      return;
    }
    track.innerHTML = data.watchlist.map((item) => {
      const lp = item.latest_prediction;
      if (!lp || lp.predicted_price == null) {
        return `<span class="ticker-item muted">${item.symbol} · awaiting first prediction</span>`;
      }
      const up = lp.predicted_price >= lp.price_at_prediction;
      return `<span class="ticker-item ${up ? "up" : "down"}">${item.symbol} ₹${lp.price_at_prediction} → ₹${lp.predicted_price}</span>`;
    }).join("");
  } catch (e) { /* silent - non-critical */ }
}

refreshTicker();
setInterval(refreshTicker, 60000);

// ---------- PWA install ----------
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  });
}
