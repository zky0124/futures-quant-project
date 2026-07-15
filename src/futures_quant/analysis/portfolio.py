from __future__ import annotations

from pathlib import Path

import pandas as pd


def analyze_batch_results(summary_path: str | Path, reports_dir: str | Path, output_dir: str | Path) -> dict[str, Path]:
    summary_path = Path(summary_path)
    reports_dir = Path(reports_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = pd.read_csv(summary_path)
    if summary.empty:
        raise ValueError(f"Empty summary file: {summary_path}")

    equity_frames: list[pd.DataFrame] = []
    for row in summary.itertuples(index=False):
        symbol = str(row.symbol)
        curve_path = reports_dir / f"{symbol}_summary_equity_curve.csv"
        if not curve_path.exists():
            raise FileNotFoundError(f"Equity curve not found for {symbol}: {curve_path}")
        curve = pd.read_csv(curve_path, parse_dates=["datetime"])
        if curve.empty:
            continue
        first_equity = float(curve["equity"].iloc[0])
        curve = curve[["datetime", "equity"]].copy()
        curve["symbol"] = symbol
        curve["nav"] = curve["equity"].astype(float) / first_equity
        equity_frames.append(curve[["datetime", "symbol", "nav"]])

    if not equity_frames:
        raise ValueError("No equity curves available for analysis.")

    nav_long = pd.concat(equity_frames, ignore_index=True)
    nav_wide = nav_long.pivot(index="datetime", columns="symbol", values="nav").sort_index().ffill()
    portfolio_nav = nav_wide.mean(axis=1)
    portfolio_curve = pd.DataFrame(
        {
            "datetime": portfolio_nav.index,
            "portfolio_nav": portfolio_nav.values,
            "portfolio_return": portfolio_nav.pct_change().fillna(0.0).values,
        }
    )
    portfolio_path = output_dir / "portfolio_equity.csv"
    portfolio_curve.to_csv(portfolio_path, index=False, encoding="utf-8-sig")

    ranking = summary.copy()
    for col in [
        "total_return",
        "annualized_return",
        "annualized_volatility",
        "periods_per_year",
        "max_drawdown",
        "sharpe",
        "calmar",
        "trade_count",
        "rejected_order_count",
    ]:
        if col not in ranking.columns:
            continue
        ranking[col] = pd.to_numeric(ranking[col], errors="coerce")
    ranking = ranking.sort_values(["sharpe", "total_return"], ascending=[False, False])
    ranking_path = output_dir / "instrument_ranking.csv"
    ranking.to_csv(ranking_path, index=False, encoding="utf-8-sig")

    group_rows: list[dict[str, object]] = []
    for group, group_df in ranking.groupby("group", dropna=False):
        symbols = [str(s) for s in group_df["symbol"].tolist()]
        group_nav = nav_wide[symbols].mean(axis=1)
        group_rows.append(
            {
                "group": group,
                "instrument_count": len(symbols),
                "avg_total_return": round(float(group_df["total_return"].mean()), 6),
                "avg_max_drawdown": round(float(group_df["max_drawdown"].mean()), 6),
                "avg_sharpe": round(float(group_df["sharpe"].mean()), 4),
                "group_nav_final": round(float(group_nav.iloc[-1]), 6),
                "group_nav_max_drawdown": round(float((group_nav / group_nav.cummax() - 1).min()), 6),
                "total_trades": int(group_df["trade_count"].sum()),
                "rejected_orders": int(group_df["rejected_order_count"].sum()),
            }
        )
    group_summary = pd.DataFrame(group_rows).sort_values("avg_sharpe", ascending=False)
    group_path = output_dir / "group_summary.csv"
    group_summary.to_csv(group_path, index=False, encoding="utf-8-sig")

    portfolio_returns = portfolio_nav.pct_change().fillna(0.0)
    portfolio_observations = max(len(portfolio_nav) - 1, 1)
    portfolio_elapsed_years = 0.0
    if len(portfolio_nav.index) > 1:
        elapsed_seconds = (
            pd.Timestamp(portfolio_nav.index[-1]) - pd.Timestamp(portfolio_nav.index[0])
        ).total_seconds()
        portfolio_elapsed_years = max(elapsed_seconds / (365.25 * 24 * 60 * 60), 0.0)
    portfolio_periods_per_year = (
        portfolio_observations / portfolio_elapsed_years
        if portfolio_elapsed_years > 0
        else 252.0
    )
    portfolio_total_return = float(portfolio_nav.iloc[-1] - 1)
    portfolio_annualized_return = (
        float(portfolio_nav.iloc[-1] ** (1 / portfolio_elapsed_years) - 1)
        if portfolio_nav.iloc[-1] > 0 and portfolio_elapsed_years > 0
        else portfolio_total_return
    )
    portfolio_volatility = float(
        portfolio_returns.std() * (portfolio_periods_per_year ** 0.5)
    )
    portfolio_sharpe = (
        float(
            portfolio_returns.mean()
            / portfolio_returns.std()
            * (portfolio_periods_per_year ** 0.5)
        )
        if portfolio_returns.std() != 0
        else 0.0
    )
    portfolio_max_drawdown = float((portfolio_nav / portfolio_nav.cummax() - 1).min())
    portfolio_calmar = (
        portfolio_annualized_return / abs(portfolio_max_drawdown)
        if portfolio_max_drawdown < 0
        else 0.0
    )
    portfolio_summary = pd.DataFrame(
        [
            {
                "instrument_count": int(len(summary)),
                "portfolio_nav_final": round(float(portfolio_nav.iloc[-1]), 6),
                "portfolio_total_return": round(portfolio_total_return, 6),
                "portfolio_annualized_return": round(portfolio_annualized_return, 6),
                "portfolio_annualized_volatility": round(portfolio_volatility, 6),
                "portfolio_periods_per_year": round(portfolio_periods_per_year, 4),
                "portfolio_max_drawdown": round(portfolio_max_drawdown, 6),
                "portfolio_sharpe": round(portfolio_sharpe, 4),
                "portfolio_calmar": round(portfolio_calmar, 4),
                "start": str(portfolio_nav.index[0].date()),
                "end": str(portfolio_nav.index[-1].date()),
            }
        ]
    )
    portfolio_summary_path = output_dir / "portfolio_summary.csv"
    portfolio_summary.to_csv(portfolio_summary_path, index=False, encoding="utf-8-sig")

    return {
        "portfolio_equity": portfolio_path,
        "portfolio_summary": portfolio_summary_path,
        "group_summary": group_path,
        "instrument_ranking": ranking_path,
    }
