// ---------- Strategy Arena ----------
// Several intraday strategies paper-trade the same stocks with the same
// simulated capital; the leaderboard shows which one actually makes money
// after costs. Relies on helpers from app.js (apiFetch, escapeHtml,
// showToast, fmtISTDateTime), which loads first.

const ARENA_LEVEL_CLASS = {
  good: "arena-verdict-good", halted: "arena-verdict-bad", none: "arena-verdict-bad",
  early: "arena-verdict-neutral", waiting: "arena-verdict-neutral",
};
let arenaData = null;
document.getElementById("backFromArena").addEventListener("click", () => history.back());
document.getElementById("openArenaBtn").addEventListener("click", () => navigateTo("view-arena"));
let arenaOpenStrategy = null;

function inr(n, withSign = true) {
  if (n == null || Number.isNaN(n)) return "—";
  const sign = withSign && n > 0 ? "+" : n < 0 ? "−" : "";
  return `${sign}₹${Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}
function pnlClass(n) { return n > 0 ? "pos" : n < 0 ? "neg" : "neu"; }

async function loadArena(silent = false) {
  const content = document.getElementById("arenaContent");
  if (!content) return;
  if (!silent) content.innerHTML = `<div class="skeleton-block" style="height:140px"></div>
    <div class="skeleton-block" style="height:220px; margin-top:14px;"></div>`;
  try {
    const res = await apiFetch(`${API}/api/arena`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "failed");
    arenaData = data;
    // Don't clobber a settings form the user is typing in.
    const editing = content.querySelector("#arenaSettings[open]");
    if (silent && editing) { renderArenaLive(data); return; }
    renderArena(data);
    loadLastBacktest();
  } catch (e) {
    if (!silent) content.innerHTML = `<p class="muted">Could not load the arena.</p>`;
  }
}

function renderBoard(rows, { live = true } = {}) {
  if (!rows.length) return `<p class="muted">No strategies yet.</p>`;
  return `
    <div class="table-scroll"><table class="backtest-table arena-board">
      <thead><tr>
        <th>#</th><th>strategy</th><th>net (after costs)</th><th>vs bench</th>
        <th>trades</th><th>win</th><th>PF</th><th>max DD</th>${live ? "<th>today</th>" : ""}
      </tr></thead>
      <tbody>
      ${rows.map((r, i) => `
        <tr class="arena-row ${r.benchmark ? "arena-bench" : ""}" data-id="${escapeHtml(r.id)}" title="${escapeHtml(r.status_reason || "")}">
          <td>${r.benchmark ? "—" : i + 1}</td>
          <td>
            <div class="arena-name">${escapeHtml(r.name)}</div>
            <span class="arena-pill ${r.status === "active" ? "arena-pill-on" : "arena-pill-off"}">
              ${r.benchmark ? "yardstick" : r.status === "active" ? "active" : "benched · shadow"}
            </span>
          </td>
          <td class="${pnlClass(r.net_total)}">${inr(r.net_total)}<div class="arena-sub">₹${Math.round(r.charges_total)} costs</div></td>
          <td class="${pnlClass(r.vs_benchmark)}">${r.vs_benchmark == null ? "—" : inr(r.vs_benchmark)}</td>
          <td>${r.trades}</td>
          <td>${r.win_rate == null ? "—" : r.win_rate + "%"}</td>
          <td>${r.profit_factor == null ? "—" : r.profit_factor}</td>
          <td class="neg">${r.max_drawdown ? inr(-r.max_drawdown) : "—"}</td>
          ${live ? `<td class="${pnlClass(r.today.net)}">${inr(r.today.net)}
             <div class="arena-sub">${r.today.trades} tr${r.today.open_positions ? ` · ${r.today.open_positions} open` : ""}${r.today.stopped ? " · stopped" : ""}</div></td>` : ""}
        </tr>
        ${live && arenaOpenStrategy === r.id ? `<tr class="arena-detail-row"><td colspan="9"><div id="arenaTrades-${escapeHtml(r.id)}"><p class="loading">loading trades…</p></div></td></tr>` : ""}
      `).join("")}
      </tbody>
    </table></div>`;
}

function renderTrades(trades) {
  if (!trades.length) return `<p class="muted">No closed trades yet.</p>`;
  return `<div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>exit</th><th>stock</th><th>qty</th><th>in → out</th><th>net</th><th>why out</th></tr></thead>
    <tbody>${trades.map((t) => `
      <tr>
        <td>${fmtISTDateTime(t.exit_at)}</td>
        <td>${escapeHtml(t.symbol.replace(/\.(NS|BO)$/, ""))}${t.shadow ? ' <span class="arena-sub">shadow</span>' : ""}</td>
        <td>${t.qty}</td>
        <td>₹${t.entry_price} → ₹${t.exit_price}</td>
        <td class="${pnlClass(t.net)}">${inr(t.net)}<div class="arena-sub">₹${t.charges} costs</div></td>
        <td>${escapeHtml(String(t.exit_reason).replace(/_/g, " "))}</td>
      </tr>`).join("")}
    </tbody></table></div>`;
}

function renderArenaLive(data) {
  const live = document.getElementById("arenaLive");
  if (live) live.innerHTML = arenaLiveHtml(data);
  bindBoard();
}

function arenaLiveHtml(data) {
  const v = data.verdict;
  const m = data.market;
  const day = data.day ? `${data.day}${data.day_settled ? " · settled" : " · in progress"}` : "not started yet";
  const positions = data.open_positions.length ? `
    <div class="section-heading"><span class="eyebrow">right now</span><h2 style="font-size:17px;">Open positions</h2></div>
    <div class="signal-list">${data.open_positions.map((p) => `
      <div class="signal-row"><span>${escapeHtml(p.strategy)} · ${escapeHtml(p.symbol)} ×${p.qty} @ ₹${p.entry_price}</span>
      <span class="val ${pnlClass(p.unrealised)}">${inr(p.unrealised)}</span></div>`).join("")}
    </div>` : "";
  return `
    <div class="arena-verdict ${ARENA_LEVEL_CLASS[v.level] || ""}">${escapeHtml(v.text)}</div>
    <div class="stock-sub">market: ${m.is_open ? "open" : escapeHtml(m.reason.replace(/_/g, " "))} · session: ${escapeHtml(day)}
      · ${inr(data.config.daily_capital, false)} per strategy · ${data.config.symbols.length} stocks</div>
    ${renderBoard(data.leaderboard)}
    <p class="arena-hint">Tap a strategy to see its trades. Benched strategies keep trading in shadow and come back automatically if they start earning.</p>
    ${positions}`;
}

function renderArena(data) {
  const c = data.config;
  const strategyChecks = data.strategies.map((s) => `
    <label class="arena-check">
      <input type="checkbox" name="strategy" value="${escapeHtml(s.id)}" ${c.strategies.includes(s.id) ? "checked" : ""} ${s.benchmark ? "checked disabled" : ""}>
      <span><b>${escapeHtml(s.name)}</b><br><span class="arena-sub">${escapeHtml(s.description)}</span></span>
    </label>`).join("");
  const r = c.risk;
  document.getElementById("arenaContent").innerHTML = `
    <h2 class="stock-title">Strategy Arena</h2>
    <div class="disclaimer">Simulated money only. Every strategy gets its own ${inr(c.daily_capital, false)} and trades the same stocks on the same 5-minute bars,
      paying brokerage, STT, exchange fees, stamp duty, GST and slippage. No strategy can guarantee a profit - the arena's job is to show which ones
      actually earn, and to bench the ones that don't.</div>

    <div id="arenaLive">${arenaLiveHtml(data)}</div>

    <div class="section-heading"><span class="eyebrow">replay the past</span><h2 style="font-size:17px;">Backtest these strategies</h2></div>
    <div class="arena-row-inline">
      <select id="arenaBtDays">${[10, 20, 30, 60].map((d) => `<option value="${d}" ${d === 30 ? "selected" : ""}>last ${d} sessions</option>`).join("")}</select>
      <button class="add-watchlist-btn arena-btn" id="arenaRunBt">Run backtest</button>
    </div>
    <div id="arenaBacktest"><p class="muted">Replays real past 5-minute bars (Yahoo keeps ~60 days) through the same engine, so you get results today instead of after weeks of live paper trading.</p></div>

    <details id="arenaSettings" class="arena-settings">
      <summary>Settings</summary>
      <label class="arena-field">Capital per strategy (₹)
        <input type="number" id="arenaCapital" min="500" step="500" value="${c.daily_capital}"></label>
      <label class="arena-field">Each morning
        <select id="arenaMode">
          <option value="reset" ${c.capital_mode === "reset" ? "selected" : ""}>start fresh with this amount</option>
          <option value="compound" ${c.capital_mode === "compound" ? "selected" : ""}>carry over yesterday's balance</option>
        </select></label>
      <label class="arena-field">Stocks (comma separated, NSE symbols)
        <textarea id="arenaSymbols" rows="2">${escapeHtml(c.symbols.map((s) => s.replace(/\.NS$/, "")).join(", "))}</textarea></label>
      <button class="arena-link" id="arenaPreset">use 10 liquid Nifty stocks</button>
      <div class="arena-field">Strategies${strategyChecks}</div>
      <div class="arena-grid">
        <label class="arena-field">Daily loss limit %<input type="number" step="0.5" id="arenaDayLoss" value="${r.daily_loss_limit_pct}"></label>
        <label class="arena-field">Bench at drawdown %<input type="number" step="1" id="arenaMaxDD" value="${r.max_drawdown_pct}"></label>
        <label class="arena-field">Risk per trade %<input type="number" step="0.25" id="arenaRisk" value="${r.risk_per_trade_pct}"></label>
        <label class="arena-field">Max open positions<input type="number" step="1" id="arenaMaxPos" value="${r.max_open_positions}"></label>
        <label class="arena-field">Trades before judging<input type="number" step="1" id="arenaJudge" value="${r.min_trades_to_judge}"></label>
      </div>
      <p class="arena-hint">Capital changes apply from the next session. Removing a stock or strategy mid-session closes its positions at the last price.</p>
      <button class="add-watchlist-btn arena-btn" id="arenaSave">Save settings</button>
      <button class="watch-remove arena-reset" id="arenaReset">reset all results</button>
    </details>`;

  bindBoard();
  document.getElementById("arenaRunBt").addEventListener("click", runArenaBacktest);
  document.getElementById("arenaSave").addEventListener("click", saveArenaSettings);
  document.getElementById("arenaPreset").addEventListener("click", () => {
    document.getElementById("arenaSymbols").value = data.presets.liquid_nifty.map((s) => s.replace(/\.NS$/, "")).join(", ");
  });
  const reset = document.getElementById("arenaReset");
  reset.addEventListener("click", async () => {
    if (reset.dataset.confirming !== "1") {
      reset.dataset.confirming = "1"; reset.textContent = "tap again to wipe all arena results";
      setTimeout(() => { reset.dataset.confirming = "0"; reset.textContent = "reset all results"; }, 3000);
      return;
    }
    await apiFetch(`${API}/api/arena/reset`, { method: "POST" });
    showToast("Arena results reset");
    loadArena();
  });
}

function bindBoard() {
  document.querySelectorAll("#arenaLive .arena-row").forEach((row) => {
    row.addEventListener("click", () => {
      arenaOpenStrategy = arenaOpenStrategy === row.dataset.id ? null : row.dataset.id;
      renderArenaLive(arenaData);
      if (arenaOpenStrategy) loadArenaTrades(arenaOpenStrategy);
    });
  });
  if (arenaOpenStrategy) loadArenaTrades(arenaOpenStrategy);
}

async function loadArenaTrades(id) {
  const box = document.getElementById(`arenaTrades-${id}`);
  if (!box) return;
  try {
    const res = await apiFetch(`${API}/api/arena/trades?strategy=${encodeURIComponent(id)}&limit=40`);
    const data = await res.json();
    const reason = (arenaData.leaderboard.find((r) => r.id === id) || {}).status_reason;
    box.innerHTML = (reason ? `<p class="arena-hint">${escapeHtml(reason)}</p>` : "") + renderTrades(data.trades || []);
  } catch (e) {
    box.innerHTML = `<p class="muted">Could not load trades.</p>`;
  }
}

function renderBacktestResult(bt) {
  const box = document.getElementById("arenaBacktest");
  if (!box || !bt) return;
  const sessions = bt.replay ? bt.replay.sessions : [];
  const span = sessions.length ? `${sessions[0]} → ${sessions[sessions.length - 1]}` : "";
  box.innerHTML = `
    <div class="arena-verdict ${ARENA_LEVEL_CLASS[bt.verdict.level] || ""}">${escapeHtml(bt.verdict.text)}</div>
    <div class="stock-sub">${sessions.length} sessions ${escapeHtml(span)} · ${bt.replay ? bt.replay.symbols.length : 0} stocks
      · ran ${bt.replay ? fmtISTDateTime(bt.replay.ran_at) : ""}</div>
    ${renderBoard(bt.leaderboard, { live: false })}
    <p class="arena-hint">Past results on ${sessions.length} sessions are a small sample - a strategy that wins here still has to prove itself live.</p>`;
}

async function loadLastBacktest() {
  try {
    const res = await apiFetch(`${API}/api/arena/backtest`);
    const data = await res.json();
    if (data.backtest) renderBacktestResult(data.backtest);
  } catch (e) { /* optional */ }
}

async function runArenaBacktest() {
  const btn = document.getElementById("arenaRunBt");
  const days = parseInt(document.getElementById("arenaBtDays").value, 10);
  btn.disabled = true; btn.textContent = "Replaying…";
  try {
    const res = await apiFetch(`${API}/api/arena/backtest`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ days }),
    });
    const data = await res.json();
    if (!res.ok) { showToast(data.detail || "Backtest failed", "error"); return; }
    renderBacktestResult(data);
  } catch (e) {
    showToast("Network error running backtest", "error");
  } finally {
    btn.disabled = false; btn.textContent = "Run backtest";
  }
}

async function saveArenaSettings() {
  const num = (id) => parseFloat(document.getElementById(id).value);
  const body = {
    daily_capital: num("arenaCapital"),
    capital_mode: document.getElementById("arenaMode").value,
    symbols: document.getElementById("arenaSymbols").value.split(/[,\s]+/).filter(Boolean),
    strategies: [...document.querySelectorAll('#arenaSettings input[name="strategy"]:checked')].map((el) => el.value),
    risk: {
      daily_loss_limit_pct: num("arenaDayLoss"), max_drawdown_pct: num("arenaMaxDD"),
      risk_per_trade_pct: num("arenaRisk"), max_open_positions: num("arenaMaxPos"),
      min_trades_to_judge: num("arenaJudge"),
    },
  };
  try {
    const res = await apiFetch(`${API}/api/arena/config`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!res.ok) { showToast("Could not save settings", "error"); return; }
    showToast("Arena settings saved", "success");
    loadArena();
  } catch (e) {
    showToast("Network error saving settings", "error");
  }
}
