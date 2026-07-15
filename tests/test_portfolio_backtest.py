import unittest
from datetime import datetime, timedelta

from futures_quant.broker.portfolio import (
    PortfolioRiskLimits,
    SharedPortfolioBroker,
    run_portfolio_backtest,
    summarize_portfolio_period,
)
from futures_quant.data.contracts import ContractSpec
from futures_quant.models import Bar, Signal
from futures_quant.strategies.base import Strategy


def make_spec(
    symbol: str,
    *,
    exchange: str = "SHFE",
    multiplier: int = 10,
    tick_size: float = 1.0,
    margin_rate: float = 0.10,
    commission_rate: float = 0.0,
) -> ContractSpec:
    return ContractSpec(
        symbol=symbol,
        exchange=exchange,
        product=symbol.lower(),
        contract_multiplier=multiplier,
        tick_size=tick_size,
        margin_rate=margin_rate,
        commission_rate=commission_rate,
    )


def make_bar(
    timestamp: datetime,
    symbol: str,
    open_price: float,
    close_price: float,
) -> Bar:
    return Bar(
        datetime=timestamp,
        symbol=symbol,
        open=open_price,
        high=max(open_price, close_price) + 1,
        low=min(open_price, close_price) - 1,
        close=close_price,
        volume=1000,
    )


class FirstCloseTargets(Strategy):
    def __init__(self, targets: dict[str, int]) -> None:
        self.targets = targets
        self.sent: set[str] = set()

    def on_bar(self, bar: Bar) -> Signal | None:
        if bar.symbol in self.sent:
            return None
        self.sent.add(bar.symbol)
        return Signal(
            datetime=bar.datetime,
            symbol=bar.symbol,
            target_position=self.targets[bar.symbol],
            reason="first_close_target",
        )


class ScheduledTargets(Strategy):
    def __init__(self, targets: list[int | None]) -> None:
        self.targets = targets
        self.index = 0

    def on_bar(self, bar: Bar) -> Signal | None:
        target = self.targets[self.index]
        self.index += 1
        if target is None:
            return None
        return Signal(bar.datetime, bar.symbol, target, f"scheduled_target={target}")


class NoSignals(Strategy):
    def on_bar(self, bar: Bar) -> Signal | None:
        return None


class SharedPortfolioBacktestTest(unittest.TestCase):
    def test_synchronized_next_open_uses_each_contract_spec_and_exit_costs(self) -> None:
        start = datetime(2025, 1, 2, 15)
        next_day = start + timedelta(days=1)
        bars = {
            "A": [
                make_bar(start, "A", 100, 100),
                make_bar(next_day, "A", 110, 112),
            ],
            "B": [
                make_bar(start, "B", 50, 50),
                make_bar(next_day, "B", 55, 56),
            ],
        }
        specs = {
            "A": make_spec(
                "A", multiplier=10, tick_size=1, commission_rate=0.001
            ),
            "B": make_spec(
                "B",
                exchange="DCE",
                multiplier=100,
                tick_size=0.5,
                margin_rate=0.20,
                commission_rate=0.002,
            ),
        }
        broker = SharedPortfolioBroker(
            initial_cash=100_000,
            contract_specs=specs,
            risk_limits=PortfolioRiskLimits(1.0, 1.0, 0.10),
            slippage_ticks=1,
        )

        result = run_portfolio_backtest(
            bars, FirstCloseTargets({"A": 1, "B": 1}), broker
        )

        # One row per shared timestamp, not one independently funded row per symbol.
        self.assertEqual(len(result.equity_curve), 2)
        self.assertEqual(len(result.trades), 4)
        entries = result.trades[
            ~result.trades["reason"].str.contains("end_of_portfolio")
        ].set_index("symbol")
        exits = result.trades[
            result.trades["reason"].str.contains("end_of_portfolio")
        ].set_index("symbol")

        self.assertEqual(float(entries.loc["A", "reference_price"]), 110.0)
        self.assertEqual(float(entries.loc["A", "price"]), 111.0)
        self.assertAlmostEqual(float(entries.loc["A", "commission"]), 1.11)
        self.assertEqual(float(entries.loc["B", "price"]), 55.5)
        self.assertAlmostEqual(float(entries.loc["B", "commission"]), 11.1)
        self.assertEqual(float(exits.loc["A", "price"]), 111.0)
        self.assertEqual(float(exits.loc["B", "price"]), 55.5)
        self.assertAlmostEqual(result.summary["commission_total"], 24.42)
        self.assertAlmostEqual(result.summary["slippage_cost_total"], 120.0)
        self.assertAlmostEqual(result.summary["final_equity"], 99_975.58)
        self.assertEqual(result.summary["open_position_count"], 0)
        self.assertEqual(float(result.equity_curve.iloc[-1]["margin"]), 0.0)

        period = summarize_portfolio_period(
            result,
            next_day,
            initial_account_cash=100_000,
        )
        self.assertEqual(period["start"], "2025-01-03")
        self.assertEqual(period["evaluation_anchor_equity"], 100_000.0)
        self.assertTrue(period["positions_carried_into_period"])

    def test_competing_orders_use_one_shared_margin_pool(self) -> None:
        start = datetime(2025, 1, 2, 15)
        next_day = start + timedelta(days=1)
        bars = [
            make_bar(start, "A", 100, 100),
            make_bar(start, "B", 100, 100),
            make_bar(next_day, "A", 100, 100),
            make_bar(next_day, "B", 100, 100),
        ]
        specs = {
            "A": make_spec("A", multiplier=10, margin_rate=0.10),
            "B": make_spec(
                "B", exchange="DCE", multiplier=10, margin_rate=0.20
            ),
        }
        broker = SharedPortfolioBroker(
            initial_cash=1_000,
            contract_specs=specs,
            risk_limits=PortfolioRiskLimits(0.20, 1.0, 0.10),
        )

        result = run_portfolio_backtest(
            bars, FirstCloseTargets({"A": 1, "B": 1}), broker
        )

        entry_symbols = result.trades[
            ~result.trades["reason"].str.contains("end_of_portfolio")
        ]["symbol"].tolist()
        self.assertEqual(entry_symbols, ["A"])
        self.assertEqual(result.summary["rejected_order_count"], 1)
        self.assertEqual(
            result.rejections.iloc[0]["status"], "max_margin_usage_exceeded"
        )
        self.assertLessEqual(
            result.summary["max_margin_usage_observed"],
            0.20,
        )

    def test_daily_loss_stop_blocks_increase_and_reversal_but_allows_flattening(self) -> None:
        start = datetime(2025, 1, 2, 9)
        bars = [
            make_bar(start + timedelta(hours=offset), "A", open_price, close_price)
            for offset, open_price, close_price in [
                (0, 100, 100),
                (1, 100, 90),
                (2, 90, 90),
                (3, 90, 90),
                (4, 90, 90),
            ]
        ]
        broker = SharedPortfolioBroker(
            initial_cash=1_000,
            contract_specs={"A": make_spec("A", multiplier=10)},
            risk_limits=PortfolioRiskLimits(1.0, 1.0, 0.05),
        )
        strategy = ScheduledTargets([1, 2, -1, 0, None])

        result = run_portfolio_backtest(bars, strategy, broker)

        self.assertEqual(result.rejections["status"].tolist(), [
            "daily_loss_stop",
            "daily_loss_stop",
        ])
        # The first trade opens one long; the second is the allowed flat order.
        self.assertEqual(result.trades["quantity"].tolist(), [1, -1])
        self.assertEqual(result.summary["final_equity"], 900.0)
        self.assertEqual(result.summary["open_position_count"], 0)

    def test_foreign_currency_is_rejected_without_explicit_homogeneous_currency(self) -> None:
        timestamp = datetime(2025, 1, 2, 15)
        bars = [make_bar(timestamp, "ES", 6000, 6000)]
        spec = make_spec(
            "ES", exchange="CME", multiplier=50, tick_size=0.25
        )
        broker = SharedPortfolioBroker(
            initial_cash=100_000,
            contract_specs={"ES": spec},
            risk_limits=PortfolioRiskLimits(1.0, 1.0, 0.10),
        )

        with self.assertRaisesRegex(ValueError, "FX conversion is not implemented"):
            run_portfolio_backtest(bars, NoSignals(), broker)

        usd_broker = SharedPortfolioBroker(
            initial_cash=100_000,
            contract_specs={"ES": spec},
            risk_limits=PortfolioRiskLimits(1.0, 1.0, 0.10),
            base_currency="USD",
            symbol_currencies={"ES": "USD"},
        )
        result = run_portfolio_backtest(bars, NoSignals(), usd_broker)
        self.assertEqual(result.summary["base_currency"], "USD")

    def test_mixed_explicit_currencies_are_rejected_instead_of_silently_added(self) -> None:
        timestamp = datetime(2025, 1, 2, 15)
        bars = [
            make_bar(timestamp, "A", 100, 100),
            make_bar(timestamp, "ES", 6000, 6000),
        ]
        specs = {
            "A": make_spec("A"),
            "ES": make_spec(
                "ES", exchange="CME", multiplier=50, tick_size=0.25
            ),
        }
        broker = SharedPortfolioBroker(
            initial_cash=100_000,
            contract_specs=specs,
            risk_limits=PortfolioRiskLimits(1.0, 1.0, 0.10),
            base_currency="CNY",
            symbol_currencies={"A": "CNY", "ES": "USD"},
        )

        with self.assertRaisesRegex(ValueError, "Mismatches"):
            run_portfolio_backtest(bars, NoSignals(), broker)


if __name__ == "__main__":
    unittest.main()
