from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from futures_quant.optimization.walk_forward import optimize_strategy


class StrategyOptimizationTest(unittest.TestCase):
    def _write_fixture(self, root: Path, alter_final_test: bool) -> tuple[Path, Path, Path, Path]:
        dates = pd.bdate_range("2025-01-02", periods=100)
        instruments = [
            {
                "symbol": "A",
                "name": "A",
                "group": "test",
                "base_price": 100,
                "drift": 0,
                "volatility": 0.01,
                "seed": 1,
            },
            {
                "symbol": "B",
                "name": "B",
                "group": "test",
                "base_price": 200,
                "drift": 0,
                "volatility": 0.01,
                "seed": 2,
            },
        ]
        universe_path = root / "universe.json"
        universe_path.write_text(
            json.dumps(
                {
                    "start": dates[0].date().isoformat(),
                    "end": dates[-1].date().isoformat(),
                    "instruments": instruments,
                }
            ),
            encoding="utf-8",
        )
        history_dir = root / "history"
        history_dir.mkdir()
        for symbol_number, instrument in enumerate(instruments, start=1):
            prices: list[float] = []
            price = float(instrument["base_price"])
            for index in range(len(dates)):
                phase = (index // (5 + symbol_number)) % 2
                price *= 1.004 if phase == 0 else 0.997
                if alter_final_test and index >= 75:
                    price *= 1.03 if index % 2 else 0.96
                prices.append(price)
            frame = pd.DataFrame(
                {
                    "datetime": dates,
                    "symbol": instrument["symbol"],
                    "open": prices,
                    "high": [value * 1.01 for value in prices],
                    "low": [value * 0.99 for value in prices],
                    "close": prices,
                    "volume": 1000,
                    "open_interest": 500,
                }
            )
            frame.to_csv(history_dir / f"{instrument['symbol']}_1d.csv", index=False)

        base_config_path = root / "backtest.json"
        base_config_path.write_text(
            json.dumps(
                {
                    "initial_cash": 100000,
                    "commission_rate": 0.0001,
                    "slippage_ticks": 1,
                    "tick_size": 0.1,
                    "contract_multiplier": 10,
                    "margin_rate": 0.1,
                    "max_margin_usage": 0.5,
                    "max_symbol_exposure": 0.5,
                    "daily_loss_stop": 0.1,
                    "strategy": {
                        "name": "dual_ma",
                        "fast_window": 3,
                        "slow_window": 12,
                        "order_size": 1,
                    },
                    "data": {"symbol": "A", "path": "history/A_1d.csv"},
                    "contracts": {},
                    "report": {"path": "summary.csv"},
                }
            ),
            encoding="utf-8",
        )
        optimization_path = root / "optimization.json"
        optimization_path.write_text(
            json.dumps(
                {
                    "strategy": {
                        "name": "dual_ma",
                        "parameter_grid": {
                            "fast_window": [2, 4],
                            "slow_window": [10, 14],
                            "order_size": [1],
                        },
                    },
                    "split": {
                        "train_fraction": 0.5,
                        "validation_fraction": 0.25,
                        "min_bars_per_phase": 10,
                    },
                    "objective": {
                        "metric": "robust_score",
                        "selection_method": "min_train_validation",
                        "min_trades": 0,
                    },
                    "sensitivity": {
                        "commission_multipliers": [1.0, 2.0],
                        "slippage_ticks": [0, 2],
                    },
                }
            ),
            encoding="utf-8",
        )
        return base_config_path, optimization_path, universe_path, history_dir

    def test_final_test_changes_cannot_change_candidate_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            clean_root = root / "clean"
            changed_root = root / "changed"
            clean_root.mkdir()
            changed_root.mkdir()
            clean_fixture = self._write_fixture(clean_root, alter_final_test=False)
            changed_fixture = self._write_fixture(changed_root, alter_final_test=True)

            clean_outputs = optimize_strategy(
                *clean_fixture[:3],
                clean_fixture[3],
                clean_root / "output",
                project_root=clean_root,
            )
            changed_outputs = optimize_strategy(
                *changed_fixture[:3],
                changed_fixture[3],
                changed_root / "output",
                project_root=changed_root,
            )

            clean_selected = json.loads(
                clean_outputs["selected_parameters"].read_text(encoding="utf-8")
            )
            changed_selected = json.loads(
                changed_outputs["selected_parameters"].read_text(encoding="utf-8")
            )
            self.assertEqual(clean_selected["parameters"], changed_selected["parameters"])
            self.assertFalse(clean_selected["final_test_used_for_selection"])

            clean_ranking = pd.read_csv(clean_outputs["candidate_ranking"])
            changed_ranking = pd.read_csv(changed_outputs["candidate_ranking"])
            pd.testing.assert_series_equal(
                clean_ranking["selection_score"],
                changed_ranking["selection_score"],
                check_names=False,
            )
            self.assertFalse(any(column.startswith("test_") for column in clean_ranking.columns))
            winner = clean_ranking.iloc[0]
            self.assertAlmostEqual(
                float(winner["selection_score"]),
                min(float(winner["train_score"]), float(winner["validation_score"])),
            )

            sensitivity = pd.read_csv(clean_outputs["cost_sensitivity"])
            self.assertEqual(len(sensitivity), 4)
            self.assertTrue((sensitivity["used_for_selection"].astype(str) == "False").all())
            for path in clean_outputs.values():
                self.assertTrue(path.exists())

    def test_staged_optimization_freezes_both_stages_before_final_test(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            clean_root = root / "clean"
            changed_root = root / "changed"
            clean_root.mkdir()
            changed_root.mkdir()
            clean_fixture = self._write_fixture(clean_root, alter_final_test=False)
            changed_fixture = self._write_fixture(changed_root, alter_final_test=True)
            staged = {
                "strategy": {
                    "name": "dual_ma",
                    "fixed_parameters": {"order_size": 1},
                    "stages": [
                        {
                            "name": "structure",
                            "parameter_grid": {
                                "fast_window": [2, 4],
                                "slow_window": [10],
                            },
                        },
                        {
                            "name": "risk",
                            "parameter_grid": {"slow_window": [10, 14]},
                        },
                    ],
                },
                "split": {
                    "train_fraction": 0.5,
                    "validation_fraction": 0.25,
                    "min_bars_per_phase": 10,
                },
                "objective": {
                    "metric": "robust_score",
                    "selection_method": "min_train_validation",
                    "min_trades": 0,
                },
                "sensitivity": {
                    "commission_multipliers": [2.0],
                    "slippage_ticks": [3],
                },
            }
            clean_fixture[1].write_text(json.dumps(staged), encoding="utf-8")
            changed_fixture[1].write_text(json.dumps(staged), encoding="utf-8")

            clean_outputs = optimize_strategy(
                *clean_fixture[:3],
                clean_fixture[3],
                clean_root / "output",
                project_root=clean_root,
            )
            changed_outputs = optimize_strategy(
                *changed_fixture[:3],
                changed_fixture[3],
                changed_root / "output",
                project_root=changed_root,
            )

            clean_selected = json.loads(
                clean_outputs["selected_parameters"].read_text(encoding="utf-8")
            )
            changed_selected = json.loads(
                changed_outputs["selected_parameters"].read_text(encoding="utf-8")
            )
            self.assertEqual(
                clean_selected["stage1_parameters"],
                changed_selected["stage1_parameters"],
            )
            self.assertEqual(
                clean_selected["parameters"], changed_selected["parameters"]
            )
            self.assertFalse(clean_selected["final_test_used_for_selection"])
            self.assertTrue(clean_outputs["stage1_structure_ranking"].exists())
            self.assertTrue(clean_outputs["stage2_risk_ranking"].exists())
            self.assertTrue(clean_outputs["strategy_comparison"].exists())
            self.assertTrue(clean_outputs["promotion_decision"].exists())


if __name__ == "__main__":
    unittest.main()
