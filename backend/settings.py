"""The settings block in AGENTS.md, parsed.

Every phase reads its capital figures, windows and ticker lists from here
rather than hardcoding them, so changing a number is editing one block of
prose instead of hunting through modules.

The block is parsed by hand rather than with PyYAML. PyYAML is present in
this environment only as a transitive dependency of uvicorn[standard], so
importing it directly would be a dependency the project never declared -
and the format here is small enough that a hand parser gives better errors
("settings line 7") than a YAML traceback would.
"""
import datetime
import pathlib
import re

AGENTS = pathlib.Path(__file__).resolve().parent.parent / "AGENTS.md"

SETTINGS_HEADING = "## 2. My settings"


class SettingsError(ValueError):
    """The block is missing, malformed, or missing a required key."""


# ------------------------------------------------------------- parsing

def _strip_comment(value: str) -> str:
    """Drop a trailing ` # comment`, keeping a '#' that is inside a value."""
    out, quote = [], None
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or value[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).strip()


def _scalar(text: str):
    text = text.strip().strip('"').strip("'")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d*\.\d+", text):
        return float(text)
    return text


def extract_block(markdown: str) -> str:
    """The fenced code block under the settings heading."""
    start = markdown.find(SETTINGS_HEADING)
    if start < 0:
        raise SettingsError("AGENTS.md has no %r section" % SETTINGS_HEADING)
    fence = markdown.find("```", start)
    if fence < 0:
        raise SettingsError("the settings section has no code block")
    body_start = markdown.index("\n", fence) + 1
    end = markdown.find("```", body_start)
    if end < 0:
        raise SettingsError("the settings code block is never closed")
    return markdown[body_start:end]


def parse(block: str) -> dict:
    """key: value pairs, with [a, b] lists that may wrap over lines."""
    out: dict = {}
    key = None
    buffer = ""
    for lineno, raw in enumerate(block.splitlines(), 1):
        line = _strip_comment(raw)
        if not line:
            continue
        if buffer:                      # continuing a wrapped [ ... ] list
            buffer += " " + line
            if "]" in line:
                out[key] = _list(buffer, lineno)
                buffer, key = "", None
            continue
        if ":" not in line:
            raise SettingsError("settings line %d has no 'key: value': %r"
                                % (lineno, raw.strip()))
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not key:
            raise SettingsError("settings line %d has an empty key" % lineno)
        if key in out:
            raise SettingsError("settings key %r appears twice" % key)
        if value.startswith("[") and "]" not in value:
            buffer = value              # list wraps onto the next line
            continue
        out[key] = _list(value, lineno) if value.startswith("[") else _scalar(value)
        if not value.startswith("["):
            key = None
    if buffer:
        raise SettingsError("settings list for %r is never closed" % key)
    return out


def _list(text: str, lineno: int) -> list:
    inner = text[text.index("[") + 1:text.rindex("]")]
    return [_scalar(p) for p in inner.split(",") if p.strip()]


# ------------------------------------------------------------ the view

REQUIRED = ("market", "timezone", "session", "bar_sizes", "default_bar_size",
            "study_tickers", "engine_candidates", "safe_candidates",
            "daily_capital_inr", "swing_capital_inr", "train_window_daily",
            "locked_window_daily", "cost_model", "kill_line_multiple",
            "hourly_train_fraction", "unproven_bar_sizes",
            "min_months_for_funnel")

SESSION_RE = re.compile(
    r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2}).*?square-off by\s*(\d{1,2}:\d{2})")

WINDOW_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\s*\.\.\s*(\d{4}-\d{2}-\d{2}|today)")

# Not a ticker: holding rupees. It must never be sent to a data source.
CASH = "CASH"


def _window(value: str, field: str, today: datetime.date) -> tuple:
    m = WINDOW_RE.fullmatch(str(value).strip())
    if not m:
        raise SettingsError(
            "%s must read 'YYYY-MM-DD .. YYYY-MM-DD' or '.. today', got %r"
            % (field, value))
    start = datetime.date.fromisoformat(m.group(1))
    end = today if m.group(2) == "today" else datetime.date.fromisoformat(m.group(2))
    if end < start:
        raise SettingsError("%s ends before it starts" % field)
    return start, end


def load(path: pathlib.Path | None = None, today: datetime.date | None = None) -> dict:
    """The parsed, validated settings every phase reads."""
    return _load_from_block(extract_block((path or AGENTS).read_text()), today)


def _load_from_block(block: str, today: datetime.date | None = None) -> dict:
    """Validation, separated from reading the file so it can be tested
    against a block directly."""
    today = today or datetime.date.today()
    raw = parse(block)

    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise SettingsError("settings block is missing: %s" % ", ".join(missing))

    m = SESSION_RE.search(str(raw["session"]))
    if not m:
        raise SettingsError("session must read '09:15-15:30 IST, intraday "
                            "square-off by 15:15', got %r" % raw["session"])
    open_, close, square_off = m.groups()

    train = _window(raw["train_window_daily"], "train_window_daily", today)
    locked = _window(raw["locked_window_daily"], "locked_window_daily", today)
    if locked[0] <= train[1]:
        raise SettingsError(
            "the locked window starts %s, inside the training window that "
            "ends %s. A locked test that overlaps training proves nothing."
            % (locked[0], train[1]))

    fraction = float(raw["hourly_train_fraction"])
    if not 0 < fraction < 1:
        raise SettingsError("hourly_train_fraction must be between 0 and 1")

    bar_sizes = [str(b) for b in raw["bar_sizes"]]
    unproven = [str(b) for b in raw["unproven_bar_sizes"]]
    unknown = [b for b in unproven if b not in bar_sizes]
    if unknown:
        raise SettingsError("unproven_bar_sizes names a bar size that is not "
                            "in bar_sizes: %s" % ", ".join(unknown))
    if str(raw["default_bar_size"]) not in bar_sizes:
        raise SettingsError("default_bar_size %r is not in bar_sizes"
                            % raw["default_bar_size"])

    tickers = [str(t) for t in raw["study_tickers"]]
    engine = [str(t) for t in raw["engine_candidates"]]
    safe = [str(t) for t in raw["safe_candidates"]]

    return {
        "market": str(raw["market"]),
        "timezone": str(raw["timezone"]),
        "session_open": open_, "session_close": close, "square_off": square_off,
        "bar_sizes": bar_sizes,
        "default_bar_size": str(raw["default_bar_size"]),
        "study_tickers": tickers,
        "engine_candidates": engine,
        "safe_candidates": safe,
        "daily_capital_inr": float(raw["daily_capital_inr"]),
        "swing_capital_inr": float(raw["swing_capital_inr"]),
        "train_window_daily": train,
        "locked_window_daily": locked,
        "cost_model": str(raw["cost_model"]),
        "kill_line_multiple": float(raw["kill_line_multiple"]),
        "hourly_train_fraction": fraction,
        "unproven_bar_sizes": unproven,
        "min_months_for_funnel": int(raw["min_months_for_funnel"]),
    }


def all_tickers(cfg: dict) -> list[str]:
    """Every symbol a data source should be able to resolve.

    CASH is excluded: it means holding rupees, not an instrument.
    """
    seen = []
    for name in ("study_tickers", "engine_candidates", "safe_candidates"):
        for t in cfg[name]:
            if t != CASH and t not in seen:
                seen.append(t)
    return sorted(seen)


def is_unproven(cfg: dict, bar_size: str) -> bool:
    """True when a result on this bar size must be labelled UNPROVEN."""
    return str(bar_size) in cfg["unproven_bar_sizes"]


def fingerprint_values(cfg: dict) -> dict:
    return {
        "market": cfg["market"],
        "tickers": len(all_tickers(cfg)),
        "study": len(cfg["study_tickers"]),
        "bar_sizes": ",".join(cfg["bar_sizes"]),
        "daily_capital_inr": cfg["daily_capital_inr"],
        "swing_capital_inr": cfg["swing_capital_inr"],
        "kill_line_multiple": cfg["kill_line_multiple"],
        "train_start": cfg["train_window_daily"][0].isoformat(),
        "train_end": cfg["train_window_daily"][1].isoformat(),
        "locked_start": cfg["locked_window_daily"][0].isoformat(),
        "hourly_train_fraction": cfg["hourly_train_fraction"],
        "min_months_for_funnel": cfg["min_months_for_funnel"],
        "unproven": ",".join(cfg["unproven_bar_sizes"]),
    }


def report(cfg: dict) -> str:
    """The settings, as parsed. Printed so a phase's assumptions are visible
    rather than implied."""
    train, locked = cfg["train_window_daily"], cfg["locked_window_daily"]
    rows = [
        ("market", cfg["market"]),
        ("timezone", cfg["timezone"]),
        ("session", "%s-%s, square-off %s"
         % (cfg["session_open"], cfg["session_close"], cfg["square_off"])),
        ("bar sizes", "%s (default %s)"
         % (", ".join(cfg["bar_sizes"]), cfg["default_bar_size"])),
        ("unproven", "%s - labelled UNPROVEN until %d months are stored"
         % (", ".join(cfg["unproven_bar_sizes"]), cfg["min_months_for_funnel"])),
        ("1h split", "train first %.0f%%, locked last %.0f%%"
         % (cfg["hourly_train_fraction"] * 100,
            (1 - cfg["hourly_train_fraction"]) * 100)),
        ("daily capital", f"\u20b9{cfg['daily_capital_inr']:,.0f}"),
        ("swing capital", f"\u20b9{cfg['swing_capital_inr']:,.0f}"),
        ("train window", "%s .. %s" % (train[0], train[1])),
        ("locked window", "%s .. %s" % (locked[0], locked[1])),
        ("kill line", "%.1fx worst tested drawdown" % cfg["kill_line_multiple"]),
        ("cost model", cfg["cost_model"]),
        ("study tickers", "%d: %s" % (len(cfg["study_tickers"]),
                                      ", ".join(cfg["study_tickers"]))),
        ("engine candidates", ", ".join(cfg["engine_candidates"])),
        ("safe candidates", ", ".join(cfg["safe_candidates"])),
    ]
    width = max(len(k) for k, _ in rows)
    return "\n".join("%-*s  %s" % (width, k, v) for k, v in rows)


if __name__ == "__main__":
    import argparse

    import fingerprint as _fp

    ap = argparse.ArgumentParser(description="Print and check the settings block.")
    ap.add_argument("--offline", action="store_true",
                    help="skip the ticker probe (no network)")
    args = ap.parse_args()

    cfg = load()
    print("SETTINGS (parsed from AGENTS.md §2)")
    print(report(cfg))
    print()

    values = fingerprint_values(cfg)
    if args.offline:
        print("Tickers: not checked (--offline). %d would be probed."
              % len(all_tickers(cfg)))
    else:
        import ticker_check

        result = ticker_check.check(cfg)
        print(ticker_check.report(result))
        values["unresolved"] = len(result["unresolved"])
    print()
    print(_fp.line(0, values))
