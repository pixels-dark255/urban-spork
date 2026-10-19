/**
 * Tests for arena.js's pure render helpers, run with:
 * node --test app.helpers.test.js arena.helpers.test.js
 *
 * These cover the parts of the free-tier wake-up work that only exist in
 * the browser: the day list that says which sessions were replayed rather
 * than watched, the today column that now separates entries from open
 * positions, the no-loss profit factor dash, the storage banner and the
 * freshness line. None of them can be seen in this environment (no CDN, no
 * live backend), but they are pure functions of the API payload, so they
 * can be checked directly.
 *
 * arena.js is a plain script whose top level touches the DOM, so the
 * helpers are lifted out by name instead of evaluating the whole file.
 */
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

process.env.TZ = process.env.TZ || "Asia/Kolkata";

const SOURCE = fs.readFileSync(path.join(__dirname, "arena.js"), "utf8");
const APP = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");

function extractConst(source, name) {
  const start = source.indexOf(`const ${name} =`);
  assert.ok(start >= 0, `${name} not found in arena.js`);
  const end = source.indexOf(";\n", start);
  assert.ok(end > start, `could not find the end of ${name}`);
  return source.slice(start, end + 1);
}

function extractFunction(source, name, where) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} not found in ${where}`);
  // Top-level declarations end at a closing brace in column 0.
  const end = source.indexOf("\n}", start);
  assert.ok(end > start, `could not find the end of ${name}`);
  return source.slice(start, end + 2);
}

function loadArenaHelpers() {
  const context = { Intl, Date, console, Number, Math, Object, String, JSON };
  // renderBoard reads the module-level "which row is expanded" flag.
  context.arenaOpenStrategy = null;
  context.arenaMarket = "nse";
  context.arenaMarkets = null;
  context.globalThis = context;
  vm.createContext(context);

  // Literals lifted out of arena.js, so the labels stay in one place
  // rather than being restated here.
  vm.runInContext(extractConst(SOURCE, "MARKET_LABELS"), context);
  vm.runInContext(extractConst(SOURCE, "DAY_NAMES"), context);

  // escapeHtml goes through a DOM node in the real app; the arena helpers
  // only care that it is called, so a string version keeps this hermetic.
  vm.runInContext(
    `function escapeHtml(s) { return String(s)
       .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
       .replace(/"/g, "&quot;"); }`, context);
  for (const name of ["parseUTC", "fmtIST"]) {
    vm.runInContext(extractFunction(APP, name, "app.js"), context);
  }
  for (const name of ["inr", "money", "moneyWithInr", "pnlClass", "todaySub",
                      "fmtDayLabel", "shortSymbol", "renderDayHistory", "renderBoard",
                      "storageWarning", "freshnessLine", "sessionLine",
                      "renderMarketTabs", "leverageWarning", "regulatoryNote",
                      "renderSlippageSweep", "presetButtons"]) {
    vm.runInContext(extractFunction(SOURCE, name, "arena.js"), context);
  }
  return context;
}

const arena = loadArenaHelpers();

const STRATEGIES = [
  { id: "ema_cross", name: "EMA 9/21 crossover", benchmark: false },
  { id: "orb", name: "Opening-range breakout", benchmark: false },
  { id: "benchmark", name: "Benchmark: buy first bar", benchmark: true },
];

function day(date, { caught_up = false, ema = 100, orb = 40, bench = 20 } = {}) {
  return {
    date, caught_up,
    books: {
      ema_cross: { net: ema, trades: 2, status: "active" },
      orb: { net: orb, trades: 1, status: "active" },
      benchmark: { net: bench, trades: 3, status: "active" },
    },
  };
}

// ---------------------------------------------------------------------------
// The day list - the point of the per-day caught_up flag
// ---------------------------------------------------------------------------

test("a session date is formatted without being parsed as an instant", () => {
  // "2026-10-09" as a Date is UTC midnight; converted through any timezone
  // west of UTC it is the 8th. These are already IST session dates, so the
  // label must come from the string, not from a clock.
  assert.strictEqual(arena.fmtDayLabel("2026-10-09"), "09 Oct");
  assert.strictEqual(arena.fmtDayLabel("2026-01-01"), "01 Jan");
  assert.strictEqual(arena.fmtDayLabel("2026-12-31"), "31 Dec");
});

test("fmtDayLabel passes anything that is not a plain date through unchanged", () => {
  assert.strictEqual(arena.fmtDayLabel(""), "");
  assert.strictEqual(arena.fmtDayLabel(null), "");
  assert.strictEqual(arena.fmtDayLabel("2026-10-09T09:15:00+05:30"), "2026-10-09T09:15:00+05:30");
});

test("a replayed day is tagged and a watched day is not", () => {
  const html = arena.renderDayHistory(
    [day("2026-10-08"), day("2026-10-09", { caught_up: true })], STRATEGIES);
  // Cut each row at its own closing tag: the footnote below the table
  // explains the tag in words, and would otherwise match everywhere.
  const rows = html.split("<tr>\n").slice(1).map((r) => r.split("</tr>")[0]);
  // Newest first, so the caught-up day leads.
  assert.ok(rows[0].includes("09 Oct"), "newest session should be first");
  assert.ok(rows[0].includes("arena-tag"), "a replayed day must say so");
  assert.ok(rows[1].includes("08 Oct"));
  assert.ok(!rows[1].includes("arena-tag"), "a watched day must not be tagged");
});

test("the day list names the best non-benchmark strategy and the benchmark apart", () => {
  const html = arena.renderDayHistory([day("2026-10-09", { ema: 10, orb: 250, bench: 900 })],
                                      STRATEGIES);
  assert.ok(html.includes("Opening-range breakout"), "best of the real strategies");
  assert.ok(!html.includes("Benchmark: buy first bar"),
            "the benchmark is the yardstick column, not a contender");
  assert.ok(html.includes("+₹250") && html.includes("+₹900"));
});

test("the day list counts every book's trades, benchmark included", () => {
  const html = arena.renderDayHistory([day("2026-10-09")], STRATEGIES);
  assert.ok(/<td>6<\/td>/.test(html), `2 + 1 + 3 trades expected, got: ${html}`);
});

test("the day list is empty rather than an empty table when nothing has settled", () => {
  assert.strictEqual(arena.renderDayHistory([], STRATEGIES), "");
  assert.strictEqual(arena.renderDayHistory(undefined, STRATEGIES), "");
});

test("the day list shows at most the last ten sessions", () => {
  const days = Array.from({ length: 14 }, (_, i) =>
    day(`2026-10-${String(i + 1).padStart(2, "0")}`));
  const html = arena.renderDayHistory(days, STRATEGIES);
  assert.strictEqual(html.split("<tr>\n").length - 1, 10);
  assert.ok(html.includes("14 Oct") && !html.includes("04 Oct"));
});

// ---------------------------------------------------------------------------
// Today's column: entries vs open positions vs closed trades
// ---------------------------------------------------------------------------

test("today spells out entries, open positions and open P&L separately", () => {
  const sub = arena.todaySub({ entries: 5, trades: 2, open_positions: 3, unrealised: 40 });
  assert.ok(sub.includes("5 entries"), sub);
  assert.ok(sub.includes("3 open"), sub);
  assert.ok(sub.includes("open P&amp;L") || sub.includes("open P&L"), sub);
  assert.ok(!sub.includes("2 entries"), "closed trades must not be read as entries");
});

test("today omits the open-position bits when the book is flat", () => {
  const sub = arena.todaySub({ entries: 2, trades: 2, open_positions: 0, unrealised: 0 });
  assert.strictEqual(sub, "2 entries");
});

test("a book stopped for the day says so", () => {
  const sub = arena.todaySub({ entries: 1, open_positions: 0, unrealised: 0, stopped: "daily_loss" });
  assert.ok(sub.includes("stopped"), sub);
});

// ---------------------------------------------------------------------------
// The leaderboard's no-loss profit factor
// ---------------------------------------------------------------------------

function row(over = {}) {
  return {
    id: "ema_cross", name: "EMA 9/21 crossover", benchmark: false, status: "active",
    net_total: 120, charges_total: 8, vs_benchmark: 30, trades: 3, win_rate: 100,
    profit_factor: null, no_losses: true, max_drawdown: 0,
    today: { net: 0, entries: 0, trades: 0, open_positions: 0, unrealised: 0 },
    ...over,
  };
}

test("a window with no losing trades shows a dash, not a made-up profit factor", () => {
  const html = arena.renderBoard([row()]);
  assert.ok(!html.includes("99"), "the 99 placeholder must be gone");
  assert.ok(html.includes('title="no losing trades yet"'),
            "the dash needs to explain itself");
});

test("a real profit factor is still printed", () => {
  const html = arena.renderBoard([row({ profit_factor: 1.8, no_losses: false })]);
  assert.ok(html.includes("1.8"));
  assert.ok(!html.includes("no losing trades yet"));
});

test("the net column says it is closed trades only", () => {
  const html = arena.renderBoard([row()]);
  assert.ok(html.includes("net (closed)"),
            "the realised total must not read as mark-to-market");
});

// ---------------------------------------------------------------------------
// Storage and freshness: telling sleep apart from a data failure
// ---------------------------------------------------------------------------

test("the storage banner appears only on ephemeral storage", () => {
  assert.ok(arena.storageWarning({ backend: "json_file", ephemeral: true })
    .includes("DATABASE_URL"));
  assert.strictEqual(arena.storageWarning({ backend: "postgres", ephemeral: false }), "");
  assert.strictEqual(arena.storageWarning(null), "");
});

test("a configured-but-broken Postgres is not told to set DATABASE_URL", () => {
  // The failure that prompted this: the driver would not import, the app
  // fell back to files, and the banner advised setting a variable that was
  // already set.
  const html = arena.storageWarning({
    backend: "json_file", ephemeral: true, configured: true,
    error: "ImportError: undefined symbol: _PyInterpreterState_Get",
  });
  assert.ok(html.includes("is set, but Postgres could not be used"), html);
  assert.ok(html.includes("_PyInterpreterState_Get"), "the reason must be printed");
  assert.ok(!html.includes("Set <code>DATABASE_URL</code> to a free Postgres"),
            "do not tell them to do the thing they have already done");
});

test("a broken Postgres without a captured reason still says it is broken", () => {
  const html = arena.storageWarning({ backend: "json_file", ephemeral: true,
                                      configured: true, error: null });
  assert.ok(html.includes("is set, but Postgres could not be used"), html);
});

test("a stale tick during market hours is called out as sleep", () => {
  const html = arena.freshnessLine({
    last_tick_at: "2026-10-09T04:00:00Z", age_minutes: 95, stale: true,
    caught_up_last: 0, symbols_ok: ["RELIANCE.NS"], symbols_failed: [],
    latest_bar_at: "2026-10-09T05:35:00+05:30",
  });
  assert.ok(html.includes("The server was asleep"), html);
  assert.ok(html.includes("last updated"), html);
});

test("a fresh tick with a failed symbol blames the data, not sleep", () => {
  const html = arena.freshnessLine({
    last_tick_at: "2026-10-09T04:00:00Z", age_minutes: 2, stale: false,
    caught_up_last: 0, symbols_ok: ["RELIANCE.NS"], symbols_failed: ["TCS.NS"],
    latest_bar_at: null,
  });
  assert.ok(!html.includes("The server was asleep"), html);
  assert.ok(html.includes("data for TCS failed"), html);
});

test("the freshness line reports how many days the wake caught up", () => {
  const one = arena.freshnessLine({ last_tick_at: "2026-10-09T04:00:00Z", stale: false,
                                    caught_up_last: 1, symbols_failed: [] });
  const many = arena.freshnessLine({ last_tick_at: "2026-10-09T04:00:00Z", stale: false,
                                     caught_up_last: 4, symbols_failed: [] });
  assert.ok(one.includes("1 day caught up"), one);
  assert.ok(many.includes("4 days caught up"), many);
});

test("an arena that has never ticked says so instead of showing a blank time", () => {
  const html = arena.freshnessLine({ last_tick_at: null, stale: false,
                                     caught_up_last: 0, symbols_failed: [] });
  assert.ok(html.includes("no update yet"), html);
});

// ---------------------------------------------------------------------------
// Global markets: currency, tabs, sessions, the regulatory note
// ---------------------------------------------------------------------------

const USD = { code: "USD", symbol: "$", fractional: false };
const RUPEES = { code: "INR", symbol: "₹", fractional: false };

test("rupees print whole and other currencies keep their cents", () => {
  // A forex session's entire profit can be a few cents, so rounding the
  // book's own currency to whole units would quantise the result away.
  assert.strictEqual(arena.money(1234.56, RUPEES), "+₹1,235");
  assert.strictEqual(arena.money(12.3456, USD), "+$12.35");
  assert.strictEqual(arena.money(-12.3, USD), "−$12.30");
  assert.strictEqual(arena.money(0, USD), "$0.00");
  assert.strictEqual(arena.money(null, USD), "—");
  assert.strictEqual(arena.money(12.3, USD, false), "$12.30");
});

test("money defaults to rupees when no currency is given", () => {
  assert.strictEqual(arena.money(500, null), "+₹500");
});

test("a foreign amount is shown with its rupee equivalent underneath", () => {
  const html = arena.moneyWithInr(12.97, 1148.08, USD);
  assert.ok(html.includes("+$12.97"), html);
  assert.ok(html.includes("+₹1,148"), html);
});

test("the India arena shows one figure, not the same number twice", () => {
  const html = arena.moneyWithInr(1148, 1148, RUPEES);
  assert.strictEqual(html, "+₹1,148");
});

test("a missing rate is admitted rather than filled in with the native number", () => {
  const html = arena.moneyWithInr(12.97, null, USD);
  assert.ok(html.includes("+$12.97"));
  assert.ok(html.includes("rate unavailable"), html);
  assert.ok(!html.includes("₹12"), "the dollar figure must not be passed off as rupees");
});

test("the market strip marks the selected market and shows open/shut", () => {
  const html = arena.renderMarketTabs({});
  for (const label of ["India", "US", "Forex", "Crypto"]) {
    assert.ok(html.includes(label), `${label} tab missing`);
  }
  // arenaMarket is "nse" in this harness.
  const nseTab = html.split("</button>").find((t) => t.includes("India"));
  assert.ok(nseTab.includes('class="arena-market-tab on"'), nseTab);
  assert.ok(nseTab.includes('aria-selected="true"'));
  const usTab = html.split("</button>").find((t) => t.includes(">US"));
  assert.ok(usTab.includes('aria-selected="false"'));
});

test("the open/shut dot is backed by a text title, not colour alone", () => {
  arena.arenaMarkets = {
    markets: [{ id: "nse", is_open: false }, { id: "us", is_open: true },
              { id: "forex", is_open: true }, { id: "crypto", is_open: true }],
  };
  const html = arena.renderMarketTabs({});
  assert.ok(html.includes('title="market open"'));
  assert.ok(html.includes('title="market closed"'));
  arena.arenaMarkets = null;
});

test("the session line gives both clocks, because only one of them moves", () => {
  const html = arena.sessionLine({
    market_profile: { name: "US stocks" },
    session: {
      today: { start_ist: "19:00", end_ist: "01:30",
               start_local: "09:30", end_local: "16:00" },
      next: { start_ist: "19:00" }, next_date: "2026-10-20",
      market_timezone_label: "New York", same_clock_as_ist: false, active_now: false,
    },
  });
  assert.ok(html.includes("19:00–01:30 IST"), html);
  assert.ok(html.includes("09:30–16:00 New York"), html);
  assert.ok(html.includes("next 20 Oct at 19:00 IST"), html);
});

test("the session line says when there is no session today", () => {
  const html = arena.sessionLine({
    market_profile: { name: "Forex" },
    session: { today: null, next: null, next_date: null,
               market_timezone_label: "New York", same_clock_as_ist: false,
               active_now: false },
  });
  assert.ok(html.includes("no session in your window today"), html);
});

test("a live session says so rather than pointing at the next one", () => {
  const html = arena.sessionLine({
    market_profile: { name: "Crypto" },
    session: { today: { start_ist: "19:00", end_ist: "23:30",
                        start_local: "13:30", end_local: "18:00" },
               next: { start_ist: "19:00" }, next_date: "2026-10-20",
               market_timezone_label: "UTC", same_clock_as_ist: false,
               active_now: true },
  });
  assert.ok(html.includes("trading now"), html);
  assert.ok(!html.includes("next 20 Oct"), html);
});

test("leverage above 1x is warned about, and 1x is not", () => {
  assert.strictEqual(arena.leverageWarning({ config: { leverage: 1 } }), "");
  assert.strictEqual(arena.leverageWarning({ config: {} }), "");
  const warn = arena.leverageWarning({ config: { leverage: 3 } });
  assert.ok(warn.includes("3×"), warn);
  assert.ok(warn.includes("arena-warn-hard"), "the leverage warning is the loud one");
});

test("the regulatory note is rendered whenever the server sends it", () => {
  const note = "Simulation only. For real money in India: forex is legal only in INR pairs.";
  assert.ok(arena.regulatoryNote({ regulatory_note: note }).includes("INR pairs"));
  assert.strictEqual(arena.regulatoryNote({}), "");
});

test("symbols are shown without the feed suffix the user never typed", () => {
  assert.strictEqual(arena.shortSymbol("RELIANCE.NS"), "RELIANCE");
  assert.strictEqual(arena.shortSymbol("TATASTEEL.BO"), "TATASTEEL");
  assert.strictEqual(arena.shortSymbol("EURUSD=X"), "EURUSD");
  assert.strictEqual(arena.shortSymbol("AAPL"), "AAPL");
  assert.strictEqual(arena.shortSymbol("BTC-USD"), "BTC-USD");
});

test("preset buttons are built from whatever the market offers", () => {
  const html = arena.presetButtons({ presets: { majors: ["A", "B"], inr_pairs: ["C"] } });
  assert.ok(html.includes("use 2 majors"), html);
  assert.ok(html.includes("use 1 inr pairs"), html);
  assert.strictEqual(arena.presetButtons({}), "");
});

test("the leaderboard prints a foreign book in both currencies", () => {
  const html = arena.renderBoard([{
    id: "ema_cross", name: "EMA 9/21 crossover", benchmark: false, status: "active",
    net_total: 12.97, net_total_inr: 1148.08, charges_total: 0.42, vs_benchmark: 3.1,
    trades: 4, win_rate: 75, profit_factor: 1.8, no_losses: false, max_drawdown: 0,
    currency: "USD", currency_symbol: "$",
    today: { net: 1.5, net_inr: 132.75, entries: 1, trades: 1, open_positions: 0, unrealised: 0 },
  }], { currency: USD });
  assert.ok(html.includes("+$12.97"), html);
  assert.ok(html.includes("+₹1,148"), html);
  assert.ok(html.includes("$0.42 costs"), html);
});

test("the slippage sweep labels each column with what it assumes", () => {
  const html = arena.renderSlippageSweep({
    levels: [{ label: "0×", describes: "perfect fills, no cost" },
             { label: "1×", describes: "1.5 pips typical spread" }],
    rows: [{ id: "benchmark", name: "Benchmark", benchmark: true,
             by_slippage: { "0×": { net: 5.5 }, "1×": { net: -2.25 } } }],
    note: "Same bars at each level.",
  }, USD);
  assert.ok(html.includes("1.5 pips typical spread"), html);
  assert.ok(html.includes("+$5.50") && html.includes("−$2.25"), html);
});

test("the India arena is not told its own session twice", () => {
  const html = arena.sessionLine({
    market_profile: { name: "India (NSE)" },
    session: { today: { start_ist: "09:15", end_ist: "15:30",
                        start_local: "09:15", end_local: "15:30" },
               next: null, next_date: null, market_timezone_label: "India",
               same_clock_as_ist: true, active_now: true },
  });
  assert.ok(html.includes("09:15–15:30 IST"), html);
  assert.ok(!html.includes("arena-sub"),
            "no redundant second clock on the India arena");
});
