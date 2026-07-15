from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    fast_window: int = 5
    slow_window: int = 20
    order_size: int = 1
    entry_window: int = 20
    exit_window: int = 10
    trend_window: int = 60
    momentum_window: int = 20
    volatility_window: int = 20
    target_annual_volatility: float = 0.15
    max_order_size: int | None = None
    max_notional_fraction: float = 0.10
    momentum_threshold: float = 0.0
    allow_short: bool = True
    annualization_factor: int = 252
    daily_fast_window: int = 13
    daily_slow_window: int = 45
    extreme_lookback_days: int = 120
    extreme_move_threshold: float = 0.20
    setup_valid_days: int = 10
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    divergence_lookback: int = 80
    divergence_pivot_radius: int = 2
    divergence_valid_bars: int = 32
    second_cross_window: int = 48
    atr_window: int = 14
    atr_stop_buffer: float = 0.20
    reward_risk: float = 2.0
    trailing_atr_multiple: float = 2.5
    slope_lookback: int = 8
    pullback_lookback: int = 5
    min_pullback_closes: int = 2
    max_entry_distance_atr: float = 0.5
    partial_exit_size: int = 2
    break_even_trigger_r: float = 1.0
    ma_exit_buffer_atr: float = 0.1
    cooldown_bars: int = 8
    loss_pause_after: int = 3
    loss_pause_bars: int = 32
    atr_stop_multiple: float = 2.5
    partial_exit_fraction: float = 0.40
    position_equity_fraction: float = 0.60


@dataclass(frozen=True)
class DataConfig:
    symbol: str
    path: Path
    source_interval_minutes: int = 15
    bar_interval_minutes: int = 15


@dataclass(frozen=True)
class ReportConfig:
    path: Path


@dataclass(frozen=True)
class ContractsConfig:
    path: Path | None = None


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float
    commission_rate: float
    slippage_ticks: int
    tick_size: float
    contract_multiplier: int
    margin_rate: float
    max_margin_usage: float
    max_symbol_exposure: float
    max_symbol_margin_usage: float
    max_open_positions: int
    max_trade_risk: float
    max_group_positions: int
    daily_loss_stop: float
    strategy: StrategyConfig
    data: DataConfig
    contracts: ContractsConfig
    report: ReportConfig


def load_backtest_config(path: str | Path, project_root: str | Path | None = None) -> BacktestConfig:
    path = Path(path)
    root = Path(project_root) if project_root else path.parent.parent
    raw = json.loads(path.read_text(encoding="utf-8"))
    cfg = BacktestConfig(
        initial_cash=float(raw["initial_cash"]),
        commission_rate=float(raw["commission_rate"]),
        slippage_ticks=int(raw["slippage_ticks"]),
        tick_size=float(raw["tick_size"]),
        contract_multiplier=int(raw["contract_multiplier"]),
        margin_rate=float(raw["margin_rate"]),
        max_margin_usage=float(raw["max_margin_usage"]),
        max_symbol_exposure=float(raw["max_symbol_exposure"]),
        max_symbol_margin_usage=float(raw.get("max_symbol_margin_usage", 1.0)),
        max_open_positions=int(raw.get("max_open_positions", 999999)),
        max_trade_risk=float(raw.get("max_trade_risk", 1.0)),
        max_group_positions=int(raw.get("max_group_positions", 999999)),
        daily_loss_stop=float(raw["daily_loss_stop"]),
        strategy=StrategyConfig(**raw["strategy"]),
        data=DataConfig(
            symbol=raw["data"]["symbol"],
            path=root / raw["data"]["path"],
            source_interval_minutes=int(
                raw["data"].get("source_interval_minutes", 15)
            ),
            bar_interval_minutes=int(raw["data"].get("bar_interval_minutes", 15)),
        ),
        contracts=ContractsConfig(path=root / raw["contracts"]["path"] if raw.get("contracts", {}).get("path") else None),
        report=ReportConfig(path=root / raw["report"]["path"]),
    )
    validate_backtest_config(cfg)
    return cfg


def validate_backtest_config(cfg: BacktestConfig) -> None:
    from futures_quant.data.timeframe import validate_intervals

    validate_intervals(
        cfg.data.source_interval_minutes, cfg.data.bar_interval_minutes
    )
    if cfg.initial_cash <= 0:
        raise ValueError("initial_cash must be positive.")
    if cfg.commission_rate < 0:
        raise ValueError("commission_rate cannot be negative.")
    if cfg.slippage_ticks < 0:
        raise ValueError("slippage_ticks cannot be negative.")
    if cfg.tick_size <= 0:
        raise ValueError("tick_size must be positive.")
    if cfg.contract_multiplier <= 0:
        raise ValueError("contract_multiplier must be positive.")
    if not 0 < cfg.margin_rate < 1:
        raise ValueError("margin_rate must be between 0 and 1.")
    if not 0 < cfg.max_margin_usage <= 1:
        raise ValueError("max_margin_usage must be in (0, 1].")
    if cfg.max_symbol_exposure <= 0:
        raise ValueError("max_symbol_exposure must be positive.")
    if not 0 < cfg.max_symbol_margin_usage <= 1:
        raise ValueError("max_symbol_margin_usage must be in (0, 1].")
    if cfg.max_open_positions <= 0:
        raise ValueError("max_open_positions must be positive.")
    if not 0 < cfg.max_trade_risk <= 1:
        raise ValueError("max_trade_risk must be in (0, 1].")
    if cfg.max_group_positions <= 0:
        raise ValueError("max_group_positions must be positive.")
    if not 0 < cfg.daily_loss_stop <= 1:
        raise ValueError("daily_loss_stop must be in (0, 1].")
    if cfg.strategy.name == "dual_ma_pullback":
        if cfg.strategy.order_size < 0:
            raise ValueError(
                "strategy.order_size cannot be negative; use zero for automatic sizing."
            )
        if cfg.strategy.order_size > 5:
            raise ValueError(
                "dual_ma_pullback order_size cannot exceed the 5-lot hard limit."
            )
    elif cfg.strategy.order_size <= 0:
        raise ValueError("strategy.order_size must be positive.")
    if cfg.strategy.name == "dual_ma":
        if cfg.strategy.fast_window <= 0 or cfg.strategy.slow_window <= 0:
            raise ValueError("strategy moving-average windows must be positive.")
        if cfg.strategy.fast_window >= cfg.strategy.slow_window:
            raise ValueError("strategy.fast_window must be smaller than strategy.slow_window.")
    elif cfg.strategy.name == "dual_ma_pullback":
        strategy = cfg.strategy
        if cfg.data.bar_interval_minutes != 15:
            raise ValueError("dual_ma_pullback is defined only for 15-minute bars.")
        positive_windows = {
            "fast_window": strategy.fast_window,
            "slow_window": strategy.slow_window,
            "atr_window": strategy.atr_window,
        }
        for name, value in positive_windows.items():
            if value <= 0:
                raise ValueError(f"strategy.{name} must be positive.")
        if strategy.fast_window >= strategy.slow_window:
            raise ValueError("strategy.fast_window must be smaller than strategy.slow_window.")
        if not 0 < strategy.partial_exit_fraction <= 1:
            raise ValueError("strategy.partial_exit_fraction must be in (0, 1].")
        if not 0 < strategy.position_equity_fraction <= 1:
            raise ValueError("strategy.position_equity_fraction must be in (0, 1].")
        if strategy.max_order_size is not None and strategy.max_order_size < 0:
            raise ValueError("strategy.max_order_size cannot be negative.")
        if strategy.max_order_size is not None and strategy.max_order_size > 5:
            raise ValueError(
                "dual_ma_pullback max_order_size cannot exceed the 5-lot hard limit."
            )
        non_negative = {
            "ma_exit_buffer_atr": strategy.ma_exit_buffer_atr,
        }
        for name, value in non_negative.items():
            if value < 0:
                raise ValueError(f"strategy.{name} cannot be negative.")
    elif cfg.strategy.name in {"adaptive_trend", "adaptive_trend_v2"}:
        windows = {
            "entry_window": cfg.strategy.entry_window,
            "exit_window": cfg.strategy.exit_window,
            "trend_window": cfg.strategy.trend_window,
            "momentum_window": cfg.strategy.momentum_window,
            "volatility_window": cfg.strategy.volatility_window,
        }
        for name, value in windows.items():
            if value <= 1:
                raise ValueError(f"strategy.{name} must be greater than 1.")
        if cfg.strategy.exit_window > cfg.strategy.entry_window:
            raise ValueError("strategy.exit_window cannot exceed strategy.entry_window.")
        if cfg.strategy.target_annual_volatility <= 0:
            raise ValueError("strategy.target_annual_volatility must be positive.")
        if (
            cfg.strategy.max_order_size is not None
            and cfg.strategy.max_order_size < cfg.strategy.order_size
        ):
            raise ValueError("strategy.max_order_size cannot be smaller than order_size.")
        if not 0 < cfg.strategy.max_notional_fraction <= 1:
            raise ValueError("strategy.max_notional_fraction must be in (0, 1].")
        if cfg.strategy.momentum_threshold < 0:
            raise ValueError("strategy.momentum_threshold cannot be negative.")
        if cfg.strategy.annualization_factor <= 0:
            raise ValueError("strategy.annualization_factor must be positive.")
        if cfg.strategy.name == "adaptive_trend_v2":
            if cfg.strategy.atr_window <= 1:
                raise ValueError("strategy.atr_window must be greater than 1.")
            for name, value in {
                "atr_stop_multiple": cfg.strategy.atr_stop_multiple,
                "break_even_trigger_r": cfg.strategy.break_even_trigger_r,
                "reward_risk": cfg.strategy.reward_risk,
                "trailing_atr_multiple": cfg.strategy.trailing_atr_multiple,
            }.items():
                if value <= 0:
                    raise ValueError(f"strategy.{name} must be positive.")
            if not 0 < cfg.strategy.partial_exit_fraction <= 1:
                raise ValueError(
                    "strategy.partial_exit_fraction must be in (0, 1]."
                )
    elif cfg.strategy.name == "dual_period_reversal":
        strategy = cfg.strategy
        windows = {
            "fast_window": strategy.fast_window,
            "slow_window": strategy.slow_window,
            "daily_fast_window": strategy.daily_fast_window,
            "daily_slow_window": strategy.daily_slow_window,
            "extreme_lookback_days": strategy.extreme_lookback_days,
            "macd_fast": strategy.macd_fast,
            "macd_slow": strategy.macd_slow,
            "macd_signal": strategy.macd_signal,
            "divergence_lookback": strategy.divergence_lookback,
            "divergence_pivot_radius": strategy.divergence_pivot_radius,
            "divergence_valid_bars": strategy.divergence_valid_bars,
            "second_cross_window": strategy.second_cross_window,
            "atr_window": strategy.atr_window,
            "setup_valid_days": strategy.setup_valid_days,
        }
        for name, value in windows.items():
            if value <= 0:
                raise ValueError(f"strategy.{name} must be positive.")
        if strategy.fast_window >= strategy.slow_window:
            raise ValueError("strategy.fast_window must be smaller than strategy.slow_window.")
        if strategy.daily_fast_window >= strategy.daily_slow_window:
            raise ValueError(
                "strategy.daily_fast_window must be smaller than strategy.daily_slow_window."
            )
        if strategy.macd_fast >= strategy.macd_slow:
            raise ValueError("strategy.macd_fast must be smaller than strategy.macd_slow.")
        if not 0 < strategy.extreme_move_threshold < 1:
            raise ValueError("strategy.extreme_move_threshold must be in (0, 1).")
        if strategy.atr_stop_buffer < 0:
            raise ValueError("strategy.atr_stop_buffer cannot be negative.")
        if strategy.reward_risk <= 0:
            raise ValueError("strategy.reward_risk must be positive.")
        if strategy.trailing_atr_multiple <= 0:
            raise ValueError("strategy.trailing_atr_multiple must be positive.")
    else:
        raise ValueError(f"Unsupported strategy: {cfg.strategy.name}")
    if not cfg.data.path.exists():
        raise FileNotFoundError(f"Data file not found: {cfg.data.path}")
    if cfg.contracts.path is not None and not cfg.contracts.path.exists():
        raise FileNotFoundError(f"Contract spec file not found: {cfg.contracts.path}")
