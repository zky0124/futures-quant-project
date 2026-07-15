from __future__ import annotations

from abc import ABC, abstractmethod

from futures_quant.models import Bar, Order, Trade


class TradingGateway(ABC):
    """Unified boundary for broker or trading-software API integrations."""

    @abstractmethod
    def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def subscribe(self, symbol: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def send_order(self, order: Order) -> str:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def latest_bar(self, symbol: str) -> Bar | None:
        raise NotImplementedError

    @abstractmethod
    def trades(self) -> list[Trade]:
        raise NotImplementedError
