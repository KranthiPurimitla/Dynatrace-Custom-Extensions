# DevinAI ACU extension

Dynatrace Extension 2.0 (Python) that pulls ACU consumption, usage, and topology data from the Devin AI enterprise API and reports them into Dynatrace. It supports multiple Devin AI tenants/endpoints from a single monitoring configuration.

> **Current Version: 2.0.0** | **Python Runtime: 3.14** | **dt-extensions-sdk: 1.10.2**

## What it collects

Every collection cycle, the extension:

1. Reads the configured Devin endpoint(s).
2. Tests connectivity to the Devin API.
3. Retrieves the enterprise's Devin users.
4. Retrieves ACU consumption per user, per organization, and enterprise-wide for the current Devin billing day.
5. Retrieves enterprise usage metrics (sessions, searches, PRs).
6. Reports everything as Dynatrace metrics.

The billing day is calculated on a fixed Pacific Standard Time (UTC-8) boundary, matching how the Devin API itself buckets daily consumption — not a DST-aware Pacific timezone, which would misalign with the API's day buckets and silently return empty data.

## Metrics

| Metric | Description | Dimensions |
|---|---|---|
| `custom.devin.acu` | Daily ACU consumption per user and product | `user_id`, `user_name`, `product`, `org_id` (when available) |
| `custom.devin.enterprise.acu` | Daily ACU consumption per product, enterprise-wide | `product` |
| `custom.devin.organization.acu` | Daily ACU consumption per product, per organization | `org_id`, `product` |
| `custom.devin.acu.total` | Total daily ACU consumption across all products, enterprise-wide | — |
| `custom.devin.acu.count` | Number of distinct products a user consumed ACU on that day | `user_id`, `user_name`, `org_id` (when available) |
| `custom.devin.sessions` | Devin sessions | — |
| `custom.devin.searches` | Devin searches | — |
| `custom.devin.prs.created` | Pull requests created through Devin | — |
| `custom.devin.prs.merged` | Pull requests merged through Devin | — |

Zero/null ACU records are skipped rather than reported. Collection runs every 15 minutes — see [Collection interval](#collection-interval) below for why that's not the SDK's default 1-minute schedule.

## Entities and topology

The extension defines three generic entity types and extracts them from ingested metrics:

* **`devinai:user`** — one per Devin user (`user_id`), named from `user_name`, extracted from `custom.devin.acu`. Carries `org_id` and `product` as attributes.
* **`devinai:organization`** — one per Devin organization (`org_id`), extracted from either `custom.devin.acu` or `custom.devin.organization.acu`.
* **`devinai:product`** — one per Devin product (`devin`, `cascade`, `terminal`, `review`), extracted from any of `custom.devin.acu`, `custom.devin.enterprise.acu`, or `custom.devin.organization.acu` — any metric that carries a `product` dimension.

Relationships:

* `devinai:user` **PART_OF** `devinai:organization`
* `devinai:user` **CALLS** `devinai:product` — sourced from `custom.devin.acu`, since that metric line carries both `user_id` and `product` together.
* `devinai:organization` **CALLS** `devinai:product` — sourced from `custom.devin.organization.acu`, since that metric line carries both `org_id` and `product` together.

A relationship can only be extracted from a metric line that carries identifying dimensions for *both* sides at once — that's why each relationship is pinned to the specific metric that actually has both.

## Dashboards

Two dashboards ship with the extension:

* `extension/documents/devinai_acu_extension_overview.dashboard.json` — a gen3 (Document Dashboard) overview, declared under `documents.dashboards` in `extension.yaml`. Requires Dynatrace **1.309+**.
* `extension/dashboards/overview_dashboard.json` — a Dashboards Classic overview, declared under the top-level `dashboards` key.

These are two distinct Dynatrace dashboard formats with separate YAML wiring — a classic dashboard export can't be dropped into the `documents` folder and vice versa.

## Requirements

* **Dynatrace**: `minDynatraceVersion` **1.333** — the higher of the two floors this extension needs: 1.309 for gen3 document dashboards, and 1.333 for the OneAgent/ActiveGate Python 3.14 runtime.
* **Python Runtime**: **3.14** ✅ (this extension requires Python 3.14 on ActiveGate/OneAgent). Python 3.10 reached end-of-life on 2026-10-01 and is no longer supported.
* **dt-extensions-sdk**: **1.10.2+** (currently using 1.10.2 for building with Python 3.14 support)
* **Build Platform**: Windows, macOS, or Linux with Python 3.14 installed

## Configuration

Each monitoring configuration takes one or more Devin AI endpoints (`activationSchema.json`), each with:

* **Devin API URL** — e.g. `https://api.devin.ai/v3`
* **Devin API token** — stored as a Dynatrace secret, never logged or written to disk

Endpoints are processed independently; a failure on one endpoint doesn't stop the others from being collected.

## Collection interval

The `dynatrace_extension` SDK auto-schedules a special `query()` method every 60 seconds. Devin's consumption APIs are too slow for that (collecting ACU for hundreds of users regularly takes 90+ seconds), so this extension leaves `query()` as a no-op and instead schedules its own collection method (`_collect_devin_metrics`) on a 15-minute interval via `self.schedule(...)` in `initialize()`.

## Error handling

API failures are mapped to Dynatrace `StatusValue` categories so failures show up meaningfully in fastcheck and monitoring status, instead of as raw exceptions:

| Devin API response | Status |
|---|---|
| 401 | `AUTHENTICATION_ERROR` |
| 403 | `AUTHENTICATION_ERROR` |
| 404 | `INVALID_ARGS_ERROR` |
| 429 | `WARNING` (collection continues) |
| 5xx | `GENERIC_ERROR` |
| No response / connection failure | `DEVICE_CONNECTION_ERROR` |

Per-user and per-organization ACU collection failures are logged and skipped so one bad user/org doesn't abort the whole run. HTTP requests use a 30s timeout with up to 3 retries.

`fastcheck()` validates configuration and tests connectivity against every configured endpoint before the extension is allowed to activate.

## Building and signing

### Build Requirements

The ActiveGate runs on Linux (manylinux2014_x86_64) with Python 3.14, but development normally happens on Windows/macOS. The `dt-sdk build` command fetches wheels for "the current platform" only, unless `--extra-platform` is specified. Cross-platform wheels (e.g., `charset_normalizer` from `requests`) must be explicitly downloaded or they'll fail to install on the ActiveGate.

### Build Command (Python 3.14)

```bash
dt-sdk build . --python-version 3.14 --target-directory ./dist
```

**What this does:**
- **Stage 1**: Downloads and builds Python 3.14 dependencies into `extension/lib`
- **Stage 2**: Assembles the extension into a zip file (`extension.zip`)
- **Stage 3**: Cryptographically signs the package with your developer certificate
- **Stage 4**: Cleans up intermediate files

**Output**: `dist/custom_devinai-2.0.0.zip` — this is the file you upload to Dynatrace

### Build Command with Cross-Platform Support (optional)

For production builds that must run on Linux ActiveGates from a Windows dev machine:

```bash
dt-sdk build . --python-version 3.14 --extra-platform manylinux2014_x86_64 --target-directory ./dist
```

### Release Workflow

1. **Update code** as needed in `devinai/` folder
2. **Increment version** in `extension/extension.yaml`
3. **Run linting and format** (optional but recommended):
   ```bash
   dt-sdk lint
   dt-sdk format
   ```
4. **Build the extension**:
   ```bash
   dt-sdk build . --python-version 3.14 --target-directory ./dist
   ```
5. **Upload** the resulting `dist/custom_devinai-<version>.zip` to Dynatrace Extensions app
6. **Update** monitoring configuration to use the new extension version
7. **Restart** the extension in Dynatrace

## Running

For local testing/development:

```bash
dt-sdk run --activation-config activation.json
```

This runs the extension locally using the credentials in `activation.json` (and secrets in `secrets.json`).

## Developing

### Setup

```bash
# Clone repository
git clone <repo>
cd devinai-extension

# Install in development mode
pip install -e .

# Verify Python 3.14 is available
python --version
```

### Development Workflow

1. Make code changes in `devinai/` folder
2. Increment version in `extension/extension.yaml`
3. Test locally: `dt-sdk run --activation-config activation.json`
4. Validate code: `dt-sdk lint` and `dt-sdk format`
5. Build: `dt-sdk build . --python-version 3.14 --target-directory ./dist`

## Structure

### devinai folder

Python source for the extension:

* `__main__.py` — the `ExtensionImpl` class: scheduling, per-endpoint orchestration, metric reporting, error-to-status mapping, `fastcheck()`.
* `devin_api.py` — `DevinAPIClient`, the Devin REST API wrapper (users, per-user/org/enterprise consumption, usage metrics, billing-day calculation).

### extension folder

The YAML and activation definitions for the Extensions 2.0 framework:

* `extension.yaml` — metrics, topology, dashboards, Python runtime requirements.
* `activationSchema.json` — the monitoring configuration UI schema (Devin endpoint URL + token).
* `dashboards/` — Dashboards Classic export.
* `documents/` — gen3 Document Dashboard export.

### setup.py

Python package metadata: dependencies, `python_requires`, `dt-extensions-sdk` version.

### activation.json

Used during simulation only (`dt-sdk run`); contains the activation definition for the extension.

### secrets.json

Used during simulation only; supplies the Devin API URL and token referenced by `activation.json`. Not part of the built extension package.

## Changelog

### Version 2.0.0 (October 10, 2026)

**Major Update: Python 3.14 Support & Dependencies Upgrade**

#### Changes Made

* ✅ **Python 3.14 Runtime**: Extension now exclusively targets Python 3.14 (no longer supports 3.10)
* ✅ **dt-extensions-sdk Updated**: Upgraded from 1.8.0+ to 1.10.2 (latest stable)
* ✅ **Dependencies Updated**:
  - `requests`: 2.34.2
  - `urllib3`: 2.8.0
  - `certifi`: 2026.7.22
  - `charset_normalizer`: 3.5.2
  - `idna`: 3.20

* ✅ **Code Quality Improvements**:
  - Fixed all linting issues (ruff check)
  - Reformatted code with ruff format (3 files)
  - Added trailing newlines to all Python files
  - Organized imports per project standards

* ✅ **Extension Validation**:
  - Module loads correctly with Python 3.14
  - Fastcheck validation passes
  - All 9 metrics properly defined
  - Topology configuration validated
  - Error handling comprehensive

* ✅ **Build & Deployment**:
  - Extension signed and ready for production
  - Cross-platform wheel support verified
  - No runtime errors in test environments

#### Build Command Used

```bash
dt-sdk build . --python-version 3.14 --target-directory ./dist
```

#### Breaking Changes

* **Python 3.10 no longer supported** — ActiveGate/OneAgent must run Python 3.14
* Minimum `dt-extensions-sdk` now **1.8.0** (previously 1.8.0+, now strictly enforced)

#### Migration from v1.x

1. Download `custom_devinai-2.0.0.zip`
2. Upload to Dynatrace Extensions app
3. Update monitoring configuration to use v2.0.0
4. Ensure ActiveGate is running Python 3.14 runtime
5. Restart the extension

No API changes — monitoring configurations remain compatible, only extension version reference changes.

### Version 1.1.1 (October 10, 2026)

* Minor version bump with Python 3.14 initial support
* Build validation with dt-extensions-sdk 1.10.2

### Version 1.0.0 (Initial Release)

* Support for Python 3.10+
* 9 custom metrics for Devin ACU consumption tracking
* Enterprise, user-level, and organization-level analytics
* Topology modeling (users, organizations, products)
* Error handling with Dynatrace StatusValue mapping
