"""An unresolved ticker must be reported, never dropped.

A run that silently loses one instrument still produces a full-looking
table, which is exactly how a missing symbol survives to production.
"""
import pandas as pd
import pytest

import settings
import ticker_check

CFG = settings.load()


def frame(n=10, close=100.0, blank=False):
    idx = pd.date_range("2026-10-01", periods=n, freq="D", tz="UTC")
    closes = [None] * n if blank else [close + i for i in range(n)]
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes,
                         "Close": closes, "Volume": [1000] * n}, index=idx)


def test_a_good_ticker_resolves(monkeypatch):
    monkeypatch.setattr(ticker_check.data_sources, "fetch_daily_history",
                        lambda t, period="1mo", timeout=None: frame())
    row = ticker_check.probe("RELIANCE.NS")
    assert row["ok"] and row["bars"] == 10
    assert row["last_close"] == 109.0 and row["last_date"] == "2026-10-10"


def test_an_empty_frame_is_reported_not_dropped(monkeypatch):
    monkeypatch.setattr(ticker_check.data_sources, "fetch_daily_history",
                        lambda t, period="1mo", timeout=None: pd.DataFrame())
    row = ticker_check.probe("NOSUCH.NS")
    assert not row["ok"] and row["reason"] == "no data returned"


def test_all_blank_closes_are_reported(monkeypatch):
    monkeypatch.setattr(ticker_check.data_sources, "fetch_daily_history",
                        lambda t, period="1mo", timeout=None: frame(blank=True))
    assert ticker_check.probe("X.NS")["reason"] == "every close was blank"


def test_too_few_bars_is_not_treated_as_resolved(monkeypatch):
    # One stray bar is how a delisted symbol looks. Calling that "resolved"
    # would put an instrument with no history into a backtest.
    monkeypatch.setattr(ticker_check.data_sources, "fetch_daily_history",
                        lambda t, period="1mo", timeout=None: frame(n=1))
    row = ticker_check.probe("THIN.NS")
    assert not row["ok"] and "too thin" in row["reason"]


def test_a_network_error_is_captured_not_raised(monkeypatch):
    def boom(t, period="1mo", timeout=None):
        raise ConnectionError("CONNECT tunnel failed")
    monkeypatch.setattr(ticker_check.data_sources, "fetch_daily_history", boom)
    row = ticker_check.probe("X.NS")
    assert not row["ok"] and "ConnectionError" in row["reason"]


def test_check_probes_every_ticker_exactly_once():
    seen = []

    def fake(t):
        seen.append(t)
        return {"ticker": t, "ok": True, "bars": 9, "last_close": 1.0,
                "last_date": "2026-10-10", "reason": ""}

    result = ticker_check.check(CFG, probe_fn=fake)
    assert seen == settings.all_tickers(CFG)
    assert len(seen) == len(set(seen))
    assert result["unresolved"] == []


def test_cash_is_excluded_and_said_so():
    result = ticker_check.check(CFG, probe_fn=lambda t: {
        "ticker": t, "ok": True, "bars": 9, "last_close": 1.0,
        "last_date": "2026-10-10", "reason": ""})
    assert settings.CASH not in [r["ticker"] for r in result["rows"]]
    assert result["excluded"] == [settings.CASH]
    assert "not a ticker" in ticker_check.report(result)


def test_the_report_shouts_about_unresolved_tickers():
    def fake(t):
        ok = t != "GOLDBEES.NS"
        return {"ticker": t, "ok": ok, "bars": 9 if ok else 0,
                "last_close": 1.0 if ok else None,
                "last_date": "2026-10-10" if ok else None,
                "reason": "" if ok else "no data returned"}

    text = ticker_check.report(ticker_check.check(CFG, probe_fn=fake))
    assert "UNRESOLVED: GOLDBEES.NS" in text
    assert "NOT dropped" in text


def test_every_probe_is_bounded_by_a_timeout(monkeypatch):
    """An unanswered request must not be able to eat the whole job.

    The first archive run spent fifteen minutes on one symbol and was
    killed by the step timeout having checked nothing, because the fetch
    had no deadline.
    """
    seen = {}

    def spy(ticker, period="1mo", timeout=None):
        seen["timeout"] = timeout
        return frame()

    monkeypatch.setattr(ticker_check.data_sources, "fetch_daily_history", spy)
    ticker_check.probe("RELIANCE.NS")
    assert seen["timeout"] == ticker_check.PROBE_TIMEOUT
    assert 0 < ticker_check.PROBE_TIMEOUT <= 60
