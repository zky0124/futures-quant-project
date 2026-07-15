from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from futures_quant.broker.portfolio import PortfolioRiskLimits, SharedPortfolioBroker
from futures_quant.data.contracts import ContractSpec
from futures_quant.models import Bar, Order
from futures_quant.strategies.dual_period_reversal import DualPeriodReversalStrategy


def make_bar(when: datetime, symbol: str, close: float, *, low: float | None = None) -> Bar:
    return Bar(
        datetime=when,
        symbol=symbol,
        open=close,
        high=close + 0.2,
        low=close - 0.2 if low is None else low,
        close=close,
        volume=1000,
        open_interest=500,
    )


class DualPeriodReversalTest(unittest.TestCase):
    def _strategy(self) -> DualPeriodReversalStrategy:
        return DualPeriodReversalStrategy(
            fast_window=2,
            slow_window=4,
            order_size=5,
            daily_fast_window=2,
            daily_slow_window=3,
            extreme_lookback_days=5,
            setup_valid_days=3,
            macd_fast=2,
            macd_slow=3,
            macd_signal=2,
            divergence_lookback=10,
            divergence_pivot_radius=1,
            divergence_valid_bars=5,
            second_cross_window=8,
            atr_window=2,
            atr_stop_buffer=0.2,
            reward_risk=2.0,
            trailing_atr_multiple=2.5,
        )

    def test_daily_bar_is_not_visible_until_next_trading_day(self) -> None:
        strategy = self._strategy()
        start = datetime(2025, 1, 6, 15)
        strategy.on_bar(make_bar(start, "A", 10))
        self.assertEqual(len(strategy.daily_closes["A"]), 0)
        strategy.on_bar(make_bar(start + timedelta(days=1), "A", 9))
        self.assertEqual(list(strategy.daily_closes["A"]), [10])

    def test_intraday_cross_after_recent_divergence_opens_exactly_five(self) -> None:
        strategy = self._strategy()
        start = datetime(2025, 1, 6, 9)
        for index, price in enumerate([10, 9, 8, 7, 7]):
            strategy.on_bar(make_bar(start + timedelta(minutes=15 * index), "A", price))
        strategy.setup_direction["A"] = 1
        strategy.setup_days_left["A"] = 3
        strategy.last_bull_divergence["A"] = strategy.bar_number["A"]
        signal = strategy.on_bar(make_bar(start + timedelta(minutes=75), "A", 12))
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.target_position, 5)
        self.assertIn("golden_cross", signal.reason)

        stop = strategy.stop_price["A"]
        exit_signal = strategy.on_bar(
            make_bar(start + timedelta(minutes=90), "A", stop, low=stop - 0.1)
        )
        self.assertIsNotNone(exit_signal)
        assert exit_signal is not None
        self.assertEqual(exit_signal.target_position, 0)
        self.assertIn("protective_stop_long", exit_signal.reason)

    def test_friday_night_maps_to_monday_trading_day(self) -> None:
        bar = make_bar(datetime(2025, 1, 10, 21), "A", 100)
        self.assertEqual(self._strategy()._trading_day(bar).isoformat(), "2025-01-13")


class PortfolioHardLimitTest(unittest.TestCase):
    @staticmethod
    def _spec(symbol: str, *, multiplier: int = 1, margin: float = 0.1) -> ContractSpec:
        return ContractSpec(symbol, "SHFE", "test", multiplier, 1.0, margin, 0.0)

    def test_sixth_open_position_is_rejected(self) -> None:
        symbols = [f"S{index}" for index in range(6)]
        specs = {symbol: self._spec(symbol) for symbol in symbols}
        broker = SharedPortfolioBroker(
            100_000,
            specs,
            PortfolioRiskLimits(1.0, 1.0, 0.5, 1.0, 5),
        )
        broker.validate_symbols(symbols)
        marks = {symbol: 100.0 for symbol in symbols}
        for index, symbol in enumerate(symbols):
            bar = make_bar(datetime(2025, 1, 6, 9, index), symbol, 100)
            filled, reason = broker.submit_order(
                Order(bar.datetime, symbol, 1, 100, "test"), bar, marks
            )
            if index < 5:
                self.assertTrue(filled)
            else:
                self.assertFalse(filled)
                self.assertEqual(reason, "max_open_positions_exceeded")

    def test_symbol_margin_above_twenty_percent_is_rejected(self) -> None:
        spec = self._spec("A", multiplier=10, margin=0.5)
        broker = SharedPortfolioBroker(
            10_000,
            {"A": spec},
            PortfolioRiskLimits(1.0, 1.0, 0.5, 0.20, 5),
        )
        broker.validate_symbols(["A"])
        bar = make_bar(datetime(2025, 1, 6, 9), "A", 100)
        filled, reason = broker.submit_order(
            Order(bar.datetime, "A", 5, 100, "five_lots"), bar, {"A": 100.0}
        )
        self.assertFalse(filled)
        self.assertEqual(reason, "max_symbol_margin_usage_exceeded")


if __name__ == "__main__":
    unittest.main()
