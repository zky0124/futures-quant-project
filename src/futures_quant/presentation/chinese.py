from __future__ import annotations

from pathlib import Path
from typing import Mapping

import pandas as pd


# Internal keys deliberately remain stable English identifiers. Every
# user-facing table resolves its heading through this dictionary.
COLUMN_LABELS: dict[str, str] = {
    "datetime": "时间",
    "date": "日期",
    "symbol": "品种代码",
    "name": "品种名称",
    "group": "品种分组",
    "exchange": "交易所",
    "product": "品种",
    "quantity": "成交手数",
    "position": "持仓手数",
    "target_position": "目标持仓手数",
    "active_position_count": "持仓品种数",
    "open_position_count": "未平仓品种数",
    "positions": "持仓明细",
    "status": "状态",
    "direction": "方向",
    "price": "成交价格",
    "reference_price": "参考价格",
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "close": "收盘价",
    "volume": "成交量",
    "open_interest": "持仓量",
    "cash": "可用现金",
    "cash_after": "成交后现金",
    "equity": "账户权益",
    "final_cash": "期末现金",
    "initial_cash": "期初资金",
    "final_equity": "期末权益",
    "margin": "占用保证金",
    "margin_usage": "保证金占用率",
    "max_margin_usage_observed": "最高保证金占用率",
    "gross_notional": "总名义敞口",
    "net_notional": "净名义敞口",
    "session_return": "当日收益率",
    "total_return": "总收益率",
    "annualized_return": "年化收益率",
    "annualized_volatility": "年化波动率",
    "max_drawdown": "最大回撤",
    "sharpe": "夏普比率",
    "calmar": "卡玛比率",
    "commission": "手续费",
    "commission_total": "手续费合计",
    "commission_rate": "手续费率",
    "slippage_cost": "滑点成本",
    "slippage_cost_total": "滑点成本合计",
    "realized_pnl": "已实现盈亏",
    "net_realized_pnl": "已实现净盈亏",
    "realized_pnl_before_commission": "手续费前已实现盈亏",
    "closed_quantity": "平仓手数",
    "winning_closures": "盈利平仓次数",
    "losing_closures": "亏损平仓次数",
    "gross_win_rate": "平仓胜率",
    "trade_count": "成交笔数",
    "closed_trade_count": "平仓成交笔数",
    "realized_trade_count": "实现盈亏笔数",
    "winning_realizations": "盈利实现笔数",
    "rejected_order_count": "拒单笔数",
    "rejection_rate": "拒单率",
    "median_instrument_return": "品种收益中位数",
    "positive_instrument_ratio": "正收益品种比例",
    "objective_score": "稳健目标得分",
    "reason": "成交原因",
    "currency": "币种",
    "base_currency": "账户币种",
    "contract_multiplier": "合约乘数",
    "tick_size": "最小变动价位",
    "margin_rate": "保证金率",
    "start": "开始日期",
    "end": "结束日期",
    "periods_per_year": "年化观测数",
    "observations": "观测数",
    "observations_per_year": "年化观测数",
    "elapsed_years": "区间年数",
    "symbol_count": "品种数量",
    "instrument_count": "品种数量",
    "bar_count": "K线数量",
    "source_bar_count": "源K线数量",
    "aggregated_bar_count": "运行K线数量",
    "source_interval_minutes": "源数据周期（分钟）",
    "bar_interval_minutes": "运行周期（分钟）",
    "bars": "K线数量",
    "file_count": "文件数量",
    "warning_count": "警告数量",
    "issue_count": "问题数量",
    "issues": "问题明细",
    "file": "文件",
    "path": "路径",
    "data_path": "数据路径",
    "summary_path": "摘要路径",
    "provider": "数据来源",
    "provider_config": "数据来源配置",
    "error": "错误信息",
    "rank": "排名",
    "scan_status": "扫描状态",
    "candidate_id": "候选编号",
    "strategy_name": "策略名称",
    "parameters": "策略参数",
    "selection_score": "选择得分",
    "train_score": "训练得分",
    "validation_score": "验证得分",
    "phase_score_gap": "阶段得分差",
    "selection_method": "选择方法",
    "selection_eligible": "可参与选择",
    "validation_instrument_count": "验证品种数量",
    "validation_instruments_meeting_min_trades": "达到最低成交数的验证品种数量",
    "scenario": "方案",
    "used_for_selection": "是否参与选参",
    "used_for_parameter_selection": "是否参与参数选择",
    "phase": "数据阶段",
    "commission_multiplier": "手续费倍数",
    "slippage_ticks": "滑点跳数",
    "portfolio_nav": "组合净值",
    "portfolio_return": "组合收益率",
    "nav": "净值",
    "nav_final": "期末净值",
    "avg_total_return": "平均总收益率",
    "avg_max_drawdown": "平均最大回撤",
    "avg_sharpe": "平均夏普比率",
    "total_trades": "成交总笔数",
    "rejected_orders": "拒单总笔数",
    "reason_code": "原因代码",
    "candidate_ranking": "候选参数排名文件",
    "selected_parameters": "选中参数文件",
    "selected_backtest_config": "选中回测配置文件",
    "oos_instrument_comparison": "样本外逐品种对比文件",
    "oos_portfolio_comparison": "样本外组合对比文件",
    "cost_sensitivity": "成本敏感性文件",
    "split_manifest": "数据切分清单",
    "oos_portfolio_equity": "样本外组合权益曲线文件",
    "optimization_report": "参数优化报告",
    "portfolio_equity": "组合权益曲线文件",
    "portfolio_summary": "组合摘要文件",
    "group_summary": "分组摘要文件",
    "instrument_ranking": "品种排名文件",
}

PARAMETER_LABELS: dict[str, str] = {
    "fast_window": "短周期窗口",
    "slow_window": "长周期窗口",
    "daily_fast_window": "日线短周期窗口",
    "daily_slow_window": "日线长周期窗口",
    "entry_window": "入场窗口",
    "exit_window": "离场窗口",
    "trend_window": "趋势窗口",
    "momentum_window": "动量窗口",
    "volatility_window": "波动率窗口",
    "target_annual_volatility": "目标年化波动率",
    "order_size": "下单手数",
    "max_order_size": "最大下单手数",
    "max_notional_fraction": "最大名义敞口比例",
    "momentum_threshold": "动量阈值",
    "allow_short": "允许做空",
    "atr_stop_multiple": "ATR初始止损倍数",
    "partial_exit_fraction": "2R部分退出比例",
    "extreme_move_threshold": "极端行情阈值",
    "reward_risk": "盈亏比",
    "trailing_atr_multiple": "ATR跟踪倍数",
}

VALUE_LABELS: dict[str, str] = {
    "ok": "正常",
    "warning": "警告",
    "error": "错误",
    "invalid": "无效",
    "filled": "已成交",
    "long": "多头",
    "short": "空头",
    "flat": "空仓",
    "True": "是",
    "False": "否",
}


def chinese_column_name(column: object) -> str:
    key = str(column)
    if key in COLUMN_LABELS:
        return COLUMN_LABELS[key]
    if key.startswith("param_"):
        parameter = key.removeprefix("param_")
        return f"参数-{PARAMETER_LABELS.get(parameter, parameter)}"
    if key.startswith("train_"):
        return f"训练-{chinese_column_name(key.removeprefix('train_'))}"
    if key.startswith("validation_"):
        return f"验证-{chinese_column_name(key.removeprefix('validation_'))}"
    if key.startswith("portfolio_"):
        return f"组合-{chinese_column_name(key.removeprefix('portfolio_'))}"
    if key.startswith("group_"):
        return f"分组-{chinese_column_name(key.removeprefix('group_'))}"
    return key


def chinese_frame(frame: pd.DataFrame, *, translate_values: bool = True) -> pd.DataFrame:
    displayed = frame.copy()
    if translate_values and not displayed.empty:
        for column in displayed.columns:
            if str(column) in {"status", "direction", "used_for_selection", "selection_eligible"}:
                displayed[column] = displayed[column].map(
                    lambda value: VALUE_LABELS.get(str(value), value)
                )
    return displayed.rename(columns={column: chinese_column_name(column) for column in displayed.columns})


def chinese_summary_frame(summary: Mapping[str, object]) -> pd.DataFrame:
    return chinese_frame(pd.DataFrame([dict(summary)]))


def chinese_value(value: object) -> object:
    return VALUE_LABELS.get(str(value), value)


def write_chinese_csv(frame: pd.DataFrame, path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    chinese_frame(frame).to_csv(output, index=False, encoding="utf-8-sig")
    return output


def write_chinese_companion(frame: pd.DataFrame, path: str | Path) -> Path:
    source_path = Path(path)
    output = source_path.with_name(f"{source_path.stem}_中文{source_path.suffix}")
    return write_chinese_csv(frame, output)
