"""DI-080: accept/close a story whose run never reached review, only with --from-in-progress and evidence."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from shared import scrum_flow as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import dis_agent as da  # noqa: E402
import desk_worker as dw  # noqa: E402

STATUS = {"planned": "🔵 planned", "in progress": "🏃 in progress", "in review": "🔍 in review",
          "blocked": "🚧 blocked", "cancelled": "❌ cancelled 2026-11-01", "done": "✅ done 2026-11-01"}
EV = "https://github.com/o/r/pull/9 merged 2026-11-12"


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch):
    """Never depend on a developer's .env or the real environment: scrum_cli.client() reads SECRET_KEY from the
    environment first (a .env only fills gaps), so a throwaway value here keeps these tests hermetic."""
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("ORCHESTRATOR_URLS", "http://localhost:1")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@t")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    (work / "demo").mkdir()
    return work


def _set(work: Path, status: str):
    (work / "demo" / "product-backlog.md").write_text(
        "# Demo\n\n## Open items\n\n| ID | Story | Priority | Status | Evidence |\n|---|---|---|---|---|\n"
        f"| DM-001 | First | High | {status} | |\n")


def _close(work, evidence=EV, **kw):
    return sf.close_story(work, "demo", "DM-001", "michael", "2026-11-12", evidence, run=_norun, **kw)


def _norun(*a, **k):
    return subprocess.CompletedProcess(a, 0, "", "")


def _row(work):
    return next(l for l in (work / "demo" / "product-backlog.md").read_text().split("\n") if l.startswith("| DM-001"))


@pytest.mark.parametrize("name", list(STATUS))
def test_without_the_flag_only_in_review_closes(repo, name):
    _set(repo, STATUS[name])
    if name == "in review":
        _close(repo)
        assert "✅ done 2026-11-12" in _row(repo)
    else:
        with pytest.raises(sf.PlanError, match="is not in review; only a story whose work is finished and under review can be closed"):
            _close(repo)


@pytest.mark.parametrize("name,msg", [("planned", "is planned"), ("in review", "already in review"), ("blocked", "is blocked"),
                                      ("cancelled", "is cancelled"), ("done", "already closed")])
def test_with_the_flag_every_other_status_is_refused_distinctly(repo, name, msg):
    _set(repo, STATUS[name])
    with pytest.raises(sf.PlanError, match=msg):
        _close(repo, from_in_progress=True)


def test_unknown_status_is_refused_with_the_flag(repo):
    _set(repo, "🤷 whatever")
    with pytest.raises(sf.PlanError, match="not 'in progress'"):
        _close(repo, from_in_progress=True)


def test_with_the_flag_in_progress_closes_and_records_the_wording(repo):
    _set(repo, STATUS["in progress"])
    _close(repo, from_in_progress=True)
    row = _row(repo)
    assert "✅ done 2026-11-12" in row
    assert "accepted from in progress, run never reached review: " + EV in row and "confirmed by michael" in row


@pytest.mark.parametrize("ev", ["", "   ", None])
def test_with_the_flag_evidence_is_required(repo, ev):
    _set(repo, STATUS["in progress"])
    with pytest.raises(sf.PlanError, match="needs evidence"):
        _close(repo, ev, from_in_progress=True)
    assert "in progress" in _row(repo)


def test_cli_refuses_from_in_progress_without_pr_or_evidence(monkeypatch):
    monkeypatch.setattr(da.sf, "close_story", lambda *a, **k: pytest.fail("must not be called"))
    a = argparse.Namespace(project="demo", story="DM-001", pr=None, evidence=" ", yes=True, from_in_progress=True)
    with pytest.raises(SystemExit, match="needs evidence"):
        da.cmd_accept(a)


def test_cli_passes_the_flag_and_continues_when_the_fleet_is_unreachable(monkeypatch, capsys):
    got = []
    monkeypatch.setattr(da.sf, "close_story", lambda *a, **k: got.append(k) or {"committed": True, "pushed": True, "push_error": None})
    monkeypatch.setattr(da.sf, "find_sprint_of", lambda *a: None)
    monkeypatch.setattr(da, "viz_repo", lambda: Path("/nonexistent"))
    monkeypatch.setattr(da, "_report", lambda *a, **k: None)

    class Down:
        def _call(self, *a, **k):
            raise ConnectionError("no orchestrator")
    monkeypatch.setattr(da.sc, "client", lambda: Down())
    da.cmd_accept(argparse.Namespace(project="demo", story="DM-001", pr=None, evidence="checked live", yes=True, from_in_progress=True))
    assert got == [{"from_in_progress": True}]
    assert "could not check the fleet" in capsys.readouterr().out


def test_cli_warns_when_a_task_is_still_running(monkeypatch, capsys):
    class C:
        def _call(self, *a, **k):
            return [{"id": "abcdef123", "status": "running", "payload": {"project": "demo", "story": "DM-001"}}]
    monkeypatch.setattr(da.sc, "client", lambda: C())
    da._warn_running_fleet_task("demo", {"DM-001"})
    assert "WARNING" in capsys.readouterr().out


def test_help_text_and_flags_on_both_subcommands():
    for sub in ("accept", "close"):
        p = subprocess.run([sys.executable, str(ROOT / "scripts" / "dis_agent.py"), sub, "--help"], capture_output=True, text=True, cwd=ROOT)
        assert "--from-in-progress" in p.stdout and "needs --pr or --evidence" in " ".join(p.stdout.split())


def test_desk_worker_never_passes_the_flag_and_the_gate_still_refuses_in_progress(tmp_path, repo):
    seen = []
    cfg = dw.Config("http://orch.invalid", "s", tmp_path, tmp_path / "data")
    (tmp_path / "data").mkdir()
    def run(argv, timeout):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "not accepted")
    with pytest.raises(dw.CommandError):
        dw._run_internal({"verb": "accept_confirmed", "project": "demo", "story_id": "DM-001",
                          "evidence": "https://github.com/o/r/pull/9"}, cfg, run)
    assert seen and all("--from-in-progress" not in x for x in seen[0])
    _set(repo, STATUS["in progress"])
    with pytest.raises(sf.PlanError, match="not in review"):
        _close(repo)                                          # what `dis-agent accept --pr ... --yes` calls, without the flag
