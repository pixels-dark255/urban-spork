"""The compliance table must describe the code that actually exists.

Each gap below is pinned to the real source. Fixing a gap breaks its test,
which forces system_map.py and AGENTS.md to be updated in the same change.
That is deliberate: a stale compliance table is believed, and a believed
wrong statement about look-ahead bias is how a bad strategy ships.
"""
import pathlib

import pytest

import system_map
from arena import costs, engine

BACKEND = pathlib.Path(__file__).resolve().parent.parent
ENGINE_SRC = (BACKEND / "arena" / "engine.py").read_text()


def _rule(rule_id):
    for r in system_map.RULES:
        if r["id"] == rule_id:
            return r
    raise AssertionError("no such rule: %s" % rule_id)


# ----------------------------------------------------------- the gaps

def test_entries_do_not_fill_on_the_signal_bar():
    """The §1.3 gap is closed, and this now guards the fix.

    It used to assert the opposite. Closing the gap failed this test,
    which is what forced system_map.py and AGENTS.md to be updated in the
    same change rather than drifting behind the code.
    """
    assert 'fill_price(entry_leg, float(row["Close"])' not in ENGINE_SRC, (
        "an entry is filling at the signal bar's close again")
    assert "_park(state, book[\"id\"], sym, {\"kind\": \"entry\"" in ENGINE_SRC
    assert _rule("next-open-fills")["status"] == system_map.OK


def test_every_result_is_also_reported_at_double_cost():
    assert 2.0 in engine.SLIPPAGE_MULTIPLES
    assert _rule("double-cost")["status"] == system_map.OK


def test_the_funnel_reads_the_engine_sweep_rather_than_its_own_number():
    from funnel import gates
    assert gates.DOUBLE_COST == max(engine.SLIPPAGE_MULTIPLES)


# --------------------------------------------------- the rules that hold

def test_backtest_uses_the_live_code_path():
    """replay() must keep driving the same functions as the live runner."""
    body = ENGINE_SRC.split("def replay(", 1)[1].split("\ndef ", 1)[0]
    for call in ("start_day(state", "process_bars(state", "settle_day(state"):
        assert call in body, (
            "replay() no longer calls %s. Backtest and live must share one "
            "code path (AGENTS.md §1.2)." % call)
    assert _rule("one-signal-module")["status"] == system_map.OK


def test_every_indian_cost_leg_is_charged():
    for leg in ("brokerage_pct", "stt_sell_pct", "exchange_pct",
                "sebi_per_crore", "stamp_buy_pct", "gst_pct", "slippage_pct"):
        assert leg in costs.DEFAULT_COSTS, "missing cost leg: %s" % leg
    assert all(costs.DEFAULT_COSTS[k] > 0 for k in
               ("brokerage_pct", "stt_sell_pct", "slippage_pct"))
    assert _rule("costs-on")["status"] == system_map.OK


def test_nothing_can_place_a_live_order():
    """Paper only. A module that talks to a broker must not appear."""
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        text = path.read_text()
        for needle in ("place_order", "place_stock_order", "submit_order",
                       "kiteconnect", "alpaca", "import ccxt"):
            if needle in text:
                offenders.append("%s: %s" % (path.name, needle))
    assert not offenders, "live-order code found: %s" % offenders
    assert _rule("paper-only")["status"] == system_map.OK


# ------------------------------------------------------- the map itself

def test_every_listed_module_exists():
    root = BACKEND.parent
    for entry in system_map.MODULES:
        for mod in entry["modules"]:
            name = mod.split("::", 1)[0]
            candidates = [BACKEND / name, root / name]
            assert any(c.exists() for c in candidates), (
                "%s is listed under %r but does not exist"
                % (mod, entry["role"]))


def test_compliance_counts_match_the_rules():
    c = system_map.compliance()
    assert c["ok"] + c["gap"] == c["total"] == len(system_map.RULES)


@pytest.mark.parametrize("rule", system_map.RULES, ids=lambda r: r["id"])
def test_each_rule_is_well_formed(rule):
    assert rule["status"] in (system_map.OK, system_map.GAP)
    assert rule["detail"].strip() and rule["rule"].strip()


# ---------------------------------------------------------- the endpoint

def test_system_endpoint_serves_the_map(client):
    res = client.get("/api/system")
    assert res.status_code == 200
    body = res.json()
    assert body["compliance"]["total"] == len(system_map.RULES)
    assert {r["id"] for r in body["rules"]} == {r["id"] for r in system_map.RULES}
    assert any(e["role"] == "Signals" for e in body["modules"])


def test_system_endpoint_is_read_only(client):
    """It reports; it must not be a way to change anything."""
    assert client.post("/api/system").status_code in (404, 405)
