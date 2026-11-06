"""Desk commands end to end through the HTTP routes (S-8): tiers, validation, lifecycle, rate limit, pause, audit."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from shared.desk_allowlist import ALLOWLIST
from tests.test_scrum_responses import BODY, _load

KEY = {"X-Secret-Key": "k"}   # the same SECRET_KEY the existing scrum tests set in _load
T1 = [v for v, s in ALLOWLIST.items() if s["tier"] == "T1"]
T2 = [v for v, s in ALLOWLIST.items() if s["tier"] == "T2"]
T3 = [v for v, s in ALLOWLIST.items() if s["tier"] == "T3"]


@pytest.fixture
def mod(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch)


@pytest.fixture
def client(mod):
    return TestClient(mod.app)


def cmd(verb="plan", **kw):
    body = {"kind": "command", "verb": verb, "project": "p", "sprint": "s1", "actor": "mike", **kw}
    if verb == "step_done":
        body.setdefault("evidence", "tests pass")
    return body


_n = iter(range(10_000))


def post(c, body, key=None):
    return c.post("/scrum/responses", json=body, headers={**KEY, "Idempotency-Key": key or f"k{next(_n)}"})


def claim(c, cid):
    return c.post(f"/scrum/responses/{cid}/consume", headers={**KEY, "X-Actor": "runner"})


def result(c, cid, status="done", summary="ok"):
    return c.post(f"/scrum/responses/{cid}/result", json={"status": status, "summary": summary}, headers={**KEY, "X-Actor": "runner"})


def pause(c, paused, reason=None):
    return c.post("/scrum/desk/pause", json={"paused": paused, "by": "mike", "reason": reason}, headers=KEY)


def audit(tmp_path):
    con = sqlite3.connect(tmp_path / "q.db")
    return con.execute("SELECT response_id, from_status, to_status, actor, at FROM scrum_audit WHERE response_id IS NOT NULL ORDER BY id").fetchall()


def test_verbs_are_covered():
    assert T1 and T2 and T3


@pytest.mark.parametrize("verb", T1)
def test_t1_verb_is_queued(client, verb):
    r = post(client, cmd(verb))
    assert r.status_code == 201
    assert r.json() == {"id": r.json()["id"], "status": "queued", "tier": "T1"}


def test_step_done_with_evidence_is_queued(client):
    assert post(client, cmd("step_done", evidence="ran the suite")).status_code == 201


@pytest.mark.parametrize("extra", [{"extra": 1}, {"command": "rm -rf /"}, {"args": ["--force"]}])
def test_extra_or_free_text_field_is_422(client, extra):
    assert post(client, cmd(**extra)).status_code == 422


@pytest.mark.parametrize("verb", T2)
def test_t2_needs_confirmation(client, verb):
    r = post(client, cmd(verb))
    assert r.status_code == 403 and r.json()["detail"] == "needs confirm-with-digest (sprint 3)"


@pytest.mark.parametrize("verb", T3)
def test_t3_not_available(client, verb):
    r = post(client, cmd(verb))
    assert r.status_code == 403 and r.json()["detail"] == "not available from the page"


def test_unknown_verb_is_422(client):
    assert post(client, cmd("format_disk")).status_code == 422


@pytest.mark.parametrize("ev", [{}, {"evidence": ""}, {"evidence": "   "}, {"evidence": None}])
def test_step_done_needs_evidence(client, ev):
    body = cmd("step_done")
    body.pop("evidence")
    assert post(client, {**body, **ev}).status_code == 422


@pytest.mark.parametrize("field", ["project", "sprint", "story_id"])
@pytest.mark.parametrize("bad", ["; rm -rf", "../x"])
def test_ids_are_validated(client, field, bad):
    r = post(client, cmd(**{field: bad}))
    assert r.status_code == 422
    assert bad not in r.text   # submitted values are not echoed back


def test_lifecycle_queued_running_done(client, tmp_path):
    cid = post(client, cmd()).json()["id"]
    assert claim(client, cid).status_code == 200
    assert claim(client, cid).status_code == 409
    assert result(client, cid, "done", "all good").status_code == 200
    row = next(r for r in client.get("/scrum/responses?kind=command", headers=KEY).json() if r["id"] == cid)
    assert row["status"] == "done" and row["result_summary"] == "all good"


def test_lifecycle_failed(client):
    cid = post(client, cmd()).json()["id"]
    claim(client, cid)
    assert result(client, cid, "failed", "boom").status_code == 200
    rows = client.get("/scrum/responses?kind=command", headers=KEY).json()
    assert [r["status"] for r in rows if r["id"] == cid] == ["failed"]


def test_result_requires_secret_key(client):
    cid = post(client, cmd()).json()["id"]
    claim(client, cid)
    r = client.post(f"/scrum/responses/{cid}/result", json={"status": "done", "summary": "x"})
    assert r.status_code in (401, 403)


def test_summary_limit(client):
    cid = post(client, cmd()).json()["id"]
    claim(client, cid)
    assert result(client, cid, "done", "x" * 2001).status_code == 422
    assert result(client, cid, "done", "x" * 2000).status_code == 200


def test_invalid_transitions(client):
    cid = post(client, cmd()).json()["id"]
    assert result(client, cid).status_code == 409          # queued -> done
    claim(client, cid)
    assert result(client, cid).status_code == 200
    assert result(client, cid, "failed").status_code == 409  # done twice
    assert result(client, 99999).status_code == 404


def test_rate_limit_per_actor(client):
    for _ in range(10):
        assert post(client, cmd()).status_code == 201
    assert post(client, cmd()).status_code == 429
    assert post(client, cmd(actor="sam")).status_code == 201


def test_pause_shape_and_toggle(client):
    off = client.get("/scrum/desk/pause", headers=KEY).json()
    assert set(off) == {"paused", "by", "at", "reason"} and off["paused"] is False
    on = pause(client, True, "freeze").json()
    assert set(on) == {"paused", "by", "at", "reason"}
    assert on["paused"] is True and on["by"] == "mike" and on["reason"] == "freeze" and on["at"]
    assert client.get("/scrum/desk/pause", headers=KEY).json() == on


def test_pause_survives_restart(tmp_path, monkeypatch):
    c1 = TestClient(_load(tmp_path, monkeypatch).app)
    pause(c1, True, "overnight")
    c2 = TestClient(_load(tmp_path, monkeypatch).app)   # fresh modules, same backing store
    got = c2.get("/scrum/desk/pause", headers=KEY).json()
    assert got["paused"] is True and got["by"] == "mike" and got["reason"] == "overnight"


def test_paused_desk_queues_but_will_not_claim_commands(client):
    pause(client, True, "stop")
    r = post(client, cmd())
    assert r.status_code == 201 and r.json()["status"] == "queued"
    c = claim(client, r.json()["id"])
    assert c.status_code == 423 and c.json()["detail"] == "desk paused"
    pause(client, False)
    assert claim(client, r.json()["id"]).status_code == 200


def test_paused_desk_does_not_affect_responses(client):
    rid = client.post("/scrum/responses", json=BODY, headers={**KEY, "Idempotency-Key": "r1"}).json()["id"]
    pause(client, True)
    assert claim(client, rid).status_code == 200


def test_pause_while_running_lets_result_through(client):
    a = post(client, cmd()).json()["id"]
    b = post(client, cmd("validate")).json()["id"]
    assert claim(client, a).status_code == 200
    pause(client, True)
    assert result(client, a).status_code == 200
    assert claim(client, b).status_code == 423


def test_idempotency_key_with_different_body_is_409(client):
    assert post(client, cmd(), "same").status_code == 201
    assert post(client, cmd("validate"), "same").status_code == 409


def test_idempotency_key_same_body_replays(client):
    a = post(client, cmd(), "same").json()["id"]
    assert post(client, cmd(), "same").json()["id"] == a


def test_list_commands_shape(client):
    cid = post(client, cmd("validate")).json()["id"]
    claim(client, cid)
    result(client, cid, "done", "summary text")
    row = next(r for r in client.get("/scrum/responses?kind=command", headers=KEY).json() if r["id"] == cid)
    assert row["status"] == "done" and row["tier"] == "T1" and row["actor"] == "mike"
    assert row["created_at"].endswith("Z") and row["result_summary"] == "summary text"


def test_audit_row_per_transition(client, tmp_path):
    cid = post(client, cmd()).json()["id"]
    claim(client, cid)
    result(client, cid, "failed", "no")
    rows = audit(tmp_path)
    assert [(r[1], r[2]) for r in rows] == [(None, "queued"), ("queued", "running"), ("running", "failed")]
    assert all(r[0] == cid and r[3] and r[4] for r in rows)
    assert [r[3] for r in rows] == ["mike", "runner", "runner"]


def test_failed_claim_writes_no_audit(client, tmp_path):
    cid = post(client, cmd()).json()["id"]
    claim(client, cid)
    n = len(audit(tmp_path))
    claim(client, cid)
    result(client, 99999)
    assert len(audit(tmp_path)) == n


def test_legacy_rows_without_kind_read_as_response(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    from orchestrator import db
    con = sqlite3.connect(tmp_path / "q.db")
    con.executescript("".join(s + ";" for s in db.SCRUM_RESPONSES_DDL[:1]))
    con.execute("INSERT INTO scrum_responses (item_key, project, section, action, actor, created_at) "
                "VALUES ('k','p','s','confirm','a','2026-01-01T00:00:00.000000Z')")
    con.commit(); con.close()
    c = TestClient(m.app)
    rows = c.get("/scrum/responses", headers=KEY).json()
    assert rows and all(r["kind"] == "response" for r in rows)
    assert c.get("/scrum/responses?kind=command", headers=KEY).json() == []
