"""Worker FastAPI app — runs on ThinkPad (Android) and Mac Mini (iOS)."""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Header

from shared.models import Task, TaskStatus, TaskType, WorkerStatus
from worker.poller import Poller

MACHINE_NAME = os.getenv("MACHINE_NAME", "unknown-worker")
TAILSCALE_IP = os.getenv("TAILSCALE_IP", "")
WORKER_PORT = int(os.getenv("WORKER_PORT", "8001"))
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000")
SECRET_KEY = os.getenv("SECRET_KEY", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "10"))
HEADERS = {"x-secret-key": SECRET_KEY}

START_TIME = time.time()
poller: Poller | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global poller
    poller = Poller(
        machine_name=MACHINE_NAME,
        orchestrator_url=ORCHESTRATOR_URL,
        headers=HEADERS,
        interval=POLL_INTERVAL,
    )
    task = asyncio.create_task(poller.run())
    yield
    task.cancel()


app = FastAPI(title=f"Worker — {MACHINE_NAME}", version="0.1.0", lifespan=lifespan)


def _check_auth(x_secret_key: str) -> None:
    if SECRET_KEY and x_secret_key != SECRET_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
async def health(x_secret_key: str = Header(default="")):
    _check_auth(x_secret_key)
    return {
        "status": "ok",
        "machine": MACHINE_NAME,
        "role": "worker",
        "uptime_seconds": round(time.time() - START_TIME, 1),
    }


@app.get("/status", response_model=WorkerStatus)
async def status(x_secret_key: str = Header(default="")):
    _check_auth(x_secret_key)
    from worker.handlers import CAPABILITIES
    return WorkerStatus(
        machine_name=MACHINE_NAME,
        tailscale_ip=TAILSCALE_IP,
        capabilities=CAPABILITIES,
        active_tasks=len(poller.active_tasks) if poller else 0,
        uptime_seconds=round(time.time() - START_TIME, 1),
    )


@app.get("/tasks/active")
async def active_tasks(x_secret_key: str = Header(default="")):
    _check_auth(x_secret_key)
    return {"active": poller.active_tasks if poller else []}
