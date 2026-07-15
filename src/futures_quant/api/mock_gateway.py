from __future__ import annotations

from futures_quant.api.base import TradingGateway
from futures_quant.models import Bar, Order, Trade


class MockGateway(TradingGateway):
    """A local gateway for interface testing before real CTP or broker API wiring."""

    def __init__(self) -> None:
        self.connected = False
        self.subscriptions: set[str] = set()
        self._trades: list[Trade] = []
        self._bars: dict[str, Bar] = {}
        self._order_seq = 0

    def connect(self) -> None:
        self.connected = True

    def subscribe(self, symbol: str) -> None:
        self._ensure_connected()
        self.subscriptions.add(symbol)

    def push_bar(self, bar: Bar) -> None:
        self._bars[bar.symbol] = bar

    def send_order(self, order: Order) -> str:
        self._ensure_connected()
        self._order_seq += 1
        order_id = f"MOCK-{self._order_seq:06d}"
        self._trades.append(
            Trade(
                datetime=order.datetime,
                symbol=order.symbol,
                quantity=order.quantity,
                price=order.price,
                commission=0.0,
                reason=order.reason,
            )
        )
        return order_id

    def cancel_order(self, order_id: str) -> None:
        self._ensure_connected()

    def latest_bar(self, symbol: str) -> Bar | None:
        self._ensure_connected()
        return self._bars.get(symbol)

    def trades(self) -> list[Trade]:
        return list(self._trades)

    def _ensure_connected(self) -> None:
        if not self.connected:
            raise RuntimeError("Gateway is not connected.")
