"""HTTP-level tests for src/sidekick/api/main.py (auth, ownership, folder restriction)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from sidekick.api import main as api


@pytest.fixture
def client():
    return TestClient(api.app)


def _register_and_login(client: TestClient, uname: str, folder: Path) -> dict:
    res = client.post(
        "/api/register",
        json={"name": uname.title(), "uname": uname, "upass": "password123", "folder": str(folder)},
    )
    assert res.status_code in (200, 409), res.text
    # Whitelist the user the same way the admin script does.
    with api._db_lock, api._get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO active_users (uname, created_at) VALUES (?, ?)",
            (uname, datetime.now(timezone.utc).isoformat()),
        )
    res = client.post("/api/login", json={"uname": uname, "upass": "password123"})
    assert res.status_code == 200, res.text
    return res.json()


def _auth(user: dict) -> dict:
    return {"Authorization": f"Bearer {user['token']}"}


@pytest.fixture
def alice(client, tmp_path):
    folder = tmp_path / "alice"
    folder.mkdir()
    (folder / "hello.py").write_text("print('hi')\n")
    user = _register_and_login(client, f"alice_{tmp_path.name[-6:]}".lower(), folder)
    user["folder_path"] = folder
    return user


@pytest.fixture
def bob(client, tmp_path):
    folder = tmp_path / "bob"
    folder.mkdir()
    user = _register_and_login(client, f"bob_{tmp_path.name[-6:]}".lower(), folder)
    user["folder_path"] = folder
    return user


@pytest.fixture
def no_agent():
    """Replace the background agent runner so no LLM is called."""

    def fake_run(session, root, files, task, resume_decision=None):
        with session._lock:
            session.status = "complete"
            session.summary = "done"
            session.mark_finished()

    with patch.object(api, "_run_agent_in_background", side_effect=fake_run):
        yield


# ---------------------------------------------------------------------------
# Authentication is required on every API route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, url, body",
    [
        ("post", "/api/run", {"root": "/tmp", "files": ["a.py"], "task": "t"}),
        ("post", "/api/resume/abc", {"decision": True}),
        ("get", "/api/status/abc", None),
        ("get", "/api/metrics", None),
        ("post", "/api/clear/abc", None),
        ("post", "/api/llm", {"prompt": "hi"}),
    ],
)
def test_api_routes_require_token(client, method, url, body):
    res = getattr(client, method)(url, json=body) if body is not None else getattr(client, method)(url)
    assert res.status_code == 401


def test_invalid_token_is_rejected(client):
    res = client.get("/api/metrics", headers={"Authorization": "Bearer not-a-real-token"})
    assert res.status_code == 401


def test_login_token_grants_access(client, alice):
    res = client.get("/api/metrics", headers=_auth(alice))
    assert res.status_code == 200
    assert res.json() == {"metrics": []}


def test_logout_invalidates_token(client, alice):
    assert client.post("/api/logout", headers=_auth(alice)).status_code == 200
    assert client.get("/api/metrics", headers=_auth(alice)).status_code == 401


def test_inactive_user_cannot_login(client, tmp_path):
    res = client.post(
        "/api/register",
        json={"name": "Pending", "uname": "pending_user", "upass": "password123", "folder": str(tmp_path)},
    )
    assert res.status_code in (200, 409)
    res = client.post("/api/login", json={"uname": "pending_user", "upass": "password123"})
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# Folder restriction
# ---------------------------------------------------------------------------


def test_run_rejects_root_outside_registered_folder(client, alice, bob, no_agent):
    payload = {"root": str(bob["folder_path"]), "files": ["*"], "task": "do it"}
    res = client.post("/api/run", json=payload, headers=_auth(alice))
    assert res.status_code == 403
    assert "registered folder" in res.json()["detail"]


def test_run_rejects_parent_traversal_out_of_folder(client, alice, no_agent):
    root = str(alice["folder_path"] / ".." / "bob")
    res = client.post("/api/run", json={"root": root, "files": ["*"], "task": "t"}, headers=_auth(alice))
    assert res.status_code in (400, 403)


def test_run_accepts_root_inside_registered_folder(client, alice, no_agent):
    payload = {"root": str(alice["folder_path"]), "files": ["hello.py"], "task": "add a docstring"}
    res = client.post("/api/run", json=payload, headers=_auth(alice))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["matched_files"] == ["hello.py"]

    status = client.get(f"/api/status/{body['thread_id']}", headers=_auth(alice))
    assert status.status_code == 200
    assert status.json()["status"] == "complete"


# ---------------------------------------------------------------------------
# Thread ownership
# ---------------------------------------------------------------------------


def test_other_user_cannot_see_or_clear_thread(client, alice, bob, no_agent):
    payload = {"root": str(alice["folder_path"]), "files": ["hello.py"], "task": "t"}
    thread_id = client.post("/api/run", json=payload, headers=_auth(alice)).json()["thread_id"]

    assert client.get(f"/api/status/{thread_id}", headers=_auth(bob)).status_code == 404
    assert client.post(f"/api/resume/{thread_id}", json={"decision": True}, headers=_auth(bob)).status_code == 404
    assert client.post(f"/api/clear/{thread_id}", headers=_auth(bob)).status_code == 404

    # The owner can still clear it.
    assert client.post(f"/api/clear/{thread_id}", headers=_auth(alice)).status_code == 200
