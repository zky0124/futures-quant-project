from __future__ import annotations

import csv
from pathlib import Path

from futures_quant.models import Bar


BAR_COLUMNS = ["datetime", "symbol", "open", "high", "low", "close", "volume", "open_interest"]


class CsvBarRecorder:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def write(self, bars: list[Bar]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=BAR_COLUMNS)
            writer.writeheader()
            for bar in bars:
                writer.writerow(
                    {
                        "datetime": bar.datetime.isoformat(sep=" "),
                        "symbol": bar.symbol,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                        "open_interest": bar.open_interest,
                    }
                )
