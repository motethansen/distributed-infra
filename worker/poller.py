"""Background poller — claims tasks from the queue and dispatches them."""
from __future__ import annotations

import asyncio
import logging

import httpx

from shared.models import ClaimRequest, Task, TaskStatus
from worker.handlers import CAPABILITIES, dispatch

log = logging.getLogger(__name__)


class Poller:
    def __init__(self, machine_name: str, orchestrator_url: str, headers: dict, interval: int):
        self.machine_name = machine_name
        self.orchestrator_url = orchestrator_url
        self.headers = headers
        self.interval = interval
        self.active_tasks: list[str] = []

    async def run(self) -> None:
        log.info("Poller started — checking every %ds for %s", self.interval, CAPABILITIES)
        while True:
            try:
                await self._poll_once()
            except Exception as exc:
                log.warning("Poller error: %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(self.interval)

    async def _poll_once(self) -> None:
        async with httpx.AsyncClient(base_url=self.orchestrator_url, headers=self.headers, timeout=10) as client:
            resp = await client.post(
                "/tasks/claim",
                json=ClaimRequest(
                    worker_name=self.machine_name,
                    capabilities=CAPABILITIES,
                ).model_dump(),
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
        async with httpx.AsyncClient(base_url=self.orchestrator_url, headers=self.headers, timeout=60) as client:
            # Mark in_progress
            await client.patch(f"/tasks/{task.id}", json={"status": TaskStatus.in_progress})
            try:
                result = await dispatch(task)
                if result.get("needs_human"):
                    await client.post(
                        f"/tasks/{task.id}/needs-human",
                        params={"notes": result.get("notes", "")},
                    )
                else:
                    await client.post(f"/tasks/{task.id}/complete", json=result)
                log.info("Task %s finished", task.id[:8])
            except Exception as exc:
                log.error("Task %s failed: %s", task.id[:8], exc)
                await client.post(f"/tasks/{task.id}/fail", json={"error": str(exc)})
            finally:
                self.active_tasks = [t for t in self.active_tasks if t != task.id]
