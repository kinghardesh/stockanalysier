"""Single-token cookie auth for the dashboard.

The token is set in .env (DASHBOARD_AUTH_TOKEN). The /dashboard/login form
takes that token; on match we set a cookie that subsequent requests check.
Intended for local-only / single-user use — not a hardened auth system.
"""
import hmac
import secrets
from typing import Optional

from fastapi import Cookie, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.core.config import settings

COOKIE_NAME = "ta_dashboard"
COOKIE_MAX_AGE = 60 * 60 * 12  # 12h


def _expected_token() -> str:
    return settings.dashboard_auth_token or ""


def issue_cookie_value() -> str:
    """Cookie value is the configured token itself; rotate by changing .env."""
    return _expected_token()


def is_authed(request: Request) -> bool:
    expected = _expected_token()
    if not expected:
        return False  # if token isn't configured, everyone is unauthed
    presented = request.cookies.get(COOKIE_NAME, "")
    return hmac.compare_digest(presented, expected)


def require_auth(request: Request) -> None:
    if not is_authed(request):
        # For HTMX requests, return 401 so the partial swap can detect it.
        # For full-page nav, redirect to /dashboard/login.
        if request.headers.get("HX-Request") == "true":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/dashboard/login"},
        )


def verify_token(supplied: str) -> bool:
    expected = _expected_token()
    if not expected:
        return False
    return hmac.compare_digest(supplied or "", expected)
