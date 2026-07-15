from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import groupby

import pandas as pd

from futures_quant.data.contracts import ContractSpec, validate_contract_spec
from futures_quant.models import Bar, Order, Position, Signal, Trade
from futures_quant.strategies.base import Strategy


DOMESTIC_CNY_EXCHANGES = frozenset({"SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX"})


@dataclass(frozen=True)
class PortfolioRiskLimits:
    """Account-level limits used by the shared portfolio broker."""

    max_margin_usage: float
    max_symbol_exposure: float
    daily_loss_stop: float
    max_symbol_margin_usage: float = 1.0
    max_open_positions: int | None = None
    max_trade_risk: float = 1.0
    max_group_positions: int | None = None

    def __post_init__(self) -> None:
        if not 0 < self.max_margin_usage <= 1:
            raise ValueError("max_margin_usage must be in (0, 1].")
        if self.max_symbol_exposure <= 0:
            raise ValueError("max_symbol_exposure must be positive.")
        if not 0 < self.daily_loss_stop <= 1:
            raise ValueError("daily_loss_stop must be in (0, 1].")
        if not 0 < self.max_symbol_margin_usage <= 1:
            raise ValueError("max_symbol_margin_usage must be in (0, 1].")
        if self.max_open_positions is not None and self.max_open_positions <= 0:
            raise ValueError("max_open_positions must be positive when supplied.")
        if not 0 < self.max_trade_risk <= 1:
            raise ValueError("max_trade_risk must be in (0, 1].")
        if self.max_group_positions is not None and self.max_group_positions <= 0:
            raise ValueError("max_group_positions must be positive when supplied.")


@dataclass
class PortfolioBacktestResult:
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    summary: dict[str, float | int | str]
    rejections: pd.DataFrame


@dataclass
class SharedPortfolioBroker:
    """One cash ledger and one margin pool shared by every traded contract.

    No FX conversion is performed.  Contracts are accepted only when they are
    known to be denominated in ``base_currency``.  Domestic exchange contracts
    default to CNY; every other market requires an explicit homogeneous
    ``symbol_currencies`` mapping.
    """

    initial_cash: float
    contract_specs: Mapping[str, ContractSpec]
    risk_limits: PortfolioRiskLimits
    slippage_ticks: int = 0
    base_currency: str = "CNY"
    symbol_currencies: Mapping[str, str] | None = None
    symbol_groups: Mapping[str, str] | None = None
    cash: float = field(init=False)
    realized_pnl: float = field(default=0.0, init=False)
    positions: dict[str, Position] = field(default_factory=dict, init=False)
    trade_rows: list[dict[str, object]] = field(default_factory=list, init=False)
    session_date: date | None = field(default=None, init=False)
    session_start_equity: float | None = field(default=None, init=False)
    previous_close_equity: float | None = field(default=None, init=False)
    _validated_currencies: dict[str, str] = field(default_factory=dict, init=False)
    _validated_groups: dict[str, str] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive.")
        if self.slippage_ticks < 0:
            raise ValueError("slippage_ticks cannot be negative.")
        if not self.contract_specs:
            raise ValueError("contract_specs cannot be empty.")
        self.base_currency = self.base_currency.strip().upper()
        if not self.base_currency:
            raise ValueError("base_currency cannot be empty.")

        copied_specs: dict[str, ContractSpec] = {}
        for symbol, spec in self.contract_specs.items():
            if symbol != spec.symbol:
                raise ValueError(
                    f"Contract spec key {symbol!r} does not match spec symbol {spec.symbol!r}."
                )
            validate_contract_spec(spec)
            copied_specs[symbol] = spec
        self.contract_specs = copied_specs
        self.cash = float(self.initial_cash)

    def validate_symbols(self, symbols: Iterable[str]) -> None:
        active_symbols = sorted(set(symbols))
        missing_specs = [symbol for symbol in active_symbols if symbol not in self.contract_specs]
        if missing_specs:
            raise ValueError(f"Missing ContractSpec for symbols: {missing_specs}")

        if self.risk_limits.max_group_positions is not None:
            if self.symbol_groups is None:
                raise ValueError(
                    "symbol_groups is required when max_group_positions is configured."
                )
            normalized_groups = {
                str(symbol): str(group).strip()
                for symbol, group in self.symbol_groups.items()
            }
            missing_groups = [
                symbol for symbol in active_symbols if not normalized_groups.get(symbol)
            ]
            if missing_groups:
                raise ValueError(f"symbol_groups is missing entries for: {missing_groups}")
            self._validated_groups = {
                symbol: normalized_groups[symbol] for symbol in active_symbols
            }

        if self.symbol_currencies is None:
            if self.base_currency != "CNY":
                raise ValueError(
                    "FX conversion is not implemented. Without symbol_currencies, "
                    "only CNY domestic contracts can be used with base_currency='CNY'."
                )
            foreign = [
                symbol
                for symbol in active_symbols
                if self.contract_specs[symbol].exchange.strip().upper()
                not in DOMESTIC_CNY_EXCHANGES
            ]
            if foreign:
                raise ValueError(
                    "FX conversion is not implemented; non-domestic contracts require "
                    "an explicit symbol_currencies mapping and all currencies must equal "
                    f"base_currency. Affected symbols: {foreign}"
                )
            self._validated_currencies = {symbol: "CNY" for symbol in active_symbols}
            return

        normalized = {
            str(symbol): str(currency).strip().upper()
            for symbol, currency in self.symbol_currencies.items()
        }
        missing_currencies = [symbol for symbol in active_symbols if symbol not in normalized]
        if missing_currencies:
            raise ValueError(
                "FX conversion is not implemented; symbol_currencies is missing entries "
                f"for: {missing_currencies}"
            )
        mismatched = {
            symbol: normalized[symbol]
            for symbol in active_symbols
            if normalized[symbol] != self.base_currency
        }
        if mismatched:
            raise ValueError(
                "FX conversion is not implemented; every active contract currency must "
                f"equal base_currency={self.base_currency}. Mismatches: {mismatched}"
            )
        self._validated_currencies = {
            symbol: normalized[symbol] for symbol in active_symbols
        }

    def position(self, symbol: str) -> Position:
        return self.positions.setdefault(symbol, Position(symbol=symbol))

    def currency(self, symbol: str) -> str:
        try:
            return self._validated_currencies[symbol]
        except KeyError as exc:
            raise ValueError(f"Currency for {symbol} has not been validated.") from exc

    def equity(self, marks: Mapping[str, float]) -> float:
        unrealized = 0.0
        for symbol, position in self.positions.items():
            if not position.quantity:
                continue
            mark = _required_mark(marks, symbol)
            multiplier = self.contract_specs[symbol].contract_multiplier
            unrealized += (mark - position.avg_price) * position.quantity * multiplier
        return self.cash + unrealized

    def current_margin(self, marks: Mapping[str, float]) -> float:
        margin = 0.0
        for symbol, position in self.positions.items():
            if not position.quantity:
                continue
            spec = self.contract_specs[symbol]
            mark = _required_mark(marks, symbol)
            margin += abs(position.quantity) * mark * spec.contract_multiplier * spec.margin_rate
        return margin

    def gross_notional(self, marks: Mapping[str, float]) -> float:
        return sum(
            abs(position.quantity)
            * _required_mark(marks, symbol)
            * self.contract_specs[symbol].contract_multiplier
            for symbol, position in self.positions.items()
            if position.quantity
        )

    def net_notional(self, marks: Mapping[str, float]) -> float:
        return sum(
            position.quantity
            * _required_mark(marks, symbol)
            * self.contract_specs[symbol].contract_multiplier
            for symbol, position in self.positions.items()
            if position.quantity
        )

    def begin_timestamp(self, timestamp: datetime, marks: Mapping[str, float]) -> None:
        timestamp_date = timestamp.date()
        if self.session_date != timestamp_date:
            self.session_date = timestamp_date
            self.session_start_equity = (
                self.previous_close_equity
                if self.previous_close_equity is not None
                else self.equity(marks)
            )

    def end_timestamp(self, marks: Mapping[str, float]) -> None:
        self.previous_close_equity = self.equity(marks)

    def is_risk_reducing(self, symbol: str, order_quantity: int) -> bool:
        current = self.position(symbol).quantity
        projected = current + order_quantity
        return _same_direction_reduction_or_flat(current, projected)

    def submit_order(
        self,
        order: Order,
        bar: Bar,
        marks: Mapping[str, float],
        *,
        force_liquidation: bool = False,
    ) -> tuple[bool, str]:
        if order.symbol != bar.symbol:
            raise ValueError("Order symbol must match execution bar symbol.")
        if order.symbol not in self.contract_specs:
            raise ValueError(f"Missing ContractSpec for {order.symbol}.")
        if not order.quantity:
            return False, "zero_quantity"
        if order.price <= 0:
            return False, "price_not_positive"

        spec = self.contract_specs[order.symbol]
        current_position = self.position(order.symbol).quantity
        projected_position = current_position + order.quantity
        risk_reducing = _same_direction_reduction_or_flat(
            current_position, projected_position
        )
        if force_liquidation and not risk_reducing:
            raise ValueError("force_liquidation may only reduce or flatten a position.")

        fill_price = order.price + (
            self.slippage_ticks
            * spec.tick_size
            * (1 if order.quantity > 0 else -1)
        )
        if fill_price <= 0:
            return False, "fill_price_not_positive"

        current_equity = self.equity(marks)
        current_margin = self.current_margin(marks)
        loss_stop_hit = (
            self.session_start_equity is not None
            and current_equity
            <= self.session_start_equity * (1 - self.risk_limits.daily_loss_stop)
        )
        if loss_stop_hit and not risk_reducing and not force_liquidation:
            return False, "daily_loss_stop"

        if not risk_reducing and not force_liquidation:
            if current_equity <= 0:
                return False, "equity_not_positive"
            current_mark = _required_mark(marks, order.symbol)
            current_symbol_margin = (
                abs(current_position)
                * current_mark
                * spec.contract_multiplier
                * spec.margin_rate
            )
            projected_symbol_margin = (
                abs(projected_position)
                * fill_price
                * spec.contract_multiplier
                * spec.margin_rate
            )
            projected_margin = max(0.0, current_margin - current_symbol_margin)
            projected_margin += projected_symbol_margin
            if projected_margin / current_equity > self.risk_limits.max_margin_usage:
                return False, "max_margin_usage_exceeded"
            if (
                projected_symbol_margin / current_equity
                > self.risk_limits.max_symbol_margin_usage
            ):
                return False, "max_symbol_margin_usage_exceeded"

            open_count = sum(position.quantity != 0 for position in self.positions.values())
            projected_open_count = open_count
            if current_position == 0 and projected_position != 0:
                projected_open_count += 1
            elif current_position != 0 and projected_position == 0:
                projected_open_count -= 1
            if (
                self.risk_limits.max_open_positions is not None
                and projected_open_count > self.risk_limits.max_open_positions
            ):
                return False, "max_open_positions_exceeded"

            if (
                current_position == 0
                and projected_position != 0
                and self.risk_limits.max_group_positions is not None
            ):
                group = self._validated_groups[order.symbol]
                group_open_count = sum(
                    position.quantity != 0
                    and self._validated_groups.get(symbol) == group
                    for symbol, position in self.positions.items()
                )
                if group_open_count >= self.risk_limits.max_group_positions:
                    return False, "max_group_positions_exceeded"

            if order.stop_price is not None and projected_position:
                if (
                    projected_position > 0
                    and order.stop_price >= fill_price
                    or projected_position < 0
                    and order.stop_price <= fill_price
                ):
                    return False, "protective_stop_crossed_before_entry"
                initial_risk = (
                    abs(fill_price - order.stop_price)
                    * abs(projected_position)
                    * spec.contract_multiplier
                )
                if initial_risk / current_equity > self.risk_limits.max_trade_risk:
                    return False, "max_trade_risk_exceeded"

            projected_notional = (
                abs(projected_position) * fill_price * spec.contract_multiplier
            )
            if (
                projected_notional / current_equity
                > self.risk_limits.max_symbol_exposure
            ):
                return False, "max_symbol_exposure_exceeded"

        commission = (
            abs(order.quantity)
            * fill_price
            * spec.contract_multiplier
            * spec.commission_rate
        )
        trade = Trade(
            datetime=order.datetime,
            symbol=order.symbol,
            quantity=order.quantity,
            price=fill_price,
            commission=commission,
            reason=order.reason,
        )
        realized_points = self.position(order.symbol).update(trade)
        realized_pnl = realized_points * spec.contract_multiplier
        self.realized_pnl += realized_pnl
        self.cash += realized_pnl - commission
        self.trade_rows.append(
            {
                "datetime": order.datetime,
                "symbol": order.symbol,
                "quantity": order.quantity,
                "price": fill_price,
                "reference_price": order.price,
                "commission": commission,
                "slippage_cost": (
                    abs(order.quantity)
                    * abs(fill_price - order.price)
                    * spec.contract_multiplier
                ),
                "realized_pnl": realized_pnl,
                "cash_after": self.cash,
                "contract_multiplier": spec.contract_multiplier,
                "tick_size": spec.tick_size,
                "margin_rate": spec.margin_rate,
                "commission_rate": spec.commission_rate,
                "currency": self.currency(order.symbol),
                "reason": order.reason,
            }
        )
        return True, "filled"


def run_portfolio_backtest(
    bars: Iterable[Bar] | Mapping[str, Iterable[Bar]],
    strategies: Strategy | Mapping[str, Strategy],
    broker: SharedPortfolioBroker,
) -> PortfolioBacktestResult:
    """Run a synchronized, next-open portfolio backtest on one shared account.

    At each timestamp all available open prices are marked first, pending
    signals are executed, all available closes are marked, and only then are
    new signals generated.  A signal therefore cannot fill before the next bar
    available for its own symbol.
    """

    ordered_bars = _normalize_bars(bars)
    if not ordered_bars:
        empty = pd.DataFrame()
        return PortfolioBacktestResult(
            equity_curve=empty,
            trades=empty,
            summary={"status": "empty", "base_currency": broker.base_currency},
            rejections=empty,
        )

    active_symbols = {bar.symbol for bar in ordered_bars}
    broker.validate_symbols(active_symbols)
    _validate_strategy_mapping(strategies, active_symbols)

    marks: dict[str, float] = {}
    pending_signals: dict[str, Signal] = {}
    last_bars: dict[str, Bar] = {}
    equity_rows: list[dict[str, object]] = []
    rejected_rows: list[dict[str, object]] = []

    grouped = groupby(ordered_bars, key=lambda item: item.datetime)
    for timestamp, timestamp_group in grouped:
        event_bars = list(timestamp_group)
        bars_by_symbol = {bar.symbol: bar for bar in event_bars}

        # Synchronous open: account risk sees every instrument's new open mark
        # before any order at this timestamp is considered.
        for bar in event_bars:
            marks[bar.symbol] = bar.open
            last_bars[bar.symbol] = bar
        broker.begin_timestamp(timestamp, marks)

        orders: list[tuple[int, str, Order, Signal, Bar]] = []
        for symbol in sorted(bars_by_symbol):
            pending = pending_signals.pop(symbol, None)
            if pending is None:
                continue
            current_position = broker.position(symbol).quantity
            delta = pending.target_position - current_position
            if not delta:
                continue
            order = Order(
                datetime=timestamp,
                symbol=symbol,
                quantity=delta,
                price=bars_by_symbol[symbol].open,
                reason=f"{pending.reason}; execution=next_open",
                stop_price=pending.stop_price,
            )
            priority = 0 if broker.is_risk_reducing(symbol, delta) else 1
            orders.append((priority, symbol, order, pending, bars_by_symbol[symbol]))

        # Reductions are processed before openings so released shared margin is
        # immediately available. Symbol sorting makes competing opens repeatable.
        for _, symbol, order, pending, bar in sorted(orders):
            filled, status = broker.submit_order(order, bar, marks)
            if not filled:
                rejected_rows.append(
                    {
                        "datetime": timestamp,
                        "symbol": symbol,
                        "quantity": order.quantity,
                        "target_position": pending.target_position,
                        "status": status,
                        "reason": order.reason,
                    }
                )
                _strategy_for(strategies, symbol).on_order_rejected(pending, status)
            position = broker.position(symbol)
            _strategy_for(strategies, symbol).on_position_update(
                symbol, position.quantity, position.avg_price
            )

        # Synchronous close: signals are generated only after every close mark
        # at this timestamp is visible to account valuation.
        for bar in event_bars:
            marks[bar.symbol] = bar.close
        for symbol in sorted(bars_by_symbol):
            bar = bars_by_symbol[symbol]
            strategy = _strategy_for(strategies, symbol)
            strategy.on_account_update(broker.equity(marks))
            signal = strategy.on_bar(bar)
            if signal is None:
                continue
            _validate_signal(signal, bar)
            if signal.immediate:
                current_position = broker.position(symbol).quantity
                delta = signal.target_position - current_position
                if delta and not broker.is_risk_reducing(symbol, delta):
                    raise ValueError(
                        "Immediate strategy signals may only reduce or flatten a position."
                    )
                if delta:
                    order = Order(
                        datetime=timestamp,
                        symbol=symbol,
                        quantity=delta,
                        price=(
                            signal.execution_price
                            if signal.execution_price is not None
                            else bar.close
                        ),
                        reason=f"{signal.reason}; execution=intrabar_protective",
                    )
                    filled, status = broker.submit_order(order, bar, marks)
                    if not filled:
                        rejected_rows.append(
                            {
                                "datetime": timestamp,
                                "symbol": symbol,
                                "quantity": delta,
                                "target_position": signal.target_position,
                                "status": status,
                                "reason": order.reason,
                            }
                        )
                        strategy.on_order_rejected(signal, status)
                    position = broker.position(symbol)
                    strategy.on_position_update(
                        symbol, position.quantity, position.avg_price
                    )
                else:
                    position = broker.position(symbol)
                    strategy.on_position_update(
                        symbol, position.quantity, position.avg_price
                    )
            else:
                pending_signals[symbol] = signal

        equity_rows.append(_equity_snapshot(timestamp, broker, marks))
        broker.end_timestamp(marks)

    final_timestamp = ordered_bars[-1].datetime
    for symbol in sorted(broker.positions):
        position = broker.position(symbol)
        if not position.quantity:
            continue
        last_bar = last_bars[symbol]
        marks[symbol] = last_bar.close
        liquidation_bar = Bar(
            datetime=final_timestamp,
            symbol=symbol,
            open=last_bar.close,
            high=last_bar.close,
            low=last_bar.close,
            close=last_bar.close,
            volume=0.0,
            open_interest=last_bar.open_interest,
        )
        liquidation = Order(
            datetime=final_timestamp,
            symbol=symbol,
            quantity=-position.quantity,
            price=last_bar.close,
            reason="end_of_portfolio_backtest_liquidation",
        )
        filled, status = broker.submit_order(
            liquidation,
            liquidation_bar,
            marks,
            force_liquidation=True,
        )
        if not filled:
            rejected_rows.append(
                {
                    "datetime": final_timestamp,
                    "symbol": symbol,
                    "quantity": liquidation.quantity,
                    "target_position": 0,
                    "status": status,
                    "reason": liquidation.reason,
                }
            )
        position = broker.position(symbol)
        _strategy_for(strategies, symbol).on_position_update(
            symbol, position.quantity, position.avg_price
        )

    if equity_rows:
        equity_rows[-1] = _equity_snapshot(final_timestamp, broker, marks)
        broker.end_timestamp(marks)

    equity_curve = pd.DataFrame(equity_rows)
    trades = pd.DataFrame(broker.trade_rows)
    rejections = pd.DataFrame(rejected_rows)
    summary = _summarize_portfolio(
        equity_curve,
        trades,
        rejections,
        broker.initial_cash,
        broker.base_currency,
        len(active_symbols),
    )
    return PortfolioBacktestResult(
        equity_curve=equity_curve,
        trades=trades,
        summary=summary,
        rejections=rejections,
    )


def summarize_portfolio_period(
    result: PortfolioBacktestResult,
    start: str | datetime | pd.Timestamp,
    *,
    initial_account_cash: float,
) -> dict[str, float | int | str]:
    """Summarize a sealed evaluation period while preserving pre-period state.

    The strategy and positions may be warmed up before ``start``. Performance
    is anchored to the last equity observation before the period, so the
    evaluation does not reset positions or leak a new entry assumption.
    """
    if result.equity_curve.empty:
        return {"status": "empty"}
    start_at = pd.Timestamp(start)
    curve = result.equity_curve.copy()
    timestamps = pd.to_datetime(curve["datetime"])
    before = curve.loc[timestamps < start_at]
    anchor_equity = (
        float(before["equity"].iloc[-1]) if not before.empty else float(initial_account_cash)
    )
    phase_curve = curve.loc[timestamps >= start_at].copy()
    if phase_curve.empty:
        raise ValueError(f"No portfolio equity observations on or after {start_at}.")

    if result.trades.empty:
        phase_trades = result.trades.copy()
    else:
        trade_times = pd.to_datetime(result.trades["datetime"])
        phase_trades = result.trades.loc[trade_times >= start_at].copy()
    if result.rejections.empty:
        phase_rejections = result.rejections.copy()
    else:
        rejection_times = pd.to_datetime(result.rejections["datetime"])
        phase_rejections = result.rejections.loc[rejection_times >= start_at].copy()

    symbol_count = int(result.summary.get("symbol_count", 0))
    base_currency = str(result.summary.get("base_currency", ""))
    summary = _summarize_portfolio(
        phase_curve,
        phase_trades,
        phase_rejections,
        anchor_equity,
        base_currency,
        symbol_count,
    )
    summary["evaluation_anchor_equity"] = round(anchor_equity, 2)
    summary["positions_carried_into_period"] = True
    return summary


def _normalize_bars(
    bars: Iterable[Bar] | Mapping[str, Iterable[Bar]],
) -> list[Bar]:
    flattened: list[Bar] = []
    if isinstance(bars, Mapping):
        for expected_symbol, symbol_bars in bars.items():
            for bar in symbol_bars:
                if bar.symbol != expected_symbol:
                    raise ValueError(
                        f"Bar symbol {bar.symbol!r} does not match mapping key "
                        f"{expected_symbol!r}."
                    )
                flattened.append(bar)
    else:
        flattened.extend(bars)

    flattened.sort(key=lambda item: (item.datetime, item.symbol))
    seen: set[tuple[datetime, str]] = set()
    for bar in flattened:
        key = (bar.datetime, bar.symbol)
        if key in seen:
            raise ValueError(
                f"Duplicate bar for symbol={bar.symbol} datetime={bar.datetime.isoformat()}."
            )
        seen.add(key)
        if min(bar.open, bar.high, bar.low, bar.close) <= 0:
            raise ValueError(
                f"Bar prices must be positive for {bar.symbol} at {bar.datetime.isoformat()}."
            )
    return flattened


def _validate_strategy_mapping(
    strategies: Strategy | Mapping[str, Strategy], symbols: set[str]
) -> None:
    if not isinstance(strategies, Mapping):
        return
    missing = sorted(symbol for symbol in symbols if symbol not in strategies)
    if missing:
        raise ValueError(f"Missing strategy for symbols: {missing}")


def _strategy_for(
    strategies: Strategy | Mapping[str, Strategy], symbol: str
) -> Strategy:
    if isinstance(strategies, Mapping):
        return strategies[symbol]
    return strategies


def _validate_signal(signal: Signal, bar: Bar) -> None:
    if signal.symbol != bar.symbol:
        raise ValueError("Strategy signal symbol must match the current bar symbol.")
    if signal.datetime != bar.datetime:
        raise ValueError("Strategy signal datetime must equal the current close datetime.")
    if isinstance(signal.target_position, bool) or not isinstance(
        signal.target_position, int
    ):
        raise ValueError("Signal target_position must be an integer contract count.")


def _same_direction_reduction_or_flat(current: int, projected: int) -> bool:
    if current == 0:
        return False
    if projected == 0:
        return True
    return current * projected > 0 and abs(projected) < abs(current)


def _required_mark(marks: Mapping[str, float], symbol: str) -> float:
    try:
        return float(marks[symbol])
    except KeyError as exc:
        raise ValueError(f"Missing mark price for open position {symbol}.") from exc


def _equity_snapshot(
    timestamp: datetime,
    broker: SharedPortfolioBroker,
    marks: Mapping[str, float],
) -> dict[str, object]:
    equity = broker.equity(marks)
    margin = broker.current_margin(marks)
    positions = {
        symbol: position.quantity
        for symbol, position in sorted(broker.positions.items())
        if position.quantity
    }
    session_return = 0.0
    if broker.session_start_equity:
        session_return = equity / broker.session_start_equity - 1
    return {
        "datetime": timestamp,
        "cash": broker.cash,
        "equity": equity,
        "margin": margin,
        "margin_usage": margin / equity if equity > 0 else math.inf,
        "gross_notional": broker.gross_notional(marks),
        "net_notional": broker.net_notional(marks),
        "session_return": session_return,
        "active_position_count": len(positions),
        "positions": json.dumps(positions, ensure_ascii=False, sort_keys=True),
        "currency": broker.base_currency,
    }


def _summarize_portfolio(
    equity_curve: pd.DataFrame,
    trades: pd.DataFrame,
    rejections: pd.DataFrame,
    initial_cash: float,
    base_currency: str,
    symbol_count: int,
) -> dict[str, float | int | str]:
    if equity_curve.empty:
        return {"status": "empty", "base_currency": base_currency}

    equity = equity_curve["equity"].astype(float)
    return_base = pd.concat(
        [pd.Series([float(initial_cash)]), equity.reset_index(drop=True)],
        ignore_index=True,
    )
    returns = return_base.pct_change().dropna()
    peaks = return_base.cummax()
    max_drawdown = float((return_base / peaks - 1).min())
    timestamps = pd.to_datetime(equity_curve["datetime"])
    elapsed_years = 0.0
    if len(timestamps) > 1:
        elapsed_seconds = (timestamps.iloc[-1] - timestamps.iloc[0]).total_seconds()
        elapsed_years = max(elapsed_seconds / (365.25 * 24 * 60 * 60), 0.0)
    observation_count = max(len(equity_curve), 1)
    periods_per_year = observation_count / elapsed_years if elapsed_years > 0 else 252.0

    final_equity = float(equity.iloc[-1])
    total_return = final_equity / initial_cash - 1
    annualized_return = total_return
    if elapsed_years > 0 and final_equity > 0:
        annualized_return = (final_equity / initial_cash) ** (1 / elapsed_years) - 1
    annualized_volatility = (
        float(returns.std() * math.sqrt(periods_per_year))
        if len(returns) > 1
        else 0.0
    )
    sharpe = 0.0
    if len(returns) > 1 and returns.std() != 0:
        sharpe = float(returns.mean() / returns.std() * math.sqrt(periods_per_year))
    calmar = annualized_return / abs(max_drawdown) if max_drawdown < 0 else 0.0

    realized_trade_count = 0
    winning_realizations = 0
    commission_total = 0.0
    slippage_total = 0.0
    realized_pnl_total = 0.0
    if not trades.empty:
        realized = trades["realized_pnl"].astype(float)
        realized_trade_count = int((realized != 0).sum())
        winning_realizations = int((realized > 0).sum())
        commission_total = float(trades["commission"].astype(float).sum())
        slippage_total = float(trades["slippage_cost"].astype(float).sum())
        realized_pnl_total = float(realized.sum())

    return {
        "status": "ok",
        "base_currency": base_currency,
        "start": str(timestamps.iloc[0].date()),
        "end": str(timestamps.iloc[-1].date()),
        "symbol_count": symbol_count,
        "initial_cash": round(float(initial_cash), 2),
        "final_cash": round(float(equity_curve["cash"].iloc[-1]), 2),
        "final_equity": round(final_equity, 2),
        "total_return": round(float(total_return), 6),
        "annualized_return": round(float(annualized_return), 6),
        "annualized_volatility": round(float(annualized_volatility), 6),
        "periods_per_year": round(float(periods_per_year), 4),
        "max_drawdown": round(max_drawdown, 6),
        "sharpe": round(sharpe, 4),
        "calmar": round(float(calmar), 4),
        "max_margin_usage_observed": round(
            float(equity_curve["margin_usage"].replace(math.inf, float("nan")).max()),
            6,
        ),
        "trade_count": int(len(trades)),
        "realized_trade_count": realized_trade_count,
        "winning_realizations": winning_realizations,
        "commission_total": round(commission_total, 6),
        "slippage_cost_total": round(slippage_total, 6),
        "realized_pnl_before_commission": round(realized_pnl_total, 6),
        "rejected_order_count": int(len(rejections)),
        "open_position_count": int(equity_curve["active_position_count"].iloc[-1]),
    }
