"""The settings block is the one place phases read their numbers from.

A parser that quietly misreads it is worse than one that crashes: every
later phase would run on numbers nobody chose.
"""
import datetime
import textwrap

import pytest

import settings

TODAY = datetime.date(2026, 11, 24)


def block(**overrides) -> str:
    base = {
        "market": "NSE",
        "timezone": "Asia/Kolkata",
        "session": "09:15-15:30 IST, intraday square-off by 15:15",
        "bar_sizes": "[1d, 1h, 15m, 5m]",
        "default_bar_size": "1d",
        "study_tickers": "[RELIANCE.NS, TCS.NS]",
        "engine_candidates": "[NIFTYBEES.NS]",
        "safe_candidates": "[GOLDBEES.NS, CASH]",
        "daily_capital_inr": "10000",
        "swing_capital_inr": "100000",
        "train_window_daily": "2022-01-01 .. 2024-12-31",
        "locked_window_daily": "2025-01-01 .. today",
        "cost_model": "existing Indian cost legs + slippage; stress at 2x",
        "kill_line_multiple": "2.0",
        "hourly_train_fraction": "0.70",
        "unproven_bar_sizes": "[15m, 5m]",
        "min_months_for_funnel": "12",
    }
    base.update({k: v for k, v in overrides.items() if v is not None})
    for k, v in overrides.items():
        if v is None:
            base.pop(k, None)
    return "\n".join("%s: %s" % (k, v) for k, v in base.items())


def load(**overrides):
    return settings._load_from_block(block(**overrides), TODAY)


# ------------------------------------------------------------- parsing

def test_the_real_agents_file_parses():
    cfg = settings.load(today=TODAY)
    assert cfg["market"] == "NSE"
    assert cfg["session_open"] == "09:15"
    assert cfg["session_close"] == "15:30"
    assert cfg["square_off"] == "15:15"


def test_a_list_may_wrap_over_several_lines():
    text = textwrap.dedent("""
        study_tickers: [RELIANCE.NS, TCS.NS,
                        INFY.NS,
                        SBIN.NS]
        market: NSE
    """)
    out = settings.parse(text)
    assert out["study_tickers"] == ["RELIANCE.NS", "TCS.NS", "INFY.NS", "SBIN.NS"]
    assert out["market"] == "NSE"


def test_trailing_comments_are_stripped():
    out = settings.parse("daily_capital_inr: 10000   # intraday budget")
    assert out["daily_capital_inr"] == 10000


def test_a_hash_starts_a_comment_only_after_whitespace():
    # Same rule as YAML: " #" begins a comment, a bare '#' inside a word
    # does not. Without the second half, a value like a colour or a tag
    # would lose everything from the hash onwards.
    assert settings.parse("note: issue #42 is open")["note"] == "issue"
    assert settings.parse("note: issue#42")["note"] == "issue#42"


def test_a_duplicate_key_is_an_error():
    # Two values for one setting means one of them is silently ignored.
    with pytest.raises(settings.SettingsError, match="twice"):
        settings.parse("market: NSE\nmarket: BSE")


def test_an_unclosed_list_is_an_error():
    with pytest.raises(settings.SettingsError, match="never closed"):
        settings.parse("study_tickers: [RELIANCE.NS,\n  TCS.NS")


def test_a_line_without_a_colon_is_an_error():
    with pytest.raises(settings.SettingsError, match="no 'key: value'"):
        settings.parse("market NSE")


def test_numbers_keep_their_type():
    out = settings.parse("a: 12\nb: 2.5\nc: text")
    assert out["a"] == 12 and isinstance(out["a"], int)
    assert out["b"] == 2.5 and isinstance(out["b"], float)
    assert out["c"] == "text"


# ---------------------------------------------------------- validation

def test_a_missing_key_is_named():
    with pytest.raises(settings.SettingsError, match="kill_line_multiple"):
        load(kill_line_multiple=None)


def test_a_locked_window_overlapping_training_is_rejected():
    # This is the whole point of a locked window. Overlap makes it a
    # second training run wearing a test's clothes.
    with pytest.raises(settings.SettingsError, match="proves nothing"):
        load(locked_window_daily="2024-06-01 .. today")


def test_a_backwards_window_is_rejected():
    with pytest.raises(settings.SettingsError, match="ends before it starts"):
        load(train_window_daily="2024-12-31 .. 2022-01-01")


def test_today_resolves_to_the_date_given():
    cfg = load()
    assert cfg["locked_window_daily"][1] == TODAY


def test_a_malformed_session_is_rejected():
    with pytest.raises(settings.SettingsError, match="session must read"):
        load(session="all day")


def test_hourly_fraction_must_be_a_fraction():
    with pytest.raises(settings.SettingsError, match="between 0 and 1"):
        load(hourly_train_fraction="1.5")


def test_default_bar_size_must_be_a_known_bar_size():
    with pytest.raises(settings.SettingsError, match="not in bar_sizes"):
        load(default_bar_size="4h")


def test_unproven_must_name_a_known_bar_size():
    with pytest.raises(settings.SettingsError, match="not in bar_sizes"):
        load(unproven_bar_sizes="[3m]")


# -------------------------------------------------------------- the view

def test_cash_is_never_offered_to_a_data_source():
    cfg = load()
    assert settings.CASH in cfg["safe_candidates"]
    assert settings.CASH not in settings.all_tickers(cfg)


def test_tickers_are_deduplicated_and_sorted():
    cfg = load(study_tickers="[TCS.NS, RELIANCE.NS]",
               engine_candidates="[TCS.NS]",
               safe_candidates="[CASH]")
    assert settings.all_tickers(cfg) == ["RELIANCE.NS", "TCS.NS"]


def test_unproven_bar_sizes_are_flagged():
    cfg = load()
    assert settings.is_unproven(cfg, "5m")
    assert settings.is_unproven(cfg, "15m")
    assert not settings.is_unproven(cfg, "1d")
    assert not settings.is_unproven(cfg, "1h")


def test_fingerprint_changes_when_a_number_changes():
    import fingerprint as fp
    a = fp.fingerprint(settings.fingerprint_values(load()))
    b = fp.fingerprint(settings.fingerprint_values(load(daily_capital_inr="20000")))
    assert a != b


def test_fingerprint_is_stable_for_the_same_settings():
    import fingerprint as fp
    assert (fp.fingerprint(settings.fingerprint_values(load()))
            == fp.fingerprint(settings.fingerprint_values(load())))


def test_report_names_every_section():
    text = settings.report(load())
    for needle in ("market", "session", "train window", "locked window",
                   "kill line", "study tickers"):
        assert needle in text
