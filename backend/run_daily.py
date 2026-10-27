#!/usr/bin/env python3
"""
Advance every Strategy Arena once, then exit.

This is the Arena's actual engine room. It used to run inside the Render
web service on a five-minute scheduler, which was the wrong shape for it:
a free instance has a tiny CPU and sleeps after fifteen minutes, so the
first request at market open woke it straight into a multi-arena catch-up,
it fell over under the load, restarted, and began the same catch-up again -
503s at exactly the moment anyone would look.

Paper trading does not need to be live. The engine replays a finished
session from Yahoo's 5-minute bars and reaches the same fills it would have
reached watching them print, because it only ever acts on completed bars.
So a job that runs every couple of hours and settles whatever has finished
is not a degraded version of live trading - it is the same trading, done
afterwards. Render keeps the dashboard; this does the work.

    python run_daily.py [--market nse|us|forex|crypto|all]
                        [--now ISO8601] [--allow-file] [--max-minutes N]

Deliberately imports no web framework and starts no scheduler: it touches
the arena service, the store and storage, and nothing else.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:                                    # optional, for local runs
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

import storage                          # noqa: E402  (after load_dotenv)
import timeutil                         # noqa: E402
from arena import markets as markets_mod  # noqa: E402
from arena import service as arena_service  # noqa: E402
from arena import store as arena_store  # noqa: E402

DEFAULT_MAX_MINUTES = 12.0
MARKET_ORDER = ["nse", "us", "forex", "crypto"]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Advance every Strategy Arena once.")
    p.add_argument("--market", default="all",
                   choices=["all"] + MARKET_ORDER,
                   help="only advance arenas for this market (default: all)")
    p.add_argument("--now", default=None,
                   help="ISO 8601 instant to run as, for testing. Default: now.")
    p.add_argument("--allow-file", action="store_true",
                   help="permit the JSON-file backend. Local use only.")
    p.add_argument("--max-minutes", type=float, default=DEFAULT_MAX_MINUTES,
                   help=f"stop starting new arenas after this long "
                        f"(default: {DEFAULT_MAX_MINUTES:g})")
    return p.parse_args(argv)


def _resolve_now(raw: str | None) -> dt.datetime:
    if not raw:
        return timeutil.utc_now()
    parsed = timeutil.parse_utc(raw)
    if parsed is None:
        raise SystemExit(f"--now: could not parse {raw!r} as an ISO 8601 instant")
    return parsed


def arena_keys(market: str) -> list[str]:
    """Every live arena key, in a fixed order.

    Sorted so a run that hits its time budget always gets through the same
    arenas first - otherwise an arena at the back of an unordered set could
    go days without being advanced and nobody would notice.
    """
    keys = []
    for stored in arena_store.all_ids():
        if stored.startswith(arena_service.BACKTEST_PREFIX):
            continue        # a saved backtest result, not an arena
        _client, market_id = arena_store.split_key(stored)
        if market != "all" and market_id != market:
            continue
        keys.append(stored)
    rank = {m: i for i, m in enumerate(MARKET_ORDER)}
    return sorted(keys, key=lambda k: (rank.get(arena_store.split_key(k)[1], 99), k))


def advance(key: str, now: dt.datetime) -> dict:
    """Tick one arena and report what changed."""
    before = arena_store.load(key) or {}
    settled_before = {h["date"] for h in before.get("history", [])}
    client, market_id = arena_store.split_key(key)
    row = {
        "key": key, "client": client, "market": market_id,
        "settled": [], "caught_up": 0, "symbols_failed": [],
        "latest_bar_at": None, "net": None, "currency": None,
        "error": None, "skipped": None,
    }
    if not before:
        row["skipped"] = "no state"
        return row
    if not before.get("config", {}).get("enabled", True):
        row["skipped"] = "arena disabled"
        return row

    try:
        arena_service.tick_client(key, now)
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        return row

    after = arena_store.load(key) or {}
    tick = after.get("last_tick") or {}
    row["settled"] = sorted({h["date"] for h in after.get("history", [])} - settled_before)
    row["caught_up"] = after.get("caught_up_last", 0)
    row["symbols_failed"] = tick.get("symbols_failed", [])
    row["symbols_ok"] = tick.get("symbols_ok", [])
    row["latest_bar_at"] = tick.get("latest_bar_at")
    row["settled_nothing"] = bool(tick.get("settled_nothing"))
    market = markets_mod.get(market_id)
    row["currency"] = market.quote_currency
    row["net"] = round(sum(b["stats"]["net"] for sid, b in after.get("books", {}).items()
                           if sid in after.get("config", {}).get("strategies", [])
                           and sid != "benchmark"), 2)
    return row


def summarise(rows: list[dict], started: dt.datetime, now: dt.datetime,
              budget_hit: bool) -> str:
    """A markdown table, for GitHub's run summary and for a human reading
    the log. Written even when nothing happened: "ran, nothing was due" is
    a result, and a silent run is indistinguishable from a broken one."""
    lines = ["## Arena runner", "",
             f"Ran as **{now.isoformat()}** · "
             f"{len(rows)} arena{'' if len(rows) == 1 else 's'} · "
             f"took {(timeutil.utc_now() - started).total_seconds():.0f}s", ""]
    if not rows:
        lines += ["_No arenas stored yet._", ""]
        return "\n".join(lines)

    lines += ["| arena | market | settled | caught up | net | failed symbols | latest bar |",
              "|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["skipped"]:
            detail = f"_{r['skipped']}_"
            lines.append(f"| `{r['client'][:12]}` | {r['market']} | {detail} | | | | |")
            continue
        if r["error"]:
            lines.append(f"| `{r['client'][:12]}` | {r['market']} | **error** | | | "
                         f"`{r['error'][:60]}` | |")
            continue
        settled = ", ".join(r["settled"]) if r["settled"] else "–"
        if r.get("settled_nothing"):
            settled = "**no data – left for next run**"
        failed = ", ".join(r["symbols_failed"]) if r["symbols_failed"] else "–"
        net = "–" if r["net"] is None else f"{r['net']:,.2f} {r['currency']}"
        bar = (r["latest_bar_at"] or "–")[:16].replace("T", " ")
        lines.append(f"| `{r['client'][:12]}` | {r['market']} | {settled} | "
                     f"{r['caught_up']} | {net} | {failed} | {bar} |")
    lines.append("")
    if budget_hit:
        lines += ["> ⏱ Time budget reached — the remaining arenas were left for the "
                  "next run. Catch-up means nothing is lost.", ""]
    return "\n".join(lines)


def write_github_summary(text: str) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError as e:
        print(f"[warn] could not write the run summary: {e}")


# Distinct exit codes, because the run page shows "Process completed with
# exit code N" long before anyone opens the log, and the two causes below
# need completely different fixes.
EXIT_NO_DATABASE_URL = 2
EXIT_POSTGRES_UNUSABLE = 3


def _redact(text: str) -> str:
    """Strip anything resembling credentials out of a driver error before
    it is written anywhere. GitHub masks secret values in log output; it
    does not mask step summaries."""
    return re.sub(r"(?i)(postgres(?:ql)?://)[^@\s]*@", r"\1<redacted>@", str(text))


def refuse_file_backend() -> int:
    """Say which of the two problems this is, on the run page itself.

    Exiting with the reason only on stderr is what this looked like the
    first time it happened: a red job, "exit code 2", and a blank summary.
    """
    configured = bool(getattr(storage, "DATABASE_URL", ""))
    lines = ["## Arena runner - refused to start", ""]
    if not configured:
        code = EXIT_NO_DATABASE_URL
        lines += [
            "**`DATABASE_URL` is not set for this job.** Nothing ran: on the "
            "JSON-file backend every session this run settled would be written "
            "into a container that is deleted when the job ends.",
            "",
            "Check **Settings -> Secrets and variables -> Actions**:",
            "",
            "- it must be a **repository secret**, not a *variable* - those are "
            "separate tabs on that page, and `secrets.DATABASE_URL` cannot see "
            "a variable;",
            "- the name must be exactly `DATABASE_URL`;",
            "- a secret attached to an **Environment** is not visible here, "
            "because this workflow does not request one.",
        ]
    else:
        code = EXIT_POSTGRES_UNUSABLE
        raw = getattr(storage, "DATABASE_URL", "")
        scheme = raw.split("://", 1)[0] if "://" in raw else "(no scheme)"
        lines += [
            "**`DATABASE_URL` is set, but Postgres could not be used.** The "
            "value arrived; loading the driver or connecting failed.",
            "",
            "```",
            _redact(storage.PG_ERROR or "no reason recorded"),
            "```",
            "",
            "Common causes: a missing `?sslmode=require`, a `[YOUR-PASSWORD]` "
            "placeholder never substituted, or an IPv6-only host.",
            "",
            # Shape only, never the value: a step summary is not masked.
            f"_The value is {len(raw)} characters and begins `{scheme}://`._",
        ]
    lines += ["", "_To run against a local file instead, pass `--allow-file`._"]
    text = "\n".join(lines)
    write_github_summary(text)
    print(text, file=sys.stderr)
    return code


def main(argv=None) -> int:
    args = parse_args(argv)
    started = timeutil.utc_now()
    now = _resolve_now(args.now)

    # A file backend in CI means the results are written to a container that
    # is deleted when the job ends. Everything would appear to work and
    # nothing would ever be saved, which is far worse than refusing.
    if not storage._pg_pool and not args.allow_file:
        return refuse_file_backend()

    keys = arena_keys(args.market)
    print(f"[info] runner: {len(keys)} arena(s) for market={args.market}, "
          f"backend={arena_store.backend_name()}, as of {now.isoformat()}")

    rows, budget_hit = [], False
    budget = dt.timedelta(minutes=max(0.1, args.max_minutes))
    for key in keys:
        if timeutil.utc_now() - started > budget:
            # Stop STARTING arenas; never abandon one midway. The catch-up
            # picks up where this left off, and saves per session, so a
            # truncated run costs a delay rather than any work.
            budget_hit = True
            print(f"[info] runner: time budget of {args.max_minutes:g} min reached - "
                  f"{len(keys) - len(rows)} arena(s) left for the next run")
            break
        row = advance(key, now)
        rows.append(row)
        print(f"[info]   {key}: settled={row['settled'] or '-'} "
              f"caught_up={row['caught_up']} failed={row['symbols_failed'] or '-'}"
              + (f" ERROR={row['error']}" if row["error"] else ""))

    text = summarise(rows, started, now, budget_hit)
    write_github_summary(text)
    print()
    print(text)

    if any(r["error"] for r in rows):
        return 1
    # Every arena that had work to do failed to download anything. One
    # arena being starved is a bad symbol list; all of them is Yahoo being
    # unreachable from this runner, and that should show up red.
    attempted = [r for r in rows if not r["skipped"] and not r["error"]]
    starved = [r for r in attempted if r.get("settled_nothing")]
    if attempted and len(starved) == len(attempted):
        print("ERROR: every arena failed to download any bars - the data source "
              "looks unreachable from this runner.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
