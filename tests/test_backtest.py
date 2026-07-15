import csv
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from futures_quant.broker.backtest import BacktestBroker, run_backtest
from futures_quant.config import load_backtest_config
from futures_quant.cli import fetch_history
from futures_quant.analysis.portfolio import analyze_batch_results
from futures_quant.analysis.report import generate_markdown_report
from futures_quant.data.contracts import load_contract_specs
from futures_quant.data.history import generate_synthetic_history, load_universe
from futures_quant.data.providers import HttpHistoryProvider, SyntheticHistoryProvider, build_provider
from futures_quant.data.quality import validate_bar_csv, validate_history_dir
from futures_quant.data.recorder import CsvBarRecorder
from futures_quant.data.source import CsvBarSource, GatewaySnapshotSource
from futures_quant.data.csv_loader import load_bars
from futures_quant.api.mock_gateway import MockGateway
from futures_quant.risk.rules import RiskEngine, RiskLimits
from futures_quant.strategies.dual_ma import DualMovingAverageStrategy
from futures_quant.strategies.base import Strategy
from futures_quant.models import Bar, Signal, Order


class BacktestSmokeTest(unittest.TestCase):
    def test_signal_executes_at_next_bar_open(self) -> None:
        class BuyFirstBar(Strategy):
            def __init__(self) -> None:
                self.sent = False

            def on_bar(self, bar: Bar) -> Signal | None:
                if self.sent:
                    return None
                self.sent = True
                return Signal(bar.datetime, bar.symbol, 1, "buy_first_close")

        bars = [
            Bar(pd.Timestamp("2026-01-02").to_pydatetime(), "T", 100, 101, 99, 100, 1),
            Bar(pd.Timestamp("2026-01-05").to_pydatetime(), "T", 110, 112, 109, 111, 1),
        ]
        risk = RiskEngine(RiskLimits(1.0, 1.0, 0.1, 0.1, 1))
        broker = BacktestBroker(1000, 0, 0, 1, 1, 0.1, risk)
        result = run_backtest(bars, BuyFirstBar(), broker)
        self.assertEqual(float(result.trades.iloc[0]["price"]), 110.0)
        self.assertEqual(broker.position("T").quantity, 0)
        self.assertEqual(len(result.trades), 2)

    def test_risk_engine_allows_position_reduction(self) -> None:
        risk = RiskEngine(RiskLimits(0.1, 0.1, 0.1, 0.1, 10))
        bar = Bar(pd.Timestamp("2026-01-02").to_pydatetime(), "T", 100, 101, 99, 100, 1)
        order = Order(bar.datetime, "T", -1, 100, "reduce")
        allowed, reason = risk.check_order(order, bar, equity=1000, current_margin=200, current_position=2)
        self.assertTrue(allowed)
        self.assertEqual(reason, "risk_reducing")

    def test_daily_loss_stop_blocks_only_increasing_orders(self) -> None:
        risk = RiskEngine(RiskLimits(1.0, 2.0, 0.05, 0.1, 10))
        broker = BacktestBroker(1000, 0, 0, 1, 10, 0.1, risk)
        bar = Bar(pd.Timestamp("2026-01-02 09:00").to_pydatetime(), "T", 100, 101, 99, 100, 1)
        marks = {"T": 100.0}
        broker.begin_bar(bar, marks)
        opened, _ = broker.submit_order(Order(bar.datetime, "T", 1, 100, "open"), bar, marks)
        self.assertTrue(opened)

        marks["T"] = 90.0
        blocked, reason = broker.submit_order(Order(bar.datetime, "T", 1, 90, "increase"), bar, marks)
        self.assertFalse(blocked)
        self.assertEqual(reason, "daily_loss_stop")

        reduced, _ = broker.submit_order(Order(bar.datetime, "T", -1, 90, "close"), bar, marks)
        self.assertTrue(reduced)

    def test_daily_loss_stop_blocks_same_size_reversal(self) -> None:
        risk = RiskEngine(RiskLimits(1.0, 2.0, 0.05, 0.1, 10))
        broker = BacktestBroker(1000, 0, 0, 1, 10, 0.1, risk)
        bar = Bar(pd.Timestamp("2026-01-02 09:00").to_pydatetime(), "T", 100, 101, 99, 100, 1)
        marks = {"T": 100.0}
        broker.begin_bar(bar, marks)
        broker.submit_order(Order(bar.datetime, "T", 1, 100, "open"), bar, marks)
        marks["T"] = 90.0
        blocked, reason = broker.submit_order(Order(bar.datetime, "T", -2, 90, "reverse"), bar, marks)
        self.assertFalse(blocked)
        self.assertEqual(reason, "daily_loss_stop")

    def test_sample_backtest_runs(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_backtest_config(root / "configs/backtest.json", root)
        bars = list(CsvBarSource(str(cfg.data.path)).bars())
        strategy = DualMovingAverageStrategy(
            cfg.strategy.fast_window,
            cfg.strategy.slow_window,
            cfg.strategy.order_size,
        )
        risk = RiskEngine(
            RiskLimits(
                cfg.max_margin_usage,
                cfg.max_symbol_exposure,
                cfg.daily_loss_stop,
                cfg.margin_rate,
                cfg.contract_multiplier,
            )
        )
        broker = BacktestBroker(
            cfg.initial_cash,
            cfg.commission_rate,
            cfg.slippage_ticks,
            cfg.tick_size,
            cfg.contract_multiplier,
            cfg.margin_rate,
            risk,
        )
        result = run_backtest(bars, strategy, broker)
        self.assertEqual(result.summary["status"], "ok")
        self.assertGreater(result.summary["trade_count"], 0)
        self.assertFalse(result.equity_curve.empty)

    def test_csv_source_matches_loader(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_backtest_config(root / "configs/backtest.json", root)
        direct = load_bars(cfg.data.path)
        sourced = list(CsvBarSource(str(cfg.data.path)).bars())
        self.assertEqual(len(direct), len(sourced))
        self.assertEqual(direct[0], sourced[0])

    def test_contract_specs_load(self) -> None:
        root = Path(__file__).resolve().parents[1]
        specs = load_contract_specs(root / "configs/contracts.csv")
        self.assertIn("RB2405", specs)
        self.assertEqual(specs["RB2405"].contract_multiplier, 10)
        self.assertEqual(specs["RB2405"].tick_size, 1.0)

    def test_recorder_round_trips_bars(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_backtest_config(root / "configs/backtest.json", root)
        bars = list(CsvBarSource(str(cfg.data.path)).bars())[:2]
        out = root / "reports/test_recorded_bars.csv"
        CsvBarRecorder(out).write(bars)
        loaded = list(CsvBarSource(str(out)).bars())
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0].symbol, bars[0].symbol)

    def test_gateway_replayed_bars_can_be_backtested(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_backtest_config(root / "configs/backtest.json", root)
        gateway = MockGateway()
        gateway.connect()
        gateway.subscribe(cfg.data.symbol)
        snapshot = GatewaySnapshotSource(gateway, [cfg.data.symbol])
        recorded = []
        for bar in CsvBarSource(str(cfg.data.path)).bars():
            gateway.push_bar(bar)
            recorded.extend(snapshot.bars())

        out = root / "reports/test_gateway_replay.csv"
        CsvBarRecorder(out).write(recorded)
        replayed = list(CsvBarSource(str(out)).bars())
        self.assertEqual(len(replayed), len(recorded))

        strategy = DualMovingAverageStrategy(
            cfg.strategy.fast_window,
            cfg.strategy.slow_window,
            cfg.strategy.order_size,
        )
        risk = RiskEngine(
            RiskLimits(
                cfg.max_margin_usage,
                cfg.max_symbol_exposure,
                cfg.daily_loss_stop,
                cfg.margin_rate,
                cfg.contract_multiplier,
            )
        )
        broker = BacktestBroker(
            cfg.initial_cash,
            cfg.commission_rate,
            cfg.slippage_ticks,
            cfg.tick_size,
            cfg.contract_multiplier,
            cfg.margin_rate,
            risk,
        )
        result = run_backtest(replayed, strategy, broker)
        self.assertEqual(result.summary["status"], "ok")
        self.assertGreater(result.summary["trade_count"], 0)

    def test_universe_generates_demo_history(self) -> None:
        root = Path(__file__).resolve().parents[1]
        universe = load_universe(root / "configs/universe.json")
        self.assertGreaterEqual(len(universe.instruments), 10)
        bars = generate_synthetic_history(universe.instruments[0], universe.start, universe.end)
        self.assertGreater(len(bars), 100)
        self.assertEqual(bars[0].symbol, universe.instruments[0].symbol)

    def test_synthetic_provider_fetches_standard_bars(self) -> None:
        root = Path(__file__).resolve().parents[1]
        universe = load_universe(root / "configs/universe.json")
        provider = build_provider("synthetic")
        self.assertIsInstance(provider, SyntheticHistoryProvider)
        bars = provider.fetch(universe.instruments[0], universe.start, universe.end)
        self.assertGreater(len(bars), 100)
        self.assertEqual(bars[0].symbol, universe.instruments[0].symbol)

    def test_http_csv_provider_maps_filters_and_sorts_bars_offline(self) -> None:
        root = Path(__file__).resolve().parents[1]
        instrument = load_universe(root / "configs/universe.json").instruments[0]
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source_path = temp / f"{instrument.symbol}.csv"
            source_path.write_text(
                "trade_date,o,h,l,c,vol,oi\n"
                "2025-01-03,12,13,11,12.5,120,22\n"
                "2024-12-31,9,10,8,9.5,90,19\n"
                "2025-01-02,10,12,9,11,100,20\n",
                encoding="utf-8",
            )
            config_path = temp / "http.json"
            config_path.write_text(
                json.dumps(
                    {
                        "url_template": source_path.as_uri().replace(instrument.symbol, "{symbol}"),
                        "format": "csv",
                        "field_mapping": {
                            "datetime": "trade_date",
                            "open": "o",
                            "high": "h",
                            "low": "l",
                            "close": "c",
                            "volume": "vol",
                            "open_interest": "oi",
                        },
                    }
                ),
                encoding="utf-8",
            )

            provider = build_provider("http", config_path)
            self.assertIsInstance(provider, HttpHistoryProvider)
            bars = provider.fetch(instrument, "2025-01-02", "2025-01-03")

        self.assertEqual(len(bars), 2)
        self.assertEqual([bar.datetime.date().isoformat() for bar in bars], ["2025-01-02", "2025-01-03"])
        self.assertEqual(bars[0].symbol, instrument.symbol)
        self.assertEqual(bars[0].open_interest, 20.0)

    def test_http_json_provider_reads_nested_rows_offline(self) -> None:
        root = Path(__file__).resolve().parents[1]
        instrument = load_universe(root / "configs/universe.json").instruments[0]
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source_path = temp / "bars.json"
            source_path.write_text(
                json.dumps(
                    {
                        "result": {
                            "items": [
                                {
                                    "time": "2025-01-02T15:00:00Z",
                                    "open": 10,
                                    "high": 12,
                                    "low": 9,
                                    "close": 11,
                                    "volume": 100,
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            config_path = temp / "http.json"
            config_path.write_text(
                json.dumps(
                    {
                        "url_template": source_path.as_uri(),
                        "format": "json",
                        "data_path": ["result", "items"],
                        "field_mapping": {"datetime": "time"},
                    }
                ),
                encoding="utf-8",
            )

            provider = build_provider("http", config_path)
            bars = provider.fetch(instrument, "2025-01-02", "2025-01-02")

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].datetime.isoformat(), "2025-01-02T15:00:00")

    def test_http_provider_requires_config(self) -> None:
        with self.assertRaisesRegex(ValueError, "provider_config"):
            build_provider("http")

    def test_fetch_manifest_records_provider_without_config_contents(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            provider_config = output_dir / "provider.json"
            provider_config.write_text('{"headers":{"Authorization":"DO_NOT_LEAK"}}', encoding="utf-8")
            rows = fetch_history(
                root / "configs/universe.json",
                output_dir,
                "synthetic",
                "_1d.csv",
                provider_config,
            )
            manifest_path = output_dir / "_fetch_manifest.csv"
            manifest_text = manifest_path.read_text(encoding="utf-8-sig")
            with manifest_path.open(newline="", encoding="utf-8-sig") as fh:
                manifest = list(csv.DictReader(fh))

        self.assertEqual(len(manifest), len(rows))
        self.assertTrue(all(row["provider"] == "synthetic" for row in manifest))
        self.assertTrue(all(row["provider_config"] == str(provider_config) for row in manifest))
        self.assertNotIn("DO_NOT_LEAK", manifest_text)

    def test_batch_analysis_outputs_reports(self) -> None:
        root = Path(__file__).resolve().parents[1]
        summary_path = root / "reports/multi_asset_api_summary.csv"
        if not summary_path.exists():
            self.skipTest("Batch summary has not been generated yet.")
        out = root / "reports/test_analysis"
        outputs = analyze_batch_results(summary_path, root / "reports", out)
        for path in outputs.values():
            self.assertTrue(path.exists())

    def test_markdown_report_generation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        analysis_dir = root / "reports/analysis"
        if not (analysis_dir / "portfolio_summary.csv").exists():
            self.skipTest("Analysis outputs have not been generated yet.")
        output = root / "reports/test_backtest_report.md"
        generate_markdown_report(analysis_dir, output, title="测试报告")
        self.assertTrue(output.exists())
        self.assertIn("测试报告", output.read_text(encoding="utf-8"))

    def test_history_quality_validation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_backtest_config(root / "configs/backtest.json", root)
        result = validate_bar_csv(cfg.data.path, cfg.data.symbol)
        self.assertEqual(result.status, "ok")
        out = root / "reports/test_data_quality_report.csv"
        results = validate_history_dir(root / "data/history_api", out, "_1d.csv")
        if results:
            self.assertTrue(out.exists())

    def test_history_quality_detects_ohlc_error(self) -> None:
        root = Path(__file__).resolve().parents[1]
        bad = root / "reports/bad_ohlc.csv"
        bad.write_text(
            "datetime,symbol,open,high,low,close,volume,open_interest\n"
            "2026-01-02,RB2405,10,9,8,11,100,0\n",
            encoding="utf-8",
        )
        result = validate_bar_csv(bad, "RB2405")
        self.assertEqual(result.status, "warning")
        self.assertIn("ohlc_relation_error", result.issues)

    def test_history_quality_report_writes_warning_status(self) -> None:
        root = Path(__file__).resolve().parents[1]
        bad_dir = root / "reports/bad_history"
        bad_dir.mkdir(parents=True, exist_ok=True)
        bad = bad_dir / "RB2405_1d.csv"
        bad.write_text(
            "datetime,symbol,open,high,low,close,volume,open_interest\n"
            "2026-01-02,RB2405,10,9,8,11,100,0\n",
            encoding="utf-8",
        )
        report = root / "reports/bad_history_quality.csv"
        results = validate_history_dir(bad_dir, report, "_1d.csv")
        self.assertEqual(results[0].status, "warning")
        self.assertTrue(report.exists())


if __name__ == "__main__":
    unittest.main()
