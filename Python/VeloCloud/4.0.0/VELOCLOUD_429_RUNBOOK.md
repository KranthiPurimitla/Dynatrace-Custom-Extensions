# VeloCloud 429 "Too Many Requests" runbook

Last updated: 2026-10-19. Written for the VeloCloud Orchestrator (VCO) admin and for maintainers of this extension.

## 1. The challenge

The VeloCloud custom extension (`velocloudextensionrevised`) runs once a minute. Each cycle polls every edge in the enterprise for link stats and health stats. The VCO answers many of these calls with HTTP 429 "Too Many Requests". When a call fails after all retries, that edge's metrics are missing for that minute.

Symptoms seen in the logs:
- `Rate limited (429). Waiting 5.0s (attempt N/3, pace now X req/s)`
- `Rate limited (429) collecting link stats for edge …` (retries exhausted, data lost)
- `Query skipped: API rate limit cooldown active` (after the 10-minute cooldown started)
- The first call of a cycle sometimes gets a 429 right away, before any other call.

Scale: 158 edges. Two calls per edge (link stats and health stats) means about 316 calls per cycle when all edges are polled.

## 2. What we found

### 2.1 Extension code (before our changes)
- `async_query` in `__main__.py` planned every cycle from the pace at the start of the cycle. At 0.10 s per call it planned all 158 edges. The first 429s then dropped the pace to 1 req/s in seconds, but the plan was not resized.
- The 45 s query budget then cut calls off, and the 55 s timeout fired most cycles.
- A single edge's failure triggered the 10-minute cooldown, which stopped polling for every edge.
- Per-edge 429 failures did not count toward the cycle's error status, so the log could say "successful" while data was missing.

### 2.2 Changes made to the extension
In `velocloudextensionrevised\api_client.py`:
- `BLOCK_AFTER_REFUSALS = 10`: the cooldown (`BLOCK_COOLDOWN = 600` s) starts only after 10 refused requests in a row with no success between them. One refused edge no longer blocks everyone.
- `refused_in_a_row` counter, reset on any success.
- Call budget (approved 2026-10-19): `MAX_REQUESTS_PER_SECOND = 0.5`, `MIN_REQUESTS_PER_SECOND = 0.25`, `MAX_IN_FLIGHT = 1`. Set per run from `rate_budget_per_minute` (default 30) by `set_ceiling`. Health stats run every `health_every_n_cycles` cycles (default 5). Edges are marked polled only after a successful fetch, and failed or budget-skipped edges go first next cycle.
- Schedule (2026-10-19): the SDK fixes the default `query()` at one minute, and it marks any callback that runs longer than its interval as an error. The collection cycle is therefore scheduled from `initialize()` every 5 minutes (`collect_cycle`). `query()` is a no-op. The cycle budget is 240 s and its timeout 270 s, both under the 300 s interval. The SDK reported the first cycle at 240.2 s with no timeouts or exceptions.
- Shared quota: the local test run (20:40 to 20:46) overlapped with the tenant extension, which had been re-enabled. Both used the same credentials, so that run's 429 counts are not a clean test of the limit. Do not run local tests while the tenant extension is enabled.
- Observed: at the 0.25 req/s floor, many calls still got 429 with a 5 s penalty. Possible causes: the tenant overlap above, or a per-user limit lower than 0.25 req/s. Test again with the tenant extension disabled.
- `activation.json` is set to `"enabled": false`. The tenant extension configuration is also disabled. Neither is re-enabled until the validation below passes.
- The shared 429 pause now honors `Retry-After` (`pause(max(RATE_LIMIT_PENALTY, retry_after))`), so every caller waits as long as the retrying call.
- `RequestPacer` starts at the floor (`MIN_REQUESTS_PER_SECOND`) instead of the 10 req/s ceiling, so each run no longer starts with a burst. It ramps up by 2% per success.
- The first 429 per run logs its headers and the first 500 characters of the body (`First 429 from … Headers: … Body: …`). Use it to see the limit, the window, and whether VCO or a proxy sent the 429.

In `velocloudextensionrevised\__main__.py`:
- `_select_edges` sizes each cycle from the slowest allowed pace (`pacer.min_rate`, 1 req/s). At the floor this is about 22 edges per cycle instead of 158.
- Edges whose calls fail are recorded in `_failed_edges` and polled first in the next cycle, so no edge's data is dropped for a full rotation.
- Calls skipped because the query budget ran out are counted and logged once per cycle (`N API call(s) left for the next cycle: query budget used up`).
- Per-edge 429 and other errors now count toward `_cycle_errors`, so the cycle reports `Query finished with N error(s)`.

All edits compile (`python -m py_compile`). The extension has been run for one and two cycles against the live VCO; the cooldown no longer blocks, and the cycle reports its errors.

### 2.3 Rate-limit measurements (poll-check tool, live host)
| Test | Result |
|---|---|
| 1 req/s, 1 in flight | 11 calls succeeded, then 429 at about 10 s. Looks like a burst of about 10 calls. |
| 0.5 req/s, 1 in flight | First call (edge list) got 429, on two attempts a few minutes apart. The account was still blocked from the 1 req/s test. |
| New API token, one cycle | First call still got 429. The new token did not clear the block. |

### 2.4 Other client found
- A `dt-sdk run` process (PID 17636 at the time) started at 19:34 on 2026-10-19. It launched a Python wrapper (29296) that ran `velocloudextensionrevised` (6232). It used the **old** token and kept polling after the token change.
- It was stopped on 2026-10-19 around 19:49, after the VS Code restart. Confirm nothing named `python` or `dt-sdk` is still running.
- The tenant extension configuration was also disabled.

### 2.5 VeloCloud API Fair Usage Policy (Arista, updated 2025-09-29)
- Universal limit, measured over a 5-second window, checked per user first, then per organization:
  - Enterprise user: 20 calls per 5 s (4 req/s)
  - Enterprise organization: 50 calls per 5 s (10 req/s), shared by all users in the organization
  - Operator user: 100 per 5 s
- Dynamic or contextual throttling may also apply during high system load or large exports. The operator can impose it on a specific enterprise, and it is lifted when system health recovers.
- Repeated or excessive violations can lead to temporary API suspension.
- Implication: our 0.5 req/s ceiling (2.5 calls per 5 s) is well under the user limit. The 429s we see are therefore not explained by the universal per-user limit. Likely causes: the organization limit shared with other users or tools, dynamic throttling, or the tenant overlap in section 2.6.
- Our retries multiply load during a 429 (3 attempts per call, 5 s penalty). Under a sustained throttle, that can count as excessive use. Keep retries bounded.

### 2.6 Working theory
The block is most likely at the enterprise or source-IP level, or it's a lingering penalty from the earlier tests. A new token doesn't change either. We have not yet confirmed the actual limit from VCO.

## 3. The solution (step by step)

### Step 1: Get the real limit from VCO (admin)
You need four values:
1. Sustained requests per second allowed (the leaky-bucket rate)
2. Maximum concurrent requests
3. Penalty or lockout time after a 429
4. Whether the limit applies per API token, per user, or per source IP (or per enterprise)

Where to find them:
- The VMware SD-WAN Orchestrator API guide, "Rate Limits" section, for your VCO version (Broadcom TechDocs).
- The VCO API token or web services settings in the Operator console.
- If those don't show the values, open a Broadcom support case for your VCO instance and ask for the four values above.

### Step 2: Confirm no other client is using the credentials
- No `dt-sdk`, `velocloudextensionrevised`, or poll-check process running locally:
  ```
  Get-Process python,dt-sdk -ErrorAction SilentlyContinue
  ```
- The tenant extension configuration stays disabled until Step 5.
- Check other tools or integrations that use the same API token or the same outbound IP.

### Step 3: Wait out the block
Wait 20 to 30 minutes after the last API call from any client. Do not run any test during this time.

### Step 4: One sequential test call
From `c:\Users\280380\velorevise`:
```
python tools\independent_poll_check.py --rate 0.2 --max-in-flight 1 --cycles 1 --out <scratchpad>\rate_test.json
```
- It stops at the first 429.
- If the first call succeeds and the remaining count drops with each call, the limit is per token or per client. Continue to Step 5.
- If the first call still returns 429, the block is at enterprise or IP level. Stop and go to Step 1 with support.

### Step 5: Set the rates from the VCO numbers
Edit `velocloudextensionrevised\api_client.py`:
- `MAX_REQUESTS_PER_SECOND`: the sustained limit from VCO (keep at or below it).
- `MIN_REQUESTS_PER_SECOND`: the floor. Keep at or below the sustained limit. Lower it only if the VCO limit is lower than 1 req/s.
- `MAX_IN_FLIGHT`: the concurrency limit from VCO.
- `RATE_LIMIT_PENALTY`: the penalty time from VCO (default 5 s).

Project rule: do not lower `MAX_REQUESTS_PER_SECOND` below what the VCO Operator confirms without asking first.

Check the first-run pacer: each new run starts at `MAX_REQUESTS_PER_SECOND`. If the VCO limit is a burst limit (about 10 calls), start the pacer at the floor instead. This is an open item; see Section 5.

### Step 6: Run one cycle with the extension and verify
Re-enable the tenant extension only after this step passes.
```
python -m velocloudextensionrevised --activationconfig activation.json --secrets secrets.json
```
Run it for about 90 seconds (one cycle). Check the log for:
- `Polling N of 158 edges` with N matching the new rate.
- No `TIMEOUT` line.
- No `Query skipped: API rate limit cooldown active`.
- Few or no `Rate limited (429)` lines. The first 429 in the run shows the headers and body.
- `Query completed successfully` or `Query finished with N error(s)` (the count must match the 429 lines).

### Step 7: Re-enable and monitor
- Re-enable the tenant extension configuration.
- Watch one hour of runs. Check the number of 429s per cycle and the number of errors in the status line.

## 4. Commands reference

| Purpose | Command (run from `c:\Users\280380\velorevise`) |
|---|---|
| Compile check | `python -m py_compile velocloudextensionrevised\__main__.py velocloudextensionrevised\api_client.py` |
| One cycle of the extension (about 90 s) | `timeout 90 python -m velocloudextensionrevised --activationconfig activation.json --secrets secrets.json > <log>` |
| Sequential rate test | `python tools\independent_poll_check.py --rate 0.2 --max-in-flight 1 --cycles 1 --out <report.json>` |
| Find running clients | `Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='dt-sdk.exe'" \| Select ProcessId, CommandLine` |
| Stop a client | `Stop-Process -Id <pid> -Force` (stop the whole tree: dt-sdk, its python, and the extension) |

Use the scratchpad folder for report and log files, not the project folder.

## 5. Open items

- **Real VCO limit:** not yet confirmed. Steps 1 to 4 decide this.
- **Burst at start:** fixed. The pacer now starts at the floor on every run. Confirm this in the next run's log: the first `Polling` line should show a pace near 1.00 s per call.
- **Penalty style:** the VCO guide describes a constant penalty (about 5 s). The code halves the rate after each 429 and uses a constant wait. Exponential backoff was not added.
- **Header logs:** check the first 429 entry in the next run's log for the actual limit headers (X-RateLimit-*, Retry-After). Record them here.
- **Two-client check:** if 429s continue with nothing else running, check the VCO audit log for client IDs or source IPs making calls with these credentials.

## 6. Rules for maintainers of this project

Key points:
- Build on the existing pacing and retry code. Do not add a second retry layer.
- Do not lower `MAX_REQUESTS_PER_SECOND` below the VCO Operator's confirmed value without asking.
- Do not make live VCO calls without asking, except the approved test in Step 4 or a run the user has asked for.
- Never print or log API tokens, passwords, or customer data. Do not open `secrets.json` contents in output.
- Report test results with the actual output.
