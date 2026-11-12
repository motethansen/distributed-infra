"""DI-083: the CI secret-scan judges a pull request on its own commits (workflow, check-secrets.sh, docs)."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
SCAN = WF["jobs"]["secret-scan"]
SCAN_TEXT = yaml.safe_dump(SCAN)
# PyYAML parses the key `on` as True.
TRIGGERS = WF.get("on", WF.get(True))
needs_gitleaks = pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")

# Built at run time so this file itself is never a finding.
TAILNET = ".".join(["100", "101", "102", "103"])


def steps_text(pred):
    return "\n".join(s.get("run", "") for s in SCAN["steps"] if pred(s))


def test_triggers_include_pull_request_push_and_weekly_cron():
    assert "pull_request" in TRIGGERS and "push" in TRIGGERS
    crons = [c["cron"] for c in TRIGGERS["schedule"]]
    assert crons and all(len(c.split()) == 5 for c in crons)


def test_pr_scan_uses_merge_base_range_and_full_history():
    pr = [s for s in SCAN["steps"] if "pull_request" in str(s.get("if", "")) and "!=" not in str(s.get("if", ""))]
    assert pr, "no step restricted to pull_request events"
    run = "\n".join(s["run"] for s in pr)
    assert "merge-base" in run and "--log-opts" in run and ".." in run
    assert "gitleaks" in run
    checkout = [s for s in SCAN["steps"] if str(s.get("uses", "")).startswith("actions/checkout")][0]
    assert checkout["with"]["fetch-depth"] in (0, "0")


def test_pr_scan_does_not_scan_all_refs():
    pr = [s for s in SCAN["steps"] if "pull_request" in str(s.get("if", "")) and "!=" not in str(s.get("if", ""))]
    run = "\n".join(s["run"] for s in pr)
    assert "--all" not in run
    # every gitleaks detect invocation must carry --log-opts (a bare one scans every ref)
    for line in SCAN_TEXT.splitlines():
        pass
    for s in SCAN["steps"]:
        for ln in s.get("run", "").splitlines():
            if "gitleaks detect" in ln:
                assert "--log-opts" in ln, ln


def test_non_pr_scan_is_head_only_never_all_branches():
    run = steps_text(lambda s: "!= 'pull_request'" in str(s.get("if", "")))
    assert "gitleaks detect" in run and "HEAD" in run
    assert "--all" not in run and "--branches" not in run and "--remotes" not in run


def test_pushes_scanned_only_on_main():
    cond = str(SCAN.get("if", ""))
    assert "refs/heads/main" in cond and "push" in cond


def test_gitleaks_version_pin_stays():
    assert "8.30.1" in SCAN_TEXT


def test_test_job_unchanged_against_main():
    old = subprocess.run(["git", "-C", str(ROOT), "show", "origin/main:.github/workflows/ci.yml"],
                         capture_output=True, text=True)
    if old.returncode != 0:
        pytest.skip("origin/main not available")
    assert yaml.safe_load(old.stdout)["jobs"]["test"] == WF["jobs"]["test"]


def test_gitleaks_config_and_ignore_untouched_vs_main():
    r = subprocess.run(["git", "-C", str(ROOT), "diff", "--name-only", "origin/main...HEAD"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("origin/main not available")
    changed = r.stdout.split()
    assert ".gitleaks.toml" not in changed and ".gitleaksignore" not in changed


def test_check_secrets_history_still_covers_commits_and_documents_range():
    sh = (ROOT / "scripts" / "check-secrets.sh").read_text()
    assert "--history)" in sh and "--log-opts" in sh
    assert "--range" in sh
    head = "\n".join(sh.splitlines()[:14])
    assert "--range" in head and re.search(r"CI|pull request", head)


def test_docs_explain_pr_scope_main_full_and_finding_handling():
    full = (ROOT / "docs" / "SECURITY.md").read_text().lower()
    start = full.index("secret-scan` job scans") if "secret-scan` job scans" in full else 0
    end = full.find("also enable", start)
    doc = full[start:end if end > start else start + 1500]          # the CI section, not the older incident-response text
    assert "own commits" in doc or "commits of the pr" in doc
    assert "main" in doc and "placeholder" in doc or "<mac-mini-ip>" in doc
    assert "allowlist" in doc
    assert "rewrite" in doc, "docs must say never to rewrite public history without the owner"


# --- behaviour, with real gitleaks in throwaway repos -------------------------------------------------------

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, env=ENV).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "check-secrets.sh", tmp_path / "scripts")
    shutil.copy(ROOT / ".gitleaks.toml", tmp_path)
    (tmp_path / "a.txt").write_text("clean\n")
    git(tmp_path, "add", "-A"); git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def run_check(repo, *args):
    return subprocess.run(["bash", str(repo / "scripts" / "check-secrets.sh"), *args], cwd=repo, capture_output=True, text=True)


def branch_with(repo, name, content):
    git(repo, "checkout", "-q", "-b", name, "main")
    (repo / "x.txt").write_text(content)
    git(repo, "add", "x.txt"); git(repo, "commit", "-qm", name)
    git(repo, "checkout", "-q", "main")


@needs_gitleaks
def test_range_scan_ignores_finding_on_another_branch(repo):
    branch_with(repo, "other", f"addr {TAILNET}\n")
    branch_with(repo, "mine", "harmless\n")
    assert run_check(repo, "--range", "main..mine").returncode == 0
    assert run_check(repo, "--range", "main..other").returncode != 0


@needs_gitleaks
def test_range_scan_flags_tailnet_address_and_env_assignment(repo):
    branch_with(repo, "ip", f"host = {TAILNET}\n")
    assert run_check(repo, "--range", "main..ip").returncode != 0
    branch_with(repo, "kv", "SECRET_KEY=" + "q8Zt3vLp9XwR2mKd7Yb4NcHs\n")
    assert run_check(repo, "--range", "main..kv").returncode != 0


@needs_gitleaks
def test_history_covers_head_history_every_commit_but_not_other_branches(repo):
    branch_with(repo, "other", f"addr {TAILNET}\n")
    assert run_check(repo, "--history").returncode == 0           # other branch not reachable from HEAD
    (repo / "old.txt").write_text(f"addr {TAILNET}\n")
    git(repo, "add", "old.txt"); git(repo, "commit", "-qm", "leak")
    (repo / "old.txt").write_text("fixed\n")
    git(repo, "add", "old.txt"); git(repo, "commit", "-qm", "fix")
    assert run_check(repo, "--history").returncode != 0           # old commit still found: every commit
