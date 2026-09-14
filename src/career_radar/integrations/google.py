"""Separate least-privilege Gmail grants. Refresh/identity validation never sends email."""

from __future__ import annotations

from typing import Any, Literal

import httpx

from career_radar.settings import Settings

GMAIL_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
# This is the public OAuth exchange endpoint, not a credential.
TOKEN_URL = "https://oauth2.googleapis.com/token"  # nosec B105
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def normalize_scope(scope: str) -> str:
    return "email" if scope == "https://www.googleapis.com/auth/userinfo.email" else scope


def gmail_access_token(
    settings: Settings, purpose: Literal["read", "send"], client: httpx.Client | None = None
) -> tuple[str, str]:
    if settings.gmail_auth_mode != "oauth":
        raise ValueError("GMAIL_OAUTH_REQUIRED")
    prefix = f"gmail_{purpose}_"
    values = {
        k: getattr(settings, prefix + k).get_secret_value()
        for k in ("client_id", "client_secret", "refresh_token")
    }
    if not all(values.values()):
        raise ValueError(f"GMAIL_{purpose.upper()}_CREDENTIALS_REQUIRED")
    values["grant_type"] = "refresh_token"
    with (
        httpx.Client(timeout=20, follow_redirects=False)
        if client is None
        else _borrow(client) as http
    ):
        result = http.post(TOKEN_URL, data=values)
        if result.status_code != 200:
            raise ValueError(f"GMAIL_{purpose.upper()}_AUTH_REQUIRED")
        payload = result.json()
        expected = {
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail."
            + ("readonly" if purpose == "read" else "send"),
        }
        actual = {normalize_scope(s) for s in str(payload.get("scope", "")).split()}
        if actual != expected:
            raise ValueError(f"GMAIL_{purpose.upper()}_SCOPE_MISMATCH")
        token = str(payload.get("access_token", ""))
        if not token:
            raise ValueError("GMAIL_EMPTY_ACCESS_TOKEN")
        identity = http.get(USERINFO_URL, headers={"Authorization": f"Bearer {token}"})
        if identity.status_code != 200:
            raise ValueError("GMAIL_IDENTITY_UNVERIFIED")
        claims = identity.json()
        email = str(claims.get("email", "")).strip().lower()
        if (
            claims.get("email_verified") is not True
            or not email
            or email != settings.gmail_address.get_secret_value().strip().lower()
        ):
            raise ValueError("GMAIL_IDENTITY_MISMATCH")
    return token, email


class _borrow:
    def __init__(self, client: httpx.Client):
        self.client = client

    def __enter__(self) -> httpx.Client:
        return self.client

    def __exit__(self, *args: Any) -> None:
        return None
