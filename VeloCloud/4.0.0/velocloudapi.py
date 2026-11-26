"""
velocloudapi.py: rate-limited VeloCloud client that returns the same edge, link
and health data as the Dynatrace extension, with a request budget per cycle.

Requests (same endpoints the extension uses):
  POST /portal/rest/enterprise/getEnterpriseConfigurations
  GET  /api/sdwan/v2/enterprises/{id}/edges
  GET  /api/sdwan/v2/enterprises/{id}/edges/{edge}/linkStats?include=link.*
  GET  /api/sdwan/v2/enterprises/{id}/edges/{edge}/healthstats

What the code enforces:
  * No more than `max_rps` requests start in any second. The rate adapts to the
    server: a 429 halves it (down to `min_rps`), each success raises it 2%
    back toward `max_rps`.
  * At most `max_in_flight` requests are open at once.
  * A 429 pauses every caller for the penalty. One retry is made. If that also
    fails, all calls stop for `cooldown_s`.
  * Link stats are refreshed every `link_interval_s` and health stats every
    `health_interval_s`, so routine traffic is smaller than one request per
    edge per minute for health.
  * A cycle never starts a request after its time budget, so it ends cleanly.

What it cannot guarantee: the server's limit is set by the VCO operator. If the
server allows less than the pace this client settles on, 429s still occur, and
the cooldown is what stops them from continuing.

Usage:
  python velocloudapi.py                 # one cycle, prints a summary
  python velocloudapi.py --cycles 3      # three cycles, one per minute
  python velocloudapi.py --max-rps 5     # lower ceiling
"""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

DEFAULT_BASE_URL = "https://vco61-usvi1.velocloud.net"
SECRETS_FILE = Path(r"C:\Users\280380\velorevise\secrets.json")
TOKEN_KEY = "veloCloudToken"

# Same numeric mappings as the extension (__main__.py)
EDGE_STATE_NUM = {"CONNECTED": 1, "OFFLINE": 0, "DEGRADED": 2}
HA_STATE_NUM = {"READY": 1, "UNCONFIGURED": 0, "PENDING_DISSOCIATION": 2, "FAILED": 3}


class RateLimited(Exception):
    """The server answered 429. Carries Retry-After when the server sent it."""

    def __init__(self, retry_after: Optional[float]) -> None:
        super().__init__("HTTP 429")
        self.retry_after = retry_after


class ApiBlocked(Exception):
    """No call is made: the API is in cooldown or the cycle budget is used up."""


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    max_rps: float = 10.0  # ceiling; set to the operator's confirmed limit
    min_rps: float = 1.0  # adaptive floor
    max_in_flight: int = 2  # guide: "a handful"
    link_interval_s: float = 60.0  # link stats refresh (guide: 30 s data refresh)
    health_interval_s: float = 300.0  # health stats refresh (guide: 5 min device stats)
    edge_cache_ttl_s: float = 900.0  # edge list reuse
    cycle_budget_s: float = 45.0  # no request starts after this
    penalty_s: float = 5.0  # guide: constant backoff on 429
    cooldown_s: float = 600.0  # stop all calls after a failed retry
    timeout_s: float = 15.0


class Pacer:
    """Spaces sends at the current rate. Rate adapts to 429s and successes."""

    def __init__(self, cfg: Config) -> None:
        self.max_rate = cfg.max_rps
        self.min_rate = min(cfg.min_rps, cfg.max_rps)
        self.rate = cfg.max_rps
        self._next_slot = 0.0
        self._last_send = float("-inf")
        self._penalty_until = float("-inf")

    @property
    def interval(self) -> float:
        return 1.0 / self.rate

    def next_send_time(self) -> float:
        return max(self._next_slot, self._penalty_until, time.monotonic())

    async def wait(self) -> None:
        slot = max(self._next_slot, time.monotonic())
        self._next_slot = slot + self.interval
        # Re-check the penalty on every loop: calls already queued must also wait,
        # otherwise they send straight into the penalty window
        while True:
            due = max(slot, self._last_send + self.interval, self._penalty_until)
            remaining = due - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)
        self._last_send = time.monotonic()

    def pause(self, seconds: float) -> None:
        self._penalty_until = max(self._penalty_until, time.monotonic() + seconds)

    def on_429(self) -> None:
        self.rate = max(self.min_rate, self.rate / 2)

    def on_ok(self) -> None:
        self.rate = min(self.max_rate, self.rate * 1.02)


class VeloCloudAPI:
    """Rate-limited client. Use as an async context manager."""

    def __init__(self, cfg: Config, token: str) -> None:
        self.cfg = cfg
        self.headers = {"Authorization": f"Token {token}"}
        self.pacer = Pacer(cfg)
        self.deadline: Optional[float] = None
        self.blocked_until = 0.0
        self.requests_sent = 0
        self.status_counts: Dict[str, int] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._gate: Optional[asyncio.Semaphore] = None
        self._enterprise_id: Optional[str] = None
        self._edges: List[Dict[str, Any]] = []
        self._edges_at = float("-inf")
        self._last_link: Dict[str, float] = {}
        self._last_health: Dict[str, float] = {}

    async def __aenter__(self) -> "VeloCloudAPI":
        self._session = aiohttp.ClientSession()
        self._gate = asyncio.Semaphore(self.cfg.max_in_flight)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._session:
            await self._session.close()

    def is_blocked(self) -> bool:
        return time.monotonic() < self.blocked_until

    def _count(self, key: str) -> None:
        self.status_counts[key] = self.status_counts.get(key, 0) + 1

    async def _send(self, method: str, url: str, body: Any = None) -> Any:
        assert self._session and self._gate
        if self.is_blocked():
            raise ApiBlocked("cooldown active")
        async with self._gate:
            # Check the budget here, not before the gate: queued tasks would all
            # pass an early check, then run long after the budget ended.
            if self.is_blocked():
                raise ApiBlocked("cooldown active")
            if self.deadline is not None and self.pacer.next_send_time() > self.deadline:
                raise ApiBlocked("cycle budget used up")
            await self.pacer.wait()
            self.requests_sent += 1
            async with self._session.request(
                method, url, headers=self.headers, json=body,
                timeout=aiohttp.ClientTimeout(total=self.cfg.timeout_s),
            ) as r:
                self._count(str(r.status))
                if r.status == 429:
                    self.pacer.on_429()
                    self.pacer.pause(self.cfg.penalty_s)
                    raise RateLimited(_retry_after(r.headers.get("Retry-After")))
                r.raise_for_status()
                self.pacer.on_ok()
                return await r.json(content_type=None)

    async def _get(self, method: str, url: str, body: Any = None) -> Any:
        """One request, plus one retry after the penalty. A second 429 starts the cooldown."""
        try:
            return await self._send(method, url, body)
        except RateLimited as first:
            wait = max(self.cfg.penalty_s, first.retry_after or 0.0)
            if self.deadline is not None and time.monotonic() + wait > self.deadline:
                # Out of cycle time, not a server block: end this call only
                raise ApiBlocked("no cycle time left for the penalty; retry next cycle")
            await asyncio.sleep(wait)
        try:
            return await self._send(method, url, body)
        except RateLimited:
            self.blocked_until = time.monotonic() + self.cfg.cooldown_s
            raise ApiBlocked("still rate limited after retry; cooldown started")

    async def enterprise_id(self) -> str:
        if not self._enterprise_id:
            conf = await self._get(
                "POST", f"{self.cfg.base_url}/portal/rest/enterprise/getEnterpriseConfigurations", {}
            )
            self._enterprise_id = str(conf[0]["enterpriseLogicalId"])
        return self._enterprise_id

    async def edges(self) -> List[Dict[str, Any]]:
        if self._edges and time.monotonic() - self._edges_at < self.cfg.edge_cache_ttl_s:
            return self._edges
        eid = await self.enterprise_id()
        body = await self._get("GET", f"{self.cfg.base_url}/api/sdwan/v2/enterprises/{eid}/edges")
        self._edges = [e for e in body.get("data", []) if e.get("logicalId") and e.get("name")]
        self._edges_at = time.monotonic()
        return self._edges

    async def link_stats(self, edge_id: str) -> List[Dict[str, Any]]:
        eid = await self.enterprise_id()
        url = f"{self.cfg.base_url}/api/sdwan/v2/enterprises/{eid}/edges/{edge_id}/linkStats?include=link.*"
        result = await self._get("GET", url)
        return result if isinstance(result, list) else []

    async def health_stats(self, edge_id: str) -> Dict[str, Any]:
        eid = await self.enterprise_id()
        url = f"{self.cfg.base_url}/api/sdwan/v2/enterprises/{eid}/edges/{edge_id}/healthstats"
        result = await self._get("GET", url)
        return result if isinstance(result, dict) else {}

    async def poll_cycle(self) -> Dict[str, Any]:
        """
        One cycle: choose what is due, fetch it within the budget, return records.

        Link stats come first (they refresh each minute), then health stats for
        edges whose health data is older than `health_interval_s`.
        """
        started = time.monotonic()
        self.deadline = started + self.cfg.cycle_budget_s
        out: Dict[str, Any] = {"links": [], "health": [], "edges": [], "errors": [], "skipped": []}
        try:
            if self.is_blocked():
                out["skipped"].append("cooldown active; no calls made this cycle")
                return out
            edges = await self.edges()
            out["edges"] = [normalize_edge(e) for e in edges]
            budget_calls = max(1, int(self.cfg.cycle_budget_s * self.pacer.rate))

            due_link = sorted(
                (e for e in edges if started - self._last_link.get(e["logicalId"], float("-inf")) >= self.cfg.link_interval_s),
                key=lambda e: self._last_link.get(e["logicalId"], float("-inf")),
            )
            due_health = sorted(
                (e for e in edges if started - self._last_health.get(e["logicalId"], float("-inf")) >= self.cfg.health_interval_s),
                key=lambda e: self._last_health.get(e["logicalId"], float("-inf")),
            )
            # Reserve about a quarter of the budget for health, so health data keeps
            # refreshing when the link calls alone would use the whole budget.
            health_quota = min(len(due_health), max(1, budget_calls // 4))
            link_quota = budget_calls - health_quota
            links_now = due_link[:link_quota]
            health_now = due_health[:health_quota]
            spare = budget_calls - len(links_now) - len(health_now)  # unused quota goes to health
            if spare > 0:
                health_now = due_health[:health_quota + spare]
            # Interleave, so that if the budget runs out mid-cycle both kinds progressed
            plan: List[Tuple[str, Dict[str, Any]]] = []
            for i in range(max(len(links_now), len(health_now))):
                if i < len(links_now):
                    plan.append(("link", links_now[i]))
                if i < len(health_now):
                    plan.append(("health", health_now[i]))

            async def run(kind: str, edge: Dict[str, Any]) -> None:
                eid = edge["logicalId"]
                try:
                    if kind == "link":
                        links = await self.link_stats(eid)
                        out["links"].extend(normalize_links(edge, links))
                        self._last_link[eid] = time.monotonic()
                    else:
                        health = await self.health_stats(eid)
                        out["health"].append(normalize_health(edge, health))
                        self._last_health[eid] = time.monotonic()
                except ApiBlocked as exc:
                    out["skipped"].append(f"{kind} {edge['name']}: {exc}")
                except Exception as exc:  # network or HTTP error for one call
                    out["errors"].append(f"{kind} {edge['name']}: {type(exc).__name__}")

            await asyncio.gather(*(run(k, e) for k, e in plan))
        finally:
            self.deadline = None
        out["seconds"] = round(time.monotonic() - started, 1)
        out["rate_req_s"] = round(self.pacer.rate, 2)
        out["planned_calls"] = len(plan) if "plan" in locals() else 0
        out["blocked"] = self.is_blocked()
        return out


def _retry_after(value: Optional[str]) -> Optional[float]:
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


def normalize_edge(edge: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "edge_name": str(edge.get("name")),
        "edge_logical_id": str(edge.get("logicalId")),
        "ip_address": str(edge.get("ipAddress") or "UNKNOWN"),
        "edge_state": edge.get("edgeState", "UNKNOWN"),
        "edge_state_num": EDGE_STATE_NUM.get(str(edge.get("edgeState")), 0),
        "ha_state": edge.get("haState", "UNKNOWN"),
        "ha_state_num": HA_STATE_NUM.get(str(edge.get("haState")), 0),
        "service_state": edge.get("serviceState", "UNKNOWN"),
        "service_state_num": 1 if edge.get("serviceState") == "IN_SERVICE" else 0,
    }


def normalize_links(edge: Dict[str, Any], links: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One record per link, plus the edge's all_links_offline flag (as the extension computes it)."""
    e = normalize_edge(edge)
    total = len(links)
    offline = sum(1 for l in links if l.get("link", {}).get("state") != "STABLE")
    records = []
    for link in links:
        state = link.get("link", {}).get("state")
        records.append({
            **e,
            "ip_address": str(link.get("link", {}).get("ipAddress") or "UNKNOWN"),
            "link_state": state or "UNKNOWN",
            "link_state_num": 1 if state == "STABLE" else 0,
            "jitter_ms_rx": link.get("bestJitterMsRx"),
            "jitter_ms_tx": link.get("bestJitterMsTx"),
            "latency_ms_rx": link.get("bestLatencyMsRx"),
            "latency_ms_tx": link.get("bestLatencyMsTx"),
            "loss_pct_rx": link.get("bestLossPctRx"),
            "loss_pct_tx": link.get("bestLossPctTx"),
            "all_links_offline": 1 if total > 0 and offline == total else 0,
        })
    if not records:  # keep the edge-level flag even when there are no links
        records.append({**e, "link_state": "UNKNOWN", "link_state_num": 0, "all_links_offline": 0})
    return records


def normalize_health(edge: Dict[str, Any], h: Dict[str, Any]) -> Dict[str, Any]:
    def avg(key: str) -> Any:
        return (h.get(key) or {}).get("average")

    return {
        **normalize_edge(edge),
        "cpu_utilization": avg("cpuPct"),
        "cpu_core_temperature": avg("cpuCoreTemp"),
        "memory_utilization": avg("memoryPct"),
        "flow_count": avg("flowCount"),
        "over_capacity_drops": avg("handoffQueueDrops"),
        "tunnel_count": avg("tunnelCount"),
        "tunnel_count_v6": avg("tunnelCountV6"),
    }


def load_token() -> str:
    return json.loads(SECRETS_FILE.read_text(encoding="utf-8"))[TOKEN_KEY]


async def run(cfg: Config, token: str, cycles: int) -> List[Dict[str, Any]]:
    results = []
    async with VeloCloudAPI(cfg, token) as api:
        for n in range(1, cycles + 1):
            res = await api.poll_cycle()
            res["cycle"] = n
            res["requests_total"] = api.requests_sent
            res["status_counts"] = dict(api.status_counts)
            results.append(res)
            print(
                f"cycle {n}: links {len(res['links'])}, health {len(res['health'])}, "
                f"errors {len(res['errors'])}, skipped {len(res['skipped'])}, "
                f"rate {res['rate_req_s']} req/s, {res['seconds']} s, "
                f"blocked {res['blocked']}"
            )
            if n < cycles:
                await asyncio.sleep(max(0.0, 60.0 - res["seconds"]))
    return results


def main() -> int:
    p = argparse.ArgumentParser(description="VeloCloud rate-limited poll")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--max-rps", type=float, default=10.0)
    p.add_argument("--cycles", type=int, default=1)
    p.add_argument("--out", type=Path, default=Path(__file__).with_name("velocloudapi_last.json"))
    args = p.parse_args()
    cfg = Config(base_url=args.base_url, max_rps=args.max_rps)
    results = asyncio.run(run(cfg, load_token(), args.cycles))
    args.out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
