import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import llm_experiment as ex  # noqa: E402

ALL_FLAGS = ["--effort", "--strict-mcp-config", "--disable-slash-commands"]


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def fixture(tmp_path):
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
    return ex.load_fixture(d)


class Fake:
    def __init__(self, help_text):
        self.help_text = help_text
        self.argvs = []
        self.n_tests = 0

    def run_agent(self, argv, cwd, timeout):
        self.argvs.append(argv)
        return json.dumps({"result": "ok", "num_turns": 3, "total_cost_usd": 0.5,
                           "usage": {"input_tokens": 1, "output_tokens": 1}}), 0, 1.0

    def run_tests(self, test_cmd, cwd):
        self.n_tests += 1
        return 1 if self.n_tests == 1 else 0  # fails before the agent, passes after

    def run_reviewer(self, diff, task_prompt, cwd):
        return "APPROVE"

    def cli_help(self):
        return self.help_text


@pytest.mark.parametrize("missing", ALL_FLAGS)
def test_variant_with_flag_missing_from_help_is_reported_unsupported(fixture, tmp_path, missing):
    help_text = " ".join(f for f in ALL_FLAGS if f != missing)
    fake = Fake(help_text)
    rep, out_dir = ex.run_experiment(fixture, list(ex.VARIANTS), 1, fake, tmp_path / "out", "2026-03-04")

    affected = [n for n, v in ex.VARIANTS.items() if missing in v["flags"]]
    assert affected
    on_disk = json.loads((out_dir / "report.json").read_text())
    md = (out_dir / "report.md").read_text()
    for name in affected:
        assert missing in on_disk["unsupported"][name]
        assert on_disk["verdicts"][name] == "UNSUPPORTED"
        assert name in md and "unsupported" in md.lower()
        assert not any(r["variant"] == name for r in on_disk["runs"])
    assert missing in md
    # the run did not crash and the unaffected variants (at least baseline) still ran
    assert any(r["variant"] == "baseline" for r in on_disk["runs"])
    assert not any(missing in argv for argv in fake.argvs)
