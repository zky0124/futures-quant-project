from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from futures_quant.api.ctp_config import CtpConfig, SecretValue
from futures_quant.models import Bar


class CtpSdkUnavailable(RuntimeError):
    """Raised when the selected CTP SDK adapter cannot be imported."""


class CtpAdapterError(RuntimeError):
    """Raised when an adapter does not implement the required boundary."""


class CtpChannel(str, Enum):
    MARKET_DATA = "market_data"
    TRADING = "trading"


class CtpAdapterStage(str, Enum):
    MARKET_CONNECTED = "market_connected"
    TRADING_CONNECTED = "trading_connected"
    AUTHENTICATED = "authenticated"
    MARKET_LOGGED_IN = "market_logged_in"
    TRADING_LOGGED_IN = "trading_logged_in"
    SETTLEMENT_CONFIRMED = "settlement_confirmed"
    HEARTBEAT = "heartbeat"


class CtpDirection(str, Enum):
    BUY = "buy"
    SELL = "sell"


class CtpOffset(str, Enum):
    OPEN = "open"
    CLOSE = "close"
    CLOSE_TODAY = "close_today"
    CLOSE_YESTERDAY = "close_yesterday"


class CtpOrderStatus(str, Enum):
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        return self in {self.FILLED, self.CANCELLED, self.REJECTED}


@dataclass(frozen=True)
class CtpConnectionInfo:
    broker_id: str
    user_id: str
    trade_front: str
    market_front: str
    app_id: str = ""
    api_version: str = ""


@dataclass(frozen=True)
class CtpCredentials:
    password: SecretValue = field(repr=False)
    auth_code: SecretValue = field(repr=False)


@dataclass(frozen=True)
class CtpOrderRequest:
    client_order_id: str
    symbol: str
    exchange_id: str
    direction: CtpDirection
    offset: CtpOffset
    volume: int
    limit_price: float
    reason: str = ""
    price_type: str = "limit"
    time_condition: str = "GFD"
    volume_condition: str = "AV"
    hedge_flag: str = "speculation"


@dataclass(frozen=True)
class CtpCancelRequest:
    client_order_id: str
    broker_order_id: str
    symbol: str
    exchange_id: str


@dataclass(frozen=True)
class CtpOrderUpdate:
    order_id: str
    client_order_id: str
    symbol: str
    exchange_id: str
    direction: CtpDirection
    offset: CtpOffset
    total_volume: int
    traded_volume: int
    limit_price: float
    status: CtpOrderStatus
    update_time: datetime
    status_message: str = ""
    front_id: int | None = None
    session_id: int | None = None


@dataclass(frozen=True)
class CtpTradeUpdate:
    trade_id: str
    order_id: str
    client_order_id: str
    symbol: str
    exchange_id: str
    direction: CtpDirection
    offset: CtpOffset
    volume: int
    price: float
    trade_time: datetime
    trading_day: str = ""
    commission: float = 0.0
    is_query: bool = False


@dataclass(frozen=True)
class CtpPositionSnapshot:
    symbol: str
    exchange_id: str
    long_quantity: int = 0
    short_quantity: int = 0
    today_long: int = 0
    today_short: int = 0
    avg_long_price: float = 0.0
    avg_short_price: float = 0.0
    used_margin: float = 0.0
    updated_at: datetime | None = None

    @property
    def net_quantity(self) -> int:
        return self.long_quantity - self.short_quantity

    @property
    def is_flat(self) -> bool:
        return self.long_quantity == 0 and self.short_quantity == 0


@dataclass(frozen=True)
class CtpAccountSnapshot:
    account_id: str
    balance: float
    available: float
    current_margin: float
    frozen_margin: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    commission: float = 0.0
    risk_ratio: float = 0.0
    trading_day: str = ""
    updated_at: datetime | None = None


@dataclass(frozen=True)
class CtpInstrumentSnapshot:
    symbol: str
    exchange_id: str
    volume_multiple: int
    long_margin_rate: float
    short_margin_rate: float
    price_tick: float = 0.0
    upper_limit_price: float = 0.0
    lower_limit_price: float = 0.0


@dataclass(frozen=True)
class CtpGatewayError:
    code: int | str
    message: str
    source: str = "adapter"
    retryable: bool = False


@runtime_checkable
class CtpAdapterCallbacks(Protocol):
    """Callbacks an SDK wrapper uses to publish normalized CTP events."""

    def on_adapter_stage(self, stage: CtpAdapterStage) -> None: ...

    def on_adapter_disconnected(
        self, channel: CtpChannel, reason: str, *, retryable: bool = True
    ) -> None: ...

    def on_heartbeat(self, channel: CtpChannel) -> None: ...

    def on_bar(self, bar: Bar) -> None: ...

    def on_order_update(self, update: CtpOrderUpdate) -> None: ...

    def on_order_query_complete(self) -> None: ...

    def on_trade_update(self, update: CtpTradeUpdate) -> None: ...

    def on_trade_query_complete(self) -> None: ...

    def on_position(
        self, position: CtpPositionSnapshot | None, *, is_last: bool
    ) -> None: ...

    def on_account(
        self, account: CtpAccountSnapshot | None, *, is_last: bool
    ) -> None: ...

    def on_instrument(self, instrument: CtpInstrumentSnapshot) -> None: ...

    def on_adapter_error(self, error: CtpGatewayError) -> None: ...


@runtime_checkable
class CtpAdapter(Protocol):
    """High-level boundary implemented around a broker-supported CTP SDK.

    A concrete wrapper owns SDK-specific enum conversion and authentication.
    It must not log the credential object or include credential values in
    callback errors.
    """

    def connect(
        self,
        connection: CtpConnectionInfo,
        credentials: CtpCredentials,
        callbacks: CtpAdapterCallbacks,
    ) -> None: ...

    def disconnect(self) -> None: ...

    def subscribe(self, symbol: str) -> None: ...

    def send_order(self, request: CtpOrderRequest) -> str | None: ...

    def cancel_order(self, request: CtpCancelRequest) -> None: ...

    def query_account(self) -> None: ...

    def query_positions(self) -> None: ...

    def query_orders(self) -> None: ...

    def query_trades(self) -> None: ...


_REQUIRED_ADAPTER_METHODS = (
    "connect",
    "disconnect",
    "subscribe",
    "send_order",
    "cancel_order",
    "query_account",
    "query_positions",
    "query_orders",
    "query_trades",
)


def validate_adapter(adapter: object) -> CtpAdapter:
    missing = [name for name in _REQUIRED_ADAPTER_METHODS if not callable(getattr(adapter, name, None))]
    if missing:
        raise CtpAdapterError(
            "CTP adapter is missing required methods: " + ", ".join(missing) + "."
        )
    return adapter  # type: ignore[return-value]


def load_ctp_adapter(spec: str) -> CtpAdapter:
    """Load ``module:factory`` without importing any optional SDK at startup."""

    normalized = spec.strip()
    if not normalized:
        raise CtpSdkUnavailable(
            "No CTP SDK adapter is configured. Install the broker-supported SDK, "
            "implement the CtpAdapter boundary, and set adapter to "
            "'package.module:factory'. No network connection was attempted."
        )
    if ":" not in normalized:
        raise CtpSdkUnavailable(
            "CTP adapter must use 'package.module:factory' syntax."
        )
    module_name, attribute_name = normalized.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise CtpSdkUnavailable(
            f"Cannot import configured CTP adapter module {module_name!r}: "
            f"{type(exc).__name__}. Install the exact broker-supported SDK and "
            "matching Python architecture; no login was attempted."
        ) from exc
    try:
        provider = getattr(module, attribute_name)
    except AttributeError as exc:
        raise CtpSdkUnavailable(
            f"CTP adapter module {module_name!r} has no attribute "
            f"{attribute_name!r}."
        ) from exc
    try:
        adapter = provider() if inspect.isclass(provider) or callable(provider) else provider
    except Exception as exc:
        raise CtpSdkUnavailable(
            f"CTP adapter factory {normalized!r} failed: {type(exc).__name__}."
        ) from exc
    return validate_adapter(adapter)


def diagnose_ctp_adapter(config: CtpConfig) -> dict[str, object]:
    """Return a redacted, non-network SDK readiness diagnostic."""

    result = config.redacted_summary()
    normalized = config.adapter.strip()
    if not normalized:
        result.update(
            {
                "adapter_available": False,
                "diagnostic": (
                    "No CTP SDK adapter is configured. Set adapter to "
                    "'package.module:factory'; no import or connection was attempted."
                ),
            }
        )
        return result
    if ":" not in normalized:
        result.update(
            {
                "adapter_available": False,
                "diagnostic": "CTP adapter must use 'package.module:factory' syntax.",
            }
        )
        return result
    module_name, attribute_name = normalized.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        provider = getattr(module, attribute_name)
    except Exception as exc:
        result.update({"adapter_available": False, "diagnostic": str(exc)})
        return result
    if not callable(provider) and not all(
        callable(getattr(provider, name, None)) for name in _REQUIRED_ADAPTER_METHODS
    ):
        result.update(
            {
                "adapter_available": False,
                "diagnostic": "Configured adapter provider is neither callable nor an adapter object.",
            }
        )
        return result
    result.update(
        {
            "adapter_available": True,
            "diagnostic": (
                "Adapter provider import succeeded; the factory was not invoked and "
                "no network connection was attempted. Runtime interface validation "
                "will occur only when connect() is explicitly called."
            ),
        }
    )
    return result
