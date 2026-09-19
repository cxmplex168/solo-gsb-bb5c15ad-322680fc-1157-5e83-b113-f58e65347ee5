"""End-to-end API tests using a temporary SQLite database."""

import os
import tempfile

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test-merges.db")
    monkeypatch.setenv("MERGE_DB_PATH", db_path)

    # Import lazily so MERGE_DB_PATH is read after the env var is set.
    from fastapi.testclient import TestClient

    from app.main import app, init_db

    init_db()
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

def test_create_auto_resolved_merge_returns_immutable_result(client):
    payload = {
        "base": {"a": 1, "b": 2},
        "left": {"a": 10, "b": 2},
        "right": {"a": 1, "b": 20},
    }
    resp = client.post("/merges", json=payload, headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["revision"] == 1
    assert body["conflicts"] == []
    assert body["merged"] == {"a": 10, "b": 20}
    assert body["result"] == {"a": 10, "b": 20}


def test_create_with_conflicts_reports_placeholders_and_candidates(client):
    payload = {
        "base": {"a": 1},
        "left": {"a": 10},
        "right": {"a": 20},
    }
    resp = client.post("/merges", json=payload, headers={"Idempotency-Key": "k2"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "open"
    assert body["conflicts"] == [
        {"pointer": "/a", "base": 1, "left": 10, "right": 20}
    ]
    assert body["merged"]["a"]["__conflict__"] == {
        "base": 1,
        "left": 10,
        "right": 20,
    }
    assert body["result"] is None


def test_missing_idempotency_key_is_422(client):
    resp = client.post("/merges", json={"base": {}, "left": {}, "right": {}})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "missing_idempotency_key"


# ---------------------------------------------------------------------------
# Revision / conflict resolution lifecycle
# ---------------------------------------------------------------------------

def _create_conflicted(client, key="k3"):
    payload = {
        "base": {"a": 1, "b": 1},
        "left": {"a": 10, "b": 10},
        "right": {"a": 20, "b": 20},
    }
    resp = client.post("/merges", json=payload, headers={"Idempotency-Key": key})
    return resp.json()


def test_status_query_returns_current_state(client):
    merge = _create_conflicted(client)
    resp = client.get(f"/merges/{merge['merge_id']}")
    assert resp.status_code == 200
    assert resp.json()["revision"] == 1
    assert len(resp.json()["conflicts"]) == 2


def test_stale_revision_returns_409_with_current_revision(client):
    merge = _create_conflicted(client)
    resp = client.post(
        f"/merges/{merge['merge_id']}/resolve",
        json={"revision": 99, "resolutions": {"/a": 10}},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "revision_mismatch"
    assert detail["current_revision"] == 1


def test_batch_resolution_increments_revision(client):
    merge = _create_conflicted(client)
    mid = merge["merge_id"]

    resp = client.post(
        f"/merges/{mid}/resolve",
        json={"revision": 1, "resolutions": {"/a": 10}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["revision"] == 2
    assert body["status"] == "open"
    assert [c["pointer"] for c in body["conflicts"]] == ["/b"]
    assert body["merged"]["a"] == 10
    assert body["result"] is None

    # The first conflict must not be resolvable twice.
    resp = client.post(
        f"/merges/{mid}/resolve",
        json={"revision": 2, "resolutions": {"/a": 10}},
    )
    assert resp.status_code == 422

    # Final batch finalizes the immutable result.
    resp = client.post(
        f"/merges/{mid}/resolve",
        json={"revision": 2, "resolutions": {"/b": 20}},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["status"] == "resolved"
    assert body["revision"] == 3
    assert body["conflicts"] == []
    assert body["result"] == {"a": 10, "b": 20}
    assert body["merged"] == body["result"]


def test_resolved_merge_is_immutable(client):
    merge = _create_conflicted(client, key="k4")
    mid = merge["merge_id"]
    client.post(
        f"/merges/{mid}/resolve",
        json={"revision": 1, "resolutions": {"/a": 10, "/b": 10}},
    )
    resp = client.post(
        f"/merges/{mid}/resolve",
        json={"revision": 2, "resolutions": {"/a": 1}},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "already_resolved"

    result = client.get(f"/merges/{mid}/result").json()
    assert result["result"] == {"a": 10, "b": 10}


def test_result_endpoint_conflicts_while_open(client):
    merge = _create_conflicted(client, key="k5")
    resp = client.get(f"/merges/{merge['merge_id']}/result")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "not_resolved"


def test_invalid_pointer_does_not_change_revision(client):
    merge = _create_conflicted(client, key="k6")
    mid = merge["merge_id"]
    resp = client.post(
        f"/merges/{mid}/resolve",
        json={"revision": 1, "resolutions": {"/nope": 1}},
    )
    assert resp.status_code == 422
    assert client.get(f"/merges/{mid}").json()["revision"] == 1


def test_unknown_merge_returns_404(client):
    assert client.get("/merges/does-not-exist").status_code == 404


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_same_idempotency_key_does_not_create_new_order(client):
    payload = {"base": {"a": 1}, "left": {"a": 2}, "right": {"a": 3}}
    first = client.post("/merges", json=payload, headers={"Idempotency-Key": "dup"})
    assert first.status_code == 201

    second = client.post("/merges", json=payload, headers={"Idempotency-Key": "dup"})
    assert second.status_code == 200
    assert second.json()["merge_id"] == first.json()["merge_id"]
    assert second.json()["revision"] == first.json()["revision"]


def test_idempotency_key_reuse_with_different_payload_is_422(client):
    payload_a = {"base": {"a": 1}, "left": {"a": 2}, "right": {"a": 3}}
    payload_b = {"base": {"a": 1}, "left": {"a": 20}, "right": {"a": 30}}
    client.post("/merges", json=payload_a, headers={"Idempotency-Key": "reuse"})
    resp = client.post("/merges", json=payload_b, headers={"Idempotency-Key": "reuse"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "idempotency_key_reuse"


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
