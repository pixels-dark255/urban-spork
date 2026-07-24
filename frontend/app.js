// Same-origin API (backend serves this frontend directly)
const API = "";

let currentStock = null;   // { symbol, exchange, name, yf_symbol }
let currentHorizon = "1d";

const HORIZONS = ["15m", "1h", "4h", "1d", "3d", "1wk", "1mo", "3mo"];

// ---------- Navigation ----------
// Uses real browser history so the phone's back gesture/button navigates
// within the app (Search -> Analysis -> Watchlist -> Detail) instead of
// immediately exiting - the single biggest "feels broken" PWA gotcha.
let currentWatchItemId = null;
let viewPollHandle = null;

function startViewPolling(fn, intervalMs) {
  stopViewPolling();
  viewPollHandle = setInterval(fn, intervalMs);
}
function stopViewPolling() {
  if (viewPollHandle) { clearInterval(viewPollHandle); viewPollHandle = null; }
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => navigateTo(btn.dataset.view));
});
document.getElementById("backFromAnalysis").addEventListener("click", () => history.back());
document.getElementById("backFromWatchDetail").addEventListener("click", () => history.back());

function navigateTo(viewId, extraState = {}) {
  history.pushState({ view: viewId, ...extraState }, "", `#${viewId}`);
  renderView(viewId, extraState);
}

window.addEventListener("popstate", (e) => {
  const state = e.state || { view: "view-search" };
  renderView(state.view, state);
});

// First load: establish the base history entry so back() from Search exits
// cleanly instead of landing on an undefined state.
history.replaceState({ view: "view-search" }, "", "#view-search");

function renderView(viewId, state = {}) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  document.getElementById(viewId).classList.add("active");
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === viewId));
  stopViewPolling();

  if (viewId === "view-watchlist") {
    loadWatchlist();
    startViewPolling(() => loadWatchlist(true), 30000);
  } else if (viewId === "view-watch-detail") {
    currentWatchItemId = state.itemId ?? currentWatchItemId;
    if (currentWatchItemId != null) {
      loadWatchDetail(currentWatchItemId);
      startViewPolling(() => loadWatchDetail(currentWatchItemId, true), 30000);
    }
  }
}

// Kept for compatibility with existing calls below - pushes new history.
function switchView(viewId) {
  navigateTo(viewId);
}

// ---------- Toast ----------
function showToast(message, tone = "info") {
  const host = document.getElementById("toastHost");
  const el = document.createElement("div");
  el.className = `toast-item ${tone}`;
  el.textContent = message;
  host.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 250);
  }, 2600);
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

function fmtPrice(n) {
  return (n == null) ? "—" : Number(n).toFixed(2);
}

// ---------- Analysis ----------
async function openAnalysis(stock) {
  currentStock = stock;
  switchView("view-analysis");
  await loadAnalysis();
}

async function loadAnalysis() {
  const content = document.getElementById("analysisContent");
  content.innerHTML = `
    <div class="skeleton-line" style="width:55%; height:26px;"></div>
    <div class="skeleton-line" style="width:30%; margin-top:8px;"></div>
    <div class="skeleton-block" style="height:120px; margin-top:18px;"></div>
    <div class="skeleton-block" style="height:160px; margin-top:14px;"></div>
  `;
  try {
    const res = await fetch(
      `${API}/api/stocks/${encodeURIComponent(currentStock.symbol)}/analyze?exchange=${currentStock.exchange}&horizon=${currentHorizon}`
    );
    const data = await res.json();
    if (!res.ok) {
      content.innerHTML = `<p class="muted">${escapeHtml(data.detail || "Analysis failed.")}</p>`;
      return;
    }
    renderAnalysis(data);
  } catch (e) {
    content.innerHTML = `<p class="muted">Could not reach the backend. Check your connection and try again.</p>`;
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
  btn.textContent = "Running 90-day backtest & calibrating…";
  try {
    const res = await fetch(`${API}/api/watchlist`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol: currentStock.symbol,
        exchange: currentStock.exchange,
        display_name: currentStock.name,
        horizon: currentHorizon,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      btn.textContent = `Failed: ${data.detail || "unknown error"}`;
      btn.disabled = false;
      return;
    }
    btn.textContent = "Added & calibrated ✓ — see Watchlist tab";
    refreshTicker();
  } catch (e) {
    btn.textContent = "Failed — network error, try again";
    btn.disabled = false;
  }
}

// ---------- Watch detail (90-day backtest + refinement history) ----------
function openWatchDetail(itemId) {
  navigateTo("view-watch-detail", { itemId });
}

async function loadWatchDetail(itemId, silent = false) {
  const content = document.getElementById("watchDetailContent");
  if (!silent) content.innerHTML = `
    <div class="skeleton-line" style="width:50%; height:24px;"></div>
    <div class="skeleton-block" style="height:100px; margin-top:16px;"></div>
    <div class="skeleton-block" style="height:180px; margin-top:14px;"></div>
  `;
  try {
    const res = await fetch(`${API}/api/watchlist/${itemId}/detail`);
    const data = await res.json();
    if (!res.ok) {
      if (!silent) content.innerHTML = `<p class="muted">${escapeHtml(data.detail || "Could not load detail.")}</p>`;
      return;
    }
    renderWatchDetail(data);
  } catch (e) {
    if (!silent) content.innerHTML = `<p class="muted">Could not reach the backend.</p>`;
  }
}

function renderWatchDetail(data) {
  const bt = data.backtest_summary;
  const weights = data.signal_weights || {};
  const lp = data.latest_prediction;
  const livePrice = data.live_price;

  let liveHero;
  if (livePrice != null && lp) {
    const delta = lp.predicted_price - livePrice;
    const deltaPct = (delta / livePrice) * 100;
    const dirClass = delta >= 0 ? "up" : "down";
    liveHero = `
      <div class="price-hero">
        <span class="price-current">₹${livePrice.toFixed(2)}</span>
      </div>
      <div class="prediction-card">
        <div class="label">latest prediction · target ${new Date(lp.target_at).toLocaleString()}</div>
        <div class="predicted-price">₹${lp.predicted_price.toFixed(2)}
          <span class="price-delta ${dirClass}">${delta >= 0 ? "+" : ""}${deltaPct.toFixed(2)}%</span>
        </div>
        ${lp.predicted_low != null ? `<div class="band-row">68% range: ₹${lp.predicted_low} – ₹${lp.predicted_high}</div>` : ""}
        <div class="confidence-bar-track"><div class="confidence-bar-fill" style="width:${lp.confidence * 100}%"></div></div>
        <div class="band-row" style="margin-top:6px;">confidence score: ${(lp.confidence * 100).toFixed(0)}/100</div>
      </div>`;
  } else if (livePrice != null) {
    liveHero = `<div class="price-hero"><span class="price-current">₹${livePrice.toFixed(2)}</span></div><p class="muted">No live prediction yet.</p>`;
  } else {
    liveHero = `<p class="muted">Live price unavailable right now — Yahoo Finance may be temporarily rate-limiting. Try again shortly.</p>`;
  }

  const summaryHtml = bt ? `
    <div class="prediction-card">
      <div class="label">90-day walk-forward backtest</div>
      <div class="band-row" style="margin-top:6px;">tested on ${bt.total_days_backtested} trading days</div>
      <div class="band-row">overall avg error: <b>${bt.avg_abs_error_pct_overall}%</b></div>
      <div class="band-row">earliest third of the window: <b>${bt.avg_abs_error_pct_early_period}%</b> avg error</div>
      <div class="band-row">most recent third: <b>${bt.avg_abs_error_pct_recent_period}%</b> avg error</div>
      <div class="band-row" style="margin-top:6px; color:${bt.improved ? "var(--brass)" : "var(--signal-red)"}">
        ${bt.improved ? "Accuracy improved as weights refined ✓" : "No clear improvement yet on this stock"}
      </div>
    </div>
  ` : `<p class="muted">Backtest still running or there wasn't enough history for this stock — check back shortly.</p>`;

  const weightRows = Object.entries(weights).map(([name, val]) => {
    const pct = Math.round((val / 1.0) * 100);
    return `<div class="signal-row"><span>${name}</span><span class="val ${val >= 1 ? "pos" : "neg"}">${val.toFixed(2)}×</span></div>`;
  }).join("");

  const history = (data.backtest_history || []).slice(-15).reverse();
  const historyHtml = history.length ? `
    <table class="backtest-table">
      <thead><tr><th>date</th><th>predicted</th><th>actual</th><th>error</th></tr></thead>
      <tbody>
        ${history.map((h) => `
          <tr>
            <td>${h.date}</td>
            <td>₹${fmtPrice(h.predicted_price)}</td>
            <td>₹${fmtPrice(h.actual_price)}</td>
            <td class="${h.error_pct >= 0 ? "pos" : "neg"}">${h.error_pct}%</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  ` : `<p class="muted">No backtest days recorded.</p>`;

  const livePreds = (data.live_predictions || []).slice(-10).reverse();
  const liveHtml = livePreds.length ? `
    <table class="backtest-table">
      <thead><tr><th>made at</th><th>predicted</th><th>actual</th><th>status</th></tr></thead>
      <tbody>
        ${livePreds.map((p) => `
          <tr>
            <td>${new Date(p.made_at).toLocaleDateString()}</td>
            <td>₹${fmtPrice(p.predicted_price)}</td>
            <td>${p.actual_price != null ? "₹" + fmtPrice(p.actual_price) : "—"}</td>
            <td>${p.resolved ? (p.error_pct + "%") : "pending"}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  ` : `<p class="muted">No live predictions resolved yet.</p>`;

  document.getElementById("watchDetailContent").innerHTML = `
    <h2 class="stock-title">${escapeHtml(data.display_name || data.symbol)}</h2>
    <div class="stock-sub">${data.symbol} &middot; horizon ${data.horizon_minutes}m</div>

    ${liveHero}

    <div class="section-heading"><span class="eyebrow">calibration</span><h2 style="font-size:17px;">90-day backtest</h2></div>
    ${summaryHtml}

    <div class="section-heading"><span class="eyebrow">self-refinement</span><h2 style="font-size:17px;">Current signal weights</h2></div>
    <div class="signal-list">${weightRows}</div>
    <div class="disclaimer">
      Weights above 1× mean that signal has been right more often for this specific stock and is
      trusted more; below 1× means it's been trusted less. These update after every backtest day
      and every live prediction that resolves - as long as this stock stays on your watchlist.
    </div>

    <div class="section-heading"><span class="eyebrow">walk-forward</span><h2 style="font-size:17px;">Backtest days (most recent 15)</h2></div>
    ${historyHtml}

    <div class="section-heading"><span class="eyebrow">since adding</span><h2 style="font-size:17px;">Live predictions (most recent 10)</h2></div>
    ${liveHtml}
  `;
}

document.getElementById("refreshWatchlistBtn").addEventListener("click", (e) => {
  e.currentTarget.classList.add("spinning");
  loadWatchlist().then(() => {
    refreshTicker();
    setTimeout(() => e.currentTarget.classList.remove("spinning"), 400);
  });
});

function skeletonCards(n = 2) {
  return Array(n).fill(`
    <div class="watch-card skeleton-card">
      <div class="skeleton-line" style="width:40%"></div>
      <div class="skeleton-line" style="width:70%; margin-top:14px;"></div>
      <div class="skeleton-block"></div>
    </div>
  `).join("");
}

// ---------- Watchlist ----------
async function loadWatchlist(silent = false) {
  const content = document.getElementById("watchlistContent");
  if (!silent) content.innerHTML = skeletonCards();
  try {
    const res = await fetch(`${API}/api/watchlist`);
    const data = await res.json();
    if (!data.watchlist.length) {
      content.innerHTML = `<p class="muted">Nothing yet — add a stock from its analysis screen.</p>`;
      return;
    }
    content.innerHTML = data.watchlist.map(renderWatchCard).join("");
    content.querySelectorAll(".watch-remove").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (btn.dataset.confirming !== "1") {
          btn.dataset.confirming = "1";
          btn.textContent = "tap again to confirm";
          btn.classList.add("confirming");
          setTimeout(() => {
            if (btn.isConnected && btn.dataset.confirming === "1") {
              btn.dataset.confirming = "0";
              btn.textContent = "remove";
              btn.classList.remove("confirming");
            }
          }, 3000);
          return;
        }
        const symbol = btn.dataset.symbol;
        await fetch(`${API}/api/watchlist/${btn.dataset.id}`, { method: "DELETE" });
        showToast(`Removed ${symbol} from watchlist`);
        loadWatchlist();
        refreshTicker();
      });
    });
    content.querySelectorAll(".watch-symbol-link").forEach((el) => {
      el.addEventListener("click", () => openWatchDetail(el.dataset.id));
    });
  } catch (e) {
    if (!silent) content.innerHTML = `<p class="muted">Could not load watchlist.</p>`;
  }
}

function renderWatchCard(item) {
  const lp = item.latest_prediction;
  const tr = item.track_record;
  const bt = item.backtest_summary;
  const livePrice = item.live_price;

  let heroRow;
  if (livePrice != null && lp) {
    const delta = lp.predicted_price - livePrice;
    const deltaPct = (delta / livePrice) * 100;
    const dirClass = delta >= 0 ? "up" : "down";
    heroRow = `
      <div class="watch-hero">
        <div class="watch-hero-block">
          <div class="watch-hero-label">live now</div>
          <div class="watch-hero-price">₹${livePrice.toFixed(2)}</div>
        </div>
        <div class="watch-hero-arrow">→</div>
        <div class="watch-hero-block">
          <div class="watch-hero-label">predicted</div>
          <div class="watch-hero-price ${dirClass}">₹${lp.predicted_price.toFixed(2)}</div>
          <div class="watch-hero-sub ${dirClass}">${delta >= 0 ? "+" : ""}${deltaPct.toFixed(2)}%</div>
        </div>
      </div>`;
  } else if (livePrice != null) {
    heroRow = `<div class="watch-hero"><div class="watch-hero-block"><div class="watch-hero-label">live now</div><div class="watch-hero-price">₹${livePrice.toFixed(2)}</div></div></div>`;
  } else {
    heroRow = `<div class="watch-row"><span>live price unavailable right now</span></div>`;
  }

  const targetRow = lp
    ? `<div class="watch-row"><span>target time</span><span>${new Date(lp.target_at).toLocaleString()}</span></div>`
    : `<div class="watch-row"><span>calibrating…</span></div>`;
  const trackRow = tr.resolved_count > 0
    ? `<div class="watch-track-record">live tracked accuracy: avg ${tr.avg_abs_error_pct}% error over ${tr.resolved_count} resolved predictions</div>`
    : `<div class="watch-track-record muted">no resolved live predictions yet</div>`;
  const btRow = bt
    ? `<div class="watch-track-record">90-day backtest: ${bt.avg_abs_error_pct_overall}% avg error (early ${bt.avg_abs_error_pct_early_period}% → recent ${bt.avg_abs_error_pct_recent_period}%) ${bt.improved ? "— improving ✓" : ""}</div>`
    : `<div class="watch-track-record muted">backtest running — tap to check back</div>`;

  return `
    <div class="watch-card">
      <div class="watch-card-top">
        <span class="watch-symbol watch-symbol-link" data-id="${item.id}">${item.symbol} ›</span>
        <button class="watch-remove" data-id="${item.id}" data-symbol="${item.symbol}">remove</button>
      </div>
      <div class="watch-row"><span>${item.display_name || ""}</span><span>horizon: ${item.horizon_minutes}m</span></div>
      ${heroRow}
      ${targetRow}
      ${trackRow}
      ${btRow}
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
      return `<span class="ticker-item ${up ? "up" : "down"}">${item.symbol} ₹${fmtPrice(lp.price_at_prediction)} → ₹${fmtPrice(lp.predicted_price)}</span>`;
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
