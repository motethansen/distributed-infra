"""scripts/check-secrets.sh scans the STAGED content, not the working files (needs gitleaks; skipped without it)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")

# Built at run time so this file itself is never a finding.
FAKE_KEY = "AKIA" + "ABCDEFGHIJKLMNOQ"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "check-secrets.sh", tmp_path / "scripts")
    shutil.copy(ROOT / ".gitleaks.toml", tmp_path)
    (tmp_path / "a.txt").write_text("clean\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def scan(repo, *args):
    return subprocess.run(["bash", str(repo / "scripts" / "check-secrets.sh"), *args],
                          cwd=repo, capture_output=True, text=True)


def test_clean_tree_passes(repo):
    assert scan(repo).returncode == 0


def test_staged_secret_is_found_even_if_the_working_file_is_cleaned(repo):
    (repo / "a.txt").write_text(f"aws_access_key_id = {FAKE_KEY}\n")
    git(repo, "add", "a.txt")
    (repo / "a.txt").write_text("clean again\n")          # unstaged edit hides it from a working-tree scan
    r = scan(repo)
    assert r.returncode != 0, r.stdout + r.stderr
    assert FAKE_KEY not in r.stdout + r.stderr             # redacted


def test_unstaged_secret_is_not_part_of_the_commit(repo):
    (repo / "a.txt").write_text(f"aws_access_key_id = {FAKE_KEY}\n")   # not staged
    assert scan(repo).returncode == 0
    assert scan(repo, "--worktree").returncode != 0
