from __future__ import annotations

from pathlib import Path

import pandas as pd

from futures_quant.models import Bar


REQUIRED_COLUMNS = {"datetime", "symbol", "open", "high", "low", "close", "volume"}


def load_bars(path: str | Path) -> list[Bar]:
    df = pd.read_csv(path)
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"CSV missing required columns: {sorted(missing)}")

    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values(["datetime", "symbol"]).reset_index(drop=True)

    bars: list[Bar] = []
    for row in df.itertuples(index=False):
        bars.append(
            Bar(
                datetime=row.datetime.to_pydatetime(),
                symbol=str(row.symbol),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
                open_interest=float(getattr(row, "open_interest", 0.0)),
            )
        )
    return bars
