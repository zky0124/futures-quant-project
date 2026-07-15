from __future__ import annotations

import math
from collections import defaultdict, deque
from statistics import fmean, pstdev

from futures_quant.models import Bar, Signal
from futures_quant.strategies.base import Strategy


class EnhancedAdaptiveTrendStrategy(Strategy):
    """Donchian trend strategy with bounded volatility and stop-risk sizing."""

    def __init__(
        self,
        entry_window: int = 55,
        exit_window: int = 20,
        trend_window: int = 120,
        momentum_window: int = 60,
        volatility_window: int = 30,
        target_annual_volatility: float = 0.12,
        order_size: int = 1,
        max_order_size: int = 5,
        initial_cash: float = 1_000_000,
        contract_multiplier: float = 1.0,
        margin_rate: float = 0.10,
        max_notional_fraction: float = 0.10,
        max_margin_fraction: float = 0.20,
        max_trade_risk: float = 0.005,
        momentum_threshold: float = 0.0,
        allow_short: bool = True,
        annualization_factor: int = 4032,
        atr_window: int = 20,
        atr_stop_multiple: float = 2.5,
        break_even_trigger_r: float = 1.0,
        reward_risk: float = 2.0,
        partial_exit_fraction: float = 0.40,
        trailing_atr_multiple: float = 3.0,
        cooldown_bars: int = 8,
        loss_pause_after: int = 3,
        loss_pause_bars: int = 32,
    ) -> None:
        windows = {
            "entry_window": entry_window,
            "exit_window": exit_window,
            "trend_window": trend_window,
            "momentum_window": momentum_window,
            "volatility_window": volatility_window,
            "atr_window": atr_window,
        }
        for name, value in windows.items():
            if value <= 1:
                raise ValueError(f"{name} must be greater than 1.")
        if exit_window > entry_window:
            raise ValueError("exit_window cannot be greater than entry_window.")
        if order_size <= 0 or max_order_size < order_size:
            raise ValueError("Order sizes must satisfy 0 < order_size <= max_order_size.")
        if initial_cash <= 0 or contract_multiplier <= 0:
            raise ValueError("initial_cash and contract_multiplier must be positive.")
        if not 0 < margin_rate < 1:
            raise ValueError("margin_rate must be in (0, 1).")
        for name, value in {
            "target_annual_volatility": target_annual_volatility,
            "atr_stop_multiple": atr_stop_multiple,
            "break_even_trigger_r": break_even_trigger_r,
            "reward_risk": reward_risk,
            "trailing_atr_multiple": trailing_atr_multiple,
        }.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive.")
        for name, value in {
            "max_notional_fraction": max_notional_fraction,
            "max_margin_fraction": max_margin_fraction,
            "max_trade_risk": max_trade_risk,
            "partial_exit_fraction": partial_exit_fraction,
        }.items():
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1].")
        if momentum_threshold < 0 or annualization_factor <= 0:
            raise ValueError("Momentum threshold and annualization factor are invalid.")
        if cooldown_bars < 0 or loss_pause_after <= 0 or loss_pause_bars < 0:
            raise ValueError("Cooldown and loss-pause parameters are invalid.")

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
        self.margin_rate = margin_rate
        self.max_notional_fraction = max_notional_fraction
        self.max_margin_fraction = max_margin_fraction
        self.max_trade_risk = max_trade_risk
        self.momentum_threshold = momentum_threshold
        self.allow_short = allow_short
        self.annualization_factor = annualization_factor
        self.atr_window = atr_window
        self.atr_stop_multiple = atr_stop_multiple
        self.break_even_trigger_r = break_even_trigger_r
        self.reward_risk = reward_risk
        self.partial_exit_fraction = partial_exit_fraction
        self.trailing_atr_multiple = trailing_atr_multiple
        self.cooldown_bars = cooldown_bars
        self.loss_pause_after = loss_pause_after
        self.loss_pause_bars = loss_pause_bars

        history_size = max(
            entry_window,
            exit_window,
            trend_window,
            momentum_window,
            volatility_window + 1,
            atr_window,
        )
        self.highs: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=history_size)
        )
        self.lows: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=history_size)
        )
        self.closes: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=history_size)
        )
        self.true_ranges: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=history_size)
        )
        self.bar_number: dict[str, int] = defaultdict(int)
        self.last_target: dict[str, int] = defaultdict(int)
        self.initial_size: dict[str, int] = {}
        self.entry_price: dict[str, float] = {}
        self.initial_risk: dict[str, float] = {}
        self.stop_price: dict[str, float] = {}
        self.first_target: dict[str, float] = {}
        self.partial_taken: dict[str, bool] = defaultdict(bool)
        self.pending_entries: set[str] = set()
        self.pending_exits: dict[str, bool] = {}
        self.blocked_until: dict[str, int] = defaultdict(int)
        self.consecutive_stops: dict[str, int] = defaultdict(int)

    def on_bar(self, bar: Bar) -> Signal | None:
        symbol = bar.symbol
        self.bar_number[symbol] += 1
        closes = self.closes[symbol]
        previous_close = closes[-1] if closes else bar.close
        true_range = max(
            bar.high - bar.low,
            abs(bar.high - previous_close),
            abs(bar.low - previous_close),
        )
        warmup = max(
            self.entry_window,
            self.exit_window,
            self.trend_window,
            self.momentum_window,
            self.volatility_window + 1,
            self.atr_window,
        )
        if len(closes) < warmup or len(self.true_ranges[symbol]) < self.atr_window:
            self._append_bar(bar, true_range)
            return None

        highs = list(self.highs[symbol])
        lows = list(self.lows[symbol])
        values = list(closes)
        entry_high = max(highs[-self.entry_window :])
        entry_low = min(lows[-self.entry_window :])
        exit_high = max(highs[-self.exit_window :])
        exit_low = min(lows[-self.exit_window :])
        trend_average = fmean(values[-self.trend_window :])
        momentum_base = values[-self.momentum_window]
        momentum = bar.close / momentum_base - 1.0 if momentum_base > 0 else 0.0
        atr = fmean(list(self.true_ranges[symbol])[-self.atr_window :])

        signal: Signal | None = None
        current_target = self.last_target[symbol]
        if current_target > 0:
            signal = self._manage_long(
                bar, exit_low, trend_average, momentum, atr
            )
        elif current_target < 0:
            signal = self._manage_short(
                bar, exit_high, trend_average, momentum, atr
            )
        elif self.bar_number[symbol] >= self.blocked_until[symbol]:
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
            if long_breakout or short_breakout:
                direction = 1 if long_breakout else -1
                stop = bar.close - direction * self.atr_stop_multiple * atr
                risk = abs(bar.close - stop)
                size, realized_volatility = self._position_size(
                    values, bar.close, risk
                )
                if size > 0 and risk > 0:
                    target = direction * size
                    self._open_state(symbol, target, bar.close, stop, risk)
                    signal = Signal(
                        datetime=bar.datetime,
                        symbol=symbol,
                        target_position=target,
                        reason=(
                            "adaptive_trend_v2 "
                            f"direction={direction} size={size} momentum={momentum:.6f} "
                            f"realized_vol={realized_volatility:.6f} atr={atr:.6f} "
                            f"stop={stop:.6f}"
                        ),
                        stop_price=stop,
                    )

        self._append_bar(bar, true_range)
        return signal

    def _manage_long(
        self,
        bar: Bar,
        exit_low: float,
        trend_average: float,
        momentum: float,
        atr: float,
    ) -> Signal | None:
        symbol = bar.symbol
        stop = self.stop_price[symbol]
        if bar.low <= stop:
            execution = bar.open if bar.open <= stop else stop
            losing_stop = execution < self.entry_price[symbol]
            return self._full_exit(
                bar, execution, "protective_stop_long", stopped=losing_stop
            )
        partial = self._partial_signal(bar, direction=1)
        if partial is not None:
            return partial
        break_even = (
            self.entry_price[symbol]
            + self.break_even_trigger_r * self.initial_risk[symbol]
        )
        if bar.high >= break_even:
            self.stop_price[symbol] = max(stop, self.entry_price[symbol])
        if self.partial_taken[symbol]:
            self.stop_price[symbol] = max(
                self.stop_price[symbol], bar.close - self.trailing_atr_multiple * atr
            )
        if bar.close < exit_low or (bar.close < trend_average and momentum <= 0):
            return self._close_signal(bar, "channel_or_trend_exit_long")
        return None

    def _manage_short(
        self,
        bar: Bar,
        exit_high: float,
        trend_average: float,
        momentum: float,
        atr: float,
    ) -> Signal | None:
        symbol = bar.symbol
        stop = self.stop_price[symbol]
        if bar.high >= stop:
            execution = bar.open if bar.open >= stop else stop
            losing_stop = execution > self.entry_price[symbol]
            return self._full_exit(
                bar, execution, "protective_stop_short", stopped=losing_stop
            )
        partial = self._partial_signal(bar, direction=-1)
        if partial is not None:
            return partial
        break_even = (
            self.entry_price[symbol]
            - self.break_even_trigger_r * self.initial_risk[symbol]
        )
        if bar.low <= break_even:
            self.stop_price[symbol] = min(stop, self.entry_price[symbol])
        if self.partial_taken[symbol]:
            self.stop_price[symbol] = min(
                self.stop_price[symbol], bar.close + self.trailing_atr_multiple * atr
            )
        if bar.close > exit_high or (bar.close > trend_average and momentum >= 0):
            return self._close_signal(bar, "channel_or_trend_exit_short")
        return None

    def _partial_signal(self, bar: Bar, direction: int) -> Signal | None:
        symbol = bar.symbol
        if self.partial_taken[symbol]:
            return None
        target = self.first_target[symbol]
        reached = bar.high >= target if direction > 0 else bar.low <= target
        if not reached:
            return None
        initial_size = self.initial_size[symbol]
        if initial_size <= 1:
            self.partial_taken[symbol] = True
            self.stop_price[symbol] = self.entry_price[symbol]
            self.consecutive_stops[symbol] = 0
            return None
        reduction = max(1, int(round(initial_size * self.partial_exit_fraction)))
        reduction = min(reduction, initial_size - 1)
        remaining = direction * (initial_size - reduction)
        execution = (
            bar.open
            if direction > 0 and bar.open >= target
            or direction < 0 and bar.open <= target
            else target
        )
        return Signal(
            datetime=bar.datetime,
            symbol=symbol,
            target_position=remaining,
            reason=f"adaptive_trend_v2 partial_2R_{'long' if direction > 0 else 'short'}",
            execution_price=execution,
            immediate=True,
        )

    def _position_size(
        self, closes: list[float], current_price: float, stop_distance: float
    ) -> tuple[int, float]:
        values = closes[-(self.volatility_window + 1) :]
        returns = [
            values[index] / values[index - 1] - 1.0
            for index in range(1, len(values))
            if values[index - 1] > 0
        ]
        bar_volatility = pstdev(returns) if len(returns) > 1 else 0.0
        annualized_volatility = bar_volatility * math.sqrt(self.annualization_factor)
        notional_per_contract = current_price * self.contract_multiplier
        margin_per_contract = notional_per_contract * self.margin_rate
        stop_risk_per_contract = stop_distance * self.contract_multiplier
        caps = [self.max_order_size]
        if notional_per_contract > 0:
            caps.append(
                math.floor(
                    self.initial_cash
                    * self.max_notional_fraction
                    / notional_per_contract
                )
            )
        if margin_per_contract > 0:
            caps.append(
                math.floor(
                    self.initial_cash * self.max_margin_fraction / margin_per_contract
                )
            )
        if stop_risk_per_contract > 0:
            caps.append(
                math.floor(
                    self.initial_cash * self.max_trade_risk / stop_risk_per_contract
                )
            )
        maximum = min(caps)
        if maximum < 1:
            return 0, annualized_volatility
        if annualized_volatility <= 1e-12:
            return maximum, annualized_volatility
        annual_risk_per_contract = notional_per_contract * annualized_volatility
        volatility_size = math.floor(
            self.initial_cash * self.target_annual_volatility / annual_risk_per_contract
        ) if annual_risk_per_contract > 0 else maximum
        return max(0, min(maximum, volatility_size)), annualized_volatility

    def _append_bar(self, bar: Bar, true_range: float) -> None:
        self.highs[bar.symbol].append(bar.high)
        self.lows[bar.symbol].append(bar.low)
        self.closes[bar.symbol].append(bar.close)
        self.true_ranges[bar.symbol].append(true_range)

    def _open_state(
        self, symbol: str, target: int, entry: float, stop: float, risk: float
    ) -> None:
        self.last_target[symbol] = target
        self.initial_size[symbol] = abs(target)
        self.entry_price[symbol] = entry
        self.initial_risk[symbol] = risk
        self.stop_price[symbol] = stop
        self.first_target[symbol] = entry + (1 if target > 0 else -1) * self.reward_risk * risk
        self.partial_taken[symbol] = False
        self.pending_entries.add(symbol)

    def _full_exit(
        self, bar: Bar, execution_price: float, reason: str, *, stopped: bool
    ) -> Signal:
        self.pending_exits[bar.symbol] = stopped
        return Signal(
            datetime=bar.datetime,
            symbol=bar.symbol,
            target_position=0,
            reason=f"adaptive_trend_v2 {reason}",
            execution_price=execution_price,
            immediate=True,
        )

    def _close_signal(self, bar: Bar, reason: str) -> Signal:
        self.pending_exits[bar.symbol] = False
        return Signal(
            datetime=bar.datetime,
            symbol=bar.symbol,
            target_position=0,
            reason=f"adaptive_trend_v2 {reason}",
        )

    def on_position_update(
        self, symbol: str, quantity: int, avg_price: float
    ) -> None:
        if quantity == 0:
            if symbol in self.pending_exits:
                stopped = self.pending_exits.pop(symbol)
                if stopped:
                    self.consecutive_stops[symbol] += 1
                else:
                    self.consecutive_stops[symbol] = 0
                pause = self.cooldown_bars
                if self.consecutive_stops[symbol] >= self.loss_pause_after:
                    pause = max(pause, self.loss_pause_bars)
                    self.consecutive_stops[symbol] = 0
                self.blocked_until[symbol] = self.bar_number[symbol] + pause
            self._clear_position_state(symbol)
            return

        self.last_target[symbol] = quantity
        if symbol in self.pending_entries:
            self.pending_entries.discard(symbol)
            stop = self.stop_price[symbol]
            risk = abs(avg_price - stop)
            if risk <= 0:
                risk = self.initial_risk[symbol]
            self.initial_size[symbol] = abs(quantity)
            self.entry_price[symbol] = avg_price
            self.initial_risk[symbol] = risk
            self.first_target[symbol] = (
                avg_price + (1 if quantity > 0 else -1) * self.reward_risk * risk
            )
        if abs(quantity) < self.initial_size.get(symbol, abs(quantity)):
            self.partial_taken[symbol] = True
            self.consecutive_stops[symbol] = 0
            if quantity > 0:
                self.stop_price[symbol] = max(
                    self.stop_price[symbol], self.entry_price[symbol]
                )
            else:
                self.stop_price[symbol] = min(
                    self.stop_price[symbol], self.entry_price[symbol]
                )

    def on_order_rejected(self, signal: Signal, status: str) -> None:
        symbol = signal.symbol
        self.pending_exits.pop(symbol, None)
        if symbol in self.pending_entries:
            self.blocked_until[symbol] = self.bar_number[symbol] + self.cooldown_bars
            self._clear_position_state(symbol)

    def _clear_position_state(self, symbol: str) -> None:
        self.last_target[symbol] = 0
        self.initial_size.pop(symbol, None)
        self.entry_price.pop(symbol, None)
        self.initial_risk.pop(symbol, None)
        self.stop_price.pop(symbol, None)
        self.first_target.pop(symbol, None)
        self.partial_taken.pop(symbol, None)
        self.pending_entries.discard(symbol)
        self.pending_exits.pop(symbol, None)
