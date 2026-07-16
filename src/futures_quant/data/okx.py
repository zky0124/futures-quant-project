from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from futures_quant.api.okx_rest import OkxPublicClient
from futures_quant.data.recorder import CsvBarRecorder
from futures_quant.models import Bar


@dataclass(frozen=True)
class OkxHistoryDownload:
    inst_id: str
    bar: str
    output_path: Path
    page_count: int
    bar_count: int
    first_datetime: datetime
    last_datetime: datetime


def okx_candles_to_bars(rows: Sequence[object], inst_id: str) -> list[Bar]:
    """Convert confirmed OKX candle arrays to ascending UTC-naive Bars."""

    by_timestamp: dict[int, Bar] = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            raise ValueError("OKX candle row must contain at least nine fields.")
        if str(row[8]) != "1":
            continue
        try:
            timestamp_ms = int(str(row[0]))
            opened = float(row[1])
            high = float(row[2])
            low = float(row[3])
            close = float(row[4])
            volume = float(row[5])
        except (TypeError, ValueError) as exc:
            raise ValueError("OKX candle contains a non-numeric field.") from exc
        timestamp = datetime.fromtimestamp(
            timestamp_ms / 1000, tz=timezone.utc
        ).replace(tzinfo=None)
        by_timestamp[timestamp_ms] = Bar(
            datetime=timestamp,
            symbol=inst_id,
            open=opened,
            high=high,
            low=low,
            close=close,
            volume=volume,
            open_interest=0.0,
        )
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def download_okx_history(
    client: OkxPublicClient,
    *,
    inst_id: str,
    bar: str,
    output_path: str | Path,
    start: datetime,
    max_pages: int = 200,
    page_limit: int = 300,
    request_delay_seconds: float = 0.11,
    sleeper: Callable[[float], None] = time.sleep,
) -> OkxHistoryDownload:
    """Download confirmed candles backwards, deduplicate, and save standard CSV."""

    if start.tzinfo is None:
        start_utc = start.replace(tzinfo=timezone.utc)
    else:
        start_utc = start.astimezone(timezone.utc)
    start_ms = int(start_utc.timestamp() * 1000)
    if max_pages <= 0:
        raise ValueError("max_pages must be positive.")
    if not 1 <= page_limit <= 300:
        raise ValueError("page_limit must be between 1 and 300.")
    if request_delay_seconds < 0:
        raise ValueError("request_delay_seconds cannot be negative.")

    all_rows: dict[int, object] = {}
    after = ""
    previous_oldest: int | None = None
    page_count = 0
    for page_number in range(max_pages):
        rows = client.get_candles(
            inst_id,
            bar=bar,
            after=after,
            limit=page_limit,
            history=True,
        )
        page_count += 1
        timestamps: list[int] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or not row:
                raise ValueError("OKX candle history returned an invalid row.")
            try:
                timestamp_ms = int(str(row[0]))
            except ValueError as exc:
                raise ValueError("OKX candle timestamp is not numeric.") from exc
            timestamps.append(timestamp_ms)
            if timestamp_ms >= start_ms:
                all_rows[timestamp_ms] = row
        if not timestamps:
            break
        oldest = min(timestamps)
        if oldest <= start_ms or oldest == previous_oldest:
            break
        previous_oldest = oldest
        after = str(oldest)
        if page_number + 1 < max_pages and request_delay_seconds:
            sleeper(request_delay_seconds)

    bars = okx_candles_to_bars(list(all_rows.values()), inst_id)
    if not bars:
        raise ValueError("OKX returned no confirmed candles in the requested range.")
    output = Path(output_path)
    CsvBarRecorder(output).write(bars)
    manifest = output.with_suffix(output.suffix + ".source.json")
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "data_kind": "real_market",
                "provider": "okx_public_rest",
                "market": "crypto",
                "instrument_id": inst_id,
                "bar": bar,
                "timezone": "UTC",
                "first_datetime": bars[0].datetime.isoformat(),
                "last_datetime": bars[-1].datetime.isoformat(),
                "bar_count": len(bars),
                "page_count": page_count,
                "downloaded_at": datetime.now(timezone.utc).isoformat(),
                "warning": (
                    "OKX crypto-market data is separate from domestic futures data; "
                    "the current unconfirmed candle was excluded."
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return OkxHistoryDownload(
        inst_id=inst_id,
        bar=bar,
        output_path=output,
        page_count=page_count,
        bar_count=len(bars),
        first_datetime=bars[0].datetime,
        last_datetime=bars[-1].datetime,
    )
