"""Validation for the shipped NSE holiday list.

The dates could not be fetched from nseindia.com (blocked egress), so they
were transcribed from secondary sources. These checks are what stands in for
reading the official circular: they are mechanical, they caught a bad 2027
list that turned out to be a festival calendar rather than a trading one,
and they run on every commit so a future edit gets the same scrutiny.

They cannot prove a date is right. They can prove a list is wrong.
"""
import datetime as dt
import json
import pathlib

import pytest

import market_calendar

HOLIDAY_FILE = pathlib.Path(market_calendar.HOLIDAY_FILE)


def _payload():
    return json.loads(HOLIDAY_FILE.read_text())


def _entries(year):
    return _payload()["holidays"].get(str(year), [])


def test_file_is_valid_json_with_the_expected_shape():
    payload = _payload()
    assert isinstance(payload.get("holidays"), dict)
    for year, entries in payload["holidays"].items():
        assert year.isdigit() and len(year) == 4
        assert isinstance(entries, list)


def test_every_date_parses_and_is_iso():
    for year, entries in _payload()["holidays"].items():
        for entry in entries:
            parsed = dt.date.fromisoformat(entry["date"])
            assert parsed.isoformat() == entry["date"]
            assert str(parsed.year) == year, f"{entry['date']} filed under {year}"


def test_no_holiday_falls_on_a_weekend():
    """The check that caught the bad 2027 list: 5 of its 14 dates were a
    Saturday or Sunday. NSE does not publish weekend dates as trading
    holidays - the market is already shut - so a weekend entry means the
    list is a festival calendar, not a trading calendar."""
    offenders = []
    for entries in _payload()["holidays"].values():
        for entry in entries:
            day = dt.date.fromisoformat(entry["date"])
            if day.weekday() >= 5:
                offenders.append(f"{entry['date']} ({entry['description']}) "
                                 f"is a {day.strftime('%A')}")
    assert not offenders, "weekend dates in the holiday list: " + "; ".join(offenders)


def test_no_duplicate_dates():
    for year, entries in _payload()["holidays"].items():
        dates = [e["date"] for e in entries]
        duplicates = {d for d in dates if dates.count(d) > 1}
        assert not duplicates, f"{year} lists {duplicates} more than once"


def test_every_entry_has_a_description():
    for entries in _payload()["holidays"].values():
        for entry in entries:
            assert entry.get("description", "").strip(), entry


def test_dates_are_in_ascending_order():
    """Not correctness as such, but an out-of-order date is a reliable sign
    the list was edited carelessly."""
    for year, entries in _payload()["holidays"].items():
        dates = [e["date"] for e in entries]
        assert dates == sorted(dates), f"{year} is not in date order"


def test_a_populated_year_has_a_plausible_count():
    """NSE closes somewhere in the mid-teens of weekdays a year. A list with
    three entries is truncated; one with forty is not a trading calendar."""
    for year, entries in _payload()["holidays"].items():
        if entries:
            assert 8 <= len(entries) <= 25, f"{year} has {len(entries)} holidays"


def test_the_source_and_updated_fields_are_filled_in():
    """Provenance matters more than usual here, because these were not read
    from NSE directly."""
    payload = _payload()
    assert payload.get("source", "").strip()
    assert payload.get("updated", "").strip()


# --- the loader honours the list --------------------------------------------

def test_2026_is_loaded():
    assert market_calendar.holidays_loaded_for(2026)
    assert len(_entries(2026)) >= 8


def test_2027_is_not_claimed_as_known():
    """Left empty on purpose - see the note in nse_holidays.json. The app
    must report that it does not know 2027 rather than implying every
    weekday is a trading day."""
    assert _entries(2027) == []
    assert not market_calendar.holidays_loaded_for(2027)


@pytest.mark.parametrize("date_text,name", [
    ("2026-01-26", "Republic Day"),
    ("2026-03-03", "Holi"),
    ("2026-10-02", "Mahatma Gandhi Jayanti"),
    ("2026-12-25", "Christmas"),
])
def test_known_holidays_close_the_market(date_text, name):
    day = dt.date.fromisoformat(date_text)
    assert market_calendar.is_trading_holiday(day)
    assert not market_calendar.is_trading_day(day)

    # Mid-session on a holiday is still closed.
    moment = dt.datetime(day.year, day.month, day.day, 11, 0,
                         tzinfo=market_calendar.IST)
    assert not market_calendar.is_market_open(moment)
    status = market_calendar.market_status(moment)
    assert status["reason"] == "holiday"
    assert name.split()[0] in status["holiday_name"]


def test_an_ordinary_weekday_is_still_open():
    moment = dt.datetime(2026, 3, 4, 11, 0, tzinfo=market_calendar.IST)  # Wed
    assert market_calendar.is_market_open(moment)


def test_next_trading_day_skips_a_holiday():
    # 2026-01-26 is a Monday holiday, so Friday rolls to Tuesday the 27th.
    assert market_calendar.next_trading_day(dt.date(2026, 1, 23)) == dt.date(2026, 1, 27)


def test_christmas_eve_2026_is_a_normal_trading_day():
    """Guards against over-eager holiday entry: only the published dates."""
    assert market_calendar.is_trading_day(dt.date(2026, 12, 24))


# --- the loader rejects a bad list ------------------------------------------

def test_loader_drops_weekend_entries(tmp_path, monkeypatch):
    """A weekend date in the file must not load, so a festival calendar
    cannot be mistaken for a trading calendar."""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "source": "test", "updated": "test",
        "holidays": {"2027": [
            {"date": "2027-03-06", "description": "Saturday festival"},
            {"date": "2027-03-22", "description": "Holi (Monday)"},
        ]},
    }))
    monkeypatch.setattr(market_calendar, "HOLIDAY_FILE", str(bad))
    market_calendar.reload_holidays()
    try:
        assert not market_calendar.is_trading_holiday(dt.date(2027, 3, 6))
        assert market_calendar.is_trading_holiday(dt.date(2027, 3, 22))
    finally:
        monkeypatch.undo()
        market_calendar.reload_holidays()


def test_loader_survives_a_corrupt_file(tmp_path, monkeypatch):
    bad = tmp_path / "corrupt.json"
    bad.write_text("{ not json at all")
    monkeypatch.setattr(market_calendar, "HOLIDAY_FILE", str(bad))
    try:
        market_calendar.reload_holidays()
        # Degrades to weekends-and-hours rather than taking the app down.
        assert market_calendar.market_status()["holidays"]["holiday_count"] == 0
    finally:
        monkeypatch.undo()
        market_calendar.reload_holidays()


def test_reload_restores_the_real_list():
    """The tests above swap the file out; make sure the suite leaves the
    module holding the shipped list."""
    market_calendar.reload_holidays()
    assert market_calendar.is_trading_holiday(dt.date(2026, 12, 25))
