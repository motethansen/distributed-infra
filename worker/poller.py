"""Background poller — claims tasks from the queue and dispatches them.

Talks to a LIST of orchestrator endpoints (#20 Phase 3, active-active). It uses
the last-known-good endpoint and, on any connection failure, fails over to the
next one — so a worker keeps claiming/reporting even if one orchestrator (e.g.
mac-mini) goes down, since every orchestrator fronts the same rqlite queue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import httpx

from shared.models import ClaimRequest, Task, TaskStatus
from worker.handlers import CAPABILITIES, dispatch

log = logging.getLogger(__name__)

# Results that finished but could not be reported are kept here, never dropped.
_UNREPORTED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "unreported")
_REPORT_ATTEMPTS = 3


class Poller:
    def __init__(self, machine_name: str, headers: dict, interval: int,
                 orchestrator_urls: list[str] | None = None,
                 orchestrator_url: str | None = None, max_concurrent: int = 0):
        self.machine_name = machine_name
        self.urls = orchestrator_urls or ([orchestrator_url] if orchestrator_url else [])
        self._pref = 0  # index of the last-known-good orchestrator
        self.headers = headers
        self.interval = interval
        # Max tasks this worker runs at once. 0 = unlimited. When the cap is hit the
        # poller stops claiming, so tasks age in the queue and (if they prefer this
        # box) overflow to another capable worker after the grace window (#5b).
        self.max_concurrent = max_concurrent
        self.active_tasks: list[str] = []
        self._bg: set[asyncio.Task] = set()  # keeps fire-and-forget tasks alive

    @property
    def orchestrator_url(self) -> str:  # current preferred endpoint (compat)
        return self.urls[self._pref] if self.urls else ""

    def _ordered(self) -> list[tuple[int, str]]:
        n = len(self.urls)
        return [((self._pref + k) % n, self.urls[(self._pref + k) % n]) for k in range(n)]

    async def _send(self, method: str, path: str, *, timeout: float = 30, **kw) -> httpx.Response:
        """Send a request, failing over across orchestrators. Remembers the endpoint
        that worked as the new preferred one. A connection error OR an HTTP 5xx moves
        on to the next endpoint (a reachable-but-unhealthy orchestrator, e.g. one that
        lost its rqlite leader, must not stall the worker). If every endpoint answers
        5xx the last response is returned so the caller can see it; if none answer at
        all this raises."""
        last_exc: Exception | None = None
        last_resp: httpx.Response | None = None
        for idx, url in self._ordered():
            try:
                async with httpx.AsyncClient(base_url=url, headers=self.headers, timeout=timeout) as c:
                    resp = await c.request(method, path, **kw)
            except httpx.HTTPError as e:
                last_exc = e
                continue
            if resp.status_code >= 500:
                last_resp = resp
                log.warning("orchestrator %s answered %d for %s %s — trying next", url, resp.status_code, method, path)
                continue
            if idx != self._pref:
                log.warning("orchestrator failover -> %s", url)
                self._pref = idx
            return resp
        if last_resp is not None:
            return last_resp
        raise last_exc if last_exc else RuntimeError("no orchestrator endpoints configured")

    async def _report(self, method: str, path: str, **kw) -> httpx.Response:
        """_send + require success. Retries connection errors and 5xx with backoff; a 4xx
        (the orchestrator understood and refused) fails at once. Raises on failure so a
        lost report is never silently treated as delivered."""
        last = "no attempt"
        for attempt in range(_REPORT_ATTEMPTS):
            try:
                resp = await self._send(method, path, **kw)
                if resp.status_code < 400:
                    return resp
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code < 500:
                    break
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
            if attempt < _REPORT_ATTEMPTS - 1:
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"could not report {method} {path}: {last}")

    def _heartbeat(self, urls: list[str] | None = None) -> None:
        """Fire-and-forget: a dead orchestrator must not slow the poll loop."""
        t = asyncio.create_task(self._heartbeat_now(urls))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _heartbeat_now(self, urls: list[str] | None = None) -> None:
        """Tell orchestrators this worker is alive. Liveness is kept per orchestrator
        process, so a worker polling one would look dead through the other; this
        announces it to the rest (and to all of them while at the concurrency cap,
        when no claim is made). Best effort."""
        for url in (urls if urls is not None else self.urls):
            try:
                async with httpx.AsyncClient(base_url=url, headers=self.headers, timeout=3) as c:
                    await c.post("/workers/heartbeat", json={"worker_name": self.machine_name})
            except httpx.HTTPError:
                pass

    def _spool_unreported(self, task: Task, outcome: dict) -> None:
        try:
            os.makedirs(_UNREPORTED_DIR, exist_ok=True)
            path = os.path.join(_UNREPORTED_DIR, f"{task.id}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"task_id": task.id, "type": task.type, "worker": self.machine_name,
                           "saved_at": datetime.now(timezone.utc).isoformat(), "outcome": outcome},
                          f, indent=2, default=str)
            log.error("Task %s: outcome could not be reported — saved to %s", task.id[:8], path)
        except OSError as e:
            log.error("Task %s: outcome could not be reported or saved: %s", task.id[:8], e)

    async def run(self) -> None:
        log.info("Poller started — every %ds for %s via %s", self.interval, CAPABILITIES, self.urls)
        while True:
            try:
                await self._poll_once()
            except Exception as exc:
                log.warning("Poller error: %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(self.interval)

    async def _poll_once(self) -> None:
        # Concurrency cap: don't claim more while at capacity (lets work overflow).
        if self.max_concurrent and len(self.active_tasks) >= self.max_concurrent:
            log.debug("At concurrency cap (%d/%d) — not claiming", len(self.active_tasks), self.max_concurrent)
            self._heartbeat()   # busy is not dead
            return
        # Claim goes to one orchestrator; announce to the others so liveness is cluster-wide.
        self._heartbeat([u for i, u in self._ordered() if i != self._pref])
        resp = await self._send(
            "POST", "/tasks/claim", timeout=10,
            json=ClaimRequest(worker_name=self.machine_name, capabilities=CAPABILITIES).model_dump(),
        )

        if resp.status_code == 204:
            return  # nothing in the queue
        if resp.status_code != 200:
            log.warning("Unexpected claim response: %d", resp.status_code)
            return

        task = Task(**resp.json())
        self.active_tasks.append(task.id)
        log.info("Claimed task %s (%s)", task.id[:8], task.type)
        asyncio.create_task(self._run_task(task))

    async def _run_task(self, task: Task) -> None:
        outcome: dict = {}
        try:
            await self._report("PATCH", f"/tasks/{task.id}", json={"status": TaskStatus.in_progress})
            result = await dispatch(task)
            outcome = {"kind": "needs_human" if result.get("needs_human") else "complete", "result": result}
            if result.get("needs_human"):
                # Preserve stdout/stderr/action in result so nothing is lost.
                output = {k: v for k, v in result.items() if k not in ("needs_human", "notes")}
                if output:
                    await self._report("PATCH", f"/tasks/{task.id}", json={"result": output})
                await self._report(
                    "POST", f"/tasks/{task.id}/needs-human",
                    params={"notes": result.get("notes", ""), "action": result.get("action", "")},
                )
            else:
                await self._report("POST", f"/tasks/{task.id}/complete", json=result)
            log.info("Task %s finished", task.id[:8])
        except Exception as exc:
            log.error("Task %s failed: %s", task.id[:8], exc)
            if outcome:
                # The work ran; only the report failed. Keep the result locally.
                self._spool_unreported(task, outcome)
            try:
                await self._report("POST", f"/tasks/{task.id}/fail", json={"error": str(exc)})
            except Exception as exc2:
                log.error("Task %s: could not report failure: %s", task.id[:8], exc2)
        finally:
            self.active_tasks = [t for t in self.active_tasks if t != task.id]
