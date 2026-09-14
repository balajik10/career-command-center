import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from career_radar.integrations.google import TOKEN_URL, USERINFO_URL, gmail_access_token
from career_radar.settings import Settings

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def settings(**values):
    return Settings(_env_file=None, **values)


def test_private_config_modes_and_budget():
    s = settings()
    assert not s.tracker_enabled and s.zero_cost_mode
    assert s.budget_state(NOW) == "BLOCKED_BUDGET_UNKNOWN"
    s.budget_verified_at = NOW
    s.included_private_minutes_remaining = 2000
    assert s.budget_state(NOW) == "BLOCKED_PAID_OVERAGE"
    s.paid_overage_disabled = True
    assert s.budget_state(NOW) == "READY"
    s.included_private_minutes_remaining = 100
    assert s.budget_state(NOW) == "BLOCKED_BUDGET_INSUFFICIENT"
    s.budget_verified_at = NOW - timedelta(days=8)
    assert s.budget_state(NOW) == "BLOCKED_BUDGET_UNKNOWN"
    with pytest.raises(ValidationError):
        settings(github_actions=True, gmail_auth_mode="app_password")
    with pytest.raises(ValidationError):
        settings(gmail_auth_mode="oauth", gmail_app_password="secret")
    with pytest.raises(ValidationError):
        settings(gmail_auth_mode="app_password", gmail_send_refresh_token="secret")
    with pytest.raises(ValidationError):
        settings(gmail_auth_mode="app_password")
    with pytest.raises(ValidationError):
        settings(budget_verified_at=datetime(2026, 1, 1))


def test_required_private_values(tmp_path: Path):
    s = settings()
    with pytest.raises(ValueError):
        s.recipient()
    with pytest.raises(ValueError):
        s.hmac_key()
    with pytest.raises(ValueError):
        s.load_bootstrap_profile()
    s.alert_recipient_email = SecretStr("candidate@example.com")
    s.privacy_hmac_key = SecretStr("a" * 32)
    assert s.recipient() == "candidate@example.com"
    assert len(s.hmac_key()) == 32
    data = "name: Example Candidate\n"
    s.private_profile_yaml_b64 = SecretStr(base64.b64encode(data.encode()).decode())
    assert s.load_bootstrap_profile().name == "Example Candidate"
    s.private_profile_yaml_b64 = SecretStr("")
    s.private_profile_path = tmp_path / "profile.yaml"
    s.private_profile_path.write_text(data)
    assert s.load_bootstrap_profile().name == "Example Candidate"


@pytest.mark.parametrize("purpose,scope", [("read", "readonly"), ("send", "send")])
def test_independent_oauth_and_identity(purpose, scope):
    values = {
        f"gmail_{purpose}_{k}": "fake" for k in ["client_id", "client_secret", "refresh_token"]
    }
    s = settings(gmail_auth_mode="oauth", gmail_address="candidate@example.com", **values)
    calls = []

    def handle(request):
        calls.append(str(request.url))
        if str(request.url) == TOKEN_URL:
            return httpx.Response(
                200,
                json={
                    "access_token": "fake",
                    "scope": f"openid email https://www.googleapis.com/auth/gmail.{scope}",
                },
            )
        assert str(request.url) == USERINFO_URL
        return httpx.Response(200, json={"email": "candidate@example.com", "email_verified": True})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert gmail_access_token(s, purpose, client) == ("fake", "candidate@example.com")
    assert len(calls) == 2


@pytest.mark.parametrize(
    "kind", ["mode", "missing", "http", "scope", "empty", "identity_http", "identity_wrong"]
)
def test_oauth_fails_closed(kind):
    values = {f"gmail_send_{k}": "fake" for k in ["client_id", "client_secret", "refresh_token"]}
    s = settings(gmail_auth_mode="oauth", gmail_address="candidate@example.com", **values)
    if kind == "mode":
        s.gmail_auth_mode = "disabled"
    if kind == "missing":
        s.gmail_send_refresh_token = SecretStr("")

    def handle(request):
        if str(request.url) == TOKEN_URL:
            return httpx.Response(
                401 if kind == "http" else 200,
                json={
                    "access_token": "" if kind == "empty" else "fake",
                    "scope": "https://mail.google.com/"
                    if kind == "scope"
                    else "openid email https://www.googleapis.com/auth/gmail.send",
                },
            )
        return httpx.Response(
            401 if kind == "identity_http" else 200,
            json={"email": "invalid@example.com", "email_verified": False},
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, pytest.raises(ValueError):
        gmail_access_token(s, "send", client)
