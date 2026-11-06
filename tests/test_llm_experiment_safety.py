"""Safety and honesty of the experiment harness (PR #53 review): env allowlist, independent repo, test tampering,
preflight, reviewer routing, diff, report sanitising. Fakes only: no model is called and no money is spent."""
import asyncio
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import llm_experiment as ex  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_venv_walk(monkeypatch):
    """Keep the suite fast and off the real venv: the interpreter manifest is empty unless a test injects roots."""
    monkeypatch.setattr(ex, "interpreter_roots", lambda: [])

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
ACCEPT = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
SECRET = "sk-ant-api03-ABCDEFGH12345678"
OK_JSON = json.dumps({"result": "ok", "num_turns": 3, "total_cost_usd": 0.5,
                      "usage": {"input_tokens": 1, "output_tokens": 1}})


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


def commit_dir(d):
    """Make the fixture directory a committed git repo (the oracle is frozen from committed blobs only)."""
    for a in (("init", "-q"), ("config", "user.email", "t@example.com"), ("config", "user.name", "t"),
              ("add", "-A"), ("commit", "-qm", "fixture")):
        subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)


@pytest.fixture
def world(tmp_path):
    """A source repo with a buggy pinned commit and a fixed known-good commit, plus a fixture dir."""
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "calc.py").write_text(BUGGY)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "pinned")
    pinned = git(repo, "rev-parse", "HEAD")
    (repo / "calc.py").write_text(FIXED)
    git(repo, "commit", "-qam", "THE-SECRET-FIX-TEXT")
    good = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", pinned)
    d = tmp_path / "fix"
    d.mkdir()
    (d / "task.md").write_text("fix add")
    (d / "acc.py").write_text(ACCEPT)
    (d / "fixture.json").write_text(json.dumps({
        "name": "f", "repo_url_or_path": str(repo), "pinned_commit": pinned, "known_good_commit": good,
        "test_file": "acc.py", "test_dest": "test_acc.py", "test_cmd": "python -m pytest test_acc.py -q"}))
    commit_dir(d)
    return ex.load_fixture(d), repo, pinned, good


class Real(ex.SubprocessRunner):
    """Real tests/env handling; the agent and reviewer are scripted."""

    def __init__(self, agent=None, verdict="APPROVE"):
        super().__init__(cli="claude")
        self.agent, self.verdict, self.diffs = agent, verdict, []

    def run_agent(self, argv, cwd, timeout):
        if self.agent:
            self.agent(Path(cwd))
        return OK_JSON, 0, 1.0

    def run_reviewer(self, diff, task_prompt):
        self.diffs.append(diff)
        self.reviewer_info = {"agent": "codex", "model": "m"}
        return self.verdict

    def cli_help(self):
        return "--effort --strict-mcp-config --disable-slash-commands"


# ── P1-1 environment ─────────────────────────────────────────────────────────
# Poison values are BUILT at runtime, never written as literals: a literal that looks like a token trips the repo's secret scan,
# and the point of the test is only that no environment VALUE from the parent reaches the agent.
_POISON_KEYS = ["ORCHESTRATOR_URL", "RQLITE_URL", "SECRET_KEY", "OPENAI_API", "GITHUB_TOKEN", "DESK_TOKEN",
                "DEEPSEEK_API_KEY", "CF_API_TOKEN", "STRIPE_SECRET_KEY", "QUEUE_DB", "ANTHROPIC_API_KEY"]
POISON = {k: "-".join(["poison", str(i), k.lower().replace("_", "")]) for i, k in enumerate(_POISON_KEYS)}


def test_env_builders_use_an_explicit_allowlist(monkeypatch, tmp_path):
    for k, v in POISON.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PATH", "/bin")
    a, t = ex.agent_env(), ex.test_env(tmp_path)
    assert set(a) <= set(ex.AGENT_ENV_KEYS) and not set(a) & set(POISON)
    assert set(t) <= set(ex.TEST_ENV_KEYS) | {"HOME", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE"}
    assert t["HOME"] == str(tmp_path) and not set(t) & set(POISON)


def test_agent_child_sees_no_poisoned_environment(monkeypatch, tmp_path):
    cli = tmp_path / "fakeclaude"
    cli.write_text(f"#!{sys.executable}\nimport json, os\njson.dump(dict(os.environ), open('env.json', 'w'))\n"
                   "print('{}')\n")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    for k, v in POISON.items():
        monkeypatch.setenv(k, v)
    work = tmp_path / "w"
    work.mkdir()
    ex.SubprocessRunner(cli=str(cli)).run_agent(["claude", "-p", "x"], work, 60)
    seen = json.loads((work / "env.json").read_text())
    assert not set(seen) & set(POISON)
    assert not [v for v in seen.values() if "poison" in v or "fake-" in v]


def test_acceptance_tests_child_sees_no_poisoned_environment(monkeypatch, tmp_path):
    for k, v in POISON.items():
        monkeypatch.setenv(k, v)
    real_home = os.path.expanduser("~")
    (tmp_path / "test_env.py").write_text(
        "import os, json\n\n\ndef test_env():\n    json.dump(dict(os.environ), open('seen.json', 'w'))\n")
    rc = ex.SubprocessRunner(cli="claude").run_tests("python -m pytest test_env.py -q", tmp_path)
    assert rc == 0
    seen = json.loads((tmp_path / "seen.json").read_text())
    assert not set(seen) & set(POISON)
    assert seen["HOME"] != real_home


def test_same_allowlist_for_every_variant(world, tmp_path, monkeypatch):
    fixture = world[0]
    envs = []
    monkeypatch.setattr(ex.subprocess, "run", lambda *a, **k: envs.append(k.get("env")) or
                        subprocess.CompletedProcess(a, 0, stdout="{}", stderr=""))
    r = ex.SubprocessRunner(cli="claude")
    for v in ex.VARIANTS:
        r.run_agent(ex.build_command(v, "p"), tmp_path, 5)
    assert envs and all(e == envs[0] for e in envs)


# ── P1-2 independent repository ──────────────────────────────────────────────
def test_snapshot_repo_is_independent(world, tmp_path):
    fixture, repo, pinned, good = world
    sb = ex.make_snapshot_repo(repo, pinned, tmp_path / "b", fixture)
    log = git(sb.path, "log", "--all", "--format=%H")
    assert len(log.splitlines()) == 1 and log.strip() != pinned
    assert git(sb.path, "remote", "-v") == ""
    assert git(sb.path, "branch", "-a", "--format=%(refname)") == "refs/heads/main"
    assert git(sb.path, "worktree", "list").count("\n") == 0
    assert subprocess.run(["git", "-C", str(sb.path), "cat-file", "-e", good]).returncode != 0
    assert "THE-SECRET-FIX-TEXT" not in git(sb.path, "log", "--all", "--format=%B")
    assert (sb.path / "calc.py").read_text() == BUGGY       # the fix is not in the tree either
    assert (sb.path / ".git").is_dir()                       # a real dir, not a `gitdir:` file
    needles = [str(repo).encode(), str(repo.resolve()).encode()]
    for dp, _, fns in os.walk(sb.path):
        for fn in fns:
            blob = (Path(dp) / fn).read_bytes()
            assert not any(n in blob for n in needles), f"{dp}/{fn} references the source repo"
    assert sb.base == log.strip()


# ── P1-3 tampering ───────────────────────────────────────────────────────────
def run_tampered(world, tmp_path, agent):
    fixture = world[0]
    return ex.run_one(fixture, "baseline", 1, Real(agent), tmp_path / "wd")


def test_honest_fix_passes_untampered(world, tmp_path):
    r = run_tampered(world, tmp_path, lambda w: (w / "calc.py").write_text(FIXED))
    assert (r.tests_passed, r.tampered, r.status) == (True, False, "ok")


def test_agent_that_weakens_acceptance_test_fails_and_is_tampered(world, tmp_path):
    r = run_tampered(world, tmp_path, lambda w: (w / "test_acc.py").write_text("def test_add():\n    pass\n"))
    assert r.tests_passed is False and r.tampered is True and "test_acc.py" in r.tampered_files


@pytest.mark.parametrize("name,body", [
    ("conftest.py", "import pytest\n\n\ndef pytest_collection_modifyitems(items):\n    items.clear()\n"),
    ("pytest.ini", "[pytest]\naddopts = --deselect test_acc.py::test_add\n"),
    ("sitecustomize.py", "import os\nos._exit(0)\n"),
    ("setup.cfg", "[tool:pytest]\naddopts = -k nothing\n")])
def test_added_pytest_config_is_removed_and_counts_as_tampering(world, tmp_path, name, body):
    r = run_tampered(world, tmp_path, lambda w: (w / name).write_text(body))
    assert r.tampered is True and r.tests_passed is False and name in r.tampered_files


def test_agent_edit_hidden_by_pycache_or_symlink_still_fails(world, tmp_path):
    def agent(w):
        (w / "test_acc.py").unlink()
        (w / "test_acc.py").symlink_to(w / "calc.py")
    r = run_tampered(world, tmp_path, agent)
    assert r.tampered is True and r.tests_passed is False


def test_pytest_runs_with_trusted_config_and_no_cache(tmp_path):
    ini = tmp_path / "t.ini"
    argv = ex.resolve_test_argv("python -m pytest test_acc.py -q", tmp_path, python="/venv/py", ini=ini)
    assert argv[0] == "/venv/py"
    assert argv[argv.index("-c") + 1] == str(ini) and argv[argv.index("--rootdir") + 1] == str(tmp_path)
    assert "no:cacheprovider" in argv


def test_correct_fix_plus_added_config_is_still_a_fail(world, tmp_path):
    """Tests pass once the config is restored, but the run tampered: it must not count as a pass."""
    def agent(w):
        (w / "calc.py").write_text(FIXED)
        (w / "conftest.py").write_text("# harmless\n")
    r = run_tampered(world, tmp_path, agent)
    assert r.tampered is True and r.tests_passed is False and r.status == "ok"


def test_tampered_run_is_not_safe(world):
    runs = [ex.RunResult("baseline", i, cost_usd=2.0, tests_passed=True) for i in (1, 2, 3)]
    runs += [ex.RunResult("lean", i, cost_usd=1.0, tests_passed=(i != 3), tampered=(i == 3)) for i in (1, 2, 3)]
    assert ex.build_report_dict("d", "f", "abc", 3, runs)["verdicts"]["lean"] == "NOT SAFE"


# ── P2-4 preflight ───────────────────────────────────────────────────────────
class CodeRunner(Real):
    def __init__(self, codes):
        super().__init__()
        self.codes, self.cwds = list(codes), []

    def run_tests(self, test_cmd, cwd):
        self.cwds.append(Path(cwd))
        return self.codes.pop(0)


def test_preflight_real_runner_accepts_good_fixture(world):
    ex.check_fixture_fails_before(world[0], Real())


@pytest.mark.parametrize("code", [2, 3, 4, 5, 127])
def test_preflight_aborts_on_infrastructure_exit_codes(world, code):
    with pytest.raises(ex.FixtureInfraError, match="not 1"):
        ex.check_fixture_fails_before(world[0], CodeRunner([code]))


def test_preflight_aborts_when_known_good_fails(world):
    with pytest.raises(ex.FixtureInfraError, match="known_good_commit"):
        ex.check_fixture_fails_before(world[0], CodeRunner([1, 1]))
    with pytest.raises(ex.FixtureInfraError, match="known_good_commit"):
        ex.check_fixture_fails_before(world[0], CodeRunner([1, 2]))


def test_preflight_checks_the_known_good_snapshot_and_spends_nothing(world, tmp_path):
    fixture = world[0]
    fake = CodeRunner([1, 0])
    ex.check_fixture_fails_before(fixture, fake)
    assert len(fake.cwds) == 2
    fake = CodeRunner([2])
    with pytest.raises(ex.FixtureInfraError):
        ex.run_experiment(fixture, ["baseline"], 1, fake, tmp_path / "out", "2026-01-01")
    assert not (tmp_path / "out").exists()


def test_preflight_pass_before_is_a_broken_fixture(world):
    with pytest.raises(ex.FixtureBrokenError):
        ex.check_fixture_fails_before(world[0], CodeRunner([0]))


def test_runner_uses_the_harness_interpreter(tmp_path):
    seen = []
    orig = ex.subprocess.run
    ex.subprocess.run = lambda argv, **k: seen.append(argv) or subprocess.CompletedProcess(argv, 0)
    try:
        ex.SubprocessRunner(cli="claude").run_tests("python -m pytest x.py", tmp_path)
    finally:
        ex.subprocess.run = orig
    assert seen[0][0] == sys.executable


# ── P2-5 reviewer ────────────────────────────────────────────────────────────
class FakeFleet:
    def __init__(self, result=None, status="done", boom=None):
        self.result, self.status, self.boom, self.payloads = result, status, boom, []

    def queue(self, payload, notes):
        if self.boom:
            raise self.boom
        self.payloads.append(payload)
        return {"id": "t1"}

    def wait(self, ids, timeout=0):
        return {"t1": {"id": "t1", "status": self.status, "result": self.result}}


CFG = {"agent": "codex", "model": None}


def test_reviewer_routes_through_the_reviewer_role_with_failover_off():
    fleet = FakeFleet({"response": "looks fine\nAPPROVE", "answered_by": "codex", "answered_model": "gpt-x"})
    r = ex.SubprocessRunner(cli="claude", fleet=fleet, reviewer_cfg=CFG)
    assert r.run_reviewer("diff --git a b\n+x", "task") == "APPROVE"
    p = fleet.payloads[0]
    assert p["role"] == "reviewer" and p["agent"] == "codex" and p["failover"] is False
    assert "diff --git" in p["prompt"] and "untrusted" in p["prompt"]
    assert r.reviewer_info["agent"] == "codex" and r.reviewer_info["model"] == "gpt-x"
    assert "sha256:" in r.reviewer_info["command"] and "diff --git" not in r.reviewer_info["command"]


@pytest.mark.parametrize("fleet", [
    FakeFleet({"response": "APPROVE", "answered_by": "claude"}),       # silently the author's family
    FakeFleet({"response": "APPROVE", "answered_by": "codex"}, status="failed"),
    FakeFleet({"response": "APPROVE"}),                                  # nobody says who answered
    FakeFleet(boom=RuntimeError("no orchestrator reachable"))])
def test_reviewer_unavailable_never_falls_back(fleet):
    r = ex.SubprocessRunner(cli="claude", fleet=fleet, reviewer_cfg=CFG)
    assert r.run_reviewer("d", "t") == "unavailable"


def test_report_records_reviewer_and_cost_caveat(world, tmp_path):
    fake = Real()
    fake.reviewer_info = {"agent": "codex", "model": "m", "command": "agent_run role=reviewer"}
    rep, out = ex.run_experiment(world[0], ["baseline"], 1, fake, tmp_path / "o", "2026-01-01")
    assert rep["reviewer"]["agent"] == "codex" and "outside the Claude cost caps" in rep["reviewer"]["note"]
    assert rep["runs"][0]["reviewer_agent"] == "codex"
    md = (out / "report.md").read_text()
    assert "## Reviewer" in md and "codex" in md and "outside the Claude cost caps" in md


def test_reviewer_not_claude_sonnet_literal():
    src = Path(ex.__file__).read_text()
    assert '"sonnet"' not in src and "--model" not in src.split("def run_reviewer")[1].split("def cli_help")[0]


# ── P2-6 diff ────────────────────────────────────────────────────────────────
def diff_of(world, tmp_path, agent):
    fixture, repo, pinned, _ = world
    sb = ex.make_snapshot_repo(repo, pinned, tmp_path / "b", fixture)
    agent(sb.path)
    return ex.compute_diff(sb, tmp_path)


def test_diff_includes_untracked_new_files(world, tmp_path):
    d = diff_of(world, tmp_path, lambda w: (w / "new_file.py").write_text("print('hi')\n"))
    assert "new_file.py" in d and "+print('hi')" in d


def test_diff_survives_an_agent_that_committed(world, tmp_path):
    def agent(w):
        (w / "calc.py").write_text(FIXED)
        (w / "other.py").write_text("x = 1\n")
        git(w, "add", "-A")
        git(w, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "mine")
    d = diff_of(world, tmp_path, agent)
    assert "+    return a + b" in d and "other.py" in d
    assert diff_of(world, tmp_path, lambda w: None) == ""


def test_diff_survives_a_rewritten_history_and_does_not_run_agent_git_config(world, tmp_path):
    marker = tmp_path / "pwned"

    def agent(w):
        (w / ".gitattributes").write_text("* filter=evil\n")
        with open(w / ".git" / "config", "a") as f:
            f.write(f"[filter \"evil\"]\n\tclean = touch {marker}\n[core]\n\tfsmonitor = touch {marker}\n")
        hooks = w / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        (hooks / "pre-commit").write_text(f"#!/bin/sh\ntouch {marker}\n")
        (w / "x.py").write_text("1\n")
    d = diff_of(world, tmp_path, agent)
    assert "x.py" in d and not marker.exists()


def test_diff_ignores_nested_git_dirs(world, tmp_path):
    def agent(w):
        (w / "sub" / ".git").mkdir(parents=True)
        (w / "sub" / ".git" / "config").write_text("[core]\n")
        (w / "sub" / "m.py").write_text("1\n")
    d = diff_of(world, tmp_path, agent)
    assert "sub/m.py" in d and ".git/" not in d


def test_diff_failure_is_a_review_failure_not_an_empty_pass(world, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("git exploded")
    monkeypatch.setattr(ex, "compute_diff", boom)
    fake = Real(lambda w: (w / "calc.py").write_text(FIXED))
    r = ex.run_one(world[0], "baseline", 1, fake, tmp_path / "wd")
    assert r.reviewer_verdict == "diff_failed" and "diff generation failed" in r.error
    assert fake.diffs == []                       # the reviewer was never handed an empty diff


def test_reviewer_gets_the_agents_diff_in_a_run(world, tmp_path):
    fake = Real(lambda w: ((w / "calc.py").write_text(FIXED), (w / "added.txt").write_text("a")))
    ex.run_one(world[0], "baseline", 1, fake, tmp_path / "wd")
    assert "added.txt" in fake.diffs[0] and "return a + b" in fake.diffs[0]


# ── P2-7 reports ─────────────────────────────────────────────────────────────
def test_sanitize_redacts_tokens_paths_and_truncates(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "envvalue-123456")
    home = os.path.expanduser("~")
    raw = (f"key {SECRET} gh ghp_abcdefghijklmnop Authorization: Bearer abc.def-123456 "
           f"hex {'a1b2c3d4' * 8} b64 {'Zm9vYmFy' * 8} env envvalue-123456 at {home}/Projects/x and /tmp/foo "
           f"password=hunter2hunter2")
    out = ex.sanitize(raw, limit=10000)
    for bad in (SECRET, "ghp_abcdefghijklmnop", "abc.def-123456", "a1b2c3d4" * 8, "Zm9vYmFy" * 8, "envvalue-123456",
                home, "hunter2hunter2", "/tmp/foo"):
        assert bad not in out, bad
    long = "word " * 1000
    other = ex.sanitize("/Users/someoneelse/Projects/x /home/ci/work /private/var/folders/ab/cd/T/pytest-1/x")
    assert "someoneelse" not in other and "/home/ci" not in other and "folders" not in other
    assert len(ex.sanitize(long)) < 400 and "truncated" in ex.sanitize(long)


def test_planted_secret_and_path_in_agent_output_never_reach_reports(world, tmp_path):
    home = os.path.expanduser("~")
    leak = f"crash: {SECRET} at {home}/private/stuff and {tmp_path}/wd/repo-1 Bearer tok123456789"
    fake = Real()
    fake.run_agent = lambda argv, cwd, timeout: (leak, 1, 1.0)
    rep, out = ex.run_experiment(world[0], ["baseline"], 1, fake, tmp_path / "o", "2026-01-01")
    text = (out / "report.json").read_text() + (out / "report.md").read_text()
    for bad in (SECRET, home, str(tmp_path), "tok123456789"):
        assert bad not in text, bad
    assert rep["runs"][0]["status"] == "error"


def test_run_one_error_and_report_each_sanitise_on_their_own(world, tmp_path):
    fake = Real()
    fake.run_agent = lambda argv, cwd, timeout: (f"boom {SECRET}", 1, 1.0)
    r = ex.run_one(world[0], "baseline", 1, fake, tmp_path / "wd")
    assert SECRET not in r.error                       # sanitised where it is recorded
    raw = ex.RunResult("baseline", 1, error=f"x {SECRET} {os.path.expanduser('~')}/p", status="error")
    rep = ex.build_report_dict("d", "f", "abc", 1, [raw])
    assert SECRET not in json.dumps(rep) and os.path.expanduser("~") not in json.dumps(rep)  # and where it is reported


def test_exception_text_is_sanitised(world, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(f"cannot open {os.path.expanduser('~')}/x with {SECRET}")
    monkeypatch.setattr(ex, "make_snapshot_repo", boom)
    r = ex.run_one(world[0], "baseline", 1, Real(), tmp_path / "wd")
    assert SECRET not in r.error and os.path.expanduser("~") not in r.error


def test_report_commands_show_exact_flags_and_hashed_prompt(world, tmp_path):
    fixture = world[0]
    fake = Real()
    fake.cli = "/opt/tools/claude"
    rep, out = ex.run_experiment(fixture, ["baseline", "lean_low_effort"], 1, fake, tmp_path / "o", "2026-01-01")
    cmd = rep["commands"]["baseline"]
    assert cmd.startswith("/opt/tools/claude -p ")
    assert "--dangerously-skip-permissions --output-format json --max-budget-usd 2" in cmd
    assert "<prompt: sha256:" in cmd and f"{len(fixture.prompt)} chars>" in cmd and fixture.prompt not in cmd
    assert "--effort low" in rep["commands"]["lean_low_effort"]
    assert rep["commands"]["baseline"] in (out / "report.md").read_text()


# ── baseline flags and usage ─────────────────────────────────────────────────
def test_baseline_command_matches_what_claude_agent_builds(monkeypatch):
    from agents import claude_agent
    captured = {}

    class Proc:
        returncode = 0

        async def communicate(self):
            return b'{"result": "x"}', b""

    async def fake_exec(*args, **kw):
        captured["args"] = list(args)
        return Proc()

    monkeypatch.setattr(claude_agent, "_find_cli", lambda: "claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(claude_agent.run("PROMPT", model="sonnet", tools_off=False))
    real = captured["args"]

    def strip(argv):
        out, skip = [], False
        for a in argv:
            if skip:
                skip = False
            elif a in ("--model", "--max-budget-usd"):
                skip = True
            else:
                out.append(a)
        return out
    ours = ex.build_command("baseline", "PROMPT", model="sonnet")
    assert strip(ours) == strip(real)
    assert "--dangerously-skip-permissions" in real and real[real.index("--output-format") + 1] == "json"


CLI_SAMPLE = json.dumps({
    "type": "result", "subtype": "success", "is_error": False, "num_turns": 6, "result": "done",
    "total_cost_usd": 0.4321,
    "usage": {"input_tokens": 12, "cache_creation_input_tokens": 3456, "cache_read_input_tokens": 78901,
              "output_tokens": 321},
    "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 12, "outputTokens": 321, "cacheReadInputTokens": 78901,
                                         "cacheCreationInputTokens": 3456, "costUSD": 0.4321}}})


def test_usage_maps_cache_read_and_write_from_a_recorded_cli_sample(world, tmp_path):
    class Fixed(Real):
        def run_agent(self, argv, cwd, timeout):
            return CLI_SAMPLE, 0, 2.0
    r = ex.run_one(world[0], "baseline", 1, Fixed(), tmp_path / "wd")
    assert (r.tokens_fresh_in, r.cache_read, r.cache_write, r.tokens_out) == (12, 78901, 3456, 321)
    assert (r.cost_usd, r.turns) == (0.4321, 6)


# ── Round 2, P1-2: the trusted bytes live in memory, not in a sibling directory ───────────────────────────────────
def test_no_pristine_directory_is_left_for_the_agent(world, tmp_path):
    seen = []

    def agent(w):
        seen.extend(p.name for p in w.parent.iterdir())
    run_tampered(world, tmp_path, agent)
    assert not [n for n in seen if n.startswith("pristine")]


def test_agent_replacing_acceptance_test_in_repo_and_a_sibling_pristine_dir_is_still_tampered(world, tmp_path):
    trivial = "def test_add():\n    pass\n"

    def agent(w):
        for d in (w, w.parent / w.name.replace("repo-", "pristine-")):
            d.mkdir(exist_ok=True)
            (d / "test_acc.py").write_text(trivial)
    r = run_tampered(world, tmp_path, agent)
    assert r.tampered is True and r.tests_passed is False and "test_acc.py" in r.tampered_files


def test_trusted_bytes_are_read_once_before_any_agent_runs(world, tmp_path):
    fixture = world[0]
    ex.check_fixture_fails_before(fixture, Real())              # preflight freezes them
    sha = fixture.test_sha256
    (Path(fixture.fixture_dir) / "acc.py").write_text("def test_add():\n    pass\n")   # disk changes mid-experiment
    r = ex.run_one(fixture, "baseline", 1, Real(lambda w: (w / "calc.py").write_text(FIXED)), tmp_path / "wd")
    assert (r.tests_passed, r.tampered) == (True, False)
    assert fixture.test_sha256 == sha and fixture.test_bytes == ACCEPT.encode()
    sb = ex.make_snapshot_repo(world[1], world[2], tmp_path / "b", fixture)
    assert (sb.path / "test_acc.py").read_bytes() == ACCEPT.encode()   # reinstalled from memory, not the edited file


def test_diff_baseline_ignores_an_agent_rewritten_pristine_dir(world, tmp_path):
    def agent(w):
        fake = w.parent / w.name.replace("repo-", "pristine-")
        fake.mkdir(exist_ok=True)
        (fake / "calc.py").write_text(FIXED)
        (w / "calc.py").write_text(FIXED)
    d = diff_of(world, tmp_path, agent)
    assert "+    return a + b" in d and "-    return a - b" in d


# ── Round 2, P1-3: nothing in the agent's tree can shadow or reconfigure pytest ──────────────────────────────────
def test_pytest_is_launched_isolated_with_a_trusted_pythonpath_ini(tmp_path):
    ini = tmp_path / "t.ini"
    argv = ex.resolve_test_argv("python -m pytest test_acc.py -q", tmp_path, python="/venv/py", ini=ini)
    assert argv[:4] == ["/venv/py", "-I", "-B", "-m"] and argv[4] == "pytest"
    assert argv[:1] + argv[4:5] == ["/venv/py", "pytest"]
    assert ex.resolve_test_argv("pytest x.py", tmp_path, python="/venv/py")[:5] == ["/venv/py", "-I", "-B", "-m", "pytest"]
    assert f"pythonpath = {tmp_path}\n" in ex.trusted_pytest_ini(tmp_path)


def test_isolated_launcher_alone_ignores_a_shadow_pytest_module(tmp_path):
    """Launcher layer only (no restore step): a root pytest.py that exits 0 must not replace the real pytest."""
    (tmp_path / "pytest.py").write_text("raise SystemExit(0)\n")
    (tmp_path / "test_x.py").write_text("def test_x():\n    assert 1 == 2\n")
    assert ex.SubprocessRunner(cli="claude").run_tests("python -m pytest test_x.py -q", tmp_path) == ex.PYTEST_FAILED


def test_isolated_launcher_still_imports_the_agents_code(tmp_path):
    (tmp_path / "calc.py").write_text(FIXED)
    (tmp_path / "test_x.py").write_text(ACCEPT)
    assert ex.SubprocessRunner(cli="claude").run_tests("python -m pytest test_x.py -q", tmp_path) == ex.PYTEST_PASSED


def test_agent_pytest_py_shadow_is_a_fail(world, tmp_path):
    r = run_tampered(world, tmp_path, lambda w: (w / "pytest.py").write_text("raise SystemExit(0)\n"))
    assert r.tests_passed is False
    assert r.tampered is True and "pytest.py" in r.tampered_files


def test_agent_pytest_py_shadow_with_a_correct_fix_is_still_not_a_pass(world, tmp_path):
    def agent(w):
        (w / "calc.py").write_text(FIXED)
        (w / "pytest.py").write_text("raise SystemExit(0)\n")
    r = run_tampered(world, tmp_path, agent)
    assert r.tests_passed is False and r.tampered is True


@pytest.mark.parametrize("rel,body", [
    ("sub/conftest.py", "import os\n\n\ndef pytest_sessionfinish(session):\n    os._exit(0)\n"),
    ("sub/deeper/sitecustomize.py", "import os\nos._exit(0)\n"),
    ("usercustomize.py", "import os\nos._exit(0)\n"),
    ("_pytest/__init__.py", "raise SystemExit(0)\n"),
    ("pytest/__init__.py", "raise SystemExit(0)\n")])
def test_agent_hooks_at_any_depth_are_removed_and_fail(world, tmp_path, rel, body):
    def agent(w):
        (w / rel).parent.mkdir(parents=True, exist_ok=True)
        (w / rel).write_text(body)
    r = run_tampered(world, tmp_path, agent)
    assert r.tampered is True and r.tests_passed is False
    assert rel.split("/")[0] in " ".join(r.tampered_files) or rel in r.tampered_files


def test_conftest_forcing_a_pass_does_not_survive_restore(world, tmp_path):
    body = ("import pytest\n\n\n@pytest.hookimpl(trylast=True)\ndef pytest_runtest_makereport(item, call):\n"
            "    from _pytest.reports import TestReport\n    return None\n")

    def agent(w):
        (w / "sub").mkdir()
        (w / "sub" / "conftest.py").write_text(body)
    r = run_tampered(world, tmp_path, agent)
    assert r.tampered is True and r.tests_passed is False


# ── Round 2, P2-5 budget detection / P2-7b tampered_files ────────────────────────────────────────────────────────
def test_budget_stop_is_read_from_structured_fields_only(world, tmp_path):
    answer = json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
                         "total_cost_usd": 0.5, "result": "I added the --max-budget-usd flag and error_max_budget handling"})
    fake = Real()
    fake.run_agent = lambda argv, cwd, timeout: (answer, 0, 1.0)
    r = ex.run_one(world[0], "baseline", 1, fake, tmp_path / "wd")
    assert r.status == "ok"                                     # the answer text mentions the flag: not a budget stop
    stop = json.dumps({"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "total_cost_usd": 2.1})
    fake.run_agent = lambda argv, cwd, timeout: (stop, 1, 1.0)
    r = ex.run_one(world[0], "baseline", 1, fake, tmp_path / "wd")
    assert r.status == "budget_exceeded" and r.tests_passed is False


@pytest.mark.parametrize("out,rc,hit", [
    (json.dumps({"subtype": "success", "result": "pass --max-budget-usd 2 to cap spend"}), 0, False),
    (json.dumps({"subtype": "success", "is_error": False, "result": "budget exceeded handling added"}), 0, False),
    (json.dumps({"subtype": "error_during_execution", "is_error": True, "result": "max-budget-usd"}), 1, False),
    (json.dumps({"subtype": "error_max_budget_usd", "is_error": True}), 1, True),
    (json.dumps([{"type": "system"}, {"type": "result", "subtype": "error_max_budget_usd", "is_error": True}]), 1, True),
    ("Error: Exceeded USD budget (2)", 1, False),                 # no keyword match, no structure
    ("Error: max budget reached", 1, True),                      # stderr text fallback: failed CLI, output not JSON
    ("Error: max budget reached", 0, False),
    ("", 1, False)])
def test_budget_hit_cases(out, rc, hit):
    assert ex._budget_hit(out, rc) is hit


def test_tampered_files_are_sanitised_in_the_public_report(world):
    leaked = "/Users/someone/secret-project/" + "conftest.py"
    raw = ex.RunResult("baseline", 1, tampered=True, tampered_files=[leaked, "ok/conftest.py"])
    rep = ex.build_report_dict("d", "f", "abc", 1, [raw])
    dumped = json.dumps(rep)
    assert "someone" not in dumped and "ok/conftest.py" in dumped
    assert raw.tampered_files[0] == leaked                       # the run object itself is untouched


def test_absolute_pythonpath_lets_the_launcher_import_the_checkout_from_another_cwd(tmp_path, monkeypatch):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "mymod.py").write_text("VALUE = 7\n")
    (sandbox / "test_x.py").write_text("import mymod\n\n\ndef test_x():\n    assert mymod.VALUE == 7\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert ex.SubprocessRunner(cli="claude").run_tests("python -m pytest test_x.py -q", sandbox) == ex.PYTEST_PASSED


# ── Round 3, P1-b: the oracle is frozen from committed blobs ─────────────────────────────────────────────────────
def test_modified_uncommitted_acceptance_test_is_refused_and_nothing_runs(world, tmp_path):
    fixture = world[0]
    (Path(fixture.fixture_dir) / "acc.py").write_text("def test_add():\n    pass\n")   # weakened, not committed
    runner = Real()
    with pytest.raises(ex.FixtureError, match="uncommitted|differs"):
        ex.run_experiment(fixture, ["baseline"], 1, runner, tmp_path / "out", "2026-01-01")
    assert fixture.test_bytes is None and runner.diffs == []


def test_untracked_file_in_fixture_dir_is_refused(world):
    fixture = world[0]
    (Path(fixture.fixture_dir) / "extra.py").write_text("x = 1\n")
    with pytest.raises(ex.FixtureError, match="uncommitted"):
        ex.freeze_test(fixture)


def test_fixture_outside_git_is_refused(world, tmp_path):
    fixture = world[0]
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "acc.py").write_text(ACCEPT)
    fixture.fixture_dir = loose
    with pytest.raises(ex.FixtureError, match="git"):
        ex.freeze_test(fixture)


def test_committed_acceptance_test_is_used_and_its_blob_hash_is_reported(world, tmp_path):
    fixture = world[0]
    expected = git(fixture.fixture_dir, "rev-parse", "HEAD:acc.py")
    ex.freeze_test(fixture)
    assert fixture.test_bytes == ACCEPT.encode() and fixture.test_blob_sha1 == expected
    report = ex.build_report_dict("2026-01-01", "f", "abc", 1, [], oracle={"test_blob_sha1": fixture.test_blob_sha1,
                                                                            "test_sha256": fixture.test_sha256})
    assert report["oracle"]["test_blob_sha1"] == expected
    assert expected in ex.render_markdown(report)


# ── Round 3, P1-a: the interpreter environment is part of the tamper check ───────────────────────────────────────
@pytest.fixture
def fake_venv(tmp_path):
    sp = tmp_path / "fakevenv" / "site-packages"
    (sp / "pkg").mkdir(parents=True)
    (sp / "pkg" / "__init__.py").write_text("X = 1\n")
    (sp / "pkg-1.0.dist-info").mkdir()
    (sp / "pkg-1.0.dist-info" / "METADATA").write_text("Name: pkg\n")
    return sp


def run_with_venv(world, tmp_path, venv, agent):
    """The agent fixes the bug (so the tests would pass) and also does `agent(venv)`."""
    def both(w):
        (w / "calc.py").write_text(FIXED)
        agent(venv)
    return ex.run_one(world[0], "baseline", 1, Real(both), tmp_path / "wd", venv_roots=[venv])


def test_agent_adding_a_pth_file_to_the_venv_fails_tampered(world, tmp_path, fake_venv):
    r = run_with_venv(world, tmp_path, fake_venv, lambda v: (v / "evil.pth").write_text("import os\n"))
    assert r.tampered is True and r.tests_passed is False and "venv:evil.pth" in r.tampered_files


def test_agent_adding_a_pytest_plugin_entry_point_fails_tampered(world, tmp_path, fake_venv):
    def agent(v):
        d = v / "plug-1.0.dist-info"
        d.mkdir()
        (d / "entry_points.txt").write_text("[pytest11]\nplug = plug\n")
    r = run_with_venv(world, tmp_path, fake_venv, agent)
    assert r.tampered is True and r.tests_passed is False
    assert "venv:plug-1.0.dist-info/entry_points.txt" in r.tampered_files


def test_agent_editing_an_installed_module_fails_tampered(world, tmp_path, fake_venv):
    r = run_with_venv(world, tmp_path, fake_venv, lambda v: (v / "pkg" / "__init__.py").write_text("X = 2\n"))
    assert r.tampered is True and r.tests_passed is False and "venv:pkg/__init__.py" in r.tampered_files


def test_agent_that_only_writes_in_its_repo_has_no_venv_tamper(world, tmp_path, fake_venv):
    r = run_with_venv(world, tmp_path, fake_venv, lambda v: None)
    assert (r.tampered, r.tests_passed, r.tampered_files) == (False, True, None)


def test_pycache_in_the_venv_is_not_tampering(world, tmp_path, fake_venv):
    def agent(v):
        (v / "pkg" / "__pycache__").mkdir()
        (v / "pkg" / "__pycache__" / "x.cpython-314.pyc").write_bytes(b"\0")
    assert run_with_venv(world, tmp_path, fake_venv, agent).tampered is False
