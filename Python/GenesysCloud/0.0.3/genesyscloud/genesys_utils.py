"""Pure helper functions and logging setup. No HTTP, no SDK coupling."""

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any


def get_logger(name: str) -> logging.Logger:
    """
    Namespaced child of the SDK's root logger.

    The SDK configures handlers that ship records to the EEC, so we only
    namespace here (e.g. 'genesys.conversations') for attribution. We set the
    level explicitly because this logger has no ancestor with a level of its
    own; without this, effective level resolution falls through to Python's
    default root level (WARNING) and every .info() call is silently dropped,
    even though a handler is attached and would otherwise print it.
    """
    logger = logging.getLogger(f"genesys.{name}")
    logger.setLevel(logging.INFO)
    return logger


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def last_full_minute_interval(lookback_minutes: int = 1, offset_minutes: int = 0) -> str:
    """
    ISO-8601 interval string for a closed, minute-aligned window.

    Genesys expects e.g. 2026-06-26T10:00:00.000Z/2026-06-26T10:01:00.000Z.

    offset_minutes shifts the window further into the past (in addition to
    lookback_minutes) to absorb Genesys's conversation-aggregate finalization
    lag: the most recently closed minute is frequently not yet queryable, so
    querying it on every cycle yields empty results. The window width itself
    must stay equal to the collection cadence (1 minute) so consecutive cycles
    don't overlap and double-count DELTA metrics.
    """
    end = utc_now().replace(second=0, microsecond=0) - timedelta(minutes=offset_minutes)
    start = end - timedelta(minutes=lookback_minutes)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return f"{start.strftime(fmt)}/{end.strftime(fmt)}"


def stat(stats: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    """Read one statistic (count/sum/...) from a Genesys stats block safely."""
    if not stats:
        return default
    try:
        return float(stats.get(key, default))
    except (TypeError, ValueError):
        return default


def ms_to_seconds(milliseconds: float) -> float:
    return milliseconds / 1000.0


def safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def business_hours_hint(
    interval: str,
    utc_offset_hours: float,
    biz_start: int = 8,
    biz_end: int = 18,
) -> str:
    """
    Returns a one-line hint about whether the midpoint of an ISO-8601 query
    interval falls within typical business hours for the Genesys region.

    Uses a fixed UTC offset (standard time) — DST is not accounted for, but
    the hint is informational only and close enough for diagnostic purposes.

    Returns an empty string on any parse failure so callers can safely append
    it to log messages without extra guarding.
    """
    try:
        start_str, _, end_str = interval.partition("/")
        start = datetime.strptime(start_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        end   = datetime.strptime(end_str,   "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        mid   = start + (end - start) / 2
        local = mid + timedelta(hours=utc_offset_hours)
        local_decimal_hour = local.hour + local.minute / 60.0

        # Format UTC offset as "UTC+1", "UTC-8", "UTC+5:30", etc.
        total_minutes = int(abs(utc_offset_hours) * 60)
        h, m = divmod(total_minutes, 60)
        sign = "+" if utc_offset_hours >= 0 else "-"
        tz_label = f"UTC{sign}{h}:{m:02d}" if m else f"UTC{sign}{h}"
        local_str = f"{local.strftime('%H:%M')} {tz_label}"

        if biz_start <= local_decimal_hour < biz_end:
            return f"[region local time {local_str} — within business hours]"
        return (
            f"[region local time {local_str} — outside business hours "
            f"{biz_start:02d}:00-{biz_end:02d}:00 {tz_label}, empty results may be expected]"
        )
    except Exception:  # noqa: BLE001
        return ""
