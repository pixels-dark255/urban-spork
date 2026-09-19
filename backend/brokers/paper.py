"""
Paper broker - the same interface, simulated fills, no money.

This exists so the entire live-trading path (gates, order records, audit
log, exit handling, square-off, reconciliation) can be exercised end to end
without a broker account. It is also the default: if the broker is
misconfigured, orders land here rather than at an exchange.

Fills are modelled honestly rather than flatteringly: a LIMIT order fills at
its limit price only if the market is actually there, otherwise it rests
open exactly as a real one would.
"""
from __future__ import annotations

import uuid

import market_data
import market_store
from brokers.base import (
    Broker, OrderRequest, OrderResult,
    STATUS_FILLED, STATUS_OPEN, STATUS_CANCELLED, STATUS_FAILED,
)


class PaperBroker(Broker):
    name = "paper"
    supports_real_money = False

    def is_configured(self) -> bool:
        return True

    def connect(self) -> dict:
        return {"connected": True, "profile": {"name": "paper trading (no broker)"}}

    def _price(self, symbol: str, exchange: str) -> float | None:
        return market_data.get_quote(symbol, exchange)

    def place_order(self, order: OrderRequest) -> OrderResult:
        reference = order.reference_id or uuid.uuid4().hex[:12]
        price = self._price(order.symbol, order.exchange)
        if price is None:
            return OrderResult(ok=False, reference_id=reference, status=STATUS_FAILED,
                               error="no price available to simulate a fill")

        if order.order_type.upper() == "MARKET":
            fill = price
        else:
            limit = float(order.price or price)
            # A buy limit fills only if the market is at or below it; a sell
            # limit only at or above. Otherwise it rests, like a real one.
            marketable = (price <= limit) if order.side.upper() == "BUY" else (price >= limit)
            if not marketable:
                return OrderResult(ok=True, reference_id=reference,
                                   broker_order_id=f"PAPER-{reference}",
                                   status=STATUS_OPEN,
                                   raw={"simulated": True, "resting_at": limit,
                                        "market": price})
            fill = limit

        return OrderResult(
            ok=True,
            reference_id=reference,
            broker_order_id=f"PAPER-{reference}",
            status=STATUS_FILLED,
            filled_quantity=int(order.quantity),
            average_price=round(float(fill), 2),
            raw={"simulated": True, "market": price},
        )

    def cancel_order(self, broker_order_id: str, segment: str = "CASH") -> OrderResult:
        return OrderResult(ok=True, broker_order_id=broker_order_id, status=STATUS_CANCELLED,
                           raw={"simulated": True})

    def order_status(self, broker_order_id: str, segment: str = "CASH") -> OrderResult:
        row = market_store.get_order_by_reference(
            str(broker_order_id).replace("PAPER-", "")
        )
        if not row:
            return OrderResult(ok=True, broker_order_id=broker_order_id, status=STATUS_OPEN,
                               raw={"simulated": True})
        return OrderResult(ok=True, broker_order_id=broker_order_id,
                           status=row.get("status") or STATUS_OPEN,
                           filled_quantity=int(row.get("filled_quantity") or 0),
                           average_price=row.get("average_price"),
                           raw={"simulated": True})

    def positions(self) -> list[dict]:
        out = []
        for order in market_store.list_orders(limit=500):
            if order.get("status") == STATUS_FILLED and not order.get("closed_at") \
                    and order.get("intent") == "ENTRY":
                out.append({
                    "trading_symbol": order["symbol"],
                    "exchange": order["exchange"],
                    "quantity": order["quantity"] if order["side"] == "BUY" else -order["quantity"],
                    "average_price": order.get("average_price"),
                    "simulated": True,
                })
        return out

    def holdings(self) -> list[dict]:
        return []

    def funds(self) -> dict:
        import risk
        settings = risk.get_settings("default")
        return {"simulated": True, "available_margin": settings["capital"],
                "note": "Paper broker - this is your configured capital, not real funds."}

    def quote(self, symbol: str, exchange: str = "NSE") -> float | None:
        return self._price(symbol, exchange)

    def place_bracket_exit(self, symbol: str, exchange: str, side: str, quantity: int,
                           target_price: float, stop_price: float,
                           product: str = "MIS") -> OrderResult:
        # Simulated brackets are monitored locally by the scheduler, the same
        # fallback a real broker gets when OCO is unavailable.
        return OrderResult(ok=False, error="paper broker uses locally monitored exits")

    def status(self) -> dict:
        return {"name": self.name, "configured": True, "supports_real_money": False,
                "note": "Simulated fills. No orders leave this machine."}
