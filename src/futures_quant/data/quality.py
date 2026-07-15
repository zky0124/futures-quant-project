from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from futures_quant.data.csv_loader import REQUIRED_COLUMNS


@dataclass(frozen=True)
class DataQualityResult:
    file: str
    symbol: str
    status: str
    bar_count: int
    start: str
    end: str
    issue_count: int
    issues: str


def validate_bar_csv(path: str | Path, expected_symbol: str | None = None) -> DataQualityResult:
    path = Path(path)
    issues: list[str] = []
    try:
        df = pd.read_csv(path)
    except Exception as exc:
        return DataQualityResult(str(path), expected_symbol or "", "error", 0, "", "", 1, f"read_error:{exc}")

    missing = sorted(REQUIRED_COLUMNS - set(df.columns))
    if missing:
        issues.append(f"missing_columns:{','.join(missing)}")
        return DataQualityResult(str(path), expected_symbol or "", "error", len(df), "", "", len(issues), ";".join(issues))

    symbol = expected_symbol or str(df["symbol"].iloc[0]) if not df.empty else ""
    if df.empty:
        issues.append("empty_file")
        return DataQualityResult(str(path), symbol, "error", 0, "", "", len(issues), ";".join(issues))

    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    if df["datetime"].isna().any():
        issues.append("invalid_datetime")

    if expected_symbol is not None and (df["symbol"].astype(str) != expected_symbol).any():
        issues.append("symbol_mismatch")

    duplicate_count = int(df.duplicated(subset=["datetime", "symbol"]).sum())
    if duplicate_count:
        issues.append(f"duplicate_bars:{duplicate_count}")

    if not df.sort_values(["symbol", "datetime"]).reset_index(drop=True).equals(df.reset_index(drop=True)):
        issues.append("not_sorted")

    numeric_cols = ["open", "high", "low", "close", "volume"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if df[numeric_cols].isna().any().any():
        issues.append("numeric_null_or_invalid")

    price_cols = ["open", "high", "low", "close"]
    if (df[price_cols] <= 0).any().any():
        issues.append("non_positive_price")
    if (df["volume"] < 0).any():
        issues.append("negative_volume")
    if ((df["high"] < df[["open", "close", "low"]].max(axis=1)) | (df["low"] > df[["open", "close", "high"]].min(axis=1))).any():
        issues.append("ohlc_relation_error")

    clean_dt = df["datetime"].dropna()
    start = str(clean_dt.min().date()) if not clean_dt.empty else ""
    end = str(clean_dt.max().date()) if not clean_dt.empty else ""
    status = "ok" if not issues else "warning"
    return DataQualityResult(str(path), symbol, status, len(df), start, end, len(issues), ";".join(issues))


def validate_history_dir(history_dir: str | Path, output_path: str | Path, suffix: str = "_1d.csv") -> list[DataQualityResult]:
    history_dir = Path(history_dir)
    output_path = Path(output_path)
    results: list[DataQualityResult] = []
    for path in sorted(history_dir.glob(f"*{suffix}")):
        symbol = path.name[: -len(suffix)] if suffix and path.name.endswith(suffix) else path.stem
        results.append(validate_bar_csv(path, symbol))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["file", "symbol", "status", "bar_count", "start", "end", "issue_count", "issues"],
        )
        writer.writeheader()
        for result in results:
            writer.writerow(result.__dict__)
    return results
