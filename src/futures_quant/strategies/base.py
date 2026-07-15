from __future__ import annotations

from abc import ABC, abstractmethod

from futures_quant.models import Bar, Signal


class Strategy(ABC):
    @abstractmethod
    def on_bar(self, bar: Bar) -> Signal | None:
        raise NotImplementedError

    def on_position_update(
        self, symbol: str, quantity: int, avg_price: float
    ) -> None:
        """Synchronize strategy state after a broker fill or position check."""

    def on_account_update(self, equity: float) -> None:
        """Provide current marked account equity before evaluating a bar."""

    def on_order_rejected(self, signal: Signal, status: str) -> None:
        """Notify the strategy that a submitted signal was rejected."""
