#!/usr/bin/env python3
"""Desk worker (S-9): long-running launchd job that claims typed desk commands from the orchestrator and runs them.

Command rows are untrusted data: their text never reaches an agent prompt. Only the typed verb and identifiers that
match ID_RE become argv entries (always shell=False). Secrets come from the environment (see run-with-env.sh).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


CLAIM_PATH = "/desk/commands/claim"
DONE_PATH = "/desk/commands/{id}/done"
FAILED_PATH = "/desk/commands/{id}/failed"
HEARTBEAT_PATH = "/desk/heartbeat"
RUNNING_PATH = "/desk/commands/running"

ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
DECISIONS_REL = Path("vizneoprojects/vantage/decisions.md")
BACKOFF_START, BACKOFF_MAX = 30.0, 300.0
PLAN_TIMEOUT = 1800


class OrchestratorUnreachable(Exception):
    """Any network-level failure talking to the orchestrator."""


class CommandError(Exception):
    """A command failed with a short machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class LockBusy(Exception):
    """Another command holds the worker lock."""


@dataclass
class Config:
    orchestrator_url: str
    secret: str
    repo_root: Path
    data_dir: Path
    poll_seconds: float = 30.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        root = Path(env.get("DESK_REPO_ROOT") or Path(__file__).resolve().parent.parent)
        return cls(env.get("ORCHESTRATOR_URL", "").rstrip("/"), env.get("DESK_SECRET", ""), root,
                   Path(env.get("DESK_DATA_DIR") or root / "data"), float(env.get("DESK_POLL_SECONDS") or 30))


class OrchestratorClient:
    """Thin HTTP client (urllib); every network failure becomes OrchestratorUnreachable."""

    def __init__(self, base_url: str, secret: str, opener: Callable[..., Any] = urllib.request.urlopen, timeout: float = 15.0) -> None:
        self.base, self.timeout, self.opener = base_url, timeout, opener
        self.headers = {"X-Secret-Key": secret, "Content-Type": "application/json"}

    def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=self.headers, method=method)
        try:
            with self.opener(req, timeout=self.timeout) as r:
                raw = r.read()
            return json.loads(raw) if raw else None
        except (OSError, ValueError) as e:  # URLError/HTTPError/timeouts are OSErrors
            raise OrchestratorUnreachable(type(e).__name__) from None  # never echo the URL or headers

    def heartbeat(self, state: str) -> None:
        self._call("POST", HEARTBEAT_PATH, {"state": state})

    def claim(self) -> dict | None:
        out = self._call("POST", CLAIM_PATH, {})
        cmd = out.get("command", out) if isinstance(out, dict) else None
        return cmd or None

    def list_running(self) -> list:
        out = self._call("GET", RUNNING_PATH)
        return list((out or {}).get("ids", [])) if isinstance(out, dict) else list(out or [])

    def done(self, cmd_id: Any, result: dict) -> None:
        self._call("POST", DONE_PATH.format(id=cmd_id), {"result": result})

    def failed(self, cmd_id: Any, error: str) -> None:
        self._call("POST", FAILED_PATH.format(id=cmd_id), {"error": error})


def d006_accepted(decisions_path: Path | str | None = None, repo_root: Path | None = None) -> bool:
    """True only if the D-006 entry in decisions.md carries 'Status: accepted' or '[accepted]'."""
    path = Path(decisions_path) if decisions_path else (repo_root or Path(__file__).resolve().parent.parent) / DECISIONS_REL
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return False
    lead = re.compile(r"^[\s#>*|`_\-\[]*D-(\d+)\b")
    status = re.compile(r"status[\s:*_`|]*accepted\b(?!-)|\[accepted\]", re.I)
    entry: list[str] = []
    inside = False
    for line in lines:
        m = lead.match(line)
        if m:
            if inside:
                break
            inside = m.group(1) == "006"
        elif inside and re.match(r"^#{1,6}\s", line):
            break
        if inside:
            entry.append(line)
    return any(status.search(x) for x in entry)


def load_caps(data_dir: Path) -> dict:
    """Read <data_dir>/desk_caps.json; a missing or unreadable file means no usage recorded."""
    try:
        return json.loads((data_dir / "desk_caps.json").read_text())
    except (OSError, ValueError):
        return {}


def check_caps(cfg: Config, load: Callable[[Path], dict] = load_caps) -> None:
    """Raise CommandError('cap_reached') when an Opus call count has reached its D-006 cap."""
    c = load(cfg.data_dir)
    for used, cap in (("opus_calls_today", "opus_cap_per_day"), ("opus_calls_this_sprint", "opus_cap_per_sprint")):
        if cap in c and int(c.get(used, 0)) >= int(c[cap]):
            raise CommandError("cap_reached", f"{used} {c.get(used, 0)} >= {cap} {c[cap]}")


@contextmanager
def worker_lock(data_dir: Path) -> Iterator[None]:
    """Exclusive non-blocking flock on <data_dir>/desk_worker.lock; raises LockBusy if held."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / "desk_worker.lock", "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LockBusy("desk worker lock is held") from None
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _ident(args: dict, key: str) -> str:
    v = args.get(key)
    if not isinstance(v, str) or not ID_RE.fullmatch(v) or v in (".", ".."):
        raise CommandError("bad_args", f"invalid {key}")
    return v


def _sprint_dir(cfg: Config, project: str, sprint: str) -> Path:
    return cfg.data_dir / "scrum" / project / sprint


def _load_stories(sdir: Path) -> list[dict]:
    try:
        data = json.loads((sdir / "stories.json").read_text())
    except (OSError, ValueError):
        return []
    stories = data.get("stories", []) if isinstance(data, dict) else data
    return [s for s in stories if isinstance(s, dict) and "id" in s]


def proposal_from_dir(data_dir: Path, project: str, sprint: str, now: datetime | None = None) -> dict:
    """Pure: which non-human stories still lack a plan file and which already have one."""
    sdir = Path(data_dir) / "scrum" / project / sprint
    unplanned, planned = [], []
    for s in _load_stories(sdir):
        if s.get("human_only"):
            continue
        if (sdir / f"{s['id']}.plan.json").exists():
            planned.append(s["id"])
        else:
            unplanned.append({"id": s["id"], "title": s.get("title"), "points": s.get("points")})
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    return {"project": project, "sprint": sprint, "unplanned": unplanned, "planned": planned, "generated_at": stamp}


def _run_argv(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=timeout)


def _tail(text: str | None, n: int = 2000) -> str:
    return (text or "")[-n:]


def run_command(cmd: dict, cfg: Config, run: Callable[..., Any] = _run_argv,
                caps_hook: Callable[[Config], None] = check_caps,
                model_hook: Callable[[Config], None] | None = None) -> dict:
    """Execute one typed command under the worker lock. Raises CommandError on failure.

    model_hook is called before validate/step_done (neither calls a model today); plan always calls caps_hook.
    """
    verb, args = cmd.get("verb"), cmd.get("args") or {}
    if not isinstance(args, dict):
        raise CommandError("bad_args", "args must be an object")
    if verb not in ("refresh_proposal", "plan", "validate", "step_done"):
        raise CommandError("unknown_verb", "verb is not supported by this worker")
    project, sprint = _ident(args, "project"), _ident(args, "sprint")
    with worker_lock(cfg.data_dir):
        if verb == "refresh_proposal":
            return proposal_from_dir(cfg.data_dir, project, sprint)
        story = _ident(args, "story_id")
        base = ["dis-agent"]
        tail = ["-p", project, "-s", sprint, "--story", story]
        if verb == "plan":
            if story not in {s["id"] for s in _load_stories(_sprint_dir(cfg, project, sprint))}:
                raise CommandError("story_not_found", f"story {story} not found")
            plan_file = _sprint_dir(cfg, project, sprint) / f"{story}.plan.json"
            if plan_file.exists():  # last-moment re-check; read-only, never opened for writing
                raise CommandError("plan_exists", f"plan for {story} already exists")
            caps_hook(cfg)
            p = run(base + ["plan"] + tail, PLAN_TIMEOUT)
            if p.returncode != 0:
                raise CommandError("plan_failed", _tail(p.stderr))
            try:
                plan = json.loads(plan_file.read_text())
            except (OSError, ValueError):
                raise CommandError("plan_failed", "plan file missing or unreadable after plan") from None
            return {"tasks": len(plan.get("tasks", [])), "planned_by": plan.get("planned_by")}
        if model_hook:
            model_hook(cfg)
        if verb == "validate":
            p = run(base + ["validate"] + tail, 600)
            return {"ok": p.returncode == 0, "output": _tail(p.stdout)}
        step, evidence = _ident(args, "step"), args.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            raise CommandError("evidence_required", "evidence is required")
        p = run(base + ["step-done"] + tail + ["--step", step, "--evidence", evidence], 600)
        if p.returncode != 0:
            raise CommandError("step_done_failed", _tail(p.stderr))
        return {"ok": True, "output": _tail(p.stdout)}


def _lock_free(data_dir: Path) -> bool:
    try:
        with worker_lock(data_dir):
            return True
    except LockBusy:
        return False


def on_start(client: OrchestratorClient, cfg: Config) -> None:
    """Mark rows left in 'running' by a previous crash as failed. Only called at startup."""
    for cmd_id in client.list_running():
        client.failed(cmd_id, "worker restarted")


def tick(client: OrchestratorClient, cfg: Config, run: Callable[..., dict] = run_command) -> None:
    """One poll iteration. OrchestratorUnreachable propagates (the caller backs off)."""
    if not d006_accepted(repo_root=cfg.repo_root):
        client.heartbeat("disabled: D-006 not confirmed")
        return
    if not _lock_free(cfg.data_dir):
        client.heartbeat("busy")
        return
    client.heartbeat("idle")
    cmd = client.claim()
    if not cmd:
        return
    try:
        result = run(cmd, cfg)
    except CommandError as e:
        client.failed(cmd["id"], f"{e.code}: {e.message}")
    except LockBusy:
        client.failed(cmd["id"], "busy: worker lock held")
    except Exception as e:  # unexpected: report the type only, not text that may carry untrusted data
        client.failed(cmd["id"], f"internal_error: {type(e).__name__}")
    else:
        client.done(cmd["id"], result)


def next_backoff(current: float) -> float:
    return min(max(current * 2, BACKOFF_START), BACKOFF_MAX)


def main() -> None:
    cfg = Config.from_env()
    if not cfg.orchestrator_url or not cfg.secret:
        sys.exit("desk_worker: ORCHESTRATOR_URL and DESK_SECRET must be set")
    client = OrchestratorClient(cfg.orchestrator_url, cfg.secret)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    backoff, started = 0.0, False
    while not stop.is_set():
        try:
            if not started:
                on_start(client, cfg)
                started = True
            tick(client, cfg)
            backoff = 0.0
        except OrchestratorUnreachable:
            backoff = next_backoff(backoff)
            print(f"desk_worker: orchestrator unreachable, retrying in {backoff:.0f}s", file=sys.stderr, flush=True)
        stop.wait(backoff or cfg.poll_seconds)
    print("desk_worker: stopped", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
