"""
VeloCloud Extension for Dynatrace.

Collects edge metrics from VeloCloud API including link statistics and health metrics.
Supports multi-endpoint configuration with automatic retry and rate-limit handling.
"""

import asyncio
import ssl
import sys
import time
from datetime import timedelta
from typing import Optional, Dict, Any, List
from pathlib import Path

import certifi
from aiohttp import (
    ClientError,
    ClientResponseError,
    ClientSession,
    TCPConnector,
)
from dynatrace_extension import Extension, Status, StatusValue

# Add current directory to path to ensure api_client can be imported
sys.path.insert(0, str(Path(__file__).parent))

try:
    from api_client import (
        QueryBudgetExceeded,
        VeloCloudApiError,
        VeloCloudClient,
    )
except ImportError:
    from .api_client import (
        QueryBudgetExceeded,
        VeloCloudApiError,
        VeloCloudClient,
    )

# Configuration Constants
# The cycle runs on its own schedule (see initialize). The Dynatrace SDK marks a
# callback as an error if it runs longer than its interval, so the cycle must
# finish inside CYCLE_INTERVAL_MINUTES.
CYCLE_INTERVAL_MINUTES = 5
# Seconds of API calls one cycle may plan (at 0.5 req/s this is about 120 calls).
# Calls are never cancelled mid-flight, so the cycle finishes inside the timeout.
QUERY_BUDGET_SECONDS = 240
CYCLE_TIMEOUT_SECONDS = 270  # hard stop, below the 300 s interval
FASTCHECK_TIMEOUT = 20
DEFAULT_API_HOST = "https://vco61-usvi1.velocloud.net"


def sanitize_value(value: Any) -> Optional[Any]:
    """
    Sanitize metric value for reporting.

    Args:
        value: Raw metric value

    Returns:
        Sanitized value (float rounded to 2 decimals, int, or str)
    """
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return str(value)
    return None


def sanitize_dimension_value(value: Any) -> str:
    """
    Sanitize dimension value (max 255 characters).

    Args:
        value: Dimension value

    Returns:
        Sanitized string (max 255 chars)
    """
    return str(value)[:255]


class ExtensionImpl(Extension):
    """
    Dynatrace VeloCloud Extension implementation.

    Collects metrics from VeloCloud edges and reports them to Dynatrace.
    """

    def __init__(self) -> None:
        """Initialize extension with SSL context for secure connections."""
        super().__init__()
        self.ssl_context = ssl.create_default_context(
            cafile=certifi.where()
        )
        self.client = VeloCloudClient(self.logger)
        # Last time (epoch seconds) each edge was polled, for rotation
        self._last_polled: Dict[str, float] = {}
        # Outcome of the current cycle, read by query() for its final status
        self._cycle_errors = 0
        self._cycle_skipped = False
        # Calls left out of the cycle because the query budget ran out
        self._budget_skips = 0
        # Edges whose calls failed in the last cycle; polled first next cycle
        self._failed_edges: set = set()
        # Cycle counter, and whether this cycle includes health stats
        self._cycle_no = 0
        self._with_health = True
        self._health_every = 5
        self.logger.info("VeloCloud Extension initialized")

    def _read_int(self, key: str, default: int, lo: int, hi: int) -> int:
        """Read an integer setting; fall back to the default if missing or out of range."""
        raw = self.activation_config.get(key)
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            self.logger.warning(f"{key}={raw!r} is not a number; using {default}")
            return default
        if not lo <= value <= hi:
            self.logger.warning(f"{key}={value} is outside {lo}-{hi}; using {default}")
            return default
        return value

    async def async_query(self) -> None:
        """
        Execute async query to collect metrics from VeloCloud endpoints.

        This is the main data collection method that:
        1. Retrieves enterprise configuration
        2. Lists all edges
        3. Collects link and health statistics for each edge
        4. Reports metrics to Dynatrace

        Uses semaphore to limit concurrent requests and respects
        overall timeout to ensure completion within 1 minute.
        """
        self.logger.info("Query method started for VeloCloud metrics")
        self._cycle_errors = 0
        self._cycle_skipped = False
        self._budget_skips = 0

        # Per-cycle settings: the call budget, and how often health stats are fetched
        self._cycle_no += 1
        budget_per_minute = self._read_int("rate_budget_per_minute", 30, 1, 120)
        self.client.pacer.set_ceiling(budget_per_minute / 60.0)
        self._health_every = self._read_int("health_every_n_cycles", 5, 1, 60)
        self._with_health = (self._cycle_no - 1) % self._health_every == 0

        # After retries ran out on a 429, make no calls for a cooldown period.
        # Each call during a block can extend it, so skipping is the safe option.
        if self.client.is_blocked():
            remaining = self.client.blocked_until - time.monotonic()
            self.logger.warning(
                f"VeloCloud API is rate limited; skipping this cycle. "
                f"Calls resume in about {remaining / 60:.0f} min."
            )
            self._cycle_skipped = True
            return

        endpoints = self.activation_config.get("endpoints", [])
        if not isinstance(endpoints, list):
            endpoints = []
        self.logger.debug(f"Configured endpoints: {len(endpoints)}")

        # PROBLEM FIX 4 & 5: Validate configuration before processing
        if not endpoints:
            self.logger.error(
                "❌ No endpoints configured in activation.json. "
                "Expected 'pythonLocal.endpoints' with 'api_host' and 'api_token'. "
                "Ensure activation.json has correct structure."
            )
            return

        # Concurrency stays low; the client's RequestPacer sets the real pace.
        # Each cycle only plans as many edges as the time budget allows, so
        # no call is cancelled by an overall timeout (that wasted quota).
        semaphore = asyncio.Semaphore(1)

        # No call may start after the budget ends, so the cycle finishes on
        # its own instead of being cancelled by the 55 s guard in query()
        self.client.deadline = time.monotonic() + QUERY_BUDGET_SECONDS
        try:
            for endpoint in endpoints:
                await self._process_endpoint(endpoint, semaphore)

        except Exception as e:
            self._cycle_errors += 1
            self.logger.error(f"Unexpected error in async_query: {e}")
        finally:
            self.client.deadline = None
            if self._budget_skips:
                self.logger.info(
                    f"{self._budget_skips} API call(s) left for the next cycle: "
                    f"query budget used up"
                )

    def _select_edges(self, edges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Choose the edges that fit this cycle's call budget, failed edges first.

        The budget is the configured ceiling (rate_budget_per_minute). Link-only
        cycles spend one call per edge, health cycles two. Edges are marked as
        polled only after a successful fetch (see the collectors).

        Args:
            edges: Edges with logicalId and name set

        Returns:
            Edges to poll in this cycle
        """
        budget_rps = self.client.pacer.max_rate
        calls_per_edge = 2 if self._with_health else 1
        per_cycle = max(1, int(QUERY_BUDGET_SECONDS * budget_rps / calls_per_edge))
        ordered = sorted(
            edges,
            key=lambda e: (
                str(e.get("logicalId")) not in self._failed_edges,
                self._last_polled.get(str(e.get("logicalId")), 0.0),
            ),
        )
        chosen = ordered[:per_cycle]
        for edge in chosen:
            self._failed_edges.discard(str(edge.get("logicalId")))
        return chosen

    async def _process_endpoint(
        self, endpoint: Dict[str, Any], semaphore: asyncio.Semaphore
    ) -> None:
        """
        Process a single VeloCloud endpoint.

        Args:
            endpoint: Endpoint configuration with api_host and api_token
            semaphore: Semaphore for limiting concurrent requests
        """
        api_host = endpoint.get("api_host", DEFAULT_API_HOST)
        if not isinstance(api_host, str):
            self.logger.error(f"Invalid api_host in endpoint: {api_host}")
            return

        api_token = endpoint.get("api_token")
        if not api_token:
            self.logger.error(f"Missing API token for endpoint: {api_host}")
            return

        headers = {"Authorization": f"Token {api_token}"}
        self.logger.debug(f"Processing endpoint: {api_host}")

        try:
            async with ClientSession(
                connector=TCPConnector(ssl=self.ssl_context)
            ) as session:
                try:
                    # Get enterprise configuration and edges
                    enterprise_id, edges = (
                        await self.client.get_edges_cached(
                            api_host, session, headers
                        )
                    )

                    # Collect metrics for each edge
                    if not isinstance(edges, list):
                        self.logger.error("Invalid edges data type")
                        return

                    valid_edges = []
                    for edge in edges:
                        if not edge.get("logicalId") or not edge.get("name"):
                            self.logger.warning(
                                f"Edge missing required fields: {edge}"
                            )
                            continue
                        valid_edges.append(edge)

                    selected = self._select_edges(valid_edges)
                    self.logger.info(
                        f"Polling {len(selected)} of {len(valid_edges)} edges "
                        f"this cycle (pace {self.client.pacer.interval:.2f}s "
                        f"per call)"
                    )
                    tasks = []

                    for edge in selected:
                        edge_logical_id = str(edge.get("logicalId"))
                        edge_name = str(edge.get("name"))

                        tasks.append(
                            self.collect_link_stats_metrics(
                                api_host,
                                session,
                                enterprise_id,
                                edge_logical_id,
                                edge_name,
                                headers,
                                edge,
                                semaphore,
                            )
                        )

                        if self._with_health:
                            tasks.append(
                                self.collect_health_stats_metrics(
                                    api_host,
                                    session,
                                    enterprise_id,
                                    edge_logical_id,
                                    edge_name,
                                    headers,
                                    edge,
                                    semaphore,
                                )
                            )

                    if tasks:
                        # Execute all collection tasks concurrently
                        results = await asyncio.gather(
                            *tasks, return_exceptions=True
                        )

                        # Log results
                        successful = sum(
                            1 for r in results
                            if not isinstance(r, Exception)
                        )
                        failed = len(results) - successful
                        self.logger.info(
                            f"Metrics collected: {successful} successful, "
                            f"{failed} failed"
                        )

                except ValueError as e:
                    self._cycle_errors += 1
                    self.logger.error(
                        f"Configuration error for {api_host}: {e}"
                    )
                except asyncio.TimeoutError:
                    self._cycle_errors += 1
                    self.logger.error(
                        f"Timeout collecting metrics for {api_host}"
                    )
                except Exception as e:
                    self._cycle_errors += 1
                    self.logger.error(
                        f"Error processing endpoint {api_host}: {e}"
                    )

        except Exception as e:
            self._cycle_errors += 1
            self.logger.error(f"Failed to create session for {api_host}: {e}")

    async def collect_link_stats_metrics(
        self,
        api_host: str,
        session: ClientSession,
        enterprise_logical_id: str,
        edge_logical_id: str,
        edge_name: str,
        headers: Dict[str, str],
        edge: Dict[str, Any],
        semaphore: asyncio.Semaphore,
    ) -> int:
        """
        Collect link statistics metrics for an edge.

        Args:
            api_host: VeloCloud API host
            session: aiohttp ClientSession
            enterprise_logical_id: Enterprise ID
            edge_logical_id: Edge ID
            edge_name: Edge name
            headers: HTTP headers with authorization
            edge: Edge configuration
            semaphore: Semaphore for rate limiting

        Returns:
            Count of metrics reported
        """
        metric_count = 0

        async with semaphore:
            try:
                self.logger.debug(
                    f"Collecting link stats for edge: {edge_name} "
                    f"({edge_logical_id})"
                )

                link_stats = await self.client.get_link_stats(
                    api_host,
                    session,
                    enterprise_logical_id,
                    edge_logical_id,
                    headers,
                )
                self._last_polled[edge_logical_id] = time.time()

                total_links = len(link_stats)
                offline_links = sum(
                    1
                    for link in link_stats
                    if link.get("link", {}).get("state") != "STABLE"
                )

                all_links_offline_value = (
                    1 if total_links > 0 and offline_links == total_links
                    else 0
                )

                # Edge dimensions (metadata)
                edge_dimensions = {
                    "edge_name": sanitize_dimension_value(edge_name),
                    "edge_logical_id": sanitize_dimension_value(
                        edge_logical_id
                    ),
                    "edge_state": sanitize_dimension_value(
                        edge.get("edgeState", "UNKNOWN")
                    ),
                    "ha_state": sanitize_dimension_value(
                        edge.get("haState", "UNKNOWN")
                    ),
                    "service_state": sanitize_dimension_value(
                        edge.get("serviceState", "UNKNOWN")
                    ),
                }

                # Report all links offline metric
                self.report_metric(
                    "custom.velocloud.edge.all_links_offline",
                    all_links_offline_value,
                    dimensions=edge_dimensions,
                )
                metric_count += 1
                self.logger.debug(
                    f"Reported all_links_offline for {edge_name}: "
                    f"{all_links_offline_value}"
                )

                # State metrics
                edge_state = edge.get("edgeState", "UNKNOWN")
                edge_state_numeric = {
                    "CONNECTED": 1,
                    "OFFLINE": 0,
                    "DEGRADED": 2,
                }.get(str(edge_state), 0)

                ha_state = edge.get("haState", "UNKNOWN")
                ha_state_numeric = {
                    "READY": 1,
                    "UNCONFIGURED": 0,
                    "PENDING_DISSOCIATION": 2,
                    "FAILED": 3,
                }.get(str(ha_state), 0)

                service_state = edge.get("serviceState", "UNKNOWN")
                service_state_numeric = (
                    1 if service_state == "IN_SERVICE" else 0
                )

                # Per-link metrics
                for link in link_stats:
                    ip_address = link.get("link", {}).get("ipAddress")
                    link_state = link.get("link", {}).get("state")
                    link_state_numeric = (
                        1 if link_state == "STABLE" else 0
                    )

                    dimensions = {
                        "edge_name": sanitize_dimension_value(edge_name),
                        "ip_address": sanitize_dimension_value(
                            ip_address or "UNKNOWN"
                        ),
                        "edge_state": sanitize_dimension_value(
                            edge.get("edgeState", "UNKNOWN")
                        ),
                        "ha_state": sanitize_dimension_value(
                            edge.get("haState", "UNKNOWN")
                        ),
                        "service_state": sanitize_dimension_value(
                            edge.get("serviceState", "UNKNOWN")
                        ),
                        "link_state": sanitize_dimension_value(
                            link_state or "UNKNOWN"
                        ),
                    }

                    metrics = {
                        "jitter_ms_rx": sanitize_value(
                            link.get("bestJitterMsRx")
                        ),
                        "jitter_ms_tx": sanitize_value(
                            link.get("bestJitterMsTx")
                        ),
                        "latency_ms_rx": sanitize_value(
                            link.get("bestLatencyMsRx")
                        ),
                        "latency_ms_tx": sanitize_value(
                            link.get("bestLatencyMsTx")
                        ),
                        "loss_pct_rx": sanitize_value(
                            link.get("bestLossPctRx")
                        ),
                        "loss_pct_tx": sanitize_value(
                            link.get("bestLossPctTx")
                        ),
                        "edge_state": edge_state_numeric,
                        "ha_state": ha_state_numeric,
                        "service_state": service_state_numeric,
                        "link_state": link_state_numeric,
                    }

                    for metric_name, value in metrics.items():
                        if value is not None:
                            self.report_metric(
                                f"custom.velocloud.edge.{metric_name}",
                                value,
                                dimensions=dimensions,
                            )
                            metric_count += 1

                self.logger.info(
                    f"Link stats: {metric_count} metrics reported for "
                    f"{edge_name} ({total_links} links, {offline_links} "
                    f"offline)"
                )

            except QueryBudgetExceeded:
                self._budget_skips += 1
                self._failed_edges.add(edge_logical_id)
                self.logger.debug(
                    f"Skipped link stats for edge {edge_logical_id}: "
                    f"query budget used up"
                )
            except VeloCloudApiError as e:
                self._cycle_errors += 1
                self._failed_edges.add(edge_logical_id)
                if e.status == 429:
                    self.logger.warning(
                        f"Rate limited (429) collecting link stats for "
                        f"edge {edge_logical_id}"
                    )
                else:
                    self.logger.error(
                        f"API error collecting link stats for "
                        f"edge {edge_logical_id}: {e}"
                    )
            except Exception as e:
                self._cycle_errors += 1
                self._failed_edges.add(edge_logical_id)
                self.logger.error(
                    f"Error collecting link stats for edge {edge_logical_id}: "
                    f"{e}"
                )

        return metric_count

    async def collect_health_stats_metrics(
        self,
        api_host: str,
        session: ClientSession,
        enterprise_logical_id: str,
        edge_logical_id: str,
        edge_name: str,
        headers: Dict[str, str],
        edge: Dict[str, Any],
        semaphore: asyncio.Semaphore,
    ) -> int:
        """
        Collect health statistics metrics for an edge.

        Args:
            api_host: VeloCloud API host
            session: aiohttp ClientSession
            enterprise_logical_id: Enterprise ID
            edge_logical_id: Edge ID
            edge_name: Edge name
            headers: HTTP headers with authorization
            edge: Edge configuration
            semaphore: Semaphore for rate limiting

        Returns:
            Count of metrics reported
        """
        metric_count = 0

        async with semaphore:
            try:
                self.logger.debug(
                    f"Collecting health stats for edge: {edge_name} "
                    f"({edge_logical_id})"
                )

                health_stats = await self.client.get_health_stats(
                    api_host,
                    session,
                    enterprise_logical_id,
                    edge_logical_id,
                    headers,
                )
                self._last_polled[edge_logical_id] = time.time()

                if not health_stats:
                    self.logger.debug(
                        f"No health stats available for edge {edge_logical_id}"
                    )
                    return 0

                dimensions = {
                    "edge_name": sanitize_dimension_value(edge_name),
                    "edge_logical_id": sanitize_dimension_value(
                        edge_logical_id
                    ),
                    "ip_address": sanitize_dimension_value(
                        edge.get("ipAddress", "UNKNOWN")
                    ),
                    "edge_state": sanitize_dimension_value(
                        edge.get("edgeState", "UNKNOWN")
                    ),
                    "ha_state": sanitize_dimension_value(
                        edge.get("haState", "UNKNOWN")
                    ),
                    "service_state": sanitize_dimension_value(
                        edge.get("serviceState", "UNKNOWN")
                    ),
                }

                metrics = {
                    "cpu_utilization": sanitize_value(
                        health_stats.get("cpuPct", {}).get("average")
                    ),
                    "cpu_core_temperature": sanitize_value(
                        health_stats.get("cpuCoreTemp", {}).get("average")
                    ),
                    "memory_utilization": sanitize_value(
                        health_stats.get("memoryPct", {}).get("average")
                    ),
                    "flow_count": sanitize_value(
                        health_stats.get("flowCount", {}).get("average")
                    ),
                    "over_capacity_drops": sanitize_value(
                        health_stats.get("handoffQueueDrops", {}).get(
                            "average"
                        )
                    ),
                    "tunnel_count": sanitize_value(
                        health_stats.get("tunnelCount", {}).get("average")
                    ),
                    "tunnel_count_v6": sanitize_value(
                        health_stats.get("tunnelCountV6", {}).get("average")
                    ),
                }

                for metric_name, value in metrics.items():
                    if value is not None:
                        self.report_metric(
                            f"custom.velocloud.edge.{metric_name}",
                            value,
                            dimensions=dimensions,
                        )
                        metric_count += 1

                self.logger.info(
                    f"Health stats: {metric_count} metrics reported for "
                    f"{edge_name}"
                )

            except QueryBudgetExceeded:
                self._budget_skips += 1
                self._failed_edges.add(edge_logical_id)
                self.logger.debug(
                    f"Skipped health stats for edge {edge_logical_id}: "
                    f"query budget used up"
                )
            except VeloCloudApiError as e:
                self._cycle_errors += 1
                self._failed_edges.add(edge_logical_id)
                if e.status == 429:
                    self.logger.warning(
                        f"Rate limited (429) collecting health stats for "
                        f"edge {edge_logical_id}"
                    )
                else:
                    self.logger.error(
                        f"API error collecting health stats for "
                        f"edge {edge_logical_id}: {e}"
                    )
            except Exception as e:
                self._cycle_errors += 1
                self._failed_edges.add(edge_logical_id)
                self.logger.error(
                    f"Error collecting health stats for edge {edge_logical_id}: "
                    f"{e}"
                )

        return metric_count

    def initialize(self) -> None:
        """
        Schedule the VeloCloud cycle every CYCLE_INTERVAL_MINUTES.

        The SDK also schedules query() every minute. query() is a no-op (see
        below), so the collection cycle runs only on this schedule.
        """
        self.schedule(self.collect_cycle, timedelta(minutes=CYCLE_INTERVAL_MINUTES))

    def query(self) -> None:
        """
        SDK default one-minute callback. Intentionally does nothing.

        The SDK requires each callback to finish inside its interval, so the
        five-minute cycle is scheduled from initialize() instead.
        """
        return None

    def collect_cycle(self) -> None:
        """
        Run one VeloCloud collection cycle, scheduled every CYCLE_INTERVAL_MINUTES.

        PROBLEM FIX 6: Execution timeout (CYCLE_TIMEOUT_SECONDS) keeps the cycle
        inside its interval.
        """
        try:
            async def query_with_timeout() -> None:
                try:
                    async with asyncio.timeout(CYCLE_TIMEOUT_SECONDS):
                        await self.async_query()
                except asyncio.TimeoutError:
                    self._cycle_errors += 1
                    self.logger.error(
                        f"❌ TIMEOUT: Cycle exceeded {CYCLE_TIMEOUT_SECONDS} seconds. "
                        "Increase timeout or optimize VeloCloud API calls."
                    )

            asyncio.run(query_with_timeout())
            # Report what happened, not just that the method returned
            if self._cycle_skipped:
                self.logger.warning("Query skipped: API rate limit cooldown active")
            elif self._cycle_errors:
                self.logger.error(
                    f"Query finished with {self._cycle_errors} error(s); "
                    f"metrics for those edges were not reported"
                )
            else:
                self.logger.info("Query completed successfully")
        except Exception as e:
            self.logger.error(f"Query execution failed: {e}")

    def fastcheck(self) -> Status:
        """
        Health check method for the extension.

        Verifies connectivity to VeloCloud API endpoints.

        Returns:
            Status.OK if all endpoints are reachable, GENERIC_ERROR otherwise
        """
        self.logger.info("Fastcheck started")

        try:
            async def fastcheck_task() -> Status:
                try:
                    async with asyncio.timeout(FASTCHECK_TIMEOUT):
                        endpoints = self.activation_config.get(
                            "endpoints", []
                        )

                        if not endpoints:
                            self.logger.error("No endpoints configured")
                            return Status(StatusValue.GENERIC_ERROR)

                        for endpoint in endpoints:
                            api_host = endpoint.get("api_host")
                            api_token = endpoint.get("api_token")

                            if not api_host or not api_token:
                                self.logger.error(
                                    "Missing configuration for endpoint"
                                )
                                return Status(StatusValue.GENERIC_ERROR)

                            headers = {
                                "Authorization": f"Token {api_token}"
                            }

                            async with ClientSession(
                                connector=TCPConnector(ssl=self.ssl_context)
                            ) as session:
                                try:
                                    # One cheap call proves the token and host
                                    # work. Single attempt: retries would
                                    # outlast FASTCHECK_TIMEOUT.
                                    await self.client.get_enterprise_logical_id(
                                        api_host, session, headers, retries=1
                                    )
                                    self.logger.debug(
                                        f"Fastcheck OK for {api_host}"
                                    )
                                except VeloCloudApiError as e:
                                    if e.status == 429:
                                        # The server answered, so the token
                                        # was not refused. Don't fail activation
                                        # on a temporary limit.
                                        self.logger.warning(
                                            f"Fastcheck rate limited (429) "
                                            f"for {api_host}"
                                        )
                                        return Status(
                                            StatusValue.OK,
                                            "Rate limited (429) during "
                                            "fastcheck; host reachable",
                                        )
                                    self.logger.error(
                                        f"Fastcheck failed for {api_host}: {e}"
                                    )
                                    return Status(
                                        StatusValue.DEVICE_CONNECTION_ERROR,
                                        str(e),
                                    )
                                except ClientResponseError as e:
                                    if e.status in (401, 403):
                                        status_value = (
                                            StatusValue.AUTHENTICATION_ERROR
                                        )
                                        message = (
                                            f"VeloCloud rejected the API "
                                            f"token (HTTP {e.status})"
                                        )
                                    else:
                                        status_value = (
                                            StatusValue.DEVICE_CONNECTION_ERROR
                                        )
                                        message = f"HTTP {e.status} from {api_host}"
                                    self.logger.error(
                                        f"Fastcheck failed for {api_host}: {message}"
                                    )
                                    return Status(status_value, message)
                                except (ClientError, asyncio.TimeoutError) as e:
                                    self.logger.error(
                                        f"Fastcheck cannot reach {api_host}: {e!r}"
                                    )
                                    return Status(
                                        StatusValue.DEVICE_CONNECTION_ERROR,
                                        f"Cannot reach {api_host}",
                                    )
                                except ValueError as e:
                                    self.logger.error(
                                        f"Fastcheck got invalid response from "
                                        f"{api_host}: {e}"
                                    )
                                    return Status(
                                        StatusValue.GENERIC_ERROR, str(e)
                                    )

                        self.logger.info("Fastcheck completed successfully")
                        return Status(StatusValue.OK)

                except asyncio.TimeoutError:
                    self.logger.error(
                        f"Fastcheck exceeded {FASTCHECK_TIMEOUT} second "
                        f"timeout"
                    )
                    return Status(StatusValue.GENERIC_ERROR)

            return asyncio.run(fastcheck_task())

        except Exception as e:
            self.logger.error(f"Fastcheck failed: {e}")
            return Status(StatusValue.GENERIC_ERROR)


def main() -> None:
    """Entry point for the extension."""
    try:
        extension = ExtensionImpl()
        extension.run()
    except Exception as e:
        print(f"Extension failed to start: {e}")
        raise


if __name__ == "__main__":
    main()
