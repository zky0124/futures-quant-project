from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from futures_quant.models import Bar


SUPPORTED_INTERVAL_MINUTES = (5, 15, 30, 60, 120, 240)


def validate_intervals(source_minutes: int, target_minutes: int) -> None:
    if source_minutes <= 0 or target_minutes <= 0:
        raise ValueError("Bar intervals must be positive minutes.")
    if target_minutes < source_minutes:
        raise ValueError("Target bar interval cannot be smaller than source interval.")
    if target_minutes % source_minutes:
        raise ValueError("Target bar interval must be a multiple of source interval.")


def aggregate_bars(
    bars: list[Bar], target_minutes: int, source_minutes: int = 15
) -> list[Bar]:
    """Aggregate bars without crossing market-session gaps.

    Bars are timestamped at the end of their interval. A gap larger than one
    and a half source intervals starts a new session segment. A session's
    final incomplete bucket is retained so midday and close data are not lost.
    """

    validate_intervals(source_minutes, target_minutes)
    ordered = sorted(bars, key=lambda bar: (bar.symbol, bar.datetime))
    if target_minutes == source_minutes:
        return ordered

    bucket_size = target_minutes // source_minutes
    max_contiguous_gap = timedelta(minutes=source_minutes * 1.5)
    by_symbol: dict[str, list[Bar]] = defaultdict(list)
    for bar in ordered:
        by_symbol[bar.symbol].append(bar)

    aggregated: list[Bar] = []
    for symbol in sorted(by_symbol):
        bucket: list[Bar] = []
        previous: Bar | None = None
        for bar in by_symbol[symbol]:
            if previous is not None:
                gap = bar.datetime - previous.datetime
                if gap <= timedelta(0):
                    raise ValueError(
                        f"Bars for {symbol} must have unique increasing timestamps."
                    )
                if gap > max_contiguous_gap:
                    _flush_bucket(bucket, aggregated)
                    bucket = []
            bucket.append(bar)
            if len(bucket) == bucket_size:
                _flush_bucket(bucket, aggregated)
                bucket = []
            previous = bar
        _flush_bucket(bucket, aggregated)

    return sorted(aggregated, key=lambda bar: (bar.datetime, bar.symbol))


def _flush_bucket(bucket: list[Bar], output: list[Bar]) -> None:
    if not bucket:
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
