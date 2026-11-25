"""Who actually makes the call.

An interface with two implementations. The stub is deterministic and is
what the tests and the offline CHECK use; the model-backed one is there
so that turning this on is a configuration change rather than a rewrite.

The API key comes from the environment and is never logged, for the same
reason the Telegram token is not: a failed request carries it into
whatever catches the exception.

Nothing here decides anything on its own. A caller returns a call and a
one-line reason; `calls.apply` refuses to act on either until the record
exists on disk.
"""
from __future__ import annotations

import datetime as dt
import os
import re

from . import calls as calls_mod
from .mask import mask

KEY_VAR = "ANTHROPIC_API_KEY"

PROMPT = """You are given a central bank policy document and the one before it.
Reply with exactly one line:

  <call>: <one short reason>

where <call> is one of: {calls}

Choose `hold` unless the document clearly changes the outlook for equities.
Do not explain beyond the single line.

--- previous document ---
{previous}

--- current document ---
{current}
"""


def redact(text, key: str | None = None) -> str:
    out = str(text)
    key = key or os.environ.get(KEY_VAR, "")
    if key:
        out = out.replace(key, "<redacted>")
    return re.sub(r"\bsk-[A-Za-z0-9_\-]{10,}\b", "<redacted>", out)


class Caller:
    name = "base"

    def decide(self, current: str, previous: str, core_holding: str) -> tuple:
        raise NotImplementedError


class StubCaller(Caller):
    """Deterministic, and deliberately simple.

    It reads two things out of the text - whether the policy language is
    tightening or easing - and calls accordingly. It is not pretending to
    be clever: its job is to make the pipeline runnable and testable
    without a model, and to make the replay's arithmetic checkable by
    hand. A result produced with this caller is labelled as such.
    """
    name = "stub-v1"

    TIGHTEN = ("hike", "tighten", "withdrawal of accommodation", "raise the",
               "increased the policy")
    EASE = ("cut", "ease", "accommodative", "reduce the policy", "lowered")

    def decide(self, current, previous, core_holding):
        text = (current or "").lower()
        tighten = sum(text.count(w) for w in self.TIGHTEN)
        ease = sum(text.count(w) for w in self.EASE)
        if tighten > ease:
            return "safe_for_10d", "language tightened against the previous statement"
        if ease > tighten:
            return "engine_for_10d", "language eased against the previous statement"
        return "hold", "no clear change in policy direction"


class AnthropicCaller(Caller):
    """The real one. Off unless a key is configured."""
    name = "claude"

    def __init__(self, model: str = "claude-sonnet-5-5", key: str | None = None):
        self.model = model
        self.key = key or os.environ.get(KEY_VAR, "")
        self.name = "anthropic:%s" % model
        self.errors: list[str] = []

    def configured(self) -> bool:
        return bool(self.key)

    def decide(self, current, previous, core_holding):
        if not self.configured():
            raise RuntimeError("%s is not set; the AI layer stays off" % KEY_VAR)
        import json
        import urllib.request
        body = json.dumps({
            "model": self.model, "max_tokens": 100,
            "messages": [{"role": "user", "content": PROMPT.format(
                calls=", ".join(calls_mod.CALLS),
                previous=previous or "(none)", current=current)}],
        }).encode()
        request = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"content-type": "application/json",
                     "x-api-key": self.key,
                     "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                payload = json.loads(resp.read())
            line = payload["content"][0]["text"].strip().splitlines()[0]
        except Exception as exc:
            self.errors.append(redact("%s: %s" % (type(exc).__name__, exc), self.key))
            raise
        return parse_line(line)


def parse_line(line: str) -> tuple:
    """`call: reason`, refusing anything outside the closed list."""
    if ":" not in line:
        raise calls_mod.CallError("expected `call: reason`, got %r" % line)
    call, _, reason = line.partition(":")
    call = call.strip().lower()
    if call not in calls_mod.CALLS:
        raise calls_mod.CallError("%r is not one of: %s"
                                  % (call, ", ".join(calls_mod.CALLS)))
    return call, reason.strip()


def make_call(caller: Caller, document: dict, previous: dict | None,
              core_holding: str, as_of: dt.date, masked: bool,
              log: calls_mod.CallLog) -> dict:
    """One call, recorded before it can be used. Returns the record.

    Returns None where no call should be made at all: a document older
    than five trading days is about a market that has already moved on.
    """
    doc_date = dt.date.fromisoformat(document["date"])
    if calls_mod.too_old(doc_date, as_of):
        return {"skipped": True,
                "why": "document is older than %d trading days"
                       % calls_mod.MAX_DOCUMENT_AGE_TRADING_DAYS,
                "document_date": document["date"]}

    current_text = document["text"]
    previous_text = (previous or {}).get("text", "")
    if masked:
        current_text = mask(current_text)
        previous_text = mask(previous_text)

    call, reason = caller.decide(current_text, previous_text, core_holding)
    record = calls_mod.Call(
        call=call, reason=reason[:200],
        made_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        model=caller.name, source_url=document["source_url"],
        document_date=document["date"],
        document_hash=calls_mod.document_hash(current_text),
        masked=masked, core_holding=core_holding)
    stored = log.record(record)
    return {"skipped": False, "record": stored, "call": record}
