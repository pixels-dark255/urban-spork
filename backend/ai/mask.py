"""Hide when a document is from, and who wrote it.

This is the load-bearing part of the replay test. A model asked about an
RBI statement from March 2023 may not be reasoning about the statement at
all: it may be remembering what the market did next, because that is in
its training data. The masked replay is the attempt to take that away.

So masking is aggressive about anything that dates a document - dates,
years, month names, quarter and fiscal-year markers, meeting numbers -
and about the names that identify the institution and its officials. It
deliberately leaves the numbers that are the actual content: a repo rate
of 6.50% stays, because that is what the call is supposed to be about.

It cannot be perfect. A distinctive turn of phrase, an unusual policy or
a once-only event can still date a document to a reader who has seen it
before. That is exactly why the result is labelled "replay, not a clean
test" wherever it appears, rather than being presented as evidence.
"""
from __future__ import annotations

import re

MONTHS = ("january february march april may june july august september "
          "october november december").split()
MONTH_ABBR = [m[:3] for m in MONTHS]

# Institution and role names that place a document immediately.
NAMES = [
    "reserve bank of india", "reserve bank", "rbi",
    "monetary policy committee", "mpc",
    "securities and exchange board of india", "sebi",
    "government of india", "ministry of finance",
    "governor", "deputy governor", "chairman", "chairperson",
]

PLACEHOLDER = "[masked]"


def _month_pattern() -> str:
    return "|".join(MONTHS + MONTH_ABBR)


PATTERNS = [
    # 2024-03-15, 15/03/2024, 15-03-24
    (re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"), PLACEHOLDER),
    (re.compile(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b"), PLACEHOLDER),
    # 15 March 2024, March 15, 2024, March 2024
    (re.compile(r"\b\d{1,2}\s+(?:%s)\w*\s+\d{4}\b" % _month_pattern(), re.I), PLACEHOLDER),
    (re.compile(r"\b(?:%s)\w*\s+\d{1,2},?\s+\d{4}\b" % _month_pattern(), re.I), PLACEHOLDER),
    (re.compile(r"\b(?:%s)\w*\s+\d{4}\b" % _month_pattern(), re.I), PLACEHOLDER),
    # Any remaining month name on its own.
    (re.compile(r"\b(?:%s)\w*\b" % _month_pattern(), re.I), PLACEHOLDER),
    # FY24, FY2024, Q1 FY24, H1FY25
    (re.compile(r"\b[QH][1-4]?\s*FY\s*\d{2,4}\b", re.I), PLACEHOLDER),
    (re.compile(r"\bFY\s*\d{2,4}\b", re.I), PLACEHOLDER),
    (re.compile(r"\b[QH][1-4]\b"), PLACEHOLDER),
    # A meeting or resolution number dates a document as surely as a date.
    (re.compile(r"\b(?:meeting|resolution|statement)\s+(?:no\.?\s*)?\d+\b", re.I),
     PLACEHOLDER),
    # A fiscal span like 2024-25. Before bare years, or the first half is
    # masked and the trailing "-25" still dates the document.
    (re.compile(r"\b(?:19|20)\d{2}\s*[-/\u2013]\s*\d{2,4}\b"), PLACEHOLDER),
    # Bare years. Last, so the dated forms above match first.
    (re.compile(r"\b(?:19|20)\d{2}\b"), PLACEHOLDER),
]


def mask(text: str, extra_names: list[str] | None = None) -> str:
    """The document with its date and its authorship removed."""
    out = str(text)
    for pattern, replacement in PATTERNS:
        out = pattern.sub(replacement, out)
    for name in sorted(NAMES + [n.lower() for n in (extra_names or [])],
                       key=len, reverse=True):
        out = re.sub(r"\b%s\b" % re.escape(name), PLACEHOLDER, out, flags=re.I)
    # Collapse the runs of placeholders this leaves behind, which would
    # otherwise themselves be a fingerprint of how much was removed.
    out = re.sub(r"(?:\[masked\][\s,.;:-]*){2,}", PLACEHOLDER + " ", out)
    return out


def leaks(text: str) -> list[str]:
    """Anything still in the text that could date it. Used by the tests."""
    found = []
    for pattern in (re.compile(r"\b(?:19|20)\d{2}\b"),
                    re.compile(r"\b(?:%s)\b" % _month_pattern(), re.I)):
        found += pattern.findall(text)
    for name in NAMES:
        if re.search(r"\b%s\b" % re.escape(name), text, flags=re.I):
            found.append(name)
    return sorted(set(found))
