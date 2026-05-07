"""Handlers available on all worker machines."""
from __future__ import annotations

import asyncio
import os
import shlex

from shared.models import Task


async def _run(cmd: str, cwd: str | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


async def handle_git_pull(task: Task) -> dict:
    """
    payload:
      repo_path: str   — absolute path to the local git repo
      branch: str      — optional branch to checkout first
    """
    repo_path = task.payload.get("repo_path", "")
    branch = task.payload.get("branch", "")

    if not repo_path or not os.path.isdir(repo_path):
        return {"needs_human": True, "notes": f"repo_path not found: {repo_path}"}

    if branch:
        rc, out, err = await _run(f"git checkout {shlex.quote(branch)}", cwd=repo_path)
        if rc != 0:
            return {"needs_human": True, "notes": f"checkout failed: {err}"}

    rc, out, err = await _run("git pull --ff-only", cwd=repo_path)
    if rc != 0:
        return {"needs_human": True, "notes": f"git pull failed: {err}"}

    return {"stdout": out.strip(), "branch": branch or "current"}


async def handle_run_script(task: Task) -> dict:
    """
    payload:
      script: str    — shell command/script to run
      cwd: str       — optional working directory
      timeout: int   — seconds (default 120)
    """
    script = task.payload.get("script", "")
    cwd = task.payload.get("cwd") or None
    timeout = int(task.payload.get("timeout", 120))

    if not script:
        return {"needs_human": True, "notes": "No script provided in payload"}

    try:
        rc, out, err = await asyncio.wait_for(_run(script, cwd=cwd), timeout=timeout)
    except asyncio.TimeoutError:
        return {"needs_human": True, "notes": f"Script timed out after {timeout}s"}

    if rc != 0:
        return {
            "needs_human": True,
            "notes": f"Script exited {rc}",
            "stdout": out,
            "stderr": err,
        }

    return {"returncode": rc, "stdout": out, "stderr": err}


async def handle_test_run(task: Task) -> dict:
    """
    payload:
      project_path: str   — repo root
      runner: str         — pytest | jest | gradle_test | xcode_test (default: pytest)
      args: str           — extra args passed to the runner
      timeout: int        — seconds (default 300)
    """
    project_path = task.payload.get("project_path", "")
    runner = task.payload.get("runner", "pytest")
    args = task.payload.get("args", "")
    timeout = int(task.payload.get("timeout", 300))

    RUNNERS = {
        "pytest":        f"python -m pytest {args} --tb=short -q",
        "jest":          f"npx jest {args} --ci",
        "gradle_test":   f"./gradlew test {args}",
        "xcode_test":    f"xcodebuild test {args}",
    }
    cmd = RUNNERS.get(runner, f"{runner} {args}")

    if not project_path or not os.path.isdir(project_path):
        return {"needs_human": True, "notes": f"project_path not found: {project_path}"}

    try:
        rc, out, err = await asyncio.wait_for(_run(cmd, cwd=project_path), timeout=timeout)
    except asyncio.TimeoutError:
        return {"needs_human": True, "notes": f"Tests timed out after {timeout}s"}

    if rc != 0:
        return {
            "needs_human": True,
            "notes": f"{runner} exited {rc} — tests may be failing",
            "stdout": out[-3000:],
            "stderr": err[-2000:],
        }
    return {"runner": runner, "stdout": out[-3000:], "status": "tests_passed"}


async def handle_lint(task: Task) -> dict:
    """
    payload:
      project_path: str   — repo root
      tool: str           — ruff | eslint | ktlint | swiftlint (default: ruff)
      fix: bool           — auto-fix if supported (default: false)
      args: str           — extra args
    """
    project_path = task.payload.get("project_path", "")
    tool = task.payload.get("tool", "ruff")
    fix = task.payload.get("fix", False)
    args = task.payload.get("args", "")

    FIX_FLAG = {"ruff": "--fix", "eslint": "--fix", "ktlint": "-F", "swiftlint": "--fix"}
    fix_flag = FIX_FLAG.get(tool, "") if fix else ""

    CMDS = {
        "ruff":      f"ruff check {fix_flag} {args} .",
        "eslint":    f"npx eslint {fix_flag} {args} .",
        "ktlint":    f"ktlint {fix_flag} {args}",
        "swiftlint": f"swiftlint {fix_flag} {args}",
    }
    cmd = CMDS.get(tool, f"{tool} {args}")

    if not project_path or not os.path.isdir(project_path):
        return {"needs_human": True, "notes": f"project_path not found: {project_path}"}

    rc, out, err = await _run(cmd, cwd=project_path)
    if rc != 0:
        return {
            "needs_human": True,
            "notes": f"{tool} found issues (exit {rc})",
            "stdout": out[-3000:],
            "stderr": err[-1000:],
        }
    return {"tool": tool, "stdout": out, "status": "lint_passed"}


async def handle_npm_build(task: Task) -> dict:
    """
    payload:
      project_path: str   — repo root (must have package.json)
      script: str         — npm script to run (default: build)
      install: bool       — run npm ci first (default: false)
      timeout: int        — seconds (default: 600)
    """
    project_path = task.payload.get("project_path", "")
    script = task.payload.get("script", "build")
    install = task.payload.get("install", False)
    timeout = int(task.payload.get("timeout", 600))

    if not project_path or not os.path.isdir(project_path):
        return {"needs_human": True, "notes": f"project_path not found: {project_path}"}

    if install:
        rc, out, err = await asyncio.wait_for(_run("npm ci", cwd=project_path), timeout=120)
        if rc != 0:
            return {"needs_human": True, "notes": f"npm ci failed: {err[-1000:]}"}

    try:
        rc, out, err = await asyncio.wait_for(
            _run(f"npm run {shlex.quote(script)}", cwd=project_path),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {"needs_human": True, "notes": f"npm run {script} timed out after {timeout}s"}

    if rc != 0:
        return {
            "needs_human": True,
            "notes": f"npm run {script} failed (exit {rc})",
            "stdout": out[-3000:],
            "stderr": err[-2000:],
        }
    return {"script": script, "stdout": out[-2000:], "status": "build_success"}
