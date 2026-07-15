from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from futures_quant.dashboard import (
    DEFAULT_STRATEGY_LABEL,
    STRATEGY_LABELS,
    assess_api_readiness,
    available_instruments_for_data,
    classify_data_source,
    scan_data_directory,
)
from futures_quant.data.history import Instrument


class DashboardDataReadinessTest(unittest.TestCase):
    @staticmethod
    def _instrument(symbol: str) -> Instrument:
        return Instrument(
            symbol=symbol,
            name=symbol,
            group="测试板块",
            base_price=100.0,
            drift=0.0,
            volatility=0.01,
            seed=1,
        )

    def test_default_strategy_is_established_adaptive_baseline(self) -> None:
        self.assertEqual(DEFAULT_STRATEGY_LABEL, "自适应趋势")
        self.assertEqual(STRATEGY_LABELS[DEFAULT_STRATEGY_LABEL], "adaptive_trend")

    def test_synthetic_and_unknown_data_are_never_labelled_real(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            synthetic = root / "domestic_15m"
            synthetic.mkdir()
            unknown = root / "vendor_export"
            unknown.mkdir()

            self.assertEqual(classify_data_source(synthetic).kind, "synthetic")
            self.assertEqual(classify_data_source(unknown).kind, "unknown")
            self.assertIn("禁止视为真实", classify_data_source(unknown).label)

    def test_pobo_source_manifest_marks_real_but_keeps_coverage_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csv_path = root / "RB0_15m.csv"
            csv_path.touch()
            csv_path.with_suffix(".csv.source.json").write_text(
                json.dumps(
                    {
                        "data_kind": "real_market",
                        "provider": "pobo_local_cache",
                    }
                ),
                encoding="utf-8",
            )

            classification = classify_data_source(root)
            self.assertEqual(classification.kind, "real")
            self.assertIn("覆盖", classification.detail)

    def test_directory_scan_reports_three_year_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pd.DataFrame(
                [
                    {"datetime": "2023-01-01 09:00", "symbol": "RB0"},
                    {"datetime": "2026-01-02 09:00", "symbol": "RB0"},
                ]
            ).to_csv(root / "RB0_15m.csv", index=False)
            pd.DataFrame(
                [
                    {"datetime": "2025-10-01 09:00", "symbol": "AU0"},
                    {"datetime": "2026-01-02 09:00", "symbol": "AU0"},
                ]
            ).to_csv(root / "AU0_15m.csv", index=False)

            coverage = scan_data_directory(root, "_15m.csv").set_index("symbol")
            self.assertEqual(coverage.loc["RB0", "three_year_check"], "约3年或以上")
            self.assertEqual(coverage.loc["AU0", "three_year_check"], "不足3年")
            self.assertEqual(coverage.loc["RB0", "status"], "ok")

    def test_available_instruments_only_uses_matching_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            instruments = [
                self._instrument("RB0"),
                self._instrument("HC0"),
                self._instrument("AU0"),
                self._instrument("C0"),
            ]
            (root / "RB0_15m.csv").touch()
            (root / "AU0_15m.csv").touch()
            (root / "HC0_60m.csv").touch()
            (root / "C0_15m.csv").mkdir()

            available = available_instruments_for_data(
                instruments, root, "_15m.csv"
            )

            self.assertEqual([item.symbol for item in available], ["RB0", "AU0"])
            self.assertEqual(
                available_instruments_for_data(instruments, root, ""), []
            )


class DashboardApiReadinessTest(unittest.TestCase):
    def _write_config(self, root: Path, payload: dict[str, object]) -> Path:
        path = root / "api.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_missing_config_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            readiness = assess_api_readiness(Path(temp_dir) / "missing.json")
            self.assertEqual(readiness.status_code, "missing_config")
            self.assertIn("配置缺失", readiness.status)

    def test_disabled_ctp_shows_sdk_and_never_exposes_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write_config(
                Path(temp_dir),
                {
                    "gateway": "ctp",
                    "broker_name": "长江期货",
                    "broker_id": "4300",
                    "trade_front": "tcp://127.0.0.1:10001",
                    "market_front": "tcp://127.0.0.1:10002",
                    "user_id": "PRIVATE_USER",
                    "password": "TOP_SECRET_PASSWORD",
                    "app_id": "PRIVATE_APP",
                    "auth_code": "TOP_SECRET_AUTH",
                    "enabled": False,
                },
            )
            readiness = assess_api_readiness(path, sdk_available=False)
            rendered = repr(readiness)

            self.assertEqual(readiness.status_code, "disabled")
            self.assertIn(("CTP SDK模块", "缺失/未配置"), readiness.safe_fields)
            self.assertIn(
                ("项目CTP网关层", "已实现；默认禁用并带运行时风控"),
                readiness.safe_fields,
            )
            self.assertNotIn("PRIVATE_USER", rendered)
            self.assertNotIn("TOP_SECRET_PASSWORD", rendered)
            self.assertNotIn("PRIVATE_APP", rendered)
            self.assertNotIn("TOP_SECRET_AUTH", rendered)

    def test_enabled_ctp_without_sdk_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write_config(
                Path(temp_dir),
                {
                    "gateway": "ctp",
                    "broker_id": "4300",
                    "trade_front": "tcp://127.0.0.1:10001",
                    "market_front": "tcp://127.0.0.1:10002",
                    "enabled": True,
                },
            )
            readiness = assess_api_readiness(path, sdk_available=False)
            self.assertEqual(readiness.status_code, "ctp_sdk_missing")
            self.assertIn("已阻止", readiness.status)

    def test_mock_gateway_is_clearly_research_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._write_config(Path(temp_dir), {"gateway": "mock"})
            readiness = assess_api_readiness(path, sdk_available=False)
            self.assertEqual(readiness.status_code, "mock")
            self.assertIn("接口工程测试", readiness.status)


if __name__ == "__main__":
    unittest.main()
