from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from futures_quant.models import Bar, Signal
from futures_quant.strategies.adaptive_trend_v2 import EnhancedAdaptiveTrendStrategy


def make_bar(
    index: int,
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
) -> Bar:
    return Bar(
        datetime=datetime(2025, 1, 2, 9, 15) + timedelta(minutes=15 * index),
        symbol="A",
        open=close,
        high=close + 0.1 if high is None else high,
        low=close - 0.1 if low is None else low,
        close=close,
        volume=1000,
        open_interest=500,
    )


class EnhancedAdaptiveTrendTest(unittest.TestCase):
    @staticmethod
    def _strategy(max_order_size: int = 5) -> EnhancedAdaptiveTrendStrategy:
        return EnhancedAdaptiveTrendStrategy(
            entry_window=3,
            exit_window=2,
            trend_window=4,
            momentum_window=2,
            volatility_window=3,
            target_annual_volatility=10.0,
            order_size=1,
            max_order_size=max_order_size,
            initial_cash=100_000,
            contract_multiplier=1,
            margin_rate=0.1,
            max_notional_fraction=1.0,
            max_margin_fraction=1.0,
            max_trade_risk=1.0,
            annualization_factor=1,
            atr_window=3,
            atr_stop_multiple=2.5,
            break_even_trigger_r=1.0,
            reward_risk=2.0,
            partial_exit_fraction=0.4,
            trailing_atr_multiple=3.0,
            cooldown_bars=2,
            loss_pause_after=3,
            loss_pause_bars=8,
        )

    def _long_entry(self, strategy: EnhancedAdaptiveTrendStrategy) -> Signal:
        signal = None
        for index, price in enumerate([10, 11, 12, 13, 14]):
            signal = strategy.on_bar(make_bar(index, price))
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertGreater(signal.target_position, 0)
        strategy.on_position_update("A", signal.target_position, 14.2)
        return signal

    def test_dynamic_entry_is_bounded_to_five_lots(self) -> None:
        strategy = self._strategy()

        signal = self._long_entry(strategy)

        self.assertEqual(signal.target_position, 5)
        self.assertIsNotNone(signal.stop_price)

    def test_five_lots_reduce_by_two_at_2r(self) -> None:
        strategy = self._strategy()
        self._long_entry(strategy)
        target = strategy.first_target["A"]
        stop = strategy.stop_price["A"]

        signal = strategy.on_bar(
            make_bar(5, target, high=target + 0.1, low=stop + 0.1)
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertTrue(signal.immediate)
        self.assertEqual(signal.target_position, 3)
        strategy.on_position_update("A", 3, strategy.entry_price["A"])
        self.assertTrue(strategy.partial_taken["A"])
        self.assertEqual(strategy.stop_price["A"], strategy.entry_price["A"])

    def test_stop_has_priority_over_2r_on_the_same_bar(self) -> None:
        strategy = self._strategy()
        self._long_entry(strategy)
        target = strategy.first_target["A"]
        stop = strategy.stop_price["A"]

        signal = strategy.on_bar(
            make_bar(5, 14.2, high=target + 0.1, low=stop - 0.1)
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.target_position, 0)
        self.assertIn("protective_stop_long", signal.reason)

    def test_one_lot_activates_trailing_without_partial_order(self) -> None:
        strategy = self._strategy(max_order_size=1)
        self._long_entry(strategy)
        target = strategy.first_target["A"]
        stop = strategy.stop_price["A"]

        signal = strategy.on_bar(
            make_bar(5, strategy.entry_price["A"], high=target + 0.1, low=stop + 0.1)
        )

        self.assertIsNone(signal)
        self.assertTrue(strategy.partial_taken["A"])
        self.assertEqual(strategy.stop_price["A"], strategy.entry_price["A"])

    def test_short_partial_is_mirrored(self) -> None:
        strategy = self._strategy()
        strategy._open_state("A", -5, 100.0, 105.0, 5.0)
        strategy.on_position_update("A", -5, 100.0)

        signal = strategy._partial_signal(
            make_bar(0, 91.0, high=104.0, low=89.0), direction=-1
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.target_position, -3)
        self.assertIn("partial_2R_short", signal.reason)

    def test_rejected_entry_clears_provisional_state(self) -> None:
        strategy = self._strategy()
        signal = self._long_entry(strategy)
        strategy.on_order_rejected(signal, "max_trade_risk_exceeded")
        strategy.on_position_update("A", 0, 0.0)

        self.assertEqual(strategy.last_target["A"], 0)
        self.assertNotIn("A", strategy.pending_entries)
        self.assertNotIn("A", strategy.stop_price)

    def test_high_volatility_never_produces_more_size_than_low_volatility(self) -> None:
        strategy = self._strategy()
        low_size, _ = strategy._position_size([100, 100.1, 100.2, 100.3], 100, 2)
        high_size, _ = strategy._position_size([100, 120, 90, 130], 100, 2)

        self.assertLessEqual(high_size, low_size)
        self.assertLessEqual(low_size, 5)


if __name__ == "__main__":
    unittest.main()
