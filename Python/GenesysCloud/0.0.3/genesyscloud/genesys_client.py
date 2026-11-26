"""
Shared Genesys Cloud API client (Requirement 6).

Owns every HTTP concern so collectors never make raw requests:
  * OAuth2 client-credentials grant
  * token caching + refresh
  * retry with exponential backoff
  * pagination over the standard entities/pageCount envelope
  * rate-limit handling (HTTP 429 + Retry-After)
  * error handling
  * telemetry counters drained by the Phase 0 platform-health collector

Genesys API reference: https://developer.genesys.cloud/platform/api/
"""

import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import requests

from genesyscloud import genesys_constants as constants
from genesyscloud.genesys_utils import get_logger

logger = get_logger("client")


class GenesysAuthError(Exception):
    """OAuth token could not be obtained (bad credentials or region)."""


class GenesysApiError(Exception):
    """An API call failed after exhausting retries."""


@dataclass
class CallTelemetry:
    """Per-cycle counters; drained (read + reset) by platform_health each minute."""

    latencies_ms: list = field(default_factory=list)
    calls: int = 0
    errors: int = 0
    rate_limited: int = 0
    auth_failures: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_call(self, latency_ms: float):
        with self._lock:
            self.calls += 1
            self.latencies_ms.append(latency_ms)

    def record_error(self):
        with self._lock:
            self.errors += 1

    def record_rate_limited(self):
        with self._lock:
            self.rate_limited += 1

    def record_auth_failure(self):
        with self._lock:
            self.auth_failures += 1

    def drain(self) -> dict:
        with self._lock:
            snap = {
                "latencies_ms": list(self.latencies_ms),
                "calls": self.calls,
                "errors": self.errors,
                "rate_limited": self.rate_limited,
                "auth_failures": self.auth_failures,
            }
            self.latencies_ms.clear()
            self.calls = self.errors = self.rate_limited = self.auth_failures = 0
            return snap


class GenesysClient:
    def __init__(self, region: str, client_id: str, client_secret: str, proxy: str | None = None):
        if region not in constants.REGIONS:
            raise GenesysAuthError(f"Unknown Genesys region '{region}'. Known: {sorted(constants.REGIONS)}")
        hosts = constants.REGIONS[region]
        self._login_base = f"https://{hosts['login']}"
        self._api_base = f"https://{hosts['api']}"
        self._client_id = client_id
        self._client_secret = client_secret

        self._token: str | None = None
        self._token_expiry_epoch: float = 0.0
        self._token_lock = threading.Lock()

        self._session = requests.Session()
        self._session.trust_env = True
        if proxy:
            self._session.proxies = {"https": proxy, "http": proxy}
        self.telemetry = CallTelemetry()

    # ------------------------------------------------------------------ auth
    def _token_valid(self) -> bool:
        return bool(self._token) and time.time() < (
            self._token_expiry_epoch - constants.TOKEN_REFRESH_SKEW_SECONDS
        )

    def _ensure_token(self) -> str:
        if self._token_valid():
            token = self._token
            if token is None:
                raise GenesysAuthError("Token unexpectedly missing")
            return token
        with self._token_lock:
            if self._token_valid():
                token = self._token
                if token is None:
                    raise GenesysAuthError("Token unexpectedly missing")
                return token

            logger.info("Requesting new Genesys OAuth token")
            try:
                resp = self._session.post(
                    f"{self._login_base}{constants.TOKEN_PATH}",
                    data={"grant_type": "client_credentials"},
                    auth=(self._client_id, self._client_secret),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=constants.DEFAULT_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                self.telemetry.record_auth_failure()
                raise GenesysAuthError(f"Token request failed: {exc}") from exc

            if resp.status_code != 200:
                self.telemetry.record_auth_failure()
                raise GenesysAuthError(f"Token request returned {resp.status_code}: {resp.text[:200]}")

            payload = resp.json()
            token = str(payload["access_token"])
            self._token = token
            self._token_expiry_epoch = time.time() + int(payload.get("expires_in", 86400))
            logger.info("Obtained Genesys OAuth token")
            return token

    # --------------------------------------------------------------- request
    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        """Authenticated call with retry, backoff and 429 handling."""
        url = f"{self._api_base}{path}"
        attempt = 0
        connection_retries = 0

        while True:
            attempt += 1
            token = self._ensure_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }

            started = time.perf_counter()
            try:
                resp = self._session.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    headers=headers,
                    timeout=constants.DEFAULT_TIMEOUT_SECONDS,
                )
            except requests.exceptions.ConnectionError as exc:
                self.telemetry.record_call((time.perf_counter() - started) * 1000.0)
                self.telemetry.record_error()
                message = str(exc)
                if self._is_dns_failure(message):
                    raise GenesysApiError(self._dns_error_message(method, path, exc)) from exc
                if connection_retries >= 1:
                    raise GenesysApiError(f"{method} {path} failed after retry: {exc}") from exc
                connection_retries += 1
                logger.warning("Connection error on %s; retrying once in 1s: %s", path, exc)
                time.sleep(1)
                continue
            except requests.RequestException as exc:
                self.telemetry.record_call((time.perf_counter() - started) * 1000.0)
                self.telemetry.record_error()
                if attempt > constants.MAX_RETRIES:
                    raise GenesysApiError(f"{method} {path} failed: {exc}") from exc
                self._sleep_backoff(attempt)
                continue

            self.telemetry.record_call((time.perf_counter() - started) * 1000.0)

            if resp.status_code == 401:
                self.telemetry.record_auth_failure()
                logger.warning("401 from %s; forcing token refresh", path)
                self._token = None
                if attempt > constants.MAX_RETRIES:
                    raise GenesysApiError(f"{method} {path}: repeated 401 (auth)")
                continue

            if resp.status_code == 429:
                self.telemetry.record_rate_limited()
                retry_after = float(resp.headers.get("Retry-After", constants.BACKOFF_BASE_SECONDS))
                logger.warning("429 from %s; sleeping %.1fs", path, retry_after)
                if attempt > constants.MAX_RETRIES:
                    raise GenesysApiError(f"{method} {path}: rate limited after retries")
                time.sleep(retry_after)
                continue

            if 500 <= resp.status_code < 600:
                self.telemetry.record_error()
                logger.warning("%s from %s (server error)", resp.status_code, path)
                if attempt > constants.MAX_RETRIES:
                    raise GenesysApiError(f"{method} {path}: {resp.status_code} after retries")
                self._sleep_backoff(attempt)
                continue

            if resp.status_code >= 400:
                self.telemetry.record_error()
                raise GenesysApiError(f"{method} {path}: {resp.status_code} {resp.text[:200]}")

            return resp.json() if resp.content else {}

    @staticmethod
    def _sleep_backoff(attempt: int):
        time.sleep(constants.BACKOFF_BASE_SECONDS**attempt)

    @staticmethod
    def _is_dns_failure(message: str) -> bool:
        lowered = message.lower()
        return any(
            marker in lowered
            for marker in ("getaddrinfo failed", "nameresolutionerror", "failed to resolve")
        )

    def _dns_error_message(self, method: str, path: str, exc: Exception) -> str:
        host = urlsplit(self._api_base).hostname
        return (
            f"{method} {path} failed: {exc}. "
            f"DNS resolution failed for '{host}' - the host could not be resolved. "
            f"Check `nslookup {host}` and your HTTPS_PROXY/HTTP_PROXY environment variables."
        )

    # ----------------------------------------------------------- pagination
    def get_paginated(
        self,
        path: str,
        params: dict | None = None,
        page_size: int = constants.DEFAULT_PAGE_SIZE,
    ) -> list:
        """Walk a standard Genesys entities/pageCount collection."""
        params = dict(params or {})
        params["pageSize"] = page_size
        params["pageNumber"] = 1
        results: list = []
        while True:
            body = self.request("GET", path, params=params)
            results.extend(body.get("entities", []))
            if params["pageNumber"] >= body.get("pageCount", 1):
                break
            params["pageNumber"] += 1
        return results

    def close(self):
        self._session.close()
