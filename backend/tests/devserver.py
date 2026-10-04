"""Dev launcher: serves the real app with synthetic market data.

Yahoo is unreachable from this sandbox, so this patches the single network
chokepoint (data_sources._yf_download) before importing main, letting the
whole PWA - charts, markers, IST axes - be driven in a browser without a
live data source. Not imported by the app; it is a tool.

    TZ=Asia/Kolkata python tests/devserver.py --port 8131
"""
import argparse
import datetime as dt
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

IST = "Asia/Kolkata"


def _session_start(days_ago: int = 0) -> pd.Timestamp:
    day = dt.date.today() - dt.timedelta(days=days_ago)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return pd.Timestamp(f"{day.isoformat()} 09:15", tz=IST)


def synthetic(period: str, interval: str) -> pd.DataFrame:
    rng = np.random.default_rng(7)

    if interval in ("1d", "1wk"):
        n = 180
        index = pd.date_range(end=_session_start().normalize(), periods=n, freq="D", tz=IST)
    else:
        minutes = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30, "1h": 60}.get(interval, 5)
        n = max(30, min(375 // minutes, 75))
        index = pd.date_range(start=_session_start(), periods=n, freq=f"{minutes}min", tz=IST)

    closes = 1400 + np.cumsum(rng.normal(0, 2.0, len(index))) + np.linspace(0, 25, len(index))
    return pd.DataFrame(
        {
            "Open": closes - 0.5,
            "High": closes + abs(rng.normal(0, 1.5, len(index))) + 1,
            "Low": closes - abs(rng.normal(0, 1.5, len(index))) - 1,
            "Close": closes,
            "Volume": rng.integers(1000, 9000, len(index)).astype(float),
        },
        index=index,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8131)
    args = parser.parse_args()

    import data_sources
    data_sources._yf_download = lambda symbol, period, interval: synthetic(period, interval)
    data_sources._fetch_company_news_uncached = lambda *a, **k: [
        {"title": "Synthetic headline for local testing", "description": "",
         "source": "devserver", "published_at": "", "url": "https://example.com/story"},
    ]
    data_sources._fetch_weather_signal_uncached = lambda *a, **k: {
        "current": {"temperature_2m": 30, "precipitation": 0}}
    data_sources.get_stock_universe = lambda *a, **k: [
        {"symbol": "RELIANCE", "name": "Reliance Industries Ltd", "exchange": "NSE"},
        {"symbol": "TCS", "name": "Tata Consultancy Services Ltd", "exchange": "NSE"},
        {"symbol": "INFY", "name": "Infosys Ltd", "exchange": "NSE"},
    ]

    import uvicorn
    import main as app_module
    uvicorn.run(app_module.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
