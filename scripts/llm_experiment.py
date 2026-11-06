"""Pure logic for the Claude CLI flag experiment (variants, commands, report maths, rendering).

No I/O beyond write_reports, no network. The runner/CLI is added by a later task.
"""
import json
import shlex
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path

# Today's flags for an agent_run-style invocation. A later task aligns this with agents/claude_agent.py.
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
