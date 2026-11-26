"""
Phase 0 - Platform Health (self-monitoring).

Reports on the extension's own interaction with the Genesys API. It makes no API
calls of its own: it drains the telemetry the shared GenesysClient accumulated
during this collection cycle, so the numbers reflect the real calls Phase 1 (and
later phases) just made.

Metrics:
    genesys.api.response.time   average API latency this cycle (ms, gauge)
    genesys.api.calls           number of API calls this cycle (delta)
    genesys.api.errors          failed calls this cycle (delta)
    genesys.api.auth.failures   auth/token failures this cycle (delta)
    genesys.api.rate.limit      HTTP 429 responses this cycle (delta)
"""

from dynatrace_extension import MetricType

from genesyscloud import genesys_constants as constants
from genesyscloud.genesys_utils import get_logger


class PlatformHealthCollector:
    feature_set = constants.FS_PLATFORM

    def __init__(self, client, org_name: str):
        self.client = client
        self.org_name = org_name
        self.logger = get_logger("platform_health")

    def collect(self, report):
        snap = self.client.telemetry.drain()
        dims = {constants.DIM_ORG_NAME: self.org_name}

        latencies = snap["latencies_ms"]
        if latencies:
            avg_ms = sum(latencies) / len(latencies)
            report(constants.M_API_RESPONSE_TIME, avg_ms, dimensions=dims, metric_type=MetricType.GAUGE)

        # Per-cycle counts use DELTA so Dynatrace sums them across a timeframe.
        report(constants.M_API_CALLS, snap["calls"], dimensions=dims, metric_type=MetricType.DELTA)
        report(constants.M_API_ERRORS, snap["errors"], dimensions=dims, metric_type=MetricType.DELTA)
        report(
            constants.M_API_AUTH_FAILURES,
            snap["auth_failures"],
            dimensions=dims,
            metric_type=MetricType.DELTA,
        )
        report(
            constants.M_API_RATE_LIMIT,
            snap["rate_limited"],
            dimensions=dims,
            metric_type=MetricType.DELTA,
        )

        self.logger.info(
            "Platform health: calls=%s avg_latency=%.1fms errors=%s 429s=%s auth_fail=%s",
            snap["calls"],
            (sum(latencies) / len(latencies)) if latencies else 0.0,
            snap["errors"],
            snap["rate_limited"],
            snap["auth_failures"],
        )
