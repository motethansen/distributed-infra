"""Independent acceptance tests for story S-1: Orchestrator records owner responses to board items."""
from __future__ import annotations

from datetime import datetime
import importlib

from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def api(tmp_path, monkeypatch):
    db_path = tmp_path / "s1_queue.db"
    monkeypatch.setenv("QUEUE_DB_PATH", str(db_path))
    monkeypatch.setenv("QUEUE_BACKEND", "sqlite")
    monkeypatch.setenv("SECRET_KEY", "test-secret-s1")
    import orchestrator.db as dbm
    import orchestrator.main as m
    importlib.reload(dbm)
    importlib.reload(m)
    return m


@pytest.fixture
def client(api):
    c = TestClient(api.app)
    c.secret = "test-secret-s1"
    return c


def _headers(client, key=None):
    return {"X-Secret-Key": client.secret if key is None else key}


def _valid_body(**overrides):
    body = {
        "item_key": "review:vantage:S-1",
        "project": "vantage",
        "item_id": "S-1",
        "section": "review",
        "action": "confirm",
        "suggestion_id": "close",
        "suggestion_text": "Close story S-1",
        "comment": None,
        "actor": "owner@vantage",
    }
    body.update(overrides)
    return body


def test_c1_post_response_returns_201_with_id_created_at_and_recorded_status(client):
    for section in ("review", "decision", "adjustment"):
        for action, extra in (
            ("confirm", {"suggestion_id": "sug-1", "suggestion_text": "Apply fix", "comment": None}),
            ("comment", {"suggestion_id": None, "suggestion_text": None, "comment": "Please check edge case", "item_id": None}),
        ):
            r = client.post(
                "/scrum/responses",
                headers=_headers(client),
                json=_valid_body(section=section, action=action, **extra),
            )
            assert r.status_code == 201, r.text
            data = r.json()
            assert data["status"] == "recorded"
            assert data.get("id") is not None
            assert isinstance(data.get("created_at"), str) and len(data["created_at"]) > 0


def test_c2_validation_rejects_confirm_without_suggestion_empty_comment_and_overlong_comment(client):
    # action=confirm without suggestion_id -> 422
    r1 = client.post(
        "/scrum/responses",
        headers=_headers(client),
        json=_valid_body(action="confirm", suggestion_id=None),
    )
    assert r1.status_code == 422

    r1b = client.post(
        "/scrum/responses",
        headers=_headers(client),
        json=_valid_body(action="confirm", suggestion_id=""),
    )
    assert r1b.status_code == 422

    # action=comment with empty or whitespace comment -> 422
    for bad_comment in (None, "", "   ", "\t\n "):
        r2 = client.post(
            "/scrum/responses",
            headers=_headers(client),
            json=_valid_body(action="comment", suggestion_id=None, comment=bad_comment),
        )
        assert r2.status_code == 422, f"Expected 422 for comment={bad_comment!r}, got {r2.status_code}"

    # comment longer than 4000 chars -> 422
    r3 = client.post(
        "/scrum/responses",
        headers=_headers(client),
        json=_valid_body(action="comment", suggestion_id=None, comment="x" * 4001),
    )
    assert r3.status_code == 422


def test_c3_get_pending_responses_returns_newest_first_with_required_fields(client):
    r1 = client.post(
        "/scrum/responses",
        headers=_headers(client),
        json=_valid_body(
            item_key="review:vantage:S-1",
            action="confirm",
            suggestion_id="sug-a",
            suggestion_text="First suggestion",
            comment="note 1",
            actor="alice",
        ),
    )
    assert r1.status_code == 201
    id1 = r1.json()["id"]

    r2 = client.post(
        "/scrum/responses",
        headers=_headers(client),
        json=_valid_body(
            item_key="decision:vantage:S-2",
            section="decision",
            action="comment",
            suggestion_id=None,
            suggestion_text=None,
            comment="Second note",
            actor="bob",
        ),
    )
    assert r2.status_code == 201
    id2 = r2.json()["id"]

    res = client.get("/scrum/responses?status=pending", headers=_headers(client))
    assert res.status_code == 200
    body = res.json()
    rows = body["responses"] if isinstance(body, dict) and "responses" in body else body
    assert len(rows) == 2
    assert [row["id"] for row in rows] == [id2, id1]

    required = {"actor", "created_at", "item_key", "suggestion_id", "suggestion_text", "action", "comment", "status"}
    for row in rows:
        assert required.issubset(row.keys())
        dt = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        assert dt.tzinfo is not None or row["created_at"].endswith("Z") or "+" in row["created_at"]

    assert rows[0]["actor"] == "bob"
    assert rows[0]["item_key"] == "decision:vantage:S-2"
    assert rows[0]["action"] == "comment"
    assert rows[0]["comment"] == "Second note"
    assert rows[1]["actor"] == "alice"
    assert rows[1]["suggestion_id"] == "sug-a"
    assert rows[1]["suggestion_text"] == "First suggestion"


def test_c4_responses_survive_orchestrator_restart_with_fresh_db_instance(tmp_path, monkeypatch):
    db_file = tmp_path / "restart_test.db"
    monkeypatch.setenv("QUEUE_DB_PATH", str(db_file))
    monkeypatch.setenv("QUEUE_BACKEND", "sqlite")
    monkeypatch.setenv("SECRET_KEY", "restart-key")

    import orchestrator.db as dbm
    import orchestrator.main as m
    importlib.reload(dbm)
    importlib.reload(m)

    c1 = TestClient(m.app)
    post_res = c1.post(
        "/scrum/responses",
        headers={"X-Secret-Key": "restart-key"},
        json=_valid_body(item_key="adjustment:vantage:adj-1", section="adjustment", actor="michael"),
    )
    assert post_res.status_code == 201
    created_id = post_res.json()["id"]

    # Simulate orchestrator restart with a freshly reloaded db & main module
    importlib.reload(dbm)
    importlib.reload(m)

    c2 = TestClient(m.app)
    get_res = c2.get("/scrum/responses", headers={"X-Secret-Key": "restart-key"})
    assert get_res.status_code == 200
    body = get_res.json()
    rows = body["responses"] if isinstance(body, dict) and "responses" in body else body
    assert any(r["id"] == created_id and r["item_key"] == "adjustment:vantage:adj-1" for r in rows)


def test_c5_two_responses_same_item_key_append_only_and_latest_returns_newer_winner(client):
    r1 = client.post(
        "/scrum/responses",
        headers=_headers(client),
        json=_valid_body(item_key="review:vantage:S-10", action="confirm", suggestion_id="s1", suggestion_text="Old", actor="owner"),
    )
    assert r1.status_code == 201
    id1 = r1.json()["id"]

    r2 = client.post(
        "/scrum/responses",
        headers=_headers(client),
        json=_valid_body(item_key="review:vantage:S-10", action="comment", suggestion_id=None, comment="Newer override", actor="owner"),
    )
    assert r2.status_code == 201
    id2 = r2.json()["id"]

    r3 = client.post(
        "/scrum/responses",
        headers=_headers(client),
        json=_valid_body(item_key="review:vantage:S-20", action="confirm", suggestion_id="s2", suggestion_text="Other item", actor="owner"),
    )
    assert r3.status_code == 201
    id3 = r3.json()["id"]

    all_res = client.get("/scrum/responses", headers=_headers(client))
    assert all_res.status_code == 200
    all_body = all_res.json()
    all_rows = all_body["responses"] if isinstance(all_body, dict) and "responses" in all_body else all_body
    s10_rows = [r for r in all_rows if r["item_key"] == "review:vantage:S-10"]
    assert len(s10_rows) == 2
    assert {r["id"] for r in s10_rows} == {id1, id2}

    latest_res = client.get("/scrum/responses?latest=1", headers=_headers(client))
    assert latest_res.status_code == 200
    latest_body = latest_res.json()
    latest_rows = latest_body["responses"] if isinstance(latest_body, dict) and "responses" in latest_body else latest_body
    by_key = {r["item_key"]: r for r in latest_rows}
    assert len(latest_rows) == 2
    assert by_key["review:vantage:S-10"]["id"] == id2
    assert by_key["review:vantage:S-10"]["comment"] == "Newer override"
    assert by_key["review:vantage:S-20"]["id"] == id3


def test_c6_consume_response_sets_status_consumed_at_consumed_by_and_second_call_returns_409(client):
    post_res = client.post(
        "/scrum/responses",
        headers=_headers(client),
        json=_valid_body(item_key="review:vantage:S-6"),
    )
    assert post_res.status_code == 201
    resp_id = post_res.json()["id"]

    c1 = client.post(
        f"/scrum/responses/{resp_id}/consume",
        headers={**_headers(client), "X-Client": "dis-agent"},
        params={"consumed_by": "dis-agent", "actor": "dis-agent"},
        json={"consumed_by": "dis-agent", "actor": "dis-agent"},
    )
    assert c1.status_code == 200, c1.text
    c1_data = c1.json()
    c1_row = c1_data["response"] if isinstance(c1_data, dict) and "response" in c1_data else c1_data
    assert c1_row["status"] == "consumed"

    # Verify via GET that status=consumed, consumed_at and consumed_by are set on the stored row
    get_res = client.get("/scrum/responses?status=consumed", headers=_headers(client))
    assert get_res.status_code == 200
    get_body = get_res.json()
    rows = get_body["responses"] if isinstance(get_body, dict) and "responses" in get_body else get_body
    row = next((r for r in rows if r["id"] == resp_id), c1_row)
    assert row["status"] == "consumed"
    assert row.get("consumed_at") is not None
    assert isinstance(row.get("consumed_by"), str) and len(row["consumed_by"]) > 0

    # Second call -> 409
    c2 = client.post(
        f"/scrum/responses/{resp_id}/consume",
        headers=_headers(client),
        json={"consumed_by": "dis-agent"},
    )
    assert c2.status_code == 409


def test_c7_missing_or_wrong_secret_key_returns_401_on_all_three_endpoints(client):
    for bad_headers in ({}, {"X-Secret-Key": "wrong-secret"}):
        r_post = client.post("/scrum/responses", headers=bad_headers, json=_valid_body())
        assert r_post.status_code == 401

        r_get = client.get("/scrum/responses", headers=bad_headers)
        assert r_get.status_code == 401

        r_consume = client.post("/scrum/responses/1/consume", headers=bad_headers, json={"consumed_by": "tester"})
        assert r_consume.status_code == 401
