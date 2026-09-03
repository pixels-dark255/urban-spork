"""API-level tests. The scheduler and the universe refresh are stubbed out:
a test suite must not start background jobs or reach the network."""
import pytest
from fastapi.testclient import TestClient

from conftest import make_bars, store_bars


@pytest.fixture(scope="module")
def client():
    import scheduler
    import universe
    scheduler.start_scheduler = lambda *a, **k: None
    universe.refresh_universe = lambda *a, **k: {"refreshed": False, "reason": "stubbed in tests"}
    import main
    main.start_scheduler = lambda *a, **k: None
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    class DeadProvider:
        name = "offline"
        def get_bars(self, *a, **k):
            raise ConnectionError("offline in tests")
        def get_quote(self, *a, **k):
            return None
    monkeypatch.setattr("providers.get_provider", lambda *a, **k: DeadProvider())
    monkeypatch.setattr("market_overview.market_context",
                        lambda: {"available": False, "reason": "offline in tests"})


HEADERS = {"X-Client-Id": "apitests"}


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_search_endpoint_returns_matches(client):
    body = client.get("/api/stocks/search", params={"q": "reli"}).json()
    assert any(r["symbol"] == "RELIANCE" for r in body["results"])
    assert body["universe_size"] > 0


def test_timeframes_endpoint_lists_every_supported_timeframe(client):
    labels = [t["label"] for t in client.get("/api/timeframes").json()["timeframes"]]
    assert labels == ["30s", "1m", "2m", "5m", "10m", "15m", "30m", "1h", "1.5h", "2h"]


def test_platform_status_reports_providers_and_universe(client):
    body = client.get("/api/platform/status").json()
    names = {p["name"] for p in body["providers"]}
    assert {"yahoo", "kotak_neo"} <= names
    # The broker stub must never report itself usable without credentials -
    # that is what keeps the engine on Yahoo instead of a half-wired broker.
    kotak = next(p for p in body["providers"] if p["name"] == "kotak_neo")
    assert kotak["available"] is False
    assert body["universe"]["total_stocks"] > 0
    assert body["ml_backends"]


def test_analyze_endpoint_returns_a_full_recommendation(client):
    store_bars("APITREND", make_bars(n=420, trend=120, noise=0.3, volume_trend=True, seed=50))
    body = client.get("/api/intraday/analyze",
                      params={"symbol": "APITREND", "timeframe": "5m", "record": "false"},
                      headers=HEADERS).json()
    assert body["recommendation"] in ("BUY", "STRONG_BUY", "HOLD", "SELL", "STRONG_SELL", "NO_TRADE")
    for key in ("confidence", "indicators", "regime", "components", "explanation", "chart"):
        assert key in body


def test_analyze_rejects_an_unknown_timeframe(client):
    assert client.get("/api/intraday/analyze",
                      params={"symbol": "APITREND", "timeframe": "7m"}).status_code == 400


def test_settings_round_trip(client):
    saved = client.post("/api/settings", json={"capital": 250000, "risk_per_trade_pct": 0.75},
                        headers=HEADERS).json()["settings"]
    assert saved["capital"] == 250000
    assert client.get("/api/settings", headers=HEADERS).json()["settings"]["risk_per_trade_pct"] == 0.75


def test_settings_are_clamped_to_survivable_values(client):
    saved = client.post("/api/settings", json={"risk_per_trade_pct": 500},
                        headers=HEADERS).json()["settings"]
    assert saved["risk_per_trade_pct"] <= 10.0


def test_paper_open_refuses_when_there_is_no_tradeable_plan(client):
    store_bars("APIFLAT", make_bars(n=420, noise=2.5, ranging=True, seed=51))
    response = client.post("/api/paper/open", json={"symbol": "APIFLAT", "timeframe": "5m"},
                           headers=HEADERS)
    assert response.status_code == 400
    assert response.json()["detail"]


def test_paper_positions_and_summary(client):
    body = client.get("/api/paper/positions", headers=HEADERS).json()
    assert "open" in body and "closed" in body and "summary" in body
    assert "expectancy_per_trade" in body["summary"]


def test_predictions_and_accuracy_endpoints(client):
    assert "predictions" in client.get("/api/predictions", headers=HEADERS).json()
    assert "resolved" in client.post("/api/predictions/resolve").json()
    accuracy = client.get("/api/predictions/accuracy", headers=HEADERS).json()
    assert "resolved_predictions" in accuracy


def test_market_overview_degrades_gracefully_when_offline(client):
    body = client.get("/api/market/overview").json()
    assert body["stale"] is True
    assert body["stale_note"]


def test_models_endpoint(client):
    body = client.get("/api/models").json()
    assert "random_forest" in body["backends"]


def test_data_status_endpoint(client):
    assert "total_bars" in client.get("/api/data/status").json()
