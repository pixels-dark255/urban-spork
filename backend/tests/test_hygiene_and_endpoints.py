"""Items 15-18, plus a pass over every endpoint.

The endpoint sweep is the automated form of "run the app and hit every
endpoint": it drives the real ASGI app with the network stubbed, so it can
run anywhere and is checked on every commit rather than once by hand.
"""
import json

import pytest

import storage


# --- item 16: dead code -----------------------------------------------------

def test_db_module_is_gone():
    import importlib

    with pytest.raises(ImportError):
        importlib.import_module("db")


def test_sqlalchemy_and_sklearn_are_not_imported_anywhere():
    import pathlib

    backend = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in backend.glob("*.py"):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped.startswith(("import ", "from ")):
                continue
            for banned in ("sqlalchemy", "sklearn"):
                if banned in stripped.lower():
                    offenders.append(f"{path.name}: {stripped}")
    assert not offenders, offenders


def test_requirements_install_on_modern_python():
    import pathlib

    text = (pathlib.Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
    # Requirement lines only - the file's own comment explains the old pins
    # and would otherwise match these assertions.
    requirements = [line.strip() for line in text.splitlines()
                    if line.strip() and not line.strip().startswith("#")]
    joined = "\n".join(requirements)

    # Exact pins on these two had no Python 3.13 wheels.
    assert "numpy==1.26.4" not in joined
    assert "pandas==2.2.3" not in joined
    assert "numpy>=1.26,<3" in joined
    assert "pandas>=2.2,<3" in joined
    # Removed with db.py.
    assert "sqlalchemy" not in joined.lower()
    assert "scikit-learn" not in joined


# --- item 18: no duplicate watchlist entries --------------------------------

def test_adding_the_same_symbol_and_horizon_twice_returns_the_same_item(isolated_storage):
    first = storage.add_item("1.1.1.1", "RELIANCE.NS", "Reliance", 1440)
    second = storage.add_item("1.1.1.1", "RELIANCE.NS", "Reliance", 1440)
    assert first["id"] == second["id"]
    assert len(storage.get_watchlist("1.1.1.1")) == 1


def test_the_same_symbol_at_a_different_horizon_is_a_separate_entry(isolated_storage):
    storage.add_item("1.1.1.1", "RELIANCE.NS", "Reliance", 1440)
    storage.add_item("1.1.1.1", "RELIANCE.NS", "Reliance", 15)
    assert len(storage.get_watchlist("1.1.1.1")) == 2


def test_duplicate_add_through_the_api_does_not_create_a_second_card(client):
    payload = {"symbol": "RELIANCE", "exchange": "NSE", "horizon": "1d"}
    first = client.post("/api/watchlist", json=payload).json()
    second = client.post("/api/watchlist", json=payload).json()
    assert first["id"] == second["id"]
    assert len(client.get("/api/watchlist").json()["watchlist"]) == 1


# --- item 18: scheduler starts once ----------------------------------------

def test_start_scheduler_is_idempotent(monkeypatch):
    import scheduler

    starts = []
    monkeypatch.setattr(scheduler.scheduler, "start", lambda: starts.append(1))
    monkeypatch.setattr(scheduler.scheduler, "add_job", lambda *a, **k: None)
    monkeypatch.setattr(type(scheduler.scheduler), "running", property(lambda self: False))
    scheduler.start_scheduler()
    assert len(starts) == 1

    monkeypatch.setattr(type(scheduler.scheduler), "running", property(lambda self: True))
    scheduler.start_scheduler()
    assert len(starts) == 1, "a second call must not start a second scheduler"


def test_app_uses_a_lifespan_handler_not_the_deprecated_hook():
    import pathlib

    text = (pathlib.Path(__file__).resolve().parent.parent / "main.py").read_text()
    # The decorator, not the word - the lifespan docstring names the hook it
    # replaced.
    assert '@app.on_event' not in text
    assert "lifespan=lifespan" in text
    assert "@asynccontextmanager" in text


# --- item 17: service worker ------------------------------------------------

def _sw_source():
    import pathlib
    return (pathlib.Path(__file__).resolve().parent.parent.parent
            / "frontend" / "sw.js").read_text()


def test_service_worker_only_caches_get():
    source = _sw_source()
    assert 'request.method !== "GET"' in source


def test_service_worker_does_not_cache_api_responses():
    assert 'url.pathname.startsWith("/api/")' in _sw_source()


def test_service_worker_cache_name_was_bumped():
    import re
    m = re.search(r"tickerboard-shell-v(\d+)", _sw_source())
    assert m and int(m.group(1)) >= 3   # bumped past the v2 that cached POSTs


# --- the endpoint sweep -----------------------------------------------------

def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_search(client):
    body = client.get("/api/stocks/search", params={"q": "reliance"}).json()
    assert "results" in body


@pytest.mark.parametrize("horizon", ["15m", "1d", "3mo"])
def test_analyze_every_horizon_class(client, horizon):
    response = client.get("/api/stocks/RELIANCE/analyze", params={"horizon": horizon})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["predicted_price"] is not None
    assert body["band_68"][0] < body["band_68"][1]
    # No NaN survived serialisation (Starlette would have raised).
    assert "NaN" not in response.text


def test_analyze_rejects_an_unknown_horizon(client):
    assert client.get("/api/stocks/RELIANCE/analyze",
                      params={"horizon": "7y"}).status_code == 400


def test_chart(client):
    response = client.get("/api/stocks/RELIANCE/chart", params={"horizon": "1d"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["historical"] and body["forecast"]
    # Forecast starts at "now", not 5.5 hours ago.
    assert body["forecast"][0]["time"] <= body["forecast"][-1]["time"]


def test_watchlist_lifecycle(client):
    created = client.post("/api/watchlist",
                          json={"symbol": "TCS", "exchange": "NSE", "horizon": "1d"}).json()
    item_id = created["id"]

    listing = client.get("/api/watchlist").json()["watchlist"]
    assert any(i["id"] == item_id for i in listing)
    card = next(i for i in listing if i["id"] == item_id)
    assert "last_price" in card and "last_price_at" in card

    assert client.get(f"/api/watchlist/{item_id}/analysis").status_code == 200
    assert client.get(f"/api/watchlist/{item_id}/detail").status_code == 200
    assert client.get(f"/api/watchlist/{item_id}/history").status_code == 200
    assert client.delete(f"/api/watchlist/{item_id}").status_code == 200
    assert client.get(f"/api/watchlist/{item_id}/detail").status_code == 404


def test_intraday_lifecycle(client):
    assert client.post("/api/intraday/stocks",
                       json={"symbol": "INFY", "exchange": "NSE"}).status_code == 200

    listing = client.get("/api/intraday/stocks").json()
    assert listing["stocks"]
    symbol = listing["stocks"][0]["symbol"]

    detail = client.get(f"/api/intraday/stocks/{symbol}/detail")
    assert detail.status_code == 200, detail.text
    assert "NaN" not in detail.text

    assert client.delete(f"/api/intraday/stocks/{symbol}").status_code == 200
    assert client.delete(f"/api/intraday/stocks/{symbol}").status_code == 404


def test_intraday_detail_on_strictly_rising_prices_returns_200(client, fake_yf):
    """RSI is exactly 100 here, which used to be NaN and 500 the request."""
    from conftest import make_frame

    fake_yf.set_default(make_frame(n=80, step=1.0))
    fake_yf.clear_cache()
    client.post("/api/intraday/stocks", json={"symbol": "WIPRO", "exchange": "NSE"})
    response = client.get("/api/intraday/stocks/WIPRO.NS/detail")
    assert response.status_code == 200, response.text
    assert "NaN" not in response.text


def test_market_status(client):
    assert "is_open" in client.get("/api/market-status").json()


def test_every_json_response_is_strict_json(client):
    """allow_nan=False is what Starlette uses; anything non-finite would have
    raised rather than reaching here, but parse explicitly too."""
    for path in ("/api/health", "/api/market-status", "/api/watchlist",
                 "/api/intraday/stocks"):
        response = client.get(path)
        assert response.status_code == 200, path
        json.loads(response.text)
