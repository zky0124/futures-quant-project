from __future__ import annotations

from dataclasses import dataclass

from futures_quant.models import Bar, Order


@dataclass(frozen=True)
class RiskLimits:
    max_margin_usage: float
    max_symbol_exposure: float
    daily_loss_stop: float
    margin_rate: float
    contract_multiplier: int
    max_symbol_margin_usage: float = 1.0
    max_trade_risk: float = 1.0


class RiskEngine:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def check_order(
        self,
        order: Order,
        bar: Bar,
        equity: float,
        current_margin: float,
        current_position: int = 0,
    ) -> tuple[bool, str]:
        price = float(order.price)
        projected_position = current_position + order.quantity
        current_symbol_margin = (
            abs(current_position)
            * price
            * self.limits.contract_multiplier
            * self.limits.margin_rate
        )
        projected_symbol_margin = (
            abs(projected_position)
            * price
            * self.limits.contract_multiplier
            * self.limits.margin_rate
        )
        projected_margin = max(0.0, current_margin - current_symbol_margin) + projected_symbol_margin
        projected_notional = abs(projected_position) * price * self.limits.contract_multiplier
        if equity <= 0:
            return False, "equity_not_positive"
        # Closing or otherwise reducing an existing position must remain possible
        # even when the account is already above an opening-position limit.
        if abs(projected_position) < abs(current_position):
            return True, "risk_reducing"
        if order.stop_price is not None and projected_position:
            if (
                projected_position > 0
                and order.stop_price >= price
                or projected_position < 0
                and order.stop_price <= price
            ):
                return False, "protective_stop_crossed_before_entry"
            initial_risk = (
                abs(price - order.stop_price)
                * abs(projected_position)
                * self.limits.contract_multiplier
            )
            if initial_risk / equity > self.limits.max_trade_risk:
                return False, "max_trade_risk_exceeded"
        if projected_margin / equity > self.limits.max_margin_usage:
            return False, "max_margin_usage_exceeded"
        if projected_symbol_margin / equity > self.limits.max_symbol_margin_usage:
            return False, "max_symbol_margin_usage_exceeded"
        if projected_notional / equity > self.limits.max_symbol_exposure:
            return False, "max_symbol_exposure_exceeded"
        return True, "ok"
