"""Accumulating intraday history: the pure parts, without a database.

Yahoo's 60-day intraday window slides, so a backtest run next month covers
different sessions than one run today. These tests cover the rules that
decide what gets stored and when a bar size has earned its way in.
"""
import datetime

import pandas as pd
import pytest

import bar_archive
import settings

CFG = settings.load()


def frame(closes, tz="UTC"):
    idx = pd.date_range("2026-11-20 09:15", periods=len(closes), freq="5min", tz=tz)
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes,
                         "Close": closes, "Volume": [100] * len(closes)}, index=idx)


# ------------------------------------------------------------ what is stored

def test_bars_become_rows_in_order():
    rows = bar_archive._rows("TCS.NS", "5m", frame([10.0, 11.0, 12.0]))
    assert len(rows) == 3
    assert [r[0] for r in rows] == ["TCS.NS"] * 3
    assert [r[1] for r in rows] == ["5m"] * 3
    assert [r[6] for r in rows] == [10.0, 11.0, 12.0]


def test_a_bar_with_no_close_is_skipped_not_zeroed():
    # A blank close means Yahoo had nothing for that minute. Storing 0.0
    # would invent a price that never traded, and a strategy would happily
    # buy the crash.
    rows = bar_archive._rows("TCS.NS", "5m", frame([10.0, float("nan"), 12.0]))
    assert [r[6] for r in rows] == [10.0, 12.0]


def test_a_naive_timestamp_is_treated_as_utc():
    rows = bar_archive._rows("TCS.NS", "5m", frame([10.0], tz=None))
    assert rows[0][2].tzinfo is not None


def test_an_empty_frame_writes_nothing():
    assert bar_archive._rows("TCS.NS", "5m", pd.DataFrame()) == []


def test_missing_volume_becomes_null_not_zero():
    f = frame([10.0])
    f["Volume"] = [float("nan")]
    assert bar_archive._rows("TCS.NS", "5m", f)[0][7] is None


# --------------------------------------------------------------- coverage

def test_months_between_counts_in_months():
    a = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
    b = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    assert bar_archive.months_between(a, b) == pytest.approx(12.0, abs=0.1)


def test_months_between_handles_an_empty_table():
    assert bar_archive.months_between(None, None) == 0.0


# -------------------------------------------------------------- the funnel

def test_a_bar_size_with_long_source_history_is_eligible_at_once():
    out = bar_archive.eligible(CFG, {})
    assert out["1d"]["eligible"] and out["1h"]["eligible"]


def test_an_unproven_bar_size_stays_out_until_enough_is_stored():
    out = bar_archive.eligible(CFG, {"5m": {"months": 2.0}})
    assert not out["5m"]["eligible"]
    assert "2.0 of 12 months" in out["5m"]["why"]


def test_an_unproven_bar_size_qualifies_once_the_history_is_there():
    out = bar_archive.eligible(CFG, {"5m": {"months": 12.5}})
    assert out["5m"]["eligible"]


def test_exactly_the_threshold_qualifies():
    out = bar_archive.eligible(CFG, {"5m": {"months": 12.0}})
    assert out["5m"]["eligible"]


def test_a_bar_size_never_archived_is_not_eligible():
    out = bar_archive.eligible(CFG, {})
    assert not out["5m"]["eligible"] and not out["15m"]["eligible"]


def test_every_configured_bar_size_gets_a_verdict():
    out = bar_archive.eligible(CFG, {})
    assert set(out) == set(CFG["bar_sizes"])


# ------------------------------------------------------- refusing to lose data

def test_archiving_without_a_database_refuses_loudly(monkeypatch):
    # Writing to the JSON file that gets wiped would defeat the entire
    # point of the job, so it must fail rather than appear to work.
    monkeypatch.setattr(bar_archive.storage, "_pg_pool", None)
    with pytest.raises(bar_archive.NoDatabase, match="nowhere durable"):
        bar_archive.ensure_table()
