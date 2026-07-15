from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

from futures_quant.data.recorder import CsvBarRecorder
from futures_quant.data.timeframe import validate_intervals
from futures_quant.models import Bar


POBO_MAGIC = b"PoboHis"
POBO_HEADER_SIZE = 9_744
POBO_RECORD_SIZE = 96
POBO_SOURCE_MINUTES = 5
POBO_METADATA_OFFSET = 0x610
POBO_BATCH_TARGET_MINUTES = 15
POBO_DEFAULT_MINIMUM_COVERAGE_DAYS = 365.25 * 3
POBO_SERIES_SUFFIXES = {
    "ZL": "main",
    "LX": "continuous",
    "ZS": "weighted",
}
POBO_MANIFEST_FIELDS = [
    "source_file",
    "name_table",
    "pb_code",
    "br_code",
    "instrument_name",
    "series_kind",
    "symbol",
    "output_file",
    "source_5m_bar_count",
    "output_15m_bar_count",
    "first_datetime",
    "last_datetime",
    "server_total_5m_bar_count",
    "coverage_days",
    "duplicate_timestamp_count",
    "out_of_order_count",
    "gap_count",
    "max_gap_minutes",
    "leading_partial_5m_bar_count",
    "file_size",
    "file_mtime_utc",
    "sha256",
    "status",
    "warnings",
]


@dataclass(frozen=True)
class PoboInstrument:
    """Instrument metadata stored in a Pobo ``NameTable.xml`` file."""

    pb_code: str
    br_code: str
    name: str
    price_rate: float


@dataclass(frozen=True)
class PoboImportResult:
    instrument: PoboInstrument
    symbol: str
    source_bar_count: int
    output_bar_count: int
    output_path: Path


@dataclass(frozen=True)
class PoboHisAudit:
    """File-level facts that can be checked without trusting roll semantics."""

    source_bar_count: int
    server_bar_count: int | None
    first_datetime: datetime | None
    last_datetime: datetime | None
    coverage_days: float
    duplicate_timestamp_count: int
    out_of_order_count: int
    gap_count: int
    max_gap_minutes: float
    file_size: int
    file_mtime_utc: str
    sha256: str


@dataclass
class PoboManifestRow:
    source_file: str
    name_table: str = ""
    pb_code: str = ""
    br_code: str = ""
    instrument_name: str = ""
    series_kind: str = ""
    symbol: str = ""
    output_file: str = ""
    source_5m_bar_count: int = 0
    output_15m_bar_count: int = 0
    first_datetime: str = ""
    last_datetime: str = ""
    server_total_5m_bar_count: int | str = ""
    coverage_days: float = 0.0
    duplicate_timestamp_count: int = 0
    out_of_order_count: int = 0
    gap_count: int = 0
    max_gap_minutes: float = 0.0
    leading_partial_5m_bar_count: int = 0
    file_size: int = 0
    file_mtime_utc: str = ""
    sha256: str = ""
    status: str = "pending"
    warnings: str = ""


@dataclass(frozen=True)
class PoboBatchImportResult:
    rows: list[PoboManifestRow]
    exported_symbols: list[str]
    output_dir: Path
    manifest_path: Path


def load_pobo_name_table(path: str | Path) -> dict[str, PoboInstrument]:
    """Load PBCode -> instrument mappings from ``NameTable.xml``."""

    table_path = Path(path)
    try:
        root = ET.parse(table_path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"Invalid Pobo NameTable XML: {table_path}") from exc

    instruments: dict[str, PoboInstrument] = {}
    for stock in root.findall(".//Stock"):
        pb_code = (stock.findtext("PBCode") or "").strip()
        br_code = (stock.findtext("BRCode") or "").strip()
        name = (stock.findtext("Name") or "").strip()
        parameter = stock.find("Parameter")
        if not pb_code or not br_code or parameter is None:
            continue
        try:
            price_rate = float(parameter.attrib["PriceRate"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid PriceRate for PBCode {pb_code} in {table_path}"
            ) from exc
        if not math.isfinite(price_rate) or price_rate <= 0:
            raise ValueError(
                f"PriceRate must be positive for PBCode {pb_code}: {price_rate}"
            )
        instruments[pb_code] = PoboInstrument(
            pb_code=pb_code,
            br_code=br_code,
            name=name,
            price_rate=price_rate,
        )
    return instruments


def resolve_pobo_instrument(
    his_path: str | Path, name_table_path: str | Path | None = None
) -> PoboInstrument:
    """Resolve a ``.his`` file's PBCode using the adjacent name table."""

    source_path = Path(his_path)
    table_path = (
        Path(name_table_path)
        if name_table_path is not None
        else source_path.parent.parent / "NameTable.xml"
    )
    instruments = load_pobo_name_table(table_path)
    pb_code = source_path.stem
    try:
        return instruments[pb_code]
    except KeyError as exc:
        raise ValueError(
            f"PBCode {pb_code} from {source_path.name} was not found in {table_path}"
        ) from exc


def read_pobo_server_bar_count(path: str | Path) -> int | None:
    """Read Pobo's advertised history count from the UTF-16 header metadata.

    Pobo stores a small JSON fragment such as ``{"HisKLineCount":11707}``
    near header offset ``0x610``.  This is the server-advertised count for the
    selected series; it is deliberately kept separate from the number of
    records currently cached in the local file.
    """

    source_path = Path(path)
    with source_path.open("rb") as fh:
        header = fh.read(POBO_HEADER_SIZE)
    if len(header) < POBO_HEADER_SIZE:
        return None
    metadata = header[POBO_METADATA_OFFSET:].decode("utf-16-le", errors="ignore")
    match = re.search(r'"HisKLineCount"\s*:\s*(\d+)', metadata)
    return int(match.group(1)) if match else None


def audit_pobo_his(path: str | Path) -> PoboHisAudit:
    """Inspect record coverage, ordering, gaps, provenance, and header count."""

    source_path = Path(path)
    data = source_path.read_bytes()
    if len(data) < POBO_HEADER_SIZE:
        raise ValueError(
            f"PoboHis file is shorter than its {POBO_HEADER_SIZE}-byte header: "
            f"{source_path}"
        )
    if data[: len(POBO_MAGIC)] != POBO_MAGIC:
        raise ValueError(f"Invalid PoboHis signature: {source_path}")
    payload_size = len(data) - POBO_HEADER_SIZE
    if payload_size % POBO_RECORD_SIZE:
        raise ValueError(
            f"PoboHis payload is not aligned to {POBO_RECORD_SIZE}-byte records: "
            f"{source_path}"
        )

    timestamps: list[datetime] = []
    record_count = payload_size // POBO_RECORD_SIZE
    for index in range(record_count):
        offset = POBO_HEADER_SIZE + index * POBO_RECORD_SIZE
        try:
            timestamp_parts = struct.unpack_from("<6i", data, offset)
            timestamps.append(datetime(*timestamp_parts))
        except (struct.error, TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid datetime in PoboHis record {index}: {source_path}"
            ) from exc

    duplicate_count = len(timestamps) - len(set(timestamps))
    out_of_order_count = sum(
        current < previous
        for previous, current in zip(timestamps, timestamps[1:])
    )
    ordered_unique = sorted(set(timestamps))
    gaps = [
        (current - previous).total_seconds() / 60
        for previous, current in zip(ordered_unique, ordered_unique[1:])
        if current - previous > timedelta(minutes=POBO_SOURCE_MINUTES)
    ]
    first_datetime = ordered_unique[0] if ordered_unique else None
    last_datetime = ordered_unique[-1] if ordered_unique else None
    coverage_days = (
        (last_datetime - first_datetime).total_seconds() / 86_400
        if first_datetime is not None and last_datetime is not None
        else 0.0
    )
    stat = source_path.stat()
    return PoboHisAudit(
        source_bar_count=record_count,
        server_bar_count=_server_count_from_header(data[:POBO_HEADER_SIZE]),
        first_datetime=first_datetime,
        last_datetime=last_datetime,
        coverage_days=coverage_days,
        duplicate_timestamp_count=duplicate_count,
        out_of_order_count=out_of_order_count,
        gap_count=len(gaps),
        max_gap_minutes=max(gaps, default=0.0),
        file_size=stat.st_size,
        file_mtime_utc=datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def scan_pobo_his_files(data_root: str | Path) -> list[Path]:
    """Return every cached 5-minute Pobo history under a Data directory."""

    root = Path(data_root)
    if not root.is_dir():
        raise FileNotFoundError(f"Pobo Data directory does not exist: {root}")
    return sorted(
        (
            path
            for path in root.rglob("*.his")
            if path.is_file() and path.parent.name.casefold() == "5min"
        ),
        key=lambda path: str(path).casefold(),
    )


def classify_pobo_series(br_code: str) -> str:
    """Classify an actual, main, continuous, or weighted Pobo series."""

    match = re.fullmatch(r"[A-Za-z]+_([A-Za-z0-9]+)", br_code.strip())
    if match:
        return POBO_SERIES_SUFFIXES.get(match.group(1).upper(), "synthetic")
    if re.fullmatch(r"[A-Za-z]+\d+", br_code.strip()):
        return "actual_contract"
    return "other"


def map_pobo_symbol(
    br_code: str, project_symbols: Iterable[str]
) -> str | None:
    """Map a Pobo BRCode to an exact project contract or ``ROOT0`` series."""

    symbol_lookup = {str(symbol).upper(): str(symbol) for symbol in project_symbols}
    normalized = br_code.strip().upper()
    if normalized in symbol_lookup:
        return symbol_lookup[normalized]
    match = re.fullmatch(r"([A-Z]+)_(ZL|LX|ZS)", normalized)
    if not match:
        return None
    return symbol_lookup.get(f"{match.group(1)}0")


def _server_count_from_header(header: bytes) -> int | None:
    metadata = header[POBO_METADATA_OFFSET:].decode("utf-16-le", errors="ignore")
    match = re.search(r'"HisKLineCount"\s*:\s*(\d+)', metadata)
    return int(match.group(1)) if match else None


def read_pobo_his(
    path: str | Path,
    *,
    name_table_path: str | Path | None = None,
    symbol: str | None = None,
) -> tuple[PoboInstrument, list[Bar]]:
    """Read a Pobo 5-minute history file into normalized bars.

    The observed PoboHis layout uses a 9,744-byte header followed by fixed
    96-byte records. Prices are signed 32-bit integers divided by the
    instrument's ``PriceRate``. Volume and open interest are little-endian
    doubles at record offsets 48 and 56.
    """

    source_path = Path(path)
    instrument = resolve_pobo_instrument(source_path, name_table_path)
    output_symbol = symbol or instrument.br_code
    data = source_path.read_bytes()
    if len(data) < POBO_HEADER_SIZE:
        raise ValueError(
            f"PoboHis file is shorter than its {POBO_HEADER_SIZE}-byte header: "
            f"{source_path}"
        )
    if data[: len(POBO_MAGIC)] != POBO_MAGIC:
        raise ValueError(f"Invalid PoboHis signature: {source_path}")
    payload_size = len(data) - POBO_HEADER_SIZE
    if payload_size % POBO_RECORD_SIZE:
        raise ValueError(
            f"PoboHis payload is not aligned to {POBO_RECORD_SIZE}-byte records: "
            f"{source_path}"
        )

    bars: list[Bar] = []
    previous_datetime: datetime | None = None
    record_count = payload_size // POBO_RECORD_SIZE
    for index in range(record_count):
        offset = POBO_HEADER_SIZE + index * POBO_RECORD_SIZE
        try:
            year, month, day, hour, minute, second = struct.unpack_from(
                "<6i", data, offset
            )
            timestamp = datetime(year, month, day, hour, minute, second)
        except (struct.error, ValueError) as exc:
            raise ValueError(
                f"Invalid datetime in PoboHis record {index}: {source_path}"
            ) from exc

        if previous_datetime is not None and timestamp <= previous_datetime:
            raise ValueError(
                f"PoboHis records must have unique increasing timestamps; "
                f"record {index} is {timestamp}: {source_path}"
            )

        open_raw, high_raw, low_raw, close_raw = struct.unpack_from(
            "<4i", data, offset + 28
        )
        volume, open_interest = struct.unpack_from("<2d", data, offset + 48)
        prices = tuple(
            raw_price / instrument.price_rate
            for raw_price in (open_raw, high_raw, low_raw, close_raw)
        )
        open_price, high, low, close = prices
        if not all(math.isfinite(value) for value in (*prices, volume, open_interest)):
            raise ValueError(
                f"Non-finite market data in PoboHis record {index}: {source_path}"
            )
        if high < max(open_price, close) or low > min(open_price, close) or high < low:
            raise ValueError(
                f"Invalid OHLC relationship in PoboHis record {index}: {source_path}"
            )
        if volume < 0 or open_interest < 0:
            raise ValueError(
                f"Negative volume/open interest in PoboHis record {index}: "
                f"{source_path}"
            )

        bars.append(
            Bar(
                datetime=timestamp,
                symbol=output_symbol,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                open_interest=open_interest,
            )
        )
        previous_datetime = timestamp
    return instrument, bars


def import_pobo_his(
    input_path: str | Path,
    output_path: str | Path,
    *,
    name_table_path: str | Path | None = None,
    symbol: str | None = None,
    target_minutes: int = 15,
) -> PoboImportResult:
    """Convert Pobo 5-minute history to normalized session-safe CSV bars."""

    source_path = Path(input_path)
    instrument, source_bars = read_pobo_his(
        source_path,
        name_table_path=name_table_path,
        symbol=symbol,
    )
    output_symbol = symbol or instrument.br_code
    output_bars = aggregate_pobo_bars(
        source_bars,
        target_minutes=target_minutes,
    )
    resolved_output = Path(output_path)
    CsvBarRecorder(resolved_output).write(output_bars)
    _write_single_pobo_source_manifest(
        resolved_output,
        input_path=source_path,
        name_table_path=(
            Path(name_table_path)
            if name_table_path is not None
            else source_path.parent.parent / "NameTable.xml"
        ),
        instrument=instrument,
        symbol=output_symbol,
        source_bar_count=len(source_bars),
        output_bar_count=len(output_bars),
    )
    return PoboImportResult(
        instrument=instrument,
        symbol=output_symbol,
        source_bar_count=len(source_bars),
        output_bar_count=len(output_bars),
        output_path=resolved_output,
    )


def batch_import_pobo_data(
    data_root: str | Path,
    output_dir: str | Path,
    manifest_path: str | Path,
    *,
    project_symbols: Iterable[str],
    minimum_coverage_days: float = POBO_DEFAULT_MINIMUM_COVERAGE_DAYS,
    series_preference: Sequence[str] = ("ZL", "LX", "ZS"),
) -> PoboBatchImportResult:
    """Audit all Pobo 5-minute caches and export one 15-minute series/symbol.

    Only project symbols supplied by the caller are eligible for export.  If
    Pobo has multiple synthetic series for one project symbol, the default
    order selects main (``ZL``), then continuous (``LX``), then weighted
    (``ZS``).  Every discovered cache still receives a manifest row, including
    unmapped, invalid, and lower-priority files.
    """

    if minimum_coverage_days <= 0:
        raise ValueError("minimum_coverage_days must be positive.")
    preference = tuple(value.strip().upper() for value in series_preference)
    if not preference or any(
        value not in POBO_SERIES_SUFFIXES for value in preference
    ):
        raise ValueError("series_preference may contain only ZL, LX, and ZS.")
    if len(set(preference)) != len(preference):
        raise ValueError("series_preference cannot contain duplicates.")

    resolved_output_dir = Path(output_dir)
    resolved_manifest_path = Path(manifest_path)
    eligible_symbols = {str(symbol) for symbol in project_symbols}
    rows: list[PoboManifestRow] = []
    candidates: list[tuple[PoboManifestRow, list[Bar], list[str], int]] = []

    for source_path in scan_pobo_his_files(data_root):
        table_path = source_path.parent.parent / "NameTable.xml"
        row = PoboManifestRow(
            source_file=str(source_path.resolve()),
            name_table=str(table_path.resolve()),
            pb_code=source_path.stem,
        )
        rows.append(row)
        warnings: list[str] = []

        try:
            audit = audit_pobo_his(source_path)
            _populate_manifest_audit(row, audit)
        except (OSError, ValueError, struct.error) as exc:
            row.status = "error"
            _append_warning(warnings, f"file_audit_error:{exc}")
            row.warnings = ";".join(warnings)
            continue

        if audit.server_bar_count is None:
            _append_warning(warnings, "server_bar_count_unknown")
        elif audit.source_bar_count < audit.server_bar_count:
            missing = audit.server_bar_count - audit.source_bar_count
            _append_warning(warnings, f"cache_truncated_at_start:{missing}_bars_not_local")
        elif audit.source_bar_count > audit.server_bar_count:
            _append_warning(warnings, "local_bar_count_exceeds_server_header")
        if audit.coverage_days < minimum_coverage_days:
            _append_warning(
                warnings,
                f"coverage_below_required_days:{audit.coverage_days:.3f}"
                f"<{minimum_coverage_days:.3f}",
            )
        if audit.duplicate_timestamp_count:
            _append_warning(
                warnings,
                f"duplicate_timestamps:{audit.duplicate_timestamp_count}",
            )
        if audit.out_of_order_count:
            _append_warning(warnings, f"out_of_order_records:{audit.out_of_order_count}")
        if audit.gap_count:
            _append_warning(
                warnings,
                f"timestamp_gaps:{audit.gap_count}:scheduled_breaks_not_classified",
            )

        try:
            instrument = resolve_pobo_instrument(source_path, table_path)
            row.pb_code = instrument.pb_code
            row.br_code = instrument.br_code
            row.instrument_name = instrument.name
            row.series_kind = classify_pobo_series(instrument.br_code)
            mapped_symbol = map_pobo_symbol(instrument.br_code, eligible_symbols)
            row.symbol = mapped_symbol or ""
            _, source_bars = read_pobo_his(
                source_path,
                name_table_path=table_path,
                symbol=mapped_symbol or instrument.br_code,
            )
            row.leading_partial_5m_bar_count = _leading_partial_bar_count(
                source_bars, POBO_BATCH_TARGET_MINUTES
            )
            if row.leading_partial_5m_bar_count:
                _append_warning(
                    warnings,
                    "leading_partial_15m_bucket_discarded:"
                    f"{row.leading_partial_5m_bar_count}_bars",
                )
            output_bars = aggregate_pobo_bars(
                source_bars,
                target_minutes=POBO_BATCH_TARGET_MINUTES,
            )
            row.output_15m_bar_count = len(output_bars)
        except (OSError, ValueError, struct.error) as exc:
            row.status = "error"
            _append_warning(warnings, f"decode_error:{exc}")
            row.warnings = ";".join(warnings)
            continue

        _append_series_semantics_warning(warnings, row.series_kind)
        if mapped_symbol is None:
            row.status = "skipped"
            _append_warning(warnings, "not_in_project_contracts_and_universe")
            row.warnings = ";".join(warnings)
            continue
        if not output_bars:
            row.status = "error"
            _append_warning(warnings, "no_complete_15m_bars")
            row.warnings = ";".join(warnings)
            continue

        is_exact_contract = instrument.br_code.strip().upper() == mapped_symbol.upper()
        if is_exact_contract:
            rank = -1
        else:
            suffix = _pobo_series_suffix(instrument.br_code)
            rank = (
                preference.index(suffix)
                if suffix in preference
                else len(preference) + tuple(POBO_SERIES_SUFFIXES).index(suffix)
            )
        candidates.append((row, output_bars, warnings, rank))

    exported_symbols: list[str] = []
    by_symbol: dict[str, list[tuple[PoboManifestRow, list[Bar], list[str], int]]] = (
        defaultdict(list)
    )
    for candidate in candidates:
        by_symbol[candidate[0].symbol].append(candidate)

    for symbol in sorted(by_symbol):
        ranked = sorted(
            by_symbol[symbol],
            key=lambda item: (item[3], item[0].source_file.casefold()),
        )
        selected_row, selected_bars, selected_warnings, _ = ranked[0]
        output_path = resolved_output_dir / f"{symbol}_15m.csv"
        CsvBarRecorder(output_path).write(selected_bars)
        selected_row.output_file = str(output_path.resolve())
        selected_row.status = "warning" if selected_warnings else "ok"
        selected_row.warnings = ";".join(selected_warnings)
        exported_symbols.append(symbol)

        for alternate_row, _, alternate_warnings, _ in ranked[1:]:
            alternate_row.status = "skipped"
            _append_warning(
                alternate_warnings,
                f"alternate_series_not_selected:{selected_row.br_code}",
            )
            alternate_row.warnings = ";".join(alternate_warnings)

    _write_pobo_manifest(resolved_manifest_path, rows)
    _write_batch_pobo_source_manifest(
        resolved_output_dir,
        resolved_manifest_path,
        exported_symbols,
        minimum_coverage_days,
    )
    return PoboBatchImportResult(
        rows=rows,
        exported_symbols=exported_symbols,
        output_dir=resolved_output_dir,
        manifest_path=resolved_manifest_path,
    )


def _populate_manifest_audit(row: PoboManifestRow, audit: PoboHisAudit) -> None:
    row.source_5m_bar_count = audit.source_bar_count
    row.server_total_5m_bar_count = (
        audit.server_bar_count if audit.server_bar_count is not None else ""
    )
    row.first_datetime = (
        audit.first_datetime.isoformat(sep=" ") if audit.first_datetime else ""
    )
    row.last_datetime = (
        audit.last_datetime.isoformat(sep=" ") if audit.last_datetime else ""
    )
    row.coverage_days = round(audit.coverage_days, 6)
    row.duplicate_timestamp_count = audit.duplicate_timestamp_count
    row.out_of_order_count = audit.out_of_order_count
    row.gap_count = audit.gap_count
    row.max_gap_minutes = round(audit.max_gap_minutes, 6)
    row.file_size = audit.file_size
    row.file_mtime_utc = audit.file_mtime_utc
    row.sha256 = audit.sha256


def _write_pobo_manifest(path: Path, rows: list[PoboManifestRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=POBO_MANIFEST_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def _write_single_pobo_source_manifest(
    output_path: Path,
    *,
    input_path: Path,
    name_table_path: Path,
    instrument: PoboInstrument,
    symbol: str,
    source_bar_count: int,
    output_bar_count: int,
) -> None:
    """Attach non-secret provenance to a single exported CSV."""

    sidecar = output_path.with_suffix(output_path.suffix + ".source.json")
    payload = {
        "data_kind": "real_market",
        "provider": "pobo_local_cache",
        "source_format": "PoboHis",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_file": str(input_path.resolve()),
        "name_table": str(name_table_path.resolve()),
        "pb_code": instrument.pb_code,
        "br_code": instrument.br_code,
        "symbol": symbol,
        "source_5m_bar_count": source_bar_count,
        "output_15m_bar_count": output_bar_count,
    }
    sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_batch_pobo_source_manifest(
    output_dir: Path,
    manifest_path: Path,
    exported_symbols: list[str],
    minimum_coverage_days: float,
) -> None:
    """Attach a directory-level provenance marker for the workbench/UI."""

    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "data_kind": "real_market",
        "provider": "pobo_local_cache",
        "source_format": "PoboHis",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "audit_manifest": str(manifest_path.resolve()),
        "exported_symbols": sorted(exported_symbols),
        "minimum_coverage_days": minimum_coverage_days,
        "coverage_notice": (
            "The audit manifest, not this marker, determines whether individual "
            "symbols meet the requested history coverage."
        ),
    }
    (output_dir / "_source_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _append_warning(warnings: list[str], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)


def _append_series_semantics_warning(warnings: list[str], series_kind: str) -> None:
    if series_kind == "main":
        _append_warning(warnings, "main_contract_roll_rule_unknown")
    elif series_kind == "continuous":
        _append_warning(warnings, "continuous_roll_adjustment_unknown")
    elif series_kind == "weighted":
        _append_warning(warnings, "weighted_index_construction_unknown")


def _pobo_series_suffix(br_code: str) -> str:
    match = re.fullmatch(r"[A-Za-z]+_([A-Za-z0-9]+)", br_code.strip())
    if match is None or match.group(1).upper() not in POBO_SERIES_SUFFIXES:
        raise ValueError(f"Unsupported Pobo synthetic series: {br_code}")
    return match.group(1).upper()


def _leading_partial_bar_count(bars: list[Bar], target_minutes: int) -> int:
    if not bars:
        return 0
    expected_size = target_minutes // POBO_SOURCE_MINUTES
    max_contiguous_gap = timedelta(minutes=POBO_SOURCE_MINUTES * 1.5)
    ordered = sorted(bars, key=lambda item: item.datetime)
    bucket: list[Bar] = []
    previous: Bar | None = None
    for bar in ordered:
        if previous is not None and bar.datetime - previous.datetime > max_contiguous_gap:
            return len(bucket) if len(bucket) < expected_size else 0
        bucket.append(bar)
        minutes_since_midnight = bar.datetime.hour * 60 + bar.datetime.minute
        if minutes_since_midnight % target_minutes == 0:
            return len(bucket) if len(bucket) < expected_size else 0
        previous = bar
    return len(bucket) if len(bucket) < expected_size else 0


def aggregate_pobo_bars(
    bars: list[Bar], target_minutes: int = 15
) -> list[Bar]:
    """Aggregate end-stamped Pobo bars on clock-aligned session boundaries.

    A Pobo cache can begin part-way through a session. Grouping every three
    rows would then permanently shift 15-minute bars. Instead, a bucket closes
    only on the natural wall-clock boundary (for example 10:45 or 11:00), and
    any market-data gap longer than one and a half source intervals cuts the
    session first. The first under-filled bucket is discarded because bars
    before the file's starting point cannot be recovered or verified.
    """

    validate_intervals(POBO_SOURCE_MINUTES, target_minutes)
    expected_size = target_minutes // POBO_SOURCE_MINUTES
    max_contiguous_gap = timedelta(minutes=POBO_SOURCE_MINUTES * 1.5)
    by_symbol: dict[str, list[Bar]] = defaultdict(list)
    for bar in sorted(bars, key=lambda item: (item.symbol, item.datetime)):
        by_symbol[bar.symbol].append(bar)

    output: list[Bar] = []
    for symbol in sorted(by_symbol):
        bucket: list[Bar] = []
        previous: Bar | None = None
        is_first_bucket = True
        for bar in by_symbol[symbol]:
            if previous is not None:
                gap = bar.datetime - previous.datetime
                if gap <= timedelta(0):
                    raise ValueError(
                        f"Bars for {symbol} must have unique increasing timestamps."
                    )
                if gap > max_contiguous_gap:
                    _flush_pobo_bucket(
                        bucket,
                        output,
                        discard=is_first_bucket and len(bucket) < expected_size,
                    )
                    if bucket:
                        is_first_bucket = False
                    bucket = []

            bucket.append(bar)
            minutes_since_midnight = bar.datetime.hour * 60 + bar.datetime.minute
            if minutes_since_midnight % target_minutes == 0:
                _flush_pobo_bucket(
                    bucket,
                    output,
                    discard=is_first_bucket and len(bucket) < expected_size,
                )
                is_first_bucket = False
                bucket = []
            previous = bar

        _flush_pobo_bucket(
            bucket,
            output,
            discard=is_first_bucket and len(bucket) < expected_size,
        )
    return sorted(output, key=lambda item: (item.datetime, item.symbol))


def _flush_pobo_bucket(
    bucket: list[Bar], output: list[Bar], *, discard: bool = False
) -> None:
    if not bucket or discard:
        return
    first = bucket[0]
    last = bucket[-1]
    output.append(
        Bar(
            datetime=last.datetime,
            symbol=first.symbol,
            open=first.open,
            high=max(bar.high for bar in bucket),
            low=min(bar.low for bar in bucket),
            close=last.close,
            volume=sum(bar.volume for bar in bucket),
            open_interest=last.open_interest,
        )
    )
