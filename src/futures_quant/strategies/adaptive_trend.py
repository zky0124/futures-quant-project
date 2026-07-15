from __future__ import annotations

import math
from collections import defaultdict, deque
from statistics import fmean, pstdev

from futures_quant.models import Bar, Signal
from futures_quant.strategies.base import Strategy


class AdaptiveTrendStrategy(Strategy):
    """Donchian trend following with momentum confirmation and volatility sizing.

    The entry and exit channels, trend average, and realized volatility are all
    calculated from bars observed *before* the current bar.  The current close
    can therefore trigger a breakout, but the channel it is compared with never
    contains current or future information.

    When ``initial_cash`` is supplied, contract count is calculated from a
    portfolio volatility budget using price and ``contract_multiplier``.  When
    it is omitted, ``order_size`` is the desired number of contracts at the
    target volatility, which preserves compatibility with the existing fixed
    size configuration.
    """

    def __init__(
        self,
        entry_window: int = 20,
        exit_window: int = 10,
        trend_window: int = 60,
        momentum_window: int = 20,
        volatility_window: int = 20,
        target_annual_volatility: float = 0.15,
        order_size: int = 1,
        max_order_size: int | None = None,
        initial_cash: float | None = None,
        contract_multiplier: float = 1.0,
        max_notional_fraction: float = 0.10,
        momentum_threshold: float = 0.0,
        allow_short: bool = True,
        annualization_factor: int = 252,
    ) -> None:
        windows = {
            "entry_window": entry_window,
            "exit_window": exit_window,
            "trend_window": trend_window,
            "momentum_window": momentum_window,
            "volatility_window": volatility_window,
        }
        for name, value in windows.items():
            if value <= 1:
                raise ValueError(f"{name} must be greater than 1.")
        if exit_window > entry_window:
            raise ValueError("exit_window cannot be greater than entry_window.")
        if target_annual_volatility <= 0:
            raise ValueError("target_annual_volatility must be positive.")
        if order_size <= 0:
            raise ValueError("order_size must be positive.")
        if max_order_size is None:
            max_order_size = order_size * 4
        if max_order_size < order_size:
            raise ValueError("max_order_size cannot be smaller than order_size.")
        if initial_cash is not None and initial_cash <= 0:
            raise ValueError("initial_cash must be positive when supplied.")
        if contract_multiplier <= 0:
            raise ValueError("contract_multiplier must be positive.")
        if not 0 < max_notional_fraction <= 1:
            raise ValueError("max_notional_fraction must be in (0, 1].")
        if momentum_threshold < 0:
            raise ValueError("momentum_threshold cannot be negative.")
        if annualization_factor <= 0:
            raise ValueError("annualization_factor must be positive.")

        self.entry_window = entry_window
        self.exit_window = exit_window
        self.trend_window = trend_window
        self.momentum_window = momentum_window
        self.volatility_window = volatility_window
        self.target_annual_volatility = target_annual_volatility
        self.order_size = order_size
        self.max_order_size = max_order_size
        self.initial_cash = initial_cash
        self.contract_multiplier = contract_multiplier
        self.max_notional_fraction = max_notional_fraction
        self.momentum_threshold = momentum_threshold
        self.allow_short = allow_short
        self.annualization_factor = annualization_factor

        history_size = max(
            entry_window,
            exit_window,
            trend_window,
            momentum_window,
            volatility_window + 1,
        )
        self.highs: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=history_size))
        self.lows: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=history_size))
        self.closes: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=history_size))
        self.last_target: dict[str, int] = defaultdict(int)

    def on_bar(self, bar: Bar) -> Signal | None:
        highs = self.highs[bar.symbol]
        lows = self.lows[bar.symbol]
        closes = self.closes[bar.symbol]
        warmup = max(
            self.entry_window,
            self.exit_window,
            self.trend_window,
            self.momentum_window,
            self.volatility_window + 1,
        )

        if len(closes) < warmup:
            self._append_bar(bar)
            return None

        # All reference values deliberately exclude the current bar.
        entry_high = max(list(highs)[-self.entry_window :])
        entry_low = min(list(lows)[-self.entry_window :])
        exit_high = max(list(highs)[-self.exit_window :])
        exit_low = min(list(lows)[-self.exit_window :])
        trend_average = fmean(list(closes)[-self.trend_window :])
        momentum_base = list(closes)[-self.momentum_window]
        momentum = bar.close / momentum_base - 1.0 if momentum_base > 0 else 0.0
        size, realized_volatility = self._volatility_target_size(closes, bar.close)

        current_target = self.last_target[bar.symbol]
        current_direction = 1 if current_target > 0 else -1 if current_target < 0 else 0
        long_breakout = (
            bar.close > entry_high
            and bar.close > trend_average
            and momentum > self.momentum_threshold
        )
        short_breakout = (
            self.allow_short
            and bar.close < entry_low
            and bar.close < trend_average
            and momentum < -self.momentum_threshold
        )

        direction = current_direction
        if current_direction == 0:
            if long_breakout:
                direction = 1
            elif short_breakout:
                direction = -1
        elif current_direction > 0:
            if short_breakout:
                # Flatten first; this avoids a double-notional reversal order.
                direction = 0
            elif bar.close < exit_low or (bar.close < trend_average and momentum <= 0):
                direction = 0
        else:
            if long_breakout:
                # Flatten first; enter the opposite side on a later signal.
                direction = 0
            elif bar.close > exit_high or (bar.close > trend_average and momentum >= 0):
                direction = 0

        target = direction * size if direction else 0
        self._append_bar(bar)
        if target == current_target:
            return None

        self.last_target[bar.symbol] = target
        return Signal(
            datetime=bar.datetime,
            symbol=bar.symbol,
            target_position=target,
            reason=(
                "adaptive_trend "
                f"direction={direction} size={size} momentum={momentum:.4f} "
                f"realized_vol={realized_volatility:.4f} trend={trend_average:.4f}"
            ),
        )

    def _append_bar(self, bar: Bar) -> None:
        self.highs[bar.symbol].append(bar.high)
        self.lows[bar.symbol].append(bar.low)
        self.closes[bar.symbol].append(bar.close)

    def _volatility_target_size(self, closes: deque[float], current_price: float) -> tuple[int, float]:
        values = list(closes)[-(self.volatility_window + 1) :]
        returns = [
            values[idx] / values[idx - 1] - 1.0
            for idx in range(1, len(values))
            if values[idx - 1] > 0
        ]
        daily_volatility = pstdev(returns) if len(returns) > 1 else 0.0
        annualized_volatility = daily_volatility * math.sqrt(self.annualization_factor)
        maximum_size = self.max_order_size
        if self.initial_cash is not None:
            notional_per_contract = current_price * self.contract_multiplier
            notional_cap = (
                math.floor(
                    self.initial_cash
                    * self.max_notional_fraction
                    / notional_per_contract
                )
                if notional_per_contract > 0
                else 0
            )
            maximum_size = min(maximum_size, notional_cap)
            if maximum_size <= 0:
                return 0, annualized_volatility
        if annualized_volatility <= 1e-12:
            return maximum_size, annualized_volatility

        if self.initial_cash is not None:
            annual_risk_per_contract = (
                current_price * self.contract_multiplier * annualized_volatility
            )
            raw_size = (
                self.initial_cash * self.target_annual_volatility / annual_risk_per_contract
                if annual_risk_per_contract > 0
                else self.max_order_size
            )
        else:
            raw_size = self.order_size * self.target_annual_volatility / annualized_volatility
        size = max(1, min(maximum_size, int(round(raw_size))))
        return size, annualized_volatility
