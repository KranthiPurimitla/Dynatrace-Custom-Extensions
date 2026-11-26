"""
Genesys Cloud Dynatrace Extension - SDK entry class.

SDK lifecycle:
    * initialize()       - called once at startup; schedules _run_collectors() every 5 min
    * _run_collectors()  - Phase 0 + Phase 1 collection; runs every 5 minutes
    * query()            - not overridden; SDK schedules it every minute but base-class
                           default returns IgnoreStatus() (no-op)
    * fastcheck()        - quick credential/connectivity probe before activation
    * on_shutdown()      - cleanup

Business logic lives in helper modules (genesys_client, conversations,
platform_health). Phase 0 (platform health) and Phase 1 (conversations) only.
"""
import sys
from datetime import timedelta
from pathlib import Path

if __package__ in (None, ""):
    # Running as a script (e.g. `python genesyscloud/__main__.py`) instead of
    # `python -m genesyscloud`: put the project root on sys.path so the
    # package-qualified imports below can resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dynatrace_extension import Extension, Status, StatusValue

from genesyscloud.genesys_client import GenesysClient, GenesysAuthError
from genesyscloud.conversations import ConversationsCollector
from genesyscloud.platform_health import PlatformHealthCollector


class GenesysCloudExtension(Extension):

    def initialize(self):
        """Build the shared client and the Phase 0/1 collectors. Runs once."""
        cfg = self._read_config()
        self.client = GenesysClient(
            region=cfg["region"],
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
        )
        self.org_name = cfg["org_name"] or cfg["region"]

        self.conversations = ConversationsCollector(self.client, self.org_name, region=cfg["region"])
        self.platform_health = PlatformHealthCollector(self.client, self.org_name)

        # Override the SDK's default 1-minute query() cadence: schedule our
        # collection method at 5-minute intervals instead.
        self.schedule(self._run_collectors, timedelta(minutes=5))

        self.logger.info(
            "Initialized Genesys Cloud extension (org=%s, region=%s)",
            self.org_name, cfg["region"],
        )

    def _run_collectors(self):
        """
        Runs every 5 minutes (scheduled in initialize()).

        Phase 1 runs first so Phase 0 can report on the API calls it just made.
        Each collector is gated by its feature set, so disabling a phase in the
        monitoring configuration also stops its API traffic.
        """
        if self._enabled(self.conversations.feature_set):
            try:
                self.conversations.collect(self.report_metric)
            except Exception:  # noqa: BLE001 - isolate one phase's failure
                self.logger.exception("Conversations collection failed this cycle")

        # Platform health drains telemetry; run it whenever its feature set is on,
        # even if conversations failed, so we still see the error counters.
        if self._enabled(self.platform_health.feature_set):
            try:
                self.platform_health.collect(self.report_metric)
            except Exception:  # noqa: BLE001
                self.logger.exception("Platform health collection failed this cycle")

    def fastcheck(self) -> Status:
        """Validate region + credentials quickly before full activation."""
        try:
            cfg = self._read_config()
            client = GenesysClient(cfg["region"], cfg["client_id"], cfg["client_secret"])
            client._ensure_token()  # noqa: SLF001 - intentional connectivity probe
            client.close()
            return Status(StatusValue.OK)
        except Exception as exc:  # noqa: BLE001
            return Status(StatusValue.GENERIC_ERROR, f"Genesys connectivity failed: {exc}")

    def on_shutdown(self):
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ internals
    def _read_config(self) -> dict:
        """
        Read connection settings from the activation config.

        activationSchema.json nests them under a 'genesys' object; we fall back
        to the root for resilience.
        """
        root: dict = self.activation_config.config if self.activation_config is not None else {}
        conn: dict = root.get("genesys") or root
        try:
            return {
                "region": conn["region"],
                "client_id": conn["clientId"],
                "client_secret": conn["clientSecret"],
                "org_name": conn.get("orgName", ""),
            }
        except KeyError as exc:
            raise GenesysAuthError(
                f"Missing required activation setting {exc}; "
                "expected region, clientId, clientSecret."
            ) from exc

    def _enabled(self, feature_set: str) -> bool:
        enabled = getattr(self, "enabled_feature_sets", None)
        # If the AG does not supply the map, default to enabled (don't go silent).
        return True if not enabled else feature_set in enabled


def main():
    """Entry point invoked by genesys/__main__.py (python -m genesys)."""
    GenesysCloudExtension().run()


if __name__ == "__main__":
    main()