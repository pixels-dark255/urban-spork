"""
Groww adapter - places real orders with real money.

Written against growwapi 1.5.0's actual client surface:
  place_order(validity, exchange, order_type, product, quantity, segment,
              trading_symbol, transaction_type, order_reference_id=None,
              price=0.0, trigger_price=None, timeout=None)
  cancel_order(groww_order_id, segment)
  get_order_status(segment, groww_order_id)
  get_positions_for_user(segment=None) / get_holdings_for_user()
  get_available_margin_details()
  get_ltp(exchange_trading_symbols, segment)        # "NSE_RELIANCE"
  create_smart_order(...)                            # OCO target+stop pair
  get_instrument_by_exchange_and_trading_symbol(exchange, trading_symbol)

The SDK is imported lazily so the rest of the platform runs, and the whole
test suite passes, on a machine where growwapi is not installed at all.

Authentication: Groww access tokens are short-lived. Supply either a daily
GROWW_ACCESS_TOKEN, or an API key plus TOTP secret and this will mint one
and refresh it when it expires mid-session. Credentials come from the
environment only and are never logged - `status()` reports whether each is
present, never its value.
"""
from __future__ import annotations

import datetime as dt
import threading

import config
from brokers.base import (
    Broker, OrderRequest, OrderResult, BrokerError,
    STATUS_NEW, STATUS_OPEN, STATUS_FILLED, STATUS_PARTIALLY_FILLED,
    STATUS_CANCELLED, STATUS_REJECTED, STATUS_FAILED,
)

# Groww's order states -> ours. Anything unrecognised is treated as OPEN
# rather than terminal: assuming an unknown state means "done" is how a live
# position stops being watched while it is still open.
_STATUS_MAP = {
    "NEW": STATUS_NEW,
    "ACK": STATUS_OPEN,
    "ACKED": STATUS_OPEN,
    "OPEN": STATUS_OPEN,
    "PENDING": STATUS_OPEN,
    "TRIGGER_PENDING": STATUS_OPEN,
    "APPROVED": STATUS_OPEN,
    "EXECUTED": STATUS_FILLED,
    "COMPLETE": STATUS_FILLED,
    "COMPLETED": STATUS_FILLED,
    "FILLED": STATUS_FILLED,
    "PARTIALLY_FILLED": STATUS_PARTIALLY_FILLED,
    "PARTIAL": STATUS_PARTIALLY_FILLED,
    "CANCELLED": STATUS_CANCELLED,
    "CANCELED": STATUS_CANCELLED,
    "REJECTED": STATUS_REJECTED,
    "FAILED": STATUS_FAILED,
}

_TOKEN_TTL_MINUTES = 8 * 60


def normalise_status(raw: str | None) -> str:
    if not raw:
        return STATUS_OPEN
    return _STATUS_MAP.get(str(raw).strip().upper(), STATUS_OPEN)


class GrowwBroker(Broker):
    name = "groww"
    supports_real_money = True

    def __init__(self):
        self._client = None
        self._token_minted_at: dt.datetime | None = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- setup

    @staticmethod
    def _sdk():
        try:
            from growwapi import GrowwAPI
            return GrowwAPI
        except Exception as e:
            raise BrokerError(
                "growwapi is not installed. Run `pip install growwapi` in the "
                f"environment running this app. ({e})"
            )

    def _sdk_present(self) -> bool:
        try:
            self._sdk()
            return True
        except BrokerError:
            return False

    def _credentials_present(self) -> bool:
        return bool(config.GROWW_ACCESS_TOKEN or
                    (config.GROWW_API_KEY and (config.GROWW_TOTP_SECRET or config.GROWW_API_SECRET)))

    def is_configured(self) -> bool:
        return self._credentials_present() and self._sdk_present()

    def _mint_token(self) -> str:
        """A daily token if one was supplied, otherwise mint one from the API
        key. TOTP is generated locally from the shared secret - the secret
        never leaves this process except as a six-digit code."""
        if config.GROWW_ACCESS_TOKEN:
            return config.GROWW_ACCESS_TOKEN
        if not config.GROWW_API_KEY:
            raise BrokerError("No GROWW_ACCESS_TOKEN and no GROWW_API_KEY configured.")

        GrowwAPI = self._sdk()
        if config.GROWW_TOTP_SECRET:
            try:
                import pyotp
            except Exception as e:
                raise BrokerError(
                    "GROWW_TOTP_SECRET is set but pyotp is not installed. "
                    f"Run `pip install pyotp`. ({e})"
                )
            totp = pyotp.TOTP(config.GROWW_TOTP_SECRET).now()
            return GrowwAPI.get_access_token(api_key=config.GROWW_API_KEY, totp=totp)
        return GrowwAPI.get_access_token(api_key=config.GROWW_API_KEY,
                                         secret=config.GROWW_API_SECRET)

    def _token_expired(self) -> bool:
        if self._token_minted_at is None:
            return True
        age = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - self._token_minted_at)
        return age.total_seconds() > _TOKEN_TTL_MINUTES * 60

    def client(self, force_refresh: bool = False):
        with self._lock:
            if self._client is not None and not force_refresh and not self._token_expired():
                return self._client
            GrowwAPI = self._sdk()
            token = self._mint_token()
            self._client = GrowwAPI(token)
            self._token_minted_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            return self._client

    def _call(self, fn_name: str, *args, **kwargs):
        """Invoke an SDK method, refreshing the token once on an auth failure.
        Groww tokens expire during a trading day; a single retry after a
        refresh is the difference between a working session and a dead one."""
        client = self.client()
        try:
            return getattr(client, fn_name)(*args, **kwargs)
        except Exception as e:
            if _is_auth_error(e):
                client = self.client(force_refresh=True)
                return getattr(client, fn_name)(*args, **kwargs)
            raise

    def connect(self) -> dict:
        profile = self._call("get_user_profile")
        return {"connected": True, "profile": _scrub_profile(profile)}

    # ------------------------------------------------------------ symbols

    def trading_symbol(self, symbol: str, exchange: str) -> str:
        """Resolve our symbol to Groww's trading_symbol, verified against
        their instrument master. An unresolvable symbol raises rather than
        being guessed at - a wrong symbol is a wrong order."""
        candidate = symbol.upper().replace(".NS", "").replace(".BO", "")
        try:
            instrument = self._call("get_instrument_by_exchange_and_trading_symbol",
                                    exchange=exchange.upper(), trading_symbol=candidate)
        except Exception as e:
            raise BrokerError(
                f"{candidate} could not be resolved on {exchange.upper()} in Groww's "
                f"instrument master ({e}). Refusing to guess a trading symbol."
            )
        resolved = (instrument or {}).get("trading_symbol") or candidate
        return str(resolved)

    # ------------------------------------------------------------- orders

    def place_order(self, order: OrderRequest) -> OrderResult:
        GrowwAPI = self._sdk()
        symbol = self.trading_symbol(order.symbol, order.exchange)

        payload = dict(
            validity=order.validity,
            exchange=order.exchange.upper(),
            order_type=order.order_type.upper(),
            product=order.product.upper(),
            quantity=int(order.quantity),
            segment=order.segment.upper(),
            trading_symbol=symbol,
            transaction_type=order.side.upper(),
            order_reference_id=order.reference_id,
        )
        # Groww wants a numeric price field even for MARKET orders, where it
        # is ignored; sending None there is a 400.
        payload["price"] = float(order.price) if order.price else 0.0
        if order.trigger_price:
            payload["trigger_price"] = float(order.trigger_price)

        try:
            raw = self._call("place_order", **payload)
        except Exception as e:
            return OrderResult(ok=False, reference_id=order.reference_id,
                               status=STATUS_FAILED, error=str(e))

        return OrderResult(
            ok=True,
            reference_id=order.reference_id,
            broker_order_id=str(raw.get("groww_order_id") or raw.get("order_id") or ""),
            status=normalise_status(raw.get("order_status") or raw.get("status")),
            raw=raw if isinstance(raw, dict) else {"response": str(raw)},
        )

    def cancel_order(self, broker_order_id: str, segment: str = "CASH") -> OrderResult:
        try:
            raw = self._call("cancel_order", groww_order_id=str(broker_order_id),
                             segment=segment.upper())
        except Exception as e:
            return OrderResult(ok=False, broker_order_id=broker_order_id,
                               status=STATUS_FAILED, error=str(e))
        return OrderResult(ok=True, broker_order_id=str(broker_order_id),
                           status=normalise_status(raw.get("order_status") or raw.get("status")),
                           raw=raw if isinstance(raw, dict) else {})

    def order_status(self, broker_order_id: str, segment: str = "CASH") -> OrderResult:
        try:
            raw = self._call("get_order_status", segment=segment.upper(),
                             groww_order_id=str(broker_order_id))
        except Exception as e:
            return OrderResult(ok=False, broker_order_id=broker_order_id,
                               status=STATUS_FAILED, error=str(e))
        filled = raw.get("filled_quantity") or raw.get("filledQuantity") or 0
        avg = raw.get("average_fill_price") or raw.get("average_price") or raw.get("avg_price")
        return OrderResult(
            ok=True,
            broker_order_id=str(broker_order_id),
            status=normalise_status(raw.get("order_status") or raw.get("status")),
            filled_quantity=int(filled or 0),
            average_price=float(avg) if avg else None,
            raw=raw if isinstance(raw, dict) else {},
        )

    def place_bracket_exit(self, symbol: str, exchange: str, side: str, quantity: int,
                           target_price: float, stop_price: float,
                           product: str = "MIS") -> OrderResult:
        """OCO pair: whichever of target/stop triggers first cancels the other.
        Resting the exits at the broker matters - it means the position is
        protected even if this application dies."""
        GrowwAPI = self._sdk()
        try:
            trading_symbol = self.trading_symbol(symbol, exchange)
            raw = self._call(
                "create_smart_order",
                smart_order_type=GrowwAPI.SMART_ORDER_TYPE_OCO,
                segment=GrowwAPI.SEGMENT_CASH,
                trading_symbol=trading_symbol,
                quantity=int(quantity),
                product_type=product.upper(),
                exchange=exchange.upper(),
                duration=GrowwAPI.VALIDITY_DAY,
                net_position_quantity=int(quantity),
                transaction_type=side.upper(),
                target={"trigger_price": str(round(float(target_price), 2)),
                        "order_type": GrowwAPI.ORDER_TYPE_LIMIT,
                        "price": str(round(float(target_price), 2))},
                stop_loss={"trigger_price": str(round(float(stop_price), 2)),
                           "order_type": GrowwAPI.ORDER_TYPE_STOP_LOSS_MARKET},
            )
        except Exception as e:
            return OrderResult(ok=False, status=STATUS_FAILED, error=str(e))
        return OrderResult(ok=True,
                           broker_order_id=str(raw.get("smart_order_id")
                                               or raw.get("reference_id") or ""),
                           status=STATUS_OPEN,
                           raw=raw if isinstance(raw, dict) else {})

    # ------------------------------------------------------------ account

    def positions(self) -> list[dict]:
        raw = self._call("get_positions_for_user", segment="CASH")
        return _as_list(raw, "positions")

    def holdings(self) -> list[dict]:
        raw = self._call("get_holdings_for_user")
        return _as_list(raw, "holdings")

    def funds(self) -> dict:
        raw = self._call("get_available_margin_details")
        return raw if isinstance(raw, dict) else {"raw": str(raw)}

    def quote(self, symbol: str, exchange: str = "NSE") -> float | None:
        key = f"{exchange.upper()}_{symbol.upper().replace('.NS', '').replace('.BO', '')}"
        try:
            raw = self._call("get_ltp", exchange_trading_symbols=(key,), segment="CASH")
        except Exception as e:
            print(f"[warn] groww ltp failed for {key}: {e}")
            return None
        if not isinstance(raw, dict):
            return None
        value = raw.get(key)
        if value is None and len(raw) == 1:
            value = next(iter(raw.values()))
        try:
            return round(float(value), 2) if value is not None else None
        except (TypeError, ValueError):
            return None

    def status(self) -> dict:
        return {
            "name": self.name,
            "supports_real_money": True,
            "sdk_installed": self._sdk_present(),
            # Presence only. Never the values.
            "access_token_configured": bool(config.GROWW_ACCESS_TOKEN),
            "api_key_configured": bool(config.GROWW_API_KEY),
            "totp_configured": bool(config.GROWW_TOTP_SECRET),
            "configured": self.is_configured(),
            "session_active": self._client is not None and not self._token_expired(),
        }


def _is_auth_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if "authentication" in name or "authorisation" in name or "authorization" in name:
        return True
    text = str(exc).lower()
    return "401" in text or "unauthor" in text or "token" in text and "expir" in text


def _as_list(raw, key: str) -> list[dict]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for candidate in (key, "payload", "data"):
            value = raw.get(candidate)
            if isinstance(value, list):
                return value
        return [raw] if raw else []
    return []


def _scrub_profile(profile) -> dict:
    """Keep the identifying bits, drop anything that looks like a secret -
    this ends up in status responses and logs."""
    if not isinstance(profile, dict):
        return {}
    allowed = ("user_id", "userId", "name", "user_name", "email", "account_id", "broker")
    return {k: v for k, v in profile.items() if k in allowed}
