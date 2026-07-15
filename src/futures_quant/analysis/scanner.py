from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pandas as pd

from futures_quant.broker.portfolio import (
    PortfolioRiskLimits,
    SharedPortfolioBroker,
    run_portfolio_backtest,
)
from futures_quant.cli import build_strategy
from futures_quant.config import StrategyConfig
from futures_quant.data.contracts import ContractSpec
from futures_quant.data.csv_loader import load_bars
from futures_quant.data.history import Instrument
from futures_quant.data.timeframe import aggregate_bars


ProgressCallback = Callable[[int, int, str], None]
CancelCheck = Callable[[], bool]


def scan_instruments(
    instruments: Sequence[Instrument],
    *,
    data_dir: Path,
    suffix: str,
    source_interval_minutes: int,
    bar_interval_minutes: int,
    strategy_config: StrategyConfig,
    initial_cash: float,
    max_symbol_exposure: float,
    risk_limits: PortfolioRiskLimits,
    slippage_ticks: int,
    contract_specs: Mapping[str, ContractSpec],
    progress: ProgressCallback | None = None,
    cancelled: CancelCheck | None = None,
) -> pd.DataFrame:
    """Run one isolated account per instrument and return a comparable ranking."""

    rows: list[dict[str, object]] = []
    total = len(instruments)
    for index, instrument in enumerate(instruments, start=1):
        if cancelled is not None and cancelled():
            break
        symbol = instrument.symbol
        try:
            spec = contract_specs[symbol]
            source_bars = load_bars(data_dir / f"{symbol}{suffix}")
            bars = aggregate_bars(
                source_bars,
                target_minutes=bar_interval_minutes,
                source_minutes=source_interval_minutes,
            )
            strategy = build_strategy(
                strategy_config,
                initial_cash,
                spec.contract_multiplier,
                max_symbol_exposure,
                margin_rate=spec.margin_rate,
                max_symbol_margin_usage=risk_limits.max_symbol_margin_usage,
                max_trade_risk=risk_limits.max_trade_risk,
            )
            broker = SharedPortfolioBroker(
                initial_cash=initial_cash,
                contract_specs={symbol: spec},
                risk_limits=risk_limits,
                slippage_ticks=slippage_ticks,
                symbol_groups={symbol: instrument.group.split("-")[-1]},
            )
            result = run_portfolio_backtest({symbol: bars}, {symbol: strategy}, broker)
            summary = result.summary
            rows.append(
                {
                    "symbol": symbol,
                    "name": instrument.name,
                    "group": instrument.group,
                    "status": "ok",
                    "total_return": float(summary.get("total_return", 0.0)),
                    "annualized_return": float(
                        summary.get("annualized_return", 0.0)
                    ),
                    "max_drawdown": float(summary.get("max_drawdown", 0.0)),
                    "sharpe": float(summary.get("sharpe", 0.0)),
                    "calmar": float(summary.get("calmar", 0.0)),
                    "trade_count": int(summary.get("trade_count", 0)),
                    "rejected_order_count": int(
                        summary.get("rejected_order_count", 0)
                    ),
                    "max_margin_usage_observed": float(
                        summary.get("max_margin_usage_observed", 0.0)
                    ),
                    "final_equity": float(summary.get("final_equity", initial_cash)),
                    "source_bar_count": len(source_bars),
                    "aggregated_bar_count": len(bars),
                    "error": "",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "symbol": symbol,
                    "name": instrument.name,
                    "group": instrument.group,
                    "status": "error",
                    "total_return": float("nan"),
                    "annualized_return": float("nan"),
                    "max_drawdown": float("nan"),
                    "sharpe": float("nan"),
                    "calmar": float("nan"),
                    "trade_count": 0,
                    "rejected_order_count": 0,
                    "max_margin_usage_observed": float("nan"),
                    "final_equity": float("nan"),
                    "source_bar_count": 0,
                    "aggregated_bar_count": 0,
                    "error": str(exc),
                }
            )
        finally:
            if progress is not None:
                progress(index, total, symbol)

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["_successful"] = frame["status"].eq("ok")
    frame = frame.sort_values(
        ["_successful", "total_return", "sharpe", "symbol"],
        ascending=[False, False, False, True],
        na_position="last",
    ).reset_index(drop=True)
    frame.insert(0, "rank", range(1, len(frame) + 1))
    return frame.drop(columns="_successful")
