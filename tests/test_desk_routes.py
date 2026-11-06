"""Desk command routes (S-8): validation, tiers, rate limit, result, pause."""
import pytest
from fastapi.testclient import TestClient

KEY = {"X-Secret-Key": "k"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from tests.test_scrum_responses import _load
    return TestClient(_load(tmp_path, monkeypatch).app)


def cmd(verb="plan", **kw):
    return {"kind": "command", "verb": verb, "project": "p", "sprint": "s1", "actor": "mike", **kw}


def post(c, body, key="i1"):
    h = dict(KEY)
    if key:
        h["Idempotency-Key"] = key
    return c.post("/scrum/responses", json=body, headers=h)


def test_t1_queued(client):
    r = post(client, cmd())
    assert r.status_code == 201 and r.json()["status"] == "queued" and r.json()["tier"] == "T1"


def test_rejections(client):
    assert post(client, cmd(extra=1)).status_code == 422
    assert post(client, cmd(project="bad id!")).status_code == 422
    assert post(client, cmd("nope")).status_code == 422
    assert post(client, cmd("step_done")).status_code == 422
    assert post(client, cmd("step_done", evidence="ok"), "i2").status_code == 201
    assert post(client, cmd("go")).status_code == 403
    assert post(client, cmd("merge")).status_code == 403
    assert post(client, cmd(), key=None).status_code == 400


def test_idempotency_conflict_and_rate_limit(client):
    assert post(client, cmd(), "a").status_code == 201
    assert post(client, cmd("validate"), "a").status_code == 409
    for i in range(9):
        assert post(client, cmd(), f"k{i}").status_code == 201
    assert post(client, cmd(), "over").status_code == 429


def test_pause_claim_and_result(client):
    cid = post(client, cmd()).json()["id"]
    assert client.post(f"/scrum/responses/{cid}/result", json={"status": "done", "summary": "x"}, headers=KEY).status_code == 409
    p = client.post("/scrum/desk/pause", json={"paused": True, "by": "mike", "reason": "r"}, headers=KEY).json()
    assert p["paused"] is True and p["by"] == "mike" and client.get("/scrum/desk/pause", headers=KEY).json()["reason"] == "r"
    assert post(client, cmd("validate"), "z").status_code == 201
    assert client.post(f"/scrum/responses/{cid}/consume", headers=KEY).status_code == 423
    client.post("/scrum/desk/pause", json={"paused": False, "by": "mike"}, headers=KEY)
    assert client.post(f"/scrum/responses/{cid}/consume", headers=KEY).status_code == 200
    assert client.post(f"/scrum/responses/{cid}/consume", headers=KEY).status_code == 409
    bad = {"status": "done", "summary": "x", "z": 1}
    assert client.post(f"/scrum/responses/{cid}/result", json=bad, headers=KEY).status_code == 422
    assert client.post(f"/scrum/responses/{cid}/result", json={"status": "done", "summary": "ok"}, headers=KEY).status_code == 200
    assert client.post(f"/scrum/responses/{cid}/result", json={"status": "failed", "summary": "ok"}, headers=KEY).status_code == 409
    rows = client.get("/scrum/responses?kind=command", headers=KEY).json()
    assert {r["id"]: r["status"] for r in rows}[cid] == "done"
