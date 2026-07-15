from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd


def generate_markdown_report(analysis_dir: str | Path, output_path: str | Path, title: str = "多品种期货量化回测报告") -> Path:
    analysis_dir = Path(analysis_dir)
    output_path = Path(output_path)
    portfolio_summary = pd.read_csv(analysis_dir / "portfolio_summary.csv")
    group_summary = pd.read_csv(analysis_dir / "group_summary.csv")
    ranking = pd.read_csv(analysis_dir / "instrument_ranking.csv")

    if portfolio_summary.empty:
        raise ValueError("portfolio_summary.csv is empty.")

    p = portfolio_summary.iloc[0]
    annualized_return = float(p.get("portfolio_annualized_return", p["portfolio_total_return"]))
    annualized_volatility = float(p.get("portfolio_annualized_volatility", 0.0))
    portfolio_sharpe = float(p.get("portfolio_sharpe", 0.0))
    portfolio_calmar = float(p.get("portfolio_calmar", 0.0))
    lines: list[str] = [
        f"# {title}",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 重要说明",
        "",
        "本报告由项目回测结果自动生成。若数据来自 `synthetic` 或 demo provider，结果仅用于验证工程链路，不代表真实市场行情或投资建议。",
        "`http`/`file://` 只表示数据传输方式，不自动证明上游行情真实；发布结果前必须核对 `_fetch_manifest.csv`、接口配置和原始数据来源。",
        "",
        "## 组合概览",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 品种数量 | {int(p['instrument_count'])} |",
        f"| 回测区间 | {p['start']} 至 {p['end']} |",
        f"| 等权组合最终净值 | {float(p['portfolio_nav_final']):.6f} |",
        f"| 等权组合收益 | {_pct(p['portfolio_total_return'])} |",
        f"| 等权组合年化收益 | {_pct(annualized_return)} |",
        f"| 等权组合年化波动率 | {_pct(annualized_volatility)} |",
        f"| 等权组合最大回撤 | {_pct(p['portfolio_max_drawdown'])} |",
        f"| 等权组合 Sharpe | {portfolio_sharpe:.4f} |",
        f"| 等权组合 Calmar | {portfolio_calmar:.4f} |",
        "",
        "## 分组表现",
        "",
        _markdown_table(
            group_summary,
            [
                ("group", "分组"),
                ("instrument_count", "品种数"),
                ("avg_total_return", "平均收益"),
                ("avg_max_drawdown", "平均最大回撤"),
                ("avg_sharpe", "平均 Sharpe"),
                ("group_nav_final", "分组净值"),
                ("group_nav_max_drawdown", "分组净值最大回撤"),
                ("total_trades", "交易数"),
                ("rejected_orders", "拒单数"),
            ],
            pct_columns={"avg_total_return", "avg_max_drawdown", "group_nav_max_drawdown"},
        ),
        "",
        "## 品种排名",
        "",
        _markdown_table(
            ranking[
                [
                    "symbol",
                    "name",
                    "group",
                    "total_return",
                    "max_drawdown",
                    "sharpe",
                    "trade_count",
                    "rejected_order_count",
                ]
            ],
            [
                ("symbol", "代码"),
                ("name", "名称"),
                ("group", "分组"),
                ("total_return", "收益"),
                ("max_drawdown", "最大回撤"),
                ("sharpe", "Sharpe"),
                ("trade_count", "交易数"),
                ("rejected_order_count", "拒单数"),
            ],
            pct_columns={"total_return", "max_drawdown"},
        ),
        "",
        "## 文件索引",
        "",
        "- 组合摘要：`reports/analysis/portfolio_summary.csv`",
        "- 组合净值：`reports/analysis/portfolio_equity.csv`",
        "- 分组汇总：`reports/analysis/group_summary.csv`",
        "- 品种排名：`reports/analysis/instrument_ranking.csv`",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def _pct(value: object) -> str:
    return f"{float(value) * 100:.2f}%"


def _fmt(value: object, pct: bool = False) -> str:
    if pct:
        return _pct(value)
    if isinstance(value, float):
        return f"{value:.4f}"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:.4f}"


def _markdown_table(df: pd.DataFrame, columns: list[tuple[str, str]], pct_columns: set[str] | None = None) -> str:
    pct_columns = pct_columns or set()
    header = "| " + " | ".join(label for _, label in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    rows = [header, sep]
    for row in df.itertuples(index=False):
        row_dict = row._asdict()
        rows.append("| " + " | ".join(_fmt(row_dict[key], key in pct_columns) for key, _ in columns) + " |")
    return "\n".join(rows)
