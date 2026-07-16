from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from futures_quant.api.ctp_config import SecretValue


class OkxConfigurationError(ValueError):
    """Raised when OKX settings are incomplete or unsafe."""


class OkxEnvironment(str, Enum):
    DISABLED = "disabled"
    DEMO = "demo"
    LIVE = "live"


_OFFICIAL_API_HOSTS = frozenset(
    {"openapi.okx.com", "www.okx.com", "aws.okx.com"}
)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TAG = re.compile(r"^[A-Za-z0-9]{1,16}$")
_PLACEHOLDER_SECRETS = frozenset(
    {
        "",
        "use_environment_variable",
        "your_api_key",
        "your_secret_key",
        "your_passphrase",
    }
)


def validate_okx_base_url(value: str) -> str:
    """Accept only an HTTPS OKX API origin, never an arbitrary credential sink."""

    normalized = str(value or "").strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _OFFICIAL_API_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        hosts = ", ".join(sorted(_OFFICIAL_API_HOSTS))
        raise OkxConfigurationError(
            "base_url must be an HTTPS origin on the built-in OKX allowlist "
            f"({hosts}); paths, credentials, query strings, and fragments are forbidden."
        )
    return normalized


@dataclass(frozen=True)
class OkxConfig:
    """Safety-first OKX REST configuration.

    API credentials are represented by redacting wrappers and are populated by
    :func:`load_okx_config` from environment variables only.  Constructing this
    object does not connect to OKX.
    """

    enabled: bool = False
    environment: OkxEnvironment = OkxEnvironment.DEMO
    base_url: str = "https://openapi.okx.com"
    private_api_enabled: bool = False
    order_submission_enabled: bool = False
    live_trading_enabled: bool = False
    require_subaccount: bool = True
    require_ip_binding_for_orders: bool = True
    api_key: SecretValue = field(default_factory=SecretValue, repr=False)
    secret_key: SecretValue = field(default_factory=SecretValue, repr=False)
    passphrase: SecretValue = field(default_factory=SecretValue, repr=False)
    expected_uid: str = ""
    expected_main_uid: str = ""
    timeout_seconds: float = 10.0
    order_arm_timeout_seconds: float = 120.0
    allowed_trade_modes: tuple[str, ...] = ("cash", "cross", "isolated")
    allowed_order_types: tuple[str, ...] = ("limit",)
    allow_opening_orders: bool = False
    order_limits: Mapping[str, Decimal] = field(default_factory=dict, repr=False)
    client_order_tag: str = "FQWORKBENCH"

    @property
    def effective_environment(self) -> OkxEnvironment:
        return self.environment if self.enabled else OkxEnvironment.DISABLED

    def validate(self) -> None:
        validate_okx_base_url(self.base_url)
        if self.timeout_seconds <= 0:
            raise OkxConfigurationError("timeout_seconds must be positive.")
        if self.order_arm_timeout_seconds <= 0:
            raise OkxConfigurationError("order_arm_timeout_seconds must be positive.")
        if not self.allowed_trade_modes:
            raise OkxConfigurationError("allowed_trade_modes cannot be empty.")
        if not self.allowed_order_types:
            raise OkxConfigurationError("allowed_order_types cannot be empty.")
        if not _TAG.fullmatch(self.client_order_tag):
            raise OkxConfigurationError(
                "client_order_tag must contain 1-16 ASCII letters or digits."
            )
        for inst_id, maximum in self.order_limits.items():
            if not str(inst_id).strip():
                raise OkxConfigurationError("order_limits contains an empty instId.")
            if not maximum.is_finite() or maximum <= 0:
                raise OkxConfigurationError(
                    f"order_limits.{inst_id} must be a positive finite number."
                )
        if self.order_submission_enabled and not self.private_api_enabled:
            raise OkxConfigurationError(
                "order_submission_enabled requires private_api_enabled=true."
            )
        if self.allow_opening_orders and not self.order_submission_enabled:
            raise OkxConfigurationError(
                "allow_opening_orders requires order_submission_enabled=true."
            )

    def validate_private_access(self) -> None:
        self.validate()
        if not self.enabled or self.environment == OkxEnvironment.DISABLED:
            raise OkxConfigurationError("OKX integration is disabled.")
        if not self.private_api_enabled:
            raise OkxConfigurationError("OKX private API access is disabled.")
        missing = []
        if not self.api_key:
            missing.append("api_key environment variable")
        if not self.secret_key:
            missing.append("secret_key environment variable")
        if not self.passphrase:
            missing.append("passphrase environment variable")
        if missing:
            raise OkxConfigurationError(
                "Missing OKX credentials: " + ", ".join(missing) + "."
            )

    def validate_order_submission(self) -> None:
        self.validate_private_access()
        if not self.order_submission_enabled:
            raise OkxConfigurationError("OKX order submission is disabled.")
        if not self.require_subaccount:
            raise OkxConfigurationError(
                "Order submission requires require_subaccount=true."
            )
        if not self.expected_uid.strip():
            raise OkxConfigurationError(
                "Order submission requires an exact expected_uid safety check."
            )
        if not self.order_limits:
            raise OkxConfigurationError(
                "Order submission requires at least one instrument in order_limits."
            )
        if (
            self.environment == OkxEnvironment.LIVE
            and not self.live_trading_enabled
        ):
            raise OkxConfigurationError(
                "Live order submission requires live_trading_enabled=true."
            )

    def redacted_summary(self) -> dict[str, object]:
        def mask_uid(value: str) -> str:
            if not value:
                return ""
            if len(value) <= 4:
                return "***"
            return value[:2] + "***" + value[-2:]

        return {
            "enabled": self.enabled,
            "environment": self.environment.value,
            "base_url": self.base_url,
            "private_api_enabled": self.private_api_enabled,
            "order_submission_enabled": self.order_submission_enabled,
            "live_trading_enabled": self.live_trading_enabled,
            "require_subaccount": self.require_subaccount,
            "require_ip_binding_for_orders": self.require_ip_binding_for_orders,
            "api_key_configured": bool(self.api_key),
            "secret_key_configured": bool(self.secret_key),
            "passphrase_configured": bool(self.passphrase),
            "expected_uid": mask_uid(self.expected_uid),
            "expected_main_uid": mask_uid(self.expected_main_uid),
            "order_limit_instruments": tuple(sorted(self.order_limits)),
            "allow_opening_orders": self.allow_opening_orders,
        }


def _read_bool(raw: Mapping[str, object], name: str, default: bool) -> bool:
    value = raw.get(name, default)
    if not isinstance(value, bool):
        raise OkxConfigurationError(f"{name} must be true or false.")
    return value


def _env_name(raw: Mapping[str, object], field_name: str, default: str) -> str:
    value = str(raw.get(f"{field_name}_env", default) or "").strip()
    if not value or not _ENV_NAME.fullmatch(value):
        raise OkxConfigurationError(
            f"{field_name}_env must be a valid environment variable name."
        )
    return value


def _load_secret(
    raw: Mapping[str, object],
    field_name: str,
    default_env_name: str,
    environ: Mapping[str, str],
) -> SecretValue:
    inline = str(raw.get(field_name, "") or "")
    if inline not in _PLACEHOLDER_SECRETS:
        raise OkxConfigurationError(
            f"Inline {field_name} is forbidden; use {field_name}_env instead."
        )
    name = _env_name(raw, field_name, default_env_name)
    return SecretValue(environ.get(name, ""))


def _load_env_text(
    raw: Mapping[str, object],
    field_name: str,
    default_env_name: str,
    environ: Mapping[str, str],
) -> str:
    inline = str(raw.get(field_name, "") or "").strip()
    env_field = f"{field_name}_env"
    if env_field not in raw and inline:
        return inline
    name = _env_name(raw, field_name, default_env_name)
    return str(environ.get(name, inline) or "").strip()


def _parse_environment(value: object) -> OkxEnvironment:
    normalized = str(value or "demo").strip().lower()
    aliases = {
        "disabled": OkxEnvironment.DISABLED,
        "demo": OkxEnvironment.DEMO,
        "paper": OkxEnvironment.DEMO,
        "simulation": OkxEnvironment.DEMO,
        "live": OkxEnvironment.LIVE,
        "production": OkxEnvironment.LIVE,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise OkxConfigurationError(
            "environment must be disabled, demo, or live."
        ) from exc


def load_okx_config(
    path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> OkxConfig:
    """Load an OKX config while refusing all inline credential values."""

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise OkxConfigurationError("OKX config root must be a JSON object.")
    if str(raw.get("gateway", "okx")).strip().lower() != "okx":
        raise OkxConfigurationError("gateway must be 'okx'.")
    env = os.environ if environ is None else environ

    order_limits_raw = raw.get("order_limits", {}) or {}
    if not isinstance(order_limits_raw, dict):
        raise OkxConfigurationError(
            "order_limits must be an object mapping instId to maximum size."
        )
    order_limits: dict[str, Decimal] = {}
    for inst_id, maximum in order_limits_raw.items():
        try:
            order_limits[str(inst_id).strip()] = Decimal(str(maximum))
        except (InvalidOperation, ValueError) as exc:
            raise OkxConfigurationError(
                f"order_limits.{inst_id} must be numeric."
            ) from exc

    def string_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = raw.get(name, list(default))
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise OkxConfigurationError(f"{name} must be a non-empty string array.")
        return tuple(item.strip().lower() for item in value)

    cfg = OkxConfig(
        enabled=_read_bool(raw, "enabled", False),
        environment=_parse_environment(raw.get("environment", "demo")),
        base_url=str(raw.get("base_url", "https://openapi.okx.com")),
        private_api_enabled=_read_bool(raw, "private_api_enabled", False),
        order_submission_enabled=_read_bool(
            raw, "order_submission_enabled", False
        ),
        live_trading_enabled=_read_bool(raw, "live_trading_enabled", False),
        require_subaccount=_read_bool(raw, "require_subaccount", True),
        require_ip_binding_for_orders=_read_bool(
            raw, "require_ip_binding_for_orders", True
        ),
        api_key=_load_secret(raw, "api_key", "OKX_API_KEY", env),
        secret_key=_load_secret(raw, "secret_key", "OKX_SECRET_KEY", env),
        passphrase=_load_secret(raw, "passphrase", "OKX_PASSPHRASE", env),
        expected_uid=_load_env_text(
            raw, "expected_uid", "OKX_EXPECTED_UID", env
        ),
        expected_main_uid=_load_env_text(
            raw, "expected_main_uid", "OKX_EXPECTED_MAIN_UID", env
        ),
        timeout_seconds=float(raw.get("timeout_seconds", 10.0)),
        order_arm_timeout_seconds=float(
            raw.get("order_arm_timeout_seconds", 120.0)
        ),
        allowed_trade_modes=string_tuple(
            "allowed_trade_modes", ("cash", "cross", "isolated")
        ),
        allowed_order_types=string_tuple("allowed_order_types", ("limit",)),
        allow_opening_orders=_read_bool(raw, "allow_opening_orders", False),
        order_limits=order_limits,
        client_order_tag=str(raw.get("client_order_tag", "FQWORKBENCH")),
    )
    cfg.validate()
    return cfg
