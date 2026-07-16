from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from futures_quant.data.okx import download_okx_history, okx_candles_to_bars


def candle(timestamp_ms: int, close: float, confirm: str = "1") -> list[str]:
    return [
        str(timestamp_ms),
        str(close - 1),
        str(close + 1),
        str(close - 2),
        str(close),
        "100",
        "0",
        "0",
        confirm,
    ]


class FakePublicClient:
    def __init__(self, pages: list[list[object]]) -> None:
        self.pages = list(pages)
        self.calls: list[dict[str, object]] = []

    def get_candles(self, inst_id: str, **kwargs) -> list[object]:
        self.calls.append({"inst_id": inst_id, **kwargs})
        return self.pages.pop(0) if self.pages else []


class OkxHistoryTests(unittest.TestCase):
    def test_conversion_excludes_unconfirmed_deduplicates_and_sorts(self) -> None:
        rows = [
            candle(3000, 3),
            candle(1000, 1),
            candle(2000, 2, confirm="0"),
            candle(3000, 4),
        ]
        bars = okx_candles_to_bars(rows, "BTC-USDT-SWAP")
        self.assertEqual([bar.close for bar in bars], [1.0, 4.0])
        self.assertEqual([bar.symbol for bar in bars], ["BTC-USDT-SWAP"] * 2)

    def test_download_pages_backwards_and_writes_source_manifest(self) -> None:
        client = FakePublicClient(
            [
                [candle(3000, 3), candle(2000, 2)],
                [candle(1999, 1.999), candle(1000, 1)],
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "BTC-USDT-SWAP_15m.csv"
            result = download_okx_history(
                client,  # type: ignore[arg-type]
                inst_id="BTC-USDT-SWAP",
                bar="15m",
                output_path=output,
                start=datetime.fromtimestamp(1, tz=timezone.utc),
                request_delay_seconds=0,
            )
            frame = pd.read_csv(output)
            manifest = json.loads(
                output.with_suffix(".csv.source.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result.page_count, 2)
        self.assertEqual(result.bar_count, 4)
        self.assertEqual(frame["symbol"].unique().tolist(), ["BTC-USDT-SWAP"])
        self.assertEqual(client.calls[0]["after"], "")
        self.assertEqual(client.calls[1]["after"], "2000")
        self.assertEqual(manifest["data_kind"], "real_market")
        self.assertEqual(manifest["provider"], "okx_public_rest")


if __name__ == "__main__":
    unittest.main()
