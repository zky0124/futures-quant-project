from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

import pandas as pd

from futures_quant.models import Bar, Order, Position, Signal, Trade
from futures_quant.risk.rules import RiskEngine
from futures_quant.strategies.base import Strategy


@dataclass
class BacktestResult:
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    summary: dict[str, float | int | str]
    rejections: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass
class BacktestBroker:
    initial_cash: float
    commission_rate: float
    slippage_ticks: int
    tick_size: float
    contract_multiplier: int
    margin_rate: float
    risk_engine: RiskEngine
    cash: float = field(init=False)
    realized_pnl: float = field(default=0.0, init=False)
    positions: dict[str, Position] = field(default_factory=dict, init=False)
    trades: list[Trade] = field(default_factory=list, init=False)
    session_date: date | None = field(default=None, init=False)
    session_start_equity: float | None = field(default=None, init=False)
    previous_close_equity: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.cash = self.initial_cash

    def position(self, symbol: str) -> Position:
        return self.positions.setdefault(symbol, Position(symbol=symbol))

    def current_margin(self, marks: dict[str, float]) -> float:
        total = 0.0
        for symbol, pos in self.positions.items():
            if pos.quantity:
                total += abs(pos.quantity) * marks[symbol] * self.contract_multiplier * self.margin_rate
        return total

    def equity(self, marks: dict[str, float]) -> float:
        unrealized = 0.0
        for symbol, pos in self.positions.items():
            if pos.quantity:
                unrealized += (marks[symbol] - pos.avg_price) * pos.quantity * self.contract_multiplier
        return self.cash + unrealized

    def begin_bar(self, bar: Bar, marks: dict[str, float]) -> None:
        """Initialize the loss-stop reference when a new calendar day starts."""
        bar_date = bar.datetime.date()
        if self.session_date != bar_date:
            self.session_date = bar_date
            self.session_start_equity = (
                self.previous_close_equity
                if self.previous_close_equity is not None
                else self.equity(marks)
            )

    def end_bar(self, marks: dict[str, float]) -> None:
        self.previous_close_equity = self.equity(marks)

    def submit_order(self, order: Order, bar: Bar, marks: dict[str, float]) -> tuple[bool, str]:
        current_margin = self.current_margin(marks)
        current_position = self.position(order.symbol).quantity
        projected_position = current_position + order.quantity
        current_equity = self.equity(marks)
        loss_stop_hit = (
            self.session_start_equity is not None
            and current_equity
            <= self.session_start_equity * (1 - self.risk_engine.limits.daily_loss_stop)
        )
        reduces_without_reversal = (
            projected_position == 0
            or (
                current_position * projected_position > 0
                and abs(projected_position) < abs(current_position)
            )
        )
        if loss_stop_hit and not reduces_without_reversal:
            return False, "daily_loss_stop"
        allowed, reason = self.risk_engine.check_order(
            order,
            bar,
            current_equity,
            current_margin,
            current_position=current_position,
        )
        if not allowed:
            return False, reason

        fill_price = order.price + self.slippage_ticks * self.tick_size * (1 if order.quantity > 0 else -1)
        commission = abs(order.quantity) * fill_price * self.contract_multiplier * self.commission_rate
        position = self.position(order.symbol)
        closed_quantity = (
            min(abs(position.quantity), abs(order.quantity))
            if position.quantity * order.quantity < 0
            else 0
        )
        trade = Trade(
            datetime=order.datetime,
            symbol=order.symbol,
            quantity=order.quantity,
            price=fill_price,
            commission=commission,
            reason=order.reason,
        )
        realized_points = position.update(trade)
        realized_pnl = realized_points * self.contract_multiplier
        trade = replace(
            trade,
            closed_quantity=closed_quantity,
            realized_pnl=realized_pnl,
        )
        self.realized_pnl += realized_pnl
        self.cash += realized_pnl - commission
        self.trades.append(trade)
        return True, "filled"


def run_backtest(bars: list[Bar], strategy: Strategy, broker: BacktestBroker) -> BacktestResult:
    marks: dict[str, float] = {}
    equity_rows: list[dict[str, object]] = []
    rejected_orders: list[dict[str, object]] = []
    pending_signals: dict[str, Signal] = {}
    last_bars: dict[str, Bar] = {}

    for bar in bars:
        last_bars[bar.symbol] = bar
        # A signal is only known after the current bar closes. Execute it at
        # the next bar's open so the backtest does not use an unavailable
        # same-close fill.
        marks[bar.symbol] = bar.open
        broker.begin_bar(bar, marks)
        pending = pending_signals.pop(bar.symbol, None)
        if pending is not None:
            pos = broker.position(pending.symbol)
            delta = pending.target_position - pos.quantity
            if delta:
                order = Order(
                    datetime=bar.datetime,
                    symbol=bar.symbol,
                    quantity=delta,
                    price=bar.open,
                    reason=f"{pending.reason}; execution=next_open",
                    stop_price=pending.stop_price,
                )
                ok, status = broker.submit_order(order, bar, marks)
                if not ok:
                    rejected_orders.append(
                        {
                            "datetime": bar.datetime,
                            "symbol": bar.symbol,
                            "quantity": delta,
                            "status": status,
                        }
                    )
                    strategy.on_order_rejected(pending, status)
                position = broker.position(pending.symbol)
                strategy.on_position_update(
                    pending.symbol, position.quantity, position.avg_price
                )
            else:
                strategy.on_position_update(
                    pending.symbol, pos.quantity, pos.avg_price
                )

        marks[bar.symbol] = bar.close
        strategy.on_account_update(broker.equity(marks))
        signal = strategy.on_bar(bar)
        if signal is not None:
            if signal.immediate:
                pos = broker.position(signal.symbol)
                delta = signal.target_position - pos.quantity
                projected = pos.quantity + delta
                risk_reducing = (
                    pos.quantity != 0
                    and (
                        projected == 0
                        or (
                            pos.quantity * projected > 0
                            and abs(projected) < abs(pos.quantity)
                        )
                    )
                )
                if delta and not risk_reducing:
                    raise ValueError(
                        "Immediate strategy signals may only reduce or flatten a position."
                    )
                if delta:
                    order = Order(
                        datetime=bar.datetime,
                        symbol=bar.symbol,
                        quantity=delta,
                        price=(
                            signal.execution_price
                            if signal.execution_price is not None
                            else bar.close
                        ),
                        reason=f"{signal.reason}; execution=intrabar_protective",
                    )
                    ok, status = broker.submit_order(order, bar, marks)
                    if not ok:
                        rejected_orders.append(
                            {
                                "datetime": bar.datetime,
                                "symbol": bar.symbol,
                                "quantity": delta,
                                "status": status,
                            }
                        )
                        strategy.on_order_rejected(signal, status)
                    position = broker.position(signal.symbol)
                    strategy.on_position_update(
                        signal.symbol, position.quantity, position.avg_price
                    )
                else:
                    strategy.on_position_update(
                        signal.symbol, pos.quantity, pos.avg_price
                    )
            else:
                pending_signals[bar.symbol] = signal

        equity = broker.equity(marks)
        margin = broker.current_margin(marks)
        pos = broker.position(bar.symbol)
        equity_rows.append(
            {
                "datetime": bar.datetime,
                "symbol": bar.symbol,
                "close": bar.close,
                "position": pos.quantity,
                "cash": broker.cash,
                "equity": equity,
                "margin": margin,
                "margin_usage": margin / equity if equity else 0.0,
            }
        )
        broker.end_bar(marks)

    # Realize the final marked position and charge exit slippage/commission.
    # Without this, short samples systematically overstate net performance.
    for symbol, pos in list(broker.positions.items()):
        if not pos.quantity or symbol not in last_bars:
            continue
        bar = last_bars[symbol]
        marks[symbol] = bar.close
        liquidation = Order(
            datetime=bar.datetime,
            symbol=symbol,
            quantity=-pos.quantity,
            price=bar.close,
            reason="end_of_backtest_liquidation",
        )
        ok, status = broker.submit_order(liquidation, bar, marks)
        if not ok:
            rejected_orders.append(
                {
                    "datetime": bar.datetime,
                    "symbol": symbol,
                    "quantity": liquidation.quantity,
                    "status": status,
                }
            )
        position = broker.position(symbol)
        strategy.on_position_update(symbol, position.quantity, position.avg_price)

    if equity_rows:
        final_symbol = str(equity_rows[-1]["symbol"])
        final_equity = broker.equity(marks)
        final_margin = broker.current_margin(marks)
        equity_rows[-1].update(
            {
                "position": broker.position(final_symbol).quantity,
                "cash": broker.cash,
                "equity": final_equity,
                "margin": final_margin,
                "margin_usage": final_margin / final_equity if final_equity else 0.0,
            }
        )

    equity_curve = pd.DataFrame(equity_rows)
    trades = pd.DataFrame([t.__dict__ for t in broker.trades])
    summary = summarize(equity_curve, trades, rejected_orders, broker.initial_cash)
    return BacktestResult(
        equity_curve=equity_curve,
        trades=trades,
        summary=summary,
        rejections=pd.DataFrame(rejected_orders),
    )


def summarize(equity_curve: pd.DataFrame, trades: pd.DataFrame, rejected: list[dict[str, object]], initial_cash: float) -> dict[str, float | int | str]:
    if equity_curve.empty:
        return {"status": "empty"}
    equity = equity_curve["equity"].astype(float)
    returns = equity.pct_change().fillna(0.0)
    timestamps = pd.to_datetime(equity_curve["datetime"])
    elapsed_years = 0.0
    if len(timestamps) > 1:
        elapsed_seconds = (timestamps.iloc[-1] - timestamps.iloc[0]).total_seconds()
        elapsed_years = max(elapsed_seconds / (365.25 * 24 * 60 * 60), 0.0)
    observation_count = max(len(equity) - 1, 1)
    periods_per_year = observation_count / elapsed_years if elapsed_years > 0 else 252.0
    total_return = equity.iloc[-1] / initial_cash - 1
    peak = equity.cummax()
    drawdown = equity / peak - 1
    max_drawdown = drawdown.min()
    sharpe = 0.0
    if returns.std() != 0:
        sharpe = (returns.mean() / returns.std()) * (periods_per_year ** 0.5)
    annualized_return = 0.0
    if equity.iloc[-1] > 0 and initial_cash > 0:
        annualized_return = (
            (equity.iloc[-1] / initial_cash) ** (1 / elapsed_years) - 1
            if elapsed_years > 0
            else float(total_return)
        )
    annualized_volatility = (
        float(returns.std() * (periods_per_year ** 0.5)) if len(returns) > 1 else 0.0
    )
    calmar = annualized_return / abs(max_drawdown) if max_drawdown < 0 else 0.0
    wins = 0
    losses = 0
    closed_trade_count = 0
    if not trades.empty and {"closed_quantity", "realized_pnl"}.issubset(trades.columns):
        closed = trades[trades["closed_quantity"].astype(int) > 0]
        closed_trade_count = int(len(closed))
        wins = int((closed["realized_pnl"].astype(float) > 0).sum())
        losses = int((closed["realized_pnl"].astype(float) < 0).sum())
    decided_closures = wins + losses
    return {
        "status": "ok",
        "start": str(equity_curve["datetime"].iloc[0].date()),
        "end": str(equity_curve["datetime"].iloc[-1].date()),
        "initial_cash": round(initial_cash, 2),
        "final_equity": round(float(equity.iloc[-1]), 2),
        "total_return": round(float(total_return), 6),
        "annualized_return": round(float(annualized_return), 6),
        "annualized_volatility": round(float(annualized_volatility), 6),
        "periods_per_year": round(float(periods_per_year), 4),
        "max_drawdown": round(float(max_drawdown), 6),
        "sharpe": round(float(sharpe), 4),
        "calmar": round(float(calmar), 4),
        "trade_count": int(len(trades)),
        "closed_trade_count": closed_trade_count,
        "winning_closures": wins,
        "losing_closures": losses,
        "gross_win_rate": round(wins / decided_closures, 6) if decided_closures else 0.0,
        "rejected_order_count": int(len(rejected)),
    }
