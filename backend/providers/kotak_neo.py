"""
Kotak Neo provider - deliberately a stub.

The broker layer is modular precisely so this can be filled in without the
prediction engine noticing. What is real here today:
 - credentials are read from the environment only (never source), via config
 - the provider reports itself unavailable unless those are set AND the
   optional `neo_api_client` SDK is installed
 - the rest of the app therefore keeps using Yahoo until a real, tested
   integration lands

What is NOT here: any code that places orders. Order placement is Phase 6 in
the roadmap, gated behind extensive validation and a manual-approval mode.
Shipping an untested order path would be the single most expensive kind of
bug this project could have, so it does not exist yet.
"""
from __future__ import annotations

import pandas as pd

import config
from providers.base import MarketDataProvider, empty_frame


class KotakNeoProvider(MarketDataProvider):
    name = "kotak_neo"

    def __init__(self):
        self._sdk = None

    def _credentials_present(self) -> bool:
        return all([
            config.KOTAK_NEO_CONSUMER_KEY,
            config.KOTAK_NEO_CONSUMER_SECRET,
            config.KOTAK_NEO_MOBILE,
            config.KOTAK_NEO_PASSWORD,
        ])

    def _sdk_present(self) -> bool:
        try:
            import neo_api_client  # noqa: F401
            return True
        except Exception:
            return False

    def is_available(self) -> bool:
        return self._credentials_present() and self._sdk_present()

    def capabilities(self) -> dict:
        # Declared as what a completed integration would offer; `available`
        # in status() is what actually gates use.
        return {
            "quotes": True,
            "historical": True,
            "intraday": True,
            "streaming": True,
            "orders": False,   # intentionally not implemented - see module docstring
        }

    def status(self) -> dict:
        base = super().status()
        base["credentials_configured"] = self._credentials_present()
        base["sdk_installed"] = self._sdk_present()
        base["note"] = (
            "Stub. Set KOTAK_NEO_* environment variables and install neo_api_client "
            "to enable. Order placement is intentionally not implemented."
        )
        return base

    def get_bars(self, symbol: str, exchange: str, interval: str, period: str) -> pd.DataFrame:
        raise NotImplementedError("Kotak Neo historical data is not implemented yet.")

    def get_quote(self, symbol: str, exchange: str) -> float | None:
        raise NotImplementedError("Kotak Neo quotes are not implemented yet.")
