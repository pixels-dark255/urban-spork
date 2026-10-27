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
let arenaMarkets = null;

// Which market's arena is on screen. One arena per market, so every call
// carries it; the server keys storage by it and defaults to India.
let arenaMarket = (() => {
  try {
    return localStorage.getItem("arenaMarket") || "nse";
  } catch (e) {
    return "nse";       // private window, or site data blocked
  }
})();

function setArenaMarket(id) {
  arenaMarket = id;
  try {
    localStorage.setItem("arenaMarket", id);
  } catch (e) { /* remembering the tab is a convenience, not a requirement */ }
}

// Every arena endpoint is per market.
function mq(path, extra = "") {
  const sep = path.includes("?") ? "&" : "?";
  return `${API}${path}${sep}market=${encodeURIComponent(arenaMarket)}${extra}`;
}

// Money in the book's own currency. Rupees print whole; dollars and the
// like keep their cents, because a forex session's whole profit can be a
// few of them.
function money(amount, cur, withSign = true) {
  if (amount == null || Number.isNaN(amount)) return "—";
  const code = (cur && cur.code) || "INR";
  const symbol = (cur && cur.symbol) || "₹";
  const digits = code === "INR" ? 0 : 2;
  const sign = withSign && amount > 0 ? "+" : amount < 0 ? "−" : "";
  return `${sign}${symbol}${Math.abs(amount).toLocaleString("en-IN", {
    minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

// The native amount with its rupee equivalent underneath. On the India
// arena the two are the same number, so only one is shown.
function moneyWithInr(amount, inrAmount, cur) {
  const native = money(amount, cur);
  if (!cur || cur.code === "INR") return native;
  if (inrAmount == null) {
    return `${native}<div class="arena-sub">₹ rate unavailable</div>`;
  }
  return `${native}<div class="arena-sub">${inr(inrAmount)}</div>`;
}
let arenaOpenStrategy = null;

function inr(n, withSign = true) {
  if (n == null || Number.isNaN(n)) return "—";
  n = Math.round(n);            // so -0.4 shows as ₹0, not "−₹0"
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
    // The market strip needs every market's open/shut state, not just this
    // one's. Fetched alongside, and a failure there only costs the dots.
    const [res, marketsRes] = await Promise.all([
      apiFetch(mq("/api/arena")),
      apiFetch(`${API}/api/arena/markets`).catch(() => null),
    ]);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "failed");
    if (marketsRes && marketsRes.ok) {
      arenaMarkets = await marketsRes.json().catch(() => null);
    }
    arenaData = data;
    // Don't clobber a settings form the user is typing in.
    const editing = content.querySelector("#arenaSettings[open]");
    if (silent && editing) { renderArenaLive(data); return; }
    renderArena(data);
    loadLastBacktest();
    loadScoreboard();
  } catch (e) {
    if (!silent) content.innerHTML = `<p class="muted">Could not load the arena.</p>`;
  }
}

// "2 tr" used to mean entries here and closed trades in the TRADES column.
// Spelling both out, plus any unrealised P&L, stops the two reading as the
// same number disagreeing with itself.
function todaySub(today, cur = null) {
  const bits = [`${today.entries ?? today.trades ?? 0} entries`];
  if (today.open_positions) bits.push(`${today.open_positions} open`);
  if (today.stopped) bits.push("stopped");
  const text = escapeHtml(bits.join(" · "));
  // money() already produces markup-safe output and may carry a currency
  // symbol, so it is appended rather than escaped with the rest.
  return today.unrealised
    ? `${text} · ${money(today.unrealised, cur)} open P&amp;L`
    : text;
}

function renderBoard(rows, { live = true, currency = null } = {}) {
  const cur = currency || (rows[0] && { code: rows[0].currency, symbol: rows[0].currency_symbol });
  if (!rows.length) return `<p class="muted">No strategies yet.</p>`;
  return `
    <div class="table-scroll"><table class="backtest-table arena-board">
      <thead><tr>
        <th>#</th><th>strategy</th><th title="closed trades only, after costs">net (closed)</th><th>vs bench</th>
        <th>closed</th><th>win</th><th>PF</th><th>max DD</th>${live ? "<th>today</th>" : ""}
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
          <td class="${pnlClass(r.net_total)}">${moneyWithInr(r.net_total, r.net_total_inr, cur)}
            <div class="arena-sub">${money(r.charges_total, cur, false)} costs</div></td>
          <td class="${pnlClass(r.vs_benchmark)}">${r.vs_benchmark == null ? "—" : money(r.vs_benchmark, cur)}</td>
          <td>${r.trades}</td>
          <td>${r.win_rate == null ? "—" : r.win_rate + "%"}</td>
          <td title="${r.profit_factor == null && r.no_losses ? "no losing trades yet" : ""}">${
            r.profit_factor == null ? "—" : r.profit_factor}</td>
          <td class="neg">${r.max_drawdown ? money(-r.max_drawdown, cur) : "—"}</td>
          ${live ? `<td class="${pnlClass(r.today.net)}">${moneyWithInr(r.today.net, r.today.net_inr, cur)}
             <div class="arena-sub">${todaySub(r.today, cur)}</div></td>` : ""}
        </tr>
        ${live && arenaOpenStrategy === r.id ? `<tr class="arena-detail-row"><td colspan="9"><div id="arenaTrades-${escapeHtml(r.id)}"><p class="loading">loading trades…</p></div></td></tr>` : ""}
      `).join("")}
      </tbody>
    </table></div>`;
}

function renderTrades(trades, cur = null) {
  if (!trades.length) return `<p class="muted">No closed trades yet.</p>`;
  return `<div class="table-scroll"><table class="backtest-table">
    <thead><tr><th>exit</th><th>stock</th><th>qty</th><th>in → out</th><th>net</th><th>why out</th></tr></thead>
    <tbody>${trades.map((t) => `
      <tr>
        <td>${fmtISTDateTime(t.exit_at)}</td>
        <td>${t.side === "SHORT" ? '<span class="arena-tag arena-short">short</span> ' : ""}${escapeHtml(t.symbol.replace(/\.(NS|BO)$/, ""))}${t.shadow ? ' <span class="arena-sub">shadow</span>' : ""}</td>
        <td>${t.qty}</td>
        <td>${escapeHtml(`${(cur && cur.symbol) || "₹"}${t.entry_price} → ${(cur && cur.symbol) || "₹"}${t.exit_price}`)}</td>
        <td class="${pnlClass(t.net)}">${moneyWithInr(t.net, t.net_inr, cur)}
          <div class="arena-sub">${money(t.charges, cur, false)} costs</div></td>
        <td>${escapeHtml(String(t.exit_reason).replace(/_/g, " "))}${
          t.caught_up ? ' <span class="arena-tag">caught up</span>' : ""}</td>
      </tr>`).join("")}
    </tbody></table></div>`;
}

// A trading date, formatted without going near a timezone: these are already
// IST session dates, and parsing them as instants is how they drift a day.
function fmtDayLabel(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ""));
  if (!m) return String(iso || "");
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${m[3]} ${MON[Number(m[2]) - 1] || m[2]}`;
}

// The freshness line says how many days were caught up; this says which ones.
// Without it the per-day caught_up flag ships in the payload and is never
// seen, and a replayed week is indistinguishable from a watched one.
function renderDayHistory(history, strategies, cur = null) {
  const days = (history || []).slice(-10).reverse();
  if (!days.length) return "";
  const names = {}, isBench = {};
  (strategies || []).forEach((s) => { names[s.id] = s.name; isBench[s.id] = !!s.benchmark; });
  const rows = days.map((h) => {
    const books = Object.entries(h.books || {});
    const real = books.filter(([id]) => !isBench[id]);
    const benchEntry = books.find(([id]) => isBench[id]);
    const best = real.length ? real.reduce((a, b) => (b[1].net > a[1].net ? b : a)) : null;
    const trades = books.reduce((n, [, b]) => n + (b.trades || 0), 0);
    return `<tr>
      <td>${escapeHtml(fmtDayLabel(h.date))}${
        h.caught_up ? ' <span class="arena-tag">caught up</span>' : ""}</td>
      <td>${trades}</td>
      <td>${best ? `${escapeHtml(names[best[0]] || best[0])}
        <span class="${pnlClass(best[1].net)}">${money(best[1].net, cur)}</span>` : "—"}</td>
      <td class="${benchEntry ? pnlClass(benchEntry[1].net) : ""}">${
        benchEntry ? money(benchEntry[1].net, cur) : "—"}</td>
    </tr>`;
  }).join("");
  return `
    <div class="section-heading"><span class="eyebrow">settled sessions</span>
      <h2 style="font-size:17px;">Day by day</h2></div>
    <div class="table-scroll"><table class="backtest-table">
      <thead><tr><th>day</th><th>trades</th><th>best strategy</th><th>benchmark</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <p class="arena-hint">A day tagged <span class="arena-tag">caught up</span> was replayed from
      Yahoo's 5-minute bars after the server woke, not watched live. Same engine, same bars, same
      fills - but it is marked rather than implied.</p>`;
}

function renderArenaLive(data) {
  const live = document.getElementById("arenaLive");
  if (live) live.innerHTML = arenaLiveHtml(data);
  bindBoard();
  bindMarketTabs();
}

// On Render's free tier a JSON file is wiped every time the server sleeps.
// Someone who comes back to an empty arena deserves to know why before they
// conclude the thing is broken.
function storageWarning(storage) {
  if (!storage || !storage.ephemeral) return "";
  const wiped = `Results are stored in a file that Render wipes whenever the server sleeps
    or redeploys.`;
  // Telling someone who has already set DATABASE_URL to set DATABASE_URL is
  // how a broken driver stays broken for a week. If it is configured, the
  // problem is the connection, and the reason is worth printing.
  if (storage.configured) {
    return `<div class="arena-warn arena-warn-hard">${wiped}
      <code>DATABASE_URL</code> <b>is set, but Postgres could not be used</b>${
        storage.error ? `: <code>${escapeHtml(storage.error)}</code>` : "."}
      Until that is fixed the arena is still running on files.</div>`;
  }
  return `<div class="arena-warn">${wiped} Set <code>DATABASE_URL</code> to a free Postgres
    (Neon or Supabase) to keep them.</div>`;
}

// Tells three look-alike situations apart: the server was asleep, the data
// source failed, or the market is simply shut.
function freshnessLine(f) {
  if (!f) return "";
  const bits = [];
  if (f.last_tick_at) bits.push(`last updated ${escapeHtml(fmtIST(f.last_tick_at, {
    hour: "2-digit", minute: "2-digit", hour12: false }))} IST`);
  else bits.push("no update yet");

  if (f.caught_up_last > 0) {
    bits.push(`${f.caught_up_last} ${f.caught_up_last === 1 ? "day" : "days"} caught up on waking`);
  }
  if (f.latest_bar_at) bits.push(`latest bar ${escapeHtml(fmtIST(f.latest_bar_at, {
    hour: "2-digit", minute: "2-digit", hour12: false }))}`);
  if ((f.symbols_failed || []).length) {
    bits.push(`<span class="neg">data for ${escapeHtml(f.symbols_failed
      .map((s) => s.replace(/\.(NS|BO)$/, "")).join(", "))} failed</span>`);
  }

  const stale = f.stale
    ? `<div class="arena-stale">The server was asleep. Missed bars are filled in automatically
       when it wakes.</div>`
    : "";
  return `<div class="stock-sub arena-freshness">${bits.join(" · ")}</div>${stale}`;
}

// The simulation runs in a scheduled job, not in the web service, so "the
// server was asleep" is no longer the story - the dashboard being asleep is
// expected and harmless. What matters is whether the RUNNER still runs.
function runnerLine(runner) {
  if (!runner || runner.mode !== "github") return "";
  const bits = [];
  if (runner.last_tick_at) {
    bits.push(`Updated by the daily runner at ${escapeHtml(fmtIST(runner.last_tick_at, {
      hour: "2-digit", minute: "2-digit", hour12: false }))} IST`);
  } else {
    bits.push("The runner has not updated this arena yet");
  }
  bits.push(`next run ~${escapeHtml(runner.next_run_ist)} IST`);
  const line = `<div class="stock-sub arena-freshness">${bits.join(" · ")}</div>`;
  if (!runner.stale) return line;
  const when = runner.last_tick_at
    ? `hasn't updated since ${escapeHtml(fmtIST(runner.last_tick_at, {
        day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
        hour12: false }))}`
    : "has never run for this arena";
  return `${line}<div class="arena-stale">The runner ${when} — check
    GitHub → Actions. Results are caught up automatically once it runs
    again, so nothing is lost in the meantime.</div>`;
}

const MARKET_LABELS = { nse: "India", us: "US", forex: "Forex", crypto: "Crypto" };

// The tab strip. Each market has its own arena, its own hours and its own
// currency, so switching is a reload rather than a filter.
function renderMarketTabs(data) {
  const rows = (arenaMarkets && arenaMarkets.markets) || [];
  const ids = rows.length ? rows.map((m) => m.id) : Object.keys(MARKET_LABELS);
  return `<div class="arena-markets" role="tablist">${ids.map((id) => {
    const row = rows.find((m) => m.id === id);
    const open = row && row.is_open;
    const label = (row && MARKET_LABELS[id]) || MARKET_LABELS[id] || id;
    return `<button class="arena-market-tab ${id === arenaMarket ? "on" : ""}"
       role="tab" aria-selected="${id === arenaMarket}" data-market="${escapeHtml(id)}">
      <span class="arena-market-dot ${open ? "open" : "shut"}"
            title="${open ? "market open" : "market closed"}"></span>${escapeHtml(label)}</button>`;
  }).join("")}</div>`;
}

// "US session 19:00-01:30 IST (09:30-16:00 New York)" - both clocks,
// because the IST half shifts by an hour when US daylight time ends and
// only the local half stays put.
function sessionLine(data) {
  const s = data.session;
  if (!s) return "";
  // The market's own clock, except on the India arena where it is the
  // user's clock too and printing both says the same thing twice.
  const tzShort = s.same_clock_as_ist ? "" : (s.market_timezone_label || "");
  const name = (data.market_profile && data.market_profile.name) || "";
  const bits = [];
  if (s.today) {
    bits.push(`${escapeHtml(name)} session ${escapeHtml(s.today.start_ist)}–${escapeHtml(s.today.end_ist)} IST`
      + (tzShort ? ` <span class="arena-sub">(${escapeHtml(s.today.start_local)}–${escapeHtml(s.today.end_local)} ${escapeHtml(tzShort)})</span>` : ""));
  } else {
    bits.push(`${escapeHtml(name)}: no session in your window today`);
  }
  if (!s.active_now && s.next && s.next_date) {
    bits.push(`next ${escapeHtml(fmtDayLabel(s.next_date))} at ${escapeHtml(s.next.start_ist)} IST`);
  } else if (s.active_now) {
    bits.push("<b>trading now</b>");
  }
  return `<div class="stock-sub arena-session">${bits.join(" · ")}</div>`;
}

function leverageWarning(data) {
  const lev = Number(data.config.leverage || 1);
  if (!(lev > 1)) return "";
  return `<div class="arena-warn arena-warn-hard">Leverage ${lev}× is on. A ${lev}× position
    loses ${lev}× as fast, and the daily loss limit is hit ${lev} times sooner. Simulated money
    only - but the losses it simulates are real ones.</div>`;
}

function regulatoryNote(data) {
  if (!data.regulatory_note) return "";
  return `<p class="arena-legal">${escapeHtml(data.regulatory_note)}</p>`;
}

function arenaLiveHtml(data) {
  const v = data.verdict;
  const m = data.market;
  const day = data.day ? `${data.day}${data.day_settled ? " · settled" : " · in progress"}` : "not started yet";
  const positions = data.open_positions.length ? `
    <div class="section-heading"><span class="eyebrow">right now</span><h2 style="font-size:17px;">Open positions</h2></div>
    <div class="signal-list">${data.open_positions.map((p) => `
      <div class="signal-row"><span>${escapeHtml(p.strategy)} · ${escapeHtml(p.symbol)} ×${p.qty} @ ${
        escapeHtml(((data.currency && data.currency.symbol) || "₹") + p.entry_price)}</span>
      <span class="val ${pnlClass(p.unrealised)}">${money(p.unrealised, data.currency)}</span></div>`).join("")}
    </div>` : "";
  const profile = data.market_profile || {};
  const status = data.config.market === "nse"
    ? (m.is_open ? "open" : escapeHtml(m.reason.replace(/_/g, " ")))
    : (profile.is_open ? "open" : "closed");
  return `
    ${renderMarketTabs(data)}
    ${storageWarning(data.storage)}
    ${leverageWarning(data)}
    <div class="arena-verdict ${ARENA_LEVEL_CLASS[v.level] || ""}">${escapeHtml(v.text)}</div>
    <div class="stock-sub">market: ${status} · session: ${escapeHtml(day)}
      · ${inr(data.config.daily_capital, false)} per strategy · ${data.config.symbols.length} ${
        data.config.market === "forex" ? "pairs" : data.config.market === "crypto" ? "coins" : "stocks"}</div>
    ${sessionLine(data)}
    ${data.runner && data.runner.mode === "github"
      ? runnerLine(data.runner) : freshnessLine(data.freshness)}
    ${renderBoard(data.leaderboard, { currency: data.currency })}
    <p class="arena-hint">Tap a strategy to see its trades. Benched strategies keep trading in shadow and come back automatically if they start earning.</p>
    ${regulatoryNote(data)}
    ${positions}
    ${renderDayHistory(data.history, data.strategies, data.currency)}`;
}

function renderArena(data) {
  const c = data.config;
  const strategyChecks = data.strategies.map((s) => `
    <label class="arena-check">
      <input type="checkbox" name="strategy" value="${escapeHtml(s.id)}" ${c.strategies.includes(s.id) ? "checked" : ""} ${s.benchmark ? "checked disabled" : ""}>
      <span><b>${escapeHtml(s.name)}</b>${s.both_sides ? ' <span class="arena-tag">long + short</span>' : ""}<br><span class="arena-sub">${escapeHtml(s.description)}</span></span>
    </label>`).join("");
  const r = c.risk;
  const unit = c.market === "forex" ? "pairs" : c.market === "crypto" ? "coins" : "stocks";
  document.getElementById("arenaContent").innerHTML = `
    <h2 class="stock-title">Strategy Arena</h2>
    <div class="disclaimer">Simulated money only. Every strategy gets its own ${inr(c.daily_capital, false)} and trades the same ${unit} on the same 5-minute bars,
      paying the market's real costs. No strategy can guarantee a profit - the arena's job is to show which ones
      actually earn, and to bench the ones that don't.</div>

    <div id="arenaLive">${arenaLiveHtml(data)}</div>

    <div class="section-heading"><span class="eyebrow">the record so far</span>
      <h2 style="font-size:17px;">Scoreboard</h2></div>
    <p class="arena-hint">Which strategy is actually earning, over weeks rather than today.
      One good session is not a record.</p>
    <div id="arenaScore"><div class="skeleton-block" style="height:180px"></div></div>

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
      ${windowFields(data)}
      ${leverageField(data)}
      <label class="arena-field">Symbols (comma separated)
        <span class="arena-sub">${escapeHtml(data.symbol_hint || "")}</span>
        <textarea id="arenaSymbols" rows="2">${escapeHtml(c.symbols.map(shortSymbol).join(", "))}</textarea></label>
      ${presetButtons(data)}
      <div class="arena-field">Strategies${strategyChecks}</div>
      <div class="arena-grid">
        <label class="arena-field">Daily loss limit %<input type="number" step="0.5" id="arenaDayLoss" value="${r.daily_loss_limit_pct}"></label>
        <label class="arena-field">Bench at drawdown %<input type="number" step="1" id="arenaMaxDD" value="${r.max_drawdown_pct}"></label>
        <label class="arena-field">Risk per trade %<input type="number" step="0.25" id="arenaRisk" value="${r.risk_per_trade_pct}"></label>
        <label class="arena-field">Max open positions<input type="number" step="1" id="arenaMaxPos" value="${r.max_open_positions}"></label>
        <label class="arena-field">Trades before judging<input type="number" step="1" id="arenaJudge" value="${r.min_trades_to_judge}"></label>
      </div>
      ${identityFields()}
      <p class="arena-hint">Capital changes apply from the next session. Removing a stock or strategy mid-session closes its positions at the last price.</p>
      <button class="add-watchlist-btn arena-btn" id="arenaSave">Save settings</button>
      <button class="watch-remove arena-reset" id="arenaReset">reset all results</button>
    </details>`;

  bindBoard();
  document.getElementById("arenaRunBt").addEventListener("click", runArenaBacktest);
  document.getElementById("arenaSave").addEventListener("click", saveArenaSettings);
  document.querySelectorAll("#arenaContent .arena-preset").forEach((btn) => {
    btn.addEventListener("click", () => {
      const list = (data.presets || {})[btn.dataset.preset] || [];
      document.getElementById("arenaSymbols").value = list.map(shortSymbol).join(", ");
    });
  });
  bindMarketTabs();
  bindIdentity();
  const reset = document.getElementById("arenaReset");
  reset.addEventListener("click", async () => {
    if (reset.dataset.confirming !== "1") {
      reset.dataset.confirming = "1"; reset.textContent = "tap again to wipe all arena results";
      setTimeout(() => { reset.dataset.confirming = "0"; reset.textContent = "reset all results"; }, 3000);
      return;
    }
    await apiFetch(mq("/api/arena/reset"), { method: "POST" });
    showToast("Arena results reset");
    loadArena();
  });
}

// A symbol as the user types it: the suffix is how Yahoo names the feed,
// not something anyone wants to retype. The server puts it back.
function shortSymbol(sym) {
  return String(sym).replace(/\.(NS|BO)$/, "").replace(/=X$/, "");
}

const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// The window is set in IST whatever the market, because that is the clock
// the person is actually living on. A market whose own session is the
// default shows that session's local hours beside it for context.
function windowFields(data) {
  const w = data.config.window || {};
  const inIst = w.tz === "Asia/Kolkata";
  const sess = data.session && data.session.today;
  const hint = inIst
    ? "The hours you want to trade, in IST."
    : `Currently following the market's own session (${escapeHtml(w.start)}–${escapeHtml(w.end)} ${
        escapeHtml((data.session && data.session.market_timezone_label) || "local")}`
      + (sess ? `, i.e. ${escapeHtml(sess.start_ist)}–${escapeHtml(sess.end_ist)} IST today` : "")
      + "). Set a start and end below to pick your own hours in IST.";
  const start = inIst ? w.start : (sess ? sess.start_ist : "19:00");
  const end = inIst ? w.end : (sess ? sess.end_ist : "23:30");
  const days = w.days || [0, 1, 2, 3, 4];
  return `
    <div class="arena-field">Your trading window
      <span class="arena-sub">${hint}</span>
      <div class="arena-row-inline">
        <input type="time" id="arenaWinStart" value="${escapeHtml(start)}">
        <span class="arena-sub">to</span>
        <input type="time" id="arenaWinEnd" value="${escapeHtml(end)}">
        <span class="arena-sub">IST</span>
      </div>
      <div class="arena-days">${DAY_NAMES.map((name, i) => `
        <label class="arena-day"><input type="checkbox" name="arenaDay" value="${i}"
          ${days.includes(i) ? "checked" : ""}><span>${name}</span></label>`).join("")}</div>
      <span class="arena-sub">A window that ends before it starts runs past midnight and
        counts as one session, filed under the day it began.</span>
    </div>`;
}

function leverageField(data) {
  const max = Number(data.max_leverage || 1);
  if (!(max > 1)) return "";
  return `
    <label class="arena-field">Leverage
      <span class="arena-sub">1× means no borrowing. Above 1× the book holds a bigger
        position for the same money - and loses just as much faster.</span>
      <input type="number" id="arenaLeverage" min="1" max="${max}" step="0.5"
             value="${Number(data.config.leverage || 1)}"></label>`;
}

function presetButtons(data) {
  const presets = data.presets || {};
  return Object.keys(presets).map((key) => `
    <button class="arena-link arena-preset" data-preset="${escapeHtml(key)}">use ${
      presets[key].length} ${escapeHtml(key.replace(/_/g, " "))}</button>`).join(" ");
}

function bindMarketTabs() {
  document.querySelectorAll("#arenaContent .arena-market-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      if (tab.dataset.market === arenaMarket) return;
      setArenaMarket(tab.dataset.market);
      arenaOpenStrategy = null;
      loadArena();
    });
  });
}

// ---------------------------------------------------------------------------
// Scoreboard: the record over weeks, not the state right now
// ---------------------------------------------------------------------------

let scorePeriod = "30";
let scoreData = null;

async function loadScoreboard(period) {
  const box = document.getElementById("arenaScore");
  if (!box) return;
  if (period) scorePeriod = period;
  box.innerHTML = `<div class="skeleton-block" style="height:180px"></div>`;
  try {
    const res = await apiFetch(mq("/api/arena/scoreboard", `&period=${encodeURIComponent(scorePeriod)}`));
    const data = await res.json();
    if (!res.ok) throw new Error("failed");
    scoreData = data;
    box.innerHTML = scoreboardHtml(data);
    bindScoreboard();
  } catch (e) {
    box.innerHTML = `<p class="muted">Could not load the scoreboard.</p>`;
  }
}

// Arenas are keyed by a client id this browser generated once, so a phone
// and a PC are two different users with two different sets of arenas. This
// lets one be pointed at the other's id.
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function isValidClientId(id) {
  return UUID_RE.test(String(id || "").trim());
}

function identityFields() {
  const current = (typeof CLIENT_ID === "string" ? CLIENT_ID : "");
  return `
    <div class="arena-field">Your arena ID
      <span class="arena-sub">Arenas belong to this ID, not to this device. Paste it into
        another browser to see the same arenas there.</span>
      <div class="arena-row-inline">
        <input type="text" id="arenaClientId" readonly value="${escapeHtml(current)}">
        <button class="arena-link" id="arenaCopyId">copy</button>
      </div>
    </div>
    <div class="arena-field">Use an existing ID
      <span class="arena-sub">Switches this browser to another ID. Nothing is deleted -
        the arenas under the current ID stay where they are, and you can switch back by
        pasting it again, so keep a copy first.</span>
      <div class="arena-row-inline">
        <input type="text" id="arenaNewId" placeholder="00000000-0000-0000-0000-000000000000">
        <button class="arena-link" id="arenaUseId">use this ID</button>
      </div>
    </div>`;
}

function bindIdentity() {
  const copy = document.getElementById("arenaCopyId");
  if (copy) {
    copy.addEventListener("click", async () => {
      const value = document.getElementById("arenaClientId").value;
      try {
        await navigator.clipboard.writeText(value);
        showToast("Arena ID copied", "success");
      } catch (e) {
        // Clipboard access is refused outside a secure context and in some
        // in-app browsers; selecting it is then the next best thing.
        const box = document.getElementById("arenaClientId");
        box.removeAttribute("readonly");
        box.select();
        showToast("Press Ctrl/Cmd+C to copy");
      }
    });
  }
  const use = document.getElementById("arenaUseId");
  if (use) {
    use.addEventListener("click", () => {
      const next = document.getElementById("arenaNewId").value.trim();
      if (!isValidClientId(next)) {
        showToast("That is not a valid arena ID", "error");
        return;
      }
      if (next === CLIENT_ID) {
        showToast("Already using that ID");
        return;
      }
      if (!confirm(`Switch this browser to arena ID\n\n${next}\n\n`
                   + `The arenas under your current ID are not deleted, but you will need `
                   + `that ID to get back to them. Have you copied it?`)) {
        return;
      }
      try {
        localStorage.setItem("tickerboard_client_id", next.toLowerCase());
      } catch (e) {
        showToast("This browser will not let the app store the ID", "error");
        return;
      }
      location.reload();
    });
  }
}

function bindScoreboard() {
  document.querySelectorAll("#arenaScore .arena-period").forEach((btn) => {
    btn.addEventListener("click", () => loadScoreboard(btn.dataset.period));
  });
}

// A run of days as small bars. Not a chart library - just enough shape to
// tell "one huge day" apart from "grinds out small wins", which is the
// distinction the numbers beside it are making.
function sparkline(recent, cur) {
  if (!recent || !recent.length) return "";
  const peak = Math.max(...recent.map((d) => Math.abs(d.net)), 1);
  return `<span class="arena-spark" role="img"
    aria-label="${recent.length} most recent sessions">${recent.map((d) => {
      const h = Math.max(2, Math.round((Math.abs(d.net) / peak) * 14));
      const cls = d.net > 0 ? "up" : d.net < 0 ? "down" : "flat";
      return `<i class="${cls}" style="height:${h}px" title="${escapeHtml(
        `${fmtDayLabel(d.date)}: ${money(d.net, cur)}`)}"></i>`;
    }).join("")}</span>`;
}

function streakText(s) {
  if (!s || !s.current) return "—";
  const n = Math.abs(s.current);
  return s.current > 0 ? `${n} up` : `${n} down`;
}

function highlightCards(h, cur) {
  if (!h) return "";
  const card = (label, body, tone = "") =>
    `<div class="arena-card ${tone}"><span class="arena-card-label">${label}</span>${body}</div>`;
  const cards = [];
  if (h.most_profitable) {
    cards.push(card("made the most", `<b>${escapeHtml(h.most_profitable.name)}</b>
      <span class="${pnlClass(h.most_profitable.net)}">${money(h.most_profitable.net, cur)}</span>
      <span class="arena-sub">${h.most_profitable.green_days} green · ${h.most_profitable.red_days} red</span>`));
  }
  if (h.most_winning_days) {
    cards.push(card("won the most days", `<b>${escapeHtml(h.most_winning_days.name)}</b>
      <span>${h.most_winning_days.daily_wins} ${h.most_winning_days.daily_wins === 1 ? "day" : "days"}</span>
      <span class="arena-sub">best on the most sessions</span>`));
  }
  if (h.most_losing_days) {
    cards.push(card("lost on the most days", `<b>${escapeHtml(h.most_losing_days.name)}</b>
      <span class="neg">${h.most_losing_days.red_days} ${h.most_losing_days.red_days === 1 ? "day" : "days"}</span>
      <span class="arena-sub">${money(h.most_losing_days.net, cur)} over the period</span>`, "arena-card-bad"));
  }
  if (h.best_single_day) {
    cards.push(card("biggest single day", `<b>${escapeHtml(h.best_single_day.strategy)}</b>
      <span class="pos">${money(h.best_single_day.net, cur)}</span>
      <span class="arena-sub">${escapeHtml(fmtDayLabel(h.best_single_day.date))}</span>`));
  }
  if (h.worst_single_day) {
    cards.push(card("worst single day", `<b>${escapeHtml(h.worst_single_day.strategy)}</b>
      <span class="neg">${money(h.worst_single_day.net, cur)}</span>
      <span class="arena-sub">${escapeHtml(fmtDayLabel(h.worst_single_day.date))}</span>`, "arena-card-bad"));
  }
  if (!cards.length) return "";
  return `<div class="arena-cards">${cards.join("")}</div>`;
}

function scoreboardHtml(data) {
  const cur = data.currency;
  const p = data.period;
  const buttons = (data.periods || []).map((opt) => `
    <button class="arena-period ${opt.id === p.id ? "on" : ""}"
      data-period="${escapeHtml(opt.id)}">${escapeHtml(opt.label)}</button>`).join("");

  if (!p.sessions) {
    return `<div class="arena-periods">${buttons}</div>
      <p class="muted">No settled sessions yet. The scoreboard fills in once the arena has
      traded and settled its first day - one row per strategy per session.</p>`;
  }

  const rows = data.strategies.map((r) => `
    <tr class="${r.benchmark ? "arena-bench" : ""}">
      <td>
        <div class="arena-name">${escapeHtml(r.name)}</div>
        <span class="arena-sub">${r.trades} trades${
          r.status !== "active" && !r.benchmark ? " · benched" : ""}</span>
      </td>
      <td class="${pnlClass(r.net)}">${moneyWithInr(r.net, r.net_inr, cur)}</td>
      <td>${sparkline(r.recent, cur)}</td>
      <td><span class="pos">${r.green_days}</span> / <span class="neg">${r.red_days}</span>
        ${r.quiet_days ? `<div class="arena-sub">${r.quiet_days} quiet</div>` : ""}</td>
      <td>${r.day_win_rate == null ? "—" : r.day_win_rate + "%"}</td>
      <td>${r.benchmark ? "—" : r.daily_wins}</td>
      <td>${r.benchmark ? "—" : r.beat_benchmark_days}</td>
      <td class="${r.streaks.current > 0 ? "pos" : r.streaks.current < 0 ? "neg" : ""}">${
        escapeHtml(streakText(r.streaks))}</td>
      <td>${r.best_day ? `<span class="${pnlClass(r.best_day.net)}">${money(r.best_day.net, cur)}</span>
        <div class="arena-sub">${escapeHtml(fmtDayLabel(r.best_day.date))}</div>` : "—"}</td>
      <td>${r.worst_day ? `<span class="${pnlClass(r.worst_day.net)}">${money(r.worst_day.net, cur)}</span>
        <div class="arena-sub">${escapeHtml(fmtDayLabel(r.worst_day.date))}</div>` : "—"}</td>
    </tr>`).join("");

  const idle = (data.highlights && data.highlights.idle) || [];
  return `
    <div class="arena-periods">${buttons}
      <span class="arena-sub">${p.sessions} ${p.sessions === 1 ? "session" : "sessions"},
        ${escapeHtml(fmtDayLabel(p.from))} – ${escapeHtml(fmtDayLabel(p.to))}</span></div>
    ${highlightCards(data.highlights, cur)}
    <div class="table-scroll"><table class="backtest-table">
      <thead><tr>
        <th>strategy</th><th>net</th><th>last ${(data.strategies[0] || {}).recent?.length || 0}</th>
        <th title="days finished up / down">green / red</th>
        <th title="share of days with a result that finished up">day win%</th>
        <th title="sessions where this was the best strategy of the day">days won</th>
        <th title="sessions where this finished above the benchmark">beat bench</th>
        <th>streak</th><th>best day</th><th>worst day</th>
      </tr></thead>
      <tbody>${rows}</tbody></table></div>
    ${idle.length ? `<p class="arena-hint">Found no signal at all this period:
      ${idle.map((x) => escapeHtml(x.name)).join(", ")}. They are not winning - they are
      not playing.</p>` : ""}
    <p class="arena-hint">${escapeHtml(data.note || "")}</p>
    ${dailyWinnersHtml(data)}`;
}

function dailyWinnersHtml(data) {
  const days = (data.daily || []).slice(0, 15);
  if (!days.length) return "";
  const cur = data.currency;
  return `
    <details class="arena-settings">
      <summary>Day by day — who won each session</summary>
      <div class="table-scroll"><table class="backtest-table">
        <thead><tr><th>day</th><th>best that day</th><th>worst that day</th>
          <th>benchmark</th><th>traded</th></tr></thead>
        <tbody>${days.map((d) => `
          <tr>
            <td>${escapeHtml(fmtDayLabel(d.date))}${
              d.caught_up ? ' <span class="arena-tag">caught up</span>' : ""}</td>
            <td>${d.winner_name ? `${escapeHtml(d.winner_name)}
              <span class="${pnlClass(d.winner_net)}">${money(d.winner_net, cur)}</span>` : "—"}</td>
            <td>${d.loser_name ? `${escapeHtml(d.loser_name)}
              <span class="${pnlClass(d.loser_net)}">${money(d.loser_net, cur)}</span>` : "—"}</td>
            <td class="${d.benchmark_net == null ? "" : pnlClass(d.benchmark_net)}">${
              d.benchmark_net == null ? "—" : money(d.benchmark_net, cur)}</td>
            <td>${d.traded}</td>
          </tr>`).join("")}
        </tbody></table></div>
    </details>`;
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
    const res = await apiFetch(mq(`/api/arena/trades?strategy=${encodeURIComponent(id)}&limit=40`));
    const data = await res.json();
    const reason = (arenaData.leaderboard.find((r) => r.id === id) || {}).status_reason;
    box.innerHTML = (reason ? `<p class="arena-hint">${escapeHtml(reason)}</p>` : "")
      + renderTrades(data.trades || [], arenaData && arenaData.currency);
  } catch (e) {
    box.innerHTML = `<p class="muted">Could not load trades.</p>`;
  }
}

// Slippage is assumed, not measured, and at 0.05% a side it is a large part
// of a small intraday target. Seeing the same sessions at 0% / 0.02% / 0.05%
// separates "has an edge" from "has an edge only if fills are free".
function renderSlippageSweep(sweep, cur = null) {
  if (!sweep || !sweep.rows || !sweep.rows.length) return "";
  const levels = sweep.levels || [];
  return `
    <div class="section-heading"><span class="eyebrow">how much do fills matter</span>
      <h2 style="font-size:17px;">Net P&L by assumed fill cost</h2></div>
    <div class="table-scroll"><table class="backtest-table">
      <thead><tr><th>strategy</th>${levels.map((l) =>
        `<th title="${escapeHtml(l.describes || "")}">${escapeHtml(l.label)}
         <div class="arena-sub">${escapeHtml(l.describes || "")}</div></th>`).join("")}</tr></thead>
      <tbody>${sweep.rows.map((r) => `
        <tr class="${r.benchmark ? "arena-bench" : ""}">
          <td>${escapeHtml(r.name)}</td>
          ${levels.map((l) => {
            const cell = r.by_slippage[l.label];
            return `<td class="${cell ? pnlClass(cell.net) : ""}">${
              cell ? money(cell.net, cur) : "—"}</td>`;
          }).join("")}
        </tr>`).join("")}
      </tbody></table></div>
    <p class="arena-hint">${escapeHtml(sweep.note || "")}</p>`;
}

function renderBacktestResult(bt) {
  const box = document.getElementById("arenaBacktest");
  if (!box || !bt) return;
  const sessions = bt.replay ? bt.replay.sessions : [];
  const span = sessions.length ? `${sessions[0]} → ${sessions[sessions.length - 1]}` : "";
  box.innerHTML = `
    <div class="arena-verdict ${ARENA_LEVEL_CLASS[bt.verdict.level] || ""}">${escapeHtml(bt.verdict.text)}</div>
    <div class="stock-sub">${sessions.length} sessions ${escapeHtml(span)} · ${bt.replay ? bt.replay.symbols.length : 0} symbols
      · ran ${bt.replay ? fmtISTDateTime(bt.replay.ran_at) : ""}</div>
    ${renderBoard(bt.leaderboard, { live: false, currency: bt.currency })}
    ${renderSlippageSweep(bt.slippage_sweep, bt.currency)}
    <p class="arena-hint">Past results on ${sessions.length} sessions are a small sample - a strategy that wins here still has to prove itself live.</p>`;
}

async function loadLastBacktest() {
  try {
    const res = await apiFetch(mq("/api/arena/backtest"));
    const data = await res.json();
    if (data.backtest) renderBacktestResult(data.backtest);
  } catch (e) { /* optional */ }
}

async function runArenaBacktest() {
  const btn = document.getElementById("arenaRunBt");
  const days = parseInt(document.getElementById("arenaBtDays").value, 10);
  btn.disabled = true; btn.textContent = "Replaying…";
  try {
    const res = await apiFetch(mq("/api/arena/backtest"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ days, slippage_sweep: true }),
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
  const start = document.getElementById("arenaWinStart");
  const end = document.getElementById("arenaWinEnd");
  if (start && end && start.value && end.value) {
    body.window = {
      start: start.value, end: end.value, tz: "Asia/Kolkata",
      days: [...document.querySelectorAll('#arenaSettings input[name="arenaDay"]:checked')]
        .map((el) => Number(el.value)),
    };
  }
  const lev = document.getElementById("arenaLeverage");
  if (lev && lev.value) body.leverage = parseFloat(lev.value);
  try {
    const res = await apiFetch(mq("/api/arena/config"), {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!res.ok) { showToast("Could not save settings", "error"); return; }
    showToast("Arena settings saved", "success");
    loadArena();
  } catch (e) {
    showToast("Network error saving settings", "error");
  }
}
