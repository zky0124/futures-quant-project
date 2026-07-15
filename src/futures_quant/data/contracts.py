from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd


REQUIRED_COLUMNS = {
    "symbol",
    "exchange",
    "product",
    "contract_multiplier",
    "tick_size",
    "margin_rate",
    "commission_rate",
}


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    exchange: str
    product: str
    contract_multiplier: int
    tick_size: float
    margin_rate: float
    commission_rate: float


def load_contract_specs(path: str | Path) -> dict[str, ContractSpec]:
    df = pd.read_csv(path)
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Contract spec CSV missing required columns: {sorted(missing)}")

    specs: dict[str, ContractSpec] = {}
    for row in df.itertuples(index=False):
        spec = ContractSpec(
            symbol=str(row.symbol),
            exchange=str(row.exchange),
            product=str(row.product),
            contract_multiplier=int(row.contract_multiplier),
            tick_size=float(row.tick_size),
            margin_rate=float(row.margin_rate),
            commission_rate=float(row.commission_rate),
        )
        validate_contract_spec(spec)
        specs[spec.symbol] = spec
    return specs


def validate_contract_spec(spec: ContractSpec) -> None:
    if not spec.symbol:
        raise ValueError("Contract spec symbol cannot be empty.")
    if spec.contract_multiplier <= 0:
        raise ValueError(f"{spec.symbol}: contract_multiplier must be positive.")
    if spec.tick_size <= 0:
        raise ValueError(f"{spec.symbol}: tick_size must be positive.")
    if not 0 < spec.margin_rate < 1:
        raise ValueError(f"{spec.symbol}: margin_rate must be between 0 and 1.")
    if spec.commission_rate < 0:
        raise ValueError(f"{spec.symbol}: commission_rate cannot be negative.")
