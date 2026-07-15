from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from futures_quant.data.timeframe import aggregate_bars, validate_intervals
from futures_quant.models import Bar


def make_bar(index: int, price: float, *, hour: int = 9, minute: int = 15) -> Bar:
    timestamp = datetime(2025, 1, 2, hour, minute) + timedelta(minutes=15 * index)
    return Bar(
        datetime=timestamp,
        symbol="RB0",
        open=price,
        high=price + 2,
        low=price - 1,
        close=price + 1,
        volume=10 + index,
        open_interest=100 + index,
    )


class TimeframeAggregationTest(unittest.TestCase):
    def test_four_15_minute_bars_form_one_60_minute_bar(self) -> None:
        bars = [make_bar(index, 100 + index) for index in range(4)]

        result = aggregate_bars(bars, target_minutes=60, source_minutes=15)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].datetime, bars[-1].datetime)
        self.assertEqual(result[0].open, 100)
        self.assertEqual(result[0].high, 105)
        self.assertEqual(result[0].low, 99)
        self.assertEqual(result[0].close, 104)
        self.assertEqual(result[0].volume, sum(bar.volume for bar in bars))
        self.assertEqual(result[0].open_interest, bars[-1].open_interest)

    def test_session_gap_flushes_an_incomplete_bucket(self) -> None:
        morning = [make_bar(0, 100), make_bar(1, 101)]
        afternoon = [
            make_bar(0, 110, hour=13, minute=45),
            make_bar(1, 111, hour=13, minute=45),
        ]

        result = aggregate_bars(
            morning + afternoon, target_minutes=60, source_minutes=15
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].close, morning[-1].close)
        self.assertEqual(result[1].open, afternoon[0].open)

    def test_target_interval_must_be_a_source_multiple(self) -> None:
        with self.assertRaisesRegex(ValueError, "multiple"):
            validate_intervals(15, 20)


if __name__ == "__main__":
    unittest.main()
