import re
import time
from datetime import timedelta

import jwt
import requests
from dynatrace_extension import Extension, Status, StatusValue

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

BASE_URL = "https://api.pure1.purestorage.com"


# =====================================================
# JWT GENERATION
# =====================================================

def generate_jwt(app_id: str, private_key: str) -> str:
    # The "secret" config field is a single-line input, so PEM keys are
    # pasted with newlines escaped as one or more literal backslashes
    # followed by "n" (depending on whether the value was copied from raw
    # JSON text or an already-decoded string). Collapse any such run into
    # a real newline rather than assuming a fixed number of backslashes.
    private_key = re.sub(r"\\+n", "\n", private_key)

    now = int(time.time())

    payload = {
        "iss": app_id,
        "iat": now,
        "exp": now + 86400
    }

    token = jwt.encode(
        payload,
        private_key,
        algorithm="RS256"
    )

    return token


# =====================================================
# ACCESS TOKEN
# =====================================================

def get_access_token(jwt_token: str) -> str:
    url = f"{BASE_URL}/oauth2/1.0/token"

    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": jwt_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt"
    }

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"
    }

    response = requests.post(
        url,
        data=payload,
        headers=headers,
        timeout=60
    )

    if not response.ok:
        raise requests.HTTPError(
            f"{response.status_code} {response.reason} for url: {response.url} "
            f"- body: {response.text}",
            response=response
        )

    return response.json()["access_token"]


# =====================================================
# GET VOLUMES (PAGINATED)
# =====================================================

def get_volumes(access_token: str) -> list:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }

    results = []
    offset = 0
    limit = 100

    while True:
        response = requests.get(
            f"{BASE_URL}/api/1.0/volumes",
            headers=headers,
            params={
                "limit": limit,
                "offset": offset,
                "sort": "name"
            },
            timeout=60
        )

        response.raise_for_status()

        items = response.json().get("items", [])

        for volume in items:
            arrays = volume.get("arrays", [])
            cluster_name = arrays[0].get("name", "") if arrays else ""

            results.append(
                {
                    "volume_id": volume.get("id"),
                    "volume_name": volume.get("name"),
                    "cluster_name": cluster_name,
                    "provisioned": volume.get("provisioned"),
                    "serial": volume.get("serial"),
                    "created": volume.get("created"),
                    "as_of": volume.get("_as_of")
                }
            )

        if len(items) < limit:
            break

        offset += limit

    return results


# =====================================================
# GET PHYSICAL CAPACITY (BATCHED)
# =====================================================

def get_physical_capacity_lookup(
    access_token: str,
    volume_ids: list,
    logger,
    batch_size: int = 32,
    max_retries: int = 3,
    retry_backoff_seconds: float = 5.0
) -> dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }

    end_time = int(time.time() * 1000)
    start_time = end_time - (30 * 24 * 60 * 60 * 1000)

    lookup = {}

    for start in range(0, len(volume_ids), batch_size):
        batch = volume_ids[start:start + batch_size]
        resource_ids = ",".join(f"'{vid}'" for vid in batch)

        params = {
            "names": "'volume_physical_capacity'",
            "resource_ids": resource_ids,
            "aggregation": "'avg'",
            "resolution": 86400000,
            "start_time": start_time,
            "end_time": end_time
        }

        items = None

        for attempt in range(1, max_retries + 1):
            try:
                response = requests.get(
                    f"{BASE_URL}/api/1.0/metrics/history",
                    headers=headers,
                    params=params,
                    timeout=60
                )

                response.raise_for_status()
                items = response.json().get("items", [])
                break
            except requests.exceptions.RequestException as e:
                if attempt < max_retries:
                    logger.warning(
                        f"Retrying physical capacity batch "
                        f"{start}-{start + len(batch)} "
                        f"(attempt {attempt}/{max_retries}): {e}"
                    )
                    time.sleep(retry_backoff_seconds)
                else:
                    # Don't let a persistently failing batch take down
                    # physical capacity reporting for every other volume,
                    # or the provisioned-capacity metrics already retrieved
                    # for this endpoint.
                    logger.error(
                        f"Skipping physical capacity batch "
                        f"{start}-{start + len(batch)} "
                        f"after {max_retries} attempts: {e}"
                    )

        if items is None:
            continue

        for item in items:
            resources = item.get("resources", [])

            if not resources:
                continue

            volume_id = resources[0].get("id")
            data = item.get("data", [])

            if not data:
                lookup[volume_id] = None
                logger.debug(
                    f"No physical capacity samples in the last 30 days "
                    f"for volume_id '{volume_id}' (empty data set)"
                )
                continue

            latest_value = data[-1][1]
            lookup[volume_id] = (
                float(latest_value)
                if latest_value is not None
                else None
            )

        # Volumes Pure1's history API didn't return a resource for at all
        # (as opposed to returning one with an empty data set).
        missing = [vid for vid in batch if vid not in lookup]
        if missing:
            logger.debug(
                f"No physical capacity history entry returned "
                f"for volume_ids: {missing}"
            )

    return lookup


# =====================================================
# USED PERCENTAGE (CALCULATED)
# =====================================================

def calculate_used_percentage(physical_capacity, provisioned):
    """
    used_percentage = (physical_capacity / provisioned) * 100

    Returns None if the percentage cannot be calculated (missing values,
    None values, or a zero/invalid provisioned capacity), so the caller
    can skip reporting the metric for that volume.
    """
    if physical_capacity is None or provisioned is None:
        return None

    try:
        physical_capacity = float(physical_capacity)
        provisioned = float(provisioned)
    except (TypeError, ValueError):
        return None

    if provisioned == 0:
        return None

    return (physical_capacity / provisioned) * 100


# =====================================================
# EXTENSION IMPLEMENTATION
# =====================================================

class ExtensionImpl(Extension):

    def initialize(self):
        """
        Poll Pure1 on its own 5-minute schedule instead of the SDK's
        hardcoded 1-minute `query()` interval: a full run (pagination over
        all volumes plus batched physical-capacity lookups) routinely takes
        longer than 60 seconds.
        """
        self.schedule(self.poll_pure1, timedelta(minutes=5))

    def poll_pure1(self):
        self.logger.info("poll_pure1 method started for custom_pure.")

        for endpoint in self.activation_config["endpoints"]:
            app_id = endpoint.get("app_id")
            private_key = endpoint.get("private_key")

            if not app_id or not private_key:
                self.logger.error(
                    "Skipping endpoint: missing app_id or private_key"
                )
                continue

            self.logger.debug(
                f"Running endpoint with app_id '{app_id}'"
            )

            try:
                # Step 1: Generate JWT
                self.logger.debug("Generating JWT...")
                jwt_token = generate_jwt(app_id, private_key)

                # Step 2: Exchange JWT for access token
                self.logger.debug("Getting access token...")
                access_token = get_access_token(jwt_token)

                # Step 3: Retrieve volumes (paginated)
                self.logger.debug("Retrieving volumes...")
                volumes = get_volumes(access_token)

                self.logger.info(
                    f"Retrieved {len(volumes)} volumes "
                    f"from endpoint '{app_id}'"
                )

                # Step 4: Retrieve physical capacity (batched)
                volume_ids = [
                    v["volume_id"]
                    for v in volumes
                    if v.get("volume_id")
                ]

                self.logger.debug(
                    f"Fetching physical capacity "
                    f"for {len(volume_ids)} volumes..."
                )

                physical_lookup = get_physical_capacity_lookup(
                    access_token,
                    volume_ids,
                    self.logger
                )

                # Step 5: Report metrics per volume
                for volume in volumes:
                    volume_id = volume["volume_id"]
                    volume_name = volume["volume_name"]
                    cluster_name = volume["cluster_name"]
                    provisioned = volume.get("provisioned")
                    physical_capacity = physical_lookup.get(volume_id)

                    dimensions = {
                        "volume_id": volume_id or "",
                        "volume_name": volume_name or "",
                        "cluster_name": cluster_name or ""
                    }

                    # Provisioned capacity in bytes
                    if provisioned is not None:
                        self.report_metric(
                            "custompure1.volume.provisioned",
                            float(provisioned),
                            dimensions=dimensions
                        )

                    # Physical capacity in bytes (30-day avg)
                    if physical_capacity is not None:
                        self.report_metric(
                            "custompure1.volume.physicalcapacity",
                            physical_capacity,
                            dimensions=dimensions
                        )

                    # Used percentage (physical capacity / provisioned * 100)
                    used_percentage = calculate_used_percentage(
                        physical_capacity, provisioned
                    )
                    if used_percentage is not None:
                        self.report_metric(
                            "custompure1.volume.used_percentage",
                            used_percentage,
                            dimensions=dimensions
                        )
                    else:
                        self.logger.debug(
                            f"Skipping used_percentage for volume "
                            f"'{volume_name}' ({volume_id}): "
                            f"physical_capacity={physical_capacity}, "
                            f"provisioned={provisioned}"
                        )

                self.logger.info(
                    f"Reported metrics for "
                    f"{len(volumes)} volumes "
                    f"from APPID '{app_id}'."
                )

            except Exception as e:
                self.logger.error(
                    f"Error querying APPID '{app_id}': {e}"
                )

        self.logger.info("poll_pure1 method ended for custom_pure.")

    def fastcheck(self) -> Status:
        """
        Use to check if the extension can run.
        If this ActiveGate cannot run this extension, raise an Exception
        or return StatusValue.ERROR.
        """
        return Status(StatusValue.OK)


def main():
    ExtensionImpl(name="custom_pure").run()


if __name__ == "__main__":
    main()