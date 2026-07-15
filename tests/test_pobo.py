from __future__ import annotations

import contextlib
import csv
import io
import json
import struct
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from futures_quant.cli import main
from futures_quant.data.pobo import (
    POBO_HEADER_SIZE,
    POBO_MAGIC,
    POBO_METADATA_OFFSET,
    POBO_RECORD_SIZE,
    audit_pobo_his,
    batch_import_pobo_data,
    import_pobo_his,
    load_pobo_name_table,
    map_pobo_symbol,
    read_pobo_his,
    read_pobo_server_bar_count,
)
from futures_quant.data.source import CsvBarSource


def write_name_table(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<Root Version="1.1">
  <Stocks Count="1">
    <Stock>
      <Name><![CDATA[螺纹主力]]></Name>
      <PBCode><![CDATA[010690]]></PBCode>
      <BRCode><![CDATA[rb_ZL]]></BRCode>
      <Parameter PriceRate="1000" Deci="0" />
    </Stock>
  </Stocks>
</Root>
""",
        encoding="utf-8",
    )


def make_record(
    timestamp: datetime,
    ohlc: tuple[float, float, float, float],
    volume: float,
    open_interest: float,
) -> bytes:
    record = bytearray(POBO_RECORD_SIZE)
    struct.pack_into(
        "<6i",
        record,
        0,
        timestamp.year,
        timestamp.month,
        timestamp.day,
        timestamp.hour,
        timestamp.minute,
        timestamp.second,
    )
    struct.pack_into("<4i", record, 28, *(round(price * 1000) for price in ohlc))
    struct.pack_into("<2d", record, 48, volume, open_interest)
    return bytes(record)


def write_his(
    path: Path, records: list[bytes], server_count: int | None = None
) -> None:
    header = bytearray(POBO_HEADER_SIZE)
    header[: len(POBO_MAGIC)] = POBO_MAGIC
    if server_count is not None:
        metadata = json.dumps(
            {"HisKLineCount": server_count}, separators=(",", ":")
        ).encode("utf-16-le")
        header[POBO_METADATA_OFFSET : POBO_METADATA_OFFSET + len(metadata)] = metadata
    path.write_bytes(bytes(header) + b"".join(records))


def write_multi_name_table(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<Root Version="1.1">
  <Stocks Count="2">
    <Stock>
      <Name><![CDATA[螺纹主力]]></Name>
      <PBCode><![CDATA[010690]]></PBCode>
      <BRCode><![CDATA[rb_ZL]]></BRCode>
      <Parameter PriceRate="1000" Deci="0" />
    </Stock>
    <Stock>
      <Name><![CDATA[螺纹连续]]></Name>
      <PBCode><![CDATA[010620]]></PBCode>
      <BRCode><![CDATA[rb_LX]]></BRCode>
      <Parameter PriceRate="1000" Deci="0" />
    </Stock>
  </Stocks>
</Root>
""",
        encoding="utf-8",
    )


class PoboImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.market_dir = self.root / "21005"
        self.his_dir = self.market_dir / "5Min"
        self.his_dir.mkdir(parents=True)
        self.name_table = self.market_dir / "NameTable.xml"
        self.his_path = self.his_dir / "010690.his"
        write_name_table(self.name_table)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_name_table_and_record_fields_are_decoded(self) -> None:
        write_his(
            self.his_path,
            [
                make_record(
                    datetime(2026, 7, 13, 14, 55),
                    (3054, 3057, 3054, 3056),
                    14178,
                    2158293,
                )
            ],
        )

        mappings = load_pobo_name_table(self.name_table)
        instrument, bars = read_pobo_his(self.his_path)

        self.assertEqual(mappings["010690"].name, "螺纹主力")
        self.assertEqual(instrument.br_code, "rb_ZL")
        self.assertEqual(instrument.price_rate, 1000)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].symbol, "rb_ZL")
        self.assertEqual(bars[0].datetime, datetime(2026, 7, 13, 14, 55))
        self.assertEqual(
            (bars[0].open, bars[0].high, bars[0].low, bars[0].close),
            (3054, 3057, 3054, 3056),
        )
        self.assertEqual(bars[0].volume, 14178)
        self.assertEqual(bars[0].open_interest, 2158293)

    def test_aggregation_never_crosses_session_gaps(self) -> None:
        observations = [
            (datetime(2026, 7, 13, 10, 5), (100, 103, 99, 102), 10, 1000),
            (datetime(2026, 7, 13, 10, 10), (102, 104, 101, 103), 20, 1001),
            (datetime(2026, 7, 13, 10, 15), (103, 105, 102, 104), 30, 1002),
            (datetime(2026, 7, 13, 10, 35), (110, 112, 109, 111), 30, 1010),
            (datetime(2026, 7, 13, 10, 40), (111, 114, 110, 113), 40, 1011),
            (datetime(2026, 7, 13, 10, 45), (113, 115, 112, 114), 50, 1012),
            (datetime(2026, 7, 13, 22, 50), (120, 122, 119, 121), 60, 1020),
            (datetime(2026, 7, 13, 22, 55), (121, 123, 120, 122), 70, 1021),
            (datetime(2026, 7, 13, 23, 0), (122, 124, 121, 123), 80, 1022),
            (datetime(2026, 7, 14, 9, 5), (130, 132, 129, 131), 80, 1030),
            (datetime(2026, 7, 14, 9, 10), (131, 134, 130, 133), 90, 1031),
            (datetime(2026, 7, 14, 9, 15), (133, 135, 132, 134), 100, 1032),
        ]
        write_his(
            self.his_path,
            [make_record(timestamp, ohlc, volume, oi) for timestamp, ohlc, volume, oi in observations],
        )
        output = self.root / "rb_ZL_15m.csv"

        result = import_pobo_his(self.his_path, output)
        bars = list(CsvBarSource(str(output)).bars())

        self.assertEqual(result.source_bar_count, 12)
        self.assertEqual(result.output_bar_count, 4)
        self.assertEqual([bar.datetime for bar in bars], [
            datetime(2026, 7, 13, 10, 15),
            datetime(2026, 7, 13, 10, 45),
            datetime(2026, 7, 13, 23, 0),
            datetime(2026, 7, 14, 9, 15),
        ])
        self.assertEqual(bars[0].volume, 60)
        self.assertEqual(bars[0].open_interest, 1002)
        self.assertEqual((bars[1].open, bars[1].high, bars[1].low, bars[1].close), (110, 115, 109, 114))
        self.assertEqual(bars[2].volume, 210)
        self.assertEqual(bars[3].volume, 270)

    def test_truncated_start_does_not_shift_clock_aligned_buckets(self) -> None:
        observations = [
            (datetime(2025, 10, 28, 10, 45), (100, 101, 99, 100), 5, 1000),
            (datetime(2025, 10, 28, 10, 50), (110, 111, 109, 110), 10, 1010),
            (datetime(2025, 10, 28, 10, 55), (110, 112, 109, 111), 20, 1011),
            (datetime(2025, 10, 28, 11, 0), (111, 113, 110, 112), 30, 1012),
        ]
        write_his(
            self.his_path,
            [make_record(timestamp, ohlc, volume, oi) for timestamp, ohlc, volume, oi in observations],
        )
        output = self.root / "aligned.csv"

        result = import_pobo_his(self.his_path, output)
        bars = list(CsvBarSource(str(output)).bars())

        self.assertEqual(result.output_bar_count, 1)
        self.assertEqual(bars[0].datetime, datetime(2025, 10, 28, 11, 0))
        self.assertEqual((bars[0].open, bars[0].close), (110, 112))
        self.assertEqual(bars[0].volume, 60)

    def test_cli_imports_with_symbol_override(self) -> None:
        write_his(
            self.his_path,
            [
                make_record(datetime(2026, 7, 13, 9, 5), (100, 102, 99, 101), 10, 1000),
                make_record(datetime(2026, 7, 13, 9, 10), (101, 103, 100, 102), 20, 1001),
                make_record(datetime(2026, 7, 13, 9, 15), (102, 104, 101, 103), 30, 1002),
            ],
        )
        output = self.root / "RB0_15m.csv"
        argv = [
            "fq",
            "import-pobo-his",
            "--input",
            str(self.his_path),
            "--output",
            str(output),
            "--symbol",
            "RB0",
        ]

        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            main()

        bars = list(CsvBarSource(str(output)).bars())
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].symbol, "RB0")
        sidecar = json.loads(
            output.with_suffix(output.suffix + ".source.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(sidecar["data_kind"], "real_market")
        self.assertEqual(sidecar["provider"], "pobo_local_cache")

    def test_invalid_payload_alignment_is_rejected(self) -> None:
        header = bytearray(POBO_HEADER_SIZE)
        header[: len(POBO_MAGIC)] = POBO_MAGIC
        self.his_path.write_bytes(bytes(header) + b"x")

        with self.assertRaisesRegex(ValueError, "aligned"):
            read_pobo_his(self.his_path)

    def test_header_server_count_and_file_audit_are_separate_from_cache(self) -> None:
        records = [
            make_record(
                datetime(2026, 7, 13, 9, minute),
                (100, 102, 99, 101),
                10,
                1000,
            )
            for minute in (5, 10, 15)
        ]
        write_his(self.his_path, records, server_count=30)

        audit = audit_pobo_his(self.his_path)

        self.assertEqual(read_pobo_server_bar_count(self.his_path), 30)
        self.assertEqual(audit.server_bar_count, 30)
        self.assertEqual(audit.source_bar_count, 3)
        self.assertEqual(audit.duplicate_timestamp_count, 0)
        self.assertEqual(len(audit.sha256), 64)

    def test_project_symbol_mapping_is_case_insensitive_and_series_limited(self) -> None:
        self.assertEqual(map_pobo_symbol("rb_ZL", {"RB0", "CU0"}), "RB0")
        self.assertEqual(map_pobo_symbol("RB2610", {"RB2610"}), "RB2610")
        self.assertIsNone(map_pobo_symbol("rb_L3", {"RB0"}))
        self.assertIsNone(map_pobo_symbol("cu_ZL", {"RB0"}))

    def test_batch_prefers_main_and_writes_auditable_manifest(self) -> None:
        write_multi_name_table(self.name_table)
        continuous_path = self.his_dir / "010620.his"
        records = [
            make_record(
                datetime(2026, 7, 13, 9, minute),
                (100 + index, 102 + index, 99 + index, 101 + index),
                10 + index,
                1000 + index,
            )
            for index, minute in enumerate((5, 10, 15))
        ]
        write_his(self.his_path, records, server_count=30)
        write_his(continuous_path, records, server_count=3)
        output_dir = self.root / "output"
        manifest_path = self.root / "manifest.csv"

        result = batch_import_pobo_data(
            self.root,
            output_dir,
            manifest_path,
            project_symbols={"RB0"},
            minimum_coverage_days=1095,
        )

        self.assertEqual(result.exported_symbols, ["RB0"])
        bars = list(CsvBarSource(str(output_dir / "RB0_15m.csv")).bars())
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].symbol, "RB0")
        rows = {row.br_code: row for row in result.rows}
        self.assertEqual(rows["rb_ZL"].status, "warning")
        self.assertEqual(rows["rb_ZL"].server_total_5m_bar_count, 30)
        self.assertIn("cache_truncated_at_start", rows["rb_ZL"].warnings)
        self.assertIn("coverage_below_required_days", rows["rb_ZL"].warnings)
        self.assertIn("main_contract_roll_rule_unknown", rows["rb_ZL"].warnings)
        self.assertEqual(rows["rb_LX"].status, "skipped")
        self.assertIn("alternate_series_not_selected:rb_ZL", rows["rb_LX"].warnings)
        with manifest_path.open(newline="", encoding="utf-8-sig") as fh:
            persisted = list(csv.DictReader(fh))
        self.assertEqual(len(persisted), 2)
        self.assertEqual(len(persisted[0]["sha256"]), 64)
        directory_source = json.loads(
            (output_dir / "_source_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(directory_source["data_kind"], "real_market")
        self.assertEqual(directory_source["exported_symbols"], ["RB0"])

    def test_batch_manifest_records_duplicate_timestamp_failure(self) -> None:
        record = make_record(
            datetime(2026, 7, 13, 9, 5),
            (100, 102, 99, 101),
            10,
            1000,
        )
        write_his(self.his_path, [record, record], server_count=2)

        result = batch_import_pobo_data(
            self.root,
            self.root / "output",
            self.root / "manifest.csv",
            project_symbols={"RB0"},
            minimum_coverage_days=1095,
        )

        self.assertEqual(result.rows[0].status, "error")
        self.assertEqual(result.rows[0].duplicate_timestamp_count, 1)
        self.assertIn("duplicate_timestamps:1", result.rows[0].warnings)
        self.assertIn("decode_error", result.rows[0].warnings)

    def test_batch_cli_uses_contract_and_universe_intersection(self) -> None:
        records = [
            make_record(
                datetime(2026, 7, 13, 9, minute),
                (100, 102, 99, 101),
                10,
                1000,
            )
            for minute in (5, 10, 15)
        ]
        write_his(self.his_path, records, server_count=3)
        contracts = self.root / "contracts.csv"
        contracts.write_text(
            "symbol,exchange,product,contract_multiplier,tick_size,margin_rate,commission_rate\n"
            "RB0,SHFE,rebar_continuous,10,1,0.12,0.00012\n",
            encoding="utf-8",
        )
        universe = self.root / "universe.json"
        universe.write_text(
            json.dumps(
                {
                    "start": "2023-01-01",
                    "end": "2026-01-01",
                    "instruments": [
                        {
                            "symbol": "RB0",
                            "name": "rebar",
                            "group": "SHFE",
                            "base_price": 3500,
                            "drift": 0,
                            "volatility": 0.01,
                            "seed": 1,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        output_dir = self.root / "cli_output"
        manifest = self.root / "cli_manifest.csv"
        argv = [
            "fq",
            "import-pobo-batch",
            "--data-root",
            str(self.root),
            "--output-dir",
            str(output_dir),
            "--manifest",
            str(manifest),
            "--contracts",
            str(contracts),
            "--universe",
            str(universe),
            "--minimum-coverage-days",
            "1",
        ]

        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            main()

        self.assertTrue((output_dir / "RB0_15m.csv").exists())
        self.assertTrue(manifest.exists())


if __name__ == "__main__":
    unittest.main()
