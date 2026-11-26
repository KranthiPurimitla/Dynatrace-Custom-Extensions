import requests
import base64
from collections import defaultdict
from dynatrace_extension import Extension, Status, StatusValue


class ExtensionImpl(Extension):

    def query(self):
        self.logger.info("query() started.")

        for endpoint in self.activation_config["endpoints"]:

            base_url = endpoint["url"].rstrip("/")
            username = endpoint["username"]
            password = endpoint["password"]

            self.logger.info(f"Processing NetApp endpoint: {base_url}")

            try:
                # ------------------------------------------------------------------
                # Build auth headers ONCE (DO NOT change auth logic)
                # ------------------------------------------------------------------
                token = base64.b64encode(
                    f"{username}:{password}".encode("utf-8")
                ).decode("utf-8")

                headers = {
                    "Authorization": f"Basic {token}",
                    "Accept": "application/json"
                }

                # ------------------------------------------------------------------
                # 1. Fetch cluster name explicitly
                # ------------------------------------------------------------------
                cluster_name = self.get_cluster_name(base_url, headers)

                # ------------------------------------------------------------------
                # 2. Fetch CIFS session data
                # ------------------------------------------------------------------
                response_json = self.get_cifs_sessions(base_url, headers)
                records = response_json.get("records", [])

                # ------------------------------------------------------------------
                # 3. Count sessions per SVM
                # ------------------------------------------------------------------
                svm_session_counts = defaultdict(int)

                for record in records:
                    svm_name = record.get("svm", {}).get("name", "unknown")
                    svm_session_counts[svm_name] += 1

                # ------------------------------------------------------------------
                # 4. Report per-SVM session metrics
                # ------------------------------------------------------------------
                for svm, count in svm_session_counts.items():
                    self.report_metric(
                        "netapp.cifs.sessions",
                        count,
                        dimensions={
                            "cluster": cluster_name,
                            "svm": svm
                        }
                    )

                # ------------------------------------------------------------------
                # 5. Report TOTAL session count
                # ------------------------------------------------------------------
                self.report_metric(
                    "netapp.cifs.total_sessions",
                    len(records),
                    dimensions={
                        "cluster": cluster_name
                    }
                )

                # ------------------------------------------------------------------
                # 6. Fetch volume space metrics
                # ------------------------------------------------------------------
                volumes = self.get_volumes(base_url, headers)

                for vol in volumes:
                    volume_name = vol.get("name", "unknown")
                    svm_name = vol.get("svm", {}).get("name", "unknown")

                    space = vol.get("space", {})
                    space_used = space.get("used")
                    space_size = space.get("size")
                    space_available = space.get("available")

                    # Calculate space used percent safely
                    space_used_percent = None
                    if space_used is not None and space_size:
                        space_used_percent = (space_used / space_size) * 100

                    # Report metrics
                    if space_used is not None:
                        self.report_metric(
                            "netapp.volume.space.used",
                            space_used,
                            dimensions={
                                "cluster": cluster_name,
                                "svm": svm_name,
                                "volume": volume_name
                            }
                        )

                    if space_available is not None:
                        self.report_metric(
                            "netapp.volume.space.available",
                            space_available,
                            dimensions={
                                "cluster": cluster_name,
                                "svm": svm_name,
                                "volume": volume_name
                            }
                        )

                    if space_used_percent is not None:
                        self.report_metric(
                            "netapp.volume.space_used_percent",
                            space_used_percent,
                            dimensions={
                                "cluster": cluster_name,
                                "svm": svm_name,
                                "volume": volume_name
                            }
                        )

                self.logger.info(
                    f"Metrics collected | "
                    f"Cluster={cluster_name}, "
                    f"CIFS sessions={len(records)}, "
                    f"Volumes={len(volumes)}"
                )

            except Exception as e:
                self.logger.error(
                    f"Failed to collect metrics from {base_url}: {e}"
                )

        self.logger.info("query() ended.")

    # ----------------------------------------------------------------------
    # Fetch cluster name
    # ----------------------------------------------------------------------
    def get_cluster_name(self, base_url: str, headers: dict) -> str:
        url = f"{base_url}/api/cluster"
        self.logger.debug(f"Fetching cluster info: {url}")

        response = requests.get(
            url,
            headers=headers,
            verify=False,
            timeout=30
        )

        if response.status_code != 200:
            self.logger.warning(
                f"Failed to fetch cluster name: {response.text}"
            )
            return "unknown"

        return response.json().get("name", "unknown")

    # ----------------------------------------------------------------------
    # Fetch CIFS sessions
    # ----------------------------------------------------------------------
    def get_cifs_sessions(self, base_url: str, headers: dict):
        url = f"{base_url}/api/protocols/cifs/sessions"
        self.logger.debug(f"Calling NetApp API: {url}")

        response = requests.get(
            url,
            headers=headers,
            verify=False,
            timeout=30
        )

        if response.status_code != 200:
            raise Exception(
                f"HTTP {response.status_code}: {response.text}"
            )

        return response.json()

    # ----------------------------------------------------------------------
    # Fetch volume space metrics
    # ----------------------------------------------------------------------
    def get_volumes(self, base_url: str, headers: dict):
        url = (
            f"{base_url}/api/storage/volumes"
            "?fields=name,svm.name,space.size,space.used,space.available"
        )

        self.logger.debug(f"Calling NetApp API: {url}")

        response = requests.get(
            url,
            headers=headers,
            verify=False,
            timeout=30
        )

        if response.status_code != 200:
            raise Exception(
                f"HTTP {response.status_code}: {response.text}"
            )

        return response.json().get("records", [])

    def fastcheck(self):
        return Status(StatusValue.OK)


def main():
    extension = ExtensionImpl()
    extension.run()


if __name__ == "__main__":
    main()
