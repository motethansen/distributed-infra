import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import llm_experiment as ex  # noqa: E402


def run(variant="lean", i=1, cost=1.0, passed=True, status="ok", **kw):
    return ex.RunResult(variant=variant, repeat=i, cost_usd=cost, tests_passed=passed, status=status,
                        wall_s=10.0, turns=5, tokens_fresh_in=100, cache_read=1000, cache_write=50, tokens_out=20, **kw)


def summ(runs):
    return ex.summarise_variant(runs)


BASE = summ([run("baseline", i, 2.0) for i in (1, 2, 3)])


def test_build_command_variants():
    b = ex.build_command("baseline", "hi")
    assert b[:3] == ["claude", "-p", "hi"] and b[-4:] == ["--output-format", "json", "--max-budget-usd", "2"]
    assert "--strict-mcp-config" not in b
    lean = ex.build_command("lean", "hi", model="m")
    assert "--strict-mcp-config" in lean and "--disable-slash-commands" in lean and "--effort" not in lean
    assert lean[lean.index("--model") + 1] == "m"
    low = ex.build_command("lean_low_effort", "hi")
    assert low[low.index("--effort") + 1] == "low"
    assert ex.format_command(["a", "b c"]) == "a 'b c'"


def test_median_and_spread():
    assert ex.median_and_spread([3, 1, 2]) == (2, 1, 3)
    assert ex.median_and_spread([1, 2, 3, 4]) == (2.5, 1, 4)


def test_invalid_failover_excluded():
    s = summ([run(cost=1.0), run(cost=1.2), run(cost=99, status="invalid_failover"), run(cost=3.0, status="error")])
    assert s["n_valid"] == 2 and s["n_excluded"] == 2
    assert s["cost_usd"]["max"] == 1.2


def test_budget_exceeded_counts_as_failure():
    s = summ([run(), run(), run(status="budget_exceeded", passed=True)])
    assert s["n_valid"] == 3 and s["tests_passed_count"] == 2


def test_safe_needs_three_of_three():
    good = summ([run(cost=1.0) for _ in range(3)])
    assert ex.verdict(good, BASE, 3) == "SAFE"
    two = summ([run(), run(), run(passed=False)])
    assert ex.verdict(two, BASE, 3) == "NOT SAFE"
    excluded = summ([run(), run(), run(status="invalid_failover")])
    assert ex.verdict(excluded, BASE, 3) == "NOT SAFE"
    assert ex.verdict(good, BASE, 2) == "NOT SAFE"


def test_safe_needs_cost_below_baseline():
    same = summ([run(cost=2.0) for _ in range(3)])
    assert ex.verdict(same, BASE, 3) == "NOT SAFE"
    assert ex.verdict(BASE, BASE, 3, name="baseline") == "BASELINE"
    assert ex.verdict(same, BASE, 3, unsupported="x") == "UNSUPPORTED"


def test_detect_unsupported_without_effort():
    help_text = "--strict-mcp-config  --disable-slash-commands --output-format"
    bad = ex.detect_unsupported(list(ex.VARIANTS), help_text)
    assert set(bad) == {"lean_low_effort", "lean_medium_effort"}
    assert "--effort" in bad["lean_low_effort"]
    assert ex.detect_unsupported(list(ex.VARIANTS), help_text + " --effort") == {}


def test_markdown_and_reports(tmp_path):
    runs = [run(v, i, c) for v, c in (("baseline", 2.0), ("lean", 1.0)) for i in (1, 2, 3)]
    rep = ex.build_report_dict("2026-11-06", "fx", "abc123", 3, runs, unsupported={"lean_low_effort": "no --effort"})
    md = ex.render_markdown(rep)
    for v in ("baseline", "lean"):
        assert ex.format_command(ex.build_command(v, "<prompt>")) in md
    assert "unsupported: no --effort" in md and "SAFE" in md and "abc123" in md
    assert "dev_frontend and integrator roles were NOT tested" in md
    d = ex.report_dir(tmp_path, "2026-11-06")
    assert d == tmp_path / "data" / "experiments" / "2026-11-06"
    ex.write_reports(rep, d)
    assert json.loads((d / "report.json").read_text())["commit"] == "abc123"
    assert (d / "report.md").exists()
