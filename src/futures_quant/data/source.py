from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from futures_quant.api.base import TradingGateway
from futures_quant.data.csv_loader import load_bars
from futures_quant.models import Bar


class MarketDataSource(ABC):
    """A source that can provide normalized Bar objects to research or trading."""

    @abstractmethod
    def bars(self) -> Iterable[Bar]:
        raise NotImplementedError


class CsvBarSource(MarketDataSource):
    def __init__(self, path: str) -> None:
        self.path = path

    def bars(self) -> Iterable[Bar]:
        return load_bars(self.path)


class GatewaySnapshotSource(MarketDataSource):
    """Read the latest normalized bars from a TradingGateway.

    This is intentionally snapshot-oriented. A real streaming loop can poll this
    source or replace it with a queue-backed source while preserving the same
    Bar contract used by backtests.
    """

    def __init__(self, gateway: TradingGateway, symbols: list[str]) -> None:
        self.gateway = gateway
        self.symbols = symbols

    def bars(self) -> Iterable[Bar]:
        for symbol in self.symbols:
            bar = self.gateway.latest_bar(symbol)
            if bar is not None:
                yield bar
