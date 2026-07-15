from __future__ import annotations

import math
from collections import defaultdict, deque
from statistics import fmean

from futures_quant.models import Bar, Signal
from futures_quant.strategies.base import Strategy


class TrendPullbackMovingAverageStrategy(Strategy):
    """15-minute MA169 crossing entry with repeated MA13 scale-outs.

    The historical strategy id ``dual_ma_pullback`` is retained for report and
    configuration compatibility.  Signals use completed bars and execute at
    the next bar's open, so no future bar information is used.
    """

    def __init__(
        self,
        fast_window: int = 13,
        slow_window: int = 169,
        order_size: int = 0,
        atr_window: int = 14,
        ma_exit_buffer_atr: float = 0.1,
        partial_exit_fraction: float = 0.30,
        position_equity_fraction: float = 0.60,
        initial_cash: float = 1_000_000.0,
        contract_multiplier: float = 10.0,
        margin_rate: float = 0.10,
        max_trade_risk: float = 0.005,
        max_notional_fraction: float = 10.0,
        max_order_size: int | None = 5,
        allow_short: bool = True,
    ) -> None:
        if fast_window <= 1 or slow_window <= fast_window:
            raise ValueError("Moving-average windows must satisfy 1 < fast < slow.")
        if order_size < 0:
            raise ValueError("order_size cannot be negative; use zero for automatic sizing.")
        if order_size > 5:
            raise ValueError("order_size cannot exceed the 5-lot hard limit.")
        if atr_window <= 1:
            raise ValueError("atr_window must exceed 1.")
        if ma_exit_buffer_atr < 0:
            raise ValueError("ma_exit_buffer_atr cannot be negative.")
        if not 0 < partial_exit_fraction <= 1:
            raise ValueError("partial_exit_fraction must be in (0, 1].")
        if not 0 < position_equity_fraction <= 1:
            raise ValueError("position_equity_fraction must be in (0, 1].")
        if initial_cash <= 0 or contract_multiplier <= 0:
            raise ValueError("initial_cash and contract_multiplier must be positive.")
        if not 0 < margin_rate < 1:
            raise ValueError("margin_rate must be in (0, 1).")
        if not 0 < max_trade_risk <= 1:
            raise ValueError("max_trade_risk must be in (0, 1].")
        if max_notional_fraction <= 0:
            raise ValueError("max_notional_fraction must be positive.")
        # This strategy is explicitly constrained to 1--5 contracts.  Legacy
        # configurations used zero to mean automatic sizing; retain that
        # syntax, but never interpret it as an unlimited order size.
        if max_order_size is not None and max_order_size < 0:
            raise ValueError("max_order_size cannot be negative.")
        if max_order_size is not None and max_order_size > 5:
            raise ValueError("max_order_size cannot exceed the 5-lot hard limit.")

        self.fast_window = fast_window
        self.slow_window = slow_window
        self.order_size = order_size
        self.atr_window = atr_window
        self.ma_exit_buffer_atr = ma_exit_buffer_atr
        self.partial_exit_fraction = partial_exit_fraction
        self.position_equity_fraction = position_equity_fraction
        self.account_equity = initial_cash
        self.contract_multiplier = contract_multiplier
        self.margin_rate = margin_rate
        self.max_trade_risk = max_trade_risk
        self.max_notional_fraction = max_notional_fraction
        self.max_order_size = max_order_size or 5
        self.allow_short = allow_short

        history = slow_window + 2
        self.closes: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=history)
        )
        self.true_ranges: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=atr_window)
        )
        self.last_target: dict[str, int] = defaultdict(int)
        self.pending_entries: set[str] = set()
        self.pending_exits: set[str] = set()

    def on_account_update(self, equity: float) -> None:
        if equity > 0:
            self.account_equity = equity

    def on_bar(self, bar: Bar) -> Signal | None:
        symbol = bar.symbol
        closes = self.closes[symbol]
        previous_close = closes[-1] if closes else bar.close
        true_range = max(
            bar.high - bar.low,
            abs(bar.high - previous_close),
            abs(bar.low - previous_close),
        )
        self.true_ranges[symbol].append(true_range)
        closes.append(bar.close)

        if len(closes) < self.slow_window + 1:
            return None
        if len(self.true_ranges[symbol]) < self.atr_window:
            return None

        values = list(closes)
        atr = fmean(self.true_ranges[symbol])
        fast = fmean(values[-self.fast_window :])
        slow = fmean(values[-self.slow_window :])
        previous_fast = fmean(values[-self.fast_window - 1 : -1])
        previous_slow = fmean(values[-self.slow_window - 1 : -1])
        target = self.last_target[symbol]

        if target > 0:
            if bar.close < slow - self.ma_exit_buffer_atr * atr:
                self.pending_exits.add(symbol)
                return Signal(
                    bar.datetime,
                    symbol,
                    0,
                    "ma169_cross buffered_slow_ma_stop_long",
                )
            if previous_close >= previous_fast and bar.close < fast:
                return self._partial_exit(bar, target, "ma13_reverse_cross_long")
            return None

        if target < 0:
            if bar.close > slow + self.ma_exit_buffer_atr * atr:
                self.pending_exits.add(symbol)
                return Signal(
                    bar.datetime,
                    symbol,
                    0,
                    "ma169_cross buffered_slow_ma_stop_short",
                )
            if previous_close <= previous_fast and bar.close > fast:
                return self._partial_exit(bar, target, "ma13_reverse_cross_short")
            return None

        if symbol in self.pending_entries or symbol in self.pending_exits:
            return None

        crossed_above_slow = previous_close <= previous_slow and bar.close > slow
        crossed_below_slow = previous_close >= previous_slow and bar.close < slow

        if crossed_above_slow:
            stop = slow - self.ma_exit_buffer_atr * atr
            size = self._position_size(bar.close, stop)
            if size < 1:
                return None
            self.pending_entries.add(symbol)
            return Signal(
                datetime=bar.datetime,
                symbol=symbol,
                target_position=size,
                reason=(
                    "ma169_cross long "
                    f"previous_close={previous_close:.6f} previous_slow={previous_slow:.6f} "
                    f"close={bar.close:.6f} slow={slow:.6f} atr={atr:.6f}"
                ),
                stop_price=stop,
            )

        if self.allow_short and crossed_below_slow:
            stop = slow + self.ma_exit_buffer_atr * atr
            size = self._position_size(bar.close, stop)
            if size < 1:
                return None
            self.pending_entries.add(symbol)
            return Signal(
                datetime=bar.datetime,
                symbol=symbol,
                target_position=-size,
                reason=(
                    "ma169_cross short "
                    f"previous_close={previous_close:.6f} previous_slow={previous_slow:.6f} "
                    f"close={bar.close:.6f} slow={slow:.6f} atr={atr:.6f}"
                ),
                stop_price=stop,
            )
        return None

    def _position_size(self, entry_price: float, stop_price: float) -> int:
        notional_per_contract = entry_price * self.contract_multiplier
        margin_per_contract = notional_per_contract * self.margin_rate
        risk_per_contract = abs(entry_price - stop_price) * self.contract_multiplier
        if margin_per_contract <= 0 or risk_per_contract <= 0:
            return 0

        margin_cap = math.floor(
            self.account_equity * self.position_equity_fraction / margin_per_contract
        )
        risk_cap = math.floor(
            self.account_equity * self.max_trade_risk / risk_per_contract
        )
        notional_cap = math.floor(
            self.account_equity * self.max_notional_fraction / notional_per_contract
        )
        caps = [margin_cap, risk_cap, notional_cap]
        if self.order_size > 0:
            caps.append(self.order_size)
        if self.max_order_size:
            caps.append(self.max_order_size)
        return max(0, min(caps))

    def _partial_exit(self, bar: Bar, current: int, reason: str) -> Signal:
        exit_size = max(1, math.floor(abs(current) * self.partial_exit_fraction))
        exit_size = min(exit_size, abs(current))
        remaining = abs(current) - exit_size
        target = remaining if current > 0 else -remaining
        if target == 0:
            self.pending_exits.add(bar.symbol)
        return Signal(
            datetime=bar.datetime,
            symbol=bar.symbol,
            target_position=target,
            reason=(
                f"ma169_cross {reason} fraction={self.partial_exit_fraction:.4f} "
                f"exit_size={exit_size}"
            ),
        )

    def on_position_update(
        self, symbol: str, quantity: int, avg_price: float
    ) -> None:
        self.last_target[symbol] = quantity
        self.pending_entries.discard(symbol)
        if quantity == 0:
            self.pending_exits.discard(symbol)

    def on_order_rejected(self, signal: Signal, status: str) -> None:
        symbol = signal.symbol
        self.pending_entries.discard(symbol)
        self.pending_exits.discard(symbol)
