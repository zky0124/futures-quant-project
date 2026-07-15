from __future__ import annotations

from collections import defaultdict, deque

from futures_quant.models import Bar, Signal
from futures_quant.strategies.base import Strategy


class DualMovingAverageStrategy(Strategy):
    def __init__(self, fast_window: int, slow_window: int, order_size: int) -> None:
        if fast_window <= 0 or slow_window <= 0:
            raise ValueError("Moving-average windows must be positive.")
        if fast_window >= slow_window:
            raise ValueError("fast_window must be smaller than slow_window.")
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.order_size = order_size
        self.closes: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=slow_window))
        self.last_target: dict[str, int] = defaultdict(int)

    def on_bar(self, bar: Bar) -> Signal | None:
        closes = self.closes[bar.symbol]
        closes.append(bar.close)
        if len(closes) < self.slow_window:
            return None

        fast = sum(list(closes)[-self.fast_window :]) / self.fast_window
        slow = sum(closes) / self.slow_window
        target = self.order_size if fast > slow else -self.order_size if fast < slow else 0
        if target == self.last_target[bar.symbol]:
            return None

        self.last_target[bar.symbol] = target
        return Signal(
            datetime=bar.datetime,
            symbol=bar.symbol,
            target_position=target,
            reason=f"dual_ma fast={fast:.2f} slow={slow:.2f}",
        )
