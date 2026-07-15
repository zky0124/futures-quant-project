from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping


class CtpConfigurationError(ValueError):
    """Raised when a CTP configuration is incomplete or unsafe."""


class TradingMode(str, Enum):
    DISABLED = "disabled"
    PAPER = "paper"
    LIVE = "live"


class SecretValue:
    """A small redacting wrapper used to avoid accidental secret disclosure.

    The plaintext is deliberately available only through ``reveal()`` so that
    logging a config object, exception context, or dataclass cannot print it.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str | None = "") -> None:
        self._value = str(value or "")

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __str__(self) -> str:
        return "********" if self._value else ""

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)" if self._value else "SecretValue(<empty>)"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretValue):
            return NotImplemented
        return self._value == other._value


@dataclass(frozen=True)
class CtpRiskLimits:
    """Fail-closed limits applied before any request reaches the SDK adapter."""

    max_order_volume: int = 5
    max_abs_position_per_symbol: int = 50
    max_open_symbols: int = 5
    max_pending_orders: int = 20
    max_daily_order_requests: int = 100
    max_symbol_margin_fraction: float = 0.20
    max_total_margin_fraction: float = 0.60

    def validate(self) -> None:
        integer_limits = {
            "max_order_volume": self.max_order_volume,
            "max_abs_position_per_symbol": self.max_abs_position_per_symbol,
            "max_open_symbols": self.max_open_symbols,
            "max_pending_orders": self.max_pending_orders,
            "max_daily_order_requests": self.max_daily_order_requests,
        }
        for name, value in integer_limits.items():
            if value <= 0:
                raise CtpConfigurationError(f"risk_limits.{name} must be positive.")
        for name, value in {
            "max_symbol_margin_fraction": self.max_symbol_margin_fraction,
            "max_total_margin_fraction": self.max_total_margin_fraction,
        }.items():
            if not 0 < value <= 1:
                raise CtpConfigurationError(f"risk_limits.{name} must be in (0, 1].")
        if self.max_symbol_margin_fraction > self.max_total_margin_fraction:
            raise CtpConfigurationError(
                "risk_limits.max_symbol_margin_fraction cannot exceed "
                "max_total_margin_fraction."
            )


@dataclass(frozen=True)
class CtpInstrumentConfig:
    symbol: str
    exchange_id: str
    volume_multiple: int
    margin_rate: float

    def validate(self) -> None:
        if not self.symbol.strip():
            raise CtpConfigurationError("instrument symbol cannot be empty.")
        if not self.exchange_id.strip():
            raise CtpConfigurationError(
                f"instrument {self.symbol!r} must define exchange_id."
            )
        if self.volume_multiple <= 0:
            raise CtpConfigurationError(
                f"instrument {self.symbol!r} volume_multiple must be positive."
            )
        if not 0 < self.margin_rate <= 1:
            raise CtpConfigurationError(
                f"instrument {self.symbol!r} margin_rate must be in (0, 1]."
            )


@dataclass(frozen=True)
class CtpConfig:
    """CTP connection settings with safe defaults.

    ``enabled`` is the first outbound safety switch. ``mode`` defaults to
    paper and live order submission has two additional gates:
    ``live_trading_enabled`` plus a short-lived runtime arm on the gateway.
    """

    broker_id: str = ""
    trade_front: str = ""
    market_front: str = ""
    user_id: str = ""
    password: SecretValue = field(default_factory=SecretValue, repr=False)
    app_id: str = ""
    auth_code: SecretValue = field(default_factory=SecretValue, repr=False)
    broker_name: str = ""
    api_version: str = ""
    adapter: str = ""
    mode: TradingMode = TradingMode.PAPER
    enabled: bool = False
    live_trading_enabled: bool = False
    programmatic_trading_report_confirmed: bool = False
    settlement_confirmation_required: bool = True
    heartbeat_timeout_seconds: float = 45.0
    reconnect_initial_delay_seconds: float = 2.0
    reconnect_max_delay_seconds: float = 60.0
    reconnect_max_attempts: int = 8
    live_arm_timeout_seconds: float = 300.0
    risk_limits: CtpRiskLimits = field(default_factory=CtpRiskLimits)
    instruments: Mapping[str, CtpInstrumentConfig] = field(
        default_factory=dict, repr=False
    )

    @property
    def effective_mode(self) -> TradingMode:
        return self.mode if self.enabled else TradingMode.DISABLED

    def validate_for_connection(self) -> None:
        if not self.enabled or self.mode == TradingMode.DISABLED:
            raise CtpConfigurationError(
                "CTP gateway is disabled. Set enabled=true only after reviewing "
                "the broker environment and safety limits."
            )
        required = {
            "broker_id": self.broker_id,
            "trade_front": self.trade_front,
            "market_front": self.market_front,
            "user_id": self.user_id,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if not self.password:
            missing.append("password (environment variable)")
        if missing:
            raise CtpConfigurationError(
                "Missing required CTP configuration: " + ", ".join(missing) + "."
            )
        for name, value in {
            "trade_front": self.trade_front,
            "market_front": self.market_front,
        }.items():
            if not value.startswith(("tcp://", "ssl://")):
                raise CtpConfigurationError(
                    f"{name} must start with tcp:// or ssl://."
                )
        if self.heartbeat_timeout_seconds <= 0:
            raise CtpConfigurationError("heartbeat_timeout_seconds must be positive.")
        if self.reconnect_initial_delay_seconds < 0:
            raise CtpConfigurationError(
                "reconnect_initial_delay_seconds cannot be negative."
            )
        if self.reconnect_max_delay_seconds < self.reconnect_initial_delay_seconds:
            raise CtpConfigurationError(
                "reconnect_max_delay_seconds cannot be smaller than the initial delay."
            )
        if self.reconnect_max_attempts < 0:
            raise CtpConfigurationError("reconnect_max_attempts cannot be negative.")
        if self.live_arm_timeout_seconds <= 0:
            raise CtpConfigurationError("live_arm_timeout_seconds must be positive.")
        if self.mode == TradingMode.LIVE:
            if not self.live_trading_enabled:
                raise CtpConfigurationError(
                    "Live mode requires live_trading_enabled=true."
                )
            if not self.programmatic_trading_report_confirmed:
                raise CtpConfigurationError(
                    "Live mode requires programmatic_trading_report_confirmed=true."
                )
        self.risk_limits.validate()
        for instrument in self.instruments.values():
            instrument.validate()

    def redacted_summary(self) -> dict[str, object]:
        """Return diagnostics that are safe to show in a UI or log."""

        masked_user = ""
        if self.user_id:
            masked_user = self.user_id[:1] + "***" + self.user_id[-1:]
        return {
            "broker_name": self.broker_name,
            "broker_id": self.broker_id,
            "trade_front": self.trade_front,
            "market_front": self.market_front,
            "user_id": masked_user,
            "api_version": self.api_version,
            "adapter": self.adapter,
            "mode": self.mode.value,
            "enabled": self.enabled,
            "live_trading_enabled": self.live_trading_enabled,
            "password_configured": bool(self.password),
            "auth_code_configured": bool(self.auth_code),
        }


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER_VALUES = {
    "",
    "use_environment_variable_in_production",
    "your_password",
    "your_auth_code",
}


def _env_value(
    raw: Mapping[str, object],
    field_name: str,
    default_env_name: str,
    environ: Mapping[str, str],
    *,
    allow_inline: bool,
    secret: bool = False,
) -> str:
    inline = str(raw.get(field_name, "") or "")
    if secret and inline not in _PLACEHOLDER_VALUES and not allow_inline:
        raise CtpConfigurationError(
            f"Inline {field_name} is disabled; use {field_name}_env instead."
        )
    env_name = str(raw.get(f"{field_name}_env", default_env_name) or "")
    if env_name and not _ENV_NAME.fullmatch(env_name):
        raise CtpConfigurationError(f"Invalid environment variable name for {field_name}.")
    from_env = environ.get(env_name, "") if env_name else ""
    if from_env:
        return from_env
    return inline if allow_inline or not secret else ""


def _parse_mode(value: object) -> TradingMode:
    normalized = str(value or "paper").strip().lower()
    aliases = {
        "simulation": TradingMode.PAPER,
        "simulation_development": TradingMode.PAPER,
        "sim": TradingMode.PAPER,
        "paper": TradingMode.PAPER,
        "live": TradingMode.LIVE,
        "production": TradingMode.LIVE,
        "disabled": TradingMode.DISABLED,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise CtpConfigurationError(
            "mode must be one of disabled, paper, or live."
        ) from exc


def load_ctp_config(
    path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    allow_inline_secrets: bool = False,
) -> CtpConfig:
    """Load a CTP config without copying secret values into diagnostics.

    Password and AuthCode are environment-only by default. The optional
    ``allow_inline_secrets`` exists solely for migration/testing and should not
    be enabled for a repository configuration file.
    """

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise CtpConfigurationError("CTP config root must be a JSON object.")
    if str(raw.get("gateway", "ctp")).lower() != "ctp":
        raise CtpConfigurationError("gateway must be 'ctp'.")
    env = os.environ if environ is None else environ
    user_id = _env_value(raw, "user_id", "CTP_USER_ID", env, allow_inline=True)
    password = _env_value(
        raw,
        "password",
        "CTP_PASSWORD",
        env,
        allow_inline=allow_inline_secrets,
        secret=True,
    )
    app_id = _env_value(raw, "app_id", "CTP_APP_ID", env, allow_inline=True)
    auth_code = _env_value(
        raw,
        "auth_code",
        "CTP_AUTH_CODE",
        env,
        allow_inline=allow_inline_secrets,
        secret=True,
    )

    risk_raw = raw.get("risk_limits", {}) or {}
    if not isinstance(risk_raw, dict):
        raise CtpConfigurationError("risk_limits must be a JSON object.")
    risk = CtpRiskLimits(**risk_raw)

    instrument_raw = raw.get("instruments", {}) or {}
    if not isinstance(instrument_raw, dict):
        raise CtpConfigurationError("instruments must be a JSON object keyed by symbol.")
    instruments: dict[str, CtpInstrumentConfig] = {}
    for symbol, item in instrument_raw.items():
        if not isinstance(item, dict):
            raise CtpConfigurationError(f"instrument {symbol!r} must be an object.")
        instruments[str(symbol)] = CtpInstrumentConfig(
            symbol=str(symbol),
            exchange_id=str(item.get("exchange_id", "")),
            volume_multiple=int(item.get("volume_multiple", 0)),
            margin_rate=float(item.get("margin_rate", 0.0)),
        )

    cfg = CtpConfig(
        broker_id=str(raw.get("broker_id", "")),
        trade_front=str(raw.get("trade_front", raw.get("front_addr", ""))),
        market_front=str(
            raw.get("market_front", raw.get("market_data_addr", ""))
        ),
        user_id=user_id,
        password=SecretValue(password),
        app_id=app_id,
        auth_code=SecretValue(auth_code),
        broker_name=str(raw.get("broker_name", "")),
        api_version=str(raw.get("api_version", "")),
        adapter=str(raw.get("adapter", "")),
        mode=_parse_mode(raw.get("mode", "paper")),
        enabled=bool(raw.get("enabled", False)),
        live_trading_enabled=bool(raw.get("live_trading_enabled", False)),
        programmatic_trading_report_confirmed=bool(
            raw.get("programmatic_trading_report_confirmed", False)
        ),
        settlement_confirmation_required=bool(
            raw.get("settlement_confirmation_required", True)
        ),
        heartbeat_timeout_seconds=float(raw.get("heartbeat_timeout_seconds", 45.0)),
        reconnect_initial_delay_seconds=float(
            raw.get("reconnect_initial_delay_seconds", 2.0)
        ),
        reconnect_max_delay_seconds=float(
            raw.get("reconnect_max_delay_seconds", 60.0)
        ),
        reconnect_max_attempts=int(raw.get("reconnect_max_attempts", 8)),
        live_arm_timeout_seconds=float(raw.get("live_arm_timeout_seconds", 300.0)),
        risk_limits=risk,
        instruments=instruments,
    )
    # Validate limits/instruments even while disabled; connection-only fields are
    # intentionally deferred so an example template can remain loadable.
    cfg.risk_limits.validate()
    for instrument in cfg.instruments.values():
        instrument.validate()
    return cfg
