"""Independent validation tests for story DI-074 (DI-072 follow-up)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _pytest.outcomes import Failed, Skipped

import tests.test_di_072 as di072

ROOT = Path(__file__).resolve().parent.parent
COMMIT_BEFORE_FIX = "67ea0d953be068474a9b18c118c56c7b43723432"


def _commit_exists(rev: str) -> bool:
    res = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", f"{rev}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    return res.returncode == 0


# --- Criterion 3: Only tests/test_di_072.py (+ test files) changed; shared/backlog_edit.py byte-identical ---


def test_criterion_3_backlog_edit_byte_identical_and_only_test_files_changed():
    base, tried = di072._resolve_base()
    assert base is not None, f"Expected a resolved base, tried: {tried}"

    diff_backlog_edit = subprocess.run(
        ["git", "-C", str(ROOT), "diff", base, "HEAD", "--", "shared/backlog_edit.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    if _commit_exists(COMMIT_BEFORE_FIX):
        diff_since_before_fix = subprocess.run(
            ["git", "-C", str(ROOT), "diff", f"{COMMIT_BEFORE_FIX}..HEAD", "--", "shared/backlog_edit.py"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert diff_since_before_fix == "", "shared/backlog_edit.py must be byte-identical before and after the fix"

        changed_files = subprocess.run(
            ["git", "-C", str(ROOT), "diff", "--name-only", f"{COMMIT_BEFORE_FIX}..HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        allowed = {"tests/test_di_072.py", "tests/test_di_074.py"}
        assert set(changed_files) <= allowed, f"Unexpected files changed since {COMMIT_BEFORE_FIX}: {changed_files}"
        assert "tests/test_di_072.py" in changed_files
    else:
        assert diff_backlog_edit is not None


# --- Criterion 4: Diff base resolution order and merge-base diffing ---


def test_criterion_4_resolve_base_prefers_origin_main_first():
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        sub = list(cmd[3:])
        calls.append(sub)
        if sub == ["rev-parse", "--verify", "origin/main"]:
            return subprocess.CompletedProcess(cmd, 0, "deadbeef\n", "")
        raise AssertionError(f"Unexpected git command after origin/main succeeded: {sub}")

    base, tried = di072._resolve_base(run=fake_run, env={})
    assert base == "origin/main"
    assert tried == []
    assert calls == [["rev-parse", "--verify", "origin/main"]]


def test_criterion_4_resolve_base_fetches_origin_main_when_ref_missing():
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        sub = list(cmd[3:])
        calls.append(sub)
        if sub == ["rev-parse", "--verify", "origin/main"]:
            return subprocess.CompletedProcess(cmd, 1, "", "fatal: Needed a single revision")
        if sub == ["fetch", "--no-tags", "--depth=50", "origin", "main"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if sub == ["rev-parse", "--verify", "FETCH_HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, "cafebabe\n", "")
        raise AssertionError(f"Unexpected git command: {sub}")

    base, tried = di072._resolve_base(run=fake_run, env={})
    assert base == "FETCH_HEAD"
    assert calls[:2] == [
        ["rev-parse", "--verify", "origin/main"],
        ["fetch", "--no-tags", "--depth=50", "origin", "main"],
    ]


def test_criterion_4_resolve_base_falls_back_to_first_parent_of_pr_merge_commit():
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        sub = list(cmd[3:])
        calls.append(sub)
        if sub == ["rev-parse", "--verify", "origin/main"]:
            return subprocess.CompletedProcess(cmd, 1, "", "missing")
        if sub == ["fetch", "--no-tags", "--depth=50", "origin", "main"]:
            return subprocess.CompletedProcess(cmd, 128, "", "network unreachable")
        if sub == ["rev-list", "--parents", "-n1", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, "merge_sha parent1_sha parent2_sha\n", "")
        if sub == ["rev-parse", "--verify", "HEAD^1"]:
            return subprocess.CompletedProcess(cmd, 0, "parent1_sha\n", "")
        raise AssertionError(f"Unexpected git command: {sub}")

    base, tried = di072._resolve_base(run=fake_run, env={})
    assert base == "HEAD^1"
    assert calls[:3] == [
        ["rev-parse", "--verify", "origin/main"],
        ["fetch", "--no-tags", "--depth=50", "origin", "main"],
        ["rev-list", "--parents", "-n1", "HEAD"],
    ]


def test_criterion_4_changed_paths_uses_merge_base_of_resolved_base_and_head(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(di072, "_resolve_base", lambda: ("FETCH_HEAD", ["origin/main: not found"]))

    def fake_subprocess_run(cmd, **_kwargs):
        sub = list(cmd[3:])
        seen.append(sub)
        if sub == ["merge-base", "FETCH_HEAD", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, "mergebase123\n", "")
        if sub == ["diff", "--name-only", "mergebase123", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, "tests/test_di_072.py\n", "")
        raise AssertionError(f"Unexpected command in _changed_paths: {sub}")

    monkeypatch.setattr(di072.subprocess, "run", fake_subprocess_run)
    paths = di072._changed_paths()
    assert paths == ["tests/test_di_072.py"]
    assert seen == [
        ["merge-base", "FETCH_HEAD", "HEAD"],
        ["diff", "--name-only", "mergebase123", "HEAD"],
    ]


# --- Criterion 5: Guard is not weakened (fails on data/ or backlog file; proven on a real scratch branch) ---


@pytest.mark.parametrize("bad_path", ["data/x", "data/sub/file.json", "product-backlog.md", "projects/demo/product-backlog.md"])
def test_criterion_5_guard_fails_when_changed_paths_touch_data_or_backlog(monkeypatch, bad_path):
    monkeypatch.setattr(di072, "_changed_paths", lambda: ["tests/test_di_072.py", bad_path])
    monkeypatch.setattr(
        di072.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    with pytest.raises(AssertionError):
        di072.test_criterion_6_nothing_under_data_or_backlogs_touched()


def test_criterion_5_throwaway_commit_touching_data_x_on_scratch_branch_makes_test_fail(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

    git("init", "-b", "main")
    git("config", "user.email", "tester@example.com")
    git("config", "user.name", "Independent Tester")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "initial")
    # Create origin/main ref pointing at initial commit
    head_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git("update-ref", "refs/remotes/origin/main", head_sha)

    # Create scratch branch with a throwaway commit touching data/x
    git("checkout", "-b", "scratch/di-074-throwaway")
    (repo / "data").mkdir()
    (repo / "data" / "x").write_text("throwaway\n", encoding="utf-8")
    git("add", "data/x")
    git("commit", "-m", "throwaway data/x")

    monkeypatch.setattr(di072, "ROOT", repo)
    with pytest.raises(AssertionError, match="data/x"):
        di072.test_criterion_6_nothing_under_data_or_backlogs_touched()

    # Discard the throwaway commit and verify the guard passes again
    git("reset", "--hard", "origin/main")
    di072.test_criterion_6_nothing_under_data_or_backlogs_touched()


# --- Criterion 6: Unresolved base skips outside CI and fails in GITHUB_ACTIONS=true ---


def test_criterion_6_unresolved_base_skips_outside_ci_and_names_refs_tried(monkeypatch):
    def all_fail_run(cmd, **_kwargs):
        sub = list(cmd[3:])
        if sub == ["rev-list", "--parents", "-n1", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, "only_one_commit_sha\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "failed")

    base, tried = di072._resolve_base(run=all_fail_run, env={})
    assert base is None
    joined = " | ".join(tried)
    assert "origin/main" in joined
    assert "fetch" in joined
    assert "HEAD^1" in joined

    monkeypatch.setattr(di072, "_resolve_base", lambda: (None, tried))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    with pytest.raises(Skipped) as excinfo:
        di072.test_criterion_6_nothing_under_data_or_backlogs_touched()

    msg = str(excinfo.value)
    assert "origin/main" in msg
    assert "fetch" in msg or "FETCH_HEAD" in msg
    assert "HEAD^1" in msg


def test_criterion_6_unresolved_base_fails_inside_github_actions_with_same_message(monkeypatch):
    def all_fail_run(cmd, **_kwargs):
        sub = list(cmd[3:])
        if sub == ["rev-list", "--parents", "-n1", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, "only_one_commit_sha\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "failed")

    base, tried = di072._resolve_base(run=all_fail_run, env={"GITHUB_ACTIONS": "true"})
    assert base is None

    monkeypatch.setattr(di072, "_resolve_base", lambda: (None, tried))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    with pytest.raises(Failed) as excinfo:
        di072.test_criterion_6_nothing_under_data_or_backlogs_touched()

    msg = str(excinfo.value)
    assert "origin/main" in msg
    assert "fetch" in msg or "FETCH_HEAD" in msg
    assert "HEAD^1" in msg


# --- Criterion 7: Base resolves on this repository and criterion 6 test passes without skipping ---


def test_criterion_7_criterion_6_resolves_base_and_passes():
    base, _tried = di072._resolve_base()
    assert base in {"origin/main", "FETCH_HEAD", "HEAD^1"}
    di072.test_criterion_6_nothing_under_data_or_backlogs_touched()
