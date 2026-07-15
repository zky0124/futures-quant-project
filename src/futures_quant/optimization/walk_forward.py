from __future__ import annotations

import inspect
import itertools
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from futures_quant.broker.backtest import BacktestBroker, BacktestResult, run_backtest
from futures_quant.data.contracts import ContractSpec, load_contract_specs
from futures_quant.data.csv_loader import load_bars
from futures_quant.data.history import load_universe
from futures_quant.data.timeframe import aggregate_bars
from futures_quant.models import Bar
from futures_quant.risk.rules import RiskEngine, RiskLimits
from futures_quant.strategies.adaptive_trend import AdaptiveTrendStrategy
from futures_quant.strategies.adaptive_trend_v2 import EnhancedAdaptiveTrendStrategy
from futures_quant.strategies.base import Strategy
from futures_quant.strategies.dual_ma import DualMovingAverageStrategy
from futures_quant.strategies.dual_period_reversal import DualPeriodReversalStrategy
from futures_quant.strategies.ma_trend_pullback import TrendPullbackMovingAverageStrategy


STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "dual_ma": DualMovingAverageStrategy,
    "adaptive_trend": AdaptiveTrendStrategy,
    "adaptive_trend_v2": EnhancedAdaptiveTrendStrategy,
    "dual_period_reversal": DualPeriodReversalStrategy,
    "dual_ma_pullback": TrendPullbackMovingAverageStrategy,
}


@dataclass(frozen=True)
class BacktestSettings:
    initial_cash: float
    commission_rate: float
    slippage_ticks: int
    tick_size: float
    contract_multiplier: int
    margin_rate: float
    max_margin_usage: float
    max_symbol_exposure: float
    max_symbol_margin_usage: float
    max_trade_risk: float
    daily_loss_stop: float
    baseline_strategy_name: str
    baseline_parameters: dict[str, Any]
    contracts_path: Path | None
    source_interval_minutes: int
    bar_interval_minutes: int


@dataclass(frozen=True)
class SearchSettings:
    strategy_name: str
    parameter_grid: dict[str, list[Any]]
    train_fraction: float
    validation_fraction: float
    train_end: pd.Timestamp | None
    validation_end: pd.Timestamp | None
    min_bars_per_phase: int
    objective: dict[str, Any]
    commission_multipliers: list[float]
    sensitivity_slippage_ticks: list[int]


@dataclass(frozen=True)
class TimeSplit:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    validation_start: pd.Timestamp
    validation_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def _rank_parameter_candidates(
    candidates: list[dict[str, Any]],
    *,
    base_parameters: dict[str, Any],
    stage: str,
    bars_by_symbol: dict[str, list[Bar]],
    search: SearchSettings,
    settings: BacktestSettings,
    contracts: dict[str, ContractSpec],
    split: TimeSplit,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for candidate_number, overrides in enumerate(candidates, start=1):
        parameters = {**base_parameters, **overrides}
        candidate_id = f"{stage}_candidate_{candidate_number:04d}"
        row: dict[str, Any] = {
            "stage": stage,
            "candidate_id": candidate_id,
            "strategy_name": search.strategy_name,
            "parameters": _canonical_json(parameters),
        }
        row.update({f"param_{key}": value for key, value in sorted(parameters.items())})
        try:
            results = _run_universe(
                bars_by_symbol,
                search.strategy_name,
                parameters,
                settings,
                contracts,
                run_end=split.validation_end,
            )
            train = _summarize_universe_phase(
                results, split.train_start, split.train_end, settings.initial_cash
            )
            validation = _summarize_universe_phase(
                results,
                split.validation_start,
                split.validation_end,
                settings.initial_cash,
            )
            train_score = _objective_score(
                train["portfolio"], search.objective, train["instruments"]
            )
            validation_score = _objective_score(
                validation["portfolio"], search.objective, validation["instruments"]
            )
            selection_score = _combine_phase_scores(
                train_score, validation_score, search.objective
            )
            minimum_trades = int(search.objective.get("min_trades_per_instrument", 0))
            row.update(
                {
                    "status": "ok",
                    "selection_eligible": selection_score > -1.0e29,
                    "validation_instrument_count": len(validation["instruments"]),
                    "validation_instruments_meeting_min_trades": sum(
                        int(metrics["trade_count"]) >= minimum_trades
                        for metrics in validation["instruments"].values()
                    ),
                    "selection_score": selection_score,
                    "train_score": train_score,
                    "validation_score": validation_score,
                    "phase_score_gap": abs(train_score - validation_score),
                    "selection_method": search.objective.get(
                        "selection_method", "min_train_validation"
                    ),
                    **_prefixed_metrics("train", train["portfolio"]),
                    **_prefixed_metrics("validation", validation["portfolio"]),
                    "error": "",
                }
            )
        except Exception as exc:
            row.update(
                {
                    "status": "invalid",
                    "selection_eligible": False,
                    "selection_score": -1.0e30,
                    "train_score": -1.0e30,
                    "validation_score": -1.0e30,
                    "phase_score_gap": 0.0,
                    "selection_method": search.objective.get(
                        "selection_method", "min_train_validation"
                    ),
                    "error": str(exc),
                }
            )
        rows.append(row)
    ranking = pd.DataFrame(rows).sort_values(
        ["selection_score", "validation_score", "train_score", "candidate_id"],
        ascending=[False, False, False, True],
        kind="stable",
    ).reset_index(drop=True)
    ranking.insert(0, "rank", range(1, len(ranking) + 1))
    return ranking


def _ranking_winner(ranking: pd.DataFrame) -> pd.Series:
    eligible = ranking[
        (ranking["status"] == "ok")
        & (ranking["selection_eligible"] == True)  # noqa: E712
    ]
    if eligible.empty:
        errors = "; ".join(str(value) for value in ranking["error"].dropna().head(3))
        raise ValueError(f"All parameter candidates were invalid. First errors: {errors}")
    return eligible.iloc[0]


def optimize_strategy(
    backtest_config_path: str | Path,
    optimization_config_path: str | Path,
    universe_path: str | Path,
    history_dir: str | Path,
    output_dir: str | Path,
    suffix: str = "_1d.csv",
    project_root: str | Path | None = None,
) -> dict[str, Path]:
    """Select one parameter set across a universe without touching test data.

    Every candidate is run only through ``validation_end``.  The final test
    period is first accessed after ranking and parameter selection; later
    baseline and cost scenarios are reporting-only and cannot feed back into
    the winner.  This ordering is intentionally explicit to make accidental
    test leakage difficult during future extensions.
    """

    backtest_config_path = Path(backtest_config_path)
    optimization_config_path = Path(optimization_config_path)
    universe_path = Path(universe_path)
    history_dir = Path(history_dir)
    output_dir = Path(output_dir)
    root = Path(project_root) if project_root is not None else backtest_config_path.parent.parent

    settings = _load_backtest_settings(backtest_config_path, root)
    base_backtest_document = json.loads(backtest_config_path.read_text(encoding="utf-8"))
    optimization_document = json.loads(
        optimization_config_path.read_text(encoding="utf-8")
    )
    search = _load_search_settings(optimization_config_path)
    universe = load_universe(universe_path)
    bars_by_symbol = {
        instrument.symbol: aggregate_bars(
            load_bars(history_dir / f"{instrument.symbol}{suffix}"),
            target_minutes=settings.bar_interval_minutes,
            source_minutes=settings.source_interval_minutes,
        )
        for instrument in universe.instruments
    }
    if not bars_by_symbol:
        raise ValueError("The optimization universe is empty.")
    split = _make_time_split(bars_by_symbol, search)
    _validate_phase_coverage(bars_by_symbol, split, search.min_bars_per_phase)

    contracts = (
        load_contract_specs(settings.contracts_path)
        if settings.contracts_path is not None
        else {}
    )
    strategy_search = optimization_document.get("strategy", optimization_document)
    stages = strategy_search.get("stages", [])
    stage_rankings: dict[str, pd.DataFrame] = {}
    if stages:
        if not isinstance(stages, list) or len(stages) != 2:
            raise ValueError("Staged optimization requires exactly two stages.")
        inherited: dict[str, Any] = dict(strategy_search.get("fixed_parameters", {}))
        for stage_number, stage_document in enumerate(stages, start=1):
            grid = stage_document.get("parameter_grid", {})
            if not isinstance(grid, dict) or not grid:
                raise ValueError(f"stage {stage_number} requires parameter_grid.")
            candidates = _expand_parameter_grid(
                {str(key): list(values) for key, values in grid.items()}
            )
            stage_name = f"stage{stage_number}"
            stage_ranking = _rank_parameter_candidates(
                candidates,
                base_parameters=inherited,
                stage=stage_name,
                bars_by_symbol=bars_by_symbol,
                search=search,
                settings=settings,
                contracts=contracts,
                split=split,
            )
            stage_rankings[stage_name] = stage_ranking
            stage_winner = _ranking_winner(stage_ranking)
            inherited = json.loads(str(stage_winner["parameters"]))
        ranking = stage_rankings["stage2"]
        winner = _ranking_winner(ranking)
    else:
        candidates = _expand_parameter_grid(search.parameter_grid)
        if not candidates:
            raise ValueError("parameter_grid did not produce any candidates.")
        ranking = _rank_parameter_candidates(
            candidates,
            base_parameters={},
            stage="single",
            bars_by_symbol=bars_by_symbol,
            search=search,
            settings=settings,
            contracts=contracts,
            split=split,
        )
        winner = _ranking_winner(ranking)
    selected_parameters = json.loads(str(winner["parameters"]))

    # Final test data is first used here, after selected_parameters is frozen.
    selected_results = _run_universe(
        bars_by_symbol,
        search.strategy_name,
        selected_parameters,
        settings,
        contracts,
        run_end=split.test_end,
    )
    baseline_results = _run_universe(
        bars_by_symbol,
        settings.baseline_strategy_name,
        settings.baseline_parameters,
        settings,
        contracts,
        run_end=split.test_end,
    )
    selected_oos = _summarize_universe_phase(
        selected_results, split.test_start, split.test_end, settings.initial_cash
    )
    baseline_oos = _summarize_universe_phase(
        baseline_results, split.test_start, split.test_end, settings.initial_cash
    )

    instrument_rows = _comparison_instrument_rows(
        selected_oos,
        baseline_oos,
        search.strategy_name,
        settings.baseline_strategy_name,
        selected_parameters,
        settings.baseline_parameters,
    )
    portfolio_rows = _comparison_portfolio_rows(
        selected_oos,
        baseline_oos,
        search.strategy_name,
        settings.baseline_strategy_name,
        selected_parameters,
        settings.baseline_parameters,
    )
    comparison_rows = list(portfolio_rows)
    for comparison in optimization_document.get("comparisons", []):
        comparison_name = str(comparison["name"])
        comparison_parameters = dict(comparison.get("parameters", {}))
        if comparison_name == settings.baseline_strategy_name:
            continue
        comparison_results = _run_universe(
            bars_by_symbol,
            comparison_name,
            comparison_parameters,
            settings,
            contracts,
            run_end=split.test_end,
        )
        comparison_oos = _summarize_universe_phase(
            comparison_results, split.test_start, split.test_end, settings.initial_cash
        )
        comparison_rows.append(
            {
                "scenario": f"comparison_{comparison_name}",
                "strategy_name": comparison_name,
                "parameters": _canonical_json(comparison_parameters),
                "used_for_selection": False,
                **comparison_oos["portfolio"],
            }
        )
    sensitivity_rows = _cost_sensitivity(
        bars_by_symbol,
        search,
        selected_parameters,
        settings,
        contracts,
        split,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    ranking_path = output_dir / "candidate_ranking.csv"
    selected_path = output_dir / "selected_parameters.json"
    instrument_path = output_dir / "oos_instrument_comparison.csv"
    portfolio_path = output_dir / "oos_portfolio_comparison.csv"
    sensitivity_path = output_dir / "cost_sensitivity.csv"
    split_path = output_dir / "split_manifest.csv"
    equity_path = output_dir / "oos_portfolio_equity.csv"
    report_path = output_dir / "optimization_report.md"
    selected_backtest_config_path = output_dir / "selected_backtest_config.json"
    comparison_path = output_dir / "strategy_comparison.csv"
    promotion_path = output_dir / "promotion_decision.json"
    stage1_path = output_dir / "stage1_structure_ranking.csv"
    stage2_path = output_dir / "stage2_risk_ranking.csv"

    ranking.to_csv(ranking_path, index=False, encoding="utf-8-sig")
    if "stage1" in stage_rankings:
        stage_rankings["stage1"].to_csv(
            stage1_path, index=False, encoding="utf-8-sig"
        )
        stage_rankings["stage2"].to_csv(
            stage2_path, index=False, encoding="utf-8-sig"
        )
    pd.DataFrame(instrument_rows).to_csv(instrument_path, index=False, encoding="utf-8-sig")
    pd.DataFrame(portfolio_rows).to_csv(portfolio_path, index=False, encoding="utf-8-sig")
    pd.DataFrame(comparison_rows).to_csv(
        comparison_path, index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(sensitivity_rows).to_csv(sensitivity_path, index=False, encoding="utf-8-sig")
    _write_portfolio_equity(selected_oos, baseline_oos, equity_path)
    _write_split_manifest(split, bars_by_symbol, split_path)

    selected_document = {
        "strategy_name": search.strategy_name,
        "candidate_id": str(winner["candidate_id"]),
        "parameters": selected_parameters,
        "selection_score": float(winner["selection_score"]),
        "train_score": float(winner["train_score"]),
        "validation_score": float(winner["validation_score"]),
        "phase_score_gap": float(winner["phase_score_gap"]),
        "selection_method": str(winner["selection_method"]),
        "objective": search.objective,
        "selection_used_periods": ["train", "validation"],
        "selection_data_end": split.validation_end.date().isoformat(),
        "final_test_used_for_selection": False,
        "final_test_start": split.test_start.date().isoformat(),
        "final_test_end": split.test_end.date().isoformat(),
        "universe_symbols": sorted(bars_by_symbol),
        "staged_optimization": bool(stage_rankings),
        "research_data_label": optimization_document.get(
            "research_data_label", "unspecified"
        ),
    }
    if "stage1" in stage_rankings:
        stage1_winner = _ranking_winner(stage_rankings["stage1"])
        selected_document["stage1_parameters"] = json.loads(
            str(stage1_winner["parameters"])
        )
    selected_path.write_text(
        json.dumps(selected_document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    selected_backtest_document = dict(base_backtest_document)
    selected_backtest_document["strategy"] = {
        "name": search.strategy_name,
        **selected_parameters,
    }
    selected_backtest_config_path.write_text(
        json.dumps(selected_backtest_document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_markdown_report(
        report_path,
        selected_document,
        split,
        pd.DataFrame(portfolio_rows),
        pd.DataFrame(sensitivity_rows),
    )
    baseline_validation = _summarize_universe_phase(
        baseline_results,
        split.validation_start,
        split.validation_end,
        settings.initial_cash,
    )
    baseline_validation_score = _objective_score(
        baseline_validation["portfolio"],
        search.objective,
        baseline_validation["instruments"],
    )
    required_validation_score = baseline_validation_score + 0.05 * abs(
        baseline_validation_score
    )
    selected_test_drawdown = abs(float(selected_oos["portfolio"]["max_drawdown"]))
    baseline_test_drawdown = abs(float(baseline_oos["portfolio"]["max_drawdown"]))
    stressed = [
        row
        for row in sensitivity_rows
        if float(row["commission_multiplier"]) >= 2.0
        and int(row["slippage_ticks"]) >= 3
    ]
    stress_score = min(
        (float(row.get("objective_score", -1.0e30)) for row in stressed),
        default=-1.0e30,
    )
    gates = {
        "validation_improvement_5pct": float(winner["validation_score"])
        >= required_validation_score,
        "oos_drawdown_not_worse_than_10pct": selected_test_drawdown
        <= baseline_test_drawdown * 1.10,
        "double_cost_three_tick_score_positive": stress_score > 0,
    }
    promotion_document = {
        "candidate_strategy": search.strategy_name,
        "baseline_strategy": settings.baseline_strategy_name,
        "research_data_label": selected_document["research_data_label"],
        "gates": gates,
        "all_gates_passed": all(gates.values()),
        "automatic_default_update": False,
        "decision": (
            "research_candidate_passed"
            if all(gates.values())
            else "keep_existing_default"
        ),
    }
    promotion_path.write_text(
        json.dumps(promotion_document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    outputs = {
        "candidate_ranking": ranking_path,
        "selected_parameters": selected_path,
        "selected_backtest_config": selected_backtest_config_path,
        "oos_instrument_comparison": instrument_path,
        "oos_portfolio_comparison": portfolio_path,
        "cost_sensitivity": sensitivity_path,
        "split_manifest": split_path,
        "oos_portfolio_equity": equity_path,
        "optimization_report": report_path,
        "strategy_comparison": comparison_path,
        "promotion_decision": promotion_path,
    }
    if "stage1" in stage_rankings:
        outputs["stage1_structure_ranking"] = stage1_path
        outputs["stage2_risk_ranking"] = stage2_path
    return outputs


def _load_backtest_settings(path: Path, root: Path) -> BacktestSettings:
    raw = json.loads(path.read_text(encoding="utf-8"))
    strategy = dict(raw["strategy"])
    strategy_name = str(strategy.pop("name"))
    contracts_value = raw.get("contracts", {}).get("path")
    data = raw.get("data", {})
    return BacktestSettings(
        initial_cash=float(raw["initial_cash"]),
        commission_rate=float(raw["commission_rate"]),
        slippage_ticks=int(raw["slippage_ticks"]),
        tick_size=float(raw["tick_size"]),
        contract_multiplier=int(raw["contract_multiplier"]),
        margin_rate=float(raw["margin_rate"]),
        max_margin_usage=float(raw["max_margin_usage"]),
        max_symbol_exposure=float(raw["max_symbol_exposure"]),
        max_symbol_margin_usage=float(raw.get("max_symbol_margin_usage", 1.0)),
        max_trade_risk=float(raw.get("max_trade_risk", 1.0)),
        daily_loss_stop=float(raw["daily_loss_stop"]),
        baseline_strategy_name=strategy_name,
        baseline_parameters=strategy,
        contracts_path=root / contracts_value if contracts_value else None,
        source_interval_minutes=int(data.get("source_interval_minutes", 15)),
        bar_interval_minutes=int(data.get("bar_interval_minutes", 15)),
    )


def _load_search_settings(path: Path) -> SearchSettings:
    raw = json.loads(path.read_text(encoding="utf-8"))
    strategy = raw.get("strategy", raw)
    grid = strategy.get("parameter_grid")
    stages = strategy.get("stages", [])
    if grid is None and stages:
        if not isinstance(stages, list) or not stages:
            raise ValueError("strategy.stages must be a non-empty list.")
        grid = stages[0].get("parameter_grid")
    if not isinstance(grid, dict) or not grid:
        raise ValueError("optimization config requires a non-empty strategy.parameter_grid.")
    normalized_grid: dict[str, list[Any]] = {}
    candidate_count = 1
    for key, values in grid.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"parameter_grid.{key} must be a non-empty list.")
        normalized_grid[str(key)] = values
        candidate_count *= len(values)
    if candidate_count > int(raw.get("max_candidates", 1000)):
        raise ValueError(
            f"Parameter grid expands to {candidate_count} candidates; "
            "raise max_candidates explicitly if this is intentional."
        )

    split = raw.get("split", {})
    train_fraction = float(split.get("train_fraction", 0.50))
    validation_fraction = float(split.get("validation_fraction", 0.25))
    if train_fraction <= 0 or validation_fraction <= 0 or train_fraction + validation_fraction >= 1:
        raise ValueError("train_fraction and validation_fraction must be positive and sum to less than 1.")
    train_end = pd.Timestamp(split["train_end"]).normalize() if split.get("train_end") else None
    validation_end = (
        pd.Timestamp(split["validation_end"]).normalize()
        if split.get("validation_end")
        else None
    )
    if (train_end is None) != (validation_end is None):
        raise ValueError("Explicit split requires both train_end and validation_end.")
    if train_end is not None and validation_end is not None and train_end >= validation_end:
        raise ValueError("train_end must be earlier than validation_end.")

    sensitivity = raw.get("sensitivity", {})
    commission_multipliers = [
        float(value) for value in sensitivity.get("commission_multipliers", [0.5, 1.0, 1.5, 2.0])
    ]
    slippage_ticks = [
        int(value) for value in sensitivity.get("slippage_ticks", [0, 1, 2])
    ]
    if any(value < 0 for value in commission_multipliers):
        raise ValueError("commission_multipliers cannot contain negative values.")
    if any(value < 0 for value in slippage_ticks):
        raise ValueError("sensitivity slippage_ticks cannot contain negative values.")

    return SearchSettings(
        strategy_name=str(strategy["name"]),
        parameter_grid=normalized_grid,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
        train_end=train_end,
        validation_end=validation_end,
        min_bars_per_phase=int(split.get("min_bars_per_phase", 20)),
        objective=dict(raw.get("objective", {"metric": "robust_score"})),
        commission_multipliers=commission_multipliers,
        sensitivity_slippage_ticks=slippage_ticks,
    )


def _make_time_split(
    bars_by_symbol: dict[str, list[Bar]], search: SearchSettings
) -> TimeSplit:
    dates = sorted(
        {
            pd.Timestamp(bar.datetime).normalize()
            for bars in bars_by_symbol.values()
            for bar in bars
        }
    )
    if len(dates) < 3:
        raise ValueError("At least three distinct timestamps are required for chronological splitting.")

    if search.train_end is not None and search.validation_end is not None:
        train_end = search.train_end
        validation_end = search.validation_end
        validation_dates = [value for value in dates if train_end < value <= validation_end]
        test_dates = [value for value in dates if value > validation_end]
        if not validation_dates or not test_dates:
            raise ValueError("Explicit boundaries leave validation or final test empty.")
        validation_start = validation_dates[0]
        test_start = test_dates[0]
    else:
        train_count = max(1, int(math.floor(len(dates) * search.train_fraction)))
        validation_count = max(1, int(math.floor(len(dates) * search.validation_fraction)))
        if train_count + validation_count >= len(dates):
            raise ValueError("Fractional split leaves no final-test observations.")
        train_end = dates[train_count - 1]
        validation_start = dates[train_count]
        validation_end = dates[train_count + validation_count - 1]
        test_start = dates[train_count + validation_count]

    return TimeSplit(
        train_start=dates[0],
        train_end=train_end,
        validation_start=validation_start,
        validation_end=validation_end,
        test_start=test_start,
        test_end=dates[-1],
    )


def _validate_phase_coverage(
    bars_by_symbol: dict[str, list[Bar]], split: TimeSplit, min_bars: int
) -> None:
    if min_bars <= 0:
        raise ValueError("min_bars_per_phase must be positive.")
    phases = {
        "train": (split.train_start, split.train_end),
        "validation": (split.validation_start, split.validation_end),
        "test": (split.test_start, split.test_end),
    }
    failures: list[str] = []
    for symbol, bars in bars_by_symbol.items():
        dates = [pd.Timestamp(bar.datetime).normalize() for bar in bars]
        for phase, (start, end) in phases.items():
            count = sum(start <= value <= end for value in dates)
            if count < min_bars:
                failures.append(f"{symbol}:{phase}={count}")
    if failures:
        raise ValueError(
            f"Each instrument/phase needs at least {min_bars} bars; "
            + ", ".join(failures[:12])
        )


def _expand_parameter_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = sorted(grid)
    return [
        dict(zip(keys, values, strict=True))
        for values in itertools.product(*(grid[key] for key in keys))
    ]


def _build_strategy(
    name: str,
    parameters: dict[str, Any],
    settings: BacktestSettings,
    spec: ContractSpec | None,
) -> Strategy:
    if name not in STRATEGY_REGISTRY:
        raise ValueError(f"Unsupported optimization strategy: {name}")
    strategy_class = STRATEGY_REGISTRY[name]
    signature = inspect.signature(strategy_class.__init__)
    accepted = {key for key in signature.parameters if key != "self"}
    unknown = set(parameters) - accepted
    if unknown:
        raise ValueError(f"{name} does not accept parameters: {sorted(unknown)}")
    kwargs = dict(parameters)
    runtime_values: dict[str, Any] = {
        "initial_cash": settings.initial_cash,
        "contract_multiplier": (
            spec.contract_multiplier if spec is not None else settings.contract_multiplier
        ),
        "max_notional_fraction": settings.max_symbol_exposure,
        "margin_rate": spec.margin_rate if spec is not None else settings.margin_rate,
        "max_margin_fraction": settings.max_symbol_margin_usage,
        "max_trade_risk": settings.max_trade_risk,
    }
    for key, value in runtime_values.items():
        if key in accepted and key not in kwargs:
            kwargs[key] = value
    return strategy_class(**kwargs)


def _run_universe(
    bars_by_symbol: dict[str, list[Bar]],
    strategy_name: str,
    parameters: dict[str, Any],
    settings: BacktestSettings,
    contracts: dict[str, ContractSpec],
    run_end: pd.Timestamp,
    commission_multiplier: float = 1.0,
    slippage_ticks: int | None = None,
) -> dict[str, BacktestResult]:
    results: dict[str, BacktestResult] = {}
    for symbol, all_bars in bars_by_symbol.items():
        bars = [
            bar
            for bar in all_bars
            if pd.Timestamp(bar.datetime).normalize() <= run_end
        ]
        if not bars:
            raise ValueError(f"No bars available for {symbol} through {run_end.date()}.")
        spec = contracts.get(symbol)
        multiplier = spec.contract_multiplier if spec is not None else settings.contract_multiplier
        tick_size = spec.tick_size if spec is not None else settings.tick_size
        margin_rate = spec.margin_rate if spec is not None else settings.margin_rate
        commission_rate = spec.commission_rate if spec is not None else settings.commission_rate
        strategy = _build_strategy(strategy_name, parameters, settings, spec)
        risk = RiskEngine(
            RiskLimits(
                max_margin_usage=settings.max_margin_usage,
                max_symbol_exposure=settings.max_symbol_exposure,
                daily_loss_stop=settings.daily_loss_stop,
                margin_rate=margin_rate,
                contract_multiplier=multiplier,
                max_symbol_margin_usage=settings.max_symbol_margin_usage,
                max_trade_risk=settings.max_trade_risk,
            )
        )
        broker = BacktestBroker(
            initial_cash=settings.initial_cash,
            commission_rate=commission_rate * commission_multiplier,
            slippage_ticks=(settings.slippage_ticks if slippage_ticks is None else slippage_ticks),
            tick_size=tick_size,
            contract_multiplier=multiplier,
            margin_rate=margin_rate,
            risk_engine=risk,
        )
        results[symbol] = run_backtest(bars, strategy, broker)
    return results


def _phase_nav(
    result: BacktestResult,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_cash: float,
) -> pd.Series:
    curve = result.equity_curve[["datetime", "equity"]].copy()
    curve["datetime"] = pd.to_datetime(curve["datetime"]).dt.normalize()
    curve = curve.sort_values("datetime").drop_duplicates("datetime", keep="last")
    before = curve[curve["datetime"] < start]
    anchor_equity = float(before["equity"].iloc[-1]) if not before.empty else initial_cash
    phase = curve[(curve["datetime"] >= start) & (curve["datetime"] <= end)]
    if phase.empty:
        raise ValueError(f"No equity observations in phase {start.date()}..{end.date()}.")
    nav = phase.set_index("datetime")["equity"].astype(float) / anchor_equity
    nav.name = "nav"
    return nav


def _nav_metrics(nav: pd.Series, trade_count: int) -> dict[str, float | int | str]:
    nav = nav.dropna().astype(float)
    if nav.empty:
        raise ValueError("Cannot summarize an empty NAV series.")
    base = pd.Series([1.0], index=[nav.index[0] - pd.Timedelta(nanoseconds=1)])
    with_base = pd.concat([base, nav])
    returns = with_base.pct_change().dropna()
    total_return = float(nav.iloc[-1] - 1.0)
    observations = max(len(nav), 1)
    if len(nav) > 1:
        intervals = nav.index.to_series().diff().dropna().dt.total_seconds() / 86400.0
        typical_interval_days = max(float(intervals.median()), 1.0 / (24 * 60))
        elapsed_days = max(
            float((nav.index[-1] - nav.index[0]).total_seconds() / 86400.0)
            + typical_interval_days,
            typical_interval_days,
        )
    else:
        elapsed_days = 1.0
    elapsed_years = elapsed_days / 365.2425
    observations_per_year = observations / elapsed_years
    annualized_return = (
        float(nav.iloc[-1] ** (1.0 / elapsed_years) - 1.0) if nav.iloc[-1] > 0 else -1.0
    )
    volatility = (
        float(returns.std(ddof=1) * math.sqrt(observations_per_year))
        if len(returns) > 1
        else 0.0
    )
    sharpe = (
        float(returns.mean() / returns.std(ddof=1) * math.sqrt(observations_per_year))
        if len(returns) > 1 and returns.std(ddof=1) > 0
        else 0.0
    )
    max_drawdown = float((with_base / with_base.cummax() - 1.0).min())
    calmar = annualized_return / abs(max_drawdown) if max_drawdown < 0 else 0.0
    return {
        "start": nav.index[0].date().isoformat(),
        "end": nav.index[-1].date().isoformat(),
        "observations": observations,
        "elapsed_years": round(elapsed_years, 8),
        "observations_per_year": round(observations_per_year, 4),
        "total_return": round(total_return, 8),
        "annualized_return": round(annualized_return, 8),
        "annualized_volatility": round(volatility, 8),
        "max_drawdown": round(max_drawdown, 8),
        "sharpe": round(sharpe, 6),
        "calmar": round(calmar, 6),
        "trade_count": int(trade_count),
    }


def _phase_trade_count(result: BacktestResult, start: pd.Timestamp, end: pd.Timestamp) -> int:
    if result.trades.empty or "datetime" not in result.trades.columns:
        return 0
    dates = pd.to_datetime(result.trades["datetime"]).dt.normalize()
    return int(((dates >= start) & (dates <= end)).sum())


def _phase_rejection_count(
    result: BacktestResult, start: pd.Timestamp, end: pd.Timestamp
) -> int:
    if result.rejections.empty or "datetime" not in result.rejections.columns:
        return 0
    dates = pd.to_datetime(result.rejections["datetime"]).dt.normalize()
    return int(((dates >= start) & (dates <= end)).sum())


def _summarize_universe_phase(
    results: dict[str, BacktestResult],
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial_cash: float,
) -> dict[str, Any]:
    navs: dict[str, pd.Series] = {}
    instruments: dict[str, dict[str, Any]] = {}
    total_trades = 0
    total_rejections = 0
    for symbol, result in results.items():
        nav = _phase_nav(result, start, end, initial_cash)
        trades = _phase_trade_count(result, start, end)
        rejections = _phase_rejection_count(result, start, end)
        navs[symbol] = nav
        instruments[symbol] = _nav_metrics(nav, trades)
        instruments[symbol]["rejected_order_count"] = rejections
        instruments[symbol]["rejection_rate"] = (
            rejections / (trades + rejections) if trades + rejections else 0.0
        )
        total_trades += trades
        total_rejections += rejections
    nav_frame = pd.concat(navs, axis=1).sort_index().ffill().fillna(1.0)
    portfolio_nav = nav_frame.mean(axis=1)
    portfolio = _nav_metrics(portfolio_nav, total_trades)
    portfolio["rejected_order_count"] = total_rejections
    portfolio["rejection_rate"] = (
        total_rejections / (total_trades + total_rejections)
        if total_trades + total_rejections
        else 0.0
    )
    instrument_returns = [
        float(metrics["total_return"]) for metrics in instruments.values()
    ]
    portfolio["median_instrument_return"] = float(
        pd.Series(instrument_returns).median()
    )
    portfolio["positive_instrument_ratio"] = (
        sum(value > 0 for value in instrument_returns) / len(instrument_returns)
        if instrument_returns
        else 0.0
    )
    return {
        "portfolio": portfolio,
        "instruments": instruments,
        "portfolio_nav": portfolio_nav,
    }


def _objective_score(
    metrics: dict[str, Any],
    objective: dict[str, Any],
    instruments: dict[str, dict[str, Any]] | None = None,
) -> float:
    min_trades = int(
        objective.get("min_trades", objective.get("min_validation_trades", 1))
    )
    if int(metrics["trade_count"]) < min_trades:
        return -1.0e30
    min_instrument_trades = int(
        objective.get(
            "min_trades_per_instrument",
            objective.get("min_validation_trades_per_instrument", 0),
        )
    )
    if instruments is not None and min_instrument_trades > 0:
        if any(
            int(instrument_metrics["trade_count"]) < min_instrument_trades
            for instrument_metrics in instruments.values()
        ):
            return -1.0e30
    metric = str(objective.get("metric", "robust_score"))
    if metric in {"annualized_return", "sharpe", "calmar", "total_return"}:
        return float(metrics[metric])
    if metric == "enhanced_robust_score":
        median_return = float(metrics.get("median_instrument_return", 0.0))
        positive_ratio = float(metrics.get("positive_instrument_ratio", 0.0))
        rejection_rate = float(metrics.get("rejection_rate", 0.0))
        if median_return <= float(objective.get("min_median_instrument_return", 0.0)):
            return -1.0e30
        if positive_ratio < float(objective.get("min_positive_instrument_ratio", 0.60)):
            return -1.0e30
        if rejection_rate >= float(objective.get("max_rejection_rate", 0.10)):
            return -1.0e30
        clipped_sharpe = max(-5.0, min(5.0, float(metrics["sharpe"])))
        return (
            float(metrics["annualized_return"])
            + 0.02 * clipped_sharpe
            + 0.25 * median_return
            - 0.75 * abs(float(metrics["max_drawdown"]))
            - 0.10 * rejection_rate
        )
    if metric != "robust_score":
        raise ValueError(f"Unsupported objective metric: {metric}")
    return (
        float(objective.get("return_weight", 1.0)) * float(metrics["annualized_return"])
        + float(objective.get("sharpe_weight", 0.10)) * float(metrics["sharpe"])
        - float(objective.get("drawdown_penalty", 0.50)) * abs(float(metrics["max_drawdown"]))
    )


def _combine_phase_scores(
    train_score: float, validation_score: float, objective: dict[str, Any]
) -> float:
    """Combine pre-test phase scores; final-test metrics are unavailable here."""

    if train_score <= -1.0e29 or validation_score <= -1.0e29:
        return -1.0e30
    method = str(objective.get("selection_method", "min_train_validation"))
    if method == "min_train_validation":
        return min(train_score, validation_score)
    if method == "mean_minus_instability":
        penalty = float(objective.get("stability_penalty", 0.50))
        if penalty < 0:
            raise ValueError("stability_penalty cannot be negative.")
        return (train_score + validation_score) / 2.0 - penalty * abs(
            train_score - validation_score
        )
    raise ValueError(f"Unsupported selection_method: {method}")


def _prefixed_metrics(prefix: str, metrics: dict[str, Any]) -> dict[str, Any]:
    return {f"{prefix}_{key}": value for key, value in metrics.items()}


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _comparison_instrument_rows(
    selected: dict[str, Any],
    baseline: dict[str, Any],
    selected_name: str,
    baseline_name: str,
    selected_parameters: dict[str, Any],
    baseline_parameters: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    scenarios = [
        ("selected", selected_name, selected_parameters, selected),
        ("baseline", baseline_name, baseline_parameters, baseline),
    ]
    for scenario, strategy_name, parameters, evaluation in scenarios:
        for symbol, metrics in sorted(evaluation["instruments"].items()):
            rows.append(
                {
                    "scenario": scenario,
                    "symbol": symbol,
                    "strategy_name": strategy_name,
                    "parameters": _canonical_json(parameters),
                    **metrics,
                }
            )
    return rows


def _comparison_portfolio_rows(
    selected: dict[str, Any],
    baseline: dict[str, Any],
    selected_name: str,
    baseline_name: str,
    selected_parameters: dict[str, Any],
    baseline_parameters: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for scenario, strategy_name, parameters, evaluation in [
        ("selected", selected_name, selected_parameters, selected),
        ("baseline", baseline_name, baseline_parameters, baseline),
    ]:
        rows.append(
            {
                "scenario": scenario,
                "strategy_name": strategy_name,
                "parameters": _canonical_json(parameters),
                "used_for_selection": False,
                **evaluation["portfolio"],
            }
        )
    return rows


def _cost_sensitivity(
    bars_by_symbol: dict[str, list[Bar]],
    search: SearchSettings,
    selected_parameters: dict[str, Any],
    settings: BacktestSettings,
    contracts: dict[str, ContractSpec],
    split: TimeSplit,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for commission_multiplier, slippage_ticks in itertools.product(
        search.commission_multipliers, search.sensitivity_slippage_ticks
    ):
        results = _run_universe(
            bars_by_symbol,
            search.strategy_name,
            selected_parameters,
            settings,
            contracts,
            run_end=split.test_end,
            commission_multiplier=commission_multiplier,
            slippage_ticks=slippage_ticks,
        )
        evaluation = _summarize_universe_phase(
            results, split.test_start, split.test_end, settings.initial_cash
        )
        objective_score = _objective_score(
            evaluation["portfolio"], search.objective, evaluation["instruments"]
        )
        rows.append(
            {
                "commission_multiplier": commission_multiplier,
                "slippage_ticks": slippage_ticks,
                "used_for_selection": False,
                "objective_score": objective_score,
                **evaluation["portfolio"],
            }
        )
    return rows


def _write_portfolio_equity(
    selected: dict[str, Any], baseline: dict[str, Any], output_path: Path
) -> None:
    frames: list[pd.DataFrame] = []
    for scenario, evaluation in [("selected", selected), ("baseline", baseline)]:
        frame = evaluation["portfolio_nav"].rename("portfolio_nav").reset_index()
        frame.insert(0, "scenario", scenario)
        frames.append(frame)
    pd.concat(frames, ignore_index=True).to_csv(output_path, index=False, encoding="utf-8-sig")


def _write_split_manifest(
    split: TimeSplit, bars_by_symbol: dict[str, list[Bar]], output_path: Path
) -> None:
    rows: list[dict[str, Any]] = []
    for symbol, bars in sorted(bars_by_symbol.items()):
        dates = [pd.Timestamp(bar.datetime).normalize() for bar in bars]
        for phase, start, end, used_for_selection in [
            ("train", split.train_start, split.train_end, True),
            ("validation", split.validation_start, split.validation_end, True),
            ("final_test", split.test_start, split.test_end, False),
        ]:
            rows.append(
                {
                    "symbol": symbol,
                    "phase": phase,
                    "start": start.date().isoformat(),
                    "end": end.date().isoformat(),
                    "bar_count": sum(start <= value <= end for value in dates),
                    "used_for_parameter_selection": used_for_selection,
                }
            )
    pd.DataFrame(rows).to_csv(output_path, index=False, encoding="utf-8-sig")


def _write_markdown_report(
    output_path: Path,
    selected: dict[str, Any],
    split: TimeSplit,
    portfolio: pd.DataFrame,
    sensitivity: pd.DataFrame,
) -> None:
    selected_row = portfolio[portfolio["scenario"] == "selected"].iloc[0]
    baseline_row = portfolio[portfolio["scenario"] == "baseline"].iloc[0]
    worst_cost = sensitivity.sort_values("total_return", ascending=True).iloc[0]
    lines = [
        "# 策略参数优化与样本外报告",
        "",
        "> 参数只由训练集和验证集选择；最终测试集没有参与候选排名。",
        "",
        "## 时间切分",
        "",
        f"- 训练：{split.train_start.date()} 至 {split.train_end.date()}",
        f"- 验证：{split.validation_start.date()} 至 {split.validation_end.date()}",
        f"- 最终测试：{split.test_start.date()} 至 {split.test_end.date()}",
        "",
        "## 选中参数",
        "",
        f"- 策略：`{selected['strategy_name']}`",
        f"- 候选：`{selected['candidate_id']}`",
        f"- 参数：`{_canonical_json(selected['parameters'])}`",
        f"- 选择规则：`{selected['selection_method']}`",
        (
            f"- 训练得分：{float(selected['train_score']):.6f}；"
            f"验证得分：{float(selected['validation_score']):.6f}；"
            f"最终选择得分：{float(selected['selection_score']):.6f}"
        ),
        "",
        "## 最终测试组合对比",
        "",
        "| 方案 | 总收益 | 年化收益 | 最大回撤 | Sharpe | 交易数 |",
        "|---|---:|---:|---:|---:|---:|",
        (
            f"| 优化策略 | {float(selected_row['total_return']):.2%} | "
            f"{float(selected_row['annualized_return']):.2%} | "
            f"{float(selected_row['max_drawdown']):.2%} | "
            f"{float(selected_row['sharpe']):.3f} | {int(selected_row['trade_count'])} |"
        ),
        (
            f"| 基准策略 | {float(baseline_row['total_return']):.2%} | "
            f"{float(baseline_row['annualized_return']):.2%} | "
            f"{float(baseline_row['max_drawdown']):.2%} | "
            f"{float(baseline_row['sharpe']):.3f} | {int(baseline_row['trade_count'])} |"
        ),
        "",
        "## 成本压力测试",
        "",
        (
            f"结构化成本网格中最低最终测试收益为 {float(worst_cost['total_return']):.2%}，"
            f"对应手续费倍数 {float(worst_cost['commission_multiplier']):g}、"
            f"滑点 {int(worst_cost['slippage_ticks'])} ticks。"
        ),
        "",
        "完整结果见同目录 CSV。若输入是 synthetic 数据，结果只验证研究流程，不代表可实现收益。",
    ]
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
