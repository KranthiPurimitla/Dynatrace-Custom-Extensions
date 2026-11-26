"""
VeloCloud API Client Module.

All HTTP traffic to the VeloCloud Orchestrator goes through VeloCloudClient.
Rate control follows the VCO API guide ("Rate Limits" section):

* Leaky-bucket limit: requests are spaced strictly so no more than
  MAX_REQUESTS_PER_SECOND are sent. The limit is set by the VCO Operator and
  can differ per environment, so it is a parameter, not a hidden default.
* Concurrency limit: at most MAX_IN_FLIGHT requests are open at once
  ("a handful" in the guide).
* HTTP 429: the guide's penalty is about 5 seconds and the client should back
  off a constant amount. Every caller pauses for the penalty, so a 429 never
  triggers a burst of retries.
"""

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

# Rate control (see module docstring). Change MAX_REQUESTS_PER_SECOND only
# after the VCO Operator confirms the limit for this environment.
# VeloCloud API Fair Usage Policy (2025-09-29): the universal limit for an
# enterprise user is 20 calls per 5 s. The organization limit (50 per 5 s) is
# shared by all its users, so the per-user cap is the hard ceiling here.
POLICY_CALLS_PER_5S_USER = 20
POLICY_MAX_REQUESTS_PER_SECOND = POLICY_CALLS_PER_5S_USER / 5.0  # 4 req/s
MAX_REQUESTS_PER_SECOND = 0.5  # budget ceiling; confirm with VCO before raising
MAX_IN_FLIGHT = 1
MIN_REQUESTS_PER_SECOND = 0.25  # adaptive floor; the pacer never slows below this
RATE_LIMIT_PENALTY = 5.0  # seconds; guide default
BLOCK_COOLDOWN = 600.0  # seconds to stop all API calls after the server keeps refusing
BLOCK_AFTER_REFUSALS = 10  # refused requests in a row, with no success between, before the cooldown
MAX_ATTEMPTS = 3  # attempts per request, counting the first one
HTTP_TIMEOUT = 15  # seconds per request
EDGE_CACHE_TTL = 900  # seconds to reuse the edge list


class VeloCloudApiError(Exception):
    """Error returned by the VeloCloud API."""

    def __init__(
        self,
        status: int,
        message: str,
        url: str,
        retry_after: Optional[float] = None,
    ) -> None:
        """
        Initialize VeloCloud API error.

        Args:
            status: HTTP status code
            message: Error message
            url: Request URL that failed
            retry_after: Seconds the server asked us to wait (429 only)
        """
        self.status = status
        self.message = message
        self.url = url
        self.retry_after = retry_after
        super().__init__(
            f"VeloCloud API Error [{status}]: {message} (URL: {url})"
        )


class QueryBudgetExceeded(Exception):
    """Raised instead of sending a call that would run past the query deadline."""


class RequestPacer:
    """
    Spaces calls at the current rate, and adapts that rate to the server.

    The rate starts at the floor (MIN_REQUESTS_PER_SECOND) and never exceeds the
    ceiling. Starting at the ceiling sent a burst at the beginning of every run.
    Each success raises the rate by 2%, up to the ceiling (about 117 successes
    to go from 1 to 10 req/s). Each 429 halves the rate, down to the floor. Every call reserves
    the next free time slot, so the spacing holds even when many tasks call at
    once. There is no asyncio.Lock because the extension calls asyncio.run()
    per query, and a lock would bind to one event loop.
    """

    def __init__(self, max_requests_per_second: float = MAX_REQUESTS_PER_SECOND) -> None:
        """
        Initialize the pacer.

        Args:
            max_requests_per_second: Ceiling on calls per second
        """
        if max_requests_per_second <= 0:
            raise ValueError("max_requests_per_second must be positive")
        self.max_rate = max_requests_per_second
        self.min_rate = min(MIN_REQUESTS_PER_SECOND, max_requests_per_second)
        self.rate = self.min_rate
        self._next_slot = 0.0
        self._last_send = float("-inf")
        self._penalty_until = float("-inf")

    @property
    def interval(self) -> float:
        """Seconds between two consecutive calls at the current rate."""
        return 1.0 / self.rate

    def next_send_time(self) -> float:
        """Earliest monotonic time at which a new call could be sent."""
        return max(self._next_slot, self._penalty_until, time.monotonic())

    def on_rate_limited(self) -> None:
        """Halve the rate after a 429, but not below the floor."""
        self.rate = max(self.min_rate, self.rate / 2)

    def on_success(self) -> None:
        """Raise the rate back by 2% after a successful call, up to the ceiling."""
        self.rate = min(self.max_rate, self.rate * 1.02)

    def set_ceiling(self, requests_per_second: float) -> None:
        """
        Apply the configured budget as the hard ceiling for this run.

        The floor stays at MIN_REQUESTS_PER_SECOND, but never above the ceiling.
        The ceiling is also capped at the fair-usage per-user limit.
        """
        requests_per_second = min(requests_per_second, POLICY_MAX_REQUESTS_PER_SECOND)
        self.max_rate = requests_per_second
        self.min_rate = min(MIN_REQUESTS_PER_SECOND, requests_per_second)
        self.rate = min(self.rate, requests_per_second)

    async def wait_turn(self) -> None:
        """Sleep until this call's reserved time slot."""
        slot = max(self._next_slot, time.monotonic())
        self._next_slot = slot + self.interval
        # Wait until the reserved slot AND one full interval after the last
        # call actually sent. A sleep can overshoot, so spacing is measured from
        # the real send time, not from the planned slot. Re-check the clock after
        # each sleep, because asyncio can also wake early.
        # The penalty is checked on every loop, so calls already queued also wait.
        while True:
            due = max(slot, self._last_send + self.interval, self._penalty_until)
            remaining = due - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)
        self._last_send = time.monotonic()

    def pause(self, seconds: float) -> None:
        """
        Hold back every caller for `seconds` (used for the 429 penalty).

        Args:
            seconds: Length of the pause
        """
        self._penalty_until = max(self._penalty_until, time.monotonic() + seconds)


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """
    Parse a Retry-After header given in seconds.

    Args:
        value: Raw header value

    Returns:
        Seconds to wait, or None if missing or not a number
    """
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class VeloCloudClient:
    """
    VeloCloud API client for making authenticated, rate-limited requests.
    """

    def __init__(
        self,
        logger: Any,
        max_requests_per_second: float = MAX_REQUESTS_PER_SECOND,
        max_in_flight: int = MAX_IN_FLIGHT,
    ) -> None:
        """
        Initialize VeloCloud client.

        Args:
            logger: Logger instance from Extension
            max_requests_per_second: Strict upper bound on calls per second
            max_in_flight: Maximum number of open requests at once
        """
        self.logger = logger
        self.pacer = RequestPacer(max_requests_per_second)
        self._max_in_flight = max_in_flight
        self._gate: Optional[asyncio.Semaphore] = None
        self._gate_loop: Optional[asyncio.AbstractEventLoop] = None
        self._enterprise_ids: Dict[str, str] = {}
        self._edge_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        # Monotonic time after which no new call may start (set per query)
        self.deadline: Optional[float] = None
        # Monotonic time until which the API is treated as blocked (see fetch)
        self.blocked_until: float = 0.0
        # Requests refused after all retries, with no success since (see fetch)
        self.refused_in_a_row = 0
        # Whether the first 429 of this run has been logged in full
        self._logged_429 = False

    def is_blocked(self) -> bool:
        """True while the API is in a cooldown after retries ran out on a 429."""
        return time.monotonic() < self.blocked_until

    def _in_flight_gate(self) -> asyncio.Semaphore:
        """
        Return the concurrency gate for the running event loop.

        The gate is recreated when the loop changes, because each query runs
        in a new loop via asyncio.run().
        """
        loop = asyncio.get_running_loop()
        if self._gate is None or self._gate_loop is not loop:
            self._gate = asyncio.Semaphore(self._max_in_flight)
            self._gate_loop = loop
        return self._gate

    async def _send_once(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        headers: Dict[str, str],
        data: Optional[Dict[str, Any]],
    ) -> Any:
        """
        Send one request inside the pacing and concurrency limits.

        Raises:
            VeloCloudApiError: On HTTP 429 (after pausing every caller)
            aiohttp.ClientResponseError: On other HTTP errors
            aiohttp.ClientError, asyncio.TimeoutError: On transport errors
        """
        async with self._in_flight_gate():
            if self.deadline is not None and self.pacer.next_send_time() > self.deadline:
                raise QueryBudgetExceeded(f"query budget used up before {url}")
            await self.pacer.wait_turn()
            async with asyncio.timeout(HTTP_TIMEOUT):
                async with session.request(
                    method, url, headers=headers, json=data
                ) as response:
                    if response.status == 429:
                        retry_after = _parse_retry_after(
                            response.headers.get("Retry-After")
                        )
                        if not self._logged_429:
                            # Once per run: the headers and body show the limit, its
                            # window, and whether the refusal comes from VCO or a proxy
                            self._logged_429 = True
                            body = (await response.text())[:500]
                            self.logger.warning(
                                f"First 429 from {url}. Headers: "
                                f"{dict(response.headers)}. Body: {body}"
                            )
                        # Slow down for everyone, and hold all callers for the penalty
                        self.pacer.on_rate_limited()
                        self.pacer.pause(max(RATE_LIMIT_PENALTY, retry_after or 0.0))
                        raise VeloCloudApiError(
                            status=429,
                            message="Too Many Requests",
                            url=url,
                            retry_after=retry_after,
                        )
                    response.raise_for_status()
                    self.pacer.on_success()
                    self.refused_in_a_row = 0
                    return await response.json()

    async def fetch(
        self,
        session: aiohttp.ClientSession,
        url: str,
        headers: Dict[str, str],
        method: str = "GET",
        data: Optional[Dict[str, Any]] = None,
        retries: int = MAX_ATTEMPTS,
    ) -> Any:
        """
        Fetch JSON from the VeloCloud API with 429 handling.

        On 429 the client waits the penalty (or Retry-After if the server gave a
        longer one) and tries again, up to `retries` attempts in total.

        Args:
            session: aiohttp ClientSession
            url: API endpoint URL
            headers: HTTP headers including authorization
            method: HTTP method
            data: JSON body for POST requests
            retries: Total attempts (fastcheck passes 1 so it never waits)

        Returns:
            Decoded JSON response

        Raises:
            VeloCloudApiError: If the last attempt was rate limited
        """
        for attempt in range(1, retries + 1):
            try:
                return await self._send_once(session, method, url, headers, data)
            except VeloCloudApiError as e:
                if e.status != 429:
                    raise
                wait = max(RATE_LIMIT_PENALTY, e.retry_after or 0.0)
                out_of_attempts = attempt >= retries
                out_of_budget = (
                    self.deadline is not None
                    and time.monotonic() + wait > self.deadline
                )
                if out_of_attempts:
                    # Retries ran out. One refused edge is not a block, so stop all
                    # calls only after many refusals in a row with no success between.
                    self.refused_in_a_row += 1
                    if self.refused_in_a_row >= BLOCK_AFTER_REFUSALS:
                        self.blocked_until = time.monotonic() + BLOCK_COOLDOWN
                        self.refused_in_a_row = 0
                    raise
                if out_of_budget:
                    # Out of cycle time, not a server block: end this call only
                    raise
                self.logger.warning(
                    f"Rate limited (429). Waiting {wait:.1f}s "
                    f"(attempt {attempt}/{retries}, pace now "
                    f"{self.pacer.rate:.2f} req/s): {url}"
                )
                await asyncio.sleep(wait)
        raise RuntimeError("fetch() made no attempts")

    async def get_enterprise_logical_id(
        self,
        api_host: str,
        session: aiohttp.ClientSession,
        headers: Dict[str, str],
        retries: int = MAX_ATTEMPTS,
    ) -> str:
        """
        Retrieve the enterprise logical ID.

        Args:
            api_host: VeloCloud API host URL
            session: aiohttp ClientSession
            headers: HTTP headers with authorization
            retries: Total attempts for this request

        Returns:
            Enterprise logical ID

        Raises:
            ValueError: If the API response is invalid
            VeloCloudApiError: If the request is still rate limited
        """
        url = f"{api_host}/portal/rest/enterprise/getEnterpriseConfigurations"
        self.logger.debug(f"Fetching enterprise configuration from: {url}")

        conf_policies = await self.fetch(
            session, url, headers, method="POST", retries=retries
        )
        if isinstance(conf_policies, list) and conf_policies:
            first = conf_policies[0]
            if isinstance(first, dict) and "enterpriseLogicalId" in first:
                return str(first["enterpriseLogicalId"])

        self.logger.error(
            f"Invalid enterprise configuration response: {conf_policies}"
        )
        raise ValueError("Invalid enterprise configuration response")

    async def get_edges(
        self,
        api_host: str,
        session: aiohttp.ClientSession,
        enterprise_logical_id: str,
        headers: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        """
        Retrieve the list of edges for the enterprise.

        Raises:
            ValueError: If the API response is invalid
            VeloCloudApiError: If the request is still rate limited
        """
        url = (
            f"{api_host}/api/sdwan/v2/enterprises/"
            f"{enterprise_logical_id}/edges"
        )
        self.logger.debug(f"Fetching edges from: {url}")

        edge_result = await self.fetch(session, url, headers)
        if isinstance(edge_result, dict):
            edges_data = edge_result.get("data")
            if isinstance(edges_data, list):
                self.logger.debug(f"Retrieved {len(edges_data)} edges")
                return edges_data

        self.logger.error(f"Invalid edge list response: {edge_result}")
        raise ValueError("Invalid edge list response")

    async def get_edges_cached(
        self,
        api_host: str,
        session: aiohttp.ClientSession,
        headers: Dict[str, str],
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Return (enterprise logical ID, edges), reusing results for EDGE_CACHE_TTL.

        Edges change slowly, so most one-minute cycles make no edge-list call.

        Returns:
            Tuple of (enterprise logical ID, list of edges)
        """
        now = time.monotonic()
        cached = self._edge_cache.get(api_host)
        if cached and now - cached[0] < EDGE_CACHE_TTL:
            return self._enterprise_ids[api_host], cached[1]

        enterprise_id = self._enterprise_ids.get(api_host)
        if not enterprise_id:
            enterprise_id = await self.get_enterprise_logical_id(
                api_host, session, headers
            )
            self._enterprise_ids[api_host] = enterprise_id

        edges = await self.get_edges(api_host, session, enterprise_id, headers)
        self._edge_cache[api_host] = (now, edges)
        return enterprise_id, edges

    async def get_link_stats(
        self,
        api_host: str,
        session: aiohttp.ClientSession,
        enterprise_logical_id: str,
        edge_logical_id: str,
        headers: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        """
        Retrieve link statistics for one edge.

        Raises:
            VeloCloudApiError: If the request is still rate limited
        """
        url = (
            f"{api_host}/api/sdwan/v2/enterprises/{enterprise_logical_id}/"
            f"edges/{edge_logical_id}/linkStats?include=link.*"
        )
        self.logger.debug(f"Fetching link stats from: {url}")

        result = await self.fetch(session, url, headers)
        return result if isinstance(result, list) else []

    async def get_health_stats(
        self,
        api_host: str,
        session: aiohttp.ClientSession,
        enterprise_logical_id: str,
        edge_logical_id: str,
        headers: Dict[str, str],
    ) -> Dict[str, Any]:
        """
        Retrieve health statistics for one edge.

        Raises:
            VeloCloudApiError: If the request is still rate limited
        """
        url = (
            f"{api_host}/api/sdwan/v2/enterprises/{enterprise_logical_id}/"
            f"edges/{edge_logical_id}/healthstats"
        )
        self.logger.debug(f"Fetching health stats from: {url}")

        result = await self.fetch(session, url, headers)
        return result if isinstance(result, dict) else {}
