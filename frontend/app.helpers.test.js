/**
 * Tests for app.js's time helpers, run with: node --test frontend/
 *
 * These cover item 3's frontend half. They exist because the Lightweight
 * Charts library loads from a CDN, so in an environment without CDN access
 * the axis cannot be verified by looking at it - but the formatters that
 * produce those labels are pure functions and can be checked directly.
 *
 * app.js is a plain script, not a module, so the helper block at the top of
 * the file is evaluated here against a stub DOM.
 */
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

process.env.TZ = process.env.TZ || "Asia/Kolkata";

function loadHelpers() {
  const source = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
  // Everything up to the market-status bootstrapping: the pure helpers.
  const end = source.indexOf("const API = \"\";");
  assert.ok(end > 0, "could not locate the helper block in app.js");

  const context = {
    document: { getElementById: () => null, querySelectorAll: () => [] },
    window: { location: { origin: "https://example.test" } },
    Intl,
    Date,
    URL,
    URLSearchParams,
    console,
    fetch: async () => ({ ok: false }),
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(source.slice(0, end), context);

  // A few helpers live further down the file, past DOM-dependent top-level
  // code that cannot run here. They are top-level function declarations, so
  // lift them out by name rather than evaluating everything around them.
  for (const name of ["escapeHtml", "safeUrl", "formatExitReason"]) {
    vm.runInContext(extractFunction(source, name), context);
  }
  return context;
}

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} not found in app.js`);
  // Top-level declarations end at a closing brace in column 0.
  const end = source.indexOf("\n}", start);
  assert.ok(end > start, `could not find the end of ${name}`);
  return source.slice(start, end + 2);
}

const app = loadHelpers();

test("parseUTC reads an offset-less string as UTC, not local time", () => {
  // The bug: new Date("2026-03-03T09:45:00") is LOCAL, so on an IST device
  // every stored timestamp was read 5h30m early.
  const parsed = app.parseUTC("2026-03-03T09:45:00");
  assert.strictEqual(parsed.toISOString(), "2026-03-03T09:45:00.000Z");
});

test("parseUTC leaves an explicit offset alone", () => {
  assert.strictEqual(
    app.parseUTC("2026-03-03T15:15:00+05:30").toISOString(),
    "2026-03-03T09:45:00.000Z",
  );
  assert.strictEqual(
    app.parseUTC("2026-03-03T09:45:00Z").toISOString(),
    "2026-03-03T09:45:00.000Z",
  );
});

test("parseUTC handles junk without throwing", () => {
  assert.ok(Number.isNaN(app.parseUTC("nonsense").getTime()));
  assert.ok(Number.isNaN(app.parseUTC(null).getTime()));
});

test("toUnixSeconds matches the backend's epoch for the same instant", () => {
  // timeutil.epoch_seconds(2026-03-03T09:45Z) == 1772531100
  assert.strictEqual(app.toUnixSeconds("2026-03-03T09:45:00"), 1772531100);
  assert.strictEqual(app.toUnixSeconds("2026-03-03T09:45:00+00:00"), 1772531100);
  assert.strictEqual(app.toUnixSeconds("rubbish"), null);
});

test("fmtIST renders an offset-less timestamp in Asia/Kolkata", () => {
  // 09:45 UTC is 15:15 IST on the same day.
  const text = app.fmtISTDateTime("2026-03-03T09:45:00");
  assert.match(text, /03 Mar/);
  assert.match(text, /03:15\s*pm/i);
});

test("chart tick labels show IST session times, not UTC", () => {
  // 09:15 IST market open == 03:45 UTC. The library formats in UTC, which
  // is why an NSE session used to read 03:45-10:00.
  const open = Date.UTC(2026, 2, 3, 3, 45) / 1000;
  assert.strictEqual(app.istTickMarkFormatter(open, 3), "09:15");

  const close = Date.UTC(2026, 2, 3, 10, 0) / 1000;
  assert.strictEqual(app.istTickMarkFormatter(close, 3), "15:30");
});

test("a daily candle stamped at IST midnight keeps its own date", () => {
  // Yahoo returns .NS daily bars at 00:00 IST == 18:30 UTC the day before,
  // which rendered as the previous date on a UTC axis.
  const midnightIST = Date.UTC(2026, 2, 2, 18, 30) / 1000;
  assert.strictEqual(app.istTickMarkFormatter(midnightIST, 2), "03 Mar");
});

test("tick formatter accepts business-day objects", () => {
  assert.strictEqual(
    app.istTickMarkFormatter({ year: 2026, month: 3, day: 3 }, 2),
    "03 Mar",
  );
});

test("crosshair formatter shows date and IST time", () => {
  const seconds = Date.UTC(2026, 2, 3, 3, 45) / 1000;
  assert.strictEqual(app.istTimeFormatter(seconds), "03 Mar 09:15");
});

test("safeUrl allows http(s) and rejects script-bearing schemes", () => {
  assert.strictEqual(app.safeUrl("https://example.com/x"), "https://example.com/x");
  assert.strictEqual(app.safeUrl("http://example.com/x"), "http://example.com/x");
  assert.strictEqual(app.safeUrl("javascript:alert(1)"), null);
  assert.strictEqual(app.safeUrl("data:text/html,<script>x</script>"), null);
  assert.strictEqual(app.safeUrl(""), null);
  assert.strictEqual(app.safeUrl(undefined), null);
});

test("formatExitReason spells out the EOD square-off", () => {
  // replace("_", " ") only swapped the first underscore, so this used to
  // render as "eod square_off".
  assert.strictEqual(app.formatExitReason("eod_square_off"), "EOD square-off");
  assert.strictEqual(app.formatExitReason("stop_loss"), "stop loss");
  assert.strictEqual(app.formatExitReason("signal_reversal"), "signal reversal");
  assert.strictEqual(app.formatExitReason(undefined), "exit");
});
