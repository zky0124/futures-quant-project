from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from futures_quant.presentation.chinese import (
    chinese_column_name,
    chinese_frame,
    write_chinese_companion,
)


class ChinesePresentationTest(unittest.TestCase):
    def test_position_trade_and_pnl_headers_are_chinese(self) -> None:
        positions = chinese_frame(
            pd.DataFrame([{"symbol": "RB0", "position": 5, "status": "ok"}])
        )
        self.assertEqual(list(positions.columns), ["品种代码", "持仓手数", "状态"])
        self.assertEqual(positions.iloc[0]["状态"], "正常")

        trades = chinese_frame(
            pd.DataFrame(
                [
                    {
                        "datetime": "2026-07-12 09:00:00",
                        "symbol": "RB0",
                        "quantity": 5,
                        "price": 3200.0,
                        "commission": 8.0,
                        "reason": "test",
                    }
                ]
            )
        )
        self.assertEqual(
            list(trades.columns),
            ["时间", "品种代码", "成交手数", "成交价格", "手续费", "成交原因"],
        )

        pnl = chinese_frame(
            pd.DataFrame(
                [
                    {
                        "realized_pnl": 100.0,
                        "commission": 8.0,
                        "net_realized_pnl": 92.0,
                    }
                ]
            )
        )
        self.assertEqual(list(pnl.columns), ["已实现盈亏", "手续费", "已实现净盈亏"])

    def test_future_optimization_parameter_headers_use_chinese_prefix(self) -> None:
        self.assertEqual(chinese_column_name("param_fast_window"), "参数-短周期窗口")
        self.assertEqual(chinese_column_name("train_total_return"), "训练-总收益率")
        self.assertEqual(chinese_column_name("validation_max_drawdown"), "验证-最大回撤")

    def test_chinese_companion_keeps_internal_csv_and_writes_chinese_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "trades.csv"
            frame = pd.DataFrame([{"symbol": "RB0", "quantity": 5, "price": 3200.0}])
            frame.to_csv(source, index=False, encoding="utf-8-sig")
            companion = write_chinese_companion(frame, source)

            self.assertTrue(source.exists())
            self.assertEqual(companion.name, "trades_中文.csv")
            loaded = pd.read_csv(companion)
            self.assertEqual(list(loaded.columns), ["品种代码", "成交手数", "成交价格"])


if __name__ == "__main__":
    unittest.main()
