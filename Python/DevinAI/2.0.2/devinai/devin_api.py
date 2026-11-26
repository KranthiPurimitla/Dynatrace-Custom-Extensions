"""
Devin AI API client.

This module is responsible only for communicating with Devin AI.

It does NOT:
    - Generate CSV files
    - Write files
    - Send metrics to Dynatrace
    - Store data persistently

Responsibilities:
    - Authenticate against Devin API
    - Execute GET requests
    - Handle retries
    - Handle rate limiting
    - Handle HTTP errors
    - Retrieve enterprise users
    - Retrieve user ACU consumption
    - Retrieve enterprise ACU consumption
    - Retrieve organization ACU consumption
    - Retrieve usage metrics
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import requests


logger = logging.getLogger(__name__)


# ============================================================
# CONFIGURATION
# ============================================================

DEFAULT_TIMEOUT = 30

DEFAULT_MAX_RETRIES = 3

DEFAULT_PAGE_SIZE = 200


# ============================================================
# DATA MODELS
# ============================================================


@dataclass
class DevinUser:
    user_id: str
    name: str | None
    email: str | None
    org_id: str | None
    role: str | None
    enterprise_joined_at: int | None


@dataclass
class DevinACURecord:
    user_id: str
    date_timestamp: int
    acus: float
    product: str
    org_id: str | None
    user_name: str | None


@dataclass
class DevinEnterpriseACURecord:
    date_timestamp: int
    acus: float
    product: str


@dataclass
class DevinOrganizationACURecord:
    org_id: str
    date_timestamp: int
    acus: float
    product: str


@dataclass
class DevinUsageMetrics:
    sessions: float
    searches: float
    prs_created: float
    prs_merged: float


# ============================================================
# API EXCEPTION
# ============================================================


class DevinAPIError(Exception):
    """
    Exception raised when a Devin API request fails.

    status_code:
        HTTP status code when available.

    endpoint:
        API endpoint that failed.

    is_connection_error:
        True when the failure was caused by network,
        DNS, timeout, or connection-level problems.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        endpoint: str | None = None,
        is_connection_error: bool = False,
    ):
        super().__init__(message)

        self.status_code = status_code

        self.endpoint = endpoint

        self.is_connection_error = is_connection_error


# ============================================================
# DEVIN API CLIENT
# ============================================================


class DevinAPIClient:
    def __init__(
        self,
        api_token: str,
        base_url: str,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        """
        Initialize Devin API client.

        The base URL is supplied by activation configuration.
        It is intentionally NOT hard-coded here.
        """

        if not api_token:
            raise ValueError("Devin API token must be provided.")

        if not isinstance(
            api_token,
            str,
        ):
            raise ValueError("Devin API token must be a string.")

        if not base_url:
            raise ValueError("Devin API base URL must be provided.")

        if not isinstance(
            base_url,
            str,
        ):
            raise ValueError("Devin API base URL must be a string.")

        base_url = base_url.strip()

        if not base_url:
            raise ValueError("Devin API base URL cannot be empty.")

        self.base_url = base_url.rstrip("/")

        self.timeout = timeout

        self.max_retries = max_retries

        self.session = requests.Session()

        self.session.headers.update(
            {
                "Authorization": (f"Bearer {api_token}"),
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    # ========================================================
    # COMMON GET
    # ========================================================

    def _get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        optional: bool = False,
    ) -> dict[str, Any] | list[Any] | None:
        """
        Execute GET request against Devin API.

        optional=True means that 404/400 can be treated as
        unavailable data.

        Authentication failures are NEVER swallowed.
        """

        url = f"{self.base_url}{endpoint}"

        last_connection_error: Exception | None = None

        last_server_status: int | None = None

        last_server_error_detail: str | None = None

        for attempt in range(
            1,
            self.max_retries + 1,
        ):
            try:
                logger.debug(
                    "Devin API GET: endpoint=%s params=%s",
                    endpoint,
                    params,
                )

                response = self.session.get(
                    url,
                    params=params,
                    timeout=self.timeout,
                )

                logger.debug(
                    "Devin API response: HTTP=%s endpoint=%s",
                    response.status_code,
                    endpoint,
                )

                # ====================================================
                # SUCCESS
                # ====================================================

                if response.ok:
                    if not response.content:
                        return {}

                    try:
                        return response.json()

                    except ValueError as exc:
                        raise DevinAPIError(
                            (
                                "Devin API returned a "
                                "successful HTTP response "
                                "but the response body "
                                "was not valid JSON."
                            ),
                            status_code=400,
                            endpoint=endpoint,
                        ) from exc

                # ====================================================
                # AUTHENTICATION
                # ====================================================

                if response.status_code == 401:
                    detail = self._extract_error(response)

                    raise DevinAPIError(
                        (
                            "Devin API authentication failed. "
                            "The API token may be invalid, "
                            "expired, or revoked. "
                            f"Details={detail}"
                        ),
                        status_code=401,
                        endpoint=endpoint,
                    )

                # ====================================================
                # AUTHORIZATION
                # ====================================================

                if response.status_code == 403:
                    detail = self._extract_error(response)

                    raise DevinAPIError(
                        (f"Devin API permission denied. Endpoint={endpoint}. Details={detail}"),
                        status_code=403,
                        endpoint=endpoint,
                    )

                # ====================================================
                # NOT FOUND
                # ====================================================

                if response.status_code == 404:
                    if optional:
                        logger.warning(
                            "Optional Devin API endpoint returned HTTP 404: %s",
                            endpoint,
                        )

                        return None

                    detail = self._extract_error(response)

                    raise DevinAPIError(
                        (f"Devin API endpoint or resource was not found: {endpoint}. Details={detail}"),
                        status_code=404,
                        endpoint=endpoint,
                    )

                # ====================================================
                # BAD REQUEST
                # ====================================================

                if response.status_code == 400:
                    detail = self._extract_error(response)

                    if optional:
                        logger.warning(
                            "Optional Devin API endpoint returned HTTP 400: endpoint=%s details=%s",
                            endpoint,
                            detail,
                        )

                        return None

                    raise DevinAPIError(
                        (f"Devin API rejected the request. Endpoint={endpoint}. Details={detail}"),
                        status_code=400,
                        endpoint=endpoint,
                    )

                # ====================================================
                # RATE LIMIT
                # ====================================================

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")

                    delay = self._calculate_retry_delay(
                        retry_after,
                        attempt,
                    )

                    logger.warning(
                        "Devin API rate limit reached. Attempt %s/%s. Retrying in %.1f seconds.",
                        attempt,
                        self.max_retries,
                        delay,
                    )

                    if attempt < self.max_retries:
                        time.sleep(delay)

                        continue

                    raise DevinAPIError(
                        ("Devin API rate limit persisted after retries."),
                        status_code=429,
                        endpoint=endpoint,
                    )

                # ====================================================
                # SERVER ERROR
                # ====================================================

                if response.status_code >= 500:
                    last_server_status = response.status_code

                    last_server_error_detail = self._extract_error(response)

                    delay = self._calculate_retry_delay(
                        None,
                        attempt,
                    )

                    logger.warning(
                        "Devin API server error. HTTP=%s attempt=%s/%s. Retrying in %.1f seconds.",
                        response.status_code,
                        attempt,
                        self.max_retries,
                        delay,
                    )

                    if attempt < self.max_retries:
                        time.sleep(delay)

                        continue

                    raise DevinAPIError(
                        (
                            "Devin API server error "
                            "persisted after retries. "
                            f"HTTP={last_server_status}. "
                            f"Details="
                            f"{last_server_error_detail}"
                        ),
                        status_code=last_server_status,
                        endpoint=endpoint,
                    )

                # ====================================================
                # OTHER HTTP ERROR
                # ====================================================

                detail = self._extract_error(response)

                raise DevinAPIError(
                    (
                        "Devin API request failed. "
                        f"HTTP={response.status_code}. "
                        f"Endpoint={endpoint}. "
                        f"Details={detail}"
                    ),
                    status_code=response.status_code,
                    endpoint=endpoint,
                )

            # ========================================================
            # DEVIN API ERROR
            # ========================================================

            except DevinAPIError:
                raise

            # ========================================================
            # CONNECTION / NETWORK ERROR
            # ========================================================

            except (
                requests.Timeout,
                requests.ConnectionError,
                requests.RequestException,
            ) as exc:
                last_connection_error = exc

                logger.warning(
                    "Devin API connection error. Attempt %s/%s. Endpoint=%s. Error=%s",
                    attempt,
                    self.max_retries,
                    endpoint,
                    exc,
                )

                if attempt < self.max_retries:
                    delay = self._calculate_retry_delay(
                        None,
                        attempt,
                    )

                    time.sleep(delay)

        # ============================================================
        # CONNECTION ERROR AFTER RETRIES
        # ============================================================

        if last_connection_error is not None:
            raise DevinAPIError(
                (
                    "Unable to communicate with Devin API "
                    f"after {self.max_retries} attempts. "
                    f"Endpoint={endpoint}. "
                    f"Last error={last_connection_error}"
                ),
                status_code=None,
                endpoint=endpoint,
                is_connection_error=True,
            )

        # ============================================================
        # SERVER ERROR FALLBACK
        # ============================================================

        if last_server_status is not None:
            raise DevinAPIError(
                (
                    "Devin API server error after "
                    f"{self.max_retries} attempts. "
                    f"HTTP={last_server_status}. "
                    f"Endpoint={endpoint}. "
                    f"Details={last_server_error_detail}"
                ),
                status_code=last_server_status,
                endpoint=endpoint,
            )

        # ============================================================
        # UNKNOWN FAILURE
        # ============================================================

        raise DevinAPIError(
            (f"Unknown Devin API failure. Endpoint={endpoint}"),
            endpoint=endpoint,
        )

    # ========================================================
    # RETRY DELAY
    # ========================================================

    @staticmethod
    def _calculate_retry_delay(
        retry_after: str | None,
        attempt: int,
    ) -> float:
        """
        Calculate retry delay.

        Retry-After is preferred when supplied by Devin.
        Otherwise exponential backoff is used.
        """

        if retry_after:
            try:
                delay = float(retry_after)

                # Prevent an unreasonable server-provided
                # delay from blocking the extension for hours.

                return min(
                    max(delay, 1.0),
                    60.0,
                )

            except ValueError:
                pass

        return min(
            float(2**attempt),
            30.0,
        )

    # ========================================================
    # ERROR PARSER
    # ========================================================

    @staticmethod
    def _extract_error(
        response: requests.Response,
    ) -> str:
        """
        Extract useful error information without exposing
        authentication headers or credentials.
        """

        try:
            data = response.json()

            if isinstance(
                data,
                dict,
            ):
                for key in (
                    "detail",
                    "message",
                    "error",
                    "errors",
                    "required_permission",
                    "required_permissions",
                    "missing_permissions",
                ):
                    if key in data:
                        return str(data[key])

                return str(data)

            return str(data)

        except ValueError:
            body = response.text.strip()

            if not body:
                return f"HTTP {response.status_code}"

            return body[:500]

    # ========================================================
    # USERS
    # ========================================================

    def get_users(self) -> list[DevinUser]:
        """
        Retrieve all enterprise users.
        """

        logger.info("Fetching Devin enterprise users.")

        users: list[DevinUser] = []

        cursor: str | None = None

        page = 0

        while True:
            page += 1

            params: dict[str, Any] = {
                "limit": DEFAULT_PAGE_SIZE,
            }

            if cursor:
                params["after"] = cursor

            data = self._get(
                "/enterprise/members/users",
                params=params,
            )

            if not isinstance(
                data,
                dict,
            ):
                raise DevinAPIError(
                    ("Unexpected response from Devin users API. Expected JSON object."),
                    status_code=400,
                    endpoint=("/enterprise/members/users"),
                )

            items = data.get(
                "items",
                [],
            )

            if not isinstance(
                items,
                list,
            ):
                raise DevinAPIError(
                    ("Unexpected Devin users API response. 'items' is not a list."),
                    status_code=400,
                    endpoint=("/enterprise/members/users"),
                )

            logger.info(
                "Devin users page %s: %s users.",
                page,
                len(items),
            )

            for item in items:
                if not isinstance(
                    item,
                    dict,
                ):
                    logger.warning("Skipping invalid Devin user record.")

                    continue

                user = self._parse_user(item)

                if user:
                    users.append(user)

            has_next_page = data.get(
                "has_next_page",
                False,
            )

            next_cursor = data.get("end_cursor")

            if not has_next_page:
                break

            if not next_cursor:
                logger.warning(
                    "Devin users API indicated another page but returned no end_cursor. Stopping pagination."
                )

                break

            cursor = str(next_cursor)

        logger.info(
            "Total Devin users discovered: %s",
            len(users),
        )

        return users

    # ========================================================
    # USER PARSER
    # ========================================================

    @staticmethod
    def _parse_user(
        item: dict[str, Any],
    ) -> DevinUser | None:
        """
        Convert Devin user API response into DevinUser.
        """

        user_id = item.get("user_id") or item.get("id")

        if not user_id:
            logger.warning("Skipping Devin user without user_id.")

            return None

        name = item.get("name")

        email = item.get("email")

        org_id = None

        role = None

        role_assignments = item.get(
            "role_assignments",
            [],
        )

        if isinstance(
            role_assignments,
            list,
        ):
            # Enterprise-level role assignments have org_id=None;
            # only org-scoped assignments carry a real org_id.
            # Prefer an org-scoped assignment over an enterprise
            # one so devinai:organization entities are populated.
            org_assignment = next(
                (
                    assignment
                    for assignment in role_assignments
                    if isinstance(assignment, dict) and assignment.get("org_id")
                ),
                None,
            )

            selected_assignment = org_assignment or next(
                (assignment for assignment in role_assignments if isinstance(assignment, dict)),
                None,
            )

            if selected_assignment:
                org_id = selected_assignment.get("org_id")

                role_object = selected_assignment.get("role")

                if isinstance(
                    role_object,
                    dict,
                ):
                    role = role_object.get("role_name") or role_object.get("name")

        return DevinUser(
            user_id=str(user_id),
            name=name,
            email=email,
            org_id=org_id,
            role=role,
            enterprise_joined_at=(item.get("enterprise_joined_at")),
        )

    # ========================================================
    # USER DAILY CONSUMPTION
    # ========================================================

    def get_user_consumption(
        self,
        user: DevinUser,
        time_after: int | None = None,
        time_before: int | None = None,
    ) -> list[DevinACURecord]:
        """
        Retrieve daily ACU consumption for one Devin user.
        """

        encoded_user_id = quote(
            user.user_id,
            safe="",
        )

        endpoint = f"/enterprise/consumption/daily/users/{encoded_user_id}"

        params = self._build_time_params(
            time_after,
            time_before,
        )

        data = self._get(
            endpoint,
            params=params,
        )

        if not isinstance(
            data,
            dict,
        ):
            raise DevinAPIError(
                ("Unexpected response from Devin user consumption API. Expected JSON object."),
                status_code=400,
                endpoint=endpoint,
            )

        records: list[DevinACURecord] = []

        consumption_by_date = data.get(
            "consumption_by_date",
            [],
        )

        # ----------------------------------------------------
        # API returned no consumption data
        # ----------------------------------------------------

        if not consumption_by_date:
            logger.debug(
                "No Devin consumption data for user=%s.",
                user.user_id,
            )

            return records

        # ----------------------------------------------------
        # Dictionary response
        # ----------------------------------------------------

        if isinstance(
            consumption_by_date,
            dict,
        ):
            entries = consumption_by_date.items()

        # ----------------------------------------------------
        # List response
        # ----------------------------------------------------

        elif isinstance(
            consumption_by_date,
            list,
        ):
            entries = (
                (
                    entry.get("date"),
                    entry,
                )
                for entry in consumption_by_date
                if isinstance(
                    entry,
                    dict,
                )
            )

        else:
            raise DevinAPIError(
                (
                    "Unexpected Devin consumption "
                    "response format. "
                    "'consumption_by_date' must "
                    "be an object or list."
                ),
                status_code=400,
                endpoint=endpoint,
            )

        # ----------------------------------------------------
        # Parse daily records
        # ----------------------------------------------------

        for timestamp, values in entries:
            if timestamp is None:
                continue

            if not isinstance(
                values,
                dict,
            ):
                logger.warning(
                    "Skipping invalid Devin consumption record for user=%s.",
                    user.user_id,
                )

                continue

            products = values.get(
                "acus_by_product",
                {},
            )

            if not isinstance(
                products,
                dict,
            ):
                logger.warning(
                    "Invalid acus_by_product response for user=%s.",
                    user.user_id,
                )

                continue

            try:
                date_timestamp = int(timestamp)

            except (
                TypeError,
                ValueError,
            ):
                logger.warning(
                    "Invalid Devin consumption timestamp=%s for user=%s.",
                    timestamp,
                    user.user_id,
                )

                continue

            for product, acu_value in products.items():
                try:
                    acu = 0.0 if acu_value is None else float(acu_value)

                except (
                    TypeError,
                    ValueError,
                ):
                    logger.warning(
                        "Invalid ACU value=%s for user=%s product=%s.",
                        acu_value,
                        user.user_id,
                        product,
                    )

                    continue

                records.append(
                    DevinACURecord(
                        user_id=user.user_id,
                        date_timestamp=(date_timestamp),
                        acus=acu,
                        product=str(product),
                        org_id=user.org_id,
                        user_name=user.name,
                    )
                )

        logger.info(
            "User ACU collected: user=%s records=%s",
            user.user_id,
            len(records),
        )

        return records

    # ========================================================
    # ENTERPRISE CONSUMPTION
    # ========================================================

    def get_enterprise_consumption(
        self,
        time_after: int | None = None,
        time_before: int | None = None,
    ) -> list[DevinEnterpriseACURecord]:
        """
        Retrieve enterprise-level daily ACU consumption.
        """

        endpoint = "/enterprise/consumption/daily"

        params = self._build_time_params(
            time_after,
            time_before,
        )

        data = self._get(
            endpoint,
            params=params,
        )

        if not isinstance(
            data,
            dict,
        ):
            raise DevinAPIError(
                ("Unexpected response from Devin enterprise consumption API."),
                status_code=400,
                endpoint=endpoint,
            )

        records: list[DevinEnterpriseACURecord] = []

        consumption_by_date = data.get(
            "consumption_by_date",
            [],
        )

        if not consumption_by_date:
            return records

        if isinstance(
            consumption_by_date,
            dict,
        ):
            entries = consumption_by_date.items()

        elif isinstance(
            consumption_by_date,
            list,
        ):
            entries = (
                (
                    entry.get("date"),
                    entry,
                )
                for entry in consumption_by_date
                if isinstance(
                    entry,
                    dict,
                )
            )

        else:
            raise DevinAPIError(
                ("Unexpected Devin enterprise consumption response format."),
                status_code=400,
                endpoint=endpoint,
            )

        for timestamp, values in entries:
            if timestamp is None:
                continue

            if not isinstance(
                values,
                dict,
            ):
                continue

            products = values.get(
                "acus_by_product",
                {},
            )

            if not isinstance(
                products,
                dict,
            ):
                continue

            try:
                date_timestamp = int(timestamp)

            except (
                TypeError,
                ValueError,
            ):
                continue

            for product, acu_value in products.items():
                try:
                    acu = 0.0 if acu_value is None else float(acu_value)

                except (
                    TypeError,
                    ValueError,
                ):
                    logger.warning(
                        "Invalid enterprise ACU value=%s product=%s.",
                        acu_value,
                        product,
                    )

                    continue

                records.append(
                    DevinEnterpriseACURecord(
                        date_timestamp=(date_timestamp),
                        acus=acu,
                        product=str(product),
                    )
                )

        logger.info(
            "Enterprise ACU records: %s",
            len(records),
        )

        return records

    # ========================================================
    # ORGANIZATION CONSUMPTION
    # ========================================================

    def get_organization_consumption(
        self,
        org_id: str,
        time_after: int | None = None,
        time_before: int | None = None,
    ) -> list[DevinOrganizationACURecord]:
        """
        Retrieve organization-level daily ACU consumption.

        This endpoint is treated as optional because it may not
        be available for every Devin account/token.
        """

        endpoint = f"/enterprise/consumption/daily/organizations/{quote(org_id, safe='')}"

        params = self._build_time_params(
            time_after,
            time_before,
        )

        data = self._get(
            endpoint,
            params=params,
            optional=True,
        )

        if data is None:
            return []

        if not isinstance(
            data,
            dict,
        ):
            raise DevinAPIError(
                ("Unexpected response from Devin organization consumption API."),
                status_code=400,
                endpoint=endpoint,
            )

        records: list[DevinOrganizationACURecord] = []

        consumption_by_date = data.get(
            "consumption_by_date",
            [],
        )

        if not consumption_by_date:
            return records

        if isinstance(
            consumption_by_date,
            dict,
        ):
            entries = consumption_by_date.items()

        elif isinstance(
            consumption_by_date,
            list,
        ):
            entries = (
                (
                    entry.get("date"),
                    entry,
                )
                for entry in consumption_by_date
                if isinstance(
                    entry,
                    dict,
                )
            )

        else:
            raise DevinAPIError(
                ("Unexpected Devin organization consumption response format."),
                status_code=400,
                endpoint=endpoint,
            )

        for timestamp, values in entries:
            if timestamp is None:
                continue

            if not isinstance(
                values,
                dict,
            ):
                continue

            products = values.get(
                "acus_by_product",
                {},
            )

            if not isinstance(
                products,
                dict,
            ):
                continue

            try:
                date_timestamp = int(timestamp)

            except (
                TypeError,
                ValueError,
            ):
                continue

            for product, acu_value in products.items():
                try:
                    acu = 0.0 if acu_value is None else float(acu_value)

                except (
                    TypeError,
                    ValueError,
                ):
                    continue

                records.append(
                    DevinOrganizationACURecord(
                        org_id=org_id,
                        date_timestamp=(date_timestamp),
                        acus=acu,
                        product=str(product),
                    )
                )

        logger.info(
            "Organization ACU collected: org=%s records=%s",
            org_id,
            len(records),
        )

        return records

    # ========================================================
    # USAGE METRICS
    # ========================================================

    def get_usage_metrics(
        self,
        time_after: int | None = None,
        time_before: int | None = None,
    ) -> DevinUsageMetrics:
        """
        Retrieve Devin enterprise usage metrics.

        This endpoint is optional because the token may not
        have access to it.
        """

        endpoint = "/enterprise/metrics/usage"

        params = self._build_time_params(
            time_after,
            time_before,
        )

        data = self._get(
            endpoint,
            params=params,
            optional=True,
        )

        if data is None:
            return DevinUsageMetrics(
                sessions=0.0,
                searches=0.0,
                prs_created=0.0,
                prs_merged=0.0,
            )

        if not isinstance(
            data,
            dict,
        ):
            raise DevinAPIError(
                ("Unexpected response from Devin usage metrics API."),
                status_code=400,
                endpoint=endpoint,
            )

        return DevinUsageMetrics(
            sessions=float(
                data.get(
                    "sessions_count",
                    0,
                )
                or 0
            ),
            searches=float(
                data.get(
                    "searches_count",
                    0,
                )
                or 0
            ),
            prs_created=float(
                data.get(
                    "prs_created_count",
                    0,
                )
                or 0
            ),
            prs_merged=float(
                data.get(
                    "prs_merged_count",
                    0,
                )
                or 0
            ),
        )

    # ========================================================
    # TIME PARAMETERS
    # ========================================================

    @staticmethod
    def _build_time_params(
        time_after: int | None,
        time_before: int | None,
    ) -> dict[str, Any]:
        """
        Build optional time range parameters.
        """

        params: dict[str, Any] = {}

        if time_after is not None:
            params["time_after"] = time_after

        if time_before is not None:
            params["time_before"] = time_before

        return params

    # ========================================================
    # CURRENT DEVIN BILLING DAY
    # ========================================================

    @staticmethod
    def get_current_billing_day() -> tuple[int, int]:
        """
        Calculate the current Devin billing-day window.

        The Devin consumption API buckets days on a fixed
        UTC-8 offset (Pacific Standard Time) year-round and
        does not shift for daylight saving. Using a DST-aware
        "America/Los_Angeles" zone here would misalign the
        window by an hour during PDT and cause the API to
        return zero results for otherwise valid days.

        Returns:
            (
                billing_start_timestamp,
                billing_end_timestamp
            )
        """

        pacific_standard = timezone(timedelta(hours=-8))

        now_pacific = datetime.now(pacific_standard)

        billing_start = now_pacific.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

        billing_end = billing_start + timedelta(days=1)

        return (
            int(billing_start.timestamp()),
            int(billing_end.timestamp()),
        )

    # ========================================================
    # CONNECTION TEST
    # ========================================================

    def test_connection(self) -> bool:
        """
        Test Devin API connectivity and authentication.

        IMPORTANT:
        DevinAPIError is intentionally NOT caught here.

        main.py needs the original exception so it can
        classify the failure as:

            401 -> AUTHENTICATION_ERROR
            403 -> AUTHENTICATION_ERROR
            404 -> INVALID_ARGS_ERROR
            429 -> WARNING
            5xx -> GENERIC_ERROR
            network/timeout -> DEVICE_CONNECTION_ERROR
        """

        self._get(
            "/enterprise/members/users",
            params={
                "limit": 1,
            },
        )

        logger.info("Devin API connection test successful.")

        return True
