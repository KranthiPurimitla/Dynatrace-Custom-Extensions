"""
Dynatrace Extension 2.0 - Devin AI

Collects Devin AI enterprise ACU consumption and usage metrics.

Responsibilities:
    - Read Devin configuration from activation.json
    - Create Devin API client
    - Validate Devin API connectivity
    - Retrieve users
    - Retrieve user ACU consumption
    - Retrieve enterprise ACU consumption
    - Retrieve usage metrics
    - Report metrics to Dynatrace
    - Handle errors using Dynatrace StatusValue categories

No CSV files are generated.
No API responses are written to disk.
No credentials are logged.
"""

from datetime import timedelta

from dynatrace_extension import (
    Extension,
    Status,
    StatusValue,
)

from .devin_api import (
    DevinAPIClient,
    DevinAPIError,
    DevinUser,
)


# ============================================================
# METRIC KEYS
# ============================================================

USER_ACU_METRIC = "custom.devin.acu"

ENTERPRISE_ACU_METRIC = "custom.devin.enterprise.acu"

ORGANIZATION_ACU_METRIC = "custom.devin.organization.acu"

TOTAL_ACU_METRIC = "custom.devin.acu.total"

USER_ACU_COUNT_METRIC = "custom.devin.acu.count"

SESSIONS_METRIC = "custom.devin.sessions"

SEARCHES_METRIC = "custom.devin.searches"

PRS_CREATED_METRIC = "custom.devin.prs.created"

PRS_MERGED_METRIC = "custom.devin.prs.merged"

# Devin's ACU/usage APIs are slow to respond for large user
# counts, so collection runs on its own 15-minute schedule
# instead of the SDK's fixed 1-minute "query" interval.
COLLECTION_INTERVAL = timedelta(minutes=15)


# ============================================================
# EXTENSION
# ============================================================


class ExtensionImpl(Extension):
    # ========================================================
    # INITIALIZE
    # ========================================================

    def initialize(self):

        self.logger.info("Initializing Devin AI extension.")

        self.schedule(
            self._collect_devin_metrics,
            COLLECTION_INTERVAL,
        )

    # ========================================================
    # CREATE DEVIN CLIENT
    # ========================================================

    def _create_client(self, endpoint):
        """
        Create Devin API client from the configured endpoint.

        Expected configuration:

        {
            "url": "https://api.devin.ai/v3",
            "apiToken": "..."
        }

        The URL is NOT hard-coded.
        The token is NOT hard-coded.
        """

        if not isinstance(endpoint, dict):
            self.logger.error("Invalid Devin endpoint configuration. Expected an object.")

            return None

        url = endpoint.get("url")

        api_token = endpoint.get("apiToken") or endpoint.get("api_token") or endpoint.get("token")

        # ----------------------------------------------------
        # Validate URL
        # ----------------------------------------------------

        if not url:
            self.logger.error("Devin API URL is missing from endpoint configuration.")

            return None

        if not isinstance(url, str):
            self.logger.error("Devin API URL must be a string.")

            return None

        url = url.strip()

        if not url:
            self.logger.error("Devin API URL cannot be empty.")

            return None

        # ----------------------------------------------------
        # Validate token
        # ----------------------------------------------------

        if not api_token:
            self.logger.error("Devin API token is missing from endpoint configuration.")

            return None

        if not isinstance(api_token, str):
            self.logger.error("Devin API token must be a string.")

            return None

        if not api_token.strip():
            self.logger.error("Devin API token cannot be empty.")

            return None

        # ----------------------------------------------------
        # Create client
        # ----------------------------------------------------

        self.logger.info(
            "Creating Devin API client for URL: %s",
            url,
        )

        try:
            return DevinAPIClient(
                api_token=api_token,
                base_url=url,
            )

        except ValueError as exc:
            self.logger.error(
                "Invalid Devin API configuration: %s",
                exc,
            )

            return None

        except Exception as exc:
            self.logger.exception(
                "Unexpected error creating Devin API client: %s",
                exc,
            )

            return None

    # ========================================================
    # COLLECT DEVIN METRICS
    # ========================================================

    def _collect_devin_metrics(self):
        """
        Scheduled every COLLECTION_INTERVAL (see initialize()).
        """

        self.logger.info("==================================================")

        self.logger.info("Devin AI metrics collection started.")

        self.logger.info("==================================================")

        try:
            # ------------------------------------------------
            # Validate activation configuration
            # ------------------------------------------------

            endpoints = self.activation_config.get("endpoints")

            if endpoints is None:
                self.logger.error("Devin configuration error: 'endpoints' is missing.")

                return

            if not isinstance(endpoints, list):
                self.logger.error("Devin configuration error: 'endpoints' must be a list.")

                return

            if not endpoints:
                self.logger.error("Devin configuration error: no endpoints configured.")

                return

            # ------------------------------------------------
            # Determine billing window
            # ------------------------------------------------

            try:
                time_after, time_before = DevinAPIClient.get_current_billing_day()

                self.logger.info(
                    "Devin billing window: time_after=%s, time_before=%s",
                    time_after,
                    time_before,
                )

            except Exception as exc:
                self.logger.exception(
                    "Unable to determine Devin billing window: %s",
                    exc,
                )

                return

            # ------------------------------------------------
            # Process endpoints
            # ------------------------------------------------

            for endpoint_index, endpoint in enumerate(
                endpoints,
                start=1,
            ):
                self.logger.info(
                    "Processing Devin endpoint %s/%s.",
                    endpoint_index,
                    len(endpoints),
                )

                self._process_endpoint(
                    endpoint=endpoint,
                    time_after=time_after,
                    time_before=time_before,
                )

        except Exception as exc:
            self.logger.exception(
                "Unexpected error in Devin AI metrics collection: %s",
                exc,
            )

        finally:
            self.logger.info("==================================================")

            self.logger.info("Devin AI metrics collection ended.")

            self.logger.info("==================================================")

    # ========================================================
    # PROCESS ENDPOINT
    # ========================================================

    def _process_endpoint(
        self,
        endpoint,
        time_after,
        time_before,
    ):
        """
        Process one Devin API endpoint.
        """

        client = self._create_client(endpoint)

        if client is None:
            self.logger.error("Skipping Devin endpoint because configuration is invalid.")

            return

        # ----------------------------------------------------
        # CONNECTION TEST
        # ----------------------------------------------------

        self.logger.info("Testing Devin API connection.")

        try:
            connection_ok = client.test_connection()

            if not connection_ok:
                self.logger.error("Devin API connection test failed.")

                return

        except DevinAPIError as exc:
            self._handle_api_error(
                exc,
                "Devin API connection test",
            )

            return

        except Exception as exc:
            self.logger.exception(
                "Unexpected Devin connection error: %s",
                exc,
            )

            return

        self.logger.info("Devin API connection successful.")

        # ----------------------------------------------------
        # USERS
        # ----------------------------------------------------

        try:
            users = client.get_users()

        except DevinAPIError as exc:
            self._handle_api_error(
                exc,
                "Retrieving Devin users",
            )

            return

        except Exception as exc:
            self.logger.exception(
                "Unexpected error retrieving Devin users: %s",
                exc,
            )

            return

        if not users:
            self.logger.warning("Devin API returned no enterprise users.")

            return

        self.logger.info(
            "Total Devin users discovered: %s",
            len(users),
        )

        # ----------------------------------------------------
        # USER ACU
        # ----------------------------------------------------

        user_metric_count = self._collect_user_acu(
            client=client,
            users=users,
            time_after=time_after,
            time_before=time_before,
        )

        # ----------------------------------------------------
        # ENTERPRISE ACU
        # ----------------------------------------------------

        enterprise_metric_count = self._collect_enterprise_acu(
            client=client,
            time_after=time_after,
            time_before=time_before,
        )

        # ----------------------------------------------------
        # ORGANIZATION ACU
        # ----------------------------------------------------

        org_ids = sorted({user.org_id for user in users if user.org_id})

        organization_metric_count = self._collect_organization_acu(
            client=client,
            org_ids=org_ids,
            time_after=time_after,
            time_before=time_before,
        )

        # ----------------------------------------------------
        # USAGE
        # ----------------------------------------------------

        usage_metric_count = self._collect_usage_metrics(
            client=client,
            time_after=time_after,
            time_before=time_before,
        )

        # ----------------------------------------------------
        # SUMMARY
        # ----------------------------------------------------

        self.logger.info("Devin endpoint collection completed.")

        self.logger.info(
            "Users discovered: %s",
            len(users),
        )

        self.logger.info(
            "User ACU metrics sent: %s",
            user_metric_count,
        )

        self.logger.info(
            "Enterprise ACU metrics sent: %s",
            enterprise_metric_count,
        )

        self.logger.info(
            "Organization ACU metrics sent: %s",
            organization_metric_count,
        )

        self.logger.info(
            "Usage metrics sent: %s",
            usage_metric_count,
        )

    # ========================================================
    # USER ACU
    # ========================================================

    def _collect_user_acu(
        self,
        client: DevinAPIClient,
        users: list[DevinUser],
        time_after: int,
        time_before: int,
    ) -> int:

        metric_count = 0

        self.logger.info("Starting Devin user ACU collection.")

        for user in users:
            try:
                records = client.get_user_consumption(
                    user=user,
                    time_after=time_after,
                    time_before=time_before,
                )

                # --------------------------------------------
                # No data
                # --------------------------------------------

                if not records:
                    self.logger.debug(
                        "No ACU data returned for user=%s.",
                        user.user_id,
                    )

                    continue

                # --------------------------------------------
                # Process records
                # --------------------------------------------

                user_record_count = 0

                for record in records:
                    # ----------------------------------------
                    # Ignore zero / null consumption
                    # ----------------------------------------

                    if record.acus <= 0:
                        self.logger.debug(
                            "Skipping zero ACU record: user_id=%s, product=%s",
                            record.user_id,
                            record.product,
                        )

                        continue

                    dimensions = {
                        "user_id": (record.user_id or "unknown"),
                        "user_name": (record.user_name or "unknown"),
                        "product": (record.product or "unknown"),
                    }

                    # org_id is omitted (not defaulted to
                    # "unknown") when a user has no org-scoped
                    # role assignment, so the devinai:organization
                    # topology rule doesn't create a bogus entity
                    # for users who genuinely have no org.
                    if record.org_id:
                        dimensions["org_id"] = record.org_id

                    # ----------------------------------------
                    # Log metric
                    # ----------------------------------------

                    self.logger.info(
                        "Sending Devin metric: "
                        "metric=%s, "
                        "value=%s, "
                        "user_id=%s, "
                        "user_name=%s, "
                        "org_id=%s, "
                        "product=%s, "
                        "timestamp=%s",
                        USER_ACU_METRIC,
                        record.acus,
                        dimensions["user_id"],
                        dimensions["user_name"],
                        record.org_id or "unknown",
                        dimensions["product"],
                        record.date_timestamp,
                    )

                    # ----------------------------------------
                    # Send metric
                    # ----------------------------------------

                    self.report_metric(
                        USER_ACU_METRIC,
                        record.acus,
                        dimensions=dimensions,
                    )

                    metric_count += 1

                    user_record_count += 1

                # ------------------------------------------------
                # ACU RECORD COUNT (distinct products with
                # usage today, not the summed ACU value)
                # ------------------------------------------------

                if user_record_count > 0:
                    count_dimensions = {
                        "user_id": (user.user_id or "unknown"),
                        "user_name": (user.name or "unknown"),
                    }

                    if user.org_id:
                        count_dimensions["org_id"] = user.org_id

                    self.logger.info(
                        "Sending Devin metric: metric=%s, value=%s, user_id=%s",
                        USER_ACU_COUNT_METRIC,
                        user_record_count,
                        count_dimensions["user_id"],
                    )

                    self.report_metric(
                        USER_ACU_COUNT_METRIC,
                        user_record_count,
                        dimensions=count_dimensions,
                    )

                    metric_count += 1

            except DevinAPIError as exc:
                self._handle_api_error(
                    exc,
                    (f"Collecting ACU for user={user.user_id}"),
                )

                # Continue with next user.
                continue

            except Exception as exc:
                self.logger.exception(
                    "Unexpected error processing Devin user=%s: %s",
                    user.user_id,
                    exc,
                )

                # Continue with next user.
                continue

        self.logger.info(
            "Devin user ACU collection completed. Metrics sent=%s",
            metric_count,
        )

        return metric_count

    # ========================================================
    # ENTERPRISE ACU
    # ========================================================

    def _collect_enterprise_acu(
        self,
        client: DevinAPIClient,
        time_after: int,
        time_before: int,
    ) -> int:

        metric_count = 0

        total_acus = 0.0

        self.logger.info("Starting Devin enterprise ACU collection.")

        try:
            records = client.get_enterprise_consumption(
                time_after=time_after,
                time_before=time_before,
            )

            if not records:
                self.logger.warning("Devin enterprise ACU API returned no data.")

                return 0

            for record in records:
                if record.acus <= 0:
                    continue

                total_acus += record.acus

                dimensions = {
                    "product": (record.product or "unknown"),
                }

                self.logger.info(
                    "Sending Devin metric: metric=%s, value=%s, product=%s, timestamp=%s",
                    ENTERPRISE_ACU_METRIC,
                    record.acus,
                    dimensions["product"],
                    record.date_timestamp,
                )

                self.report_metric(
                    ENTERPRISE_ACU_METRIC,
                    record.acus,
                    dimensions=dimensions,
                )

                metric_count += 1

            # ----------------------------------------------------
            # TOTAL ACU (all products combined)
            # ----------------------------------------------------

            if total_acus > 0:
                self.logger.info(
                    "Sending Devin metric: metric=%s, value=%s",
                    TOTAL_ACU_METRIC,
                    total_acus,
                )

                self.report_metric(
                    TOTAL_ACU_METRIC,
                    total_acus,
                    dimensions={},
                )

                metric_count += 1

        except DevinAPIError as exc:
            self._handle_api_error(
                exc,
                "Collecting enterprise ACU",
            )

        except Exception as exc:
            self.logger.exception(
                "Unexpected enterprise ACU error: %s",
                exc,
            )

        self.logger.info(
            "Enterprise ACU collection completed. Metrics sent=%s",
            metric_count,
        )

        return metric_count

    # ========================================================
    # ORGANIZATION ACU
    # ========================================================

    def _collect_organization_acu(
        self,
        client: DevinAPIClient,
        org_ids: list[str],
        time_after: int,
        time_before: int,
    ) -> int:

        metric_count = 0

        self.logger.info(
            "Starting Devin organization ACU collection. Organizations=%s",
            len(org_ids),
        )

        for org_id in org_ids:
            try:
                records = client.get_organization_consumption(
                    org_id=org_id,
                    time_after=time_after,
                    time_before=time_before,
                )

                if not records:
                    self.logger.debug(
                        "No organization ACU data returned for org_id=%s.",
                        org_id,
                    )

                    continue

                for record in records:
                    if record.acus <= 0:
                        continue

                    dimensions = {
                        "org_id": org_id,
                        "product": (record.product or "unknown"),
                    }

                    self.logger.info(
                        "Sending Devin metric: metric=%s, value=%s, org_id=%s, product=%s, timestamp=%s",
                        ORGANIZATION_ACU_METRIC,
                        record.acus,
                        dimensions["org_id"],
                        dimensions["product"],
                        record.date_timestamp,
                    )

                    self.report_metric(
                        ORGANIZATION_ACU_METRIC,
                        record.acus,
                        dimensions=dimensions,
                    )

                    metric_count += 1

            except DevinAPIError as exc:
                self._handle_api_error(
                    exc,
                    (f"Collecting organization ACU for org_id={org_id}"),
                )

                # Continue with next organization.
                continue

            except Exception as exc:
                self.logger.exception(
                    "Unexpected error processing Devin org_id=%s: %s",
                    org_id,
                    exc,
                )

                # Continue with next organization.
                continue

        self.logger.info(
            "Devin organization ACU collection completed. Metrics sent=%s",
            metric_count,
        )

        return metric_count

    # ========================================================
    # USAGE METRICS
    # ========================================================

    def _collect_usage_metrics(
        self,
        client: DevinAPIClient,
        time_after: int,
        time_before: int,
    ) -> int:

        metric_count = 0

        self.logger.info("Starting Devin usage metrics collection.")

        try:
            usage = client.get_usage_metrics(
                time_after=time_after,
                time_before=time_before,
            )

            if usage is None:
                self.logger.warning("Devin usage API returned no data.")

                return 0

            metrics = [
                (
                    SESSIONS_METRIC,
                    usage.sessions,
                ),
                (
                    SEARCHES_METRIC,
                    usage.searches,
                ),
                (
                    PRS_CREATED_METRIC,
                    usage.prs_created,
                ),
                (
                    PRS_MERGED_METRIC,
                    usage.prs_merged,
                ),
            ]

            for metric_key, value in metrics:
                self.logger.info(
                    "Sending Devin metric: metric=%s, value=%s",
                    metric_key,
                    value,
                )

                self.report_metric(
                    metric_key,
                    value,
                    dimensions={},
                )

                metric_count += 1

        except DevinAPIError as exc:
            self._handle_api_error(
                exc,
                "Collecting Devin usage metrics",
            )

        except Exception as exc:
            self.logger.exception(
                "Unexpected Devin usage metrics error: %s",
                exc,
            )

        self.logger.info(
            "Devin usage metrics collection completed. Metrics sent=%s",
            metric_count,
        )

        return metric_count

    # ========================================================
    # API ERROR HANDLER
    # ========================================================

    def _handle_api_error(
        self,
        error: DevinAPIError,
        operation: str,
    ):
        """
        Convert Devin API failures into meaningful
        Dynatrace status categories for logging and
        fastcheck behavior.

        Query execution continues where possible.
        """

        status_code = error.status_code

        # ----------------------------------------------------
        # 401
        # ----------------------------------------------------

        if status_code == 401:
            self.logger.error(
                "AUTHENTICATION_ERROR: %s failed because Devin rejected the API credentials. Details=%s",
                operation,
                error,
            )

            return StatusValue.AUTHENTICATION_ERROR

        # ----------------------------------------------------
        # 403
        # ----------------------------------------------------

        if status_code == 403:
            self.logger.error(
                "AUTHENTICATION_ERROR: %s is not authorized to access the Devin API endpoint. Details=%s",
                operation,
                error,
            )

            return StatusValue.AUTHENTICATION_ERROR

        # ----------------------------------------------------
        # 404
        # ----------------------------------------------------

        if status_code == 404:
            self.logger.error(
                "INVALID_ARGS_ERROR: "
                "%s failed because the Devin "
                "API endpoint/resource was not found. "
                "Details=%s",
                operation,
                error,
            )

            return StatusValue.INVALID_ARGS_ERROR

        # ----------------------------------------------------
        # 429
        # ----------------------------------------------------

        if status_code == 429:
            self.logger.warning(
                "WARNING: "
                "%s was rate limited by Devin. "
                "The API client will retry where supported. "
                "Details=%s",
                operation,
                error,
            )

            return StatusValue.WARNING

        # ----------------------------------------------------
        # 5xx
        # ----------------------------------------------------

        if status_code is not None and status_code >= 500:
            self.logger.error(
                "GENERIC_ERROR: %s failed because Devin returned a server-side error. HTTP=%s Details=%s",
                operation,
                status_code,
                error,
            )

            return StatusValue.GENERIC_ERROR

        # ----------------------------------------------------
        # Connection / unknown HTTP status
        # ----------------------------------------------------

        if status_code is None:
            self.logger.error(
                "DEVICE_CONNECTION_ERROR: %s could not communicate with Devin. Details=%s",
                operation,
                error,
            )

            return StatusValue.DEVICE_CONNECTION_ERROR

        # ----------------------------------------------------
        # Everything else
        # ----------------------------------------------------

        self.logger.error(
            "UNKNOWN_ERROR: %s failed. HTTP=%s Details=%s",
            operation,
            status_code,
            error,
        )

        return StatusValue.UNKNOWN_ERROR

    # ========================================================
    # FASTCHECK
    # ========================================================

    def fastcheck(self) -> Status:
        """
        Validate whether this ActiveGate can execute
        the Devin AI extension.

        Fastcheck is intentionally stricter than query().
        """

        self.logger.info("Running Devin AI fastcheck.")

        # ----------------------------------------------------
        # Configuration validation
        # ----------------------------------------------------

        try:
            endpoints = self.activation_config.get("endpoints")

        except Exception as exc:
            self.logger.exception(
                "INVALID_CONFIG_ERROR: Unable to read Devin activation configuration: %s",
                exc,
            )

            return Status(StatusValue.INVALID_CONFIG_ERROR)

        if endpoints is None:
            self.logger.error("INVALID_CONFIG_ERROR: 'endpoints' is missing from Devin configuration.")

            return Status(StatusValue.INVALID_CONFIG_ERROR)

        if not isinstance(endpoints, list):
            self.logger.error("INVALID_CONFIG_ERROR: 'endpoints' must be a list.")

            return Status(StatusValue.INVALID_CONFIG_ERROR)

        if not endpoints:
            self.logger.error("INVALID_CONFIG_ERROR: No Devin API endpoints configured.")

            return Status(StatusValue.INVALID_CONFIG_ERROR)

        # ----------------------------------------------------
        # Test every endpoint
        # ----------------------------------------------------

        for endpoint_index, endpoint in enumerate(
            endpoints,
            start=1,
        ):
            self.logger.info(
                "Fastcheck: testing Devin endpoint %s/%s.",
                endpoint_index,
                len(endpoints),
            )

            # ----------------------------------------------
            # Validate endpoint object
            # ----------------------------------------------

            if not isinstance(endpoint, dict):
                self.logger.error(
                    "INVALID_CONFIG_ERROR: Devin endpoint %s is not an object.",
                    endpoint_index,
                )

                return Status(StatusValue.INVALID_CONFIG_ERROR)

            url = endpoint.get("url")

            api_token = endpoint.get("apiToken") or endpoint.get("api_token") or endpoint.get("token")

            # ----------------------------------------------
            # URL validation
            # ----------------------------------------------

            if not url:
                self.logger.error(
                    "INVALID_CONFIG_ERROR: Devin API URL is missing for endpoint %s.",
                    endpoint_index,
                )

                return Status(StatusValue.INVALID_CONFIG_ERROR)

            if not isinstance(url, str):
                self.logger.error("INVALID_CONFIG_ERROR: Devin API URL must be a string.")

                return Status(StatusValue.INVALID_CONFIG_ERROR)

            if not url.strip():
                self.logger.error("INVALID_CONFIG_ERROR: Devin API URL is empty.")

                return Status(StatusValue.INVALID_CONFIG_ERROR)

            # ----------------------------------------------
            # Token validation
            # ----------------------------------------------

            if not api_token:
                self.logger.error("INVALID_CONFIG_ERROR: Devin API token is missing.")

                return Status(StatusValue.INVALID_CONFIG_ERROR)

            # ----------------------------------------------
            # Create client
            # ----------------------------------------------

            try:
                client = DevinAPIClient(
                    api_token=api_token,
                    base_url=url,
                )

            except ValueError as exc:
                self.logger.error(
                    "INVALID_CONFIG_ERROR: Invalid Devin configuration: %s",
                    exc,
                )

                return Status(StatusValue.INVALID_CONFIG_ERROR)

            except Exception as exc:
                self.logger.exception(
                    "UNKNOWN_ERROR: Unable to create Devin API client: %s",
                    exc,
                )

                return Status(StatusValue.UNKNOWN_ERROR)

            # ----------------------------------------------
            # API connectivity
            # ----------------------------------------------

            try:
                client.test_connection()

            except DevinAPIError as exc:
                status = self._handle_api_error(
                    exc,
                    "Devin fastcheck",
                )

                return Status(status)

            except Exception as exc:
                self.logger.exception(
                    "UNKNOWN_ERROR: Unexpected Devin fastcheck error: %s",
                    exc,
                )

                return Status(StatusValue.UNKNOWN_ERROR)

        self.logger.info("Devin AI fastcheck successful.")

        return Status(StatusValue.OK)


# ============================================================
# MAIN
# ============================================================


def main():

    ExtensionImpl(name="devinai").run()


if __name__ == "__main__":
    main()
