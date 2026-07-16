from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from futures_quant.analysis.metrics import save_summary
from futures_quant.analysis.portfolio import analyze_batch_results
from futures_quant.analysis.report import generate_markdown_report
from futures_quant.api.ctp_adapter import diagnose_ctp_adapter
from futures_quant.api.ctp_config import load_ctp_config
from futures_quant.api.mock_gateway import MockGateway
from futures_quant.broker.backtest import BacktestBroker, run_backtest
from futures_quant.broker.portfolio import (
    DOMESTIC_CNY_EXCHANGES,
    PortfolioRiskLimits,
    SharedPortfolioBroker,
    run_portfolio_backtest,
    summarize_portfolio_period,
)
from futures_quant.config import StrategyConfig, load_backtest_config
from futures_quant.data.contracts import load_contract_specs
from futures_quant.data.history import load_universe, write_demo_history, write_demo_intraday_history
from futures_quant.data.pobo import batch_import_pobo_data, import_pobo_his
from futures_quant.data.providers import build_provider
from futures_quant.data.quality import validate_history_dir
from futures_quant.data.recorder import CsvBarRecorder
from futures_quant.data.source import CsvBarSource, GatewaySnapshotSource
from futures_quant.data.timeframe import aggregate_bars
from futures_quant.models import Bar, Order
from futures_quant.optimization.walk_forward import optimize_strategy
from futures_quant.presentation.chinese import (
    chinese_column_name,
    chinese_summary_frame,
    chinese_value,
    write_chinese_companion,
)
from futures_quant.risk.rules import RiskEngine, RiskLimits
from futures_quant.strategies.adaptive_trend import AdaptiveTrendStrategy
from futures_quant.strategies.adaptive_trend_v2 import EnhancedAdaptiveTrendStrategy
from futures_quant.strategies.dual_ma import DualMovingAverageStrategy
from futures_quant.strategies.dual_period_reversal import DualPeriodReversalStrategy
from futures_quant.strategies.ma_trend_pullback import TrendPullbackMovingAverageStrategy


def _print_chinese_values(values: dict[str, object]) -> None:
    for key, value in values.items():
        print(f"{chinese_column_name(key)}：{chinese_value(value)}")


def _write_existing_csv_companion(path: Path) -> Path | None:
    if path.suffix.lower() != ".csv" or not path.exists():
        return None
    return write_chinese_companion(pd.read_csv(path), path)


def build_strategy(
    config: StrategyConfig,
    initial_cash: float,
    contract_multiplier: float,
    max_symbol_exposure: float,
    margin_rate: float = 0.10,
    max_symbol_margin_usage: float = 0.20,
    max_trade_risk: float = 0.005,
):
    if config.name == "dual_ma":
        return DualMovingAverageStrategy(
            fast_window=config.fast_window,
            slow_window=config.slow_window,
            order_size=config.order_size,
        )
    if config.name == "dual_ma_pullback":
        return TrendPullbackMovingAverageStrategy(
            fast_window=config.fast_window,
            slow_window=config.slow_window,
            order_size=config.order_size,
            atr_window=config.atr_window,
            ma_exit_buffer_atr=config.ma_exit_buffer_atr,
            partial_exit_fraction=config.partial_exit_fraction,
            position_equity_fraction=config.position_equity_fraction,
            initial_cash=initial_cash,
            contract_multiplier=contract_multiplier,
            margin_rate=margin_rate,
            max_trade_risk=max_trade_risk,
            max_notional_fraction=min(
                config.max_notional_fraction, max_symbol_exposure
            ),
            max_order_size=config.max_order_size or 5,
            allow_short=config.allow_short,
        )
    if config.name == "adaptive_trend":
        return AdaptiveTrendStrategy(
            entry_window=config.entry_window,
            exit_window=config.exit_window,
            trend_window=config.trend_window,
            momentum_window=config.momentum_window,
            volatility_window=config.volatility_window,
            target_annual_volatility=config.target_annual_volatility,
            order_size=config.order_size,
            max_order_size=config.max_order_size,
            initial_cash=initial_cash,
            contract_multiplier=contract_multiplier,
            max_notional_fraction=min(
                config.max_notional_fraction,
                max_symbol_exposure,
            ),
            momentum_threshold=config.momentum_threshold,
            allow_short=config.allow_short,
            annualization_factor=config.annualization_factor,
        )
    if config.name == "adaptive_trend_v2":
        return EnhancedAdaptiveTrendStrategy(
            entry_window=config.entry_window,
            exit_window=config.exit_window,
            trend_window=config.trend_window,
            momentum_window=config.momentum_window,
            volatility_window=config.volatility_window,
            target_annual_volatility=config.target_annual_volatility,
            order_size=config.order_size,
            max_order_size=config.max_order_size or 5,
            initial_cash=initial_cash,
            contract_multiplier=contract_multiplier,
            margin_rate=margin_rate,
            max_notional_fraction=min(
                config.max_notional_fraction, max_symbol_exposure
            ),
            max_margin_fraction=max_symbol_margin_usage,
            max_trade_risk=max_trade_risk,
            momentum_threshold=config.momentum_threshold,
            allow_short=config.allow_short,
            annualization_factor=config.annualization_factor,
            atr_window=config.atr_window,
            atr_stop_multiple=config.atr_stop_multiple,
            break_even_trigger_r=config.break_even_trigger_r,
            reward_risk=config.reward_risk,
            partial_exit_fraction=config.partial_exit_fraction,
            trailing_atr_multiple=config.trailing_atr_multiple,
            cooldown_bars=config.cooldown_bars,
            loss_pause_after=config.loss_pause_after,
            loss_pause_bars=config.loss_pause_bars,
        )
    if config.name == "dual_period_reversal":
        return DualPeriodReversalStrategy(
            fast_window=config.fast_window,
            slow_window=config.slow_window,
            order_size=config.order_size,
            daily_fast_window=config.daily_fast_window,
            daily_slow_window=config.daily_slow_window,
            extreme_lookback_days=config.extreme_lookback_days,
            extreme_move_threshold=config.extreme_move_threshold,
            setup_valid_days=config.setup_valid_days,
            macd_fast=config.macd_fast,
            macd_slow=config.macd_slow,
            macd_signal=config.macd_signal,
            divergence_lookback=config.divergence_lookback,
            divergence_pivot_radius=config.divergence_pivot_radius,
            divergence_valid_bars=config.divergence_valid_bars,
            second_cross_window=config.second_cross_window,
            atr_window=config.atr_window,
            atr_stop_buffer=config.atr_stop_buffer,
            reward_risk=config.reward_risk,
            trailing_atr_multiple=config.trailing_atr_multiple,
            allow_short=config.allow_short,
        )
    raise ValueError(f"Unsupported strategy: {config.name}")


def strategy_parameters(config: StrategyConfig) -> dict[str, object]:
    """Return only constructor parameters accepted by the selected strategy."""

    common = {
        "order_size": config.order_size,
        "allow_short": config.allow_short,
    }
    if config.name == "dual_ma":
        return {
            "fast_window": config.fast_window,
            "slow_window": config.slow_window,
            "order_size": config.order_size,
        }
    if config.name == "dual_ma_pullback":
        keys = (
            "fast_window", "slow_window", "atr_window",
            "ma_exit_buffer_atr", "partial_exit_fraction",
            "position_equity_fraction", "max_order_size",
            "max_notional_fraction",
        )
    elif config.name == "dual_period_reversal":
        keys = (
            "fast_window", "slow_window", "daily_fast_window",
            "daily_slow_window", "extreme_lookback_days",
            "extreme_move_threshold", "setup_valid_days", "macd_fast",
            "macd_slow", "macd_signal", "divergence_lookback",
            "divergence_pivot_radius", "divergence_valid_bars",
            "second_cross_window", "atr_window", "atr_stop_buffer",
            "reward_risk", "trailing_atr_multiple",
        )
    elif config.name == "adaptive_trend":
        keys = (
            "entry_window", "exit_window", "trend_window",
            "momentum_window", "volatility_window",
            "target_annual_volatility", "order_size", "max_order_size",
            "max_notional_fraction", "momentum_threshold", "allow_short",
            "annualization_factor",
        )
        common = {}
    elif config.name == "adaptive_trend_v2":
        keys = (
            "entry_window", "exit_window", "trend_window",
            "momentum_window", "volatility_window",
            "target_annual_volatility", "order_size", "max_order_size",
            "max_notional_fraction", "momentum_threshold", "allow_short",
            "annualization_factor", "atr_window", "atr_stop_multiple",
            "break_even_trigger_r", "reward_risk", "partial_exit_fraction",
            "trailing_atr_multiple", "cooldown_bars", "loss_pause_after",
            "loss_pause_bars",
        )
        common = {}
    else:
        raise ValueError(f"Unsupported strategy: {config.name}")
    return {**common, **{key: getattr(config, key) for key in keys}}


def execute_backtest(
    config_path: Path,
    project_root: Path,
    data_path: Path | None = None,
    symbol: str | None = None,
    report_path: Path | None = None,
) -> dict[str, object]:
    cfg = load_backtest_config(config_path, project_root)
    data_symbol = symbol or cfg.data.symbol
    resolved_data_path = data_path or cfg.data.path
    resolved_report_path = report_path or cfg.report.path
    contract_multiplier = cfg.contract_multiplier
    tick_size = cfg.tick_size
    margin_rate = cfg.margin_rate
    commission_rate = cfg.commission_rate
    if cfg.contracts.path is not None:
        specs = load_contract_specs(cfg.contracts.path)
        if data_symbol not in specs:
            raise ValueError(f"Contract spec not found for symbol: {data_symbol}")
        spec = specs[data_symbol]
        contract_multiplier = spec.contract_multiplier
        tick_size = spec.tick_size
        margin_rate = spec.margin_rate
        commission_rate = spec.commission_rate

    source = CsvBarSource(str(resolved_data_path))
    source_bars = list(source.bars())
    bars = aggregate_bars(
        source_bars,
        target_minutes=cfg.data.bar_interval_minutes,
        source_minutes=cfg.data.source_interval_minutes,
    )
    strategy = build_strategy(
        config=cfg.strategy,
        initial_cash=cfg.initial_cash,
        contract_multiplier=contract_multiplier,
        max_symbol_exposure=cfg.max_symbol_exposure,
        margin_rate=margin_rate,
        max_symbol_margin_usage=cfg.max_symbol_margin_usage,
        max_trade_risk=cfg.max_trade_risk,
    )
    risk = RiskEngine(
        RiskLimits(
            max_margin_usage=cfg.max_margin_usage,
            max_symbol_exposure=cfg.max_symbol_exposure,
            daily_loss_stop=cfg.daily_loss_stop,
            margin_rate=margin_rate,
            contract_multiplier=contract_multiplier,
            max_symbol_margin_usage=cfg.max_symbol_margin_usage,
            max_trade_risk=cfg.max_trade_risk,
        )
    )
    broker = BacktestBroker(
        initial_cash=cfg.initial_cash,
        commission_rate=commission_rate,
        slippage_ticks=cfg.slippage_ticks,
        tick_size=tick_size,
        contract_multiplier=contract_multiplier,
        margin_rate=margin_rate,
        risk_engine=risk,
    )
    result = run_backtest(bars, strategy, broker)
    result.summary.update(
        {
            "source_interval_minutes": cfg.data.source_interval_minutes,
            "bar_interval_minutes": cfg.data.bar_interval_minutes,
            "source_bar_count": len(source_bars),
            "aggregated_bar_count": len(bars),
        }
    )
    resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
    save_summary(result.summary, str(resolved_report_path))
    equity_path = resolved_report_path.with_name(f"{resolved_report_path.stem}_equity_curve.csv")
    trades_path = resolved_report_path.with_name(f"{resolved_report_path.stem}_trades.csv")
    rejections_path = resolved_report_path.with_name(
        f"{resolved_report_path.stem}_rejections.csv"
    )
    result.equity_curve.to_csv(equity_path, index=False, encoding="utf-8-sig")
    result.trades.to_csv(trades_path, index=False, encoding="utf-8-sig")
    result.rejections.to_csv(rejections_path, index=False, encoding="utf-8-sig")
    summary = dict(result.summary)
    summary["symbol"] = data_symbol
    summary["data_path"] = str(resolved_data_path)
    summary["summary_path"] = str(resolved_report_path)
    chinese_summary_frame(summary).to_csv(
        resolved_report_path.with_name(f"{resolved_report_path.stem}_中文.csv"),
        index=False,
        encoding="utf-8-sig",
    )
    write_chinese_companion(result.equity_curve, equity_path)
    write_chinese_companion(result.trades, trades_path)
    write_chinese_companion(result.rejections, rejections_path)
    return summary


def run(
    config_path: Path,
    project_root: Path,
    data_path: Path | None = None,
    symbol: str | None = None,
    report_path: Path | None = None,
) -> None:
    summary = execute_backtest(config_path, project_root, data_path, symbol, report_path)

    print("回测完成")
    _print_chinese_values(summary)


def replay_gateway_csv(input_path: Path, output_path: Path, symbol: str) -> None:
    gateway = MockGateway()
    gateway.connect()
    gateway.subscribe(symbol)
    source_bars = list(CsvBarSource(str(input_path)).bars())
    snapshot_source = GatewaySnapshotSource(gateway, [symbol])
    recorded: list[Bar] = []
    for bar in source_bars:
        if bar.symbol != symbol:
            continue
        gateway.push_bar(bar)
        recorded.extend(snapshot_source.bars())
    CsvBarRecorder(output_path).write(recorded)
    print("Gateway CSV replay complete")
    print(f"input: {input_path}")
    print(f"output: {output_path}")
    print(f"symbol: {symbol}")
    print(f"bar_count: {len(recorded)}")


def import_pobo_history(
    input_path: Path,
    output_path: Path,
    name_table_path: Path | None,
    symbol: str | None,
    target_minutes: int,
) -> None:
    result = import_pobo_his(
        input_path,
        output_path,
        name_table_path=name_table_path,
        symbol=symbol,
        target_minutes=target_minutes,
    )
    print("Pobo history import complete")
    print(f"name: {result.instrument.name}")
    print(f"pb_code: {result.instrument.pb_code}")
    print(f"br_code: {result.instrument.br_code}")
    print(f"symbol: {result.symbol}")
    print(f"price_rate: {result.instrument.price_rate:g}")
    print(f"source_bar_count: {result.source_bar_count}")
    print(f"output_bar_count: {result.output_bar_count}")
    print(f"output: {result.output_path}")


def import_pobo_batch(
    data_root: Path,
    output_dir: Path,
    manifest_path: Path,
    contracts_path: Path,
    universe_path: Path,
    minimum_coverage_days: float,
    symbols_csv: str | None,
    series_preference_csv: str,
) -> None:
    specs = load_contract_specs(contracts_path)
    universe = load_universe(universe_path)
    universe_symbols = {instrument.symbol for instrument in universe.instruments}
    eligible_symbols = set(specs).intersection(universe_symbols)
    if symbols_csv:
        requested = {
            value.strip() for value in symbols_csv.split(",") if value.strip()
        }
        unknown = sorted(requested - eligible_symbols)
        if unknown:
            raise ValueError(
                "Requested Pobo symbols must exist in both contracts and universe: "
                f"{unknown}"
            )
        eligible_symbols = requested
    preference = tuple(
        value.strip() for value in series_preference_csv.split(",") if value.strip()
    )
    result = batch_import_pobo_data(
        data_root,
        output_dir,
        manifest_path,
        project_symbols=eligible_symbols,
        minimum_coverage_days=minimum_coverage_days,
        series_preference=preference,
    )
    status_counts: dict[str, int] = {}
    for row in result.rows:
        status_counts[row.status] = status_counts.get(row.status, 0) + 1
    print("Pobo batch history import complete")
    print(f"discovered_files: {len(result.rows)}")
    print(f"exported_symbols: {','.join(result.exported_symbols)}")
    print(f"status_counts: {status_counts}")
    print(f"output_dir: {result.output_dir}")
    print(f"manifest: {result.manifest_path}")


def gateway_smoke() -> None:
    gateway = MockGateway()
    gateway.connect()
    gateway.subscribe("RB2405")
    bar = Bar(
        datetime=datetime(2026, 1, 2, 15, 0),
        symbol="RB2405",
        open=3500,
        high=3526,
        low=3488,
        close=3518,
        volume=124320,
        open_interest=820000,
    )
    gateway.push_bar(bar)
    source = GatewaySnapshotSource(gateway, ["RB2405"])
    normalized_bars = list(source.bars())
    order_id = gateway.send_order(
        Order(
            datetime=bar.datetime,
            symbol=bar.symbol,
            quantity=1,
            price=bar.close,
            reason="gateway_smoke",
        )
    )
    print("Gateway smoke complete")
    print(f"connected: {gateway.connected}")
    print(f"subscriptions: {sorted(gateway.subscriptions)}")
    print(f"latest_bar: {gateway.latest_bar('RB2405')}")
    print(f"normalized_bar_count: {len(normalized_bars)}")
    print(f"order_id: {order_id}")
    print(f"trade_count: {len(gateway.trades())}")


def ctp_diagnose(config_path: Path) -> None:
    """Print a redacted, offline CTP readiness report.

    Passing an empty environment deliberately prevents this diagnostic from
    reading local credentials.  It never imports an adapter factory, connects,
    logs in, subscribes, or sends an order.
    """

    config = load_ctp_config(config_path, environ={})
    report = diagnose_ctp_adapter(config)
    report["config_path"] = str(config_path)
    report["credential_environment_read"] = False
    report["network_action"] = False
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def okx_diagnose(config_path: Path, *, connect_read_only: bool) -> None:
    """Inspect OKX readiness without ever placing or cancelling an order."""

    from futures_quant.api.okx_config import load_okx_config
    from futures_quant.api.okx_rest import OkxPrivateClient

    config = load_okx_config(
        config_path, environ=None if connect_read_only else {}
    )
    report: dict[str, object] = {
        "config_path": str(config_path),
        "config": config.redacted_summary(),
        "credential_environment_read": connect_read_only,
        "network_action": connect_read_only,
        "order_action": False,
    }
    if connect_read_only:
        client = OkxPrivateClient(config)
        server_time_ms = client.get_server_time_ms()
        local_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        identity = client.verify_subaccount_identity()
        report.update(
            {
                "server_clock_drift_ms": local_time_ms - server_time_ms,
                "identity": identity.redacted_summary(),
                "balance_record_count": len(client.get_balances()),
                "position_record_count": len(
                    client.get_positions(inst_type="SWAP")
                ),
                "pending_order_count": len(
                    client.get_pending_orders(inst_type="SWAP")
                ),
            }
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def okx_download_history(
    inst_id: str,
    bar: str,
    start_text: str,
    output_path: Path,
    max_pages: int,
) -> None:
    """Download public OKX candles only; API credentials are not used."""

    from futures_quant.api.okx_rest import OkxPublicClient
    from futures_quant.data.okx import download_okx_history

    start = datetime.fromisoformat(start_text)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    result = download_okx_history(
        OkxPublicClient(),
        inst_id=inst_id,
        bar=bar,
        output_path=output_path,
        start=start,
        max_pages=max_pages,
    )
    print("OKX public history download complete")
    print(f"instrument: {result.inst_id}")
    print(f"bar: {result.bar}")
    print(f"bars: {result.bar_count}")
    print(f"range: {result.first_datetime} -> {result.last_datetime} UTC")
    print(f"output: {result.output_path}")


def record_sample_bars(output: Path) -> None:
    gateway = MockGateway()
    gateway.connect()
    gateway.subscribe("RB2405")
    bars = [
        Bar(datetime=datetime(2026, 1, 2, 15, 0), symbol="RB2405", open=3500, high=3526, low=3488, close=3518, volume=124320, open_interest=820000),
        Bar(datetime=datetime(2026, 1, 5, 15, 0), symbol="RB2405", open=3520, high=3541, low=3506, close=3536, volume=132540, open_interest=823100),
    ]
    source = GatewaySnapshotSource(gateway, ["RB2405"])
    recorded: list[Bar] = []
    for bar in bars:
        gateway.push_bar(bar)
        recorded.extend(source.bars())
    recorder = CsvBarRecorder(output)
    recorder.write(recorded)
    print("Sample bars recorded")
    print(f"output: {output}")
    print(f"bar_count: {len(recorded)}")


def generate_demo_history(universe_path: Path, output_dir: Path) -> None:
    written = write_demo_history(universe_path, output_dir)
    print("Demo history generated")
    print(f"universe: {universe_path}")
    print(f"output_dir: {output_dir}")
    print(f"file_count: {len(written)}")


def generate_demo_intraday(universe_path: Path, output_dir: Path) -> None:
    written = write_demo_intraday_history(universe_path, output_dir)
    print("Synthetic 15-minute demo history generated")
    print("warning: engineering test data only; not real market performance")
    print(f"universe: {universe_path}")
    print(f"output_dir: {output_dir}")
    print(f"file_count: {len(written)}")


def fetch_history(
    universe_path: Path,
    output_dir: Path,
    provider_name: str,
    suffix: str,
    provider_config: Path | None = None,
) -> list[dict[str, object]]:
    universe = load_universe(universe_path)
    provider = build_provider(provider_name, provider_config)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for instrument in universe.instruments:
        try:
            bars = provider.fetch(instrument, universe.start, universe.end)
            output_path = output_dir / f"{instrument.symbol}{suffix}"
            CsvBarRecorder(output_path).write(bars)
            rows.append(
                {
                    "provider": provider_name,
                    "provider_config": str(provider_config) if provider_config else "",
                    "symbol": instrument.symbol,
                    "status": "ok",
                    "bars": len(bars),
                    "path": str(output_path),
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "provider": provider_name,
                    "provider_config": str(provider_config) if provider_config else "",
                    "symbol": instrument.symbol,
                    "status": "error",
                    "bars": 0,
                    "path": str(output_dir / f"{instrument.symbol}{suffix}"),
                    "error": str(exc),
                }
            )
    manifest_path = output_dir / "_fetch_manifest.csv"
    fieldnames = ["provider", "provider_config", "symbol", "status", "bars", "path", "error"]
    with manifest_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    write_chinese_companion(pd.DataFrame(rows), manifest_path)
    ok_count = sum(1 for row in rows if row["status"] == "ok")
    print("历史行情获取完成")
    print(f"数据来源：{provider_name}")
    print(f"成功品种数：{ok_count}")
    print(f"品种总数：{len(rows)}")
    print(f"获取清单：{manifest_path}")
    return rows


def batch_backtest(config_path: Path, project_root: Path, universe_path: Path, history_dir: Path, report_path: Path, suffix: str) -> None:
    universe = load_universe(universe_path)
    rows: list[dict[str, object]] = []
    for instrument in universe.instruments:
        data_path = history_dir / f"{instrument.symbol}{suffix}"
        symbol_report = report_path.parent / f"{instrument.symbol}_summary.csv"
        summary = execute_backtest(config_path, project_root, data_path, instrument.symbol, symbol_report)
        rows.append(
            {
                "symbol": instrument.symbol,
                "name": instrument.name,
                "group": instrument.group,
                "status": summary["status"],
                "start": summary["start"],
                "end": summary["end"],
                "final_equity": summary["final_equity"],
                "total_return": summary["total_return"],
                "annualized_return": summary["annualized_return"],
                "annualized_volatility": summary["annualized_volatility"],
                "periods_per_year": summary["periods_per_year"],
                "max_drawdown": summary["max_drawdown"],
                "sharpe": summary["sharpe"],
                "calmar": summary["calmar"],
                "trade_count": summary["trade_count"],
                "rejected_order_count": summary["rejected_order_count"],
                "data_path": str(data_path),
            }
        )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    write_chinese_companion(pd.DataFrame(rows), report_path)
    print("批量回测完成")
    print(f"品种数量：{len(rows)}")
    print(f"汇总文件：{report_path}")


def portfolio_backtest(
    config_path: Path,
    project_root: Path,
    universe_path: Path,
    history_dir: Path,
    output_dir: Path,
    suffix: str,
    symbols_csv: str | None,
    base_currency: str,
    symbol_currency: str | None,
    evaluation_start: str | None,
) -> None:
    cfg = load_backtest_config(config_path, project_root)
    if cfg.contracts.path is None:
        raise ValueError("portfolio-backtest requires configs.contracts.path.")
    universe = load_universe(universe_path)
    specs = load_contract_specs(cfg.contracts.path)
    universe_symbols = {instrument.symbol for instrument in universe.instruments}

    if symbols_csv:
        selected_symbols = [value.strip() for value in symbols_csv.split(",") if value.strip()]
    else:
        if base_currency.upper() != "CNY" or symbol_currency is not None:
            raise ValueError("Non-CNY portfolio-backtest requires an explicit --symbols list.")
        selected_symbols = [
            instrument.symbol
            for instrument in universe.instruments
            if instrument.symbol in specs
            and specs[instrument.symbol].exchange.strip().upper() in DOMESTIC_CNY_EXCHANGES
        ]
    if not selected_symbols:
        raise ValueError("portfolio-backtest selected no instruments.")
    unknown = sorted(set(selected_symbols) - universe_symbols)
    if unknown:
        raise ValueError(f"Symbols are not present in the universe: {unknown}")
    missing_specs = sorted(set(selected_symbols) - set(specs))
    if missing_specs:
        raise ValueError(f"Contract specs are missing for symbols: {missing_specs}")

    selected_specs = {symbol: specs[symbol] for symbol in selected_symbols}
    source_bars_by_symbol = {
        symbol: list(CsvBarSource(str(history_dir / f"{symbol}{suffix}")).bars())
        for symbol in selected_symbols
    }
    bars_by_symbol = {
        symbol: aggregate_bars(
            source_bars,
            target_minutes=cfg.data.bar_interval_minutes,
            source_minutes=cfg.data.source_interval_minutes,
        )
        for symbol, source_bars in source_bars_by_symbol.items()
    }
    # Each strategy receives a 1/sqrt(N) risk sleeve. This keeps independent
    # volatility targets from multiplying portfolio risk by the symbol count.
    strategy_risk_cash = cfg.initial_cash / math.sqrt(len(selected_symbols))
    strategies = {
        symbol: build_strategy(
            config=cfg.strategy,
            initial_cash=(
                cfg.initial_cash
                if cfg.strategy.name == "dual_ma_pullback"
                else strategy_risk_cash
            ),
            contract_multiplier=selected_specs[symbol].contract_multiplier,
            max_symbol_exposure=cfg.max_symbol_exposure,
            margin_rate=selected_specs[symbol].margin_rate,
            max_symbol_margin_usage=cfg.max_symbol_margin_usage,
            max_trade_risk=cfg.max_trade_risk,
        )
        for symbol in selected_symbols
    }
    currency_map = (
        {symbol: symbol_currency.upper() for symbol in selected_symbols}
        if symbol_currency
        else None
    )
    broker = SharedPortfolioBroker(
        initial_cash=cfg.initial_cash,
        contract_specs=selected_specs,
        risk_limits=PortfolioRiskLimits(
            max_margin_usage=cfg.max_margin_usage,
            max_symbol_exposure=cfg.max_symbol_exposure,
            daily_loss_stop=cfg.daily_loss_stop,
            max_symbol_margin_usage=cfg.max_symbol_margin_usage,
            max_open_positions=cfg.max_open_positions,
            max_trade_risk=cfg.max_trade_risk,
            max_group_positions=cfg.max_group_positions,
        ),
        slippage_ticks=cfg.slippage_ticks,
        base_currency=base_currency,
        symbol_currencies=currency_map,
        symbol_groups={
            instrument.symbol: instrument.group.split("-")[-1]
            for instrument in universe.instruments
            if instrument.symbol in selected_symbols
        },
    )
    result = run_portfolio_backtest(bars_by_symbol, strategies, broker)
    result.summary.update(
        {
            "source_interval_minutes": cfg.data.source_interval_minutes,
            "bar_interval_minutes": cfg.data.bar_interval_minutes,
            "source_bar_count": sum(map(len, source_bars_by_symbol.values())),
            "aggregated_bar_count": sum(map(len, bars_by_symbol.values())),
        }
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    save_summary(result.summary, str(output_dir / "summary.csv"))
    result.equity_curve.to_csv(output_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
    result.trades.to_csv(output_dir / "trades.csv", index=False, encoding="utf-8-sig")
    result.rejections.to_csv(output_dir / "rejections.csv", index=False, encoding="utf-8-sig")
    chinese_summary_frame(result.summary).to_csv(
        output_dir / "summary_中文.csv", index=False, encoding="utf-8-sig"
    )
    write_chinese_companion(result.equity_curve, output_dir / "equity_curve.csv")
    write_chinese_companion(result.trades, output_dir / "trades.csv")
    write_chinese_companion(result.rejections, output_dir / "rejections.csv")
    if evaluation_start:
        period_summary = summarize_portfolio_period(
            result,
            evaluation_start,
            initial_account_cash=cfg.initial_cash,
        )
        save_summary(period_summary, str(output_dir / "oos_summary.csv"))
        chinese_summary_frame(period_summary).to_csv(
            output_dir / "oos_summary_中文.csv", index=False, encoding="utf-8-sig"
        )
        print(f"样本外摘要：{output_dir / 'oos_summary_中文.csv'}")
    print("共享账户组合回测完成")
    print(f"品种：{','.join(selected_symbols)}")
    print(f"账户币种：{base_currency.upper()}")
    print(f"中文摘要：{output_dir / 'summary_中文.csv'}")
    _print_chinese_values(result.summary)


def analyze_batch(summary_path: Path, reports_dir: Path, output_dir: Path) -> None:
    outputs = analyze_batch_results(summary_path, reports_dir, output_dir)
    for path in outputs.values():
        _write_existing_csv_companion(path)
    print("批量分析完成")
    for name, path in outputs.items():
        print(f"{chinese_column_name(name)}：{path}")


def make_report(analysis_dir: Path, output_path: Path, title: str) -> None:
    path = generate_markdown_report(analysis_dir, output_path, title)
    print("报告生成完成")
    print(f"输出文件：{path}")


def optimize_adaptive_v2_timeframes(
    project_root: Path,
    universe_path: Path,
    history_dir: Path,
    output_dir: Path,
    suffix: str,
) -> dict[str, Path]:
    rows: list[dict[str, object]] = []
    for interval, base_name, search_name in [
        (15, "backtest_adaptive_research_15m.json", "optimization_adaptive_v2_staged_15m.json"),
        (60, "backtest_adaptive_research_60m.json", "optimization_adaptive_v2_staged_60m.json"),
    ]:
        interval_output = output_dir / f"{interval}m"
        paths = optimize_strategy(
            project_root / "configs" / base_name,
            project_root / "configs" / search_name,
            universe_path,
            history_dir,
            interval_output,
            suffix=suffix,
            project_root=project_root,
        )
        selected = json.loads(paths["selected_parameters"].read_text(encoding="utf-8"))
        comparison = pd.read_csv(paths["strategy_comparison"])
        selected_oos = comparison[comparison["scenario"] == "selected"].iloc[0]
        promotion = json.loads(paths["promotion_decision"].read_text(encoding="utf-8"))
        rows.append(
            {
                "bar_interval_minutes": interval,
                "selection_score": selected["selection_score"],
                "train_score": selected["train_score"],
                "validation_score": selected["validation_score"],
                "oos_total_return": selected_oos["total_return"],
                "oos_annualized_return": selected_oos["annualized_return"],
                "oos_max_drawdown": selected_oos["max_drawdown"],
                "oos_sharpe": selected_oos["sharpe"],
                "oos_positive_instrument_ratio": selected_oos.get(
                    "positive_instrument_ratio", 0.0
                ),
                "research_gates_passed": promotion["all_gates_passed"],
                "output_dir": str(interval_output),
            }
        )
    comparison_frame = pd.DataFrame(rows).sort_values(
        "selection_score", ascending=False
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    comparison_path = output_dir / "timeframe_comparison.csv"
    comparison_frame.to_csv(comparison_path, index=False, encoding="utf-8-sig")
    write_chinese_companion(comparison_frame, comparison_path)
    winner = comparison_frame.iloc[0]
    selection_path = output_dir / "timeframe_selection.json"
    selection_path.write_text(
        json.dumps(
            {
                "selected_by": "train_validation_selection_score_only",
                "selected_bar_interval_minutes": int(
                    winner["bar_interval_minutes"]
                ),
                "final_test_used_for_timeframe_selection": False,
                "automatic_default_update": False,
                "research_data_label": "synthetic_engineering",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "timeframe_comparison": comparison_path,
        "timeframe_selection": selection_path,
    }


def validate_history(history_dir: Path, output_path: Path, suffix: str) -> None:
    results = validate_history_dir(history_dir, output_path, suffix)
    _write_existing_csv_companion(output_path)
    warning_count = sum(1 for result in results if result.status != "ok")
    print("历史行情校验完成")
    print(f"行情目录：{history_dir}")
    print(f"文件数量：{len(results)}")
    print(f"警告数量：{warning_count}")
    print(f"校验报告：{output_path}")


def run_pipeline(
    project_root: Path,
    provider: str,
    provider_config: Path | None,
    universe_path: Path,
    history_dir: Path,
    suffix: str,
    config_path: Path,
    quality_report_path: Path,
    batch_summary_path: Path,
    analysis_dir: Path,
    report_path: Path,
    report_title: str,
    allow_warnings: bool,
) -> None:
    print("[1/5] 获取历史行情")
    fetch_rows = fetch_history(universe_path, history_dir, provider, suffix, provider_config)
    fetch_error_count = sum(1 for row in fetch_rows if row["status"] != "ok")
    if fetch_error_count:
        raise SystemExit(
            f"History fetch failed for {fetch_error_count} instrument(s). "
            f"See {history_dir / '_fetch_manifest.csv'} for details."
        )

    print("[2/5] 校验历史行情")
    quality_results = validate_history_dir(history_dir, quality_report_path, suffix)
    warning_count = sum(1 for result in quality_results if result.status != "ok")
    _write_existing_csv_companion(quality_report_path)
    print(f"质量报告：{quality_report_path}")
    print(f"警告数量：{warning_count}")
    if warning_count and not allow_warnings:
        raise SystemExit("History validation found warnings/errors. Re-run with --allow-warnings to continue anyway.")

    print("[3/5] 批量回测")
    batch_backtest(config_path, project_root, universe_path, history_dir, batch_summary_path, suffix)

    print("[4/5] 分析批量结果")
    analyze_batch(batch_summary_path, batch_summary_path.parent, analysis_dir)

    print("[5/5] 生成报告")
    make_report(analysis_dir, report_path, report_title)
    print("完整流程执行完成")


def main() -> None:
    parser = argparse.ArgumentParser(description="Domestic futures quant research toolkit")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("dashboard", help="Launch the desktop strategy/backtest workbench")
    bt = sub.add_parser("backtest", help="Run a CSV-based backtest")
    bt.add_argument("--config", default="configs/backtest.json", help="Backtest config path")
    bt.add_argument("--data-path", default=None, help="Override CSV data path")
    bt.add_argument("--symbol", default=None, help="Override contract symbol")
    bt.add_argument("--report-path", default=None, help="Override summary report path")
    sub.add_parser("gateway-smoke", help="Validate the trading gateway boundary with the mock gateway")
    ctp_diag = sub.add_parser(
        "ctp-diagnose",
        help="Safely inspect CTP configuration/SDK readiness without reading credentials or connecting",
    )
    ctp_diag.add_argument(
        "--config",
        default="configs/api.changjiang.example.json",
        help="CTP JSON configuration template path",
    )
    okx_diag = sub.add_parser(
        "okx-diagnose",
        help="Inspect OKX config offline, or explicitly run read-only subaccount checks",
    )
    okx_diag.add_argument(
        "--config",
        default="configs/api.okx.example.json",
        help="OKX JSON configuration path",
    )
    okx_diag.add_argument(
        "--connect-read-only",
        action="store_true",
        help="Read credential environment variables and query time/config/balance/positions; never orders",
    )
    okx_history = sub.add_parser(
        "okx-download-history",
        help="Download confirmed public OKX candles to the standard CSV format",
    )
    okx_history.add_argument("--inst-id", default="BTC-USDT-SWAP")
    okx_history.add_argument("--bar", default="15m")
    okx_history.add_argument(
        "--start",
        default=(datetime.now(timezone.utc) - timedelta(days=245)).date().isoformat(),
        help="UTC start date/time, ISO-8601",
    )
    okx_history.add_argument(
        "--output",
        default="data/okx_real_15m/BTC-USDT-SWAP_15m.csv",
    )
    okx_history.add_argument("--max-pages", type=int, default=200)
    rec = sub.add_parser("record-sample-bars", help="Write normalized gateway bars to a CSV file")
    rec.add_argument("--output", default="data/recorded/RB2405_gateway_sample.csv", help="Output CSV path")
    replay = sub.add_parser("replay-gateway-csv", help="Replay a CSV through the gateway boundary and record normalized bars")
    replay.add_argument("--input", default="data/sample/RB2405_1d.csv", help="Input CSV path")
    replay.add_argument("--output", default="data/recorded/RB2405_gateway_replay.csv", help="Output CSV path")
    replay.add_argument("--symbol", default="RB2405", help="Symbol to replay")
    pobo = sub.add_parser(
        "import-pobo-his",
        help="Convert a Pobo 5-minute .his cache to normalized session-safe CSV",
    )
    pobo.add_argument("--input", required=True, help="Pobo 5Min .his file")
    pobo.add_argument(
        "--name-table",
        default=None,
        help="NameTable.xml path; defaults to the .his market directory",
    )
    pobo.add_argument("--output", required=True, help="Normalized CSV output path")
    pobo.add_argument(
        "--symbol",
        default=None,
        help="Override output symbol; defaults to BRCode from NameTable.xml",
    )
    pobo.add_argument(
        "--target-minutes",
        type=int,
        default=15,
        help="Output interval in minutes; must be a multiple of 5",
    )
    pobo_batch = sub.add_parser(
        "import-pobo-batch",
        help="Scan a Pobo Data directory, export mapped 15m series, and write an audit manifest",
    )
    pobo_batch.add_argument(
        "--data-root",
        required=True,
        help="Pobo Data directory containing market/5Min/*.his caches",
    )
    pobo_batch.add_argument(
        "--output-dir",
        default="data/pobo_real_15m",
        help="Directory for normalized SYMBOL_15m.csv files",
    )
    pobo_batch.add_argument(
        "--manifest",
        default="reports/pobo_import_manifest.csv",
        help="CSV provenance and quality manifest",
    )
    pobo_batch.add_argument(
        "--contracts",
        default="configs/contracts.csv",
        help="Project contract specifications; exported symbols must exist here",
    )
    pobo_batch.add_argument(
        "--universe",
        default="configs/universe_domestic_3y.json",
        help="Project universe; exported symbols must exist here",
    )
    pobo_batch.add_argument(
        "--minimum-coverage-days",
        type=float,
        default=365.25 * 3,
        help="Coverage threshold used to flag histories shorter than three years",
    )
    pobo_batch.add_argument(
        "--symbols",
        default=None,
        help="Optional comma-separated project symbols, for example RB0,CU0",
    )
    pobo_batch.add_argument(
        "--series-preference",
        default="ZL,LX,ZS",
        help="Selection order when main/continuous/weighted caches overlap",
    )
    demo = sub.add_parser("generate-demo-history", help="Generate deterministic demo histories for a multi-asset universe")
    demo.add_argument("--universe", default="configs/universe.json", help="Universe config path")
    demo.add_argument("--output-dir", default="data/history", help="Output directory")
    demo_15m = sub.add_parser(
        "generate-demo-intraday",
        help="Generate deterministic 15-minute engineering-test data",
    )
    demo_15m.add_argument("--universe", default="configs/universe_domestic_3y.json", help="Universe config path")
    demo_15m.add_argument("--output-dir", default="data/domestic_15m", help="Output directory")
    fetch = sub.add_parser("fetch-history", help="Fetch history through a provider and save normalized CSV files")
    fetch.add_argument("--provider", default="synthetic", choices=["synthetic", "akshare", "binance", "http"], help="History provider")
    fetch.add_argument("--provider-config", default=None, help="Provider config path (required for provider=http)")
    fetch.add_argument("--universe", default="configs/universe.json", help="Universe config path")
    fetch.add_argument("--output-dir", default="data/history_api", help="Output directory")
    fetch.add_argument("--suffix", default="_1d.csv", help="Output file suffix")
    validate = sub.add_parser("validate-history", help="Validate normalized historical bar CSV files")
    validate.add_argument("--history-dir", default="data/history_api", help="History CSV directory")
    validate.add_argument("--suffix", default="_1d.csv", help="History file suffix")
    validate.add_argument("--output", default="reports/data_quality_report.csv", help="Validation report CSV")
    batch = sub.add_parser("batch-backtest", help="Run backtests for every instrument in a universe")
    batch.add_argument("--config", default="configs/backtest.json", help="Base backtest config path")
    batch.add_argument("--universe", default="configs/universe.json", help="Universe config path")
    batch.add_argument("--history-dir", default="data/history", help="History CSV directory")
    batch.add_argument("--report-path", default="reports/multi_asset_summary.csv", help="Aggregate report path")
    batch.add_argument("--suffix", default="_1d_demo.csv", help="History file suffix")
    portfolio = sub.add_parser(
        "portfolio-backtest",
        help="Run a synchronized multi-instrument backtest on one shared account",
    )
    portfolio.add_argument("--config", default="reports/optimization/selected_backtest_config.json", help="Backtest/strategy config path")
    portfolio.add_argument("--universe", default="configs/universe.json", help="Universe config path")
    portfolio.add_argument("--history-dir", default="data/history_api", help="History CSV directory")
    portfolio.add_argument("--suffix", default="_1d.csv", help="History file suffix")
    portfolio.add_argument("--output-dir", default="reports/shared_portfolio", help="Output directory")
    portfolio.add_argument("--symbols", default=None, help="Comma-separated symbols; default selects CNY domestic exchanges")
    portfolio.add_argument("--base-currency", default="CNY", help="Single account currency")
    portfolio.add_argument("--symbol-currency", default=None, help="Currency shared by every explicitly selected symbol; FX conversion is not implemented")
    portfolio.add_argument("--evaluation-start", default=None, help="Optional sealed-period start; keeps warmed-up positions and writes oos_summary.csv")
    analysis = sub.add_parser("analyze-batch", help="Analyze a batch backtest summary and equity curves")
    analysis.add_argument("--summary-path", default="reports/multi_asset_api_summary.csv", help="Batch summary CSV")
    analysis.add_argument("--reports-dir", default="reports", help="Directory with per-instrument equity curves")
    analysis.add_argument("--output-dir", default="reports/analysis", help="Output directory")
    report = sub.add_parser("make-report", help="Generate a Markdown report from analysis outputs")
    report.add_argument("--analysis-dir", default="reports/analysis", help="Analysis output directory")
    report.add_argument("--output", default="reports/backtest_report.md", help="Markdown report path")
    report.add_argument("--title", default="多品种期货量化回测报告", help="Report title")
    pipeline = sub.add_parser("run-pipeline", help="Fetch, validate, backtest, analyze, and generate a report")
    pipeline.add_argument("--provider", default="synthetic", choices=["synthetic", "akshare", "binance", "http"], help="History provider")
    pipeline.add_argument("--provider-config", default=None, help="Provider config path (required for provider=http)")
    pipeline.add_argument("--universe", default="configs/universe.json", help="Universe config path")
    pipeline.add_argument("--history-dir", default="data/history_api", help="History CSV directory")
    pipeline.add_argument("--suffix", default="_1d.csv", help="History file suffix")
    pipeline.add_argument("--config", default="configs/backtest_multi.json", help="Backtest config path")
    pipeline.add_argument("--quality-report", default="reports/data_quality_report.csv", help="Data quality report path")
    pipeline.add_argument("--batch-summary", default="reports/multi_asset_api_summary.csv", help="Batch summary path")
    pipeline.add_argument("--analysis-dir", default="reports/analysis", help="Analysis output directory")
    pipeline.add_argument("--report", default="reports/backtest_report.md", help="Markdown report path")
    pipeline.add_argument("--title", default="多品种期货量化回测报告", help="Report title")
    pipeline.add_argument("--allow-warnings", action="store_true", help="Continue even if history validation finds warnings/errors")
    optimize = sub.add_parser(
        "optimize-strategy",
        help="Select global strategy parameters on train/validation and report untouched final-test results",
    )
    optimize.add_argument(
        "--config",
        default="configs/backtest_multi.json",
        help="Base backtest and baseline strategy config",
    )
    optimize.add_argument(
        "--optimization-config",
        default="configs/optimization_adaptive.json",
        help="Parameter grid, chronological split, objective, and cost sensitivity config",
    )
    optimize.add_argument("--universe", default="configs/universe.json", help="Universe config path")
    optimize.add_argument("--history-dir", default="data/history_api", help="History CSV directory")
    optimize.add_argument("--suffix", default="_1d.csv", help="History file suffix")
    optimize.add_argument(
        "--output-dir",
        default="reports/optimization",
        help="Candidate ranking and out-of-sample output directory",
    )
    enhanced = sub.add_parser(
        "optimize-adaptive-v2",
        help="Run sealed two-stage enhanced-adaptive research for 15m and 60m",
    )
    enhanced.add_argument(
        "--universe", default="configs/universe_domestic_3y.json"
    )
    enhanced.add_argument("--history-dir", default="data/domestic_15m")
    enhanced.add_argument("--suffix", default="_15m.csv")
    enhanced.add_argument(
        "--output-dir", default="reports/optimization_adaptive_v2"
    )
    args = parser.parse_args()

    project_root = Path.cwd()
    if args.command == "dashboard":
        from futures_quant.dashboard import launch_dashboard

        launch_dashboard(project_root)
    elif args.command == "backtest":
        run(
            project_root / args.config,
            project_root,
            data_path=project_root / args.data_path if args.data_path else None,
            symbol=args.symbol,
            report_path=project_root / args.report_path if args.report_path else None,
        )
    elif args.command == "gateway-smoke":
        gateway_smoke()
    elif args.command == "ctp-diagnose":
        ctp_diagnose(project_root / args.config)
    elif args.command == "okx-diagnose":
        okx_diagnose(
            project_root / args.config,
            connect_read_only=args.connect_read_only,
        )
    elif args.command == "okx-download-history":
        okx_download_history(
            args.inst_id,
            args.bar,
            args.start,
            project_root / args.output,
            args.max_pages,
        )
    elif args.command == "record-sample-bars":
        record_sample_bars(project_root / args.output)
    elif args.command == "replay-gateway-csv":
        replay_gateway_csv(project_root / args.input, project_root / args.output, args.symbol)
    elif args.command == "import-pobo-his":
        import_pobo_history(
            project_root / args.input,
            project_root / args.output,
            project_root / args.name_table if args.name_table else None,
            args.symbol,
            args.target_minutes,
        )
    elif args.command == "import-pobo-batch":
        import_pobo_batch(
            project_root / args.data_root,
            project_root / args.output_dir,
            project_root / args.manifest,
            project_root / args.contracts,
            project_root / args.universe,
            args.minimum_coverage_days,
            args.symbols,
            args.series_preference,
        )
    elif args.command == "generate-demo-history":
        generate_demo_history(project_root / args.universe, project_root / args.output_dir)
    elif args.command == "generate-demo-intraday":
        generate_demo_intraday(project_root / args.universe, project_root / args.output_dir)
    elif args.command == "fetch-history":
        fetch_history(
            project_root / args.universe,
            project_root / args.output_dir,
            args.provider,
            args.suffix,
            project_root / args.provider_config if args.provider_config else None,
        )
    elif args.command == "validate-history":
        validate_history(project_root / args.history_dir, project_root / args.output, args.suffix)
    elif args.command == "batch-backtest":
        batch_backtest(
            project_root / args.config,
            project_root,
            project_root / args.universe,
            project_root / args.history_dir,
            project_root / args.report_path,
            args.suffix,
        )
    elif args.command == "portfolio-backtest":
        portfolio_backtest(
            project_root / args.config,
            project_root,
            project_root / args.universe,
            project_root / args.history_dir,
            project_root / args.output_dir,
            args.suffix,
            args.symbols,
            args.base_currency,
            args.symbol_currency,
            args.evaluation_start,
        )
    elif args.command == "analyze-batch":
        analyze_batch(project_root / args.summary_path, project_root / args.reports_dir, project_root / args.output_dir)
    elif args.command == "make-report":
        make_report(project_root / args.analysis_dir, project_root / args.output, args.title)
    elif args.command == "run-pipeline":
        run_pipeline(
            project_root,
            args.provider,
            project_root / args.provider_config if args.provider_config else None,
            project_root / args.universe,
            project_root / args.history_dir,
            args.suffix,
            project_root / args.config,
            project_root / args.quality_report,
            project_root / args.batch_summary,
            project_root / args.analysis_dir,
            project_root / args.report,
            args.title,
            args.allow_warnings,
        )
    elif args.command == "optimize-strategy":
        outputs = optimize_strategy(
            project_root / args.config,
            project_root / args.optimization_config,
            project_root / args.universe,
            project_root / args.history_dir,
            project_root / args.output_dir,
            suffix=args.suffix,
            project_root=project_root,
        )
        for path in outputs.values():
            _write_existing_csv_companion(path)
        print("策略参数优化完成")
        print("选择规则：仅使用训练集和验证集；最终测试集不参与选参")
        for name, path in outputs.items():
            print(f"{chinese_column_name(name)}：{path}")
    elif args.command == "optimize-adaptive-v2":
        outputs = optimize_adaptive_v2_timeframes(
            project_root,
            project_root / args.universe,
            project_root / args.history_dir,
            project_root / args.output_dir,
            args.suffix,
        )
        print("强化自适应15/60分钟两阶段优化完成")
        print("周期选择仅使用训练和验证得分；最终测试不参与周期选择")
        for name, path in outputs.items():
            print(f"{chinese_column_name(name)}：{path}")


if __name__ == "__main__":
    main()
