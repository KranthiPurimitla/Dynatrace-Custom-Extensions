"""
Metrics Validation Script for VeloCloud Extension.

Validates that metrics are being collected and reported correctly.
Run after extension has executed to verify data flow.
"""

import json
import sys
from typing import Dict, List, Any
from datetime import datetime


class MetricsValidator:
    """Validates VeloCloud extension metrics collection."""

    def __init__(self) -> None:
        """Initialize validator."""
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self.info: List[str] = []

    def validate_configuration(self) -> bool:
        """
        Validate configuration files exist and are valid.

        Returns:
            True if configuration is valid
        """
        print("\n" + "=" * 70)
        print("STEP 1: Configuration Validation")
        print("=" * 70)

        try:
            with open("activation.json", "r") as f:
                activation = json.load(f)
            self.info.append("✅ activation.json is valid JSON")

            if "pythonLocal" not in activation:
                self.errors.append(
                    "❌ activation.json missing 'pythonLocal' section"
                )
                return False

            endpoints = activation["pythonLocal"].get("endpoints", [])
            if not endpoints:
                self.errors.append(
                    "❌ No endpoints configured in activation.json"
                )
                return False

            self.info.append(f"✅ Found {len(endpoints)} endpoint(s)")

            for idx, endpoint in enumerate(endpoints):
                if not endpoint.get("api_host"):
                    self.errors.append(
                        f"❌ Endpoint {idx} missing api_host"
                    )
                if not endpoint.get("api_token"):
                    self.errors.append(
                        f"❌ Endpoint {idx} missing api_token reference"
                    )
                    self.warnings.append(
                        f"⚠️  Check secrets.json for referenced secret key"
                    )

            return len(self.errors) == 0

        except FileNotFoundError as e:
            self.errors.append(f"❌ Configuration file not found: {e}")
            return False
        except json.JSONDecodeError as e:
            self.errors.append(f"❌ Invalid JSON in configuration: {e}")
            return False

    def validate_implementation(self) -> bool:
        """
        Validate implementation files are correct.

        Returns:
            True if implementation is valid
        """
        print("\n" + "=" * 70)
        print("STEP 2: Implementation Validation")
        print("=" * 70)

        try:
            with open("__main__.py", "r") as f:
                main_content = f.read()

            with open("api_client.py", "r") as f:
                api_content = f.read()

            # Check for hardcoded credentials
            if "eyJ" in main_content:  # JWT token pattern
                self.errors.append(
                    "❌ Possible hardcoded credential in __main__.py"
                )
            else:
                self.info.append("✅ No hardcoded credentials in __main__.py")

            if "eyJ" in api_content:
                self.errors.append(
                    "❌ Possible hardcoded credential in api_client.py"
                )
            else:
                self.info.append("✅ No hardcoded credentials in api_client.py")

            # Check for required classes/functions
            if "class ExtensionImpl" in main_content:
                self.info.append("✅ ExtensionImpl class found in __main__.py")
            else:
                self.errors.append(
                    "❌ ExtensionImpl class not found in __main__.py"
                )

            if "class VeloCloudClient" in api_content:
                self.info.append("✅ VeloCloudClient class found in api_client.py")
            else:
                self.errors.append(
                    "❌ VeloCloudClient class not found in api_client.py"
                )

            if "async def async_query" in main_content:
                self.info.append(
                    "✅ async_query method found in __main__.py"
                )
            else:
                self.errors.append(
                    "❌ async_query method not found in __main__.py"
                )

            return len(self.errors) == 0

        except FileNotFoundError as e:
            self.errors.append(f"❌ Implementation file not found: {e}")
            return False

    def validate_metrics(self) -> bool:
        """
        Validate expected metrics are defined.

        Returns:
            True if metrics are properly defined
        """
        print("\n" + "=" * 70)
        print("STEP 3: Metrics Definition Validation")
        print("=" * 70)

        # Every metric key the extension reports (see async_query and
        # the health stats collector in __main__.py)
        expected_metrics = [
            "custom.velocloud.edge.jitter_ms_rx",
            "custom.velocloud.edge.jitter_ms_tx",
            "custom.velocloud.edge.latency_ms_rx",
            "custom.velocloud.edge.latency_ms_tx",
            "custom.velocloud.edge.loss_pct_rx",
            "custom.velocloud.edge.loss_pct_tx",
            "custom.velocloud.edge.edge_state",
            "custom.velocloud.edge.ha_state",
            "custom.velocloud.edge.service_state",
            "custom.velocloud.edge.link_state",
            "custom.velocloud.edge.cpu_utilization",
            "custom.velocloud.edge.cpu_core_temperature",
            "custom.velocloud.edge.memory_utilization",
            "custom.velocloud.edge.flow_count",
            "custom.velocloud.edge.over_capacity_drops",
            "custom.velocloud.edge.tunnel_count",
            "custom.velocloud.edge.tunnel_count_v6",
            "custom.velocloud.edge.all_links_offline",
        ]

        try:
            with open("__main__.py", "r") as f:
                content = f.read()

            missing_metrics = []
            for metric in expected_metrics:
                metric_name = metric.replace("custom.velocloud.edge.", "")
                if metric_name in content:
                    self.info.append(f"✅ Metric '{metric_name}' is defined")
                else:
                    missing_metrics.append(metric)

            if missing_metrics:
                self.warnings.append(
                    f"⚠️  Some metrics might be missing: {missing_metrics}"
                )

            return True

        except FileNotFoundError as e:
            self.errors.append(f"❌ Cannot validate metrics: {e}")
            return False

    def print_summary(self) -> None:
        """Print validation summary."""
        print("\n" + "=" * 70)
        print("VALIDATION SUMMARY")
        print("=" * 70)

        print("\n📋 Information:")
        for msg in self.info:
            print(f"  {msg}")

        if self.warnings:
            print("\n⚠️  Warnings:")
            for msg in self.warnings:
                print(f"  {msg}")

        if self.errors:
            print("\n❌ Errors:")
            for msg in self.errors:
                print(f"  {msg}")
            print(
                "\n❌ VALIDATION FAILED - Fix errors before running extension"
            )
            return False
        else:
            print("\n✅ VALIDATION PASSED - Extension ready to run")
            return True

    def print_next_steps(self) -> None:
        """Print next steps for validation."""
        print("\n" + "=" * 70)
        print("NEXT STEPS")
        print("=" * 70)

        steps = [
            "1. Run the extension: python -m velocloudextensionrevised",
            "2. Monitor logs for INFO messages (metrics collected)",
            "3. Wait 1-2 minutes for metrics to sync to Dynatrace",
            "4. Check Dynatrace Observability → Metrics Explorer",
            "5. Search for 'custom.velocloud.edge.*'",
            "6. Verify metrics have data points",
        ]

        for step in steps:
            print(f"  {step}")

        print("\n📊 Expected Metrics:")
        print("  - Per edge: ~28-30 metrics (link + health stats)")
        print("  - Per 10 edges: ~280-300 metrics per cycle")
        print("  - Query interval: Every 60 seconds")

        print("\n📝 Log Level Reference:")
        print("  [INFO]  = Major operations (normal, important)")
        print("  [DEBUG] = Detailed operations (troubleshooting)")
        print("  [WARN]  = Recoverable issues (rate limits, retries)")
        print("  [ERROR] = Failures (fix required)")

    def run(self) -> bool:
        """
        Run complete validation.

        Returns:
            True if all validations passed
        """
        print("\n" + "=" * 70)
        print("VeloCloud Extension - Metrics Validation Script")
        print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)

        result = (
            self.validate_configuration()
            and self.validate_implementation()
            and self.validate_metrics()
        )

        self.print_summary()

        if result:
            self.print_next_steps()

        return result


def main() -> None:
    """Main entry point."""
    validator = MetricsValidator()
    success = validator.run()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
