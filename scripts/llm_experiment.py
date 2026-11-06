"""Claude CLI flag experiment: pure logic (variants, commands, report maths, rendering) plus the runner/CLI.

Safety: every run happens in its own git worktree of a pinned commit under a temp directory, spend is capped per run
by --max-budget-usd, and nothing here touches a live database, the live queue or a production checkout.
The pure functions do no I/O beyond write_reports; all process/git work goes through a Runner so tests can fake it.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.claude_agent import parse_json_output  # noqa: E402  (S-1 parser; do not write a second one)

# Exactly the flags agents/claude_agent.run uses for an agent_run (tools_off=False): [cli, -p, prompt,
# --dangerously-skip-permissions, --output-format json]. There is no reusable builder there, so this is pinned by hand.
BASELINE_FLAGS = ["--dangerously-skip-permissions"]
LEAN_EXTRA = ["--strict-mcp-config", "--disable-slash-commands"]

# Single source of truth for the variants. `optional` variants only run when the CLI accepts their flags.
VARIANTS = {
    "baseline": {"flags": list(BASELINE_FLAGS), "optional": False},
    "lean": {"flags": BASELINE_FLAGS + LEAN_EXTRA, "optional": False},
    "lean_low_effort": {"flags": BASELINE_FLAGS + LEAN_EXTRA + ["--effort", "low"], "optional": False},
    "lean_medium_effort": {"flags": BASELINE_FLAGS + LEAN_EXTRA + ["--effort", "medium"], "optional": True},
}
CHECKED_FLAGS = ("--effort", "--strict-mcp-config", "--disable-slash-commands")
BASELINE_NAME = "baseline"
STATUSES = ("ok", "budget_exceeded", "invalid_failover", "error")
EXCLUDED_STATUSES = ("invalid_failover", "error")
SCOPE_NOTE = ("One dev_backend-style task only. dev_frontend and integrator roles were NOT tested; "
              "this fixture may not represent dev_frontend work (the heaviest input consumer).")

TOKEN_FIELDS = ("tokens_fresh_in", "cache_read", "cache_write", "tokens_out")
METRIC_FIELDS = ("cost_usd", "wall_s", "turns") + TOKEN_FIELDS


def build_command(variant, prompt, max_budget_usd=2.0, model=None):
    argv = ["claude", "-p", prompt] + list(VARIANTS[variant]["flags"])
    if model:
        argv += ["--model", model]
    return argv + ["--output-format", "json", "--max-budget-usd", f"{max_budget_usd:g}"]


def format_command(argv):
    return shlex.join(argv)


def detect_unsupported(variants, help_text):
    out = {}
    for name in variants:
        flags = VARIANTS[name]["flags"]
        missing = [f for f in CHECKED_FLAGS if f in flags and f not in help_text]
        if missing:
            out[name] = "CLI --help does not list " + ", ".join(missing)
    return out


@dataclass
class RunResult:
    variant: str
    repeat: int
    tokens_fresh_in: int = 0
    cache_read: int = 0
    cache_write: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    wall_s: float = 0.0
    turns: int = 0
    tests_passed: "bool | None" = None
    reviewer_verdict: "str | None" = None
    status: str = "ok"
    error: "str | None" = None


def median_and_spread(values):
    values = list(values)
    return statistics.median(values), min(values), max(values)


def summarise_variant(runs):
    valid = [r for r in runs if r.status not in EXCLUDED_STATUSES]
    summary = {
        "n_valid": len(valid),
        "n_excluded": len(runs) - len(valid),
        # budget_exceeded runs are in `valid` but never count as passes.
        "tests_passed_count": sum(1 for r in valid if r.status == "ok" and r.tests_passed is True),
    }
    for field in METRIC_FIELDS:
        if valid:
            med, lo, hi = median_and_spread(getattr(r, field) for r in valid)
        else:
            med = lo = hi = None
        summary[field] = {"median": med, "min": lo, "max": hi}
    return summary


def verdict(summary, baseline_summary, repeats, name=None, unsupported=None):
    if name == BASELINE_NAME:
        return "BASELINE"
    if unsupported:
        return "UNSUPPORTED"
    if repeats < 3 or summary["n_excluded"] or summary["n_valid"] < repeats:
        return "NOT SAFE"
    if summary["tests_passed_count"] != repeats:
        return "NOT SAFE"
    base = baseline_summary["cost_usd"]["median"] if baseline_summary else None
    cost = summary["cost_usd"]["median"]
    if base is None or cost is None or not cost < base:
        return "NOT SAFE"
    return "SAFE"


def build_report_dict(date, fixture, commit, repeats, runs, unsupported=None, max_budget_usd=2.0, prompt="<prompt>"):
    unsupported = unsupported or {}
    names = [n for n in VARIANTS if n not in unsupported or not VARIANTS[n]["optional"]]
    by_variant = {n: [r for r in runs if r.variant == n] for n in names}
    summaries = {n: summarise_variant(by_variant[n]) for n in names if n not in unsupported}
    base = summaries.get(BASELINE_NAME)
    verdicts = {n: verdict(summaries[n], base, repeats, name=n) if n in summaries else "UNSUPPORTED" for n in names}
    return {
        "date": date, "fixture": fixture, "commit": commit, "repeats": repeats,
        "commands": {n: format_command(build_command(n, prompt, max_budget_usd)) for n in names},
        "runs": [asdict(r) for r in runs],
        "summaries": summaries,
        "verdicts": verdicts,
        "unsupported": dict(unsupported),
        "note": SCOPE_NOTE,
    }


def _fmt(v):
    if v is None:
        return "n/a"
    return f"{v:.4g}" if isinstance(v, float) else str(v)


def render_markdown(report):
    L = [f"# LLM flag experiment {report['date']}", "",
         f"- Date: {report['date']}", f"- Fixture: {report['fixture']}",
         f"- Pinned commit: {report['commit']}", f"- Repeats: {report['repeats']}", "",
         "## Commands", "", "| variant | command |", "|---|---|"]
    for n, cmd in report["commands"].items():
        L.append(f"| {n} | `{cmd}` |")
    for n, reason in report["unsupported"].items():
        L.append(f"| {n} | unsupported: {reason} |")
    L += ["", "## Runs", "",
          "| variant | run | fresh in | cache read | cache write | out | cost_usd | wall time | turns | tests passed | reviewer verdict | status |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in report["runs"]:
        L.append("| " + " | ".join([
            r["variant"], str(r["repeat"]), _fmt(r["tokens_fresh_in"]), _fmt(r["cache_read"]),
            _fmt(r["cache_write"]), _fmt(r["tokens_out"]), _fmt(r["cost_usd"]), _fmt(r["wall_s"]),
            _fmt(r["turns"]), _fmt(r["tests_passed"]), _fmt(r["reviewer_verdict"]), r["status"]]) + " |")
    L += ["", "## Median / min / max per variant", "",
          "| variant | valid | excluded | tests passed | cost_usd | wall_s | turns | fresh in | cache read | cache write | out | verdict |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for n, s in report["summaries"].items():
        cells = [f"{_fmt(s[f]['median'])} ({_fmt(s[f]['min'])}-{_fmt(s[f]['max'])})"
                 for f in ("cost_usd", "wall_s", "turns") + TOKEN_FIELDS]
        L.append("| " + " | ".join([n, str(s["n_valid"]), str(s["n_excluded"]),
                                    f"{s['tests_passed_count']}/{report['repeats']}", *cells,
                                    report["verdicts"][n]]) + " |")
    if report["unsupported"]:
        L += ["", "## Unsupported", ""]
        L += [f"- {n}: unsupported: {reason}" for n, reason in report["unsupported"].items()]
    L += ["", "## Scope", "", report["note"], ""]
    return "\n".join(L)


def report_dir(root, date_str):
    return Path(root) / "data" / "experiments" / date_str


def write_reports(report, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    (out_dir / "report.md").write_text(render_markdown(report))


# ── Fixtures ─────────────────────────────────────────────────────────────────
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "experiments"
BROKEN_FIXTURE_MSG = "fixture tests pass before the agent changed anything: fixture is broken"
MAX_BUDGET_USD = 2.0
AGENT_TIMEOUT_S = 1800
EXPECTED_COST_DEFAULT_RUN = "~$5-6"


class FixtureError(Exception):
    pass


class FixtureBrokenError(FixtureError):
    pass


@dataclass
class Fixture:
    name: str
    repo_url_or_path: str
    pinned_commit: str
    test_cmd: str
    prompt: str
    known_good_commit: "str | None" = None


def load_fixture(path):
    d = Path(os.path.abspath(os.path.expanduser(str(path))))
    if not d.is_dir():
        raise FixtureError(f"fixture directory not found: {d}")
    task, meta = d / "task.md", d / "fixture.json"
    for f in (task, meta):
        if not f.is_file():
            raise FixtureError(f"fixture {d.name} is missing {f.name} ({f})")
    try:
        data = json.loads(meta.read_text())
    except ValueError as e:
        raise FixtureError(f"fixture {d.name}: fixture.json is not valid JSON: {e}")
    missing = [k for k in ("name", "repo_url_or_path", "pinned_commit", "test_cmd") if not data.get(k)]
    if missing:
        raise FixtureError(f"fixture {d.name}: fixture.json is missing {', '.join(missing)}")
    if not isinstance(data["test_cmd"], str):
        raise FixtureError(f"fixture {d.name}: test_cmd must be a string")
    prompt = task.read_text()
    if not prompt.strip():
        raise FixtureError(f"fixture {d.name}: task.md is empty")
    return Fixture(name=data["name"], repo_url_or_path=data["repo_url_or_path"], pinned_commit=data["pinned_commit"],
                   test_cmd=data["test_cmd"], prompt=prompt, known_good_commit=data.get("known_good_commit"))


def resolve_fixture_arg(task):
    """--task is a fixture name under tests/fixtures/experiments or a directory."""
    p = Path(os.path.expanduser(task))
    return p if p.is_dir() else FIXTURES_DIR / task


# ── Worktrees ────────────────────────────────────────────────────────────────
def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def make_worktree(repo, commit, base_dir):
    """Fresh detached worktree of `commit` under base_dir. The path is absolute (DI-020: expanduser, then abspath)."""
    path = Path(os.path.abspath(os.path.expanduser(os.path.join(str(base_dir), "wt-" + uuid.uuid4().hex[:8]))))
    path.parent.mkdir(parents=True, exist_ok=True)
    p = _git(os.path.expanduser(str(repo)), "worktree", "add", "--detach", str(path), commit)
    if p.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {(p.stderr or p.stdout).strip()}")
    return path


def remove_worktree(repo, path):
    _git(os.path.expanduser(str(repo)), "worktree", "remove", "--force", str(path))
    shutil.rmtree(path, ignore_errors=True)
    _git(os.path.expanduser(str(repo)), "worktree", "prune")


def resolve_repo(fixture, cache_dir):
    """A local path is used as is; a URL is cloned (no checkout) once into cache_dir."""
    loc = os.path.expanduser(fixture.repo_url_or_path)
    if os.path.isdir(loc):
        return Path(os.path.abspath(loc))
    dest = Path(os.path.abspath(os.path.join(str(cache_dir), "repo")))
    if not dest.exists():
        p = subprocess.run(["git", "clone", "--no-checkout", fixture.repo_url_or_path, str(dest)],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise FixtureError(f"cannot clone {fixture.repo_url_or_path}: {p.stderr.strip()}")
    return dest


# ── Runner ───────────────────────────────────────────────────────────────────
class Runner(Protocol):
    def run_agent(self, argv, cwd, timeout): ...      # -> (stdout, returncode, wall_s)
    def run_tests(self, test_cmd, cwd): ...           # -> exit code
    def run_reviewer(self, diff, task_prompt, cwd): ...  # -> verdict string
    def cli_help(self): ...                           # -> text of `claude --help`


VERDICT_RE = re.compile(r"\b(APPROVE|REQUEST_CHANGES)\b")


def parse_verdict(text):
    found = VERDICT_RE.findall(text or "")
    return found[-1] if found else "UNKNOWN"


class SubprocessRunner:
    """Real runner: same CLI, same flags as production (see BASELINE_FLAGS), plus a read-only reviewer call."""

    def __init__(self, cli=None):
        if cli is None:
            from agents.claude_agent import _find_cli
            cli = _find_cli() or "claude"
        self.cli = cli

    def run_agent(self, argv, cwd, timeout):
        argv = [self.cli] + list(argv[1:])
        t0 = time.monotonic()
        try:
            p = subprocess.run(argv, cwd=os.path.expanduser(str(cwd)), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return "", 124, time.monotonic() - t0
        out = p.stdout.strip()
        if p.returncode != 0 and not out:
            out = p.stderr.strip()     # budget / limit wording can arrive on stderr only
        return out, p.returncode, time.monotonic() - t0

    def run_tests(self, test_cmd, cwd):
        try:
            return subprocess.run(shlex.split(test_cmd), cwd=str(cwd), capture_output=True, timeout=900).returncode
        except subprocess.TimeoutExpired:
            return 124
        except OSError:
            return 127

    def run_reviewer(self, diff, task_prompt, cwd):
        from shared.scrum_flow import review_prompt   # the existing reviewer prompt (the role has no separate loader)
        story = {"id": "EXPERIMENT", "title": "LLM flag experiment fixture",
                 "acceptance": [task_prompt[:2000]]}
        prompt = (review_prompt(story, "claude", diff or "(empty diff)") +
                  "\n\nEnd your reply with one final line: APPROVE or REQUEST_CHANGES.")
        # Read-only like claude_agent tools_off=True: no tools, no permission bypass.
        argv = [self.cli, "-p", prompt, "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
                "--output-format", "json", "--model", "sonnet"]
        try:
            p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return "UNKNOWN"
        text, _, _ = parse_json_output(p.stdout.strip())
        return parse_verdict(text)

    def cli_help(self):
        p = subprocess.run([self.cli, "--help"], capture_output=True, text=True, timeout=60)
        return p.stdout + p.stderr


# ── Running ──────────────────────────────────────────────────────────────────
def check_fixture_fails_before(fixture, runner, workdir=None):
    """Refuse to start if the tests already pass on the pinned commit (the fixture would prove nothing)."""
    own = workdir is None
    base = tempfile.mkdtemp(prefix="llmexp-gate-") if own else workdir
    repo = None
    wt = None
    try:
        repo = resolve_repo(fixture, base)
        wt = make_worktree(repo, fixture.pinned_commit, base)
        if runner.run_tests(fixture.test_cmd, wt) == 0:
            raise FixtureBrokenError(BROKEN_FIXTURE_MSG)
    finally:
        if wt is not None:
            remove_worktree(repo, wt)
        if own:
            shutil.rmtree(base, ignore_errors=True)


def _budget_hit(text):
    return bool(re.search(r"max.?budget|budget.{0,20}(exceeded|reached)|error_max_budget", text or "", re.IGNORECASE))


def _num_turns(out):
    try:
        n = json.loads(out).get("num_turns")
        return n if isinstance(n, int) and not isinstance(n, bool) else 0
    except (ValueError, TypeError, AttributeError):
        return 0


def _apply_usage(res, usage):
    res.tokens_fresh_in = usage.get("tokens_in") or 0
    res.cache_read = usage.get("tokens_cache_read") or 0
    res.cache_write = usage.get("tokens_cache_write") or 0
    res.tokens_out = usage.get("tokens_out") or 0
    res.cost_usd = usage.get("cost_usd") or 0.0


def run_one(fixture, variant, repeat, runner, workdir):
    res = RunResult(variant=variant, repeat=repeat)
    repo = wt = None
    try:
        repo = resolve_repo(fixture, workdir)
        wt = make_worktree(repo, fixture.pinned_commit, workdir)
        argv = build_command(variant, fixture.prompt, MAX_BUDGET_USD)
        out, rc, wall = runner.run_agent(argv, wt, AGENT_TIMEOUT_S)
        res.wall_s = wall
        text, usage, is_error = parse_json_output(out)
        _apply_usage(res, usage)
        res.turns = _num_turns(out)
        if _budget_hit(out):
            res.status, res.tests_passed = "budget_exceeded", False
            return res
        if rc != 0 or is_error:
            from agents import failover
            if failover.classify(text if is_error else out) == "limit":
                res.status = "invalid_failover"
                res.error = (text or out)[:300]
                return res
            res.status, res.error = "error", f"claude exited {rc}: {(text or out)[:300]}"
            return res
        res.tests_passed = runner.run_tests(fixture.test_cmd, wt) == 0
        diff = _git(wt, "diff", "HEAD").stdout
        res.reviewer_verdict = runner.run_reviewer(diff, fixture.prompt, wt)
    except Exception as e:  # one bad run must not abort the experiment
        res.status, res.error = "error", f"{type(e).__name__}: {e}"
    finally:
        if wt is not None:
            remove_worktree(repo, wt)
    return res


def run_experiment(fixture, variant_names, repeats, runner, out_root, date_str):
    check_fixture_fails_before(fixture, runner)
    unsupported = detect_unsupported(variant_names, runner.cli_help())
    order = [n for n in variant_names if n == BASELINE_NAME] + [n for n in variant_names if n != BASELINE_NAME]
    runs = []
    base = tempfile.mkdtemp(prefix="llmexp-")
    try:
        for name in order:
            if name in unsupported:
                continue
            for i in range(1, repeats + 1):
                runs.append(run_one(fixture, name, i, runner, base))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    report = build_report_dict(date_str, fixture.name, fixture.pinned_commit, repeats, runs, unsupported=unsupported,
                               max_budget_usd=MAX_BUDGET_USD, prompt=fixture.prompt)
    # build_report_dict only lists unsupported *optional* variants; make the rest visible too.
    for n, reason in unsupported.items():
        report["unsupported"].setdefault(n, reason)
        report["verdicts"].setdefault(n, "UNSUPPORTED")
    out_dir = report_dir(out_root, date_str)
    write_reports(report, out_dir)
    return report, out_dir


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--task", required=True, help="fixture name under tests/fixtures/experiments, or a fixture dir")
    ap.add_argument("--variants", default="baseline,lean,lean_low_effort")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--out-root", default=str(REPO_ROOT))
    ap.add_argument("--date", default=date.today().isoformat())
    a = ap.parse_args(argv)
    names = [v.strip() for v in a.variants.split(",") if v.strip()]
    bad = [v for v in names if v not in VARIANTS]
    if bad:
        ap.error(f"unknown variants: {', '.join(bad)} (known: {', '.join(VARIANTS)})")
    try:
        fixture = load_fixture(resolve_fixture_arg(a.task))
    except FixtureError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"WARNING: this spends real money: {len(names)} variants x {a.repeats} repeats, each capped at "
          f"${MAX_BUDGET_USD:g} (expected {EXPECTED_COST_DEFAULT_RUN} for the default run).", flush=True)
    try:
        report, out_dir = run_experiment(fixture, names, a.repeats, SubprocessRunner(), a.out_root, a.date)
    except FixtureError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Report: {out_dir / 'report.md'}\n")
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
