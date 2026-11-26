# Release Notes - Devin AI Extension

## Version 2.0.1 (October 10, 2026)

**Python 3.14 Migration & Enhancement Release**

---

## What Changed

### Primary Update
- **Python 3.14 Support**: Extension now exclusively targets Python 3.14 runtime
- **Previous Support Ended**: Python 3.10 no longer supported (EOL: 2026-10-01)

### Dependencies Updated
- `dt-extensions-sdk`: 1.10.2 (from 1.8.0+)
- `requests`: 2.34.2
- `urllib3`: 2.8.0
- `certifi`: 2026.7.22
- `charset_normalizer`: 3.5.2
- `idna`: 3.20

### Code Quality Improvements
- Fixed all linting issues (5 auto-fixable issues resolved)
- Reformatted code with ruff (3 files)
- Added proper trailing newlines to all files
- Organized imports per project standards

---

## Steps Taken to Upgrade to Python 3.14

### 1. Updated dt-extensions-sdk
```
Verified: 1.10.2 (supports Python 3.14)
```

### 2. Increased Extension Version
- Version bumped: 1.0.0 → 2.0.1
- File: `extension/extension.yaml`

### 3. Code Quality Validation
```bash
dt-sdk lint          # Found and listed 5 fixable issues
dt-sdk format        # Auto-formatted 3 files
dt-sdk lint          # Verified fixes applied
```

### 4. Built Extension with Python 3.14
```bash
dt-sdk build . --python-version 3.14 --target-directory ./dist
```

**Build stages executed:**
- Stage 1: Downloaded Python 3.14 dependencies to `extension/lib`
- Stage 2: Assembled extension into zip archive
- Stage 3: Signed package with developer certificate
- Stage 4: Cleaned up intermediate files

### 5. Extension Validation
- ✅ Module loads correctly with Python 3.14
- ✅ Fastcheck validation passes
- ✅ All 9 metrics properly defined
- ✅ Topology configuration validated
- ✅ Error handling verified

### 6. Generated Package
```
Output: dist/custom_devinai-2.0.1.zip (690 KB)
Status: Signed & Production Ready
```

---

## Version Details

| Attribute | Value |
|-----------|-------|
| Version | 2.0.1 |
| Python Runtime | 3.14 ✅ |
| Minimum Dynatrace | 1.333 |
| dt-extensions-sdk | 1.10.2+ |
| Package Size | 690 KB |
| Build Date | October 10, 2026 |

---

## Build Command Reference

**Standard Build (Python 3.14)**
```bash
dt-sdk build . --python-version 3.14 --target-directory ./dist
```

**Production Build (Cross-Platform)**
```bash
dt-sdk build . --python-version 3.14 --extra-platform manylinux2014_x86_64 --target-directory ./dist
```

---

## Deployment Steps

### For New Installations
1. Download `custom_devinai-2.0.1.zip`
2. Upload to Dynatrace Extensions app
3. Create monitoring configuration with Devin API endpoint
4. Provide Devin API credentials (URL & token)

### For Upgrading from v1.x
1. Download `custom_devinai-2.0.1.zip`
2. Upload to Dynatrace Extensions app
3. Update monitoring configuration to reference v2.0.1
4. Ensure ActiveGate runs Python 3.14 runtime
5. Restart the monitoring configuration

---

## Metrics Included (9 Total)

- `custom.devin.acu` - User ACU consumption
- `custom.devin.enterprise.acu` - Enterprise ACU by product
- `custom.devin.organization.acu` - Organization ACU by product
- `custom.devin.acu.total` - Total daily ACU
- `custom.devin.acu.count` - User product count
- `custom.devin.sessions` - Devin sessions
- `custom.devin.searches` - Devin searches
- `custom.devin.prs.created` - PRs created
- `custom.devin.prs.merged` - PRs merged

---

## Breaking Changes

- **Python 3.10 No Longer Supported** — ActiveGate/OneAgent must run Python 3.14
- Minimum `dt-extensions-sdk` requirement strictly enforced at 1.8.0+

---

## Technical Notes

- Collection interval: 15 minutes (optimized for slow Devin API responses)
- API timeout: 30 seconds with up to 3 retries
- Error mapping: Devin API failures mapped to Dynatrace StatusValue categories
- Configuration validation: Fastcheck validates all endpoints before activation

---

**Status**: ✅ Production Ready  
**Support**: Python 3.14 runtime on ActiveGate/OneAgent 1.333+
