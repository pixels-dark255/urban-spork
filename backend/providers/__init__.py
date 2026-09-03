"""Provider registry. `get_provider()` returns the active data provider,
falling back to Yahoo whenever the configured one isn't usable."""
from __future__ import annotations

import config
from providers.base import MarketDataProvider
from providers.yahoo import YahooProvider
from providers.kotak_neo import KotakNeoProvider

_REGISTRY: dict[str, MarketDataProvider] = {}


def _registry() -> dict[str, MarketDataProvider]:
    if not _REGISTRY:
        _REGISTRY["yahoo"] = YahooProvider()
        _REGISTRY["kotak_neo"] = KotakNeoProvider()
    return _REGISTRY


def get_provider(name: str | None = None) -> MarketDataProvider:
    reg = _registry()
    wanted = (name or config.MARKET_DATA_PROVIDER or "yahoo").lower()
    provider = reg.get(wanted)
    if provider is not None and provider.is_available():
        return provider
    if provider is not None and not provider.is_available():
        print(f"[warn] provider '{wanted}' not available - falling back to yahoo")
    return reg["yahoo"]


def provider_status() -> list[dict]:
    return [p.status() for p in _registry().values()]


__all__ = ["MarketDataProvider", "get_provider", "provider_status"]
