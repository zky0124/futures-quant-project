import unittest
from datetime import datetime, timedelta
from pathlib import Path

from futures_quant.config import load_backtest_config
from futures_quant.models import Bar
from futures_quant.strategies.adaptive_trend import AdaptiveTrendStrategy


def make_bar(day: int, close: float, high: float | None = None, low: float | None = None) -> Bar:
    return Bar(
        datetime=datetime(2025, 1, 1) + timedelta(days=day),
        symbol="TEST",
        open=close,
        high=high if high is not None else close + 0.1,
        low=low if low is not None else close - 0.1,
        close=close,
        volume=1000,
    )


def small_strategy(**overrides: object) -> AdaptiveTrendStrategy:
    params: dict[str, object] = {
        "entry_window": 3,
        "exit_window": 2,
        "trend_window": 3,
        "momentum_window": 3,
        "volatility_window": 2,
        "target_annual_volatility": 0.15,
        "order_size": 1,
        "max_order_size": 100,
    }
    params.update(overrides)
    return AdaptiveTrendStrategy(**params)


class AdaptiveTrendStrategyTest(unittest.TestCase):
    def test_breakout_channel_excludes_current_bar(self) -> None:
        strategy = small_strategy(max_order_size=4)
        for day, close in enumerate([100.0, 100.2, 100.4]):
            self.assertIsNone(strategy.on_bar(make_bar(day, close)))

        # The current high is far above the close. A channel that accidentally
        # included the current bar would not classify this as a breakout.
        signal = strategy.on_bar(make_bar(3, 101.0, high=150.0, low=100.8))
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertGreater(signal.target_position, 0)

    def test_exit_channel_flattens_an_existing_long(self) -> None:
        strategy = small_strategy(max_order_size=4)
        for day, close in enumerate([100.0, 100.2, 100.4]):
            strategy.on_bar(make_bar(day, close))
        entry = strategy.on_bar(make_bar(3, 101.0))
        self.assertIsNotNone(entry)

        exit_signal = strategy.on_bar(make_bar(4, 99.0))
        self.assertIsNotNone(exit_signal)
        assert exit_signal is not None
        self.assertEqual(exit_signal.target_position, 0)

    def test_volatility_targeting_reduces_size_in_high_volatility(self) -> None:
        common = {
            "initial_cash": 100_000.0,
            "contract_multiplier": 10.0,
            "max_notional_fraction": 0.50,
        }
        low_vol = small_strategy(**common)
        high_vol = small_strategy(**common)

        for day, close in enumerate([100.0, 100.1, 100.2]):
            low_vol.on_bar(make_bar(day, close))
        low_signal = low_vol.on_bar(make_bar(3, 101.0))

        for day, close in enumerate([100.0, 105.0, 99.0]):
            high_vol.on_bar(make_bar(day, close))
        high_signal = high_vol.on_bar(make_bar(3, 110.0))

        self.assertIsNotNone(low_signal)
        self.assertIsNotNone(high_signal)
        assert low_signal is not None and high_signal is not None
        self.assertGreater(low_signal.target_position, high_signal.target_position)
        self.assertGreater(high_signal.target_position, 0)

    def test_notional_cap_prevents_an_oversized_target(self) -> None:
        strategy = small_strategy(
            initial_cash=100_000.0,
            contract_multiplier=10.0,
            max_notional_fraction=0.10,
        )
        for day, close in enumerate([100.0, 100.1, 100.2]):
            strategy.on_bar(make_bar(day, close))
        signal = strategy.on_bar(make_bar(3, 101.0))

        self.assertIsNotNone(signal)
        assert signal is not None
        maximum_contracts = int(100_000 * 0.10 // (101.0 * 10.0))
        self.assertLessEqual(signal.target_position, maximum_contracts)

    def test_no_trade_when_one_contract_exceeds_notional_cap(self) -> None:
        strategy = small_strategy(
            initial_cash=500.0,
            contract_multiplier=10.0,
            max_notional_fraction=0.10,
        )
        for day, close in enumerate([100.0, 100.1, 100.2]):
            strategy.on_bar(make_bar(day, close))
        self.assertIsNone(strategy.on_bar(make_bar(3, 101.0)))
        self.assertEqual(strategy.last_target["TEST"], 0)

    def test_legacy_and_adaptive_configs_both_load(self) -> None:
        root = Path(__file__).resolve().parents[1]
        legacy = load_backtest_config(root / "configs/backtest.json", root)
        adaptive = load_backtest_config(root / "configs/backtest_adaptive.json", root)
        self.assertEqual(legacy.strategy.name, "dual_ma")
        self.assertEqual(legacy.strategy.entry_window, 20)
        self.assertEqual(adaptive.strategy.name, "adaptive_trend")
        self.assertEqual(adaptive.strategy.max_order_size, 8)


if __name__ == "__main__":
    unittest.main()
