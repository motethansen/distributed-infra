"""Independent validation tests for story DI-072."""
from __future__ import annotations

import argparse
import ast
import importlib.util
import subprocess
from pathlib import Path

import pytest

from shared import backlog_edit as be
from shared import scrum_flow as sf

ROOT = Path(__file__).resolve().parent.parent

WD_279_STATUS = "🔍 in review — built; reviewer asked to rerun the check, `✅ done` on the first one"
WD_291_STATUS = "🔍 in review — ✅ criteria met except the last one"


def _make_backlog(story_id: str, status: str) -> str:
    return (
        "---\nproject: demo\n---\n\n"
        "# Demo backlog\n\n"
        "## Open items\n\n"
        "| ID | Story | Priority | Status | Evidence |\n"
        "|---|---|---|---|---|\n"
        f"| {story_id} | Demo story | High | {status} | initial note |\n"
    )


def _load_dis_agent():
    spec = importlib.util.spec_from_file_location("dis_agent_di072_indep", ROOT / "scripts" / "dis_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_dis_agent_accept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, story_id: str, status: str, *, evidence: str | None = None, pr: str | None = None) -> str:
    da = _load_dis_agent()
    proj_dir = tmp_path / "demo"
    proj_dir.mkdir(parents=True, exist_ok=True)
    backlog_file = proj_dir / "product-backlog.md"
    backlog_file.write_text(_make_backlog(story_id, status), encoding="utf-8")

    monkeypatch.setenv("VIZNEO_REPO", str(tmp_path))
    monkeypatch.setattr(da.sf, "_git", lambda repo, *a, run=None: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(da.sf, "verify_pr_merged", lambda url: {"merged_at": "2026-11-08", "title": "Fix issue"})
    monkeypatch.setattr(da, "_refresh_board", lambda: None)

    ns = argparse.Namespace(project="demo", story=story_id, pr=pr, evidence=evidence, yes=True)
    da.cmd_accept(ns)
    return backlog_file.read_text(encoding="utf-8")


# --- Criterion 1: helpers, callers, and CLOSED_MARK grep check ---

def test_criterion_1_leading_status_and_is_closed_status_helpers():
    assert callable(be.leading_status)
    assert callable(be.is_closed_status)
    assert callable(be.is_closed_text)

    assert be.leading_status("  🔍 in review — `✅ done` in note  ") == "🔍 in review"
    assert be.leading_status("  ✅ done 2026-11-08 — note — second note ") == "✅ done 2026-11-08"
    assert be.leading_status("🔵 planned") == "🔵 planned"

    assert be.is_closed_status("✅ done 2026-11-08 — note") is True
    assert be.is_closed_status(WD_279_STATUS) is False
    assert be.is_closed_status(WD_291_STATUS) is False

    for sample in ("✅ done", "⛔ blocked", "🟢 live", "❌ cancelled", WD_279_STATUS, WD_291_STATUS, "🔵 planned"):
        assert be.is_closed_text(sample) == be.is_closed_status(sample)


def test_criterion_1_closed_mark_grep_and_callers():
    py_files = [p for d in ("shared", "scripts") for p in (ROOT / d).rglob("*.py")]
    closed_mark_users = sorted(
        str(p.relative_to(ROOT))
        for p in py_files
        if "CLOSED_MARK" in p.read_text(encoding="utf-8")
    )
    assert closed_mark_users == ["shared/backlog_edit.py"]

    tree = ast.parse((ROOT / "shared" / "backlog_edit.py").read_text(encoding="utf-8"))
    funcs = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "leading_status" in funcs and "is_closed_status" in funcs and "is_closed_text" in funcs
    calls_in_is_closed_text = [
        c.func.id for c in ast.walk(funcs["is_closed_text"])
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    ]
    assert "is_closed_status" in calls_in_is_closed_text or "leading_status" in calls_in_is_closed_text


# --- Criterion 2: WD-279-shaped row accepted with --evidence and --pr ---

def test_criterion_2_wd_279_accepted_with_evidence_and_pr(tmp_path, monkeypatch):
    out_ev = _run_dis_agent_accept(tmp_path / "ev", monkeypatch, "WD-279", WD_279_STATUS, evidence="verified on staging")
    assert "| WD-279 | Demo story | High | ✅ done " in out_ev
    assert "verified on staging" in out_ev

    pr_url = "https://github.com/motethansen/distributed-infra/pull/72"
    out_pr = _run_dis_agent_accept(tmp_path / "pr", monkeypatch, "WD-279", WD_279_STATUS, pr=pr_url)
    assert "| WD-279 | Demo story | High | ✅ done " in out_pr
    assert f"{pr_url} merged 2026-11-08" in out_pr


# --- Criterion 3: WD-291-shaped row accepted with --evidence and --pr ---

def test_criterion_3_wd_291_accepted_with_evidence_and_pr(tmp_path, monkeypatch):
    out_ev = _run_dis_agent_accept(tmp_path / "ev", monkeypatch, "WD-291", WD_291_STATUS, evidence="all criteria verified")
    assert "| WD-291 | Demo story | High | ✅ done " in out_ev
    assert "all criteria verified" in out_ev

    pr_url = "https://github.com/motethansen/distributed-infra/pull/73"
    out_pr = _run_dis_agent_accept(tmp_path / "pr", monkeypatch, "WD-291", WD_291_STATUS, pr=pr_url)
    assert "| WD-291 | Demo story | High | ✅ done " in out_pr
    assert f"{pr_url} merged 2026-11-08" in out_pr


# --- Criterion 4: rows starting with closed marks (including U+FE0F) are refused ---

@pytest.mark.parametrize("prefix", ["✅ done", "⛔", "🟢", "❌"])
@pytest.mark.parametrize("vs", ["", "\ufe0f"])
def test_criterion_4_truly_closed_rows_with_and_without_variation_selector_are_refused(tmp_path, monkeypatch, prefix, vs):
    mark_with_vs = prefix[0] + vs + prefix[1:]
    status = f"{mark_with_vs} 2026-10-01 — some extra details in the cell"
    doc = _make_backlog("WD-300", status)

    with pytest.raises(be.BacklogError, match="already closed"):
        be.set_status(doc, "WD-300", "✅ done 2026-11-08", "attempt close", closing=True)

    with pytest.raises(SystemExit) as excinfo:
        _run_dis_agent_accept(tmp_path / f"case_{ord(prefix[0])}_{len(vs)}", monkeypatch, "WD-300", status, evidence="try accept")
    assert "not accepted" in str(excinfo.value)


# --- Criterion 5: refusal message prints only leading status, no ' — ' note text, < 120 chars ---

@pytest.mark.parametrize("leading", ["✅ done", "⛔", "🟢", "❌", "✅\ufe0f done", "❌\ufe0f cancelled 2026-10-01"])
def test_criterion_5_refusal_message_contains_only_leading_status_no_note_and_under_120_chars(leading):
    long_note = "detailed explanation with `✅ done` and extra commentary " * 5
    cell = f"{leading} — {long_note}"
    doc = _make_backlog("WD-400", cell)

    with pytest.raises(be.BacklogError) as excinfo:
        be.set_status(doc, "WD-400", "✅ done 2026-11-08", "new evidence", closing=True)

    msg = str(excinfo.value)
    assert f"already closed: {leading}" in msg
    assert " — " not in msg
    assert long_note not in msg
    assert len(msg) < 120


# --- Criterion 6: no backlog row edited by hand and nothing under data/ touched ---

def test_criterion_6_nothing_under_data_or_backlogs_touched():
    diff_Origin = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--name-only", "origin/main..HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    status_lines = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()

    for path in diff_Origin:
        assert not path.startswith("data/"), f"data/ file modified on branch: {path}"
        assert not path.endswith("product-backlog.md"), f"backlog file modified on branch: {path}"

    for line in status_lines:
        path = line[3:]
        assert not path.startswith("data/"), f"data/ file dirty in worktree: {path}"
        assert not path.endswith("product-backlog.md"), f"backlog file dirty in worktree: {path}"
