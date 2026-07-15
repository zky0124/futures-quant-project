from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=True)
class Bar:
    datetime: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    open_interest: float = 0.0


@dataclass(frozen=True)
class Signal:
    datetime: datetime
    symbol: str
    target_position: int
    reason: str
    execution_price: float | None = None
    immediate: bool = False
    stop_price: float | None = None


@dataclass(frozen=True)
class Order:
    datetime: datetime
    symbol: str
    quantity: int
    price: float
    reason: str
    stop_price: float | None = None


@dataclass(frozen=True)
class Trade:
    datetime: datetime
    symbol: str
    quantity: int
    price: float
    commission: float
    reason: str
    closed_quantity: int = 0
    realized_pnl: float = 0.0


@dataclass
class Position:
    symbol: str
    quantity: int = 0
    avg_price: float = 0.0

    def update(self, trade: Trade) -> float:
        old_qty = self.quantity
        new_qty = old_qty + trade.quantity
        realized = 0.0

        if old_qty == 0 or (old_qty > 0 and trade.quantity > 0) or (old_qty < 0 and trade.quantity < 0):
            total_cost = self.avg_price * abs(old_qty) + trade.price * abs(trade.quantity)
            self.quantity = new_qty
            self.avg_price = total_cost / abs(new_qty) if new_qty else 0.0
            return realized

        closing_qty = min(abs(old_qty), abs(trade.quantity))
        if old_qty > 0:
            realized = (trade.price - self.avg_price) * closing_qty
        else:
            realized = (self.avg_price - trade.price) * closing_qty

        self.quantity = new_qty
        if new_qty == 0:
            self.avg_price = 0.0
        elif (old_qty > 0 and new_qty > 0) or (old_qty < 0 and new_qty < 0):
            self.avg_price = self.avg_price
        else:
            self.avg_price = trade.price
        return realized
