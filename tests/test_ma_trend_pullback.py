from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from futures_quant.broker.backtest import BacktestBroker, run_backtest
from futures_quant.broker.portfolio import (
    PortfolioRiskLimits,
    SharedPortfolioBroker,
    run_portfolio_backtest,
)
from futures_quant.data.contracts import ContractSpec
from futures_quant.models import Bar, Order, Signal
from futures_quant.risk.rules import RiskEngine, RiskLimits
from futures_quant.strategies.base import Strategy
from futures_quant.strategies.ma_trend_pullback import TrendPullbackMovingAverageStrategy


def make_bar(
    index: int,
    close: float,
    *,
    symbol: str = "A",
    open_price: float | None = None,
    low: float | None = None,
    high: float | None = None,
) -> Bar:
    return Bar(
        datetime=datetime(2025, 1, 6, 9) + timedelta(minutes=15 * index),
        symbol=symbol,
        open=close if open_price is None else open_price,
        high=close + 0.1 if high is None else high,
        low=close - 0.1 if low is None else low,
        close=close,
        volume=1000,
        open_interest=500,
    )


class TrendPullbackStrategyTest(unittest.TestCase):
    def _strategy(
        self, *, order_size: int = 5, fraction: float = 0.30
    ) -> TrendPullbackMovingAverageStrategy:
        return TrendPullbackMovingAverageStrategy(
            fast_window=2,
            slow_window=5,
            order_size=order_size,
            atr_window=2,
            ma_exit_buffer_atr=0.1,
            partial_exit_fraction=fraction,
            position_equity_fraction=0.60,
            initial_cash=100_000,
            contract_multiplier=10,
            margin_rate=0.10,
            max_trade_risk=1.0,
            max_notional_fraction=10.0,
        )

    def _open_long(self, strategy: TrendPullbackMovingAverageStrategy) -> Signal:
        prices = [10, 10, 10, 10, 9, 11]
        signal = None
        for index, price in enumerate(prices):
            signal = strategy.on_bar(make_bar(index, price))
        self.assertIsNotNone(signal)
        assert signal is not None
        return signal

    def _open_short(self, strategy: TrendPullbackMovingAverageStrategy) -> Signal:
        prices = [10, 10, 10, 10, 11, 9]
        signal = None
        for index, price in enumerate(prices):
            signal = strategy.on_bar(make_bar(index, price))
        self.assertIsNotNone(signal)
        assert signal is not None
        return signal

    def test_ma169_cross_opens_once_and_not_while_price_remains_above(self) -> None:
        strategy = self._strategy()
        entry = self._open_long(strategy)
        self.assertEqual(entry.target_position, 5)
        self.assertIsNotNone(entry.stop_price)
        self.assertIn("ma169_cross long", entry.reason)

        strategy.on_position_update("A", 5, 11.2)
        self.assertIsNone(strategy.on_bar(make_bar(6, 12.0)))

    def test_long_ma13_reverse_cross_exits_thirty_percent_of_remaining(self) -> None:
        strategy = self._strategy()
        self._open_long(strategy)
        strategy.on_position_update("A", 5, 11.0)
        self.assertIsNone(strategy.on_bar(make_bar(6, 12.0)))
        partial = strategy.on_bar(make_bar(7, 10.5))
        self.assertIsNotNone(partial)
        assert partial is not None
        self.assertFalse(partial.immediate)
        self.assertEqual(partial.target_position, 4)
        self.assertIn("ma13_reverse_cross_long", partial.reason)

        strategy.on_position_update("A", 4, 11.0)
        self.assertIsNone(strategy.on_bar(make_bar(8, 10.6)))
        self.assertIsNone(strategy.on_bar(make_bar(9, 13.0)))
        second = strategy.on_bar(make_bar(10, 11.8))
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second.target_position, 3)

    def test_small_remaining_position_exits_at_least_one_contract(self) -> None:
        strategy = self._strategy(order_size=1)
        self._open_long(strategy)
        strategy.on_position_update("A", 1, 11.0)
        strategy.on_bar(make_bar(6, 12.0))
        signal = strategy.on_bar(make_bar(7, 10.5))
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.target_position, 0)

    def test_buffered_ma169_stop_precedes_ma13_scale_out(self) -> None:
        strategy = self._strategy()
        self._open_long(strategy)
        strategy.on_position_update("A", 5, 11.0)
        strategy.on_bar(make_bar(6, 12.0))
        signal = strategy.on_bar(make_bar(7, 8.0))
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.target_position, 0)
        self.assertIn("buffered_slow_ma_stop_long", signal.reason)

    def test_short_rules_are_mirrored(self) -> None:
        strategy = self._strategy()
        entry = self._open_short(strategy)
        self.assertEqual(entry.target_position, -5)
        strategy.on_position_update("A", -5, 9.0)
        self.assertIsNone(strategy.on_bar(make_bar(6, 8.0)))
        partial = strategy.on_bar(make_bar(7, 9.5))
        self.assertIsNotNone(partial)
        assert partial is not None
        self.assertEqual(partial.target_position, -4)
        self.assertIn("ma13_reverse_cross_short", partial.reason)

    def test_short_buffered_ma169_stop_is_mirrored(self) -> None:
        strategy = self._strategy()
        self._open_short(strategy)
        strategy.on_position_update("A", -5, 9.0)
        strategy.on_bar(make_bar(6, 8.0))
        signal = strategy.on_bar(make_bar(7, 12.0))
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.target_position, 0)
        self.assertIn("buffered_slow_ma_stop_short", signal.reason)

    def test_entry_signal_fills_at_next_bar_open(self) -> None:
        strategy = self._strategy(order_size=1)
        bars = [
            make_bar(index, price)
            for index, price in enumerate([10, 10, 10, 10, 9, 11])
        ]
        bars.append(make_bar(6, 12.0, open_price=12.34, high=12.40))
        risk = RiskEngine(RiskLimits(1.0, 10.0, 1.0, 0.1, 10, 1.0, 1.0))
        broker = BacktestBroker(100_000, 0.0, 0, 0.01, 10, 0.1, risk)
        result = run_backtest(bars, strategy, broker)
        self.assertGreaterEqual(len(result.trades), 2)
        self.assertEqual(float(result.trades.iloc[0]["price"]), 12.34)
        self.assertIn("execution=next_open", str(result.trades.iloc[0]["reason"]))

    def test_auto_size_is_capped_at_five_lots(self) -> None:
        strategy = TrendPullbackMovingAverageStrategy(
            fast_window=2,
            slow_window=5,
            order_size=0,
            atr_window=2,
            position_equity_fraction=0.60,
            initial_cash=100_000,
            contract_multiplier=10,
            margin_rate=0.10,
            max_trade_risk=0.005,
            max_notional_fraction=10.0,
        )
        # Margin cap=600, notional cap=1000 and risk cap=500, but the
        # strategy's hard maximum is five contracts.
        self.assertEqual(strategy._position_size(100.0, 99.9), 5)
        strategy.on_account_update(50_000)
        self.assertEqual(strategy._position_size(100.0, 99.9), 5)

    def test_rejects_order_size_above_five_lots(self) -> None:
        with self.assertRaisesRegex(ValueError, "5-lot"):
            self._strategy(order_size=6)

    def test_rejected_entry_resets_provisional_position_state(self) -> None:
        strategy = self._strategy()
        prices = [10, 10, 10, 10, 9, 11, 12]
        bars = [make_bar(index, price) for index, price in enumerate(prices)]
        broker = SharedPortfolioBroker(
            100_000,
            {"A": ContractSpec("A", "SHFE", "test", 10, 1.0, 0.1, 0.0)},
            PortfolioRiskLimits(1.0, 10.0, 0.5, 1.0, 5, 0.000001),
        )

        result = run_portfolio_backtest(bars, strategy, broker)

        self.assertEqual(result.summary["rejected_order_count"], 1)
        self.assertTrue(result.trades.empty)
        self.assertEqual(strategy.last_target["A"], 0)
        self.assertNotIn("A", strategy.pending_entries)


class ImmediateExitStrategy(Strategy):
    def __init__(self) -> None:
        self.index = 0

    def on_bar(self, bar: Bar) -> Signal | None:
        self.index += 1
        if self.index == 1:
            return Signal(bar.datetime, bar.symbol, 1, "entry", stop_price=90)
        if self.index == 2:
            return Signal(
                bar.datetime,
                bar.symbol,
                0,
                "protective",
                execution_price=95,
                immediate=True,
            )
        return None


class ImmediateExecutionTest(unittest.TestCase):
    def test_protective_reduction_fills_on_current_bar(self) -> None:
        risk = RiskEngine(RiskLimits(1.0, 2.0, 0.5, 0.1, 1, 1.0, 1.0))
        broker = BacktestBroker(100_000, 0.0, 0, 1.0, 1, 0.1, risk)
        bars = [make_bar(0, 100), make_bar(1, 100), make_bar(2, 100)]
        result = run_backtest(bars, ImmediateExitStrategy(), broker)
        self.assertEqual(len(result.trades), 2)
        self.assertEqual(float(result.trades.iloc[1]["price"]), 95.0)
        self.assertIn("intrabar_protective", str(result.trades.iloc[1]["reason"]))


class PortfolioRiskExtensionTest(unittest.TestCase):
    @staticmethod
    def _spec(symbol: str) -> ContractSpec:
        return ContractSpec(symbol, "SHFE", "test", 10, 1.0, 0.1, 0.0)

    def test_initial_stop_risk_rejects_fixed_five_lots(self) -> None:
        broker = SharedPortfolioBroker(
            100_000,
            {"A": self._spec("A")},
            PortfolioRiskLimits(1.0, 10.0, 0.5, 1.0, 5, 0.005),
        )
        broker.validate_symbols(["A"])
        bar = make_bar(0, 100)
        filled, reason = broker.submit_order(
            Order(bar.datetime, "A", 5, 100, "entry", stop_price=80),
            bar,
            {"A": 100.0},
        )
        self.assertFalse(filled)
        self.assertEqual(reason, "max_trade_risk_exceeded")

    def test_third_position_in_same_group_is_rejected(self) -> None:
        symbols = ["A", "B", "C"]
        specs = {symbol: self._spec(symbol) for symbol in symbols}
        broker = SharedPortfolioBroker(
            100_000,
            specs,
            PortfolioRiskLimits(1.0, 10.0, 0.5, 1.0, 5, 1.0, 2),
            symbol_groups={symbol: "black" for symbol in symbols},
        )
        broker.validate_symbols(symbols)
        marks = {symbol: 100.0 for symbol in symbols}
        for index, symbol in enumerate(symbols):
            bar = make_bar(index, 100, symbol=symbol)
            filled, reason = broker.submit_order(
                Order(bar.datetime, symbol, 1, 100, "entry", stop_price=99),
                bar,
                marks,
            )
            if index < 2:
                self.assertTrue(filled)
            else:
                self.assertFalse(filled)
                self.assertEqual(reason, "max_group_positions_exceeded")


if __name__ == "__main__":
    unittest.main()
