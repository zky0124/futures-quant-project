from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

import pandas as pd

from futures_quant.data.recorder import CsvBarRecorder
from futures_quant.models import Bar


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    group: str
    base_price: float
    drift: float
    volatility: float
    seed: int


@dataclass(frozen=True)
class Universe:
    start: str
    end: str
    instruments: list[Instrument]


def load_universe(path: str | Path) -> Universe:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Universe(
        start=raw["start"],
        end=raw["end"],
        instruments=[Instrument(**item) for item in raw["instruments"]],
    )


def generate_synthetic_history(instrument: Instrument, start: str, end: str) -> list[Bar]:
    rng = random.Random(instrument.seed)
    dates = pd.bdate_range(start=start, end=end)
    price = instrument.base_price
    bars: list[Bar] = []
    for idx, date in enumerate(dates):
        seasonal = 0.003 * math.sin(idx / 18.0)
        shock = rng.gauss(instrument.drift + seasonal, instrument.volatility)
        close = max(price * (1 + shock), 0.01)
        open_price = price * (1 + rng.gauss(0, instrument.volatility / 4))
        high = max(open_price, close) * (1 + abs(rng.gauss(0, instrument.volatility / 3)))
        low = min(open_price, close) * (1 - abs(rng.gauss(0, instrument.volatility / 3)))
        volume = int(50000 + abs(rng.gauss(0, 1)) * 120000)
        open_interest = int(200000 + idx * 120 + abs(rng.gauss(0, 1)) * 5000)
        bars.append(
            Bar(
                datetime=date.to_pydatetime(),
                symbol=instrument.symbol,
                open=round(open_price, 4),
                high=round(high, 4),
                low=round(max(low, 0.01), 4),
                close=round(close, 4),
                volume=volume,
                open_interest=open_interest,
            )
        )
        price = close
    return bars


def write_demo_history(universe_path: str | Path, output_dir: str | Path) -> list[Path]:
    universe = load_universe(universe_path)
    output_dir = Path(output_dir)
    written: list[Path] = []
    for instrument in universe.instruments:
        bars = generate_synthetic_history(instrument, universe.start, universe.end)
        path = output_dir / f"{instrument.symbol}_1d_demo.csv"
        CsvBarRecorder(path).write(bars)
        written.append(path)
    return written


def generate_synthetic_intraday_history(
    instrument: Instrument, start: str, end: str
) -> list[Bar]:
    """Expand deterministic demo daily bars into a daytime 15-minute path.

    This exists only for end-to-end engineering tests of multi-timeframe code.
    It is deliberately labeled synthetic and must not be used to claim market
    performance.
    """
    daily = generate_synthetic_history(instrument, start, end)
    rng = random.Random(instrument.seed + 100_000)
    endpoints = [
        time(9, 15), time(9, 30), time(9, 45), time(10, 0), time(10, 15),
        time(10, 30), time(10, 45), time(11, 0), time(11, 15), time(11, 30),
        time(13, 45), time(14, 0), time(14, 15), time(14, 30), time(14, 45), time(15, 0),
    ]
    bars: list[Bar] = []
    for day_number, daily_bar in enumerate(daily):
        previous = daily_bar.open
        for index, endpoint in enumerate(endpoints):
            fraction = (index + 1) / len(endpoints)
            bridge = daily_bar.open + (daily_bar.close - daily_bar.open) * fraction
            if index + 1 == len(endpoints):
                close = daily_bar.close
            else:
                noise_scale = max(daily_bar.open * instrument.volatility / 8, 0.0001)
                close = max(bridge + rng.gauss(0, noise_scale), 0.01)
            spread = abs(rng.gauss(0, max(close * instrument.volatility / 12, 0.0001)))
            high = max(previous, close) + spread
            low = max(min(previous, close) - spread, 0.01)
            timestamp = datetime.combine(daily_bar.datetime.date(), endpoint)
            bars.append(
                Bar(
                    datetime=timestamp,
                    symbol=instrument.symbol,
                    open=round(previous, 4),
                    high=round(high, 4),
                    low=round(low, 4),
                    close=round(close, 4),
                    volume=max(1, int(daily_bar.volume / len(endpoints) * rng.uniform(0.6, 1.4))),
                    open_interest=daily_bar.open_interest + day_number,
                )
            )
            previous = close
    return bars


def write_demo_intraday_history(
    universe_path: str | Path, output_dir: str | Path
) -> list[Path]:
    universe = load_universe(universe_path)
    output_dir = Path(output_dir)
    written: list[Path] = []
    for instrument in universe.instruments:
        bars = generate_synthetic_intraday_history(instrument, universe.start, universe.end)
        path = output_dir / f"{instrument.symbol}_15m.csv"
        CsvBarRecorder(path).write(bars)
        written.append(path)
    return written
