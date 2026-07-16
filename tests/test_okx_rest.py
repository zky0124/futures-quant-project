from __future__ import annotations

import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from futures_quant.api.ctp_config import SecretValue
from futures_quant.api.okx_config import (
    OkxConfig,
    OkxConfigurationError,
    OkxEnvironment,
    load_okx_config,
)
from futures_quant.api.okx_rest import (
    OkxHttpResponse,
    OkxIdentityError,
    OkxOrderRequest,
    OkxPrivateClient,
    OkxPublicClient,
    OkxSafetyError,
)


def response(data, *, code="0", msg="", status=200):
    return OkxHttpResponse(
        status_code=status,
        headers={},
        body=json.dumps({"code": code, "msg": msg, "data": data}).encode(),
    )


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("Unexpected HTTP request")
        return self.responses.pop(0)


def private_config(**overrides):
    values = dict(
        enabled=True,
        environment=OkxEnvironment.DEMO,
        private_api_enabled=True,
        api_key=SecretValue("TEST_API_KEY"),
        secret_key=SecretValue("TEST_SECRET"),
        passphrase=SecretValue("TEST_PASSPHRASE"),
        require_subaccount=True,
    )
    values.update(overrides)
    return OkxConfig(**values)


def subaccount_config_payload(
    *, uid="SUB123", main_uid="MAIN999", perm="read_only,trade", ip="203.0.113.10"
):
    return {
        "uid": uid,
        "mainUid": main_uid,
        "label": "futures-quant",
        "perm": perm,
        "acctLv": "2",
        "posMode": "net_mode",
        "ip": ip,
    }


class OkxConfigTests(unittest.TestCase):
    def test_credentials_are_environment_only_and_redacted(self):
        raw = {
            "gateway": "okx",
            "enabled": False,
            "environment": "demo",
            "api_key_env": "TEST_OKX_KEY",
            "secret_key_env": "TEST_OKX_SECRET",
            "passphrase_env": "TEST_OKX_PASSPHRASE",
            "expected_uid_env": "TEST_OKX_UID",
        }
        env = {
            "TEST_OKX_KEY": "VISIBLE_ONLY_BY_REVEAL",
            "TEST_OKX_SECRET": "NEVER_LOG_SECRET",
            "TEST_OKX_PASSPHRASE": "NEVER_LOG_PASSPHRASE",
            "TEST_OKX_UID": "SUB123456",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "okx.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            cfg = load_okx_config(path, environ=env)
        rendered = repr(cfg) + repr(cfg.redacted_summary())
        self.assertNotIn("VISIBLE_ONLY_BY_REVEAL", rendered)
        self.assertNotIn("NEVER_LOG_SECRET", rendered)
        self.assertNotIn("NEVER_LOG_PASSPHRASE", rendered)
        self.assertEqual(cfg.api_key.reveal(), "VISIBLE_ONLY_BY_REVEAL")
        self.assertEqual(cfg.expected_uid, "SUB123456")

    def test_inline_secret_and_non_okx_base_url_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "okx.json"
            path.write_text(
                json.dumps({"gateway": "okx", "api_key": "must-not-be-here"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(OkxConfigurationError, "api_key_env"):
                load_okx_config(path, environ={})
        with self.assertRaisesRegex(OkxConfigurationError, "allowlist"):
            OkxConfig(base_url="https://example.com").validate()

    def test_order_configuration_is_fail_closed(self):
        cfg = private_config(order_submission_enabled=True)
        with self.assertRaisesRegex(OkxConfigurationError, "expected_uid"):
            cfg.validate_order_submission()


class OkxPublicAndSigningTests(unittest.TestCase):
    def test_public_market_request_has_no_private_headers(self):
        transport = FakeTransport(response([["1", "2", "3"]]))
        client = OkxPublicClient(transport=transport)
        rows = client.get_candles("BTC-USDT", bar="15m", limit=1)
        self.assertEqual(rows, [["1", "2", "3"]])
        call = transport.calls[0]
        self.assertIn("instId=BTC-USDT", call["url"])
        self.assertNotIn("OK-ACCESS-KEY", call["headers"])

    def test_public_server_time_is_available_for_clock_checks(self):
        transport = FakeTransport(response([{"ts": "1784163723456"}]))
        client = OkxPublicClient(transport=transport)
        self.assertEqual(client.get_server_time_ms(), 1784163723456)
        self.assertTrue(transport.calls[0]["url"].endswith("/api/v5/public/time"))

    def test_demo_private_signature_covers_exact_query(self):
        transport = FakeTransport(response([{"totalEq": "100"}]))
        now = datetime(2026, 7, 16, 1, 2, 3, 456000, tzinfo=timezone.utc)
        client = OkxPrivateClient(
            private_config(), transport=transport, utc_clock=lambda: now
        )
        client.get_balances(["BTC", "USDT"])
        call = transport.calls[0]
        path = "/api/v5/account/balance?ccy=BTC%2CUSDT"
        timestamp = "2026-07-16T01:02:03.456Z"
        expected = base64.b64encode(
            hmac.new(
                b"TEST_SECRET",
                (timestamp + "GET" + path).encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        self.assertEqual(call["url"], "https://openapi.okx.com" + path)
        self.assertEqual(call["headers"]["OK-ACCESS-SIGN"], expected)
        self.assertEqual(call["headers"]["x-simulated-trading"], "1")

    def test_disabled_private_client_never_reaches_transport(self):
        transport = FakeTransport()
        client = OkxPrivateClient(OkxConfig(), transport=transport)
        with self.assertRaisesRegex(OkxConfigurationError, "disabled"):
            client.get_balances()
        self.assertEqual(transport.calls, [])


class OkxIdentityAndQueryTests(unittest.TestCase):
    def test_main_account_key_is_rejected(self):
        transport = FakeTransport(
            response([subaccount_config_payload(uid="MAIN999", main_uid="MAIN999")])
        )
        client = OkxPrivateClient(private_config(), transport=transport)
        with self.assertRaisesRegex(OkxIdentityError, "main account"):
            client.verify_subaccount_identity()
        self.assertIsNone(client.identity)

    def test_exact_subaccount_uid_is_verified(self):
        transport = FakeTransport(response([subaccount_config_payload()]))
        client = OkxPrivateClient(
            private_config(expected_uid="SUB123", expected_main_uid="MAIN999"),
            transport=transport,
        )
        identity = client.verify_subaccount_identity()
        self.assertTrue(identity.is_subaccount)
        self.assertEqual(identity.permissions, ("read_only", "trade"))
        self.assertNotIn("SUB123", repr(identity.redacted_summary()))

    def test_wrong_expected_uid_is_rejected(self):
        transport = FakeTransport(response([subaccount_config_payload()]))
        client = OkxPrivateClient(
            private_config(expected_uid="DIFFERENT"), transport=transport
        )
        with self.assertRaisesRegex(OkxIdentityError, "expected_uid"):
            client.verify_subaccount_identity()

    def test_withdraw_permission_is_rejected(self):
        transport = FakeTransport(
            response([subaccount_config_payload(perm="read_only,trade,withdraw")])
        )
        client = OkxPrivateClient(private_config(), transport=transport)
        with self.assertRaisesRegex(OkxIdentityError, "Withdraw"):
            client.verify_subaccount_identity()

    def test_balance_position_order_and_fill_queries_are_available(self):
        transport = FakeTransport(
            response([{"details": []}]),
            response([{"instId": "BTC-USDT-SWAP"}]),
            response([{"ordId": "1"}]),
            response([{"ordId": "2"}]),
            response([{"tradeId": "2A"}]),
            response([{"tradeId": "3"}]),
        )
        client = OkxPrivateClient(private_config(), transport=transport)
        client.get_balances()
        client.get_positions(inst_type="swap")
        client.get_pending_orders(inst_type="swap")
        client.get_order_history(inst_type="swap", archive=True)
        client.get_recent_fills(inst_type="swap")
        client.get_fills_history(inst_type="swap")
        urls = [call["url"] for call in transport.calls]
        self.assertIn("/api/v5/account/balance", urls[0])
        self.assertIn("/api/v5/account/positions", urls[1])
        self.assertIn("/api/v5/trade/orders-pending", urls[2])
        self.assertIn("/api/v5/trade/orders-history-archive", urls[3])
        self.assertIn("/api/v5/trade/fills", urls[4])
        self.assertNotIn("fills-history", urls[4])
        self.assertIn("/api/v5/trade/fills-history", urls[5])


class OkxOrderSafetyTests(unittest.TestCase):
    def order_config(self, **overrides):
        values = dict(
            expected_uid="SUB123",
            expected_main_uid="MAIN999",
            order_submission_enabled=True,
            order_limits={"BTC-USDT-SWAP": Decimal("0.5")},
            allowed_order_types=("limit",),
            allow_opening_orders=False,
        )
        values.update(overrides)
        return private_config(**values)

    def test_order_requires_identity_arm_whitelist_and_size_limit(self):
        transport = FakeTransport(
            response([subaccount_config_payload()]),
            response(
                [
                    {
                        "ordId": "9001",
                        "clOrdId": "FQTEST1",
                        "sCode": "0",
                        "sMsg": "",
                        "ts": "1",
                        "tag": "FQWORKBENCH",
                    }
                ]
            ),
        )
        client = OkxPrivateClient(self.order_config(), transport=transport)
        order = OkxOrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="sell",
            size="0.25",
            price="70000",
            reduce_only=True,
            client_order_id="FQTEST1",
        )
        with self.assertRaisesRegex(OkxSafetyError, "identity"):
            client.place_order(order)
        client.verify_subaccount_identity()
        with self.assertRaisesRegex(OkxSafetyError, "not armed"):
            client.place_order(order)
        with self.assertRaisesRegex(OkxSafetyError, "phrase"):
            client.arm_order_submission("yes")
        client.arm_order_submission(client.order_arm_phrase)
        with self.assertRaisesRegex(OkxSafetyError, "configured limit"):
            client.place_order(
                OkxOrderRequest(
                    "BTC-USDT-SWAP", "sell", "0.51", "70000"
                )
            )
        with self.assertRaisesRegex(OkxSafetyError, "Opening orders"):
            client.place_order(
                OkxOrderRequest(
                    "BTC-USDT-SWAP",
                    "buy",
                    "0.1",
                    "70000",
                    reduce_only=False,
                )
            )
        ack = client.place_order(order)
        self.assertEqual(ack.order_id, "9001")
        call = transport.calls[-1]
        body = json.loads(call["body"])
        self.assertEqual(body["reduceOnly"], "true")
        self.assertEqual(body["sz"], "0.25")
        self.assertEqual(call["headers"]["x-simulated-trading"], "1")

    def test_arm_expires_and_live_orders_need_extra_config_gate(self):
        current = [10.0]
        transport = FakeTransport(response([subaccount_config_payload()]))
        client = OkxPrivateClient(
            self.order_config(order_arm_timeout_seconds=5),
            transport=transport,
            monotonic_clock=lambda: current[0],
        )
        client.verify_subaccount_identity()
        client.arm_order_submission(client.order_arm_phrase)
        self.assertTrue(client.order_submission_armed)
        current[0] = 15.1
        self.assertFalse(client.order_submission_armed)

        live_cfg = self.order_config(
            environment=OkxEnvironment.LIVE, live_trading_enabled=False
        )
        with self.assertRaisesRegex(OkxConfigurationError, "live_trading_enabled"):
            live_cfg.validate_order_submission()

    def test_read_only_api_key_cannot_arm_order_submission(self):
        transport = FakeTransport(
            response([subaccount_config_payload(perm="read_only")])
        )
        client = OkxPrivateClient(self.order_config(), transport=transport)
        client.verify_subaccount_identity()
        with self.assertRaisesRegex(OkxSafetyError, "trade permission"):
            client.arm_order_submission(client.order_arm_phrase)

    def test_cancel_requires_verified_exact_subaccount_but_not_order_arm(self):
        transport = FakeTransport(
            response([subaccount_config_payload()]),
            response(
                [
                    {
                        "ordId": "9001",
                        "clOrdId": "FQTEST1",
                        "sCode": "0",
                        "sMsg": "",
                    }
                ]
            ),
        )
        client = OkxPrivateClient(self.order_config(), transport=transport)
        with self.assertRaisesRegex(OkxSafetyError, "identity"):
            client.cancel_order("BTC-USDT-SWAP", order_id="9001")
        client.verify_subaccount_identity()
        ack = client.cancel_order("BTC-USDT-SWAP", order_id="9001")
        self.assertEqual(ack.order_id, "9001")
        self.assertFalse(client.order_submission_armed)
        self.assertTrue(
            transport.calls[-1]["url"].endswith("/api/v5/trade/cancel-order")
        )


if __name__ == "__main__":
    unittest.main()
