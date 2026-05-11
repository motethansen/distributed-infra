from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    pending = "pending"
    claimed = "claimed"
    in_progress = "in_progress"
    done = "done"
    failed = "failed"
    needs_human = "needs_human"


class TaskType(str, Enum):
    # Build
    android_build = "android_build"
    ios_build = "ios_build"
    npm_build = "npm_build"
    # Dev workflow
    git_pull = "git_pull"
    test_run = "test_run"
    lint = "lint"
    run_script = "run_script"
    # Agent
    agent_run = "agent_run"
    assistant_run = "assistant_run"
    # Human review
    human_action = "human_action"
    custom = "custom"


class Task(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: TaskType
    status: TaskStatus = TaskStatus.pending
    priority: int = Field(default=5, ge=0, le=10)  # 10 = highest
    payload: dict[str, Any] = Field(default_factory=dict)
    created_by: str  # machine name
    assigned_to: str | None = None
    result: dict[str, Any] | None = None
    notes: str | None = None  # human-readable context / review notes
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TaskCreate(BaseModel):
    type: TaskType
    priority: int = 5
    payload: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class TaskUpdate(BaseModel):
    status: TaskStatus | None = None
    result: dict[str, Any] | None = None
    notes: str | None = None
    assigned_to: str | None = None


class WorkerStatus(BaseModel):
    machine_name: str
    tailscale_ip: str
    capabilities: list[str]
    active_tasks: int
    uptime_seconds: float
    worker_version: str = "0.1.0"


class ClaimRequest(BaseModel):
    worker_name: str
    capabilities: list[str]
