"""Background poller — claims tasks from the queue and dispatches them.

Talks to a LIST of orchestrator endpoints (#20 Phase 3, active-active). It uses
the last-known-good endpoint and, on any connection failure, fails over to the
next one — so a worker keeps claiming/reporting even if one orchestrator (e.g.
mac-mini) goes down, since every orchestrator fronts the same rqlite queue.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from shared.models import ClaimRequest, Task, TaskStatus
from worker.handlers import CAPABILITIES, dispatch

log = logging.getLogger(__name__)


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

    @property
    def orchestrator_url(self) -> str:  # current preferred endpoint (compat)
        return self.urls[self._pref] if self.urls else ""

    def _ordered(self) -> list[tuple[int, str]]:
        n = len(self.urls)
        return [((self._pref + k) % n, self.urls[(self._pref + k) % n]) for k in range(n)]

    async def _send(self, method: str, path: str, *, timeout: float = 30, **kw) -> httpx.Response:
        """Send a request, failing over across orchestrators. Remembers the endpoint
        that worked as the new preferred one. Raises if all endpoints are down."""
        last_exc: Exception | None = None
        for idx, url in self._ordered():
            try:
                async with httpx.AsyncClient(base_url=url, headers=self.headers, timeout=timeout) as c:
                    resp = await c.request(method, path, **kw)
                if idx != self._pref:
                    log.warning("orchestrator failover -> %s", url)
                    self._pref = idx
                return resp
            except httpx.HTTPError as e:
                last_exc = e
                continue
        raise last_exc if last_exc else RuntimeError("no orchestrator endpoints configured")

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
            return
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
        # Mark in_progress
        await self._send("PATCH", f"/tasks/{task.id}", json={"status": TaskStatus.in_progress})
        try:
            result = await dispatch(task)
            if result.get("needs_human"):
                # Preserve stdout/stderr/action in result so nothing is lost.
                output = {k: v for k, v in result.items() if k not in ("needs_human", "notes")}
                if output:
                    await self._send("PATCH", f"/tasks/{task.id}", json={"result": output})
                await self._send(
                    "POST", f"/tasks/{task.id}/needs-human",
                    params={"notes": result.get("notes", ""), "action": result.get("action", "")},
                )
            else:
                await self._send("POST", f"/tasks/{task.id}/complete", json=result)
            log.info("Task %s finished", task.id[:8])
        except Exception as exc:
            log.error("Task %s failed: %s", task.id[:8], exc)
            try:
                await self._send("POST", f"/tasks/{task.id}/fail", json={"error": str(exc)})
            except Exception as exc2:
                log.error("Task %s: could not report failure: %s", task.id[:8], exc2)
        finally:
            self.active_tasks = [t for t in self.active_tasks if t != task.id]
