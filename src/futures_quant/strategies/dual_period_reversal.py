from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import fmean

from futures_quant.models import Bar, Signal
from futures_quant.strategies.base import Strategy


@dataclass
class _DailyBuilder:
    trading_day: date
    open: float
    high: float
    low: float
    close: float


class DualPeriodReversalStrategy(Strategy):
    """Daily reversal regime plus 15-minute divergence/crossover entry.

    Input bars are expected to be chronological 15-minute bars.  Completed
    daily bars are built internally and become visible only on the first bar of
    the next trading day, avoiding an end-of-day look-ahead.  The strategy is a
    state machine: daily extreme/reclaim -> intraday divergence -> MA cross ->
    bracket/trailing/regime exit.
    """

    def __init__(
        self,
        fast_window: int = 13,
        slow_window: int = 45,
        order_size: int = 5,
        daily_fast_window: int = 13,
        daily_slow_window: int = 45,
        extreme_lookback_days: int = 120,
        extreme_move_threshold: float = 0.20,
        setup_valid_days: int = 10,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        divergence_lookback: int = 80,
        divergence_pivot_radius: int = 2,
        divergence_valid_bars: int = 32,
        second_cross_window: int = 48,
        atr_window: int = 14,
        atr_stop_buffer: float = 0.20,
        reward_risk: float = 2.0,
        trailing_atr_multiple: float = 2.5,
        allow_short: bool = True,
    ) -> None:
        if min(fast_window, slow_window, daily_fast_window, daily_slow_window) <= 0:
            raise ValueError("Moving-average windows must be positive.")
        if fast_window >= slow_window or daily_fast_window >= daily_slow_window:
            raise ValueError("Fast windows must be smaller than slow windows.")
        if macd_fast <= 0 or macd_slow <= macd_fast or macd_signal <= 0:
            raise ValueError("MACD windows must satisfy 0 < fast < slow and signal > 0.")
        if order_size <= 0:
            raise ValueError("order_size must be positive.")
        if not 0 < extreme_move_threshold < 1:
            raise ValueError("extreme_move_threshold must be in (0, 1).")
        if min(extreme_lookback_days, setup_valid_days, divergence_lookback) <= 0:
            raise ValueError("Lookback and setup windows must be positive.")
        if divergence_pivot_radius <= 0 or divergence_valid_bars <= 0:
            raise ValueError("Divergence windows must be positive.")
        if atr_window <= 1 or atr_stop_buffer < 0:
            raise ValueError("ATR window must exceed 1 and stop buffer cannot be negative.")
        if reward_risk <= 0 or trailing_atr_multiple <= 0:
            raise ValueError("Exit multiples must be positive.")

        self.fast_window = fast_window
        self.slow_window = slow_window
        self.order_size = order_size
        self.daily_fast_window = daily_fast_window
        self.daily_slow_window = daily_slow_window
        self.extreme_lookback_days = extreme_lookback_days
        self.extreme_move_threshold = extreme_move_threshold
        self.setup_valid_days = setup_valid_days
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.divergence_lookback = divergence_lookback
        self.divergence_pivot_radius = divergence_pivot_radius
        self.divergence_valid_bars = divergence_valid_bars
        self.second_cross_window = second_cross_window
        self.atr_window = atr_window
        self.atr_stop_buffer = atr_stop_buffer
        self.reward_risk = reward_risk
        self.trailing_atr_multiple = trailing_atr_multiple
        self.allow_short = allow_short

        history = max(slow_window + 2, divergence_lookback, atr_window + 2, macd_slow * 4)
        daily_history = max(extreme_lookback_days + 2, daily_slow_window + 2)
        self.closes: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=history))
        self.highs: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=history))
        self.lows: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=history))
        self.macd_raws: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=history))
        self.macd_lines: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=history))
        self.atr_values: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=atr_window))
        self.daily_closes: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=daily_history)
        )
        self.daily_highs: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=daily_history)
        )
        self.daily_lows: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=daily_history)
        )
        self.daily_builder: dict[str, _DailyBuilder] = {}
        self.bar_number: dict[str, int] = defaultdict(int)
        self.setup_direction: dict[str, int] = defaultdict(int)
        self.setup_days_left: dict[str, int] = defaultdict(int)
        self.long_extreme_days_left: dict[str, int] = defaultdict(int)
        self.short_extreme_days_left: dict[str, int] = defaultdict(int)
        self.daily_fast: dict[str, float] = {}
        self.daily_slow: dict[str, float] = {}
        self.daily_exit: dict[str, int] = defaultdict(int)
        self.last_bull_divergence: dict[str, int] = defaultdict(lambda: -10**9)
        self.last_bear_divergence: dict[str, int] = defaultdict(lambda: -10**9)
        self.bull_crosses: dict[str, deque[int]] = defaultdict(lambda: deque(maxlen=4))
        self.bear_crosses: dict[str, deque[int]] = defaultdict(lambda: deque(maxlen=4))
        self.last_target: dict[str, int] = defaultdict(int)
        self.stop_price: dict[str, float] = {}
        self.take_profit: dict[str, float] = {}

    def on_bar(self, bar: Bar) -> Signal | None:
        symbol = bar.symbol
        self.bar_number[symbol] += 1
        self._roll_daily(bar)

        closes = self.closes[symbol]
        previous_close = closes[-1] if closes else bar.close
        true_range = max(
            bar.high - bar.low,
            abs(bar.high - previous_close),
            abs(bar.low - previous_close),
        )
        self.atr_values[symbol].append(true_range)
        closes.append(bar.close)
        self.highs[symbol].append(bar.high)
        self.lows[symbol].append(bar.low)

        macd_line = self._ema(list(closes), self.macd_fast) - self._ema(
            list(closes), self.macd_slow
        )
        previous_macd = list(self.macd_raws[symbol])
        signal_line = self._ema(previous_macd + [macd_line], self.macd_signal)
        self.macd_raws[symbol].append(macd_line)
        self.macd_lines[symbol].append(macd_line - signal_line)
        self._update_divergence(symbol)

        warmup = max(self.slow_window + 1, self.macd_slow + self.macd_signal)
        if len(closes) < warmup or len(self.atr_values[symbol]) < self.atr_window:
            return None

        close_values = list(closes)
        previous_fast = fmean(close_values[-self.fast_window - 1 : -1])
        previous_slow = fmean(close_values[-self.slow_window - 1 : -1])
        fast = fmean(close_values[-self.fast_window :])
        slow = fmean(close_values[-self.slow_window :])
        bullish_cross = previous_fast <= previous_slow and fast > slow
        bearish_cross = previous_fast >= previous_slow and fast < slow
        index = self.bar_number[symbol]
        cross_label = ""
        if bullish_cross:
            recent = bool(
                self.bull_crosses[symbol]
                and index - self.bull_crosses[symbol][-1] <= self.second_cross_window
            )
            self.bull_crosses[symbol].append(index)
            cross_label = "second_golden_cross" if recent else "golden_cross"
        if bearish_cross:
            recent = bool(
                self.bear_crosses[symbol]
                and index - self.bear_crosses[symbol][-1] <= self.second_cross_window
            )
            self.bear_crosses[symbol].append(index)
            cross_label = "second_death_cross" if recent else "death_cross"

        target = self.last_target[symbol]
        atr = fmean(self.atr_values[symbol])
        if target > 0:
            self.stop_price[symbol] = max(
                self.stop_price[symbol], bar.close - self.trailing_atr_multiple * atr
            )
            daily_target_hit = (
                symbol in self.daily_slow and bar.close >= self.daily_slow[symbol]
            )
            if bar.low <= self.stop_price[symbol]:
                return self._flatten(bar, "protective_stop_long")
            if bar.high >= self.take_profit[symbol] or daily_target_hit:
                return self._flatten(bar, "profit_target_long")
            if self.daily_exit[symbol] < 0:
                self.daily_exit[symbol] = 0
                return self._flatten(bar, "daily_fast_ma_failure_long")
            return None
        if target < 0:
            self.stop_price[symbol] = min(
                self.stop_price[symbol], bar.close + self.trailing_atr_multiple * atr
            )
            daily_target_hit = (
                symbol in self.daily_slow and bar.close <= self.daily_slow[symbol]
            )
            if bar.high >= self.stop_price[symbol]:
                return self._flatten(bar, "protective_stop_short")
            if bar.low <= self.take_profit[symbol] or daily_target_hit:
                return self._flatten(bar, "profit_target_short")
            if self.daily_exit[symbol] > 0:
                self.daily_exit[symbol] = 0
                return self._flatten(bar, "daily_fast_ma_failure_short")
            return None

        long_divergence = index - self.last_bull_divergence[symbol] <= self.divergence_valid_bars
        short_divergence = index - self.last_bear_divergence[symbol] <= self.divergence_valid_bars
        if self.setup_direction[symbol] > 0 and bullish_cross and long_divergence:
            stop = bar.low - atr * self.atr_stop_buffer
            risk = max(bar.close - stop, atr * 0.25)
            self.stop_price[symbol] = stop
            self.take_profit[symbol] = bar.close + self.reward_risk * risk
            self.last_target[symbol] = self.order_size
            return Signal(
                bar.datetime,
                symbol,
                self.order_size,
                (
                    f"dual_period_reversal long {cross_label} macd_bull_divergence "
                    f"stop={stop:.6f} take_profit={self.take_profit[symbol]:.6f}"
                ),
            )
        if (
            self.allow_short
            and self.setup_direction[symbol] < 0
            and bearish_cross
            and short_divergence
        ):
            stop = bar.high + atr * self.atr_stop_buffer
            risk = max(stop - bar.close, atr * 0.25)
            self.stop_price[symbol] = stop
            self.take_profit[symbol] = bar.close - self.reward_risk * risk
            self.last_target[symbol] = -self.order_size
            return Signal(
                bar.datetime,
                symbol,
                -self.order_size,
                (
                    f"dual_period_reversal short {cross_label} macd_bear_divergence "
                    f"stop={stop:.6f} take_profit={self.take_profit[symbol]:.6f}"
                ),
            )
        return None

    def _flatten(self, bar: Bar, reason: str) -> Signal:
        self.last_target[bar.symbol] = 0
        self.stop_price.pop(bar.symbol, None)
        self.take_profit.pop(bar.symbol, None)
        return Signal(bar.datetime, bar.symbol, 0, f"dual_period_reversal {reason}")

    def _roll_daily(self, bar: Bar) -> None:
        symbol = bar.symbol
        trading_day = self._trading_day(bar)
        builder = self.daily_builder.get(symbol)
        if builder is None:
            self.daily_builder[symbol] = _DailyBuilder(
                trading_day, bar.open, bar.high, bar.low, bar.close
            )
            return
        if builder.trading_day == trading_day:
            builder.high = max(builder.high, bar.high)
            builder.low = min(builder.low, bar.low)
            builder.close = bar.close
            return

        self._finalize_daily(symbol, builder)
        self.daily_builder[symbol] = _DailyBuilder(
            trading_day, bar.open, bar.high, bar.low, bar.close
        )

    def _finalize_daily(self, symbol: str, builder: _DailyBuilder) -> None:
        closes = self.daily_closes[symbol]
        highs = self.daily_highs[symbol]
        lows = self.daily_lows[symbol]
        previous_fast = (
            fmean(list(closes)[-self.daily_fast_window :])
            if len(closes) >= self.daily_fast_window
            else None
        )
        previous_close = closes[-1] if closes else None
        prior_peak = max(list(highs)[-self.extreme_lookback_days :], default=builder.high)
        prior_trough = min(list(lows)[-self.extreme_lookback_days :], default=builder.low)
        closes.append(builder.close)
        highs.append(builder.high)
        lows.append(builder.low)
        if len(closes) < self.daily_slow_window:
            return

        fast = fmean(list(closes)[-self.daily_fast_window :])
        slow = fmean(list(closes)[-self.daily_slow_window :])
        self.daily_fast[symbol] = fast
        self.daily_slow[symbol] = slow
        if self.setup_days_left[symbol] > 0:
            self.setup_days_left[symbol] -= 1
            if self.setup_days_left[symbol] == 0:
                self.setup_direction[symbol] = 0

        if self.long_extreme_days_left[symbol] > 0:
            self.long_extreme_days_left[symbol] -= 1
        if self.short_extreme_days_left[symbol] > 0:
            self.short_extreme_days_left[symbol] -= 1

        fell_enough = prior_peak > 0 and builder.low / prior_peak - 1 <= -self.extreme_move_threshold
        rose_enough = prior_trough > 0 and builder.high / prior_trough - 1 >= self.extreme_move_threshold
        crossed_above_fast = (
            previous_fast is not None
            and previous_close is not None
            and previous_close <= previous_fast
            and builder.close > fast
        )
        crossed_below_fast = (
            previous_fast is not None
            and previous_close is not None
            and previous_close >= previous_fast
            and builder.close < fast
        )
        if fell_enough:
            self.long_extreme_days_left[symbol] = self.extreme_lookback_days
        if rose_enough:
            self.short_extreme_days_left[symbol] = self.extreme_lookback_days
        if self.long_extreme_days_left[symbol] > 0 and crossed_above_fast:
            self.setup_direction[symbol] = 1
            self.setup_days_left[symbol] = self.setup_valid_days
            self.long_extreme_days_left[symbol] = 0
        elif (
            self.allow_short
            and self.short_extreme_days_left[symbol] > 0
            and crossed_below_fast
        ):
            self.setup_direction[symbol] = -1
            self.setup_days_left[symbol] = self.setup_valid_days
            self.short_extreme_days_left[symbol] = 0

        current_target = self.last_target[symbol]
        if current_target > 0 and builder.close < fast:
            self.daily_exit[symbol] = -1
        elif current_target < 0 and builder.close > fast:
            self.daily_exit[symbol] = 1

    def _update_divergence(self, symbol: str) -> None:
        radius = self.divergence_pivot_radius
        prices = list(self.closes[symbol])
        macd = list(self.macd_lines[symbol])
        if len(prices) < radius * 2 + 5 or len(macd) != len(prices):
            return
        end = len(prices) - radius
        start = max(radius, len(prices) - self.divergence_lookback)
        pivot_lows: list[int] = []
        pivot_highs: list[int] = []
        for idx in range(start, end):
            window = prices[idx - radius : idx + radius + 1]
            if prices[idx] == min(window):
                pivot_lows.append(idx)
            if prices[idx] == max(window):
                pivot_highs.append(idx)
        index = self.bar_number[symbol]
        if len(pivot_lows) >= 2:
            first, second = pivot_lows[-2:]
            if prices[second] < prices[first] and macd[second] > macd[first]:
                self.last_bull_divergence[symbol] = index - (len(prices) - 1 - second)
        if len(pivot_highs) >= 2:
            first, second = pivot_highs[-2:]
            if prices[second] > prices[first] and macd[second] < macd[first]:
                self.last_bear_divergence[symbol] = index - (len(prices) - 1 - second)

    @staticmethod
    def _trading_day(bar: Bar) -> date:
        """Map common domestic night-session timestamps to the next weekday.

        Providers with an exchange calendar should normalize holidays before
        constructing bars.  This local fallback correctly handles ordinary
        weekday and Friday-night sessions without future market data.
        """
        trading_day = bar.datetime.date()
        if bar.datetime.hour >= 18:
            trading_day += timedelta(days=1)
            while trading_day.weekday() >= 5:
                trading_day += timedelta(days=1)
        return trading_day

    @staticmethod
    def _ema(values: list[float], window: int) -> float:
        if not values:
            return 0.0
        alpha = 2.0 / (window + 1.0)
        value = values[0]
        for item in values[1:]:
            value = alpha * item + (1.0 - alpha) * value
        return value
