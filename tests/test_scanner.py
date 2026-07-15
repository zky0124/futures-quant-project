from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from futures_quant.analysis.scanner import scan_instruments
from futures_quant.broker.portfolio import PortfolioRiskLimits
from futures_quant.config import StrategyConfig
from futures_quant.data.contracts import ContractSpec
from futures_quant.data.history import Instrument


class InstrumentScannerTest(unittest.TestCase):
    def test_scan_ranks_successes_and_keeps_file_errors_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            history = Path(temp_dir)
            timestamps = pd.date_range("2025-01-02 09:15", periods=50, freq="15min")
            for symbol, prices in {
                "A": [100 + index * 0.6 for index in range(50)],
                "B": [100 + (index % 8) * 0.2 for index in range(50)],
            }.items():
                pd.DataFrame(
                    {
                        "datetime": timestamps,
                        "symbol": symbol,
                        "open": prices,
                        "high": [price + 0.2 for price in prices],
                        "low": [price - 0.2 for price in prices],
                        "close": prices,
                        "volume": 100,
                        "open_interest": 500,
                    }
                ).to_csv(history / f"{symbol}_15m.csv", index=False)

            instruments = [
                Instrument(symbol, symbol, "test-group", 100, 0, 0.01, index)
                for index, symbol in enumerate(["A", "B", "C"], start=1)
            ]
            specs = {
                symbol: ContractSpec(symbol, "SHFE", symbol, 10, 0.1, 0.1, 0.0)
                for symbol in ["A", "B", "C"]
            }

            result = scan_instruments(
                instruments,
                data_dir=history,
                suffix="_15m.csv",
                source_interval_minutes=15,
                bar_interval_minutes=15,
                strategy_config=StrategyConfig(
                    name="dual_ma", fast_window=2, slow_window=5, order_size=1
                ),
                initial_cash=100_000,
                max_symbol_exposure=1.0,
                risk_limits=PortfolioRiskLimits(
                    1.0, 1.0, 0.5, 1.0, 5, 1.0, 2
                ),
                slippage_ticks=0,
                contract_specs=specs,
            )

            self.assertEqual(result["rank"].tolist(), [1, 2, 3])
            self.assertTrue(result.iloc[:2]["status"].eq("ok").all())
            self.assertEqual(result.iloc[-1]["symbol"], "C")
            self.assertEqual(result.iloc[-1]["status"], "error")
            self.assertIn("C_15m.csv", result.iloc[-1]["error"])
            successful_returns = result.iloc[:2]["total_return"].tolist()
            self.assertGreaterEqual(successful_returns[0], successful_returns[1])


if __name__ == "__main__":
    unittest.main()
