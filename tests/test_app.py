import os
import io
import json
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

# Import the running FastAPI app
from main import app, db

client = TestClient(app)


def make_email(prefix: str = "user") -> str:
    return f"{prefix}-{datetime.utcnow().timestamp()}@example.com"


def create_client_profile():
    # Create a client via API to ensure consistent data
    email = make_email("client")
    res = client.post("/clients", json={
        "email": email,
        "name": "Test Client",
        "company": "Test Co",
        "theme_color": "#22c55e",
        "logo_url": None,
    })
    assert res.status_code == 200, res.text
    ids = res.json()
    # Fetch profile
    prof = client.get(f"/clients/{ids['profile_id']}")
    assert prof.status_code == 200
    return prof.json()


def test_health_and_db():
    r = client.get("/test")
    assert r.status_code == 200
    data = r.json()
    assert "backend" in data


def test_otp_flow_creates_token():
    email = make_email("otp")
    # Request OTP
    r = client.post("/auth/request-otp", json={"email": email, "name": "OTP User", "role": "client"})
    assert r.status_code == 200
    # Retrieve code from DB directly (demo env)
    rec = db["otp"].find_one({"email": email})
    assert rec and rec.get("code")
    code = rec["code"]
    # Verify OTP
    v = client.post("/auth/verify-otp", json={"email": email, "code": code})
    assert v.status_code == 200, v.text
    payload = v.json()
    assert "token" in payload and payload["token"]
    assert "user" in payload and payload["user"]


def test_basic_login_and_me():
    email = make_email("login")
    r = client.post("/auth/login", json={"email": email, "name": "Login User", "role": "admin"})
    assert r.status_code == 200
    tok = r.json()["token"]
    me = client.get(f"/auth/me?token={tok}")
    assert me.status_code == 200
    user = me.json()
    assert user["email"] == email


def test_kanban_create_and_move():
    prof = create_client_profile()
    headers = {"X-User-Role": "project_manager"}
    # Create task
    r = client.post("/kanban/tasks", headers=headers, json={
        "client_id": prof["id"],
        "title": "Install scaffolding",
        "description": "North wall",
        "assignees": [],
        "due_date": None
    })
    assert r.status_code == 200, r.text
    task_id = r.json()["id"]

    # Move task to in_progress (append)
    m = client.post(f"/kanban/tasks/{task_id}/move", headers=headers, json={
        "to_status": "in_progress",
        "before_id": None,
        "after_id": None
    })
    assert m.status_code == 200, m.text
    moved = m.json()
    assert moved["status"] == "in_progress"


def test_logo_upload_and_patch_branding():
    prof = create_client_profile()
    headers = {"X-User-Role": "admin"}

    # Upload a tiny PNG header
    fake_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + os.urandom(16)
    files = {"file": ("logo.png", io.BytesIO(fake_png), "image/png")}
    up = client.post(f"/clients/{prof['id']}/logo", headers=headers, files=files)
    assert up.status_code == 200, up.text
    url = up.json()["url"]
    assert url.startswith("/uploads/")

    # Patch branding
    new_color = "#f59e0b"
    patch = client.patch(f"/clients/{prof['id']}", headers=headers, json={
        "display_name": "Branded Co",
        "theme_color": new_color,
        "logo_url": url,
        "notes": "Updated via tests"
    })
    assert patch.status_code == 200, patch.text
    body = patch.json()
    assert body["theme_color"] == new_color
    assert body["logo_url"] == url


def test_role_enforcement_for_kanban():
    prof = create_client_profile()
    # Attempt to create task as read-only viewer should fail
    r = client.post("/kanban/tasks", headers={"X-User-Role": "viewer"}, json={
        "client_id": prof["id"],
        "title": "Unauthorized",
        "description": "",
        "assignees": [],
        "due_date": None
    })
    assert r.status_code == 403
