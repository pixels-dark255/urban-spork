# Trading skills

Eleven tools from [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill),
installed here and retargeted from the NYSE to the NSE. Each tool is a
standalone script with no imports, so it carries its own copy of the market
calendar; the edits below were applied file by file rather than factored into a
shared module, which nobody asked for.

## What was changed

| Rule | Where it landed |
| --- | --- |
| Indian market | prose, currency (`₹`, not `$`) and the calendar in all 9 India-relevant tools |
| `.NS` kept as-is | `yahoo_symbol()` keeps a `.NS`/`.BO` suffix; `BRK.B` still becomes `BRK-B` |
| 09:15–15:30 IST | `OPEN`/`CLOSE`, `OPEN_MIN`/`CLOSE_MIN`, and `SESSION_BARS = 75` (375 minutes / 5) |
| NSE holidays | `NSE_HOLIDAYS`, copied from `backend/nse_holidays.json` so the skill and the app cannot disagree |
| Flat by 15:15 | `flat_by` default in the exit, risk, broker and budget tools |

India has no daylight saving, so the NYSE's DST calculator was replaced with a
flat +5:30 and the now-unreachable `_nth_sunday` helper removed.

## Known limits

- **`trader-analyst` and `trader-pelosi` were left on US sources.** They read SEC
  EDGAR company facts and US House stock disclosures respectively. Neither has an
  Indian equivalent to point at, so retargeting them would mean rewriting them,
  not adapting them. They still work on US tickers.
- **`trader-news` is half-adapted.** Its Yahoo headline feed works for `.NS`
  symbols; its SEC filing feed does not, and skips any ticker with no CIK.
- **`trader-alerts` falls back to stooq**, whose URL is `%s.us` — US only. The
  primary Yahoo path is what runs for Indian symbols.
- **Holidays cover 2026 only.** `backend/nse_holidays.json` lists no 2027 dates
  yet; until it does, the tools treat every 2027 weekday as a trading day. Add
  them in one place and the skills follow.
