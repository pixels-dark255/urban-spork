/* =========================================================================
   Urban Spork platform screens: Intraday engine, Market, Paper, Predictions,
   Settings and More.

   Loaded after app.js and reuses its helpers (apiFetch, showToast,
   escapeHtml, fmtPrice, navigateTo, startViewPolling). The original
   analysis/watchlist screens are untouched; this file wraps renderView to
   add handling for the new views rather than rewriting the router.
   ========================================================================= */

const REC_LABELS = {
  STRONG_BUY: "Strong Buy", BUY: "Buy", HOLD: "Hold",
  SELL: "Sell", STRONG_SELL: "Strong Sell", NO_TRADE: "No Trade",
};
const REC_TONE = {
  STRONG_BUY: "buy strong", BUY: "buy", HOLD: "hold",
  SELL: "sell", STRONG_SELL: "sell strong", NO_TRADE: "none",
};

const state = {
  timeframes: [],
  timeframe: "5m",
  stock: null,          // { symbol, exchange, name }
  analysis: null,
  settings: null,
};

const inr = (n) =>
  n == null || Number.isNaN(n) ? "—" : "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const pct = (n, digits = 1) => (n == null ? "—" : `${Number(n).toFixed(digits)}%`);
const signed = (n, digits = 2) => (n == null ? "—" : `${n >= 0 ? "+" : ""}${Number(n).toFixed(digits)}`);

function esc(s) {
  return typeof escapeHtml === "function" ? escapeHtml(s) : String(s ?? "");
}

/* ---------------------------------------------------------------- routing */

// The desktop 3-column dashboard only covers the original screens. Marking
// the body tells the stylesheet whether to lay the page out as that grid or
// as one full-width screen for the newer tabs.
const DASHBOARD_VIEWS = new Set([
  "view-search", "view-analysis", "view-watchlist", "view-watch-detail",
  "view-intraday", "view-intraday-detail",
]);

function applyLayoutMode(viewId) {
  document.body.classList.toggle("dash-mode", DASHBOARD_VIEWS.has(viewId));
}

const baseRenderView = window.renderView;
window.renderView = function (viewId, viewState = {}) {
  applyLayoutMode(viewId);
  baseRenderView(viewId, viewState);
  try {
    platformRenderView(viewId, viewState);
  } catch (e) {
    console.error("platform view error", e);
  }
};

// First paint happens before any navigation, so set the mode from whatever
// view the markup starts on.
applyLayoutMode(document.querySelector(".view.active")?.id || "view-search");

function platformRenderView(viewId) {
  if (viewId === "view-signals") {
    ensureTimeframes();
    if (state.stock) loadSignal();
  } else if (viewId === "view-market") {
    loadMarket();
    startViewPolling(() => loadMarket(true), 120000);
  } else if (viewId === "view-paper") {
    loadPaper();
    startViewPolling(() => loadPaper(true), 30000);
  } else if (viewId === "view-predictions") {
    loadPredictions();
  } else if (viewId === "view-settings") {
    loadSettings();
  } else if (viewId === "view-more") {
    loadPlatformStatus();
  }
}

document.querySelectorAll("[data-goto]").forEach((btn) =>
  btn.addEventListener("click", () => navigateTo(btn.dataset.goto))
);
document.getElementById("backFromPredictions").addEventListener("click", () => history.back());
document.getElementById("backFromSettings").addEventListener("click", () => history.back());
document.getElementById("refreshMarketBtn").addEventListener("click", () => loadMarket(false, true));
document.getElementById("refreshPaperBtn").addEventListener("click", () => loadPaper());

/* ------------------------------------------------------- stock + timeframe */

const signalInput = document.getElementById("signalSearchInput");
const signalResults = document.getElementById("signalSearchResults");
const signalClear = document.getElementById("signalSearchClear");
let signalDebounce = null;

signalInput.addEventListener("input", () => {
  const q = signalInput.value.trim();
  signalClear.hidden = !q;
  clearTimeout(signalDebounce);
  if (q.length < 2) {
    signalResults.innerHTML = "";
    return;
  }
  signalDebounce = setTimeout(() => runSignalSearch(q), 220);
});
signalClear.addEventListener("click", () => {
  signalInput.value = "";
  signalResults.innerHTML = "";
  signalClear.hidden = true;
});

async function runSignalSearch(q) {
  try {
    const res = await apiFetch(`${API}/api/stocks/search?q=${encodeURIComponent(q)}&limit=12`);
    const data = await res.json();
    if (!data.results.length) {
      signalResults.innerHTML = `<p class="muted">Nothing matched “${esc(q)}”.</p>`;
      return;
    }
    signalResults.innerHTML = data.results
      .map(
        (r) => `<button class="result-item" data-symbol="${esc(r.symbol)}"
                 data-exchange="${esc(r.exchange)}" data-name="${esc(r.name)}">
          <span class="result-symbol">${esc(r.symbol)}</span>
          <span class="result-name">${esc(r.name)}</span>
          <span class="result-meta">${esc(r.exchange)}${r.sector ? " · " + esc(r.sector) : ""}</span>
        </button>`
      )
      .join("");
    signalResults.querySelectorAll(".result-item").forEach((el) =>
      el.addEventListener("click", () => {
        pickStock({ symbol: el.dataset.symbol, exchange: el.dataset.exchange, name: el.dataset.name });
      })
    );
  } catch (e) {
    signalResults.innerHTML = `<p class="error">Search failed: ${esc(e.message)}</p>`;
  }
}

function pickStock(stock) {
  state.stock = stock;
  signalResults.innerHTML = "";
  signalInput.value = "";
  signalClear.hidden = true;
  const box = document.getElementById("signalPicked");
  box.hidden = false;
  box.innerHTML = `<div>
      <strong>${esc(stock.symbol)}</strong>
      <span class="muted">${esc(stock.name)} · ${esc(stock.exchange)}</span>
    </div>
    <button class="ghost-btn" id="clearPicked">change</button>`;
  document.getElementById("clearPicked").addEventListener("click", () => {
    state.stock = null;
    state.analysis = null;
    box.hidden = true;
    document.getElementById("signalContent").innerHTML =
      `<p class="muted">Choose a stock above, then a timeframe.</p>`;
  });
  loadSignal();
}

// Exposed so the search tab's results can hand a stock straight to the engine.
window.urbanSporkPickStock = (stock) => {
  navigateTo("view-signals");
  ensureTimeframes().then(() => pickStock(stock));
};

async function ensureTimeframes() {
  if (state.timeframes.length) {
    renderTimeframes();
    return;
  }
  try {
    const res = await fetch(`${API}/api/timeframes`);
    const data = await res.json();
    state.timeframes = data.timeframes || [];
  } catch (e) {
    state.timeframes = [{ label: "5m", minutes: 5 }];
  }
  renderTimeframes();
}

function renderTimeframes() {
  const host = document.getElementById("signalTimeframes");
  host.innerHTML = state.timeframes
    .map(
      (tf) => `<button class="tf-chip ${tf.label === state.timeframe ? "active" : ""}"
        data-tf="${esc(tf.label)}" title="${esc(tf.note || `Holds for about ${tf.horizon_minutes} minutes`)}">
        ${esc(tf.label)}${tf.approximated ? "<sup>*</sup>" : ""}</button>`
    )
    .join("");
  host.querySelectorAll(".tf-chip").forEach((chip) =>
    chip.addEventListener("click", () => {
      state.timeframe = chip.dataset.tf;
      renderTimeframes();
      if (state.stock) loadSignal();
    })
  );
}

/* ------------------------------------------------------------ the analysis */

async function loadSignal() {
  if (!state.stock) return;
  const host = document.getElementById("signalContent");
  host.innerHTML = `<div class="skeleton-card"></div><div class="skeleton-card"></div>`;
  const { symbol, exchange } = state.stock;
  try {
    const res = await apiFetch(
      `${API}/api/intraday/analyze?symbol=${encodeURIComponent(symbol)}` +
        `&exchange=${encodeURIComponent(exchange)}&timeframe=${encodeURIComponent(state.timeframe)}`
    );
    if (!res.ok) throw new Error((await res.json()).detail || `HTTP ${res.status}`);
    state.analysis = await res.json();
    renderSignal(state.analysis);
  } catch (e) {
    host.innerHTML = `<p class="error">Could not analyse ${esc(symbol)}: ${esc(e.message)}</p>`;
  }
}

function renderSignal(d) {
  const host = document.getElementById("signalContent");
  const rec = d.recommendation || "NO_TRADE";

  // With no bars there are no indicators, no votes and no chart. Rendering
  // the empty shells of those cards just looks broken - say what's missing
  // and stop.
  if (d.data_available === false) {
    host.innerHTML = `
      <div class="rec-card none">
        <div class="rec-head"><div>
          <span class="rec-badge">No Trade</span>
          <span class="rec-symbol">${esc(d.symbol)} · ${esc(d.timeframe)}</span>
        </div></div>
        <div class="no-trade-box">
          <strong>No data.</strong>
          <p>${esc(d.reason || "No bars available for this stock on this timeframe.")}</p>
        </div>
      </div>
      <p class="footnote">The platform stores every bar it fetches, so history for this stock
        builds up as it is tracked. Try a longer timeframe, or a more liquid stock.</p>`;
    return;
  }

  const plan = d.trade_plan;
  const ind = d.indicators || {};
  const confPct = Math.round((d.confidence || 0) * 100);

  const planHtml = plan
    ? `<div class="plan-grid">
        ${planCell("Entry", inr(plan.entry_price))}
        ${planCell("Stop loss", inr(plan.stop_loss), `${pct(plan.stop_pct, 2)} away · ${plan.stop_basis.replace(/_/g, " ")}`)}
        ${planCell("Target", inr(plan.target_price), pct(plan.target_pct, 2) + " away")}
        ${planCell("Position size", `${plan.position_size} sh`, inr(plan.capital_deployed) + " deployed")}
        ${planCell("Risk", inr(plan.risk_amount))}
        ${planCell("Reward", inr(plan.reward_amount))}
        ${planCell("Risk / reward", plan.risk_reward_label)}
        ${planCell("Confidence", confPct + "%")}
      </div>
      <button class="primary-btn" id="openPaperBtn">Open as paper trade</button>`
    : `<div class="no-trade-box">
        <strong>No trade.</strong>
        <p>${esc(d.reason || "Conditions do not justify a position right now.")}</p>
        ${d.downgraded_from ? `<p class="muted">The signal itself read ${esc(REC_LABELS[d.downgraded_from] || d.downgraded_from)}, but the risk engine vetoed it.</p>` : ""}
      </div>`;

  const componentsHtml = Object.entries(d.components || {})
    .map(([name, comp]) => {
      const label = name.replace(/_/g, " ");
      if (!comp.available) {
        return `<div class="component off"><span class="component-name">${esc(label)}</span>
          <span class="muted">${esc(comp.reason || "no data")}</span></div>`;
      }
      const score = comp.score || 0;
      const width = Math.min(50, Math.abs(score) * 50);
      const weight = (d.weights || {})[name]?.weight;
      return `<div class="component">
        <div class="component-head">
          <span class="component-name">${esc(label)}</span>
          <span class="component-weight">${weight ? Math.round(weight * 100) + "% of vote" : ""}</span>
        </div>
        <div class="component-bar">
          <span class="component-fill ${score >= 0 ? "pos" : "neg"}"
                style="width:${width}%; ${score >= 0 ? "left:50%" : `left:${50 - width}%`}"></span>
        </div>
        <div class="component-reasons">${(comp.reasons || []).map((r) => esc(r)).join(" · ") || "—"}</div>
      </div>`;
    })
    .join("");

  host.innerHTML = `
    <div class="rec-card ${REC_TONE[rec] || "none"}">
      <div class="rec-head">
        <div>
          <span class="rec-badge">${esc(REC_LABELS[rec] || rec)}</span>
          <span class="rec-symbol">${esc(d.symbol)} · ${esc(d.timeframe)}</span>
        </div>
        <div class="rec-conf">
          <div class="conf-bar"><span style="width:${confPct}%"></span></div>
          <small>${confPct}% confidence</small>
        </div>
      </div>
      <div class="rec-sub">
        <span class="regime-pill">${esc((d.market_regime || "").replace(/_/g, " "))}</span>
        <span class="muted">${inr(d.entry_price)} · ${d.bars_analysed} bars · holds ~${d.horizon_minutes} min</span>
      </div>
      ${d.timeframe_detail?.approximated ? `<p class="footnote">* ${esc(d.timeframe_detail.note)}</p>` : ""}
      ${planHtml}
    </div>

    <div class="card">
      <h3>Why</h3>
      <ul class="explain-list">${(d.explanation || []).map((l) => `<li>${esc(l)}</li>`).join("")}</ul>
    </div>

    <div class="card">
      <h3>Signal breakdown</h3>
      ${componentsHtml}
      ${(d.penalties || []).length ? `<p class="footnote">Confidence reduced for: ${d.penalties.map(esc).join(", ")}.</p>` : ""}
    </div>

    <div class="card">
      <h3>Indicators</h3>
      <div class="ind-grid">
        ${indCell("RSI (14)", ind.rsi)}
        ${indCell("ADX", ind.adx)}
        ${indCell("+DI / −DI", `${ind.plus_di ?? "—"} / ${ind.minus_di ?? "—"}`)}
        ${indCell("MACD hist", ind.macd_hist)}
        ${indCell("EMA 9 / 21", `${ind.ema9 ?? "—"} / ${ind.ema21 ?? "—"}`)}
        ${indCell("EMA 50", ind.ema50)}
        ${indCell("VWAP", ind.vwap)}
        ${indCell("ATR", `${ind.atr ?? "—"} (${pct(ind.atr_pct, 2)})`)}
        ${indCell("Bollinger %B", ind.bollinger?.percent_b)}
        ${indCell("Volume vs avg", ind.volume?.volume_ratio ? ind.volume.volume_ratio + "×" : "—")}
        ${indCell("Support", inr(ind.levels?.nearest_support))}
        ${indCell("Resistance", inr(ind.levels?.nearest_resistance))}
      </div>
    </div>

    ${chartCard(d)}

    <p class="footnote">Statistical estimate, not financial advice. Confidence is a heuristic —
      the Predictions tab shows how these calls have actually worked out.</p>
  `;

  const paperBtn = document.getElementById("openPaperBtn");
  if (paperBtn) paperBtn.addEventListener("click", () => openPaperTrade(paperBtn));
  renderCandles(document.getElementById("signalChart"), d.chart || [], plan);
}

function chartCard(d) {
  // The charting library comes from a CDN. If it did not load (offline, or a
  // blocked CDN) an empty bordered box is worse than no box - say why.
  if (!(d.chart || []).length) return "";
  if (typeof LightweightCharts === "undefined") {
    return `<div class="card"><h3>Price</h3>
      <p class="muted">Chart library unavailable — the numbers above are unaffected.</p></div>`;
  }
  return `<div class="card"><h3>Price</h3><div id="signalChart" class="chart-host"></div></div>`;
}

function planCell(label, value, sub) {
  return `<div class="plan-cell"><small>${esc(label)}</small><strong>${esc(value)}</strong>${
    sub ? `<em>${esc(sub)}</em>` : ""
  }</div>`;
}
function indCell(label, value) {
  return `<div class="ind-cell"><small>${esc(label)}</small><strong>${
    value == null || value === "" ? "—" : esc(value)
  }</strong></div>`;
}

function renderCandles(host, points, plan) {
  if (!host || !points.length || typeof LightweightCharts === "undefined") return;
  host.innerHTML = "";
  const chart = LightweightCharts.createChart(host, {
    width: host.clientWidth,
    height: 240,
    layout: { background: { color: "transparent" }, textColor: "#8fa39a" },
    grid: { vertLines: { color: "rgba(255,255,255,0.04)" }, horzLines: { color: "rgba(255,255,255,0.04)" } },
    rightPriceScale: { borderVisible: false },
    timeScale: { borderVisible: false, timeVisible: true },
  });
  const series = chart.addSeries
    ? chart.addSeries(LightweightCharts.CandlestickSeries, {
        upColor: "#4ade80", downColor: "#f87171", borderVisible: false,
        wickUpColor: "#4ade80", wickDownColor: "#f87171",
      })
    : chart.addCandlestickSeries({ upColor: "#4ade80", downColor: "#f87171" });
  series.setData(points.map((p) => ({ time: p.time, open: p.open, high: p.high, low: p.low, close: p.close })));
  if (plan) {
    // Draw the actual plan on the chart - a stop and target you can see
    // beats two numbers in a table.
    series.createPriceLine({ price: plan.entry_price, color: "#93c5fd", lineWidth: 1, title: "entry" });
    series.createPriceLine({ price: plan.stop_loss, color: "#f87171", lineWidth: 1, title: "stop" });
    series.createPriceLine({ price: plan.target_price, color: "#4ade80", lineWidth: 1, title: "target" });
  }
  chart.timeScale().fitContent();
}

async function openPaperTrade(btn) {
  if (!state.stock) return;
  btn.disabled = true;
  btn.textContent = "Opening…";
  try {
    const res = await apiFetch(`${API}/api/paper/open`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol: state.stock.symbol, exchange: state.stock.exchange, timeframe: state.timeframe,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    showToast(`Paper trade opened: ${data.trade.side} ${data.trade.quantity} ${data.trade.symbol}`, "success");
    btn.textContent = "Opened ✓";
  } catch (e) {
    showToast(e.message, "error");
    btn.disabled = false;
    btn.textContent = "Open as paper trade";
  }
}

/* ------------------------------------------------------------------ market */

async function loadMarket(silent = false, force = false) {
  const host = document.getElementById("marketContent");
  if (!silent) host.innerHTML = `<div class="skeleton-card"></div>`;
  try {
    const res = await fetch(`${API}/api/market/overview${force ? "?force=true" : ""}`);
    const d = await res.json();
    const sentiment = d.sentiment || {};
    host.innerHTML = `
      ${d.stale ? `<p class="stale-note">${esc(d.stale_note || "Showing stored data.")}</p>` : ""}
      <div class="card">
        <h3>Sentiment</h3>
        <p class="sentiment ${esc((sentiment.label || "NEUTRAL").toLowerCase())}">
          ${esc((sentiment.label || "NEUTRAL").replace(/_/g, " "))}
        </p>
        <ul class="explain-list">${(sentiment.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("") || "<li>No data.</li>"}</ul>
        ${d.breadth?.sample_size ? `<p class="footnote">Breadth sampled across ${d.breadth.sample_size} large caps:
          ${d.breadth.advancing} up / ${d.breadth.declining} down.</p>` : ""}
      </div>
      <div class="index-grid">
        ${(d.indices || []).map(indexCard).join("")}
      </div>
      ${moversCard("Top gainers", d.gainers)}
      ${moversCard("Top losers", d.losers)}
      ${sectorCard(d.sectors)}
    `;
  } catch (e) {
    host.innerHTML = `<p class="error">Could not load the market overview: ${esc(e.message)}</p>`;
  }
}

function indexCard(idx) {
  if (!idx.available) return `<div class="index-card off"><strong>${esc(idx.name)}</strong><small>unavailable</small></div>`;
  const tone = idx.change_pct >= 0 ? "pos" : "neg";
  return `<div class="index-card">
    <strong>${esc(idx.name)}</strong>
    <span class="index-price">${Number(idx.price).toLocaleString("en-IN")}</span>
    <span class="${tone}">${signed(idx.change_pct)}%</span>
    <small>${esc(idx.trend.toLowerCase())}</small>
  </div>`;
}

function moversCard(title, rows) {
  if (!rows || !rows.length) return "";
  return `<div class="card"><h3>${esc(title)}</h3>
    <div class="mover-list">${rows
      .map(
        (m) => `<div class="mover">
          <span><strong>${esc(m.symbol)}</strong><small>${esc(m.name || "")}</small></span>
          <span class="${m.change_pct >= 0 ? "pos" : "neg"}">${signed(m.change_pct)}%</span>
        </div>`
      )
      .join("")}</div></div>`;
}

function sectorCard(sectors) {
  if (!sectors || !sectors.length) return "";
  return `<div class="card"><h3>Sector performance</h3>
    <div class="mover-list">${sectors
      .map(
        (s) => `<div class="mover">
          <span><strong>${esc(s.sector)}</strong><small>${s.stocks} stocks</small></span>
          <span class="${s.avg_change_pct >= 0 ? "pos" : "neg"}">${signed(s.avg_change_pct)}%</span>
        </div>`
      )
      .join("")}</div></div>`;
}

/* ------------------------------------------------------------------- paper */

async function loadPaper(silent = false) {
  const host = document.getElementById("paperContent");
  if (!silent) host.innerHTML = `<div class="skeleton-card"></div>`;
  try {
    await apiFetch(`${API}/api/paper/mark`, { method: "POST" });
    const res = await apiFetch(`${API}/api/paper/positions`);
    const d = await res.json();
    const s = d.summary || {};
    host.innerHTML = `
      <div class="card">
        <h3>Scorecard</h3>
        <div class="ind-grid">
          ${indCell("Total P&L", inr(s.total_pnl))}
          ${indCell("Realised", inr(s.realised_pnl))}
          ${indCell("Unrealised", inr(s.unrealised_pnl))}
          ${indCell("Return", pct(s.return_pct, 2))}
          ${indCell("Closed trades", s.closed_trades)}
          ${indCell("Win rate", s.win_rate_pct == null ? "—" : pct(s.win_rate_pct))}
          ${indCell("Avg win", inr(s.avg_win))}
          ${indCell("Avg loss", inr(s.avg_loss))}
          ${indCell("Profit factor", s.profit_factor ?? "—")}
          ${indCell("Expectancy / trade", inr(s.expectancy_per_trade))}
        </div>
        <p class="footnote">${esc(s.note || "")}</p>
        ${s.daily_loss_status?.breached
          ? `<p class="error">Daily loss limit reached — the engine will refuse new trades today.</p>`
          : `<p class="footnote">Daily loss budget used: ${pct(s.daily_loss_status?.budget_used_pct || 0)} of ${inr(s.daily_loss_status?.loss_budget)}.</p>`}
      </div>
      <div class="card">
        <h3>Open positions</h3>
        ${d.open.length ? d.open.map(openTradeRow).join("") : `<p class="muted">No open paper trades. Open one from the Intraday tab.</p>`}
      </div>
      <div class="card">
        <h3>Closed trades</h3>
        ${d.closed.length ? d.closed.map(closedTradeRow).join("") : `<p class="muted">Nothing closed yet.</p>`}
      </div>`;
    host.querySelectorAll("[data-close-trade]").forEach((btn) =>
      btn.addEventListener("click", () => closePaperTrade(btn.dataset.closeTrade, btn))
    );
  } catch (e) {
    host.innerHTML = `<p class="error">Could not load paper trades: ${esc(e.message)}</p>`;
  }
}

function openTradeRow(t) {
  const tone = t.unrealised_pnl >= 0 ? "pos" : "neg";
  return `<div class="trade-row">
    <div class="trade-main">
      <strong>${esc(t.side)} ${t.quantity} ${esc(t.symbol)}</strong>
      <small>${esc(t.timeframe)} · entry ${inr(t.entry_price)} · SL ${inr(t.stop_loss)} · TGT ${inr(t.target_price)}</small>
    </div>
    <div class="trade-side">
      <span class="${tone}">${inr(t.unrealised_pnl)} (${signed(t.unrealised_pnl_pct)}%)</span>
      <button class="ghost-btn" data-close-trade="${t.id}">close</button>
    </div>
  </div>`;
}

function closedTradeRow(t) {
  const tone = (t.pnl || 0) >= 0 ? "pos" : "neg";
  return `<div class="trade-row">
    <div class="trade-main">
      <strong>${esc(t.side)} ${t.quantity} ${esc(t.symbol)}</strong>
      <small>${esc(t.timeframe)} · ${inr(t.entry_price)} → ${inr(t.exit_price)} · ${esc(t.exit_reason || "")}</small>
    </div>
    <div class="trade-side"><span class="${tone}">${inr(t.pnl)} (${signed(t.pnl_pct)}%)</span></div>
  </div>`;
}

async function closePaperTrade(id, btn) {
  btn.disabled = true;
  try {
    const res = await apiFetch(`${API}/api/paper/close/${id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    showToast("Trade closed", "success");
    loadPaper();
  } catch (e) {
    showToast(e.message, "error");
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------- predictions */

async function loadPredictions() {
  const host = document.getElementById("predictionsContent");
  host.innerHTML = `<div class="skeleton-card"></div>`;
  try {
    const [accRes, listRes] = await Promise.all([
      apiFetch(`${API}/api/predictions/accuracy`),
      apiFetch(`${API}/api/predictions?limit=100`),
    ]);
    const acc = await accRes.json();
    const list = (await listRes.json()).predictions || [];

    host.innerHTML = `
      <div class="card">
        <h3>Track record</h3>
        ${acc.resolved_predictions
          ? `<div class="ind-grid">
              ${indCell("Graded", acc.overall.graded)}
              ${indCell("Directional accuracy", acc.overall.directional_accuracy_pct == null ? "—" : pct(acc.overall.directional_accuracy_pct))}
              ${indCell("Target hit", pct(acc.overall.target_hit_pct))}
              ${indCell("Stop hit", pct(acc.overall.stop_hit_pct))}
              ${indCell("Avg gain", pct(acc.overall.avg_gain_pct, 2))}
              ${indCell("Avg loss", pct(acc.overall.avg_loss_pct, 2))}
            </div>
            ${breakdownTable("By timeframe", acc.by_timeframe)}
            ${breakdownTable("By market regime", acc.by_market_regime)}
            ${breakdownTable("By recommendation", acc.by_recommendation)}
            ${breakdownTable("Confidence calibration", acc.confidence_calibration)}`
          : `<p class="muted">${esc(acc.note || "Nothing graded yet.")}</p>`}
        <p class="footnote">${esc(acc.note || "")}</p>
      </div>
      <div class="card">
        <h3>Recent predictions</h3>
        ${list.length ? list.map(predictionRow).join("") : `<p class="muted">No predictions recorded yet.</p>`}
      </div>`;
  } catch (e) {
    host.innerHTML = `<p class="error">Could not load prediction history: ${esc(e.message)}</p>`;
  }
}

function breakdownTable(title, buckets) {
  const rows = Object.entries(buckets || {});
  if (!rows.length) return "";
  return `<h4 class="sub-heading">${esc(title)}</h4>
    <table class="mini-table">
      <thead><tr><th></th><th>n</th><th>direction</th><th>target</th><th>stop</th></tr></thead>
      <tbody>${rows
        .map(
          ([key, s]) => `<tr>
            <td>${esc(key.replace(/_/g, " "))}</td>
            <td>${s.predictions}</td>
            <td>${s.directional_accuracy_pct == null ? "—" : pct(s.directional_accuracy_pct)}</td>
            <td>${pct(s.target_hit_pct)}</td>
            <td>${pct(s.stop_hit_pct)}</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>`;
}

function predictionRow(p) {
  const outcomeTone = p.outcome === "target" ? "pos" : p.outcome === "stop" ? "neg" : "";
  return `<div class="trade-row">
    <div class="trade-main">
      <strong>${esc(REC_LABELS[p.recommendation] || p.recommendation)} ${esc(p.symbol)}</strong>
      <small>${esc(p.timeframe)} · ${fmtISTDateTime(p.made_at + "Z")} · entry ${inr(p.entry_price)}
        · ${Math.round((p.confidence || 0) * 100)}% conf · ${esc((p.market_regime || "").replace(/_/g, " ").toLowerCase())}</small>
    </div>
    <div class="trade-side">
      ${p.resolved
        ? `<span class="${outcomeTone}">${esc(p.outcome || "")} ${signed(p.move_pct)}%</span>`
        : `<span class="muted">pending</span>`}
    </div>
  </div>`;
}

/* ---------------------------------------------------------------- settings */

async function loadSettings() {
  const host = document.getElementById("settingsContent");
  try {
    const res = await apiFetch(`${API}/api/settings`);
    const d = await res.json();
    state.settings = d.settings;
    const s = d.settings;
    host.innerHTML = `
      <div class="card">
        <p class="muted">Every recommendation is sized against these. They are the difference
          between a suggestion and a trade you could actually take.</p>
        <label class="field"><span>Trading capital (₹)</span>
          <input type="number" id="setCapital" value="${s.capital}" min="1000" step="1000"></label>
        <label class="field"><span>Risk per trade (%)</span>
          <input type="number" id="setRisk" value="${s.risk_per_trade_pct}" min="0.05" max="10" step="0.05"></label>
        <label class="field"><span>Max daily loss (%)</span>
          <input type="number" id="setDaily" value="${s.max_daily_loss_pct}" min="0.1" max="50" step="0.1"></label>
        <label class="field"><span>Minimum risk/reward</span>
          <input type="number" id="setRR" value="${s.min_risk_reward}" min="0.5" max="10" step="0.1"></label>
        <label class="field"><span>Minimum confidence to trade</span>
          <input type="number" id="setConf" value="${s.min_confidence}" min="0.3" max="0.95" step="0.01"></label>
        <label class="field"><span>Max open paper positions</span>
          <input type="number" id="setMaxPos" value="${s.max_open_positions}" min="1" max="50" step="1"></label>
        <button class="primary-btn" id="saveSettingsBtn">Save</button>
        <p class="footnote">Raising the confidence floor means fewer trades, not better ones —
          it only changes how selective the engine is allowed to be.</p>
      </div>`;
    document.getElementById("saveSettingsBtn").addEventListener("click", saveSettings);
  } catch (e) {
    host.innerHTML = `<p class="error">Could not load settings: ${esc(e.message)}</p>`;
  }
}

async function saveSettings() {
  const payload = {
    capital: parseFloat(document.getElementById("setCapital").value),
    risk_per_trade_pct: parseFloat(document.getElementById("setRisk").value),
    max_daily_loss_pct: parseFloat(document.getElementById("setDaily").value),
    min_risk_reward: parseFloat(document.getElementById("setRR").value),
    min_confidence: parseFloat(document.getElementById("setConf").value),
    max_open_positions: parseInt(document.getElementById("setMaxPos").value, 10),
  };
  try {
    const res = await apiFetch(`${API}/api/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    state.settings = data.settings;
    showToast("Risk settings saved", "success");
    loadSettings();
  } catch (e) {
    showToast(e.message, "error");
  }
}

/* ------------------------------------------------------------------- about */

async function loadPlatformStatus() {
  const host = document.getElementById("platformStatusBox");
  if (!host) return;
  try {
    const d = await (await fetch(`${API}/api/platform/status`)).json();
    const data = d.historical_data || {};
    host.innerHTML = `
      <h4 class="sub-heading">Platform</h4>
      <ul class="status-list">
        <li>Data provider: <strong>${esc(d.active_provider)}</strong></li>
        <li>Stock universe: <strong>${(d.universe?.total_stocks || 0).toLocaleString("en-IN")}</strong> symbols</li>
        <li>Bars collected: <strong>${(data.total_bars || 0).toLocaleString("en-IN")}</strong>
          across ${data.distinct_symbols || 0} symbols</li>
        <li>ML backends: <strong>${(d.ml_backends || []).join(", ")}</strong></li>
        <li>Models trained: <strong>${d.models_trained || 0}</strong></li>
      </ul>`;
  } catch (e) {
    host.innerHTML = `<p class="muted">Platform status unavailable.</p>`;
  }
}
