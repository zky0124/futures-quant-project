import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from futures_quant.api.ctp_adapter import (
    CtpAccountSnapshot,
    CtpAdapterStage,
    CtpChannel,
    CtpDirection,
    CtpGatewayError,
    CtpOffset,
    CtpOrderStatus,
    CtpOrderUpdate,
    CtpTradeUpdate,
    CtpSdkUnavailable,
)
from futures_quant.api.ctp_config import (
    CtpConfig,
    CtpConfigurationError,
    CtpInstrumentConfig,
    CtpRiskLimits,
    SecretValue,
    TradingMode,
    load_ctp_config,
)
from futures_quant.api.ctp_gateway import (
    CtpGateway,
    CtpGatewayStateError,
    CtpOrderRejected,
    GatewayState,
)
from futures_quant.models import Bar, Order
from futures_quant.cli import main


class FakeCtpAdapter:
    """Deterministic in-process adapter; it performs no I/O."""

    def __init__(self, *, auto_ready: bool = True) -> None:
        self.auto_ready = auto_ready
        self.callbacks = None
        self.connection = None
        self.credentials = None
        self.connect_count = 0
        self.disconnect_count = 0
        self.subscriptions = []
        self.sent_orders = []
        self.cancelled_orders = []
        self.query_counts = {
            "account": 0,
            "positions": 0,
            "orders": 0,
            "trades": 0,
        }

    def connect(self, connection, credentials, callbacks) -> None:
        self.connection = connection
        self.credentials = credentials
        self.callbacks = callbacks
        self.connect_count += 1
        if self.auto_ready:
            self.bootstrap()

    def bootstrap(self) -> None:
        cb = self.callbacks
        cb.on_adapter_stage(CtpAdapterStage.MARKET_CONNECTED)
        cb.on_adapter_stage(CtpAdapterStage.TRADING_CONNECTED)
        cb.on_adapter_stage(CtpAdapterStage.AUTHENTICATED)
        cb.on_adapter_stage(CtpAdapterStage.MARKET_LOGGED_IN)
        cb.on_adapter_stage(CtpAdapterStage.TRADING_LOGGED_IN)
        cb.on_adapter_stage(CtpAdapterStage.SETTLEMENT_CONFIRMED)

    def disconnect(self) -> None:
        self.disconnect_count += 1

    def subscribe(self, symbol) -> None:
        self.subscriptions.append(symbol)

    def send_order(self, request):
        self.sent_orders.append(request)
        return f"BROKER-{len(self.sent_orders)}"

    def cancel_order(self, request) -> None:
        self.cancelled_orders.append(request)

    def query_account(self) -> None:
        self.query_counts["account"] += 1
        self.callbacks.on_account(
            CtpAccountSnapshot(
                account_id="FAKE",
                balance=100_000.0,
                available=100_000.0,
                current_margin=0.0,
                trading_day="20260714",
            ),
            is_last=True,
        )

    def query_positions(self) -> None:
        self.query_counts["positions"] += 1
        self.callbacks.on_position(None, is_last=True)

    def query_orders(self) -> None:
        self.query_counts["orders"] += 1
        self.callbacks.on_order_query_complete()

    def query_trades(self) -> None:
        self.query_counts["trades"] += 1
        self.callbacks.on_trade_query_complete()


def paper_config(**overrides) -> CtpConfig:
    values = dict(
        broker_id="TEST",
        trade_front="tcp://127.0.0.1:1",
        market_front="tcp://127.0.0.1:2",
        user_id="FAKE_USER",
        password=SecretValue("FAKE_PASSWORD"),
        app_id="FAKE_APP",
        auth_code=SecretValue("FAKE_AUTH"),
        mode=TradingMode.PAPER,
        enabled=True,
        risk_limits=CtpRiskLimits(),
        instruments={
            "RB2610": CtpInstrumentConfig("RB2610", "SHFE", 10, 0.10),
            "I2609": CtpInstrumentConfig("I2609", "DCE", 100, 0.10),
        },
        heartbeat_timeout_seconds=5.0,
        reconnect_initial_delay_seconds=1.0,
        reconnect_max_delay_seconds=4.0,
    )
    values.update(overrides)
    return CtpConfig(**values)


class CtpConfigTest(unittest.TestCase):
    def test_default_total_margin_limit_is_sixty_percent(self) -> None:
        self.assertEqual(CtpRiskLimits().max_symbol_margin_fraction, 0.20)
        self.assertEqual(CtpRiskLimits().max_total_margin_fraction, 0.60)

    def test_environment_secrets_are_redacted(self) -> None:
        raw = {
            "gateway": "ctp",
            "broker_id": "4300",
            "trade_front": "tcp://127.0.0.1:1",
            "market_front": "tcp://127.0.0.1:2",
            "user_id_env": "TEST_CTP_USER",
            "password_env": "TEST_CTP_PASSWORD",
            "app_id_env": "TEST_CTP_APP",
            "auth_code_env": "TEST_CTP_AUTH",
            "mode": "paper",
            "enabled": False,
        }
        env = {
            "TEST_CTP_USER": "12345678",
            "TEST_CTP_PASSWORD": "TOP_SECRET_PASSWORD",
            "TEST_CTP_APP": "APP",
            "TEST_CTP_AUTH": "TOP_SECRET_AUTH",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ctp.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            cfg = load_ctp_config(path, environ=env)
        rendered = repr(cfg) + repr(cfg.redacted_summary())
        self.assertNotIn("TOP_SECRET_PASSWORD", rendered)
        self.assertNotIn("TOP_SECRET_AUTH", rendered)
        self.assertEqual(cfg.password.reveal(), "TOP_SECRET_PASSWORD")
        self.assertEqual(cfg.auth_code.reveal(), "TOP_SECRET_AUTH")
        self.assertEqual(cfg.redacted_summary()["user_id"], "1***8")

    def test_inline_secrets_are_rejected_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ctp.json"
            path.write_text(
                json.dumps({"gateway": "ctp", "password": "do-not-store-this"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CtpConfigurationError, "password_env"):
                load_ctp_config(path, environ={})

    def test_legacy_constructor_is_disabled(self) -> None:
        gateway = CtpGateway("tcp://127.0.0.1:1", "TEST", "USER", "PASSWORD")
        self.assertEqual(gateway.state, GatewayState.DISABLED)
        with self.assertRaisesRegex(CtpConfigurationError, "disabled"):
            gateway.connect()

    def test_missing_optional_adapter_has_clear_diagnostic(self) -> None:
        gateway = CtpGateway(config=paper_config())
        with self.assertRaisesRegex(CtpSdkUnavailable, "No CTP SDK adapter"):
            gateway.connect()

    def test_cli_diagnostic_does_not_read_credentials_or_connect(self) -> None:
        raw = {
            "gateway": "ctp",
            "broker_id": "TEST",
            "trade_front": "tcp://127.0.0.1:1",
            "market_front": "tcp://127.0.0.1:2",
            "user_id_env": "SHOULD_NOT_BE_READ",
            "password_env": "SHOULD_NOT_BE_READ",
            "mode": "paper",
            "enabled": False,
            "adapter": "",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ctp.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            captured = io.StringIO()
            with patch.object(
                sys,
                "argv",
                ["fq", "ctp-diagnose", "--config", str(path)],
            ), contextlib.redirect_stdout(captured):
                main()
        result = json.loads(captured.getvalue())
        self.assertFalse(result["credential_environment_read"])
        self.assertFalse(result["network_action"])
        self.assertFalse(result["adapter_available"])
        self.assertFalse(result["password_configured"])


class CtpGatewayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = FakeCtpAdapter()
        self.gateway = CtpGateway(config=paper_config(), adapter=self.adapter)
        self.gateway.connect()
        self.now = datetime(2026, 7, 14, 9, 1)

    def test_connect_sync_subscribe_and_market_callback(self) -> None:
        self.assertEqual(self.gateway.state, GatewayState.READY)
        self.assertTrue(all(value == 1 for value in self.adapter.query_counts.values()))
        self.gateway.subscribe("RB2610")
        self.gateway.subscribe("RB2610")
        self.assertEqual(self.adapter.subscriptions, ["RB2610"])
        bar = Bar(self.now, "RB2610", 3000, 3010, 2990, 3005, 100)
        self.adapter.callbacks.on_bar(bar)
        self.assertEqual(self.gateway.latest_bar("RB2610"), bar)
        self.assertEqual(self.gateway.status().subscriptions, ("RB2610",))

    def test_not_ready_blocks_subscription_and_orders(self) -> None:
        adapter = FakeCtpAdapter(auto_ready=False)
        gateway = CtpGateway(config=paper_config(), adapter=adapter)
        gateway.connect()
        self.assertEqual(gateway.state, GatewayState.CONNECTING)
        with self.assertRaises(CtpGatewayStateError):
            gateway.subscribe("RB2610")
        with self.assertRaisesRegex(CtpOrderRejected, "not ready"):
            gateway.send_order(Order(self.now, "RB2610", 1, 3000, "test"))

    def test_open_close_mapping_and_cross_zero_rejection(self) -> None:
        order_id = self.gateway.send_order(
            Order(self.now, "RB2610", 2, 3000, "open_long")
        )
        opening = self.adapter.sent_orders[-1]
        self.assertEqual(order_id, "BROKER-1")
        self.assertEqual(opening.direction, CtpDirection.BUY)
        self.assertEqual(opening.offset, CtpOffset.OPEN)
        self._fill(opening, "T1", "BROKER-1")

        self.gateway.send_order(
            Order(self.now, "RB2610", -1, 3010, "close_long")
        )
        closing = self.adapter.sent_orders[-1]
        self.assertEqual(closing.direction, CtpDirection.SELL)
        self.assertEqual(closing.offset, CtpOffset.CLOSE)
        with self.assertRaisesRegex(CtpOrderRejected, "close and reverse"):
            self.gateway.send_order(
                Order(self.now, "RB2610", -3, 3010, "invalid_reverse")
            )

    def test_volume_margin_pending_and_cancel_limits(self) -> None:
        with self.assertRaisesRegex(CtpOrderRejected, "max_order_volume"):
            self.gateway.send_order(
                Order(self.now, "RB2610", 6, 3000, "too_many_lots")
            )
        broker_id = self.gateway.send_order(
            Order(self.now, "RB2610", 5, 4000, "uses_20_percent")
        )
        with self.assertRaisesRegex(CtpOrderRejected, "margin fraction"):
            self.gateway.send_order(
                Order(self.now, "RB2610", 1, 4000, "over_symbol_margin")
            )
        self.gateway.cancel_order(broker_id)
        self.assertEqual(self.adapter.cancelled_orders[-1].broker_order_id, broker_id)

    def test_order_trade_callbacks_are_idempotent_and_update_position(self) -> None:
        self.gateway.send_order(Order(self.now, "RB2610", 1, 3000, "signal"))
        request = self.adapter.sent_orders[-1]
        update = self._fill(request, "T100", "BROKER-1")
        self.adapter.callbacks.on_trade_update(update)
        self.assertEqual(len(self.gateway.trades()), 1)
        self.assertEqual(self.gateway.positions()[0].long_quantity, 1)
        self.assertEqual(self.gateway.trades()[0].reason, "signal")

        queried = CtpTradeUpdate(
            trade_id="OLD",
            order_id="OLD_ORDER",
            client_order_id="",
            symbol="RB2610",
            exchange_id="SHFE",
            direction=CtpDirection.BUY,
            offset=CtpOffset.OPEN,
            volume=5,
            price=2900,
            trade_time=self.now,
            trading_day="20260714",
            is_query=True,
        )
        self.adapter.callbacks.on_trade_update(queried)
        self.assertEqual(self.gateway.positions()[0].long_quantity, 1)

    def test_adapter_error_redacts_secrets(self) -> None:
        self.adapter.callbacks.on_adapter_error(
            CtpGatewayError(
                7,
                "bad FAKE_PASSWORD and FAKE_AUTH",
                source="login",
                retryable=False,
            )
        )
        status = self.gateway.status()
        self.assertNotIn("FAKE_PASSWORD", status.last_error)
        self.assertNotIn("FAKE_AUTH", status.last_error)

    def _fill(self, request, trade_id, broker_order_id):
        order_update = CtpOrderUpdate(
            order_id=broker_order_id,
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            exchange_id=request.exchange_id,
            direction=request.direction,
            offset=request.offset,
            total_volume=request.volume,
            traded_volume=request.volume,
            limit_price=request.limit_price,
            status=CtpOrderStatus.FILLED,
            update_time=self.now,
        )
        self.adapter.callbacks.on_order_update(order_update)
        trade_update = CtpTradeUpdate(
            trade_id=trade_id,
            order_id=broker_order_id,
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            exchange_id=request.exchange_id,
            direction=request.direction,
            offset=request.offset,
            volume=request.volume,
            price=request.limit_price,
            trade_time=self.now,
            trading_day="20260714",
        )
        self.adapter.callbacks.on_trade_update(trade_update)
        return trade_update


class CtpLiveAndReconnectTest(unittest.TestCase):
    def test_live_mode_requires_runtime_arm_and_disconnect_clears_it(self) -> None:
        adapter = FakeCtpAdapter()
        cfg = paper_config(
            mode=TradingMode.LIVE,
            live_trading_enabled=True,
            programmatic_trading_report_confirmed=True,
        )
        gateway = CtpGateway(config=cfg, adapter=adapter)
        gateway.connect()
        now = datetime(2026, 7, 14, 9, 1)
        with self.assertRaisesRegex(CtpOrderRejected, "not armed"):
            gateway.send_order(Order(now, "RB2610", 1, 3000, "live"))
        with self.assertRaises(CtpGatewayStateError):
            gateway.arm_live_trading("yes")
        gateway.arm_live_trading(gateway.live_arm_phrase)
        gateway.send_order(Order(now, "RB2610", 1, 3000, "live"))
        self.assertTrue(gateway.status().live_armed)
        adapter.callbacks.on_adapter_disconnected(
            CtpChannel.TRADING, "network reset", retryable=True
        )
        self.assertFalse(gateway.status().live_armed)
        self.assertEqual(gateway.state, GatewayState.RECONNECT_WAIT)

    def test_heartbeat_poll_reconnects_and_resubscribes(self) -> None:
        current = [0.0]
        adapter = FakeCtpAdapter()
        gateway = CtpGateway(
            config=paper_config(), adapter=adapter, clock=lambda: current[0]
        )
        gateway.connect()
        gateway.subscribe("RB2610")
        current[0] = 6.0
        self.assertEqual(gateway.poll(), GatewayState.RECONNECT_WAIT)
        self.assertEqual(adapter.disconnect_count, 1)
        current[0] = 7.0
        self.assertEqual(gateway.poll(), GatewayState.READY)
        self.assertEqual(adapter.connect_count, 2)
        self.assertEqual(adapter.subscriptions.count("RB2610"), 2)


if __name__ == "__main__":
    unittest.main()
