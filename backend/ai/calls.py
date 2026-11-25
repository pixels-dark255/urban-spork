"""The call vocabulary, and the record that must exist before a call acts.

Four calls, and no others:

    hold             leave the core where it is
    safe_for_5d      sit in the safe asset for five trading days
    safe_for_10d     sit in the safe asset for ten trading days
    engine_for_10d   hold the engine for ten trading days

A closed list is the point. An open-ended instruction ("reduce exposure
somewhat") cannot be backtested, cannot be replayed and cannot be
compared against the core, so it cannot be argued with either.

A call is written down before it can affect anything. Not after, and not
alongside: `apply` refuses a call that has no stored record, so the audit
trail cannot be the thing that gets skipped when it matters most.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import pathlib

CALLS = ("hold", "safe_for_5d", "safe_for_10d", "engine_for_10d")

HOLD_DAYS = {"hold": 0, "safe_for_5d": 5, "safe_for_10d": 10,
             "engine_for_10d": 10}

# A document older than this has already been priced in, and a call made
# on it is a call about the past.
MAX_DOCUMENT_AGE_TRADING_DAYS = 5

ARCHIVE = pathlib.Path(__file__).resolve().parent.parent.parent / "ai_calls"


class CallError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Call:
    """One decision. Frozen: a record that can be edited is not a record."""
    call: str
    reason: str
    made_at: str
    model: str
    source_url: str
    document_date: str
    document_hash: str
    masked: bool
    core_holding: str

    def __post_init__(self):
        if self.call not in CALLS:
            raise CallError("%r is not one of the four calls: %s"
                            % (self.call, ", ".join(CALLS)))
        if not str(self.reason).strip():
            raise CallError("a call must carry a one-line reason")
        if "\n" in self.reason.strip():
            raise CallError("the reason is one line, not a paragraph")
        if not self.source_url:
            raise CallError("a call must name the document it was made from")

    @property
    def id(self) -> str:
        """Deterministic, so the same call cannot be recorded twice."""
        return hashlib.sha256("|".join([
            self.document_hash, self.call, self.made_at[:10],
            str(self.masked)]).encode()).hexdigest()[:16]

    def hold_days(self) -> int:
        return HOLD_DAYS[self.call]

    def to_dict(self) -> dict:
        out = dataclasses.asdict(self)
        out["id"] = self.id
        return out


def document_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def trading_days_between(first: dt.date, last: dt.date) -> int:
    """Weekdays that are not NSE holidays. The calendar, not the clock."""
    import market_calendar
    if last < first:
        return -trading_days_between(last, first)
    days, day = 0, first
    while day < last:
        day += dt.timedelta(days=1)
        if day.weekday() < 5 and not market_calendar.is_trading_holiday(day):
            days += 1
    return days


def too_old(document_date: dt.date, as_of: dt.date) -> bool:
    """A call is not made on a document older than five trading days."""
    return trading_days_between(document_date, as_of) > MAX_DOCUMENT_AGE_TRADING_DAYS


class CallLog:
    """Append-only. There is no update and no delete, on purpose."""

    def __init__(self, folder: pathlib.Path | None = None):
        self.folder = pathlib.Path(folder or ARCHIVE)
        self.folder.mkdir(parents=True, exist_ok=True)

    def path_for(self, call_id: str) -> pathlib.Path:
        return self.folder / ("call-%s.json" % call_id)

    def record(self, call: Call) -> dict:
        """Write the call down. Returns the stored record.

        Recording the same call twice is not an error and does not write a
        second copy: the id is derived from the document and the decision,
        so a rerun is the same record rather than a new one.
        """
        path = self.path_for(call.id)
        if path.exists():
            return json.loads(path.read_text())
        record = call.to_dict()
        record["recorded_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
        return record

    def has(self, call_id: str) -> bool:
        return self.path_for(call_id).exists()

    def get(self, call_id: str) -> dict | None:
        path = self.path_for(call_id)
        return json.loads(path.read_text()) if path.exists() else None

    def all(self) -> list[dict]:
        return [json.loads(p.read_text())
                for p in sorted(self.folder.glob("call-*.json"))]


def apply(call: Call, log: CallLog, core_holding: str, engine: str,
          safe: str) -> dict:
    """What the core should hold, given a recorded call.

    Refuses a call that has not been written down. The ordering is the
    whole safeguard: a layer that can act first and log afterwards will,
    one day, act and not log.
    """
    if not log.has(call.id):
        raise CallError(
            "call %s has not been recorded; a call is written down before "
            "it can affect anything" % call.id)
    if call.call == "hold":
        return {"holding": core_holding, "days": 0, "changed": False}
    target = engine if call.call == "engine_for_10d" else safe
    return {"holding": target, "days": call.hold_days(),
            "changed": target != core_holding}
