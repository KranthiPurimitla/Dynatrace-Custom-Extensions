"""
Phase 1 - Conversations.

Uses the synchronous Conversation Aggregates API:
    POST /api/v2/analytics/conversations/aggregates/query

Metrics are grouped by direction and mediaType. Duration metrics are reported as
interval averages in seconds; volume metrics are reported as delta counts.
"""

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from dynatrace_extension import MetricType

from genesyscloud import genesys_constants as constants
from genesyscloud.genesys_utils import (
    business_hours_hint,
    get_logger,
    last_full_minute_interval,
    ms_to_seconds,
    stat,
)


class ConversationsCollector:
    feature_set = constants.FS_CONVERSATIONS

    def __init__(self, client, org_name: str, region: str = ""):
        self.client = client
        self.org_name = org_name
        self.region = region
        self._utc_offset = constants.REGION_UTC_OFFSET_HOURS.get(region, 0.0)
        self.logger = get_logger("conversations")
        # High-water mark: end-time (UTC) of the latest per-minute bucket already
        # folded into a report. The query window is wider than the 5-minute
        # collection cadence (see CONV_QUERY_WINDOW_MINUTES) and therefore overlaps
        # across cycles; this mark is what keeps DELTA metrics from being counted
        # twice when the same bucket reappears in a later cycle's response.
        self._last_reported_end: datetime | None = None

    def collect(self, report: Callable):
        interval = last_full_minute_interval(
            lookback_minutes=constants.CONV_QUERY_WINDOW_MINUTES,
            offset_minutes=constants.CONV_AGGREGATE_LAG_MINUTES,
        )
        body = self.client.request(
            "POST",
            constants.PATH_CONVERSATION_AGGREGATES,
            json_body={
                "interval": interval,
                "granularity": "PT1M",
                "groupBy": ["direction", "mediaType"],
                "metrics": constants.GENESYS_CONV_METRICS,
            },
        )

        total = 0
        results = body.get("results", [])
        if not isinstance(results, list):
            results = []

        biz_hint = business_hours_hint(
            interval, self._utc_offset,
            constants.BUSINESS_HOURS_START, constants.BUSINESS_HOURS_END,
        )
        self.logger.info(
            "Queried interval %s; %s group(s) returned %s",
            interval, len(results), biz_hint,
        )
        self.logger.info("Raw aggregates response body: %s", json.dumps(body)[:1000])
        if not results:
            self.logger.warning(
                "No result groups for interval %s %s — "
                "possible causes: no traffic in this window, "
                "CONV_AGGREGATE_LAG_MINUTES look-back lag too small, "
                "or OAuth client missing the Analytics read scope.",
                interval, biz_hint,
            )

        since = self._last_reported_end or self._interval_start(interval)
        newest_end = since

        for group_result in results:
            if not isinstance(group_result, Mapping):
                continue

            group = group_result.get("group", {})
            direction = self._group_value(group, "direction")
            media_type = self._group_value(group, "mediaType")
            metrics, bucket_newest_end = self._aggregate_new_buckets(group_result.get("data", []), since)
            if bucket_newest_end > newest_end:
                newest_end = bucket_newest_end

            offered = int(stat(metrics.get("nOffered", {}), "count"))
            total += offered

            conversation_name = f"{direction}_{media_type}"
            dims = {
                constants.DIM_DIRECTION: direction,
                constants.DIM_MEDIA_TYPE: media_type,
                constants.DIM_ORG_NAME: self.org_name,
                constants.DIM_CONV_NAME: conversation_name,
            }

            if direction == "inbound":
                report(constants.M_CONV_INBOUND, offered, dimensions=dims, metric_type=MetricType.DELTA)
            elif direction == "outbound":
                report(constants.M_CONV_OUTBOUND, offered, dimensions=dims, metric_type=MetricType.DELTA)

            for genesys_field, metric_key in constants.CONV_DURATION_MAP.items():
                self._emit_avg_seconds(report, metric_key, metrics.get(genesys_field, {}), dims)

        self._last_reported_end = newest_end

        report(
            constants.M_CONV_COUNT,
            total,
            dimensions={constants.DIM_ORG_NAME: self.org_name},
            metric_type=MetricType.DELTA,
        )

        self.logger.info(
            "Reported %s conversations for interval %s (new buckets since %s) %s",
            total, interval, since.isoformat(), biz_hint,
        )

    @staticmethod
    def _group_value(group: object, key: str) -> str:
        if not isinstance(group, Mapping):
            return "unknown"
        value = group.get(key)
        return str(value) if value else "unknown"

    @staticmethod
    def _interval_start(interval: str) -> datetime:
        start_str, _, _ = interval.partition("/")
        return datetime.strptime(start_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)

    @staticmethod
    def _bucket_end(data_point: Mapping) -> datetime | None:
        bucket_interval = data_point.get("interval")
        if not isinstance(bucket_interval, str) or "/" not in bucket_interval:
            return None
        _, _, end_str = bucket_interval.partition("/")
        try:
            return datetime.strptime(end_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    @classmethod
    def _aggregate_new_buckets(
        cls, data_points: object, since: datetime
    ) -> tuple[dict[str, dict[str, float]], datetime]:
        """
        Sum count/sum stats across per-minute buckets newer than `since`.

        The query window overlaps across collection cycles (see
        CONV_QUERY_WINDOW_MINUTES), so each per-minute bucket would otherwise be
        re-counted in every cycle until it ages out of the window. Skipping
        buckets at or before `since` is what makes the DELTA metrics correct.
        """
        totals: dict[str, dict[str, float]] = {}
        newest_end = since
        if not isinstance(data_points, list):
            return totals, newest_end

        for data_point in data_points:
            if not isinstance(data_point, Mapping):
                continue

            bucket_end = cls._bucket_end(data_point)
            if bucket_end is None or bucket_end <= since:
                continue
            if bucket_end > newest_end:
                newest_end = bucket_end

            metrics = data_point.get("metrics", [])
            if not isinstance(metrics, list):
                continue

            for metric in metrics:
                if not isinstance(metric, Mapping):
                    continue

                metric_name = metric.get("metric")
                stats = metric.get("stats", {})
                if not isinstance(metric_name, str) or not isinstance(stats, Mapping):
                    continue

                bucket_totals = totals.setdefault(metric_name, {"count": 0.0, "sum": 0.0})
                bucket_totals["count"] += stat(stats, "count")
                bucket_totals["sum"] += stat(stats, "sum")

        return totals, newest_end

    @staticmethod
    def _emit_avg_seconds(report: Callable, key: str, stats: Mapping[str, Any], dims: dict[str, str]):
        count = stat(stats, "count")
        if not count:
            return

        avg_ms = stat(stats, "sum") / count
        report(key, ms_to_seconds(avg_ms), dimensions=dims, metric_type=MetricType.GAUGE)
