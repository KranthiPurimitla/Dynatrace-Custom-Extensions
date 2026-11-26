"""
Independent poll check for the VeloCloud API.

Sends the same requests the extension's api_client.py sends:
  1. POST /portal/rest/enterprise/getEnterpriseConfigurations
  2. GET  /api/sdwan/v2/enterprises/{id}/edges
  3. GET  /api/sdwan/v2/enterprises/{id}/edges/{edge}/linkStats?include=link.*
  4. GET  /api/sdwan/v2/enterprises/{id}/edges/{edge}/healthstats

It does NOT import the extension. It answers one question: can every edge's
link and health data be fetched in one cycle, at a strict request rate, without
a 429?

Behaviour:
  * Requests are spaced so that no more than --rate are sent in any second.
  * Concurrency is capped with --max-in-flight.
  * On the first 429 it stops sending and reports. It does not retry, so it
    never extends a block.
  * The token is read from secrets.json and is never printed or written out.

Usage:
  python independent_poll_check.py                 # one cycle at 100 req/s
  python independent_poll_check.py --rate 10       # slower test
  python independent_poll_check.py --cycles 3      # three cycles, one per minute

Output: a summary on the console and a JSON report next to this script.
"""

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

DEFAULT_BASE_URL = "https://vco61-usvi1.velocloud.net"
DEFAULT_SECRETS = Path(r"C:\Users\280380\velorevise\secrets.json")
TOKEN_KEY = "veloCloudToken"

# Fields the extension reads (see __main__.py collect_*_metrics)
LINK_FIELDS = [
    "bestJitterMsRx", "bestJitterMsTx", "bestLatencyMsRx",
    "bestLatencyMsTx", "bestLossPctRx", "bestLossPctTx",
]
HEALTH_FIELDS = [
    "cpuPct", "cpuCoreTemp", "memoryPct", "flowCount",
    "handoffQueueDrops", "tunnelCount", "tunnelCountV6",
]


class StrictPacer:
    """Spaces sends so that at most `rate` requests start per second."""

    def __init__(self, rate: float) -> None:
        self.interval = 1.0 / rate
        self._next = 0.0
        self._last_send = float("-inf")

    async def wait(self) -> None:
        slot = max(self._next, time.monotonic())
        self._next = slot + self.interval
        while True:
            due = max(slot, self._last_send + self.interval)
            remaining = due - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)
        self._last_send = time.monotonic()


class Poller:
    def __init__(self, session: aiohttp.ClientSession, headers: Dict[str, str],
                 rate: float, max_in_flight: int) -> None:
        self.session = session
        self.headers = headers
        self.pacer = StrictPacer(rate)
        self.gate = asyncio.Semaphore(max_in_flight)
        self.stop = False
        self.sent_at: List[float] = []
        self.status_counts: Counter = Counter()

    async def call(self, method: str, url: str) -> Tuple[Optional[int], Any]:
        """One request. Returns (status, json_body). Returns (None, None) if skipped."""
        async with self.gate:
            if self.stop:
                return None, None
            await self.pacer.wait()
            if self.stop:
                return None, None
            self.sent_at.append(time.monotonic())
            try:
                async with self.session.request(
                    method, url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    self.status_counts[resp.status] += 1
                    if resp.status == 429:
                        self.stop = True  # stop everything; do not retry
                        return 429, None
                    if resp.status != 200:
                        return resp.status, None
                    return 200, await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.status_counts["error"] += 1
                return "error", str(type(exc).__name__)


def max_per_second(times: List[float]) -> int:
    """Largest number of requests inside any 1-second window."""
    if not times:
        return 0
    times = sorted(times)
    best, lo = 0, 0
    for hi in range(len(times)):
        while times[hi] - times[lo] >= 1.0:
            lo += 1
        best = max(best, hi - lo + 1)
    return best


def check_fields(sample: Any, required: List[str], label: str) -> List[str]:
    if not sample:
        return [f"{label}: no data returned"]
    item = sample[0] if isinstance(sample, list) else sample
    if isinstance(item, dict) and "link" in item and label == "linkStats":
        item = {**item, **item.get("link", {})}
    missing = [f for f in required if f not in item]
    return [f"{label}: missing {', '.join(missing)}"] if missing else []


async def run_cycle(poller: Poller, base: str, ent: str, edges: List[Dict[str, Any]]) -> Dict[str, Any]:
    started = time.monotonic()
    results: Dict[str, Tuple[Any, Any]] = {}

    async def one_edge(edge: Dict[str, Any]) -> None:
        eid = edge.get("logicalId")
        link_url = f"{base}/api/sdwan/v2/enterprises/{ent}/edges/{eid}/linkStats?include=link.*"
        health_url = f"{base}/api/sdwan/v2/enterprises/{ent}/edges/{eid}/healthstats"
        link = await poller.call("GET", link_url)
        health = await poller.call("GET", health_url)
        results[eid] = (link, health)

    await asyncio.gather(*(one_edge(e) for e in edges))

    complete = 0
    partial = 0
    field_problems: List[str] = []
    for eid, ((ls, lb), (hs, hb)) in results.items():
        if ls == 200 and hs == 200:
            complete += 1
        else:
            partial += 1
        if ls == 200 and lb is not None and not field_problems:
            field_problems += check_fields(lb, LINK_FIELDS, "linkStats")
        if hs == 200 and hb is not None and not field_problems:
            field_problems += check_fields([hb], HEALTH_FIELDS, "healthstats")

    return {
        "edges_requested": len(edges),
        "edges_complete": complete,
        "edges_incomplete": partial,
        "seconds": round(time.monotonic() - started, 1),
        "field_problems": field_problems,
    }


async def main_async(args: argparse.Namespace) -> Dict[str, Any]:
    token = json.loads(args.secrets.read_text(encoding="utf-8"))[TOKEN_KEY]
    headers = {"Authorization": f"Token {token}"}
    report: Dict[str, Any] = {"rate_limit_per_second": args.rate, "cycles": []}
    base = args.base_url.rstrip("/")

    async with aiohttp.ClientSession() as session:
        poller = Poller(session, headers, args.rate, args.max_in_flight)

        # Same two calls the extension makes first
        async with session.post(f"{base}/portal/rest/enterprise/getEnterpriseConfigurations",
                                headers=headers, json={}) as r:
            report["enterprise_lookup_status"] = r.status
            if r.status != 200:
                report["stopped"] = f"enterprise lookup returned {r.status}"
                return report
            ent = (await r.json())[0]["enterpriseLogicalId"]

        status, body = await poller.call("GET", f"{base}/api/sdwan/v2/enterprises/{ent}/edges")
        report["edges_list_status"] = status
        if status != 200:
            report["stopped"] = f"edge list returned {status}"
            return report
        edges = [e for e in body.get("data", []) if e.get("logicalId") and e.get("name")]
        report["edges_total"] = len(edges)
        report["edges_per_minute_possible_at_rate"] = int(args.rate * 60 / 2)

        for cycle in range(1, args.cycles + 1):
            poller.stop = False
            start_idx = len(poller.sent_at)
            cyc = await run_cycle(poller, base, ent, edges)
            cyc["cycle"] = cycle
            cyc["requests_sent"] = len(poller.sent_at) - start_idx
            cyc["max_requests_in_any_1s"] = max_per_second(poller.sent_at[start_idx:])
            cyc["stopped_on_429"] = poller.stop
            report["cycles"].append(cyc)
            if poller.stop:
                break
            if cycle < args.cycles:
                await asyncio.sleep(max(0.0, 60.0 - cyc["seconds"]))

        report["status_counts"] = {str(k): v for k, v in poller.status_counts.items()}
        report["max_requests_in_any_1s_overall"] = max_per_second(poller.sent_at)
    return report


def print_summary(report: Dict[str, Any]) -> None:
    print("\n=== Independent poll check ===")
    print(f"rate ceiling: {report['rate_limit_per_second']} req/s")
    if "stopped" in report:
        print(f"STOPPED: {report['stopped']}")
    for c in report.get("cycles", []):
        print(f"cycle {c['cycle']}: edges {c['edges_complete']}/{c['edges_requested']} complete, "
              f"{c['requests_sent']} requests in {c['seconds']} s, "
              f"peak {c['max_requests_in_any_1s']} req in any 1 s, "
              f"429 stop: {c['stopped_on_429']}")
        for p in c["field_problems"]:
            print(f"  field check: {p}")
    print("status counts:", report.get("status_counts"))
    print("peak requests in any 1 s (overall):", report.get("max_requests_in_any_1s_overall"))
    cycles = report.get("cycles", [])
    if cycles and all(c["edges_complete"] == c["edges_requested"] and not c["stopped_on_429"] for c in cycles):
        print("RESULT: every edge fetched within the rate limit, no 429.")
    else:
        print("RESULT: not every edge was fetched; see above.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS)
    p.add_argument("--rate", type=float, default=100.0, help="max requests per second (default 100)")
    p.add_argument("--max-in-flight", type=int, default=4)
    p.add_argument("--cycles", type=int, default=1)
    p.add_argument("--out", type=Path, default=Path(__file__).with_name("poll_check_report.json"))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.rate <= 0:
        print("--rate must be positive")
        return 2
    report = asyncio.run(main_async(args))
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print_summary(report)
    print(f"\nreport written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
