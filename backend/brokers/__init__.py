"""Broker registry.

`get_broker()` returns the configured broker, and falls back to the paper
broker whenever the real one is not fully configured. The fallback direction
matters: a misconfiguration must degrade to simulation, never to an
unexpected live order.
"""
from __future__ import annotations

import config
from brokers.base import Broker, OrderRequest, OrderResult, BrokerError  # noqa: F401
from brokers.paper import PaperBroker
from brokers.groww import GrowwBroker

_REGISTRY: dict[str, Broker] = {}


def _registry() -> dict[str, Broker]:
    if not _REGISTRY:
        _REGISTRY["paper"] = PaperBroker()
        _REGISTRY["groww"] = GrowwBroker()
    return _REGISTRY


def get_broker(name: str | None = None) -> Broker:
    reg = _registry()
    wanted = (name or config.BROKER or "paper").lower()
    broker = reg.get(wanted)
    if broker is None:
        print(f"[warn] unknown broker '{wanted}' - using paper")
        return reg["paper"]
    if broker.supports_real_money and not broker.is_configured():
        print(f"[warn] broker '{wanted}' is not configured - using paper instead")
        return reg["paper"]
    return broker


def broker_status() -> list[dict]:
    return [b.status() for b in _registry().values()]


def configured_broker_name() -> str:
    return get_broker().name


__all__ = ["Broker", "OrderRequest", "OrderResult", "BrokerError",
           "get_broker", "broker_status", "configured_broker_name"]
