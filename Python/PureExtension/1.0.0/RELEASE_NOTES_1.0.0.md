# Pure1 Volume Capacity Extension (`custom:custom-pure`) — Version 1.0.0

**Release date:** 2026-10-15
**Previous version:** 0.0.8

Written for: engineers and administrators who build, deploy, or operate this extension.

## Summary

Version 1.0.0 upgrades the extension's Python runtime from **3.10 to 3.14**. No functional, metric, topology, or configuration changes were made in this release.

## What changed

### Python runtime upgraded to 3.14

| Item | 0.0.8 | 1.0.0 |
|---|---|---|
| Python runtime minimum (`python.runtime.version.min`) | 3.10 | **3.14** |
| Bundled native wheels (`extension/lib`) | cp310 builds | **cp314 builds** (`cffi`, `charset_normalizer`, `cryptography` rebuilt for the target runtime) |

### Unchanged

- Metrics: `custompure1.volume.provisioned`, `custompure1.volume.physicalcapacity`, `custompure1.volume.used_percentage`
- Dimensions: `volume_id`, `volume_name`, `cluster_name`
- Topology entity type: `custom:pure1_volume`
- Activation schema (`activationSchema.json`) and configuration fields (App ID, Private Key PEM)
- Collection interval and processing logic

## Upgrade notes

- The extension now requires a Dynatrace ActiveGate with a **Python 3.14** runtime. Environments that only provide 3.10 will not run this version.
- Build as usual with `dt-sdk build .`. The output is `dist/custom_custom-pure-1.0.0.zip`.
- No changes are needed to existing monitoring configurations.
