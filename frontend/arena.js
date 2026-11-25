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
let arenaSystem = null;
let arenaStudy = null;
let arenaProof = null;
let arenaFunnel = null;
let arenaSwing = null;
let arenaHarness = null;

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
    const [res, marketsRes, systemRes, studyRes, proofRes, funnelRes, swingRes, harnessRes] = await Promise.all([
      apiFetch(mq("/api/arena")),
      apiFetch(`${API}/api/arena/markets`).catch(() => null),
      apiFetch(`${API}/api/system`).catch(() => null),
      apiFetch(`${API}/api/asset-study`).catch(() => null),
      apiFetch(`${API}/api/proof`).catch(() => null),
      apiFetch(`${API}/api/funnel`).catch(() => null),
      apiFetch(`${API}/api/swing`).catch(() => null),
      apiFetch(`${API}/api/harness`).catch(() => null),
    ]);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "failed");
    if (marketsRes && marketsRes.ok) {
      arenaMarkets = await marketsRes.json().catch(() => null);
    }
    if (systemRes && systemRes.ok) {
      arenaSystem = await systemRes.json().catch(() => null);
    }
    if (studyRes && studyRes.ok) {
      arenaStudy = await studyRes.json().catch(() => null);
    }
    if (proofRes && proofRes.ok) {
      arenaProof = await proofRes.json().catch(() => null);
    }
    if (funnelRes && funnelRes.ok) {
      arenaFunnel = await funnelRes.json().catch(() => null);
    }
    if (swingRes && swingRes.ok) {
      arenaSwing = await swingRes.json().catch(() => null);
    }
    if (harnessRes && harnessRes.ok) {
      arenaHarness = await harnessRes.json().catch(() => null);
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

// Which stocks it chose today and why. Without this the arena trades a
// list nobody picked and nobody can check.
function picksPanel(data) {
  const alloc = data.allocation || {};
  const picked = data.picked;
  const live = data.symbols_live || [];
  const bits = [];
  if (alloc.slots) {
    bits.push(`${alloc.slots} position${alloc.slots === 1 ? "" : "s"} at a time`
      + ` · ${inr(alloc.per_position_inr, false)} each (${alloc.share_pct}% of the book)`);
  }
  if (!alloc.screened) {
    return `<div class="stock-sub arena-session">Trading your fixed list of
      ${live.length} · ${escapeHtml(bits.join(" · "))}</div>`;
  }
  if (!picked || !picked.rows || !picked.rows.length) {
    return `<div class="stock-sub arena-session">Picking its own stocks each session
      · ${escapeHtml(bits.join(" · "))}</div>
      <p class="arena-hint">The screen has not run for this session yet — it runs once a
      day, before the first bars are fetched. Until then the arena trades its
      ${live.length}-stock fallback list.</p>`;
  }
  const head = `<div class="stock-sub arena-session">Today's picks: ${live.length} of
    ${picked.measured} stocks screened · ${escapeHtml(bits.join(" · "))}</div>`;
  return head + `
    <details class="arena-settings">
      <summary>Why these ${picked.rows.length} stocks</summary>
      <div class="table-scroll"><table class="backtest-table">
        <thead><tr><th>stock</th><th>price</th>
          <th title="median daily turnover - can you get out of it">turnover</th>
          <th title="average daily range as a share of price">daily range</th>
          <th title="how much of one position's allowance a whole number of shares uses">allowance used</th>
          <th>score</th></tr></thead>
        <tbody>${picked.rows.map((r) => `
          <tr>
            <td>${escapeHtml(shortSymbol(r.symbol))}</td>
            <td>₹${r.price.toLocaleString("en-IN")}</td>
            <td>₹${r.turnover_crore}cr</td>
            <td>${(r.atr_pct * 100).toFixed(1)}%</td>
            <td>${r.utilisation == null ? "—" : Math.round(r.utilisation * 100) + "%"}</td>
            <td>${r.score}</td>
          </tr>`).join("")}
        </tbody></table></div>
      <p class="arena-hint">${escapeHtml(picked.note || "")}
        ${picked.rejected ? `${picked.rejected} others were dropped for being too thinly
        traded, too still, too wild, or too dear for one position's allowance.` : ""}</p>
    </details>`;
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
    ${picksPanel(data)}
    ${data.runner && data.runner.mode === "github"
      ? runnerLine(data.runner) : freshnessLine(data.freshness)}
    ${renderBoard(data.leaderboard, { currency: data.currency })}
    <p class="arena-hint">Tap a strategy to see its trades. Benched strategies keep trading in shadow and come back automatically if they start earning.</p>
    ${regulatoryNote(data)}
    ${positions}
    ${renderDayHistory(data.history, data.strategies, data.currency)}`;
}

// The Arena trades 5-minute bars, and Yahoo keeps only ~60 days of those.
// Sixty days cannot prove an intraday edge, so the banner says so above the
// leaderboard rather than in a footnote nobody reads. It disappears on its
// own once the bar archive has stored enough history.
function unprovenBanner(sys) {
  const bars = (sys && sys.bars) || [];
  const live = bars.find((b) => b.used_by === "Strategy Arena");
  if (!live || !live.unproven) return "";
  return `<div class="arena-warn">
    <b>${escapeHtml(live.label)}</b>. These results run on ${escapeHtml(live.bar_size)} bars,
    with ${escapeHtml(live.why)}. Treat the table below as a work in progress, not a verdict.
    The bar archive is accumulating history daily; this notice clears itself once there is enough.
  </div>`;
}

function barTable(sys) {
  const bars = (sys && sys.bars) || [];
  if (!bars.length) return "";
  const rows = bars.map((b) => `
      <tr class="${b.unproven ? "sys-gap" : "sys-ok"}">
        <td class="sys-status">${escapeHtml(b.bar_size)}</td>
        <td class="arena-name">${b.unproven ? escapeHtml(b.label) : "proven"}</td>
        <td class="sys-detail">${escapeHtml(b.why)}${b.used_by ? ` &middot; used by ${escapeHtml(b.used_by)}` : ""}</td>
      </tr>`).join("");
  return `
    <div class="table-scroll">
      <table class="backtest-table sys-table">
        <thead><tr><th>Bars</th><th>Status</th><th>Why</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function settingsTable(sys) {
  const st = sys && sys.settings;
  if (!st) return "";
  const row = (k, v) => `<tr><td class="arena-name">${escapeHtml(k)}</td><td class="sys-detail">${escapeHtml(v)}</td></tr>`;
  return `
    <details class="arena-settings sys-modules">
      <summary>Settings in force</summary>
      <div class="table-scroll">
        <table class="backtest-table">
          <tbody>
            ${row("Market", st.market)}
            ${row("Session", st.session)}
            ${row("Daily capital", inr(st.daily_capital_inr, false))}
            ${row("Swing capital", inr(st.swing_capital_inr, false))}
            ${row("Train window", st.train_window)}
            ${row("Locked window", st.locked_window)}
            ${row("Kill line", `${st.kill_line_multiple}x worst tested drawdown`)}
            ${row("Study tickers", (st.study_tickers || []).join(", "))}
            ${row("Engine candidates", (st.engine_candidates || []).join(", "))}
            ${row("Safe candidates", (st.safe_candidates || []).join(", "))}
          </tbody>
        </table>
      </div>
    </details>`;
}

// ---------- Harness (Phase 6) ----------
// The steps and where the run stopped. A stop is the safe outcome and
// usually the right one, so it is shown neutrally rather than as an
// error: the alarming row is a step that failed, not one that refused.
function harnessPanel(payload) {
  if (!payload) return "";
  if (!payload.ok) {
    return `
      <div class="section-heading"><span class="eyebrow">phase 6</span>
        <h2 style="font-size:17px;">Harness</h2></div>
      <p class="arena-hint">${escapeHtml(payload.reason || "Not run yet.")}</p>`;
  }
  const h = payload.harness || {};
  const steps = (h.steps || []).map((st) => {
    const mark = st.ok ? (st.stop ? "STOP" : "ok") : "FAIL";
    const cls = st.ok ? (st.stop ? "sys-gap" : "sys-ok") : "sys-gap";
    const why = (st.detail && (st.detail.why || st.detail.reason)) || "";
    return `
      <tr class="${cls}">
        <td class="arena-name">${escapeHtml(st.name)}</td>
        <td class="sys-status">${mark}</td>
        <td class="sys-detail">${escapeHtml(String(why))}</td>
      </tr>`;
  }).join("");

  const limits = (h.limits || []).map((l) => `
      <tr class="${(l.rules || []).length ? "sys-gap" : "sys-ok"}">
        <td class="arena-name">${escapeHtml(l.limit)}</td>
        <td class="study-num">${l.orders_in} &rarr; ${l.orders_out}</td>
        <td class="sys-detail">${escapeHtml((l.rules || []).join(", ") || "nothing changed")}</td>
      </tr>`).join("");

  const dbl = h.double_run || {};
  return `
    <div class="section-heading"><span class="eyebrow">phase 6</span>
      <h2 style="font-size:17px;">Harness</h2></div>
    <p class="arena-hint">Each step may stop the run, and stopping is the safe outcome.
      Alerts are ${h.alerts_configured ? "configured" : "<b>not configured</b>, so nothing is sent"}.
      Trust ladder: <b>${escapeHtml(h.trust || "auto")}</b>, paper only.</p>
    <div class="table-scroll">
      <table class="backtest-table sys-table">
        <thead><tr><th>Step</th><th>Result</th><th>Why</th></tr></thead>
        <tbody>${steps}</tbody>
      </table>
    </div>
    <p class="arena-hint">Stopped at: <b>${escapeHtml(h.stopped_at || "nothing; ran to the end")}</b>.
      Plan: ${escapeHtml(h.plan_reason || "")}</p>
    <details class="arena-settings sys-modules" open>
      <summary>Risk limits, each toggled on</summary>
      <div class="table-scroll">
        <table class="backtest-table sys-table">
          <thead><tr><th>Limit</th><th>Orders</th><th>What changed</th></tr></thead>
          <tbody>${limits}</tbody>
        </table>
      </div>
      <p class="arena-hint">Running twice placed ${dbl.first} order(s) then
        <b>${dbl.second}</b>: an order id is <code>date|ticker|side|strategy</code>,
        so a rerun collides with itself instead of doubling a position.</p>
    </details>`;
}

// ---------- Swing account (Phase 5) ----------
// Inline SVG rather than a charting library: the chart CDN is one more
// thing that can fail, and two lines on a shared scale need nothing more.
function equitySvg(core, account, width = 320, height = 86) {
  if (!core || core.length < 2) return "";
  const series = [core, (account && account.length === core.length) ? account : null]
    .filter(Boolean);
  const all = series.flat().map((p) => p[1]);
  const lo = Math.min(...all), hi = Math.max(...all);
  const span = hi - lo || 1;
  const path = (pts) => pts.map((p, i) => {
    const x = (i / (pts.length - 1)) * width;
    const y = height - ((p[1] - lo) / span) * height;
    return `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const lines = series.map((pts, i) =>
    `<path d="${path(pts)}" fill="none" stroke="${i ? "var(--brass)" : "var(--mist)"}"
       stroke-width="${i ? 1.6 : 1.2}" ${i ? "" : 'stroke-dasharray="3 2"'} />`).join("");
  return `<svg class="swing-chart" viewBox="0 0 ${width} ${height}"
            preserveAspectRatio="none" role="img"
            aria-label="Account equity over the training window">${lines}</svg>`;
}

// What was held, and when. A flat equity stretch is a different thing
// depending on whether it was a bad engine or a long spell in cash.
function holdingsBands(bands, engine) {
  if (!bands || !bands.length) return "";
  const first = new Date(bands[0].start).getTime();
  const last = new Date(bands[bands.length - 1].end).getTime();
  const span = Math.max(1, last - first);
  const cls = (t) => t === "CASH" ? "band-cash" : (t === engine ? "band-engine" : "band-safe");
  const seg = bands.map((b) => {
    const a = new Date(b.start).getTime(), z = new Date(b.end).getTime();
    const w = Math.max(0.4, ((z - a) / span) * 100);
    return `<i class="${cls(b.ticker)}" style="width:${w}%" title="${escapeHtml(b.ticker)} ${escapeHtml(b.start)} to ${escapeHtml(b.end)}"></i>`;
  }).join("");
  return `<div class="swing-bands">${seg}</div>
    <div class="swing-legend">
      <span><i class="band-engine"></i> engine</span>
      <span><i class="band-safe"></i> safe</span>
      <span><i class="band-cash"></i> cash</span>
    </div>`;
}

function swingPanel(payload) {
  if (!payload) return "";
  if (!payload.ok) {
    return `
      <div class="section-heading"><span class="eyebrow">phase 5</span>
        <h2 style="font-size:17px;">Swing account</h2></div>
      <p class="arena-hint">${escapeHtml(payload.reason || "Not run yet.")}</p>`;
  }
  const w = payload.swing || {};
  const c = w.core || {}, pick = w.pick || {}, sl = w.sleeves || {};
  const metric = (label, m, note = "") => `
      <tr>
        <td class="arena-name">${escapeHtml(label)}</td>
        <td class="study-num">${m.cagr_pct === undefined ? "n/a" : pct3(m.cagr_pct)}</td>
        <td class="study-num neg">${m.max_drawdown_pct === undefined ? "n/a" : pct3(m.max_drawdown_pct)}</td>
        <td class="study-num">${m.return_over_drawdown == null ? "n/a" : m.return_over_drawdown}</td>
        <td class="sys-detail">${escapeHtml(note)}</td>
      </tr>`;
  const sleeveNote = sl.survivors_available
    ? `${(sl.chosen || []).length} of ${sl.survivors_available} survivors took a slot`
    : "no survivors in bench.yaml, so the account is the core alone";

  const safes = (pick.safes || []).map((r) => `
      <tr class="${r.ticker === w.safe ? "sys-ok" : ""}">
        <td class="arena-name">${escapeHtml(shortSymbol(r.ticker))}</td>
        <td class="study-num">${r.correlation == null ? "n/a" : r.correlation.toFixed(2)}</td>
        <td class="sys-detail">${escapeHtml(r.why || "")}</td>
      </tr>`).join("");

  const quarters = (w.quarters || []).slice(-12).map((q) => `
      <tr>
        <td class="arena-name">${escapeHtml(q.as_of)}</td>
        <td>${escapeHtml(shortSymbol(q.engine || "-"))}</td>
        <td>${escapeHtml(shortSymbol(q.safe || "-"))}</td>
      </tr>`).join("");

  const neighbours = (w.neighbours || []).map((n) => `
      <tr class="${(n.return_pct || 0) > 0 ? "sys-ok" : "sys-gap"}">
        <td class="arena-name">${escapeHtml(n.rule)}</td>
        <td class="study-num">${n.return_pct == null ? "n/a" : pct3(n.return_pct)}</td>
      </tr>`).join("");

  const causal = w.causal || {};
  return `
    <div class="section-heading"><span class="eyebrow">phase 5</span>
      <h2 style="font-size:17px;">Swing account</h2></div>
    <p class="arena-hint">Hold <b>${escapeHtml(shortSymbol(w.engine || "?"))}</b> while it is above its
      200-day average, otherwise <b>${escapeHtml(shortSymbol(w.safe || "CASH"))}</b>.
      Delivery costs, next-open fills, whole shares only.
      ${w.source === "synthetic" ? "<b>Run on synthetic bars:</b> shape only, not a verdict." : ""}</p>
    ${equitySvg(w.equity, w.account_equity)}
    ${holdingsBands(w.holdings, w.engine)}
    <div class="table-scroll">
      <table class="backtest-table sys-table">
        <thead><tr><th>Account</th><th>Return/yr</th><th>Max DD</th><th>Ret/DD</th><th></th></tr></thead>
        <tbody>
          ${metric("Core alone", c)}
          ${metric("Core + sleeves", w.account || c, sleeveNote)}
          ${w.locked ? metric("Locked window", w.locked, "run once") : ""}
        </tbody>
      </table>
    </div>
    <details class="arena-settings sys-modules" open>
      <summary>Neighbours, correlations and quarterly picks</summary>
      <p class="arena-hint">The lineup must not depend on data that did not exist yet:
        re-picked with the future removed, it came out
        <b>${causal.match ? "the same" : "DIFFERENT"}</b>.</p>
      <div class="table-scroll">
        <table class="backtest-table sys-table">
          <thead><tr><th>Trend rule</th><th>Return</th></tr></thead>
          <tbody>${neighbours}</tbody>
        </table>
      </div>
      <div class="table-scroll">
        <table class="backtest-table sys-table">
          <thead><tr><th>Safe candidate</th><th>Corr</th><th>Why</th></tr></thead>
          <tbody>${safes}</tbody>
        </table>
      </div>
      <div class="table-scroll">
        <table class="backtest-table sys-table">
          <thead><tr><th>Quarter</th><th>Engine</th><th>Safe</th></tr></thead>
          <tbody>${quarters}</tbody>
        </table>
      </div>
    </details>`;
}

// ---------- The funnel (Phase 4) ----------
// A horizontal bar per gate, each as wide as the number of ideas still
// standing. The useful reading is where the chart collapses: that is the
// gate doing the actual work, and it names what would have to change.
function funnelChart(counts, order, titles) {
  const total = counts.ideas_in || 1;
  const bars = order.map((key) => {
    const c = counts[key] || { reached: 0, passed: 0, failed: 0 };
    const width = Math.max(2, Math.round((c.passed / total) * 100));
    return `
      <div class="funnel-row">
        <div class="funnel-label">${escapeHtml(titles[key] || key)}</div>
        <div class="funnel-track">
          <i class="funnel-bar" style="width:${width}%"></i>
          <span class="funnel-count">${c.passed} of ${c.reached}</span>
        </div>
        <div class="funnel-lost">${c.failed ? `&minus;${c.failed}` : ""}</div>
      </div>`;
  }).join("");
  return `<div class="funnel">${bars}</div>`;
}

function funnelPanel(payload) {
  if (!payload) return "";
  if (!payload.ok) {
    return `
      <div class="section-heading"><span class="eyebrow">phase 4</span>
        <h2 style="font-size:17px;">The funnel</h2></div>
      <p class="arena-hint">${escapeHtml(payload.reason || "Not run yet.")}</p>`;
  }
  const f = payload.funnel || {};
  const counts = f.counts || {};
  const order = f.order || [];
  const titles = f.titles || {};
  const rows = f.rows || [];
  const survivors = rows.filter((r) => r.survivor);

  const survivorTable = survivors.length
    ? `<div class="table-scroll">
        <table class="backtest-table sys-table">
          <thead><tr><th>Survivor</th><th>Trades</th><th>Return</th><th>PF</th><th>Max drawdown</th></tr></thead>
          <tbody>${survivors.map((r) => `
            <tr class="sys-ok">
              <td class="arena-name">${escapeHtml(r.name)}</td>
              <td class="study-num">${r.training.trades}</td>
              <td class="study-num">${r.training.return_pct === null ? "n/a" : pct3(r.training.return_pct)}</td>
              <td class="study-num">${r.training.profit_factor === null ? "n/a" : r.training.profit_factor}</td>
              <td class="study-num neg">${inr(r.training.max_drawdown, true)}</td>
            </tr>`).join("")}</tbody>
        </table>
      </div>`
    : `<p class="arena-warn">No idea cleared all seven gates. That is a result, not a
        malfunction: it says none of these rules has yet earned capital.</p>`;

  const fallen = rows.filter((r) => !r.survivor).map((r) => `
      <tr class="sys-gap">
        <td class="arena-name">${escapeHtml(r.name)}</td>
        <td class="sys-status">${escapeHtml(titles[r.failed_at] || r.failed_at || "")}</td>
        <td class="sys-detail">${escapeHtml((r.gates && r.gates[r.failed_at] && r.gates[r.failed_at].why) || "")}</td>
      </tr>`).join("");

  return `
    <div class="section-heading"><span class="eyebrow">phase 4</span>
      <h2 style="font-size:17px;">The funnel</h2></div>
    <p class="arena-hint">${counts.ideas_in} ideas in, ${counts.survivors} survivors.
      Every idea that fell is listed with the gate that stopped it, because that is the part
      you can act on.${f.source === "synthetic" ? " <b>Run on synthetic bars:</b> these counts show the funnel working, not a verdict on the strategies." : ""}</p>
    ${funnelChart(counts, order, titles)}
    ${survivorTable}
    <details class="arena-settings sys-modules" open>
      <summary>Where each idea stopped</summary>
      <div class="table-scroll">
        <table class="backtest-table sys-table">
          <thead><tr><th>Idea</th><th>Stopped at</th><th>Why</th></tr></thead>
          <tbody>${fallen}</tbody>
        </table>
      </div>
    </details>`;
}

// ---------- Backtester proof (Phase 3) ----------
// The bands were written before the backtester was built. Showing the
// measured number next to the band it had to land in is the difference
// between "it passed" and something a reader can check.
function proofRow(label, measured, band, ok) {
  return `
      <tr class="${ok ? "sys-ok" : "sys-gap"}">
        <td class="arena-name">${escapeHtml(label)}</td>
        <td class="sys-status">${ok ? "PASS" : "FAIL"}</td>
        <td class="study-num">${escapeHtml(measured)}</td>
        <td class="sys-detail">${escapeHtml(band)}</td>
      </tr>`;
}

function pct3(v) { return `${v > 0 ? "+" : ""}${Number(v).toFixed(3)}%`; }

function proofPanel(payload) {
  if (!payload) return "";
  if (!payload.ok) {
    return `
      <div class="section-heading"><span class="eyebrow">phase 3</span>
        <h2 style="font-size:17px;">Backtester proof</h2></div>
      <p class="arena-hint">${escapeHtml(payload.reason || "Not run yet.")}</p>`;
  }
  const p = payload.proof || {};
  const b = p.bands || {}, r = p.results || {}, g = p.guard || {}, sc = p.samecode || {};
  const failed = (g.failed || []);
  const rows = [
    proofRow("Planted 1.0% edge", pct3(p.planted.avg_net_pct),
             `${b.planted_min}% to ${b.planted_max}% after costs`, r.planted_ok),
    proofRow("Pure noise", pct3(p.noise.avg_net_pct),
             `${pct3(p.noise_expected)} ± ${b.noise_tolerance}%, never positive`, r.noise_ok),
    proofRow("Look-ahead guard", "honest clean, cheat caught",
             "a planted cheat must be detected", r.guard_ok),
    proofRow("Every Arena strategy", failed.length ? `${failed.length} failed` : "all causal",
             "no signal may change when the future is removed", r.strategies_ok),
    proofRow("Same-code proof", `${sc.replay_trades} trades, ${sc.match ? "identical" : "DIFFERENT"}`,
             "replay and the live drip-feed must agree 1:1", r.samecode_ok),
  ].join("");
  const disabled = failed.length
    ? `<p class="arena-warn">Disabled for reading the future: ${failed.map(escapeHtml).join(", ")}</p>`
    : "";
  return `
    <div class="section-heading"><span class="eyebrow">phase 3</span>
      <h2 style="font-size:17px;">Backtester proof</h2></div>
    <p class="arena-hint">Every band here was written before the backtester was built,
      so the comparison is a test rather than a description.
      A deliberate cheat is run through the same guard as a control: if that were ever to pass,
      nothing else on this table would mean anything.</p>
    ${disabled}
    <div class="table-scroll">
      <table class="backtest-table sys-table">
        <thead><tr><th>Check</th><th>Result</th><th>Measured</th><th>Band, written first</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// ---------- Strategy rules (Phase 2) ----------
// The seven parts of every Arena rule. "Not testable yet" is the useful
// column: it names the strategies that cannot enter the funnel as written,
// which is a queue of work rather than a verdict on the strategy.
function ruleTable(spec) {
  if (!spec || !spec.strategies || !spec.strategies.length) return "";
  const rows = spec.strategies.map((r) => `
      <tr class="${r.testable ? "sys-ok" : "sys-gap"}">
        <td class="arena-name">${escapeHtml(r.name)}</td>
        <td class="sys-status">${r.testable ? "OK" : "NOT YET"}</td>
        <td class="sys-detail">${escapeHtml(r.testable ? r.setup : r.why)}</td>
        <td class="study-num">${r.stop_pct === null || r.stop_pct === undefined
          ? `<span class="arena-sub">${escapeHtml(r.stop)}</span>`
          : `${r.stop_pct}%`}</td>
      </tr>`).join("");
  return `
    <div class="section-heading"><span class="eyebrow">phase 2</span>
      <h2 style="font-size:17px;">Strategy rules</h2></div>
    <p class="arena-hint">${spec.testable} of ${spec.testable + spec.not_testable} strategies can be stated in all seven parts.
      The rest have a stop measured in ATR or
      chart structure rather than a percentage, so they cannot enter the funnel as written.
      Every filter is currently <code>none</code>: the regime layer exists, nothing consults it yet.</p>
    <div class="table-scroll">
      <table class="backtest-table sys-table">
        <thead><tr><th>Strategy</th><th>7 parts</th><th>Setup, or why not</th><th>Stop</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// ---------- Asset Study (Phase 1) ----------
// How each ticker actually moves, labelled over the training window. The
// labels matter less than the counts beside them: "keeps going (4)" is a
// story, "keeps going (31)" is a finding, and showing the number stops the
// badge being read as proof.

function labelChip(on, text, count) {
  const n = count === undefined || count === null ? "" : ` (${count})`;
  return `<span class="study-chip ${on ? "study-on" : "study-off"}">${escapeHtml(text)}${n}</span>`;
}

// A bar pair: how big the average move is in calm versus volatile stretches.
// Drawn relative to the larger of the two, so the shape reads at a glance
// without needing an axis.
function calmVolBars(cv) {
  if (!cv || cv.calm === null || cv.volatile === null) return '<span class="arena-sub">n/a</span>';
  const max = Math.max(cv.calm, cv.volatile) || 1;
  const bar = (v, cls) => `<i class="${cls}" style="width:${Math.round((v / max) * 100)}%"></i>`;
  return `<span class="study-bars" title="calm ${(cv.calm * 100).toFixed(2)}% vs volatile ${(cv.volatile * 100).toFixed(2)}%">
    ${bar(cv.calm, "study-calm")}${bar(cv.volatile, "study-vol")}
  </span><span class="arena-sub"> ${cv.ratio ? cv.ratio.toFixed(1) + "x" : ""}</span>`;
}

function pctCell(value, digits = 3) {
  if (value === null || value === undefined) return '<span class="arena-sub">n/a</span>';
  const cls = value > 0 ? "pos" : value < 0 ? "neg" : "neu";
  return `<span class="${cls}">${value > 0 ? "+" : ""}${(value * 100).toFixed(digits)}%</span>`;
}

// Correlation heatmap. Green is independent, red is "this is the same bet
// twice" - which is the thing a diversified book is trying to avoid.
function heatColor(v) {
  if (v === null || v === undefined) return "transparent";
  const x = Math.max(-1, Math.min(1, v));
  if (x >= 0) return `rgba(193, 68, 60, ${(0.08 + 0.72 * x).toFixed(3)})`;
  return `rgba(60, 154, 92, ${(0.08 + 0.72 * -x).toFixed(3)})`;
}

function heatmapTable(heat) {
  if (!heat || !heat.tickers || heat.tickers.length < 2) return "";
  const names = heat.tickers;
  const head = names.map((n) => `<th class="study-rot">${escapeHtml(shortSymbol(n))}</th>`).join("");
  const rows = names.map((name, i) => {
    const cells = heat.matrix[i].map((v) => {
      const text = v === null || v === undefined ? "" : v.toFixed(2);
      return `<td class="study-heat" style="background:${heatColor(v)}">${text}</td>`;
    }).join("");
    return `<tr><th class="study-rowhead">${escapeHtml(shortSymbol(name))}</th>${cells}</tr>`;
  }).join("");
  return `
    <details class="arena-settings sys-modules" open>
      <summary>Correlation heatmap</summary>
      <p class="arena-hint">Red is a pair that moves together, so holding both is one bet twice over.
        Green is a pair that does not.</p>
      <div class="table-scroll">
        <table class="backtest-table study-heatmap">
          <thead><tr><th></th>${head}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </details>`;
}

function assetStudyPanel(payload) {
  if (!payload) return "";
  if (!payload.ok) {
    return `
      <div class="section-heading"><span class="eyebrow">phase 1</span>
        <h2 style="font-size:17px;">Asset study</h2></div>
      <p class="arena-hint">${escapeHtml(payload.reason || "Not run yet.")}</p>`;
  }
  const st = payload.study || {};
  const rows = (st.rows || []).map((r) => `
      <tr>
        <td class="arena-name">${escapeHtml(shortSymbol(r.ticker))}</td>
        <td>${labelChip(r.momentum.labelled, "keeps going", r.momentum.events)}
            ${labelChip(r.reversion.labelled, "snaps back", r.reversion.events)}
            ${labelChip(r.safety.labelled, "safe")}</td>
        <td class="study-num">${pctCell(r.trend_gap.gap)}</td>
        <td class="study-num">${calmVolBars(r.calm_vs_volatile)}</td>
      </tr>`).join("");
  const missing = (st.missing || []).length
    ? `<p class="arena-warn">No data for ${st.missing.map(escapeHtml).join(", ")}. These are not dropped: fix or remove them in the settings block.</p>`
    : "";
  const lines = (st.rows || []).map((r) => `<li>${escapeHtml(r.summary)}</li>`).join("");
  const w = st.window || {};
  return `
    <div class="section-heading"><span class="eyebrow">phase 1</span>
      <h2 style="font-size:17px;">Asset study</h2></div>
    <p class="arena-hint">How each ticker moved over the training window
      ${escapeHtml(w.start || "")} to ${escapeHtml(w.end || "")}, on daily bars.
      A label needs enough events to be more than an anecdote, so the count is shown beside it.</p>
    ${missing}
    <div class="table-scroll">
      <table class="backtest-table study-table">
        <thead><tr><th>Ticker</th><th>Labels</th><th>Trend gap</th><th>Calm vs volatile</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    ${heatmapTable(st.heatmap)}
    <details class="arena-settings sys-modules">
      <summary>In plain English</summary>
      <ul class="study-lines">${lines}</ul>
    </details>`;
}

// ---------- System panel (Phase 0) ----------
// What the system is made of, and which of its own hard rules it does not
// meet yet. The gaps matter more than the map: a result produced under a
// known-broken fill model should be read as optimistic, and the only way
// anyone remembers that is if it is written next to the results.
function systemPanel(sys) {
  if (!sys || !sys.rules || !sys.modules) return "";
  const c = sys.compliance || { ok: 0, gap: 0, total: 0 };
  const rules = sys.rules.map((r) => `
      <tr class="${r.status === "gap" ? "sys-gap" : "sys-ok"}">
        <td class="sys-status">${r.status === "gap" ? "GAP" : "OK"}</td>
        <td class="arena-name">${escapeHtml(r.rule)}</td>
        <td class="sys-detail">${escapeHtml(r.detail)}</td>
      </tr>`).join("");
  const modules = sys.modules.map((m) => `
      <tr>
        <td class="arena-name">${escapeHtml(m.role)}</td>
        <td class="sys-detail"><code>${m.modules.map(escapeHtml).join("</code> <code>")}</code></td>
      </tr>`).join("");
  return `
    <div class="section-heading"><span class="eyebrow">phase 0</span>
      <h2 style="font-size:17px;">System</h2></div>
    <p class="arena-hint">${c.ok} of ${c.total} hard rules met${c.gap ? `, ${c.gap} open` : ""}.
      Open gaps are listed first because they change how today's numbers should be read.</p>
    <div class="table-scroll">
      <table class="backtest-table sys-table">
        <thead><tr><th>Status</th><th>Rule</th><th>Detail</th></tr></thead>
        <tbody>${rules}</tbody>
      </table>
    </div>
    ${barTable(sys)}
    ${settingsTable(sys)}
    <details class="arena-settings sys-modules">
      <summary>Module map</summary>
      <div class="table-scroll">
        <table class="backtest-table">
          <thead><tr><th>Role</th><th>Modules</th></tr></thead>
          <tbody>${modules}</tbody>
        </table>
      </div>
    </details>`;
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

    ${unprovenBanner(arenaSystem)}

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

    ${assetStudyPanel(arenaStudy)}

    ${ruleTable(arenaSystem && arenaSystem.rule_spec)}

    ${proofPanel(arenaProof)}

    ${funnelPanel(arenaFunnel)}

    ${swingPanel(arenaSwing)}

    ${harnessPanel(arenaHarness)}

    ${systemPanel(arenaSystem)}

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
