"""Phase 4: dashboard cookie-auth defense.

Verifies:
  - missing cookie redirects to /login on a full page request
  - missing cookie returns 401 on an HTMX request
  - correct cookie passes
  - wrong cookie still bounces
  - login POST with right token sets the cookie
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.dashboard_auth_token", "shh-secret")
    return TestClient(app, follow_redirects=False)


def test_no_cookie_redirects_to_login(client):
    resp = client.get("/dashboard/proposals/pending")
    assert resp.status_code in (307, 303)
    assert resp.headers["location"].endswith("/dashboard/login")


def test_no_cookie_returns_401_for_htmx(client):
    resp = client.get("/dashboard/proposals/pending", headers={"HX-Request": "true"})
    assert resp.status_code == 401


def test_correct_cookie_passes(client):
    client.cookies.set("ta_dashboard", "shh-secret")
    resp = client.get("/dashboard/system")
    assert resp.status_code == 200
    assert b"Kill Switch" in resp.content


def test_wrong_cookie_redirects(client):
    client.cookies.set("ta_dashboard", "wrong")
    resp = client.get("/dashboard/system")
    assert resp.status_code in (307, 303)


def test_login_post_sets_cookie(client):
    resp = client.post("/dashboard/login", data={"token": "shh-secret"})
    assert resp.status_code in (303, 307)
    set_cookie = resp.headers.get("set-cookie", "")
    assert "ta_dashboard" in set_cookie


def test_login_wrong_token_no_cookie(client):
    resp = client.post("/dashboard/login", data={"token": "nope"})
    assert resp.status_code == 401
    assert "ta_dashboard" not in resp.headers.get("set-cookie", "")


def test_logout_clears_cookie(client):
    client.cookies.set("ta_dashboard", "shh-secret")
    resp = client.get("/dashboard/logout")
    assert resp.status_code in (303, 307)
    set_cookie = resp.headers.get("set-cookie", "")
    # FastAPI delete_cookie sets Max-Age=0 (or expires in the past).
    assert "ta_dashboard" in set_cookie


def test_empty_configured_token_blocks_everything(client, monkeypatch):
    """If DASHBOARD_AUTH_TOKEN isn't set, nobody gets in — even with a cookie."""
    monkeypatch.setattr("app.core.config.settings.dashboard_auth_token", "")
    client.cookies.set("ta_dashboard", "anything")
    resp = client.get("/dashboard/proposals/pending")
    assert resp.status_code in (307, 303, 401)
