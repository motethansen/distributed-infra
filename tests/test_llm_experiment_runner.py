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

    def run_reviewer(self, diff, task_prompt):
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
    assert not [p for p in base.iterdir()]
    assert git(fx[1], "worktree", "list").count("\n") == 0   # the source repo never got a linked worktree


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
    ex.install_acceptance_test(fixture, wt)
    assert (wt / "test_acc.py").read_text() == "def test_x(): pass\n"


def test_di020_fixture_is_well_formed():
    d = Path(__file__).resolve().parent / "fixtures" / "experiments" / "di020_cwd_expanduser"
    fixture = ex.load_fixture(d)
    assert len(fixture.pinned_commit) == 40 and len(fixture.known_good_commit) == 40
    assert fixture.pinned_commit != fixture.known_good_commit
    assert fixture.test_cmd == "python -m pytest test_di020_acceptance.py -q"
    assert (d / fixture.test_file).is_file()


# ── S-2 runner coverage ──────────────────────────────────────────────────────
class SeqRunner(Fake):
    """Fake whose agent outputs come from a script, one (out, rc) per run_agent call."""

    def __init__(self, outputs, **kw):
        super().__init__(**kw)
        self.outputs = list(outputs)

    def run_agent(self, argv, cwd, timeout):
        self.calls.append(("agent", argv, Path(cwd)))
        out, rc = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        return out, rc, 1.0


BUDGET_OUT = json.dumps({"subtype": "error_max_budget_usd", "is_error": True, "result": "x", "total_cost_usd": 2.1})


def agent_calls(fake):
    return [c for c in fake.calls if c[0] == "agent"]


def test_run_experiment_writes_reports_under_dated_dir(fx, tmp_path):
    f = ex.load_fixture(fx[0])
    rep, out_dir = ex.run_experiment(f, ["baseline", "lean"], 2, Fake(tests=[1, 0]), tmp_path / "root", "2026-02-03")
    assert out_dir == tmp_path / "root" / "data" / "experiments" / "2026-02-03"
    assert json.loads((out_dir / "report.json").read_text())["date"] == "2026-02-03"
    assert "# LLM flag experiment 2026-02-03" in (out_dir / "report.md").read_text()
    assert len(rep["runs"]) == 4


def test_run_experiment_refuses_when_tests_pass_before_agent(fx, tmp_path):
    f = ex.load_fixture(fx[0])
    fake = Fake(tests=[0])
    with pytest.raises(ex.FixtureBrokenError):
        ex.run_experiment(f, ["baseline"], 1, fake, tmp_path / "root", "2026-02-03")
    assert not agent_calls(fake)
    assert not (tmp_path / "root" / "data").exists()


def test_acceptance_test_installed_before_gate_and_runs(fx, tmp_path):
    d, _ = fx
    (d / "acc.py").write_text("def test_x(): pass\n")
    meta = json.loads((d / "fixture.json").read_text())
    meta.update(test_file="acc.py", test_dest="test_dest_name.py")
    (d / "fixture.json").write_text(json.dumps(meta))
    f = ex.load_fixture(d)
    assert f.test_dest == "test_dest_name.py"
    seen = []

    class Checking(Fake):
        def run_tests(self, test_cmd, cwd):
            seen.append((Path(cwd) / "test_dest_name.py").is_file())
            return super().run_tests(test_cmd, cwd)

    ex.run_experiment(f, ["baseline"], 1, Checking(tests=[1, 0]), tmp_path / "root", "2026-02-03")
    assert seen == [True, True]  # pre-agent gate and the post-agent run


def test_fixture_test_dest_defaults(fx):
    f = ex.load_fixture(fx[0])  # no test_file, test_cmd names no test_*.py
    assert f.test_file is None and f.test_dest == "test_di020_acceptance.py"


def test_budget_exceeded_is_recorded_and_later_runs_execute(fx, tmp_path):
    f = ex.load_fixture(fx[0])
    ok = json.dumps({"result": "ok", "num_turns": 3, "total_cost_usd": 0.4})
    fake = SeqRunner([(BUDGET_OUT, 1), (ok, 0)], tests=[1, 0])
    rep, _ = ex.run_experiment(f, ["baseline"], 3, fake, tmp_path / "root", "2026-02-03")
    assert [r["status"] for r in rep["runs"]] == ["budget_exceeded", "ok", "ok"]
    assert len(agent_calls(fake)) == 3
    assert rep["summaries"]["baseline"]["tests_passed_count"] == 2


def test_usage_limit_failover_is_invalid_and_excluded_from_medians(fx, tmp_path):
    f = ex.load_fixture(fx[0])
    cheap = json.dumps({"result": "ok", "num_turns": 3, "total_cost_usd": 1.0})
    fake = SeqRunner([("Claude usage limit reached", 1), (cheap, 0)], tests=[1, 0])
    rep, _ = ex.run_experiment(f, ["baseline"], 3, fake, tmp_path / "root", "2026-02-03")
    assert rep["runs"][0]["status"] == "invalid_failover"
    s = rep["summaries"]["baseline"]
    assert (s["n_valid"], s["n_excluded"]) == (2, 1)
    assert s["cost_usd"]["median"] == 1.0 and s["cost_usd"]["min"] == 1.0  # the failed run's 0.0 is not in there


def test_each_run_gets_a_fresh_absolute_worktree(fx, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = ex.load_fixture(fx[0])
    fake = Fake(tests=[1, 0])
    base = Path("~/wd")  # unexpanded on purpose
    for i in (1, 2):
        ex.run_one(f, "baseline", i, fake, base)
    paths = [c[2] for c in agent_calls(fake)]
    assert len(set(paths)) == 2
    for p in paths:
        assert p.is_absolute() and "~" not in str(p)
        assert str(p).startswith(str(tmp_path))
    assert not list((tmp_path / "wd").iterdir())  # removed afterwards


def test_make_snapshot_repo_expands_user_and_is_absolute(fx, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    d, repo = fx
    sb = ex.make_snapshot_repo(repo, git(repo, "rev-parse", "HEAD"), "~/base")
    try:
        assert sb.path.is_absolute() and "~" not in str(sb.path) and (sb.path / "a.txt").is_file()
        assert sb.path.parent == tmp_path / "base"
    finally:
        ex.remove_sandbox(sb)


def test_cli_without_effort_marks_low_effort_unsupported_and_still_runs_others(fx, tmp_path):
    f = ex.load_fixture(fx[0])
    fake = Fake(tests=[1, 0], help_text="--strict-mcp-config --disable-slash-commands")
    rep, out_dir = ex.run_experiment(f, ["baseline", "lean", "lean_low_effort"], 1, fake, tmp_path / "root", "2026-02-03")
    assert sorted({r["variant"] for r in rep["runs"]}) == ["baseline", "lean"]
    assert rep["verdicts"]["lean_low_effort"] == "UNSUPPORTED"
    assert "--effort" in rep["unsupported"]["lean_low_effort"]
    assert "unsupported" in (out_dir / "report.md").read_text()
    assert not any("--effort" in c[1] for c in agent_calls(fake))


def _runs(variant, costs, passed):
    return [ex.RunResult(variant, i + 1, cost_usd=c, tests_passed=p) for i, (c, p) in enumerate(zip(costs, passed))]


def _table(lean_passed):
    runs = _runs("baseline", [2.0, 2.0, 2.0], [True] * 3) + _runs("lean", [1.0, 1.0, 1.0], lean_passed)
    return ex.build_report_dict("2026-02-03", "f", "abc", 3, runs)["verdicts"]


def test_all_pass_and_cheaper_is_safe():
    v = _table([True, True, True])
    assert v["lean"] == "SAFE" and v["baseline"] == "BASELINE"


def test_two_of_three_passes_is_not_safe():
    assert _table([True, True, False])["lean"] == "NOT SAFE"


def test_all_pass_but_not_cheaper_is_not_safe():
    runs = _runs("baseline", [1.0] * 3, [True] * 3) + _runs("lean", [1.0] * 3, [True] * 3)
    assert ex.build_report_dict("d", "f", "abc", 3, runs)["verdicts"]["lean"] == "NOT SAFE"
