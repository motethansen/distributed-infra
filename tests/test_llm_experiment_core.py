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
        assert ex.display_command(ex.build_command(v, "<prompt>"), "<prompt>", "claude") in md
    assert "unsupported: no --effort" in md and "SAFE" in md and "abc123" in md
    assert "dev_frontend and integrator roles were NOT tested" in md
    d = ex.report_dir(tmp_path, "2026-11-06")
    assert d == tmp_path / "data" / "experiments" / "2026-11-06"
    ex.write_reports(rep, d)
    assert json.loads((d / "report.json").read_text())["commit"] == "abc123"
    assert (d / "report.md").exists()


def test_two_of_three_is_not_safe_even_when_cheaper():
    cheap_two = summ([run(cost=0.1), run(cost=0.1), run(cost=0.1, passed=False)])
    assert cheap_two["cost_usd"]["median"] < BASE["cost_usd"]["median"]
    assert ex.verdict(cheap_two, BASE, 3) == "NOT SAFE"


def test_three_of_three_with_higher_cost_is_not_safe():
    pricey = summ([run(cost=5.0) for _ in range(3)])
    assert pricey["tests_passed_count"] == 3
    assert ex.verdict(pricey, BASE, 3) == "NOT SAFE"


# ── Round 2, P2-4: every SAFE condition has its own test ─────────────────────────────────────────────────────────
def three(**kw):
    return summ([run(cost=1.0, reviewer_verdict="APPROVE", **kw) for _ in range(3)])


def test_safe_baseline_case_for_the_conditions_below():
    assert ex.verdict(three(), BASE, 3) == "SAFE"


def test_tampered_run_blocks_safe_even_if_tests_passed_is_true():
    s = summ([run(reviewer_verdict="APPROVE"), run(reviewer_verdict="APPROVE"),
              run(reviewer_verdict="APPROVE", tampered=True)])
    assert s["tampered_count"] == 1 and ex.verdict(s, BASE, 3) == "NOT SAFE"


def test_diff_failed_blocks_safe():
    s = summ([run(reviewer_verdict="APPROVE"), run(reviewer_verdict="APPROVE"), run(reviewer_verdict="diff_failed")])
    assert s["diff_failed_count"] == 1 and ex.verdict(s, BASE, 3) == "NOT SAFE"


def test_any_reviewer_reject_blocks_safe():
    s = summ([run(reviewer_verdict="APPROVE"), run(reviewer_verdict="APPROVE"), run(reviewer_verdict="REQUEST_CHANGES")])
    assert s["rejected_count"] == 1 and ex.verdict(s, BASE, 3) == "NOT SAFE"


def test_median_cost_must_be_strictly_below_baseline():
    s = summ([run(cost=2.0, reviewer_verdict="APPROVE") for _ in range(3)])
    assert ex.verdict(s, BASE, 3) == "NOT SAFE"


def test_two_of_three_passes_block_safe():
    s = summ([run(reviewer_verdict="APPROVE"), run(reviewer_verdict="APPROVE"), run(passed=False, reviewer_verdict="APPROVE")])
    assert ex.verdict(s, BASE, 3) == "NOT SAFE"


def test_unavailable_reviewer_does_not_block_but_is_shown_as_unreviewed(tmp_path):
    unrev = [run("lean", i, 1.0, reviewer_verdict="unavailable") for i in (1, 2, 3)]
    s = summ(unrev)
    assert s["unreviewed_count"] == 3 and ex.verdict(s, BASE, 3) == "SAFE"
    rep = ex.build_report_dict("d", "f", "abc", 3, [run("baseline", i, 2.0) for i in (1, 2, 3)] + unrev)
    md = ex.render_markdown(rep)
    assert rep["verdicts"]["lean"] == "SAFE" and "UNREVIEWED" in md and "lean has 3 run(s)" in md


def test_report_states_the_safe_rule_under_the_verdict():
    rep = ex.build_report_dict("d", "f", "abc", 3, [run("baseline", i, 2.0) for i in (1, 2, 3)])
    md = ex.render_markdown(rep)
    assert md.index("## Median / min / max") < md.index("## Verdict rule") < md.index("## Scope")
    for phrase in ("3 of 3 acceptance passes", "no tampering", "diff was generated", "strictly below", "did not request changes"):
        assert phrase in md


# ── Codex review of S-2 (2026-11-07), P1/P2 ──────────────────────────────────────────────────────────────────────
def test_unknown_cost_is_not_zero_and_blocks_safe():
    """P1: a run whose output had no cost must not look cheaper than the baseline."""
    from agents.claude_agent import parse_json_output
    _, usage, _ = parse_json_output("plain text, not JSON")
    r = ex.RunResult(variant="lean", repeat=1)
    ex._apply_usage(r, usage)
    assert r.cost_usd is None and r.tokens_out is None
    s = summ([run(reviewer_verdict="APPROVE"), run(reviewer_verdict="APPROVE"),
              ex.RunResult("lean", 3, tests_passed=True, reviewer_verdict="APPROVE")])
    assert s["cost_unknown_count"] == 1
    assert ex.verdict(s, BASE, 3) == "NOT SAFE"
    assert s["cost_usd"]["median"] == 1.0          # known costs only, so the median is not dragged to zero


def test_all_costs_unknown_gives_no_median():
    s = summ([ex.RunResult("lean", i, tests_passed=True) for i in (1, 2, 3)])
    assert s["cost_usd"] == {"median": None, "min": None, "max": None}
    assert ex.verdict(s, BASE, 3) == "NOT SAFE"


def test_report_shows_pytest_exit_code(tmp_path):
    """P2: the report carries the acceptance command's exit code, not just a boolean."""
    runs = [run("baseline", i, 2.0, tests_exit_code=0) for i in (1, 2, 3)] + \
           [run("lean", 1, 1.0, passed=False, tests_exit_code=2)]
    rep = ex.build_report_dict("2026-11-07", "f", "abc", 3, runs)
    assert [r["tests_exit_code"] for r in rep["runs"]] == [0, 0, 0, 2]
    md = ex.render_markdown(rep)
    assert "tests exit code" in md


def test_only_selected_variants_are_reported():
    """P2: an optional variant nobody selected is not listed with a command and a verdict for zero runs."""
    runs = [run("baseline", i, 2.0) for i in (1, 2, 3)] + [run("lean", i, 1.0) for i in (1, 2, 3)]
    rep = ex.build_report_dict("2026-11-07", "f", "abc", 3, runs, variants=["baseline", "lean"])
    assert set(rep["commands"]) == {"baseline", "lean"} and set(rep["verdicts"]) == {"baseline", "lean"}
    assert "lean_medium_effort" not in ex.render_markdown(rep)
    rep = ex.build_report_dict("2026-11-07", "f", "abc", 3, runs)
    assert "lean_medium_effort" not in rep["verdicts"]


def test_scope_note_says_why_it_is_not_a_worktree():
    assert "git worktree" in ex.SCOPE_NOTE and "git archive" in ex.SCOPE_NOTE


def test_unknown_baseline_cost_blocks_safe():
    """Codex round 3, P1: baseline costs [2, unknown, unknown] must not be compared as if the median were known."""
    base = summ([run("baseline", 1, 2.0), ex.RunResult("baseline", 2, tests_passed=True),
                 ex.RunResult("baseline", 3, tests_passed=True)])
    assert base["cost_unknown_count"] == 2
    assert ex.verdict(three(), base, 3) == "NOT SAFE"
    assert ex.verdict(three(), BASE, 3) == "SAFE"          # a fully known baseline still allows SAFE
