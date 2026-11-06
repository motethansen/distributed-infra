import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import llm_experiment as ex  # noqa: E402


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def fx(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "c1")
    d = tmp_path / "fix"
    d.mkdir()
    (d / "task.md").write_text("do the thing")
    (d / "fixture.json").write_text(json.dumps({"name": "f", "repo_url_or_path": str(repo),
                                               "pinned_commit": git(repo, "rev-parse", "HEAD"), "test_cmd": "true"}))
    return d, repo


class Fake:
    def __init__(self, out=None, rc=0, tests=(1, 0), help_text="--effort --strict-mcp-config --disable-slash-commands"):
        self.out = out if out is not None else json.dumps({"result": "ok", "num_turns": 7, "total_cost_usd": 0.5,
                                                           "usage": {"input_tokens": 10, "output_tokens": 5}})
        self.rc, self.tests, self.help_text, self.calls = rc, list(tests), help_text, []

    def run_agent(self, argv, cwd, timeout):
        self.calls.append(("agent", argv, Path(cwd)))
        assert Path(cwd).is_absolute() and Path(cwd).is_dir()
        return self.out, self.rc, 1.5

    def run_tests(self, test_cmd, cwd):
        self.calls.append(("tests", test_cmd, Path(cwd)))
        return self.tests.pop(0) if len(self.tests) > 1 else self.tests[0]

    def run_reviewer(self, diff, task_prompt, cwd):
        return "APPROVE"

    def cli_help(self):
        return self.help_text


def test_load_fixture_ok_and_missing(fx, tmp_path):
    d, _ = fx
    f = ex.load_fixture(d)
    assert f.prompt == "do the thing" and f.known_good_commit is None
    (d / "task.md").unlink()
    with pytest.raises(ex.FixtureError, match="task.md"):
        ex.load_fixture(d)
    with pytest.raises(ex.FixtureError):
        ex.load_fixture(tmp_path / "nope")


def test_gate_refuses_when_tests_pass(fx):
    f = ex.load_fixture(fx[0])
    with pytest.raises(ex.FixtureBrokenError, match="fixture is broken"):
        ex.check_fixture_fails_before(f, Fake(tests=[0]))
    ex.check_fixture_fails_before(f, Fake(tests=[1]))


def test_run_one_ok_and_worktree_removed(fx, tmp_path):
    f = ex.load_fixture(fx[0])
    base = tmp_path / "wd"
    r = ex.run_one(f, "lean", 1, Fake(tests=[0]), base)
    assert (r.status, r.tests_passed, r.turns, r.cost_usd, r.reviewer_verdict) == ("ok", True, 7, 0.5, "APPROVE")
    assert not [p for p in base.glob("wt-*")]
    assert "wt-" not in git(fx[1], "worktree", "list").split("\n", 1)[-1]


def test_run_one_budget_failover_error(fx, tmp_path):
    f = ex.load_fixture(fx[0])
    budget = json.dumps({"subtype": "error_max_budget_usd", "is_error": True, "result": "x", "total_cost_usd": 2.1})
    r = ex.run_one(f, "lean", 1, Fake(out=budget, rc=1), tmp_path / "w")
    assert r.status == "budget_exceeded" and r.tests_passed is False and r.cost_usd == 2.1
    r = ex.run_one(f, "lean", 1, Fake(out="Claude usage limit reached", rc=1), tmp_path / "w")
    assert r.status == "invalid_failover"
    r = ex.run_one(f, "lean", 1, Fake(out="boom", rc=1), tmp_path / "w")
    assert r.status == "error" and "boom" in r.error
    bad = ex.Fixture("f", "/nonexistent/repo", "abc", "true", "p")
    assert ex.run_one(bad, "lean", 1, Fake(), tmp_path / "w").status == "error"


def test_run_experiment_unsupported_and_reports(fx, tmp_path):
    f = ex.load_fixture(fx[0])
    fake = Fake(tests=[1, 0], help_text="--strict-mcp-config --disable-slash-commands")
    rep, out_dir = ex.run_experiment(f, ["lean_low_effort", "baseline"], 1, fake, tmp_path / "out", "2026-01-01")
    assert [r["variant"] for r in rep["runs"]] == ["baseline"]
    assert rep["unsupported"].keys() == {"lean_low_effort"}
    assert (out_dir / "report.json").exists() and (out_dir / "report.md").exists()
    assert out_dir == tmp_path / "out" / "data" / "experiments" / "2026-01-01"


def test_main_exits_nonzero_on_broken_fixture(fx, tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "SubprocessRunner", lambda: Fake(tests=[0]))
    assert ex.main(["--task", str(fx[0]), "--out-root", str(tmp_path)]) == 1
    assert ex.main(["--task", str(tmp_path / "missing")]) == 2


def test_parse_verdict():
    assert ex.parse_verdict("blah APPROVE\nREQUEST_CHANGES") == "REQUEST_CHANGES"
    assert ex.parse_verdict("nothing") == "UNKNOWN"


def test_install_test_file_copies_into_worktree(fx, tmp_path):
    d, repo = fx
    (d / "acceptance_test.py").write_text("def test_x(): pass\n")
    meta = json.loads((d / "fixture.json").read_text())
    meta.update(test_file="acceptance_test.py", test_cmd="python -m pytest test_acc.py -q")
    (d / "fixture.json").write_text(json.dumps(meta))
    fixture = ex.load_fixture(d)
    wt = tmp_path / "wt"
    wt.mkdir()
    ex.install_test_file(fixture, wt)
    assert (wt / "test_acc.py").read_text() == "def test_x(): pass\n"


def test_di020_fixture_is_well_formed():
    d = Path(__file__).resolve().parent / "fixtures" / "experiments" / "di020_cwd_expanduser"
    fixture = ex.load_fixture(d)
    assert len(fixture.pinned_commit) == 40 and len(fixture.known_good_commit) == 40
    assert fixture.pinned_commit != fixture.known_good_commit
    assert fixture.test_cmd == "python -m pytest test_di020_acceptance.py -q"
    assert (d / fixture.test_file).is_file()
