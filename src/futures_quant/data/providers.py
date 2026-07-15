from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from futures_quant.data.history import Instrument, generate_synthetic_history
from futures_quant.models import Bar


class HistoricalDataProvider(ABC):
    @abstractmethod
    def fetch(self, instrument: Instrument, start: str, end: str) -> list[Bar]:
        raise NotImplementedError


class SyntheticHistoryProvider(HistoricalDataProvider):
    def fetch(self, instrument: Instrument, start: str, end: str) -> list[Bar]:
        return generate_synthetic_history(instrument, start, end)


class AkshareFuturesProvider(HistoricalDataProvider):
    """Fetch domestic futures daily bars through AKShare when it is installed."""

    def fetch(self, instrument: Instrument, start: str, end: str) -> list[Bar]:
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError("AKShare is not installed. Install akshare or use provider=synthetic/csv.") from exc

        df = ak.futures_zh_daily_sina(symbol=instrument.symbol)
        if df.empty:
            raise RuntimeError(f"AKShare returned no data for {instrument.symbol}.")
        df = df.rename(columns={"date": "datetime", "hold": "open_interest"})
        df["datetime"] = pd.to_datetime(df["datetime"])
        mask = (df["datetime"] >= pd.to_datetime(start)) & (df["datetime"] <= pd.to_datetime(end))
        df = df.loc[mask].sort_values("datetime")
        return _bars_from_dataframe(df, instrument.symbol)


class BinanceSpotProvider(HistoricalDataProvider):
    """Fetch Binance spot daily klines, useful for BTCUSDT-style crypto history."""

    def fetch(self, instrument: Instrument, start: str, end: str) -> list[Bar]:
        start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
        end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000)
        params = urlencode(
            {
                "symbol": instrument.symbol,
                "interval": "1d",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 1000,
            }
        )
        url = f"https://api.binance.com/api/v3/klines?{params}"
        req = Request(url, headers={"User-Agent": "futures-quant/0.1"})
        with urlopen(req, timeout=20) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        bars: list[Bar] = []
        for item in raw:
            dt = datetime.fromtimestamp(item[0] / 1000, tz=timezone.utc).replace(tzinfo=None)
            bars.append(
                Bar(
                    datetime=dt,
                    symbol=instrument.symbol,
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume=float(item[5]),
                    open_interest=0.0,
                )
            )
        if not bars:
            raise RuntimeError(f"Binance returned no data for {instrument.symbol}.")
        return bars


class HttpHistoryProvider(HistoricalDataProvider):
    """Fetch historical bars from a configurable HTTP CSV or JSON endpoint."""

    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path)
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))

    def fetch(self, instrument: Instrument, start: str, end: str) -> list[Bar]:
        url = self.config["url_template"].format(symbol=instrument.symbol, start=start, end=end)
        headers = self.config.get("headers", {})
        req = Request(url, headers=headers)
        with urlopen(req, timeout=float(self.config.get("timeout", 20))) as resp:
            payload = resp.read()

        fmt = self.config.get("format", "csv").lower()
        mapping = self.config.get("field_mapping", {})
        if fmt == "csv":
            from io import BytesIO

            df = pd.read_csv(BytesIO(payload), encoding=self.config.get("encoding", "utf-8"))
            return _bars_from_mapped_dataframe(df, instrument.symbol, mapping, start, end)
        if fmt == "json":
            raw = json.loads(payload.decode(self.config.get("encoding", "utf-8")))
            rows = _extract_json_rows(raw, self.config.get("data_path", []))
            df = pd.DataFrame(rows)
            return _bars_from_mapped_dataframe(df, instrument.symbol, mapping, start, end)
        raise ValueError(f"Unsupported HTTP history format: {fmt}")


def build_provider(name: str, config_path: str | Path | None = None) -> HistoricalDataProvider:
    normalized = name.lower()
    if normalized == "synthetic":
        return SyntheticHistoryProvider()
    if normalized == "akshare":
        return AkshareFuturesProvider()
    if normalized == "binance":
        return BinanceSpotProvider()
    if normalized == "http":
        if config_path is None:
            raise ValueError("provider_config is required for provider=http.")
        return HttpHistoryProvider(config_path)
    raise ValueError(f"Unsupported history provider: {name}")


def _bars_from_dataframe(df: pd.DataFrame, symbol: str) -> list[Bar]:
    required = {"datetime", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Historical dataframe missing columns: {sorted(missing)}")
    bars: list[Bar] = []
    for row in df.itertuples(index=False):
        bars.append(
            Bar(
                datetime=row.datetime.to_pydatetime(),
                symbol=symbol,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
                open_interest=float(getattr(row, "open_interest", 0.0)),
            )
        )
    return bars


def _bars_from_mapped_dataframe(
    df: pd.DataFrame,
    symbol: str,
    mapping: dict[str, str],
    start: str,
    end: str,
) -> list[Bar]:
    if mapping:
        rename = {source: target for target, source in mapping.items()}
        df = df.rename(columns=rename)
    if "datetime" not in df.columns:
        raise ValueError("Historical dataframe missing columns: ['datetime']")
    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"], errors="raise", utc=True).dt.tz_localize(None)
    start_at = pd.to_datetime(start, utc=True).tz_localize(None)
    end_at = pd.to_datetime(end, utc=True).tz_localize(None)
    if end_at == end_at.normalize():
        df = df.loc[(df["datetime"] >= start_at) & (df["datetime"] < end_at + pd.Timedelta(days=1))]
    else:
        df = df.loc[(df["datetime"] >= start_at) & (df["datetime"] <= end_at)]
    df = df.sort_values("datetime")
    if df.empty:
        raise RuntimeError(f"HTTP history provider returned no bars for {symbol} in {start}..{end}.")
    return _bars_from_dataframe(df, symbol)


def _extract_json_rows(raw: object, data_path: list[str]) -> list[dict[str, object]]:
    current = raw
    for key in data_path:
        if isinstance(current, dict):
            current = current[key]
        else:
            raise ValueError(f"Cannot traverse JSON data_path at {key!r}.")
    if not isinstance(current, list):
        raise ValueError("Configured JSON data_path must resolve to a list of rows.")
    if not all(isinstance(item, dict) for item in current):
        raise ValueError("JSON rows must be objects.")
    return current
