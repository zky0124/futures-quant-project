from __future__ import annotations

import pandas as pd


def save_summary(summary: dict[str, object], path: str) -> None:
    pd.DataFrame([summary]).to_csv(path, index=False, encoding="utf-8-sig")
