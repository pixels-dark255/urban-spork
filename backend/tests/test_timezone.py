"""Item 3: the 5.5-hour timezone bug.

These tests are the reason conftest stamps its synthetic frames in IST: the
bug was invisible on a UTC machine and only appeared once the process ran
with TZ=Asia/Kolkata, which is where the app actually runs.
"""
import datetime as dt
import math
import os
import subprocess
import sys
import textwrap

import timeutil


def test_utc_now_is_timezone_aware():
    now = timeutil.utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == dt.timedelta(0)


def test_iso_now_carries_an_offset():
    text = timeutil.iso_now()
    assert text.endswith("+00:00")


def test_parse_utc_reads_old_naive_strings_as_utc():
    """Everything written before this fix is naive UTC. Reading it as local
    time is exactly the 19800-second error."""
    parsed = timeutil.parse_utc("2026-03-03T09:45:00")
    assert parsed == dt.datetime(2026, 3, 3, 9, 45, tzinfo=dt.timezone.utc)


def test_parse_utc_reads_new_offset_strings():
    assert timeutil.parse_utc("2026-03-03T09:45:00+00:00") == \
        dt.datetime(2026, 3, 3, 9, 45, tzinfo=dt.timezone.utc)


def test_parse_utc_normalises_a_non_utc_offset():
    assert timeutil.parse_utc("2026-03-03T15:15:00+05:30") == \
        dt.datetime(2026, 3, 3, 9, 45, tzinfo=dt.timezone.utc)


def test_parse_utc_accepts_z_suffix():
    assert timeutil.parse_utc("2026-03-03T09:45:00Z") == \
        dt.datetime(2026, 3, 3, 9, 45, tzinfo=dt.timezone.utc)


def test_parse_utc_returns_none_for_junk():
    for value in (None, "", "not-a-time", 42, {"a": 1}):
        assert timeutil.parse_utc(value) is None


def test_mixed_format_comparison_is_correct():
    """The old code compared ISO strings. Lexicographically,
    '2026-03-03T10:00:00+00:00' > '2026-03-03T10:00:00', so an old row and a
    new row describing the same instant compared unequal - and a naive
    string could sort after a later offset-carrying one."""
    old_naive = "2026-03-03T10:00:00"
    new_offset = "2026-03-03T10:00:00+00:00"
    assert timeutil.is_before_or_equal(old_naive, new_offset)
    assert timeutil.is_before_or_equal(new_offset, old_naive)
    assert timeutil.is_before_or_equal("2026-03-03T09:00:00", new_offset)
    assert not timeutil.is_before_or_equal("2026-03-03T11:00:00", new_offset)


def test_unparseable_timestamp_is_not_treated_as_due():
    assert not timeutil.is_before_or_equal("garbage", timeutil.iso_now())


def test_epoch_seconds_ignores_local_timezone():
    moment = dt.datetime(2026, 3, 3, 9, 45, tzinfo=dt.timezone.utc)
    assert timeutil.epoch_seconds(moment) == 1772531100


GBM_SCRIPT = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, %r)
    import predictor, timeutil
    path = predictor.gbm_path(100.0, 0.1, 0.2, 60)
    drift = path[0]["time"] - int(timeutil.utc_now().timestamp())
    print(drift)
    """
)


def test_gbm_path_starts_at_now_under_ist():
    """The headline symptom: on an IST machine the forecast chart started
    19800 seconds (5h30m) in the past, because .timestamp() read the naive
    datetime as local time. Run in a subprocess so TZ is genuinely applied
    at interpreter start."""
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "TZ": "Asia/Kolkata"}
    result = subprocess.run(
        [sys.executable, "-c", GBM_SCRIPT % backend],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    drift = int(result.stdout.strip().splitlines()[-1])
    assert abs(drift) < 5, f"forecast path starts {drift}s away from now"


def test_gbm_path_times_increase_monotonically():
    import predictor
    path = predictor.gbm_path(100.0, 0.1, 0.2, 240)
    times = [p["time"] for p in path]
    assert times == sorted(times)
    assert times[-1] - times[0] == 240 * 60
