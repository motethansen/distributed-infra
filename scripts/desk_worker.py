#!/usr/bin/env python3
"""Desk worker (S-9): long-running launchd job that claims T1 desk commands from the orchestrator and runs them one at a time.

Command rows are untrusted data: their text never reaches an agent prompt, and this module makes no model call. Only the
typed verb and identifiers that pass shared.desk_allowlist.is_valid_id become argv entries (always shell=False); the one
free-text field, step_done evidence, is a single `--evidence=<text>` argv entry of `dis-agent done`, which calls no model.
The secret comes from the environment (DESK_SECRET, see run-with-env.sh), never from the repo.
"""
from __future__ import annotations

import fcntl
import json
import math
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.desk_allowlist import ALLOWLIST, check_verb, is_valid_id  # noqa: E402


LIST_PATH = "/scrum/desk/commands?status={status}"
CLAIM_PATH = "/scrum/responses/{id}/consume"
RESULT_PATH = "/scrum/responses/{id}/result"
SPRINTS_PATH = "/scrum/sprints"
ACTOR = "desk-worker"
SUMMARY_MAX = 2000
EVIDENCE_MAX = 2000
VERBS = tuple(v for v, e in ALLOWLIST.items() if e["tier"] == "T1")   # exactly the allowlisted T1 verbs

BACKOFF_START, BACKOFF_MAX = 30.0, 300.0
PLAN_TIMEOUT = 1800
VALIDATE_TIMEOUT = 2400   # a validate is a ~30 minute run (D-006)
HEARTBEAT_PATH = "/workers/heartbeat"
DISABLED_STATE = "disabled: D-006 not confirmed"
# D-006 (decided 2026-11-06): $5/day across page-triggered Product Owner calls, plus the monthly cap of
# config/roles.yaml product_owner.monthly_cost_cap_usd ($50). Env overrides; these are the only places the numbers live.
def _cap_from_env(name: str, default: float) -> float:
    """A cap from the environment; anything that is not a finite number above zero falls back to the D-006 default."""
    try:
        v = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return v if math.isfinite(v) and v > 0 else default


DAILY_CAP_USD = _cap_from_env("DESK_DAILY_CAP_USD", 5.0)
MONTHLY_CAP_USD = _cap_from_env("DESK_MONTHLY_CAP_USD", 50.0)


class OrchestratorUnreachable(Exception):
    """Any network-level failure talking to the orchestrator."""


class ApiError(Exception):
    """The orchestrator answered with an HTTP error status (409 already claimed, 423 paused, ...)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


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
        # dis-agent keeps its state under <its checkout>/data/scrum, so the worker reads the same place.
        return cls(env.get("ORCHESTRATOR_URL", "").rstrip("/"), env.get("DESK_SECRET", ""), root, root / "data",
                   float(env.get("DESK_POLL_SECONDS") or 30))


class OrchestratorClient:
    """Thin HTTP client (urllib). Network failures become OrchestratorUnreachable, HTTP 4xx/5xx become ApiError."""

    def __init__(self, base_url: str, secret: str, opener: Callable[..., Any] = urllib.request.urlopen, timeout: float = 15.0) -> None:
        self.base, self.timeout, self.opener = base_url, timeout, opener
        self.headers = {"X-Secret-Key": secret, "X-Actor": ACTOR, "Content-Type": "application/json"}

    def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=self.headers, method=method)
        try:
            with self.opener(req, timeout=self.timeout) as r:
                raw = r.read()
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                raise OrchestratorUnreachable(f"HTTP {e.code}") from None
            raise ApiError(e.code) from None
        except (OSError, ValueError) as e:
            raise OrchestratorUnreachable(type(e).__name__) from None  # never echo the URL or headers

    def list_commands(self, status: str) -> list[dict]:
        out = self._call("GET", LIST_PATH.format(status=status))
        return [c for c in out if isinstance(c, dict)] if isinstance(out, list) else []

    def claim(self, cmd_id: int) -> bool:
        """queued -> running. False when someone else claimed it (409); ApiError(423) when the desk is paused."""
        try:
            self._call("POST", CLAIM_PATH.format(id=int(cmd_id)))
        except ApiError as e:
            if e.status in (404, 409):
                return False
            raise
        return True

    def done(self, cmd_id: int, result: dict) -> None:
        self._call("POST", RESULT_PATH.format(id=int(cmd_id)), {"status": "done", "summary": _summary(result)})

    def failed(self, cmd_id: int, error: str) -> None:
        self._call("POST", RESULT_PATH.format(id=int(cmd_id)), {"status": "failed", "summary": error[:SUMMARY_MAX]})

    def heartbeat(self, state: str) -> None:
        self._call("POST", HEARTBEAT_PATH, {"worker_name": ACTOR, "state": state})

    def opus_usage(self) -> dict | None:
        """{'spent', 'cap', 'day_spent'}: this month's and today's Opus spend, as the scrum board's digest reports it
        (None if unknown)."""
        out = self._call("GET", SPRINTS_PATH)
        opus = ((out or {}).get("digest") or {}).get("opus") if isinstance(out, dict) else None
        return opus if isinstance(opus, dict) else None


def _summary(result: dict) -> str:
    """Compact JSON of the result, always valid and at most SUMMARY_MAX long: it shrinks the structure, never the text."""
    def dump(r: dict) -> str:
        return json.dumps(r, separators=(",", ":"), ensure_ascii=False)
    text = dump(result)
    if len(text) <= SUMMARY_MAX:
        return text
    slim = {k: v for k, v in result.items() if k not in ("output", "todo")}   # todo repeats the ids in unplanned
    slim["truncated"] = True
    for key in ("unplanned", "planned"):
        items = slim.get(key)
        if isinstance(items, list) and len(dump(slim)) > SUMMARY_MAX:
            slim["total" if key == "unplanned" else "planned_total"] = len(items)
            while items and len(dump({**slim, key: items})) > SUMMARY_MAX:
                items = items[:-1]
            slim[key] = items
    text = dump(slim)
    return text if len(text) <= SUMMARY_MAX else dump({"truncated": True})


def d006_accepted(decisions_path: Path | str | None = None, repo_root: Path | None = None) -> bool:
    """True only if the D-006 entry in decisions.md has a Status line that says it was decided/accepted.

    The real entry is `## D-006 — ...` followed by `**Status**: **Decided by the owner on <date>** ...`; a status that
    only says Proposed (or anything else) is not enough. The entry ends at the next heading of the same or higher level."""
    path = Path(decisions_path) if decisions_path else viz_decisions_path()
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return False
    level = 0
    for line in lines:
        m = re.match(r"^(#{1,6})\s*D-006\b", line)
        if level == 0:
            level = len(m.group(1)) if m else 0
            continue
        h = re.match(r"^(#{1,6})\s", line)
        if h and len(h.group(1)) <= level:
            break
        if re.match(r"^[\s*_>-]*status[\s*_]*:[\s*_]*(decided|accepted)\b", line, re.I):
            return True
    return False


def viz_decisions_path() -> Path:
    """vantage/decisions.md in the notes repo (VIZNEO_REPO, as dis_agent.viz_repo reads it)."""
    return Path(os.environ.get("VIZNEO_REPO", "~/Projects/github/techstartups/vizneoprojects")).expanduser() / "vantage" / "decisions.md"


def check_caps(client: OrchestratorClient) -> None:
    """Refuse a model-calling verb (plan, validate) once Opus spend has reached the D-006 daily or monthly cap.

    Usage comes from the orchestrator's /scrum/sprints digest ({'spent', 'cap', 'day_spent'}). Fails closed: unknown
    usage (lookup failed, None, or a missing/garbled figure) is `cap_unknown`, never a pass. The monthly cap is the lower
    of MONTHLY_CAP_USD and the cap the orchestrator reports from config/roles.yaml. Spend and cap figures must be finite,
    non-negative numbers; NaN, a missing or null cap and the like are cap_unknown. A cap of 0 means no spend allowed.
    The worker is a SINGLE instance (launchd KeepAlive); the startup lock refuses a second one but is not held while a
    command runs, so never start two by hand."""
    unknown = CommandError("cap_unknown", "cannot verify the Opus spend, so not running a model-calling command")
    try:
        opus = client.opus_usage()
    except (OrchestratorUnreachable, ApiError):
        raise unknown from None
    def figure(v) -> float:
        """A spend or cap figure must be a real, finite, non-negative number; NaN, inf, a bool or text is unknown."""
        if isinstance(v, bool):
            raise ValueError("bool is not a figure")
        x = float(v)
        if not math.isfinite(x) or x < 0:
            raise ValueError("not a finite non-negative number")
        return x
    try:
        spent, day = figure(opus["spent"]), figure(opus["day_spent"])
        cap = min(MONTHLY_CAP_USD, figure(opus["cap"]))        # the orchestrator must state its cap; a missing one is unknown, not the default
    except (TypeError, ValueError, KeyError):
        raise unknown from None
    if day >= DAILY_CAP_USD:
        raise CommandError("cap_reached", f"Opus spend today {day:.2f} >= daily cap {DAILY_CAP_USD:.2f}")
    if spent >= cap:
        raise CommandError("cap_reached", f"Opus spend {spent:.2f} >= monthly cap {cap:.2f}")


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


def _ident(cmd: dict, key: str) -> str:
    v = cmd.get(key)
    if not is_valid_id(v) or v in (".", ".."):
        raise CommandError("bad_args", f"invalid {key}")
    return v


def _sprint_dir(cfg: Config, project: str, sprint: str) -> Path:
    return cfg.data_dir / "scrum" / project / sprint


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def proposal_from_dir(data_dir: Path, project: str, sprint: str, now: datetime | None = None) -> dict:
    """Read-only, no model call. Mirrors dis_agent._local_proposals for one sprint dir: a story is planned when a
    non-empty *.plan.json names it, and a todo story is one not planned and not human_only; .dismissed hides it."""
    sdir = Path(data_dir) / "scrum" / project / sprint
    doc = _read_json(sdir / "stories.json")
    stories = [x for x in (doc.get("stories", []) if isinstance(doc, dict) else []) if isinstance(x, dict) and "id" in x]
    planned: set[str] = set()
    for p in sorted(sdir.glob("*.plan.json")):
        plan = _read_json(p) if p.read_text().strip() else None
        sid = (plan or {}).get("story", {}).get("id") if isinstance(plan, dict) and isinstance(plan.get("story"), dict) else None
        if sid:
            planned.add(sid)
    dismissed = (sdir / ".dismissed").exists()
    # dis_agent._local_proposals skips a dismissed sprint entirely, so a dismissed one publishes no unplanned stories
    todo = [] if dismissed else [x for x in stories if x["id"] not in planned and not x.get("human_only")]
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    return {"project": project, "sprint": sprint, "dismissed": dismissed,
            "stories": len(stories), "points": sum(x.get("points") or 0 for x in stories),
            "todo": [x["id"] for x in todo], "planned": sorted(planned),
            "unplanned": [{"id": x["id"], "title": x.get("title"), "points": x.get("points")} for x in todo],
            "generated_at": stamp}


def _dis_agent_argv(cfg: Config, *args: str) -> list[str]:
    return [sys.executable, str(cfg.repo_root / "scripts" / "dis_agent.py"), *args]


def _run_argv(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=timeout)


def _tail(text: str | None, n: int = 1000) -> str:
    return (text or "")[-n:]


def run_command(cmd: dict, cfg: Config, run: Callable[..., Any] = _run_argv,
                caps_hook: Callable[[], None] | None = None) -> dict:
    """Execute one typed command (row fields: verb, project, sprint, story_id, evidence) under the worker lock.

    Raises CommandError on failure. The verb is re-checked against shared.desk_allowlist: only T1 runs."""
    verb = cmd.get("verb")
    try:
        verdict, _tier = check_verb(verb)
    except ValueError:
        raise CommandError("unknown_verb", "verb is not on the allowlist") from None
    if verdict != "ok":
        raise CommandError("not_t1", "only T1 verbs run on the desk worker")
    project, sprint = _ident(cmd, "project"), _ident(cmd, "sprint")
    with worker_lock(cfg.data_dir):
        if verb == "refresh_proposal":
            return proposal_from_dir(cfg.data_dir, project, sprint)
        story = _ident(cmd, "story_id")
        sdir = _sprint_dir(cfg, project, sprint)
        flags = ["-p", project, "-s", sprint, "--story", story]
        if verb == "plan":
            f = sdir / "stories.json"
            doc = _read_json(f)
            if not isinstance(doc, dict):
                raise CommandError("story_not_found", f"{f} not found; run `dis-agent po` first")
            st = next((s for s in doc.get("stories", []) if isinstance(s, dict) and s.get("id") == story), None)
            if not st:
                raise CommandError("story_not_found", f"story {story} not in {f}")   # same text as dis_agent.cmd_plan
            if st.get("human_only"):
                raise CommandError("human_step", f"{story} is a HUMAN step, not agent work, so there is nothing to plan.")
            plan_file = sdir / f"{story}.plan.json"
            if plan_file.exists():  # re-checked under the lock; read-only, never opened for writing
                raise CommandError("plan_exists", f"plan for {story} already exists")
            if caps_hook:
                caps_hook()
            p = run(_dis_agent_argv(cfg, "plan", *flags), PLAN_TIMEOUT)
            if p.returncode != 0:
                raise CommandError("plan_failed", _tail(p.stderr))
            plan = _read_json(plan_file)
            if not isinstance(plan, dict):
                raise CommandError("plan_failed", "plan file missing or unreadable after plan")
            return {"tasks": len(plan.get("tasks", [])), "planned_by": plan.get("planned_by")}
        if verb == "validate":
            if caps_hook:
                caps_hook()
            p = run(_dis_agent_argv(cfg, "validate", *flags), VALIDATE_TIMEOUT)
            return {"ok": p.returncode == 0, "output": _tail(p.stdout)}
        evidence = cmd.get("evidence")  # step_done
        if not isinstance(evidence, str) or not evidence.strip():
            raise CommandError("evidence_required", "evidence is required")
        evidence = evidence.strip()[:EVIDENCE_MAX]
        # `dis-agent done` marks one of the owner's human steps (it refuses anything else); --yes because there is no tty.
        # --evidence=<text> as ONE entry, so text starting with '-' cannot be read as an option.
        p = run(_dis_agent_argv(cfg, "done", *flags, f"--evidence={evidence}", "--yes"), 600)
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
    """Mark rows left in 'running' by a previous crash as failed. Only called at startup, and only while holding the
    worker lock: if a live worker holds it, those rows are its live work, so LockBusy is raised and no row is touched."""
    with worker_lock(cfg.data_dir):
        seen: set[Any] = set()
        while True:   # the list endpoint is paged (oldest 50 first) and failed rows leave 'running', so ask until none are new
            rows = [r for r in client.list_commands("running") if r["id"] not in seen]
            if not rows:
                return
            for row in rows:
                seen.add(row["id"])
                client.failed(row["id"], "worker restarted")


def tick(client: OrchestratorClient, cfg: Config, run: Callable[..., dict] = run_command) -> None:
    """One poll iteration. OrchestratorUnreachable propagates (the caller backs off)."""
    if not d006_accepted(repo_root=cfg.repo_root):
        client.heartbeat(DISABLED_STATE)
        return
    if not _lock_free(cfg.data_dir):
        return
    for cmd in client.list_commands("queued"):
        if check_verb_ok(cmd.get("verb")) and not _claim_and_run(client, cfg, cmd, run):
            return   # paused, or a command ran: one command per tick


def check_verb_ok(verb: Any) -> bool:
    try:
        return check_verb(verb)[0] == "ok"
    except ValueError:
        return False


def _claim_and_run(client: OrchestratorClient, cfg: Config, cmd: dict, run: Callable[..., dict]) -> bool:
    """True to keep scanning (lost the claim race); False once the desk is paused or a command was handled."""
    try:
        if not client.claim(cmd["id"]):
            return True
    except ApiError:
        return False   # 423 paused (or any refusal): leave it queued
    try:
        caps = lambda: check_caps(client)  # noqa: E731
        result = run(cmd, cfg, caps_hook=caps) if run is run_command else run(cmd, cfg)
    except CommandError as e:
        client.failed(cmd["id"], f"{e.code}: {e.message}")
    except LockBusy:
        client.failed(cmd["id"], "busy: worker lock held")
    except Exception as e:  # unexpected: report the type only, not text that may carry untrusted data
        client.failed(cmd["id"], f"internal_error: {type(e).__name__}")
    else:
        client.done(cmd["id"], result)
    return False


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
        except LockBusy:
            sys.exit("desk_worker: another desk worker holds the lock, refusing to start")
        except (OrchestratorUnreachable, ApiError) as e:
            backoff = next_backoff(backoff)
            print(f"desk_worker: orchestrator problem ({e}), retrying in {backoff:.0f}s", file=sys.stderr, flush=True)
        stop.wait(backoff or cfg.poll_seconds)
    print("desk_worker: stopped", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
