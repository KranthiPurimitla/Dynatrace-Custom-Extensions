# Pure1 Volume Capacity Extension (custom_pure)

A Dynatrace Extension 2.0 (Python datasource) that connects to the [Pure Storage Pure1 REST API](https://blog.purestorage.com/purely-technical/pure1-rest-api-part-2/), pulls per-volume capacity data, and reports it into Dynatrace as metrics. Those metrics are then used to calculate volume utilization and alert before a volume runs out of space.

---

## 1. What this extension does

Every **5 minutes**, for each configured Pure1 endpoint, the extension:

1. Signs a JWT with your Pure1 private key and exchanges it for a Pure1 OAuth2 access token.
2. Retrieves **every volume** from Pure1 (paginated automatically, regardless of fleet size).
3. Retrieves each volume's **physical (used) capacity** — a 30-day average — in batches, with automatic retry on transient network/API failures.
4. Calculates each volume's **used percentage** from the two values above.
5. Reports three metrics per volume into Dynatrace.

### Metrics ingested

| Metric key | What it represents | Unit |
|---|---|---|
| `custompure1.volume.provisioned` | Total capacity **provisioned/allocated** to the volume | Byte |
| `custompure1.volume.physicalcapacity` | Capacity **actually used** by the volume (30-day average) | Byte |
| `custompure1.volume.used_percentage` | `physicalcapacity / provisioned * 100`, calculated in Python from the two metrics above. Skipped for a volume if provisioned capacity is `0` or either value is missing | Percent |

### Dimensions carried on every metric

| Dimension | Meaning |
|---|---|
| `volume_id` | Pure1's unique volume identifier |
| `volume_name` | Human-readable volume name |
| `cluster_name` | The Pure Storage array/cluster the volume belongs to |

### Entity created

Each volume is also modeled as a Dynatrace entity of type **`custom:pure1_volume`** (displayed as "Pure1 Volume"), built automatically from the `volume_name` and `cluster_name` dimensions on the metrics above. This gives every volume its own entity page in Dynatrace, with all three metrics attached, instead of just raw, disconnected timeseries data.

---

## 2. How to configure it

### Step 1 — Get Pure1 API credentials

In Pure1, go to **Administration → API Registration** and register an application. This gives you:
- An **App ID**, e.g. `pure1:apikey:XXXXXXXXXXXXXXXX`
- A matching **RSA key pair** — Pure1 keeps the public key, you keep the private key

### Step 2 — Build and upload the extension

```
dt-sdk build .
```

This packages everything (including all Python dependencies as offline wheels) and signs the result to `dist/custom_custom-pure-<version>.zip`. Upload that file to your Dynatrace environment (Settings → Extensions, or via Hub upload).

### Step 3 — Create a monitoring configuration

In Dynatrace, create a new monitoring configuration for this extension and add an endpoint with two fields:

| Field | What to enter |
|---|---|
| **App ID** | The Pure1 App ID from Step 1, e.g. `pure1:apikey:XXXXXXXXXXXXXXXX` |
| **Private Key (PEM)** | Your RSA private key, **with newlines escaped as `\n`** |

> **Why escape the newlines?** The Private Key field is a single-line input in the Dynatrace UI — it can't hold a real multi-line PEM block. Convert the key to one line by replacing every line break with the two characters `\n`, for example:
> ```
> -----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq...\n-----END PRIVATE KEY-----\n
> ```
> The extension automatically converts these back into real newlines before using the key — you don't need to do anything else.

### Step 4 — Enable the configuration

Save and make sure the configuration is **enabled** and assigned to an ActiveGate that has outbound HTTPS access to `api.pure1.purestorage.com`. Within a few minutes you should see volumes appearing in **Data Explorer** under the two metric keys above.

---

## 3. What we do with these metrics in Dynatrace: Volume Utilization

Raw capacity numbers aren't very actionable on their own — what matters is **how full each volume is**. The extension now calculates this itself and reports it as `custompure1.volume.used_percentage`, so it's available directly in Data Explorer without needing a DQL query. The DQL approach below still works identically (e.g. as a cross-check, or for alert configurations built before this metric existed).

### Metrics used

| Metric | Description |
|---|---|
| `custompure1.volume.physicalcapacity` | Actual storage consumed (used space) |
| `custompure1.volume.provisioned` | Total capacity allocated (available space) |

### The DQL logic (legacy/optional — same result as `custompure1.volume.used_percentage`)

**Step 1 — Retrieve both metrics, per volume**

```dql
used = avg(custompure1.volume.physicalcapacity)
provisioned = avg(custompure1.volume.provisioned)
```

The query averages both metrics over the selected timeframe, grouped by:
- Cluster Name
- Pure1 Volume entity
- Volume ID
- Volume Name

Grouping by these four fields ensures the calculation runs **independently for every volume** — no cross-volume mixing.

**Step 2 — Take the latest value**

```dql
used_capacity = arrayLast(used)
provisioned_capacity = arrayLast(provisioned)
```

Since each metric comes back as a timeseries array, `arrayLast()` pulls out the most recent data point in the window.

| Metric | Example value |
|---|---|
| Physical Capacity | 9 TB |
| Provisioned Capacity | 10 TB |

**Step 3 — Calculate utilization percentage**

```dql
used_percentage = (used_capacity / provisioned_capacity) * 100
```

$$
\text{Volume Utilization (\%)} = \frac{\text{Physical Capacity}}{\text{Provisioned Capacity}} \times 100
$$

Example: `(9 / 10) × 100 = 90%`. The result is rounded to two decimal places for display.

### Alert threshold

A Dynatrace metric event is configured against the calculated utilization percentage:

> **Warning condition:** Volume Utilization > 90%

| Volume | Used % | Alert? |
|---|---|---|
| Volume-A | 72% | No |
| Volume-B | 89% | No |
| Volume-C | 91% | **Yes** |
| Volume-D | 97% | **Yes** |

### Why this alert matters

When a volume crosses 90% utilization, Dynatrace raises a problem so storage administrators have time to:

- Expand the volume's capacity
- Reclaim unused storage
- Investigate unexpected growth
- Avoid application outages caused by running out of space

### Summary formula

$$
\text{Volume Utilization (\%)} = \frac{\texttt{custompure1.volume.physicalcapacity}}{\texttt{custompure1.volume.provisioned}} \times 100
$$

**Alert condition:** `Volume Utilization > 90%`, evaluated independently for every volume using the latest values reported by this extension.

---

## 4. Building, running, and developing

### Build and sign

```
dt-sdk build .
```

Installs dependencies into `extension/lib` as offline wheels (required — the ActiveGate installs with `pip install --no-index`), assembles `extension/`, and signs the result to `dist/custom_custom-pure-<version>.zip`.

Always bump `version` in `extension/extension.yaml` before rebuilding.

### Run locally (simulation)

```
dt-sdk run
```

or directly:

```
python custom_pure/__main__.py
```

Local runs use `secrets.json` (via `{{placeholders}}` in `activation.json`) instead of a real Dynatrace monitoring configuration — handy for testing changes before deploying.

### Development workflow

1. Clone this repository
2. Install dependencies with `pip install .`
3. Make your changes, increase the version in `extension/extension.yaml`
4. Run `dt-sdk build .` and re-upload

---

## 5. Project structure

| Path | Purpose |
|---|---|
| `custom_pure/__main__.py` | Extension logic: JWT generation, Pure1 API calls, metric reporting |
| `extension/extension.yaml` | Metric and topology definitions, Python runtime requirements |
| `extension/activationSchema.json` | Configuration UI schema (App ID / Private Key fields) |
| `setup.py` | Python package metadata and dependencies |
| `activation.json` | Local simulation activation config (uses `secrets.json` placeholders) |
| `secrets.json` | Local-only credentials for `dt-sdk run`. **Never commit real credentials here.** |

---

## 6. Troubleshooting

- **`pip install` on the ActiveGate fails with a missing `typing-extensions` error** — happens if the extension is built on a machine running Python ≥3.11: `cryptography`'s conditional dependency on `typing-extensions` gets resolved against the *build* interpreter rather than the extension's target runtime (3.10), and silently dropped. `setup.py` pins `typing-extensions` unconditionally so this can't happen regardless of build machine.
- **`Could not parse the provided public key` when generating the JWT** — the private key's escaped newlines didn't survive intact. `generate_jwt` normalizes any number of backslashes before `n` into a real newline, so this should self-correct regardless of how the key was pasted.
- **Callback running longer than its schedule interval** — a full poll across a large fleet (pagination + batched capacity lookups) can take well over a minute. Polling runs on its own 5-minute schedule rather than the SDK's default 1-minute interval specifically to avoid this.
- **No data in Dynatrace after uploading a build** — confirm the metric keys in `extension.yaml`'s `metrics:` section, the topology `condition:` rules, and the `report_metric(...)` calls in `__main__.py` all match exactly; Dynatrace silently drops metrics that aren't declared. Also check the `extension.status` system events for the monitoring configuration to confirm the continuous process actually started (this is separate from `fastcheck`, which only verifies the extension loads).
