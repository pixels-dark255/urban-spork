"""
Broker interface - the boundary between "decide" and "do".

The prediction engine never imports a broker SDK. It produces a trade plan;
`live_trading.py` decides whether that plan is allowed to become an order;
and only a Broker implementation actually transmits anything. Swapping
brokers means writing one adapter, not touching the engine.

Two rules every implementation must honour:

 1. `place_order` is idempotent on `reference_id`. Networks retry, schedulers
    overlap, and users double-tap. An order path that can double-fire is a
    path that can double your position size without asking.
 2. Nothing here enforces risk limits. Gates live in one place
    (`live_trading.py`) so there is exactly one answer to "what stops this
    from trading my account away", not one per adapter.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


BUY = "BUY"
SELL = "SELL"

# Normalised order states. Each adapter maps its broker's vocabulary onto
# these, so the rest of the app never parses broker-specific strings.
STATUS_NEW = "NEW"
STATUS_OPEN = "OPEN"
STATUS_FILLED = "FILLED"
STATUS_PARTIALLY_FILLED = "PARTIALLY_FILLED"
STATUS_CANCELLED = "CANCELLED"
STATUS_REJECTED = "REJECTED"
STATUS_FAILED = "FAILED"

TERMINAL_STATUSES = {STATUS_FILLED, STATUS_CANCELLED, STATUS_REJECTED, STATUS_FAILED}


@dataclass
class OrderRequest:
    symbol: str
    exchange: str            # NSE | BSE
    side: str                # BUY | SELL
    quantity: int
    order_type: str = "LIMIT"     # LIMIT | MARKET | SL | SL_M
    price: float | None = None
    trigger_price: float | None = None
    product: str = "MIS"          # MIS = intraday, CNC = delivery
    validity: str = "DAY"
    segment: str = "CASH"
    reference_id: str | None = None
    intent: str = "ENTRY"         # ENTRY | EXIT | SQUARE_OFF
    tag: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class OrderResult:
    ok: bool
    reference_id: str | None = None
    broker_order_id: str | None = None
    status: str = STATUS_NEW
    filled_quantity: int = 0
    average_price: float | None = None
    error: str | None = None
    raw: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


class BrokerError(Exception):
    """Anything the broker refused or could not do."""


class Broker:
    name = "base"
    supports_real_money = False

    def is_configured(self) -> bool:
        """Credentials present and the SDK importable. Checked before arming,
        never assumed."""
        return False

    def connect(self) -> dict:
        """Establish/verify a session. Returns a short status dict."""
        raise NotImplementedError

    def place_order(self, order: OrderRequest) -> OrderResult:
        raise NotImplementedError

    def cancel_order(self, broker_order_id: str, segment: str = "CASH") -> OrderResult:
        raise NotImplementedError

    def order_status(self, broker_order_id: str, segment: str = "CASH") -> OrderResult:
        raise NotImplementedError

    def positions(self) -> list[dict]:
        raise NotImplementedError

    def holdings(self) -> list[dict]:
        raise NotImplementedError

    def funds(self) -> dict:
        raise NotImplementedError

    def quote(self, symbol: str, exchange: str = "NSE") -> float | None:
        """Broker's own last traded price. Used as the pre-trade sanity check
        against the price the analysis was built on - if the two disagree,
        something is stale and the order should not go."""
        raise NotImplementedError

    def place_bracket_exit(self, symbol: str, exchange: str, side: str, quantity: int,
                           target_price: float, stop_price: float,
                           product: str = "MIS") -> OrderResult:
        """Attach target + stop as one exit pair, if the broker supports it.
        Implementations that don't should return ok=False with a reason so
        the caller can fall back to locally-monitored exits."""
        return OrderResult(ok=False, error="bracket exits not supported by this broker")

    def status(self) -> dict:
        return {
            "name": self.name,
            "configured": self.is_configured(),
            "supports_real_money": self.supports_real_money,
        }
