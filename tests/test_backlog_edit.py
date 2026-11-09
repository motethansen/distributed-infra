"""Backlog editing: add, move through statuses, close only on request, never guess."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from shared import backlog_edit as be
from shared import scrum

DOC = """---
project: demo
---

# Demo backlog

## Open items

| ID | Story | Priority | Status | Evidence |
|---|---|---|---|---|
| DM-001 | **First** | High | 🔵 planned | notes |
| DM-002 | Second | Low | 🏃 in progress | |

## Closed items

| ID | Story | Priority | Status | Evidence |
|---|---|---|---|---|
| DM-010 | Old | High | ✅ done 2026-08-01 | PR #1 |

Related: nothing.
"""


def test_add_story_goes_in_the_open_table_with_the_next_id_and_nowhere_else():
    new, sid = be.add_story(DOC, "Third | with a pipe", priority="High", evidence="proposed by PO")
    assert sid == "DM-011"                                    # max existing is DM-010, closed rows count
    lines = new.split("\n")
    i = lines.index("| DM-002 | Second | Low | 🏃 in progress |  |") + 1 if "| DM-002 | Second | Low | 🏃 in progress |  |" in lines else None
    row = next(l for l in lines if l.startswith("| DM-011"))
    assert row == "| DM-011 | Third / with a pipe | High | 🔵 planned | proposed by PO |"
    assert lines.index(row) < lines.index("## Closed items")      # inside the open table
    assert scrum.parse_backlog(new)["DM-011"] == "planned" and scrum.parse_backlog(new)["DM-010"] == "done"
    assert new.count("\n") == DOC.count("\n") + 1                # exactly one line added


def test_status_moves_through_the_open_states_and_keeps_every_other_row():
    s = be.set_status(DOC, "DM-001", be.IN_PROGRESS)
    s = be.set_status(s, "DM-001", be.IN_REVIEW, evidence="PR https://github.com/o/r/pull/3")
    assert scrum.parse_backlog(s)["DM-001"] == "in_review"
    assert "notes — PR https://github.com/o/r/pull/3" in s
    assert [l for l in s.split("\n") if "DM-002" in l] == [l for l in DOC.split("\n") if "DM-002" in l]
    be.status_of(s, "DM-002") == "🏃 in progress"


def test_closing_needs_the_explicit_flag_and_never_reopens_or_re_closes():
    with pytest.raises(be.BacklogError, match="closing=True"):
        be.set_status(DOC, "DM-001", "✅ done 2026-10-31")
    done = be.set_status(DOC, "DM-001", "✅ done 2026-10-31", evidence="confirmed at review", closing=True)
    assert scrum.parse_backlog(done)["DM-001"] == "done" and scrum.parse_backlog_dates(done)["DM-001"] == "2026-10-31"
    with pytest.raises(be.BacklogError, match="already closed"):
        be.set_status(done, "DM-001", be.IN_PROGRESS)
    with pytest.raises(be.BacklogError, match="already closed"):
        be.set_status(DOC, "DM-010", be.IN_PROGRESS)             # a story closed in the file stays closed


def test_unknown_or_duplicated_stories_and_missing_tables_are_errors():
    with pytest.raises(be.BacklogError, match="not in the backlog"):
        be.set_status(DOC, "DM-999", be.IN_PROGRESS)
    dup = DOC + "\n| ID | Story | Status |\n|---|---|---|\n| DM-001 | again | 🔵 planned |\n"
    with pytest.raises(be.BacklogError, match="appears in 2 tables"):
        be.set_status(dup, "DM-001", be.IN_PROGRESS)
    with pytest.raises(be.BacklogError, match="no open-items table"):
        be.add_story("# nothing\n\nno tables here\n", "x")
    with pytest.raises(be.BacklogError, match="prefix"):
        be.prefix_of("| ID |\n")


def test_a_row_without_an_evidence_column_still_takes_evidence_in_its_last_cell():
    text = "## Open\n\n| ID | Story | Status |\n|---|---|---|\n| ZZ-001 | s | 🔵 planned |\n"
    out = be.set_status(text, "ZZ-001", be.IN_REVIEW, evidence="PR 1")
    assert "| ZZ-001 | s | 🔍 in review — PR 1 |" in out


REPO = Path(os.environ.get("VIZNEO_REPO", "~/Projects/github/techstartups/vizneoprojects")).expanduser()


@pytest.mark.skipif(not REPO.exists(), reason="vizneoprojects checkout not present")
def test_real_backlogs_survive_add_and_status_edits_untouched_otherwise():
    for f in sorted(REPO.glob("*/product-backlog.md")):
        text = f.read_text(encoding="utf-8")
        before = scrum.parse_backlog(text)
        try:
            new, sid = be.add_story(text, "Probe story")
        except be.BacklogError:
            continue                                            # a backlog without an open table is refused, not mangled
        after = scrum.parse_backlog(new)
        assert after.pop(sid) == "planned" and after == before, f.parent.name
        moved = be.set_status(new, sid, be.IN_PROGRESS)
        assert scrum.parse_backlog(moved)[sid] == "in_progress"
        assert moved.count("\n") == new.count("\n") == text.count("\n") + 1


def test_a_story_never_lands_in_the_closed_table_even_when_it_comes_first():
    text = ("## Closed items\n\n| ID | Story | Status |\n|---|---|---|\n| QQ-001 | old | ✅ done 2026-01-01 |\n\n"
            "## Items\n\n| ID | Story | Status |\n|---|---|---|\n| QQ-002 | open | 🔵 planned |\n")
    new, sid = be.add_story(text, "fresh")
    lines = new.split("\n")
    assert lines.index(next(l for l in lines if l.startswith(f"| {sid}"))) > lines.index("## Items")


# --- DI-072: "already closed" is decided from the leading status only ---------------------------------------------

def _row(status: str) -> str:
    return ("| ID | Story | Priority | Status | Evidence |\n|---|---|---|---|---|\n"
            f"| WD-1 | Story | High | {status} | |\n")


WD_279 = "🔍 in review — built; reviewer asked to rerun the check, `✅ done` on the first one"
WD_291 = "🔍 in review — ✅ criteria met except the last one"


def test_leading_status_is_the_text_before_the_first_separator_trimmed():
    assert be.leading_status("  ✅ done — note — more ") == "✅ done"
    assert be.leading_status("🔵 planned") == "🔵 planned"
    assert be.is_closed_status("✅ done 2026-08-01 — x") and not be.is_closed_status(WD_279)


@pytest.mark.parametrize("status", [WD_279, WD_291])
@pytest.mark.parametrize("evidence", ["checked the response", "https://github.com/o/p/pull/9 merged 2026-10-30"])
def test_rows_that_only_mention_a_closed_mark_in_the_note_are_accepted(status, evidence):
    new = be.set_status(_row(status), "WD-1", "✅ done 2026-11-08", evidence, closing=True)
    assert "| WD-1 | Story | High | ✅ done 2026-11-08 |" in new and evidence in new


@pytest.mark.parametrize("mark", ["✅", "⛔", "🟢", "❌"])
@pytest.mark.parametrize("vs", ["", "️"])
def test_rows_that_really_start_closed_are_refused_with_only_the_leading_status(mark, vs):
    status = f"{mark}{vs} done — " + "a long note about how it went. " * 10
    with pytest.raises(be.BacklogError, match="already closed") as e:
        be.set_status(_row(status), "WD-1", "✅ done 2026-11-08", "x", closing=True)
    msg = str(e.value)
    assert f"already closed: {mark}{vs} done" in msg and " — " not in msg and len(msg) < 120


def test_closed_mark_is_only_used_in_the_helper_module():
    root = Path(__file__).resolve().parent.parent
    users = [str(p.relative_to(root)) for d in ("shared", "scripts") for p in (root / d).rglob("*.py")
             if "CLOSED_MARK" in p.read_text(encoding="utf-8")]
    assert users == ["shared/backlog_edit.py"]


# --- DI-072: the accept path (dis-agent accept) on tmp fixtures, and the helper edge cases -----------------------

def _accept(tmp_path, monkeypatch, status, *, evidence=None, pr=None):
    import argparse
    import importlib.util
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("dis_agent_di072", root / "scripts" / "dis_agent.py")
    da = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(da)
    (tmp_path / "proj").mkdir()
    backlog = tmp_path / "proj" / "product-backlog.md"
    backlog.write_text(_row(status), encoding="utf-8")
    monkeypatch.setenv("VIZNEO_REPO", str(tmp_path))
    monkeypatch.setattr(da.sf, "_git", lambda repo, *a, run=None: __import__("subprocess").CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(da.sf, "verify_pr_merged", lambda url: {"merged_at": "2026-10-30", "title": "t"})
    monkeypatch.setattr(da, "_refresh_board", lambda: None)
    da.cmd_accept(argparse.Namespace(project="proj", story="WD-1", pr=pr, evidence=evidence, yes=True))
    return backlog.read_text(encoding="utf-8")


@pytest.mark.parametrize("status", [WD_279, WD_291])
def test_accept_with_evidence_takes_a_row_that_only_mentions_a_mark_in_its_note(tmp_path, monkeypatch, status):
    out = _accept(tmp_path, monkeypatch, status, evidence="I ran the check and it passes")
    assert "| WD-1 | Story | High | ✅ done " in out and "I ran the check and it passes" in out


@pytest.mark.parametrize("status", [WD_279, WD_291])
def test_accept_with_pr_takes_a_row_that_only_mentions_a_mark_in_its_note(tmp_path, monkeypatch, status):
    pr = "https://github.com/o/p/pull/9"
    out = _accept(tmp_path, monkeypatch, status, pr=pr)
    assert "| WD-1 | Story | High | ✅ done " in out and f"{pr} merged 2026-10-30" in out


@pytest.mark.parametrize("mark", ["✅", "⛔", "🟢", "❌"])
@pytest.mark.parametrize("vs", ["", "️"])
def test_accept_refuses_a_row_that_already_starts_closed(tmp_path, monkeypatch, mark, vs):
    with pytest.raises(SystemExit) as e:
        _accept(tmp_path, monkeypatch, f"{mark}{vs} done — " + "long note " * 20, evidence="x")
    assert "not accepted" in str(e.value) or "in review" in str(e.value)


@pytest.mark.parametrize("cell,status,closed", [
    ("✅ done", "✅ done", True),                                  # no separator: the whole trimmed cell
    ("   ✅ done", "✅ done", True),                               # leading spaces
    ("   🔍 in review — `✅ done` in the note", "🔍 in review", False),
    ("🔍 in review — see `⛔`", "🔍 in review", False),
    ("planned", "planned", False),
    ("in_review", "in_review", False),
    ("", "", False),
])
def test_leading_status_and_is_closed_status_edge_cases(cell, status, closed):
    assert be.leading_status(cell) == status
    assert be.is_closed_status(cell) is closed and be.is_closed_text(cell) is closed


def test_closed_mark_appears_in_no_other_py_file_of_shared_or_scripts():
    root = Path(__file__).resolve().parent.parent
    users = sorted(str(p.relative_to(root)) for d in ("shared", "scripts") for p in (root / d).rglob("*.py")
                   if "CLOSED_MARK" in p.read_text(encoding="utf-8"))
    assert users == ["shared/backlog_edit.py"]
