from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from futures_quant.api.okx_config import (
    OkxConfig,
    OkxConfigurationError,
    OkxEnvironment,
    validate_okx_base_url,
)


class OkxError(RuntimeError):
    """Base class for normalized OKX integration failures."""


class OkxTransportError(OkxError):
    pass


class OkxApiError(OkxError):
    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        status_code: int = 0,
        api_code: str = "",
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.status_code = status_code
        self.api_code = api_code


class OkxIdentityError(OkxError):
    pass


class OkxSafetyError(OkxError):
    pass


@dataclass(frozen=True)
class OkxHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class OkxHttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> OkxHttpResponse:
        ...


class UrllibOkxTransport:
    """Small standard-library transport; constructing it performs no I/O."""

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> OkxHttpResponse:
        request = Request(
            url=url,
            data=body,
            headers=dict(headers),
            method=method.upper(),
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                return OkxHttpResponse(
                    status_code=int(response.status),
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except HTTPError as exc:
            return OkxHttpResponse(
                status_code=int(exc.code),
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=exc.read(),
            )
        except URLError as exc:
            raise OkxTransportError("OKX network request failed.") from exc


class OkxSigner:
    """OKX REST HMAC-SHA256 request signer."""

    @staticmethod
    def sign(
        secret_key: str,
        timestamp: str,
        method: str,
        request_path: str,
        body: str = "",
    ) -> str:
        if not secret_key:
            raise OkxConfigurationError("secret_key is missing.")
        prehash = timestamp + method.upper() + request_path + body
        digest = hmac.new(
            secret_key.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("ascii")


def _timestamp_utc(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (
        now.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _request_path(
    endpoint: str,
    params: Mapping[str, object] | Sequence[tuple[str, object]] | None = None,
) -> str:
    if not endpoint.startswith("/api/v5/") or any(
        marker in endpoint for marker in ("?", "#", "://")
    ):
        raise ValueError("endpoint must be a plain /api/v5/... path.")
    if not params:
        return endpoint
    items = params.items() if isinstance(params, Mapping) else params
    normalized: list[tuple[str, str]] = []
    for key, value in items:
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        normalized.append((str(key), rendered))
    query = urlencode(normalized)
    return endpoint + ("?" + query if query else "")


def _canonical_body(payload: Mapping[str, object] | None) -> str:
    if payload is None:
        return ""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _unwrap_response(response: OkxHttpResponse, endpoint: str) -> dict[str, object]:
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OkxApiError(
            "OKX returned an invalid JSON response.",
            endpoint=endpoint,
            status_code=response.status_code,
        ) from exc
    if not isinstance(payload, dict):
        raise OkxApiError(
            "OKX returned an unexpected response shape.",
            endpoint=endpoint,
            status_code=response.status_code,
        )
    api_code = str(payload.get("code", ""))
    if not 200 <= response.status_code < 300 or api_code != "0":
        message = str(payload.get("msg", "") or "OKX request was rejected.")
        raise OkxApiError(
            message,
            endpoint=endpoint,
            status_code=response.status_code,
            api_code=api_code,
        )
    return payload


def _data_list(payload: Mapping[str, object], endpoint: str) -> list[object]:
    data = payload.get("data", [])
    if not isinstance(data, list):
        raise OkxApiError(
            "OKX response data is not an array.", endpoint=endpoint, api_code="0"
        )
    return data


class OkxPublicClient:
    """Unauthenticated OKX market/public REST client."""

    def __init__(
        self,
        *,
        base_url: str = "https://openapi.okx.com",
        transport: OkxHttpTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = validate_okx_base_url(base_url)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        self.timeout_seconds = timeout_seconds
        self._transport = transport or UrllibOkxTransport()

    def _send(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, object] | Sequence[tuple[str, object]] | None = None,
        payload: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        path = _request_path(endpoint, params)
        body_text = _canonical_body(payload)
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "futures-quant/0.1",
        }
        if body_text:
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        response = self._transport.request(
            method.upper(),
            self.base_url + path,
            request_headers,
            body_text.encode("utf-8") if body_text else None,
            self.timeout_seconds,
        )
        return _unwrap_response(response, endpoint)

    def get_instruments(
        self,
        inst_type: str,
        *,
        inst_id: str = "",
        underlying: str = "",
        inst_family: str = "",
    ) -> list[object]:
        endpoint = "/api/v5/public/instruments"
        payload = self._send(
            "GET",
            endpoint,
            params={
                "instType": inst_type.upper(),
                "uly": underlying,
                "instFamily": inst_family,
                "instId": inst_id,
            },
        )
        return _data_list(payload, endpoint)

    def get_tickers(self, inst_type: str) -> list[object]:
        endpoint = "/api/v5/market/tickers"
        return _data_list(
            self._send(
                "GET", endpoint, params={"instType": inst_type.upper()}
            ),
            endpoint,
        )

    def get_server_time_ms(self) -> int:
        endpoint = "/api/v5/public/time"
        data = _data_list(self._send("GET", endpoint), endpoint)
        if len(data) != 1 or not isinstance(data[0], dict):
            raise OkxApiError(
                "OKX server time response was invalid.", endpoint=endpoint
            )
        try:
            return int(str(data[0].get("ts", "")))
        except ValueError as exc:
            raise OkxApiError(
                "OKX server time was not numeric.", endpoint=endpoint
            ) from exc

    def get_candles(
        self,
        inst_id: str,
        *,
        bar: str = "15m",
        after: str = "",
        before: str = "",
        limit: int = 100,
        history: bool = False,
    ) -> list[object]:
        if not 1 <= limit <= 300:
            raise ValueError("candle limit must be between 1 and 300.")
        endpoint = (
            "/api/v5/market/history-candles"
            if history
            else "/api/v5/market/candles"
        )
        return _data_list(
            self._send(
                "GET",
                endpoint,
                params={
                    "instId": inst_id,
                    "bar": bar,
                    "after": after,
                    "before": before,
                    "limit": limit,
                },
            ),
            endpoint,
        )


@dataclass(frozen=True)
class OkxAccountIdentity:
    uid: str
    main_uid: str
    api_key_label: str
    permissions: tuple[str, ...]
    account_level: str
    position_mode: str
    ip_addresses: tuple[str, ...]

    @property
    def is_subaccount(self) -> bool:
        return bool(self.uid and self.main_uid and self.uid != self.main_uid)

    def redacted_summary(self) -> dict[str, object]:
        def mask(value: str) -> str:
            if len(value) <= 4:
                return "***" if value else ""
            return value[:2] + "***" + value[-2:]

        return {
            "uid": mask(self.uid),
            "main_uid": mask(self.main_uid),
            "is_subaccount": self.is_subaccount,
            "api_key_label": self.api_key_label,
            "permissions": self.permissions,
            "account_level": self.account_level,
            "position_mode": self.position_mode,
            "ip_binding_configured": bool(self.ip_addresses),
        }


_INSTRUMENT_ID = re.compile(r"^[A-Z0-9-]{3,64}$")
_CLIENT_ORDER_ID = re.compile(r"^[A-Za-z0-9]{1,32}$")
_ORDER_TAG = re.compile(r"^[A-Za-z0-9]{1,16}$")
_SUPPORTED_ORDER_TYPES = frozenset({"market", "limit", "post_only", "fok", "ioc"})


def _positive_decimal(value: str, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric.") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field_name} must be a positive finite number.")
    return result


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


@dataclass(frozen=True)
class OkxOrderRequest:
    inst_id: str
    side: str
    size: str
    price: str = ""
    trade_mode: str = "cross"
    order_type: str = "limit"
    position_side: str = "net"
    reduce_only: bool | None = True
    client_order_id: str = ""
    tag: str = ""

    def validate(self) -> None:
        if not _INSTRUMENT_ID.fullmatch(self.inst_id):
            raise ValueError("inst_id has an invalid OKX instrument format.")
        if self.side.lower() not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell.")
        if self.order_type.lower() not in _SUPPORTED_ORDER_TYPES:
            raise ValueError("Unsupported OKX order_type.")
        _positive_decimal(self.size, "size")
        if self.order_type.lower() == "market":
            if self.price:
                raise ValueError("market orders must not specify price.")
        else:
            _positive_decimal(self.price, "price")
        if self.position_side and self.position_side.lower() not in {
            "net",
            "long",
            "short",
        }:
            raise ValueError("position_side must be net, long, or short.")
        if self.client_order_id and not _CLIENT_ORDER_ID.fullmatch(
            self.client_order_id
        ):
            raise ValueError(
                "client_order_id must contain 1-32 ASCII letters or digits."
            )
        if self.tag and not _ORDER_TAG.fullmatch(self.tag):
            raise ValueError("tag must contain 1-16 ASCII letters or digits.")

    def to_payload(self, *, default_tag: str) -> dict[str, object]:
        self.validate()
        client_id = self.client_order_id or ("FQ" + uuid.uuid4().hex[:20].upper())
        payload: dict[str, object] = {
            "instId": self.inst_id,
            "tdMode": self.trade_mode.lower(),
            "side": self.side.lower(),
            "ordType": self.order_type.lower(),
            "sz": _decimal_text(_positive_decimal(self.size, "size")),
            "clOrdId": client_id,
            "tag": self.tag or default_tag,
        }
        if self.price:
            payload["px"] = _decimal_text(_positive_decimal(self.price, "price"))
        if self.position_side:
            payload["posSide"] = self.position_side.lower()
        if self.reduce_only is not None:
            payload["reduceOnly"] = "true" if self.reduce_only else "false"
        return payload


@dataclass(frozen=True)
class OkxOrderAck:
    order_id: str
    client_order_id: str
    timestamp: str
    tag: str


@dataclass(frozen=True)
class OkxCancelAck:
    order_id: str
    client_order_id: str


class OkxPrivateClient(OkxPublicClient):
    """Authenticated client with fail-closed subaccount and order gates."""

    def __init__(
        self,
        config: OkxConfig,
        *,
        transport: OkxHttpTransport | None = None,
        utc_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        config.validate()
        super().__init__(
            base_url=config.base_url,
            transport=transport,
            timeout_seconds=config.timeout_seconds,
        )
        self.config = config
        self._utc_clock = utc_clock or (lambda: datetime.now(timezone.utc))
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._identity: OkxAccountIdentity | None = None
        self._armed_until = 0.0
        self._lock = threading.RLock()

    @property
    def identity(self) -> OkxAccountIdentity | None:
        return self._identity

    @property
    def order_arm_phrase(self) -> str:
        environment = self.config.environment.value.upper()
        return f"ARM OKX {environment} ORDERS"

    @property
    def order_submission_armed(self) -> bool:
        with self._lock:
            return self._armed_until > self._monotonic_clock()

    def _send_private(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, object] | Sequence[tuple[str, object]] | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        self.config.validate_private_access()
        path = _request_path(endpoint, params)
        body_text = _canonical_body(payload)
        timestamp = _timestamp_utc(self._utc_clock())
        headers = {
            "OK-ACCESS-KEY": self.config.api_key.reveal(),
            "OK-ACCESS-SIGN": OkxSigner.sign(
                self.config.secret_key.reveal(),
                timestamp,
                method,
                path,
                body_text,
            ),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.config.passphrase.reveal(),
        }
        if self.config.environment == OkxEnvironment.DEMO:
            headers["x-simulated-trading"] = "1"
        return self._send(
            method,
            endpoint,
            params=params,
            payload=payload,
            headers=headers,
        )

    def get_account_config(self) -> dict[str, object]:
        endpoint = "/api/v5/account/config"
        data = _data_list(self._send_private("GET", endpoint), endpoint)
        if len(data) != 1 or not isinstance(data[0], dict):
            raise OkxIdentityError("OKX account config did not contain one account.")
        return dict(data[0])

    def verify_subaccount_identity(self) -> OkxAccountIdentity:
        with self._lock:
            self._identity = None
            self._armed_until = 0.0
            account = self.get_account_config()
            permissions_value = account.get("perm", "")
            if isinstance(permissions_value, str):
                permissions = tuple(
                    part.strip().lower()
                    for part in permissions_value.split(",")
                    if part.strip()
                )
            elif isinstance(permissions_value, list):
                permissions = tuple(
                    str(part).strip().lower()
                    for part in permissions_value
                    if str(part).strip()
                )
            else:
                permissions = ()
            identity = OkxAccountIdentity(
                uid=str(account.get("uid", "")),
                main_uid=str(account.get("mainUid", "")),
                api_key_label=str(account.get("label", "")),
                permissions=permissions,
                account_level=str(account.get("acctLv", "")),
                position_mode=str(account.get("posMode", "")),
                ip_addresses=tuple(
                    value.strip()
                    for value in str(account.get("ip", "")).split(",")
                    if value.strip()
                ),
            )
            if not identity.uid or not identity.main_uid:
                raise OkxIdentityError("OKX did not return uid and mainUid.")
            if "withdraw" in identity.permissions:
                raise OkxIdentityError(
                    "The OKX API key has Withdraw permission; use a Read/Trade-only key."
                )
            if self.config.require_subaccount and not identity.is_subaccount:
                raise OkxIdentityError(
                    "The API key belongs to a main account, not a subaccount."
                )
            if (
                self.config.expected_uid
                and identity.uid != self.config.expected_uid
            ):
                raise OkxIdentityError("OKX uid does not match expected_uid.")
            if (
                self.config.expected_main_uid
                and identity.main_uid != self.config.expected_main_uid
            ):
                raise OkxIdentityError(
                    "OKX mainUid does not match expected_main_uid."
                )
            self._identity = identity
            return identity

    def get_balances(self, currencies: Sequence[str] = ()) -> list[object]:
        endpoint = "/api/v5/account/balance"
        return _data_list(
            self._send_private(
                "GET", endpoint, params={"ccy": ",".join(currencies)}
            ),
            endpoint,
        )

    def get_positions(
        self, *, inst_type: str = "", inst_id: str = "", position_id: str = ""
    ) -> list[object]:
        endpoint = "/api/v5/account/positions"
        return _data_list(
            self._send_private(
                "GET",
                endpoint,
                params={
                    "instType": inst_type.upper() if inst_type else "",
                    "instId": inst_id,
                    "posId": position_id,
                },
            ),
            endpoint,
        )

    def get_pending_orders(
        self, *, inst_type: str = "", inst_id: str = ""
    ) -> list[object]:
        endpoint = "/api/v5/trade/orders-pending"
        return _data_list(
            self._send_private(
                "GET",
                endpoint,
                params={
                    "instType": inst_type.upper() if inst_type else "",
                    "instId": inst_id,
                },
            ),
            endpoint,
        )

    def get_order(
        self, inst_id: str, *, order_id: str = "", client_order_id: str = ""
    ) -> dict[str, object]:
        if bool(order_id) == bool(client_order_id):
            raise ValueError("Provide exactly one of order_id or client_order_id.")
        endpoint = "/api/v5/trade/order"
        data = _data_list(
            self._send_private(
                "GET",
                endpoint,
                params={
                    "instId": inst_id,
                    "ordId": order_id,
                    "clOrdId": client_order_id,
                },
            ),
            endpoint,
        )
        if len(data) != 1 or not isinstance(data[0], dict):
            raise OkxApiError("Order lookup returned no order.", endpoint=endpoint)
        return dict(data[0])

    def get_order_history(
        self,
        *,
        inst_type: str,
        inst_id: str = "",
        after: str = "",
        before: str = "",
        limit: int = 100,
        archive: bool = False,
    ) -> list[object]:
        if not 1 <= limit <= 100:
            raise ValueError("order history limit must be between 1 and 100.")
        endpoint = (
            "/api/v5/trade/orders-history-archive"
            if archive
            else "/api/v5/trade/orders-history"
        )
        return _data_list(
            self._send_private(
                "GET",
                endpoint,
                params={
                    "instType": inst_type.upper(),
                    "instId": inst_id,
                    "after": after,
                    "before": before,
                    "limit": limit,
                },
            ),
            endpoint,
        )

    def get_fills_history(
        self,
        *,
        inst_type: str = "",
        inst_id: str = "",
        after: str = "",
        before: str = "",
        limit: int = 100,
    ) -> list[object]:
        if not 1 <= limit <= 100:
            raise ValueError("fills history limit must be between 1 and 100.")
        endpoint = "/api/v5/trade/fills-history"
        return _data_list(
            self._send_private(
                "GET",
                endpoint,
                params={
                    "instType": inst_type.upper() if inst_type else "",
                    "instId": inst_id,
                    "after": after,
                    "before": before,
                    "limit": limit,
                },
            ),
            endpoint,
        )

    def get_recent_fills(
        self,
        *,
        inst_type: str = "",
        inst_id: str = "",
        after: str = "",
        before: str = "",
        limit: int = 100,
    ) -> list[object]:
        if not 1 <= limit <= 100:
            raise ValueError("recent fills limit must be between 1 and 100.")
        endpoint = "/api/v5/trade/fills"
        return _data_list(
            self._send_private(
                "GET",
                endpoint,
                params={
                    "instType": inst_type.upper() if inst_type else "",
                    "instId": inst_id,
                    "after": after,
                    "before": before,
                    "limit": limit,
                },
            ),
            endpoint,
        )

    def arm_order_submission(self, phrase: str) -> None:
        with self._lock:
            self.config.validate_order_submission()
            if self._identity is None:
                raise OkxSafetyError(
                    "Verify the exact OKX subaccount identity before arming orders."
                )
            if self._identity.uid != self.config.expected_uid:
                raise OkxSafetyError("Verified uid no longer matches expected_uid.")
            if "trade" not in self._identity.permissions:
                raise OkxSafetyError("The verified API key has no trade permission.")
            if (
                self.config.require_ip_binding_for_orders
                and not self._identity.ip_addresses
            ):
                raise OkxSafetyError(
                    "The verified OKX API key is not bound to an IP address."
                )
            if phrase != self.order_arm_phrase:
                raise OkxSafetyError("The OKX order arm phrase is incorrect.")
            self._armed_until = (
                self._monotonic_clock() + self.config.order_arm_timeout_seconds
            )

    def disarm_order_submission(self) -> None:
        with self._lock:
            self._armed_until = 0.0

    def _validate_order_safety(self, order: OkxOrderRequest) -> None:
        self.config.validate_order_submission()
        order.validate()
        if self._identity is None:
            raise OkxSafetyError("OKX subaccount identity has not been verified.")
        if self._identity.uid != self.config.expected_uid:
            raise OkxSafetyError("Verified OKX uid does not match expected_uid.")
        if "trade" not in self._identity.permissions:
            raise OkxSafetyError("The verified API key has no trade permission.")
        if not self.order_submission_armed:
            raise OkxSafetyError("OKX order submission is not armed or has expired.")
        if order.inst_id not in self.config.order_limits:
            raise OkxSafetyError(
                f"{order.inst_id} is not in the OKX order_limits whitelist."
            )
        size = _positive_decimal(order.size, "size")
        if size > self.config.order_limits[order.inst_id]:
            raise OkxSafetyError(
                f"Order size exceeds the configured limit for {order.inst_id}."
            )
        if order.trade_mode.lower() not in self.config.allowed_trade_modes:
            raise OkxSafetyError("Order trade_mode is not allowed by configuration.")
        if order.order_type.lower() not in self.config.allowed_order_types:
            raise OkxSafetyError("Order type is not allowed by configuration.")
        if order.reduce_only is not True and not self.config.allow_opening_orders:
            raise OkxSafetyError("Opening orders are disabled; reduce_only must be true.")

    def place_order(self, order: OkxOrderRequest) -> OkxOrderAck:
        with self._lock:
            self._validate_order_safety(order)
            endpoint = "/api/v5/trade/order"
            payload = order.to_payload(default_tag=self.config.client_order_tag)
            data = _data_list(
                self._send_private("POST", endpoint, payload=payload), endpoint
            )
            if len(data) != 1 or not isinstance(data[0], dict):
                raise OkxApiError(
                    "OKX order response did not contain one acknowledgement.",
                    endpoint=endpoint,
                )
            item = data[0]
            subcode = str(item.get("sCode", ""))
            if subcode != "0":
                raise OkxApiError(
                    str(item.get("sMsg", "") or "OKX rejected the order."),
                    endpoint=endpoint,
                    api_code=subcode,
                )
            return OkxOrderAck(
                order_id=str(item.get("ordId", "")),
                client_order_id=str(item.get("clOrdId", payload["clOrdId"])),
                timestamp=str(item.get("ts", "")),
                tag=str(item.get("tag", payload["tag"])),
            )

    def cancel_order(
        self,
        inst_id: str,
        *,
        order_id: str = "",
        client_order_id: str = "",
    ) -> OkxCancelAck:
        """Cancel an existing order without requiring the opening-order arm.

        Cancellation remains fail-closed on private access, exact verified
        subaccount identity, and Trade permission.  It intentionally does not
        require the short-lived order arm because reducing pending risk should
        remain available after order entry has been disabled.
        """

        if not _INSTRUMENT_ID.fullmatch(inst_id):
            raise ValueError("inst_id has an invalid OKX instrument format.")
        if bool(order_id) == bool(client_order_id):
            raise ValueError("Provide exactly one of order_id or client_order_id.")
        with self._lock:
            self.config.validate_private_access()
            if self._identity is None:
                raise OkxSafetyError(
                    "Verify the exact OKX subaccount identity before cancellation."
                )
            if not self.config.expected_uid or self._identity.uid != self.config.expected_uid:
                raise OkxSafetyError(
                    "Verified OKX uid does not match the configured expected_uid."
                )
            if "trade" not in self._identity.permissions:
                raise OkxSafetyError("The verified API key has no trade permission.")
            endpoint = "/api/v5/trade/cancel-order"
            payload = {
                "instId": inst_id,
                "ordId": order_id,
                "clOrdId": client_order_id,
            }
            payload = {key: value for key, value in payload.items() if value}
            data = _data_list(
                self._send_private("POST", endpoint, payload=payload), endpoint
            )
            if len(data) != 1 or not isinstance(data[0], dict):
                raise OkxApiError(
                    "OKX cancel response did not contain one acknowledgement.",
                    endpoint=endpoint,
                )
            item = data[0]
            subcode = str(item.get("sCode", ""))
            if subcode != "0":
                raise OkxApiError(
                    str(item.get("sMsg", "") or "OKX rejected cancellation."),
                    endpoint=endpoint,
                    api_code=subcode,
                )
            return OkxCancelAck(
                order_id=str(item.get("ordId", order_id)),
                client_order_id=str(item.get("clOrdId", client_order_id)),
            )
