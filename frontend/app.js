// Same-origin API (backend serves this frontend directly)
// ---------- IST formatting + market status ----------
function fmtIST(dateInput, opts = {}) {
  return new Date(dateInput).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", ...opts });
}
function fmtISTDate(dateInput) {
  return fmtIST(dateInput, { day: "2-digit", month: "short", year: "numeric" });
}
function fmtISTDateTime(dateInput) {
  return fmtIST(dateInput, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: true });
}

function isMarketOpenNow() {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Kolkata", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(new Date());
  const get = (t) => parts.find((p) => p.type === t)?.value;
  const weekday = get("weekday");
  const hour = parseInt(get("hour"), 10);
  const minute = parseInt(get("minute"), 10);
  if (["Sat", "Sun"].includes(weekday)) return false;
  const mins = hour * 60 + minute;
  return mins >= (9 * 60 + 15) && mins <= (15 * 60 + 30);
}

function updateMarketStatusPill() {
  const pill = document.getElementById("marketStatusPill");
  if (!pill) return;
  const open = isMarketOpenNow();
  pill.textContent = open ? "🟢 Market Open" : "🔴 Market Closed";
  pill.title = "9:15 AM – 3:30 PM IST, Mon–Fri";
  pill.className = `market-status-pill ${open ? "open" : "closed"}`;
}
updateMarketStatusPill();
setInterval(updateMarketStatusPill, 30000);

const API = "";

// Stable per-device identity, replacing fragile IP-based lookup (public IP
// changes constantly on Indian mobile networks - sleep/wake, wifi<->mobile
// data, carrier NAT rotation - which made the watchlist appear to "vanish"
// every time it happened). Generated once, persisted in localStorage,
// unaffected by any network change.
function getClientId() {
  let id = localStorage.getItem("tickerboard_client_id");
  if (!id) {
    id = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`);
    localStorage.setItem("tickerboard_client_id", id);
  }
  return id;
}
const CLIENT_ID = getClientId();

function apiFetch(url, options = {}) {
  const headers = { ...(options.headers || {}), "X-Client-Id": CLIENT_ID };
  return fetch(url, { ...options, headers });
}

let currentStock = null;   // { symbol, exchange, name, yf_symbol }
let currentHorizon = "1d";

const HORIZONS = ["15m", "1h", "4h", "1d", "3d", "1wk", "1mo", "3mo"];

// ---------- Navigation ----------
// Uses real browser history so the phone's back gesture/button navigates
// within the app (Search -> Analysis -> Watchlist -> Detail) instead of
// immediately exiting - the single biggest "feels broken" PWA gotcha.
let currentWatchItemId = null;
let currentIntradaySymbol = null;
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
document.getElementById("backFromIntradayDetail").addEventListener("click", () => history.back());

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

// ---------- Desktop always-on side panels ----------
// On mobile this whole app is a single-view SPA (one .view visible at a
// time, driven by the tab bar + browser history). On desktop, watchlist
// and intraday become permanent side columns (see the >=1024px grid in
// style.css) instead of tabs you navigate to - so they need to load and
// keep polling independently of whatever's currently in the center
// analysis panel, not wait for a navigateTo() that will never come once
// the tab bar is hidden.
const isDesktop = () => window.matchMedia("(min-width: 1024px)").matches;
let desktopPanelsInitialized = false;
function initDesktopPanelsIfNeeded() {
  if (!isDesktop() || desktopPanelsInitialized) return;
  desktopPanelsInitialized = true;
  loadWatchlist();
  loadIntradayList();
  setInterval(() => loadWatchlist(true), 30000);
  setInterval(() => loadIntradayList(true), 20000);
}
initDesktopPanelsIfNeeded();
window.addEventListener("resize", initDesktopPanelsIfNeeded);

function renderView(viewId, state = {}) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  document.getElementById(viewId).classList.add("active");
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === viewId));
  stopViewPolling();

  if (viewId === "view-watchlist") {
    loadWatchlist();
    startViewPolling(() => loadWatchlist(true), 30000);
  } else if (viewId === "view-analysis") {
    const backBtn = document.getElementById("backFromAnalysis");
    if (state.fromWatchlist && state.itemId != null) {
      currentWatchItemId = state.itemId;
      backBtn.textContent = "← back to watchlist";
      loadWatchAnalysis(state.itemId);
      startViewPolling(() => loadWatchAnalysis(state.itemId, true), 30000);
    } else {
      backBtn.textContent = "← back to search";
      // normal search-driven flow: openAnalysis() calls loadAnalysis() itself
    }
  } else if (viewId === "view-watch-detail") {
    currentWatchItemId = state.itemId ?? currentWatchItemId;
    if (currentWatchItemId != null) {
      loadWatchDetail(currentWatchItemId);
      startViewPolling(() => loadWatchDetail(currentWatchItemId, true), 30000);
    }
  } else if (viewId === "view-intraday") {
    loadIntradayList();
    startViewPolling(() => loadIntradayList(true), 20000);
  } else if (viewId === "view-intraday-detail") {
    currentIntradaySymbol = state.symbol ?? currentIntradaySymbol;
    if (currentIntradaySymbol) {
      loadIntradayDetail(currentIntradaySymbol);
      startViewPolling(() => loadIntradayDetail(currentIntradaySymbol, true), 20000);
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

// ---------- Intraday (simulated paper trading) ----------
const intradaySearchInput = document.getElementById("intradaySearchInput");
const intradaySearchResults = document.getElementById("intradaySearchResults");
let intradaySearchDebounce = null;

intradaySearchInput.addEventListener("input", () => {
  clearTimeout(intradaySearchDebounce);
  const q = intradaySearchInput.value.trim();
  if (q.length < 1) { intradaySearchResults.innerHTML = ""; return; }
  intradaySearchDebounce = setTimeout(() => runIntradaySearch(q), 300);
});

async function runIntradaySearch(q) {
  intradaySearchResults.innerHTML = `<p class="loading">searching…</p>`;
  try {
    const res = await apiFetch(`${API}/api/stocks/search?q=${encodeURIComponent(q)}`);
    const data = await res.json();
    if (!data.results.length) {
      intradaySearchResults.innerHTML = `<p class="muted">No matches.</p>`;
      return;
    }
    intradaySearchResults.innerHTML = data.results.map((r) => `
      <div class="result-item" data-symbol="${r.symbol}" data-exchange="${r.exchange}" data-name="${escapeHtml(r.name)}">
        <div>
          <div class="result-symbol">${r.symbol}</div>
          <div class="result-name">${escapeHtml(r.name)}</div>
        </div>
        <div class="result-exchange">${r.exchange}</div>
      </div>
    `).join("");
    intradaySearchResults.querySelectorAll(".result-item").forEach((el) => {
      el.addEventListener("click", async () => {
        el.style.opacity = "0.5";
        try {
          const res = await apiFetch(`${API}/api/intraday/stocks`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              symbol: el.dataset.symbol,
              exchange: el.dataset.exchange,
              display_name: el.dataset.name,
            }),
          });
          if (!res.ok) {
            const data = await res.json();
            showToast(data.detail || "Could not add stock", "error");
            return;
          }
          intradaySearchInput.value = "";
          intradaySearchResults.innerHTML = "";
          showToast(`${el.dataset.symbol} added — paper trading across 5m/15m/30m starts next market tick`, "success");
          loadIntradayList();
        } catch (e) {
          showToast("Network error adding stock", "error");
        }
      });
    });
  } catch (e) {
    intradaySearchResults.innerHTML = `<p class="muted">Search failed.</p>`;
  }
}

document.getElementById("refreshIntradayBtn").addEventListener("click", (e) => {
  e.currentTarget.classList.add("spinning");
  loadIntradayList().then(() => setTimeout(() => e.currentTarget.classList.remove("spinning"), 400));
});

function intradaySkeleton() {
  return Array(2).fill(`
    <div class="watch-card skeleton-card">
      <div class="skeleton-line" style="width:35%"></div>
      <div class="skeleton-block" style="height:90px; margin-top:12px;"></div>
    </div>
  `).join("");
}

async function loadIntradayList(silent = false) {
  const content = document.getElementById("intradayContent");
  if (!silent) content.innerHTML = intradaySkeleton();
  try {
    const res = await apiFetch(`${API}/api/intraday/stocks`);
    const data = await res.json();
    if (!data.stocks.length) {
      content.innerHTML = `<p class="muted">No stocks tracked yet — search above to add one.</p>`;
      return;
    }
    content.innerHTML = data.stocks.map(renderIntradayCard).join("");
    content.querySelectorAll(".intraday-card-link").forEach((el) => {
      el.addEventListener("click", () => {
        navigateTo("view-intraday-detail", { symbol: el.dataset.symbol });
      });
    });
    content.querySelectorAll(".tf-col-clickable").forEach((el) => {
      el.addEventListener("click", () => {
        el.querySelector(".verdict-pill").classList.toggle("hidden");
      });
    });
    content.querySelectorAll(".intraday-remove").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (btn.dataset.confirming !== "1") {
          btn.dataset.confirming = "1";
          btn.textContent = "tap again to confirm";
          setTimeout(() => { btn.dataset.confirming = "0"; btn.textContent = "remove"; }, 3000);
          return;
        }
        await apiFetch(`${API}/api/intraday/stocks/${encodeURIComponent(btn.dataset.symbol)}`, { method: "DELETE" });
        loadIntradayList();
      });
    });
  } catch (e) {
    if (!silent) content.innerHTML = `<p class="muted">Could not load intraday list.</p>`;
  }
}

function verdictFor(score) {
  if (score == null) return { text: "warming up", cls: "verdict-neutral" };
  if (score >= 2) return { text: "Good time to buy", cls: "verdict-strong-buy" };
  if (score === 1) return { text: "Leaning buy", cls: "verdict-buy" };
  if (score === 0) return { text: "Neutral — wait", cls: "verdict-neutral" };
  if (score === -1) return { text: "Leaning sell", cls: "verdict-sell" };
  return { text: "Bad time to buy", cls: "verdict-strong-sell" };
}

function renderIntradayCard(stock) {
  const tfRows = ["5m", "15m", "30m"].map((tf) => {
    const t = stock.timeframes[tf];
    if (!t) return `<div class="tf-col"><div class="tf-label">${tf}</div><div class="tf-value muted">warming up</div></div>`;
    const cls = t.total_pnl > 0 ? "pos" : t.total_pnl < 0 ? "neg" : "neu";
    const v = verdictFor(t.score);
    return `
      <div class="tf-col tf-col-clickable" data-symbol="${stock.symbol}" data-tf="${tf}">
        <div class="tf-label">${tf}</div>
        <div class="tf-value ${cls}">${t.total_pnl >= 0 ? "+" : ""}₹${t.total_pnl.toFixed(0)}</div>
        <div class="tf-sub">${t.total_pnl_pct >= 0 ? "+" : ""}${t.total_pnl_pct}% · ${t.closed_trades} trades${t.win_rate_pct != null ? ` · ${t.win_rate_pct}% win` : ""}</div>
        <div class="tf-sub">${t.open_position ? "position open" : "flat"}</div>
        <div class="verdict-pill ${v.cls} hidden">${v.text}</div>
      </div>`;
  }).join("");

  return `
    <div class="watch-card">
      <div class="watch-card-top">
        <span class="watch-symbol watch-symbol-link intraday-card-link" data-symbol="${stock.symbol}">${stock.symbol} ›</span>
        <button class="watch-remove intraday-remove" data-symbol="${stock.symbol}">remove</button>
      </div>
      <div class="watch-row"><span>${stock.display_name || ""}</span><span>live: ${stock.live_price != null ? "₹" + stock.live_price.toFixed(2) : "—"}</span></div>
      <div class="tf-compare-row">${tfRows}</div>
    </div>
  `;
}

async function loadIntradayDetail(symbol, silent = false) {
  const content = document.getElementById("intradayDetailContent");
  if (!silent) content.innerHTML = `
    <div class="skeleton-line" style="width:50%; height:24px;"></div>
    <div class="skeleton-block" style="height:200px; margin-top:16px;"></div>
  `;
  try {
    const res = await apiFetch(`${API}/api/intraday/stocks/${encodeURIComponent(symbol)}/detail`);
    const data = await res.json();
    if (!res.ok) {
      content.innerHTML = `<p class="muted">${escapeHtml(data.detail || "Could not load detail.")}</p>`;
      return;
    }
    renderIntradayDetail(data);
  } catch (e) {
    content.innerHTML = `<p class="muted">Could not reach the backend.</p>`;
  }
}

function renderIntradayDetail(data) {
  const sections = ["5m", "15m", "30m"].map((tf) => {
    const t = data.timeframes[tf];
    if (!t) return "";
    const s = t.summary;
    const sig = t.current_signal;
    const cls = s.total_pnl > 0 ? "pos" : s.total_pnl < 0 ? "neg" : "neu";
    const sigHtml = sig ? `
      <div class="band-row">score right now: <b>${sig.score >= 0 ? "+" : ""}${sig.score}</b> (fast MA ${sig.fast_ma} / slow MA ${sig.slow_ma}, RSI ${sig.rsi}, VWAP ${sig.vwap})</div>
    ` : `<div class="band-row muted">not enough bars yet today</div>`;
    const trades = t.trade_log.slice(0, 20);
    const tradeHtml = trades.length ? `
      <table class="backtest-table">
        <thead><tr><th>entry</th><th>exit</th><th>pnl</th><th>reason</th></tr></thead>
        <tbody>
          ${trades.map((tr) => `
            <tr>
              <td>₹${tr.entry_price}</td>
              <td>₹${tr.exit_price}</td>
              <td class="${tr.pnl >= 0 ? "pos" : "neg"}">${tr.pnl >= 0 ? "+" : ""}₹${tr.pnl} (${tr.pnl_pct}%)</td>
              <td>${tr.exit_reason.replace("_", " ")}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    ` : `<p class="muted">No closed trades yet.</p>`;

    return `
      <div class="section-heading"><span class="eyebrow">${tf} strategy</span><h2 style="font-size:17px;">Timeframe: ${tf}</h2></div>
      <div id="intradayChart-${tf}" class="price-chart-container intraday-chart"></div>
      <div class="prediction-card">
        <div class="label">equity: ₹${s.equity.toFixed(0)} (started at ₹1,00,000)</div>
        <div class="predicted-price ${cls}" style="font-size:22px;">${s.total_pnl >= 0 ? "+" : ""}₹${s.total_pnl.toFixed(0)} <span class="price-delta ${cls}">${s.total_pnl_pct >= 0 ? "+" : ""}${s.total_pnl_pct}%</span></div>
        <div class="band-row">${s.closed_trades} closed trades${s.win_rate_pct != null ? `, ${s.win_rate_pct}% win rate` : ""}</div>
        <div class="band-row">${s.open_position ? `holding ${s.open_position.qty} shares @ ₹${s.open_position.entry_price}` : "no open position"}</div>
        ${sigHtml}
      </div>
      ${tradeHtml}
    `;
  }).join("");

  document.getElementById("intradayDetailContent").innerHTML = `
    <h2 class="stock-title">${escapeHtml(data.symbol)}</h2>
    <div class="stock-sub">live: ${data.live_price != null ? "₹" + data.live_price.toFixed(2) : "—"} &middot; simulated money only</div>
    <div class="disclaimer">Rule-based (MA crossover + RSI + VWAP + opening-range breakout), all simulated. Nothing here is a guarantee of real-world performance.</div>
    ${sections}
  `;

  ["5m", "15m", "30m"].forEach((tf) => renderIntradayChart(tf, data.timeframes[tf]));
}

const intradayChartInstances = {};
function renderIntradayChart(tf, t) {
  const container = document.getElementById(`intradayChart-${tf}`);
  if (!container || typeof LightweightCharts === "undefined" || !t) return;
  if (!t.bars || !t.bars.length) {
    container.innerHTML = `<p class="muted">No bars yet today for this timeframe.</p>`;
    return;
  }
  if (intradayChartInstances[tf]) {
    try { intradayChartInstances[tf].remove(); } catch (e) { /* already gone */ }
  }
  const chart = LightweightCharts.createChart(container, {
    height: 220,
    layout: { background: { color: "transparent" }, textColor: "#8FA39A" },
    grid: { vertLines: { color: "rgba(255,255,255,0.05)" }, horzLines: { color: "rgba(255,255,255,0.05)" } },
    rightPriceScale: { borderColor: "rgba(255,255,255,0.1)" },
    timeScale: { borderColor: "rgba(255,255,255,0.1)", timeVisible: true, secondsVisible: false },
  });
  intradayChartInstances[tf] = chart;

  const candles = chart.addSeries(LightweightCharts.CandlestickSeries, {
    upColor: "#3c9a5c", downColor: "#C1443C", borderVisible: false,
    wickUpColor: "#3c9a5c", wickDownColor: "#C1443C",
  });
  candles.setData(t.bars);

  const toUnix = (iso) => Math.floor(new Date(iso).getTime() / 1000);
  const reasonMeta = {
    stop_loss: { color: "#C1443C", text: "SL" },
    target: { color: "#3c9a5c", text: "TGT" },
    signal_reversal: { color: "#C9A227", text: "EXIT" },
  };
  const markers = [];
  (t.trade_log || []).forEach((tr) => {
    markers.push({
      time: toUnix(tr.entry_at), position: "belowBar", color: "#3c9a5c", shape: "arrowUp", text: "BUY",
    });
    const meta = reasonMeta[tr.exit_reason] || { color: "#C9A227", text: "EXIT" };
    markers.push({
      time: toUnix(tr.exit_at), position: "aboveBar", color: meta.color, shape: "arrowDown", text: meta.text,
    });
  });
  markers.sort((a, b) => a.time - b.time);
  if (markers.length && LightweightCharts.createSeriesMarkers) {
    LightweightCharts.createSeriesMarkers(candles, markers);
  }

  chart.timeScale().fitContent();
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
    const res = await apiFetch(`${API}/api/stocks/search?q=${encodeURIComponent(q)}`);
    const data = await res.json();
    if (!data.results.length) {
      searchResults.innerHTML = `<p class="muted">No matches. Try the exact symbol, e.g. RELIANCE.</p>`;
      return;
    }
    searchResults.innerHTML = data.results.map((r) => `
      <div class="result-item" data-symbol="${r.symbol}" data-exchange="${r.exchange}" data-name="${escapeHtml(r.name)}">
        <div class="result-main">
          <div class="result-symbol">${r.symbol}</div>
          <div class="result-name">${escapeHtml(r.name)}</div>
        </div>
        <div class="result-exchange">${r.exchange}</div>
        <button class="result-quick-add" data-symbol="${r.symbol}" data-exchange="${r.exchange}" data-name="${escapeHtml(r.name)}" title="Add to watchlist" aria-label="Add ${r.symbol} to watchlist">+</button>
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
    searchResults.querySelectorAll(".result-quick-add").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        btn.disabled = true;
        btn.textContent = "…";
        try {
          const res = await apiFetch(`${API}/api/watchlist`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              symbol: btn.dataset.symbol,
              exchange: btn.dataset.exchange,
              display_name: btn.dataset.name,
              horizon: currentHorizon,
            }),
          });
          const resData = await res.json();
          if (!res.ok) {
            showToast(resData.detail || "Could not add stock", "error");
            btn.disabled = false;
            btn.textContent = "+";
            return;
          }
          btn.textContent = "✓";
          btn.classList.add("added");
          showToast(`${btn.dataset.symbol} added — 90-day backtest calibrating`, "success");
          refreshTicker();
        } catch (e2) {
          showToast("Network error adding stock", "error");
          btn.disabled = false;
          btn.textContent = "+";
        }
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

    <div id="priceChart" class="price-chart-container"></div>

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

  loadPriceChart(currentStock.symbol, currentStock.exchange, currentHorizon, null);

  document.querySelectorAll("#horizonRow .horizon-pill").forEach((btn) => {
    btn.addEventListener("click", async () => {
      currentHorizon = btn.dataset.h;
      await loadAnalysis();
    });
  });

  document.getElementById("addWatchlistBtn").addEventListener("click", addCurrentToWatchlist);
}

async function loadWatchAnalysis(itemId, silent = false) {
  const content = document.getElementById("analysisContent");
  if (!silent) content.innerHTML = `
    <div class="skeleton-line" style="width:55%; height:26px;"></div>
    <div class="skeleton-line" style="width:30%; margin-top:8px;"></div>
    <div class="skeleton-block" style="height:120px; margin-top:18px;"></div>
    <div class="skeleton-block" style="height:160px; margin-top:14px;"></div>
  `;
  try {
    const res = await apiFetch(`${API}/api/watchlist/${itemId}/analysis`);
    const data = await res.json();
    if (!res.ok) {
      content.innerHTML = `<p class="muted">${escapeHtml(data.detail || "Could not load analysis.")}</p>`;
      return;
    }
    renderWatchAnalysis(data, itemId);
  } catch (e) {
    content.innerHTML = `<p class="muted">Could not reach the backend.</p>`;
  }
}

function renderWatchAnalysis(data, itemId) {
  const delta = data.predicted_price - data.current_price;
  const deltaPct = (delta / data.current_price) * 100;
  const dirClass = delta >= 0 ? "up" : "down";
  const signals = data.signals;
  const weights = data.weights_used || {};

  const signalMeta = [
    ["trend", "Multi-timeframe trend", signals.trend_drift_annualized],
    ["momentum", "Momentum (RSI/MACD)", signals.momentum_tilt_annualized],
    ["news", "News sentiment", signals.news_drift_annualized],
    ["seasonality", "Seasonality (5y history)", signals.seasonality_drift_annualized],
    ["weather", "Weather (experimental)", signals.weather_drift_annualized],
  ];
  const signalRows = signalMeta.map(([key, label, val]) => {
    const cls = val > 0.001 ? "pos" : val < -0.001 ? "neg" : "neu";
    const sign = val > 0 ? "+" : "";
    const w = weights[key];
    const wTag = w != null ? `<span class="weight-tag">${w.toFixed(2)}×</span>` : "";
    return `<div class="signal-row"><span>${label} ${wTag}</span><span class="val ${cls}">${sign}${(val * 100).toFixed(2)}%/yr</span></div>`;
  }).join("");

  const da = data.directional_accuracy;
  const daHtml = da ? `
    <div class="watch-track-record">
      directional accuracy: ${da.overall_pct}% over ${da.resolved_count} resolved predictions
      (early ${da.early_period_pct}% → recent ${da.recent_period_pct}%)
      ${da.improved ? "— improving ✓" : ""}
    </div>` : `<div class="watch-track-record muted">not enough resolved live predictions yet to show a trend</div>`;

  document.getElementById("analysisContent").innerHTML = `
    <h2 class="stock-title">${escapeHtml(data.display_name || data.symbol)}</h2>
    <div class="stock-sub">${data.symbol} &middot; tracking on your watchlist &middot; horizon ${(data.horizon_minutes / 1440).toFixed(2)}d</div>

    <div class="price-hero">
      <span class="price-current">₹${data.current_price.toFixed(2)}</span>
    </div>

    <div id="priceChart" class="price-chart-container"></div>

    <div class="prediction-card">
      <div class="label">predicted price right now, using this stock's refined weights</div>
      <div class="predicted-price">₹${data.predicted_price.toFixed(2)}
        <span class="price-delta ${dirClass}">${delta >= 0 ? "+" : ""}${deltaPct.toFixed(2)}%</span>
      </div>
      <div class="band-row">68% range: ₹${data.band_68[0]} – ₹${data.band_68[1]}</div>
      <div class="band-row">95% range: ₹${data.band_95[0]} – ₹${data.band_95[1]}</div>
      <div class="confidence-bar-track"><div class="confidence-bar-fill" style="width:${data.confidence * 100}%"></div></div>
      <div class="band-row" style="margin-top:6px;">confidence score: ${(data.confidence * 100).toFixed(0)}/100</div>
    </div>

    ${daHtml}

    <div class="section-heading"><span class="eyebrow">why · refined weights shown</span><h2 style="font-size:17px;">Signal breakdown</h2></div>
    <div class="signal-list">${signalRows}</div>

    <div class="disclaimer">${data.disclaimer} Raw daily error naturally stays noisy - directional accuracy above and the weight trend on the next screen are the honest signs of whether refinement is working.</div>

    <button class="add-watchlist-btn" id="viewBacktestBtn">View 90-day backtest &amp; refinement log ›</button>
  `;

  document.getElementById("viewBacktestBtn").addEventListener("click", () => openWatchDetail(itemId));
  loadPriceChart(data.symbol, null, minutesToHorizonLabel(data.horizon_minutes), weights, data.symbol);
}

const HORIZON_MINUTES = { "15m": 15, "1h": 60, "4h": 240, "1d": 1440, "3d": 4320, "1wk": 10080, "1mo": 43200, "3mo": 129600 };
function minutesToHorizonLabel(minutes) {
  let best = "1d", bestDiff = Infinity;
  for (const [label, m] of Object.entries(HORIZON_MINUTES)) {
    const diff = Math.abs(m - minutes);
    if (diff < bestDiff) { bestDiff = diff; best = label; }
  }
  return best;
}

// ---------- Price chart (Lightweight Charts) ----------
let priceChartInstance = null;
async function loadPriceChart(symbol, exchange, horizon, weights, yfSymbolOverride = null) {
  const container = document.getElementById("priceChart");
  if (!container || typeof LightweightCharts === "undefined") return;
  container.innerHTML = `<p class="loading">loading chart…</p>`;
  try {
    const params = new URLSearchParams({ horizon: horizon || "1d" });
    if (exchange) params.set("exchange", exchange);
    if (weights) params.set("weights", JSON.stringify(weights));
    if (yfSymbolOverride) params.set("yf_symbol_override", yfSymbolOverride);
    const res = await apiFetch(`${API}/api/stocks/${encodeURIComponent(symbol)}/chart?${params.toString()}`);
    const data = await res.json();
    if (!res.ok) {
      container.innerHTML = `<p class="muted">Chart unavailable: ${escapeHtml(data.detail || "unknown error")}</p>`;
      return;
    }
    container.innerHTML = "";
    renderPriceChart(container, data.historical, data.forecast);
  } catch (e) {
    container.innerHTML = `<p class="muted">Chart failed to load.</p>`;
  }
}

function renderPriceChart(container, historical, forecast) {
  if (priceChartInstance) {
    try { priceChartInstance.remove(); } catch (e) { /* already gone */ }
    priceChartInstance = null;
  }
  const chart = LightweightCharts.createChart(container, {
    height: 260,
    layout: { background: { color: "transparent" }, textColor: "#8FA39A" },
    grid: { vertLines: { color: "rgba(255,255,255,0.05)" }, horzLines: { color: "rgba(255,255,255,0.05)" } },
    rightPriceScale: { borderColor: "rgba(255,255,255,0.1)" },
    timeScale: { borderColor: "rgba(255,255,255,0.1)", timeVisible: true, secondsVisible: false },
  });
  priceChartInstance = chart;

  const candles = chart.addSeries(LightweightCharts.CandlestickSeries, {
    upColor: "#3c9a5c", downColor: "#C1443C", borderVisible: false,
    wickUpColor: "#3c9a5c", wickDownColor: "#C1443C",
  });
  candles.setData(historical);

  // Confidence "band" via bounding lines (dashed, widening outward) rather
  // than a filled polygon - Lightweight Charts has no built-in fill-between-
  // two-lines series, and a custom-series hack isn't worth the fragility.
  const mkLine = (color, width, dashed) => chart.addSeries(LightweightCharts.LineSeries, {
    color, lineWidth: width, lineStyle: dashed ? 2 : 0, priceLineVisible: false, lastValueVisible: false,
  });
  const mid = mkLine("#C9A227", 2, false);
  const b68Hi = mkLine("#C9A227", 1, true);
  const b68Lo = mkLine("#C9A227", 1, true);
  const b95Hi = mkLine("#8FA39A", 1, true);
  const b95Lo = mkLine("#8FA39A", 1, true);

  mid.setData(forecast.map((p) => ({ time: p.time, value: p.mid })));
  b68Hi.setData(forecast.map((p) => ({ time: p.time, value: p.high_68 })));
  b68Lo.setData(forecast.map((p) => ({ time: p.time, value: p.low_68 })));
  b95Hi.setData(forecast.map((p) => ({ time: p.time, value: p.high_95 })));
  b95Lo.setData(forecast.map((p) => ({ time: p.time, value: p.low_95 })));

  chart.timeScale().fitContent();
}

async function addCurrentToWatchlist() {
  const btn = document.getElementById("addWatchlistBtn");
  btn.disabled = true;
  btn.textContent = "Running 90-day backtest & calibrating…";
  try {
    const res = await apiFetch(`${API}/api/watchlist`, {
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

// ---------- Watch analysis (live, right now, using this stock's refined weights) ----------
function openWatchAnalysis(itemId) {
  navigateTo("view-analysis", { itemId, fromWatchlist: true });
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
    const res = await apiFetch(`${API}/api/watchlist/${itemId}/detail`);
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
  const da = data.directional_accuracy;

  const summaryHtml = bt ? `
    <div class="prediction-card">
      <div class="label">90-day walk-forward backtest</div>
      <div class="band-row" style="margin-top:6px;">tested on ${bt.total_days_backtested} trading days</div>
      <div class="band-row">overall avg error: <b>${bt.avg_abs_error_pct_overall}%</b> (early ${bt.avg_abs_error_pct_early_period}% → recent ${bt.avg_abs_error_pct_recent_period}%)</div>
      ${bt.directional_hit_rate_overall != null ? `
        <div class="band-row" style="margin-top:6px;">directional accuracy: <b>${bt.directional_hit_rate_overall}%</b> (early ${bt.directional_hit_rate_early_period}% → recent ${bt.directional_hit_rate_recent_period}%)</div>
      ` : ""}
      <div class="band-row" style="margin-top:6px; color:${bt.directional_improved ? "var(--brass)" : "var(--signal-red)"}">
        ${bt.directional_improved ? "Directional accuracy improved as weights refined ✓" : "No clear directional improvement yet on this stock"}
      </div>
    </div>
  ` : `<p class="muted">Backtest still running or there wasn't enough history for this stock — check back shortly.</p>`;

  const liveDaHtml = da ? `
    <div class="watch-track-record">
      live directional accuracy since adding: ${da.overall_pct}% over ${da.resolved_count} resolved
      (early ${da.early_period_pct}% → recent ${da.recent_period_pct}%) ${da.improved ? "— improving ✓" : ""}
    </div>` : `<div class="watch-track-record muted">not enough resolved live predictions yet</div>`;

  const weightRows = Object.entries(weights).map(([name, val]) => {
    return `<div class="signal-row"><span>${name}</span><span class="val ${val >= 1 ? "pos" : "neg"}">${val.toFixed(2)}×</span></div>`;
  }).join("");

  // Weight movement: compare the earliest recorded snapshot to the current
  // weights, so it's visible that refinement is actually happening even
  // when raw error % bounces around day to day.
  const wh = data.weights_history || [];
  const movementHtml = wh.length >= 2 ? (() => {
    const first = wh[0].weights;
    const rows = Object.entries(weights).map(([name, now]) => {
      const start = first[name] ?? 1.0;
      const changed = Math.abs(now - start) > 0.001;
      return `<div class="signal-row"><span>${name}</span><span class="val ${changed ? (now > start ? "pos" : "neg") : "neu"}">${start.toFixed(2)}× → ${now.toFixed(2)}×</span></div>`;
    }).join("");
    return `
      <div class="section-heading"><span class="eyebrow">${wh.length} snapshots recorded</span><h2 style="font-size:17px;">Weight movement (start → now)</h2></div>
      <div class="signal-list">${rows}</div>`;
  })() : "";

  const history = (data.backtest_history || []).slice().reverse();
  const historyHtml = history.length ? `
    <table class="backtest-table">
      <thead><tr><th>date</th><th>predicted</th><th>actual</th><th>error</th><th>dir</th></tr></thead>
      <tbody>
        ${history.map((h) => `
          <tr>
            <td>${h.date}</td>
            <td>₹${fmtPrice(h.predicted_price)}</td>
            <td>₹${fmtPrice(h.actual_price)}</td>
            <td class="${h.error_pct >= 0 ? "pos" : "neg"}">${h.error_pct}%</td>
            <td>${h.correct_direction == null ? "—" : (h.correct_direction ? "✓" : "✗")}</td>
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
            <td>${fmtISTDate(p.made_at)}</td>
            <td>₹${fmtPrice(p.predicted_price)}</td>
            <td>${p.actual_price != null ? "₹" + fmtPrice(p.actual_price) : "—"}</td>
            <td>${p.resolved ? (p.error_pct + "%" + (p.correct_direction == null ? "" : (p.correct_direction ? " ✓" : " ✗"))) : "pending"}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  ` : `<p class="muted">No live predictions resolved yet.</p>`;

  document.getElementById("watchDetailContent").innerHTML = `
    <h2 class="stock-title">${escapeHtml(data.display_name || data.symbol)}</h2>
    <div class="stock-sub">${data.symbol} &middot; horizon ${data.horizon_minutes}m</div>
    <button class="live-analysis-link" id="jumpToLiveAnalysis">‹ live analysis</button>

    <div class="section-heading"><span class="eyebrow">calibration</span><h2 style="font-size:17px;">90-day backtest</h2></div>
    ${summaryHtml}

    <div class="section-heading"><span class="eyebrow">since adding</span><h2 style="font-size:17px;">Live track record</h2></div>
    ${liveDaHtml}

    <div class="section-heading"><span class="eyebrow">self-refinement</span><h2 style="font-size:17px;">Current signal weights</h2></div>
    <div class="signal-list">${weightRows}</div>
    <div class="disclaimer">
      Weights above 1× mean that signal has been right more often for this specific stock and is
      trusted more; below 1× means it's been trusted less. Raw % error on any single day stays
      noisy - that's real market randomness, not broken refinement. Directional accuracy and the
      weight movement below are the honest signs that learning is happening.
    </div>
    ${movementHtml}

    <div class="section-heading"><span class="eyebrow">walk-forward</span><h2 style="font-size:17px;">Backtest days (all ${history.length})</h2></div>
    ${historyHtml}

    <div class="section-heading"><span class="eyebrow">since adding</span><h2 style="font-size:17px;">Live predictions (most recent 10)</h2></div>
    ${liveHtml}
  `;

  document.getElementById("jumpToLiveAnalysis").addEventListener("click", () => openWatchAnalysis(data.id));
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
    const res = await apiFetch(`${API}/api/watchlist`);
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
        await apiFetch(`${API}/api/watchlist/${btn.dataset.id}`, { method: "DELETE" });
        showToast(`Removed ${symbol} from watchlist`);
        loadWatchlist();
        refreshTicker();
      });
    });
    content.querySelectorAll(".watch-symbol-link").forEach((el) => {
      el.addEventListener("click", () => openWatchAnalysis(el.dataset.id));
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
    ? `<div class="watch-row"><span>target time</span><span>${fmtISTDateTime(lp.target_at)}</span></div>`
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
    const res = await apiFetch(`${API}/api/watchlist`);
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
