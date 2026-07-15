from __future__ import annotations

import math
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import Enum
from typing import Callable

from futures_quant.api.base import TradingGateway
from futures_quant.api.ctp_adapter import (
    CtpAccountSnapshot,
    CtpAdapter,
    CtpAdapterStage,
    CtpCancelRequest,
    CtpChannel,
    CtpConnectionInfo,
    CtpCredentials,
    CtpDirection,
    CtpGatewayError,
    CtpInstrumentSnapshot,
    CtpOffset,
    CtpOrderRequest,
    CtpOrderStatus,
    CtpOrderUpdate,
    CtpPositionSnapshot,
    CtpTradeUpdate,
    load_ctp_adapter,
    validate_adapter,
)
from futures_quant.api.ctp_config import (
    CtpConfig,
    CtpConfigurationError,
    SecretValue,
    TradingMode,
)
from futures_quant.models import Bar, Order, Trade


class GatewayState(str, Enum):
    DISABLED = "disabled"
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    LOGGING_IN = "logging_in"
    SETTLEMENT_CONFIRMING = "settlement_confirming"
    SYNCING = "syncing"
    READY = "ready"
    RECONNECT_WAIT = "reconnect_wait"
    ERROR = "error"
    STOPPED = "stopped"


class CtpGatewayStateError(RuntimeError):
    pass


class CtpOrderRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class CtpGatewayStatus:
    state: GatewayState
    mode: TradingMode
    connected: bool
    market_ready: bool
    trading_ready: bool
    live_armed: bool
    reconnect_attempts: int
    subscriptions: tuple[str, ...]
    last_event_age_seconds: float
    last_error: str


_LIVE_ARM_PHRASE = "ARM LIVE TRADING"


class CtpGateway(TradingGateway):
    """Safety-first CTP orchestration around an optional broker SDK adapter.

    The gateway never imports a broker SDK directly. A concrete adapter maps
    the broker's SDK callbacks into the normalized objects in ``ctp_adapter``.
    The legacy constructor remains accepted, but it creates a disabled paper
    configuration so upgrading cannot silently enable outbound requests.
    """

    def __init__(
        self,
        front_addr: str | CtpConfig = "",
        broker_id: str = "",
        user_id: str = "",
        password: str = "",
        app_id: str = "",
        auth_code: str = "",
        *,
        market_data_addr: str = "",
        config: CtpConfig | None = None,
        adapter: CtpAdapter | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if isinstance(front_addr, CtpConfig):
            if config is not None:
                raise TypeError("Pass CtpConfig either positionally or via config, not both.")
            config = front_addr
            front_addr = ""
        if config is None:
            config = CtpConfig(
                broker_id=broker_id,
                trade_front=str(front_addr),
                market_front=market_data_addr,
                user_id=user_id,
                password=SecretValue(password),
                app_id=app_id,
                auth_code=SecretValue(auth_code),
                mode=TradingMode.PAPER,
                enabled=False,
            )
        self.config = config
        self.front_addr = config.trade_front
        self.market_data_addr = config.market_front
        self.broker_id = config.broker_id
        self.user_id = config.user_id
        self.app_id = config.app_id

        self._adapter = validate_adapter(adapter) if adapter is not None else None
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._state = (
            GatewayState.DISABLED
            if config.effective_mode == TradingMode.DISABLED
            else GatewayState.DISCONNECTED
        )
        self._listeners: dict[str, list[Callable[[object], None]]] = defaultdict(list)
        self._last_event_at = self._clock()
        self._last_error = ""
        self._manual_stop = False
        self._market_ready = False
        self._trading_logged_in = False
        self._settlement_confirmed = False
        self._sync_started = False
        self._sync_complete = {
            "account": False,
            "positions": False,
            "orders": False,
            "trades": False,
        }
        self._position_sync_buffer: dict[str, CtpPositionSnapshot] = {}
        self._reconnect_attempts = 0
        self._next_reconnect_at: float | None = None
        self._live_armed_until = 0.0
        self._session_tag = uuid.uuid4().hex[:8].upper()
        self._order_sequence = 0
        self._subscriptions: set[str] = set()
        self._bars: dict[str, Bar] = {}
        self._orders: dict[str, CtpOrderUpdate] = {}
        self._order_fingerprints: set[tuple[object, ...]] = set()
        self._trades: list[Trade] = []
        self._trade_updates: list[CtpTradeUpdate] = []
        self._seen_trades: set[tuple[str, str, str]] = set()
        self._positions: dict[str, CtpPositionSnapshot] = {}
        self._account: CtpAccountSnapshot | None = None
        self._instruments: dict[str, CtpInstrumentSnapshot] = {
            symbol: CtpInstrumentSnapshot(
                symbol=symbol,
                exchange_id=item.exchange_id,
                volume_multiple=item.volume_multiple,
                long_margin_rate=item.margin_rate,
                short_margin_rate=item.margin_rate,
            )
            for symbol, item in config.instruments.items()
        }
        self._pending_requests: dict[str, CtpOrderRequest] = {}
        self._broker_order_ids: dict[str, str] = {}
        self._order_reasons: dict[str, str] = {}
        self._daily_order_requests: dict[date, int] = defaultdict(int)

    @property
    def state(self) -> GatewayState:
        with self._lock:
            return self._state

    @property
    def connected(self) -> bool:
        return self.state == GatewayState.READY

    @property
    def live_arm_phrase(self) -> str:
        """Human confirmation text; reading it does not arm the gateway."""

        return _LIVE_ARM_PHRASE

    def add_callback(self, event: str, callback: Callable[[object], None]) -> None:
        """Register a UI/service callback for normalized gateway events.

        Events are ``state``, ``bar``, ``order``, ``trade``, ``position``,
        ``account``, and ``error``. Listener exceptions are isolated from the
        CTP callback thread.
        """

        allowed = {"state", "bar", "order", "trade", "position", "account", "error"}
        if event not in allowed:
            raise ValueError(f"Unsupported CTP callback event: {event!r}.")
        if not callable(callback):
            raise TypeError("callback must be callable.")
        with self._lock:
            self._listeners[event].append(callback)

    def remove_callback(self, event: str, callback: Callable[[object], None]) -> None:
        with self._lock:
            if callback in self._listeners.get(event, []):
                self._listeners[event].remove(callback)

    def connect(self) -> None:
        with self._lock:
            if self._state in {
                GatewayState.CONNECTING,
                GatewayState.AUTHENTICATING,
                GatewayState.LOGGING_IN,
                GatewayState.SETTLEMENT_CONFIRMING,
                GatewayState.SYNCING,
                GatewayState.READY,
            }:
                return
            self.config.validate_for_connection()
            if self._adapter is None:
                self._adapter = load_ctp_adapter(self.config.adapter)
            self._manual_stop = False
            self._reconnect_attempts = 0
            self._start_connection(is_reconnect=False)

    def disconnect(self) -> None:
        adapter: CtpAdapter | None
        with self._lock:
            self._manual_stop = True
            self._next_reconnect_at = None
            self._disarm_locked()
            self._market_ready = False
            self._trading_logged_in = False
            self._set_state_locked(GatewayState.STOPPED)
            adapter = self._adapter
        if adapter is not None:
            try:
                adapter.disconnect()
            except Exception as exc:
                self._record_error(
                    CtpGatewayError("disconnect", type(exc).__name__, "adapter")
                )

    def subscribe(self, symbol: str) -> None:
        normalized = symbol.strip()
        if not normalized:
            raise ValueError("symbol cannot be empty.")
        with self._lock:
            if not self._market_ready:
                raise CtpGatewayStateError("CTP market-data session is not ready.")
            if normalized in self._subscriptions:
                return
            adapter = self._require_adapter_locked()
            try:
                adapter.subscribe(normalized)
            except Exception as exc:
                raise CtpGatewayStateError(
                    f"Market-data subscription failed: {type(exc).__name__}."
                ) from exc
            self._subscriptions.add(normalized)

    def send_order(self, order: Order) -> str:
        with self._lock:
            self._ensure_order_submission_allowed_locked()
            client_order_id = self._next_client_order_id_locked()
            request = self._map_order_locked(order, client_order_id)
            self._check_risk_locked(request, order.datetime.date())
            adapter = self._require_adapter_locked()
            self._pending_requests[client_order_id] = request
            self._order_reasons[client_order_id] = order.reason
            self._daily_order_requests[order.datetime.date()] += 1
            provisional = CtpOrderUpdate(
                order_id=client_order_id,
                client_order_id=client_order_id,
                symbol=request.symbol,
                exchange_id=request.exchange_id,
                direction=request.direction,
                offset=request.offset,
                total_volume=request.volume,
                traded_volume=0,
                limit_price=request.limit_price,
                status=CtpOrderStatus.SUBMITTING,
                update_time=order.datetime,
            )
            self._orders[client_order_id] = provisional
            try:
                broker_order_id = adapter.send_order(request)
            except Exception as exc:
                self._pending_requests.pop(client_order_id, None)
                self._orders.pop(client_order_id, None)
                raise CtpOrderRejected(
                    f"CTP adapter rejected the request locally: {type(exc).__name__}."
                ) from exc
            external_id = str(broker_order_id or client_order_id)
            self._broker_order_ids[client_order_id] = external_id
            return external_id

    def cancel_order(self, order_id: str) -> None:
        normalized = str(order_id).strip()
        if not normalized:
            raise ValueError("order_id cannot be empty.")
        with self._lock:
            if self._state != GatewayState.READY or not self._trading_logged_in:
                raise CtpGatewayStateError("CTP trading session is not ready.")
            client_id = self._find_client_order_id_locked(normalized)
            request = self._pending_requests.get(client_id)
            update = self._orders.get(client_id)
            if request is None and update is None:
                raise CtpOrderRejected(f"Unknown local CTP order id: {normalized!r}.")
            symbol = request.symbol if request else update.symbol  # type: ignore[union-attr]
            exchange = request.exchange_id if request else update.exchange_id  # type: ignore[union-attr]
            broker_id = self._broker_order_ids.get(client_id, normalized)
            adapter = self._require_adapter_locked()
            adapter.cancel_order(
                CtpCancelRequest(client_id, broker_id, symbol, exchange)
            )

    def latest_bar(self, symbol: str) -> Bar | None:
        with self._lock:
            if not self._market_ready:
                raise CtpGatewayStateError("CTP market-data session is not ready.")
            return self._bars.get(symbol)

    def trades(self) -> list[Trade]:
        with self._lock:
            return list(self._trades)

    def trade_updates(self) -> list[CtpTradeUpdate]:
        with self._lock:
            return list(self._trade_updates)

    def orders(self) -> list[CtpOrderUpdate]:
        with self._lock:
            return list(self._orders.values())

    def positions(self) -> list[CtpPositionSnapshot]:
        with self._lock:
            return list(self._positions.values())

    def account(self) -> CtpAccountSnapshot | None:
        with self._lock:
            return self._account

    def status(self) -> CtpGatewayStatus:
        with self._lock:
            now = self._clock()
            return CtpGatewayStatus(
                state=self._state,
                mode=self.config.effective_mode,
                connected=self._state == GatewayState.READY,
                market_ready=self._market_ready,
                trading_ready=self._state == GatewayState.READY
                and self._trading_logged_in,
                live_armed=self._is_live_armed_locked(now),
                reconnect_attempts=self._reconnect_attempts,
                subscriptions=tuple(sorted(self._subscriptions)),
                last_event_age_seconds=max(0.0, now - self._last_event_at),
                last_error=self._last_error,
            )

    def arm_live_trading(self, confirmation: str) -> datetime:
        """Temporarily arm live orders after config-level live enablement.

        The arm is cleared on disconnect/reconnect and expires automatically.
        Cancels remain available while disarmed because they reduce operational
        risk. This method never connects or submits an order.
        """

        with self._lock:
            if self.config.mode != TradingMode.LIVE:
                raise CtpGatewayStateError("Runtime arming is available only in live mode.")
            if not self.config.live_trading_enabled:
                raise CtpGatewayStateError("Live trading is disabled in configuration.")
            if self._state != GatewayState.READY:
                raise CtpGatewayStateError("CTP gateway must be READY before arming.")
            if confirmation != _LIVE_ARM_PHRASE:
                raise CtpGatewayStateError("Live arm confirmation text did not match.")
            self._live_armed_until = (
                self._clock() + self.config.live_arm_timeout_seconds
            )
            return datetime.fromtimestamp(
                time.time() + self.config.live_arm_timeout_seconds
            )

    def disarm_live_trading(self) -> None:
        with self._lock:
            self._disarm_locked()

    def poll(self) -> GatewayState:
        """Run heartbeat expiry and scheduled reconnect work without a thread."""

        adapter_to_disconnect: CtpAdapter | None = None
        reconnect = False
        with self._lock:
            now = self._clock()
            if self._live_armed_until and now >= self._live_armed_until:
                self._disarm_locked()
            if (
                self._state == GatewayState.READY
                and now - self._last_event_at
                > self.config.heartbeat_timeout_seconds
            ):
                adapter_to_disconnect = self._adapter
                self._schedule_reconnect_locked("heartbeat timeout")
            if (
                self._state == GatewayState.RECONNECT_WAIT
                and self._next_reconnect_at is not None
                and now >= self._next_reconnect_at
            ):
                reconnect = True
        if adapter_to_disconnect is not None:
            try:
                adapter_to_disconnect.disconnect()
            except Exception:
                pass
        if reconnect:
            with self._lock:
                if self._state == GatewayState.RECONNECT_WAIT:
                    self._start_connection(is_reconnect=True)
        return self.state

    # Adapter callback boundary -------------------------------------------------

    def on_adapter_stage(self, stage: CtpAdapterStage) -> None:
        begin_sync = False
        resubscribe: tuple[str, ...] = ()
        with self._lock:
            self._touch_locked()
            if stage == CtpAdapterStage.MARKET_CONNECTED:
                return
            if stage == CtpAdapterStage.TRADING_CONNECTED:
                self._set_state_locked(
                    GatewayState.AUTHENTICATING
                    if self.config.app_id or self.config.auth_code
                    else GatewayState.LOGGING_IN
                )
            elif stage == CtpAdapterStage.AUTHENTICATED:
                self._set_state_locked(GatewayState.LOGGING_IN)
            elif stage == CtpAdapterStage.MARKET_LOGGED_IN:
                self._market_ready = True
                resubscribe = tuple(sorted(self._subscriptions))
            elif stage == CtpAdapterStage.TRADING_LOGGED_IN:
                self._trading_logged_in = True
                if self.config.settlement_confirmation_required:
                    self._set_state_locked(GatewayState.SETTLEMENT_CONFIRMING)
                else:
                    begin_sync = True
            elif stage == CtpAdapterStage.SETTLEMENT_CONFIRMED:
                self._settlement_confirmed = True
                begin_sync = True
            elif stage == CtpAdapterStage.HEARTBEAT:
                return
        if resubscribe:
            adapter = self._adapter
            if adapter is not None:
                for symbol in resubscribe:
                    try:
                        adapter.subscribe(symbol)
                    except Exception as exc:
                        self._record_error(
                            CtpGatewayError(
                                "resubscribe", type(exc).__name__, "market_data", True
                            )
                        )
        if begin_sync:
            self._begin_sync()
        else:
            with self._lock:
                self._evaluate_ready_locked()

    def on_adapter_disconnected(
        self, channel: CtpChannel, reason: str, *, retryable: bool = True
    ) -> None:
        with self._lock:
            self._touch_locked()
            self._market_ready = False
            self._trading_logged_in = False
            self._disarm_locked()
            safe_reason = self._redact_text(reason)
            if self._manual_stop:
                self._set_state_locked(GatewayState.STOPPED)
            elif retryable:
                self._schedule_reconnect_locked(
                    f"{channel.value} disconnected: {safe_reason}"
                )
            else:
                self._last_error = safe_reason
                self._set_state_locked(GatewayState.ERROR)

    def on_heartbeat(self, channel: CtpChannel) -> None:
        del channel
        with self._lock:
            self._touch_locked()

    def on_bar(self, bar: Bar) -> None:
        with self._lock:
            self._touch_locked()
            if bar.symbol not in self._subscriptions:
                return
            previous = self._bars.get(bar.symbol)
            if previous is not None and bar.datetime < previous.datetime:
                return
            self._bars[bar.symbol] = bar
        self._emit("bar", bar)

    def on_order_update(self, update: CtpOrderUpdate) -> None:
        safe_update = replace(
            update, status_message=self._redact_text(update.status_message)
        )
        fingerprint = (
            safe_update.order_id,
            safe_update.client_order_id,
            safe_update.status,
            safe_update.traded_volume,
            safe_update.update_time,
            safe_update.status_message,
        )
        with self._lock:
            self._touch_locked()
            if fingerprint in self._order_fingerprints:
                return
            self._order_fingerprints.add(fingerprint)
            client_id = safe_update.client_order_id or safe_update.order_id
            self._orders[client_id] = safe_update
            self._broker_order_ids[client_id] = safe_update.order_id
            if safe_update.status.terminal:
                self._pending_requests.pop(client_id, None)
        self._emit("order", safe_update)

    def on_order_query_complete(self) -> None:
        with self._lock:
            self._touch_locked()
            self._sync_complete["orders"] = True
            self._evaluate_ready_locked()

    def on_trade_update(self, update: CtpTradeUpdate) -> None:
        key = (update.trading_day, update.exchange_id, update.trade_id)
        with self._lock:
            self._touch_locked()
            if key in self._seen_trades:
                return
            if update.volume <= 0 or not math.isfinite(update.price) or update.price <= 0:
                self._last_error = "Invalid normalized trade callback ignored."
                return
            self._seen_trades.add(key)
            self._trade_updates.append(update)
            signed_quantity = (
                update.volume
                if update.direction == CtpDirection.BUY
                else -update.volume
            )
            reason = self._order_reasons.get(update.client_order_id, update.offset.value)
            trade = Trade(
                datetime=update.trade_time,
                symbol=update.symbol,
                quantity=signed_quantity,
                price=update.price,
                commission=update.commission,
                reason=reason,
            )
            self._trades.append(trade)
            position = (
                self._positions.get(update.symbol)
                if update.is_query
                else self._apply_trade_to_position_locked(update)
            )
        self._emit("trade", update)
        if position is not None and not update.is_query:
            self._emit("position", position)

    def on_trade_query_complete(self) -> None:
        with self._lock:
            self._touch_locked()
            self._sync_complete["trades"] = True
            self._evaluate_ready_locked()

    def on_position(
        self, position: CtpPositionSnapshot | None, *, is_last: bool
    ) -> None:
        completed: list[CtpPositionSnapshot] | None = None
        with self._lock:
            self._touch_locked()
            if position is not None:
                self._position_sync_buffer[position.symbol] = position
            if is_last:
                self._positions = dict(self._position_sync_buffer)
                self._position_sync_buffer.clear()
                self._sync_complete["positions"] = True
                completed = list(self._positions.values())
                self._evaluate_ready_locked()
        if position is not None:
            self._emit("position", position)
        elif completed is not None:
            self._emit("position", tuple(completed))

    def on_account(
        self, account: CtpAccountSnapshot | None, *, is_last: bool
    ) -> None:
        with self._lock:
            self._touch_locked()
            if account is not None:
                self._account = account
            if is_last:
                self._sync_complete["account"] = True
                self._evaluate_ready_locked()
        if account is not None:
            self._emit("account", account)

    def on_instrument(self, instrument: CtpInstrumentSnapshot) -> None:
        if instrument.volume_multiple <= 0:
            return
        with self._lock:
            self._touch_locked()
            self._instruments[instrument.symbol] = instrument

    def on_adapter_error(self, error: CtpGatewayError) -> None:
        self._record_error(
            replace(error, message=self._redact_text(error.message))
        )

    # Internal state/risk helpers ----------------------------------------------

    def _start_connection(self, *, is_reconnect: bool) -> None:
        adapter = self._require_adapter_locked()
        if is_reconnect:
            self._reconnect_attempts += 1
        self._next_reconnect_at = None
        self._disarm_locked()
        self._market_ready = False
        self._trading_logged_in = False
        self._settlement_confirmed = False
        self._sync_started = False
        self._sync_complete = {name: False for name in self._sync_complete}
        self._position_sync_buffer.clear()
        self._session_tag = uuid.uuid4().hex[:8].upper()
        self._set_state_locked(GatewayState.CONNECTING)
        connection = CtpConnectionInfo(
            broker_id=self.config.broker_id,
            user_id=self.config.user_id,
            trade_front=self.config.trade_front,
            market_front=self.config.market_front,
            app_id=self.config.app_id,
            api_version=self.config.api_version,
        )
        credentials = CtpCredentials(self.config.password, self.config.auth_code)
        try:
            adapter.connect(connection, credentials, self)
        except Exception as exc:
            self._last_error = f"CTP adapter connect failed: {type(exc).__name__}."
            if is_reconnect:
                self._schedule_reconnect_locked(self._last_error)
                return
            self._set_state_locked(GatewayState.ERROR)
            raise CtpGatewayStateError(self._last_error) from exc

    def _begin_sync(self) -> None:
        with self._lock:
            if self._sync_started:
                return
            self._sync_started = True
            self._set_state_locked(GatewayState.SYNCING)
            self._position_sync_buffer.clear()
            adapter = self._require_adapter_locked()
        try:
            adapter.query_account()
            adapter.query_positions()
            adapter.query_orders()
            adapter.query_trades()
        except Exception as exc:
            with self._lock:
                self._last_error = f"CTP initial account sync failed: {type(exc).__name__}."
                self._set_state_locked(GatewayState.ERROR)

    def _evaluate_ready_locked(self) -> None:
        settlement_ok = (
            self._settlement_confirmed
            or not self.config.settlement_confirmation_required
        )
        if (
            self._market_ready
            and self._trading_logged_in
            and settlement_ok
            and all(self._sync_complete.values())
        ):
            self._reconnect_attempts = 0
            self._set_state_locked(GatewayState.READY)

    def _ensure_order_submission_allowed_locked(self) -> None:
        if self.config.effective_mode == TradingMode.DISABLED:
            raise CtpOrderRejected("CTP order submission is disabled.")
        if self._state != GatewayState.READY or not self._trading_logged_in:
            raise CtpOrderRejected("CTP trading session is not ready.")
        if self.config.mode == TradingMode.LIVE:
            if not self.config.live_trading_enabled:
                raise CtpOrderRejected("Live trading is disabled in configuration.")
            if not self.config.programmatic_trading_report_confirmed:
                raise CtpOrderRejected(
                    "Programmatic-trading report has not been confirmed."
                )
            if not self._is_live_armed_locked(self._clock()):
                raise CtpOrderRejected(
                    "Live trading is not armed or the runtime arm expired."
                )

    def _map_order_locked(self, order: Order, client_id: str) -> CtpOrderRequest:
        if not order.symbol.strip():
            raise CtpOrderRejected("Order symbol cannot be empty.")
        if order.quantity == 0:
            raise CtpOrderRejected("Order quantity cannot be zero.")
        if not isinstance(order.quantity, int):
            raise CtpOrderRejected("Order quantity must be an integer number of lots.")
        if not math.isfinite(order.price) or order.price <= 0:
            raise CtpOrderRejected("Order price must be finite and positive.")
        instrument = self._instruments.get(order.symbol)
        if instrument is None:
            raise CtpOrderRejected(
                f"Missing authoritative contract metadata for {order.symbol}; "
                "opening risk cannot be calculated safely."
            )
        if instrument.upper_limit_price and order.price > instrument.upper_limit_price:
            raise CtpOrderRejected("Order price exceeds the current upper limit.")
        if instrument.lower_limit_price and order.price < instrument.lower_limit_price:
            raise CtpOrderRejected("Order price is below the current lower limit.")
        position = self._positions.get(
            order.symbol,
            CtpPositionSnapshot(order.symbol, instrument.exchange_id),
        )
        if position.long_quantity and position.short_quantity:
            raise CtpOrderRejected(
                "Hedged long/short position requires an explicit offset API; "
                "the generic Order model is ambiguous."
            )
        volume = abs(order.quantity)
        if order.quantity > 0:
            direction = CtpDirection.BUY
            if position.short_quantity:
                remaining = position.short_quantity - self._pending_close_volume_locked(
                    order.symbol, CtpDirection.BUY
                )
                if volume > remaining:
                    raise CtpOrderRejected(
                        "A generic order cannot close and reverse in one request; split it."
                    )
                offset = CtpOffset.CLOSE
            else:
                offset = CtpOffset.OPEN
        else:
            direction = CtpDirection.SELL
            if position.long_quantity:
                remaining = position.long_quantity - self._pending_close_volume_locked(
                    order.symbol, CtpDirection.SELL
                )
                if volume > remaining:
                    raise CtpOrderRejected(
                        "A generic order cannot close and reverse in one request; split it."
                    )
                offset = CtpOffset.CLOSE
            else:
                offset = CtpOffset.OPEN
        return CtpOrderRequest(
            client_order_id=client_id,
            symbol=order.symbol,
            exchange_id=instrument.exchange_id,
            direction=direction,
            offset=offset,
            volume=volume,
            limit_price=float(order.price),
            reason=order.reason,
        )

    def _check_risk_locked(self, request: CtpOrderRequest, request_date: date) -> None:
        limits = self.config.risk_limits
        if request.volume > limits.max_order_volume:
            raise CtpOrderRejected(
                f"Order volume exceeds max_order_volume={limits.max_order_volume}."
            )
        pending_count = sum(
            1
            for update in self._orders.values()
            if not update.status.terminal
        )
        if pending_count >= limits.max_pending_orders:
            raise CtpOrderRejected("Maximum pending-order count reached.")
        if self._daily_order_requests[request_date] >= limits.max_daily_order_requests:
            raise CtpOrderRejected("Daily order-request limit reached.")
        if request.offset != CtpOffset.OPEN:
            return
        account = self._account
        if account is None or account.balance <= 0:
            raise CtpOrderRejected("Authoritative account balance is unavailable.")
        instrument = self._instruments[request.symbol]
        margin_rate = (
            instrument.long_margin_rate
            if request.direction == CtpDirection.BUY
            else instrument.short_margin_rate
        )
        if instrument.volume_multiple <= 0 or not 0 < margin_rate <= 1:
            raise CtpOrderRejected("Contract multiplier or margin rate is unavailable.")
        position = self._positions.get(request.symbol)
        current_side = 0
        if position:
            current_side = (
                position.long_quantity
                if request.direction == CtpDirection.BUY
                else position.short_quantity
            )
        pending_side = sum(
            self._remaining_volume_locked(item)
            for item in self._pending_requests.values()
            if item.symbol == request.symbol
            and item.offset == CtpOffset.OPEN
            and item.direction == request.direction
        )
        if current_side + pending_side + request.volume > limits.max_abs_position_per_symbol:
            raise CtpOrderRejected("Per-symbol position limit would be exceeded.")
        active_symbols = {
            symbol for symbol, value in self._positions.items() if not value.is_flat
        }
        active_symbols.update(
            item.symbol
            for item in self._pending_requests.values()
            if item.offset == CtpOffset.OPEN
        )
        if request.symbol not in active_symbols and len(active_symbols) >= limits.max_open_symbols:
            raise CtpOrderRejected("Maximum number of open symbols reached.")
        new_margin = (
            request.limit_price
            * request.volume
            * instrument.volume_multiple
            * margin_rate
        )
        pending_margin = sum(
            self._estimate_request_margin_locked(item)
            for item in self._pending_requests.values()
            if item.offset == CtpOffset.OPEN
        )
        symbol_pending_margin = sum(
            self._estimate_request_margin_locked(item)
            for item in self._pending_requests.values()
            if item.offset == CtpOffset.OPEN and item.symbol == request.symbol
        )
        symbol_margin = position.used_margin if position else 0.0
        if (
            symbol_margin + symbol_pending_margin + new_margin
            > account.balance * limits.max_symbol_margin_fraction
        ):
            raise CtpOrderRejected("Per-symbol margin fraction would be exceeded.")
        if (
            account.current_margin
            + account.frozen_margin
            + pending_margin
            + new_margin
            > account.balance * limits.max_total_margin_fraction
        ):
            raise CtpOrderRejected("Total margin fraction would be exceeded.")

    def _estimate_request_margin_locked(self, request: CtpOrderRequest) -> float:
        instrument = self._instruments.get(request.symbol)
        if instrument is None:
            return math.inf
        rate = (
            instrument.long_margin_rate
            if request.direction == CtpDirection.BUY
            else instrument.short_margin_rate
        )
        return (
            request.limit_price
            * self._remaining_volume_locked(request)
            * instrument.volume_multiple
            * rate
        )

    def _remaining_volume_locked(self, request: CtpOrderRequest) -> int:
        update = self._orders.get(request.client_order_id)
        traded = update.traded_volume if update is not None else 0
        return max(0, request.volume - traded)

    def _pending_close_volume_locked(
        self, symbol: str, direction: CtpDirection
    ) -> int:
        return sum(
            self._remaining_volume_locked(item)
            for item in self._pending_requests.values()
            if item.symbol == symbol
            and item.offset != CtpOffset.OPEN
            and item.direction == direction
        )

    def _apply_trade_to_position_locked(
        self, update: CtpTradeUpdate
    ) -> CtpPositionSnapshot:
        old = self._positions.get(
            update.symbol,
            CtpPositionSnapshot(update.symbol, update.exchange_id),
        )
        long_qty, short_qty = old.long_quantity, old.short_quantity
        today_long, today_short = old.today_long, old.today_short
        avg_long, avg_short = old.avg_long_price, old.avg_short_price
        used_margin = old.used_margin
        instrument = self._instruments.get(update.symbol)
        rate = 0.0
        multiplier = 0
        if instrument is not None:
            multiplier = instrument.volume_multiple
            if update.offset == CtpOffset.OPEN:
                rate = (
                    instrument.long_margin_rate
                    if update.direction == CtpDirection.BUY
                    else instrument.short_margin_rate
                )
            else:
                rate = (
                    instrument.short_margin_rate
                    if update.direction == CtpDirection.BUY
                    else instrument.long_margin_rate
                )
        if update.offset == CtpOffset.OPEN:
            margin_delta = update.price * update.volume * multiplier * rate
        else:
            closing_side = (
                old.short_quantity
                if update.direction == CtpDirection.BUY
                else old.long_quantity
            )
            margin_delta = (
                old.used_margin * min(update.volume, closing_side) / closing_side
                if closing_side > 0
                else 0.0
            )
        if update.offset == CtpOffset.OPEN and update.direction == CtpDirection.BUY:
            avg_long = (
                (avg_long * long_qty + update.price * update.volume)
                / (long_qty + update.volume)
            )
            long_qty += update.volume
            today_long += update.volume
            used_margin += margin_delta
        elif update.offset == CtpOffset.OPEN:
            avg_short = (
                (avg_short * short_qty + update.price * update.volume)
                / (short_qty + update.volume)
            )
            short_qty += update.volume
            today_short += update.volume
            used_margin += margin_delta
        elif update.direction == CtpDirection.BUY:
            short_qty = max(0, short_qty - update.volume)
            today_short = min(today_short, short_qty)
            if short_qty == 0:
                avg_short = 0.0
            used_margin = max(0.0, used_margin - margin_delta)
        else:
            long_qty = max(0, long_qty - update.volume)
            today_long = min(today_long, long_qty)
            if long_qty == 0:
                avg_long = 0.0
            used_margin = max(0.0, used_margin - margin_delta)
        result = CtpPositionSnapshot(
            symbol=update.symbol,
            exchange_id=update.exchange_id,
            long_quantity=long_qty,
            short_quantity=short_qty,
            today_long=today_long,
            today_short=today_short,
            avg_long_price=avg_long,
            avg_short_price=avg_short,
            used_margin=used_margin,
            updated_at=update.trade_time,
        )
        self._positions[update.symbol] = result
        if self._account is not None:
            account_margin = self._account.current_margin
            if update.offset == CtpOffset.OPEN:
                account_margin += margin_delta
            else:
                account_margin = max(0.0, account_margin - margin_delta)
            self._account = replace(
                self._account,
                current_margin=account_margin,
                updated_at=update.trade_time,
            )
        return result

    def _schedule_reconnect_locked(self, reason: str) -> None:
        self._disarm_locked()
        self._market_ready = False
        self._trading_logged_in = False
        self._last_error = self._redact_text(reason)
        if self._reconnect_attempts >= self.config.reconnect_max_attempts:
            self._next_reconnect_at = None
            self._set_state_locked(GatewayState.ERROR)
            return
        delay = min(
            self.config.reconnect_max_delay_seconds,
            self.config.reconnect_initial_delay_seconds
            * (2**self._reconnect_attempts),
        )
        self._next_reconnect_at = self._clock() + delay
        self._set_state_locked(GatewayState.RECONNECT_WAIT)

    def _record_error(self, error: CtpGatewayError) -> None:
        safe = replace(error, message=self._redact_text(error.message))
        with self._lock:
            self._touch_locked()
            self._last_error = f"{safe.source}:{safe.code}: {safe.message}"
            if self._state != GatewayState.READY and not safe.retryable:
                self._set_state_locked(GatewayState.ERROR)
        self._emit("error", safe)

    def _redact_text(self, value: str) -> str:
        result = str(value)
        for secret in (self.config.password.reveal(), self.config.auth_code.reveal()):
            if secret:
                result = result.replace(secret, "<redacted>")
        return result

    def _next_client_order_id_locked(self) -> str:
        self._order_sequence += 1
        return f"FQ-{self._session_tag}-{self._order_sequence:08d}"

    def _find_client_order_id_locked(self, value: str) -> str:
        if value in self._orders or value in self._pending_requests:
            return value
        for client_id, broker_id in self._broker_order_ids.items():
            if broker_id == value:
                return client_id
        return value

    def _require_adapter_locked(self) -> CtpAdapter:
        if self._adapter is None:
            raise CtpGatewayStateError("CTP SDK adapter is not initialized.")
        return self._adapter

    def _touch_locked(self) -> None:
        self._last_event_at = self._clock()

    def _set_state_locked(self, state: GatewayState) -> None:
        changed = state != self._state
        self._state = state
        if changed:
            self._emit("state", state)

    def _emit(self, event: str, payload: object) -> None:
        with self._lock:
            listeners = tuple(self._listeners.get(event, ()))
        for callback in listeners:
            try:
                callback(payload)
            except Exception:
                # SDK callback threads must never be broken by presentation code.
                continue

    def _is_live_armed_locked(self, now: float) -> bool:
        if self.config.mode != TradingMode.LIVE:
            return False
        if self._live_armed_until and now >= self._live_armed_until:
            self._disarm_locked()
        return self._live_armed_until > now

    def _disarm_locked(self) -> None:
        self._live_armed_until = 0.0
