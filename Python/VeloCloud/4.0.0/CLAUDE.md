# VeloCloud custom extension

## Project
- Source package: velocloudextensionrevised/ (api_client.py, __main__.py, validate_metrics.py)
- velocloudapi.py and tools/ are supporting scripts.
- build/lib/ is generated output from build.ps1. Never edit it; change the source and rebuild.
- logs/ holds simulator logs. Read them when debugging, don't edit them.

## Current task
- Fix the HTTP 429 "Too Many Requests" error from the VeloCloud API.
- api_client.py already has pacing (leaky bucket, 10 req/s), max 2 requests in flight, a 5 s 429 penalty, adaptive rate halving, 3 attempts with Retry-After, and a 10 min cooldown. Build on this; don't add a second retry layer.
- First confirm whether 429s still occur (check logs/ and tools/poll_check_report.json). Then tune the existing limits (MAX_REQUESTS_PER_SECOND, MAX_IN_FLIGHT, RATE_LIMIT_PENALTY) and report the change.
- Don't lower MAX_REQUESTS_PER_SECOND below what the VCO Operator confirms without asking me.
- Check that __main__.py calls is_blocked() before each cycle.
- Don't change API endpoints, payload shapes, or the metric output format unless I ask.

## Runbook
- Full context, findings, and the step-by-step fix: VELOCLOUD_429_RUNBOOK.md. Read it before changing rate settings or running live tests.

## Rules
- Make small, focused edits and explain each change before applying it.
- Don't run builds, installs, or anything that calls the live VeloCloud API without asking me first.
- Before claiming the 429 is fixed, run the relevant tool or test and report the actual output.
- Never print or log API tokens, passwords, or customer data.
- Ask before deleting files or changing setup.py / build.ps1.
