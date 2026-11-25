"""Telegram alerts, and the rules about the credentials they need.

The token and chat id come from the environment, which a human fills in
through `.env`. AGENTS.md §1.1 is absolute about this: the file is never
read by anything but python-dotenv, never printed and never logged. So:

  * the token is never put in a log line, a run summary or an exception;
  * `redact()` scrubs it from anything on its way to a log, because the
    Telegram API puts the token in the URL and a failed request would
    otherwise print it in the traceback;
  * with no credentials configured the sink is silent rather than broken.
    An unconfigured alerter must not be able to stop a run.

Every message is deduplicated on a key, so a rerun of the same session
does not fire the same alert twice. Repeated alerts are how the one that
matters gets skimmed past.
"""
from __future__ import annotations

import os
import re
import urllib.parse
import urllib.request

TOKEN_VAR = "TELEGRAM_BOT_TOKEN"
CHAT_VAR = "TELEGRAM_CHAT_ID"
TIMEOUT = 10


def redact(text, token: str | None = None) -> str:
    """Remove the bot token from anything about to be written down."""
    out = str(text)
    token = token or os.environ.get(TOKEN_VAR, "")
    if token:
        out = out.replace(token, "<redacted>")
    # Belt and braces: a Telegram token looks like 123456789:AA... and the
    # one in the environment may not be the one in the string.
    return re.sub(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b", "<redacted>", out)


class Sink:
    """Base: records what would be sent. Used by dry runs and tests."""

    def __init__(self, store=None):
        self.store = store
        self.sent: list[dict] = []
        self.skipped: list[dict] = []

    def configured(self) -> bool:
        return True

    def send(self, key: str, text: str) -> bool:
        if self.store is not None and not self.store.mark_alert(key):
            self.skipped.append({"key": key, "why": "already sent"})
            return False
        self.sent.append({"key": key, "text": text})
        return self._deliver(text)

    def _deliver(self, text: str) -> bool:
        return True


class NullSink(Sink):
    """Nothing configured. Records, delivers nothing, never raises."""

    def configured(self) -> bool:
        return False


class TelegramSink(Sink):
    def __init__(self, store=None, token: str | None = None,
                 chat_id: str | None = None):
        super().__init__(store)
        self.token = token or os.environ.get(TOKEN_VAR, "")
        self.chat_id = chat_id or os.environ.get(CHAT_VAR, "")
        self.errors: list[str] = []

    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _deliver(self, text: str) -> bool:
        if not self.configured():
            return False
        url = "https://api.telegram.org/bot%s/sendMessage" % self.token
        data = urllib.parse.urlencode(
            {"chat_id": self.chat_id, "text": text,
             "disable_web_page_preview": "true"}).encode()
        try:
            with urllib.request.urlopen(url, data=data, timeout=TIMEOUT) as resp:
                return 200 <= resp.status < 300
        except Exception as exc:
            # The token is in the URL, so an unredacted exception would
            # leak it into whatever catches this.
            self.errors.append(redact("%s: %s" % (type(exc).__name__, exc),
                                      self.token))
            return False


def open_sink(store=None) -> Sink:
    sink = TelegramSink(store)
    return sink if sink.configured() else NullSink(store)


# ----------------------------------------------------------- messages

def failure(run_id: str, step: str, why: str) -> tuple:
    return ("fail|%s|%s" % (run_id, step),
            "TickerBoard: run stopped at %s.\n%s" % (step, redact(why)))


def eod_summary(day, regime: dict, core: dict, sleeves: list, intraday: dict) -> tuple:
    lines = ["TickerBoard end of day %s" % day,
             "Regime: trend %s, volatility %s"
             % (regime.get("trend", "?"), regime.get("vol", "?")),
             "Core: %s" % (core.get("holding") or "nothing")]
    if sleeves:
        for s in sleeves:
            lines.append("Sleeve %s: %s" % (s.get("id"), s.get("note", "")))
    else:
        lines.append("Sleeves: none (no survivor has earned a slot)")
    lines.append("Intraday P&L: %s" % intraday.get("net", "n/a"))
    return ("eod|%s" % day, "\n".join(lines))


def missed_run(day, deadline: str) -> tuple:
    return ("missed|%s" % day,
            "TickerBoard: no end-of-day run recorded by %s IST on %s."
            % (deadline, day))
