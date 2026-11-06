"""DI-020 acceptance test: a `~` in a task's cwd must be expanded before the subprocess starts.

The experiment harness copies this file into the worktree root as test_di020_acceptance.py. It fails on the
pre-fix commit (asyncio.create_subprocess_shell gets the literal "~/..." cwd and raises FileNotFoundError)
and passes once the cwd is expanded.
"""
import asyncio
import os

import pytest

from shared.models import Task, TaskType
from worker.handlers import common


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _run(coro):
    return asyncio.run(coro)


def test_run_helper_expands_tilde_cwd(fake_home):
    rc, out, err = _run(common._run("pwd", cwd="~/proj"))
    assert rc == 0, err
    assert os.path.realpath(out.strip()) == os.path.realpath(str(fake_home / "proj"))


def test_run_script_handler_expands_tilde_cwd(fake_home):
    task = Task(type=TaskType.run_script, created_by="acceptance", payload={"script": "pwd", "cwd": "~/proj"})
    result = _run(common.handle_run_script(task))
    assert result.get("returncode") == 0, result
    assert os.path.realpath(result["stdout"].strip()) == os.path.realpath(str(fake_home / "proj"))


def test_absolute_and_missing_cwd_unchanged(fake_home, tmp_path):
    rc, out, _ = _run(common._run("pwd", cwd=str(fake_home / "proj")))
    assert rc == 0 and os.path.realpath(out.strip()) == os.path.realpath(str(fake_home / "proj"))
    rc, out, _ = _run(common._run("echo ok"))
    assert rc == 0 and out.strip() == "ok"
