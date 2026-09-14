from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from career_radar import runtime
from career_radar.demo import demo_book
from career_radar.domain import FetchResult, RawJob, SourceDefinition
from career_radar.integrations.google import TOKEN_URL, USERINFO_URL, gmail_access_token
from career_radar.settings import Settings
from career_radar.sheets import FakeWorkbook

NOW = datetime(2026, 9, 14, 6, tzinfo=UTC)
KEY = "test" * 8  # Synthetic HMAC key; never a credential.


@pytest.fixture(autouse=True)
def isolated_config_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each case declares its execution identity and credentials, including direct
    # Settings constructors used to exercise local-only SMTP validation.
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)


def settings(**overrides: Any) -> Settings:
    values = {
        "_env_file": None,
        "alert_recipient_email": "candidate@example.com",
        "privacy_hmac_key": KEY,
        "google_sheet_id": "synthetic-sheet",
        "gmail_auth_mode": "oauth",
        "gmail_address": "candidate@example.com",
        "gmail_oauth_in_production": True,
        "gmail_send_client_id": "synthetic-client",
        "gmail_send_client_secret": "synthetic-secret",
        "gmail_send_refresh_token": "synthetic-refresh",
        "budget_verified_at": NOW,
        "included_private_minutes_remaining": 10000,
        "paid_overage_disabled": True,
        "scheduler": "github",
    }
    return Settings(**(values | overrides))


def test_readiness_separates_read_write_send_and_optional_ingestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = demo_book()
    monkeypatch.setattr(runtime, "live_book", lambda *args: book)
    monkeypatch.setattr(
        runtime, "gmail_access_token", lambda *args: ("token", "candidate@example.com")
    )
    before = book.write_requests
    result = runtime.readiness(settings(), now=NOW)
    assert result["sheet_read_write"] == "READ_VERIFIED_WRITE_NOT_PROBED"
    assert result["scheduled_run"] == "BLOCKED_WRITE_VERIFICATION_REQUIRED"
    assert result["gmail_ingestion"] == "CONNECTOR_DISABLED"
    assert book.write_requests == before
    result = runtime.readiness(
        settings(gmail_read_refresh_token="read-token"), now=NOW, verify_write=True
    )
    assert result["sheet_read_write"] == "READ_WRITE_VERIFIED"
    assert result["scheduled_run"] == "READY"
    assert result["gmail_ingestion"] == "TOKEN_VERIFIED_CONNECTOR_DISABLED"
    assert "candidate@example.com" not in json.dumps(result)
    assert "synthetic-sheet" not in json.dumps(result)
    assert (
        runtime.readiness(settings(scheduler="disabled"), now=NOW, verify_write=True)[
            "scheduled_run"
        ]
        == "BLOCKED_SCHEDULER_NOT_SELECTED"
    )
    assert (
        runtime.readiness(settings(zero_cost_mode=False), now=NOW, verify_write=True)[
            "scheduled_run"
        ]
        == "BLOCKED_ZERO_COST_MODE_DISABLED"
    )
    assert (
        runtime.readiness(settings(gmail_oauth_in_production=False), now=NOW)["email_send"]
        == "DEMO_ONLY_OAUTH_TESTING"
    )
    assert (
        runtime.readiness(settings(), now=NOW, verify_live=False)["sheet_read_write"]
        == "SETUP_REQUIRED"
    )


def test_readiness_missing_credentials_and_failed_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "live_book", lambda *args: FakeWorkbook())

    def invalid(*args: Any) -> Any:
        raise ValueError("AUTH_FAILURE")

    monkeypatch.setattr(runtime, "gmail_access_token", invalid)
    result = runtime.readiness(
        settings(privacy_hmac_key="", gmail_read_refresh_token="read-token"), now=NOW
    )
    assert result["private_identity"] == "SETUP_REQUIRED"
    assert result["sheet_read_write"] == "SCHEMA_REPAIR_REQUIRED"
    assert result["email_send"] == "AUTH_OR_SCOPE_OR_IDENTITY_FAILURE"
    monkeypatch.setattr(runtime, "live_book", invalid)
    assert runtime.readiness(settings(), now=NOW)["sheet_read_write"] == "AUTH_OR_SHEET_UNAVAILABLE"
    assert (
        runtime.readiness(settings(google_sheet_id="", gmail_auth_mode="disabled"), now=NOW)[
            "scheduled_run"
        ]
        == "BLOCKED_CREDENTIALS"
    )
    assert (
        runtime.readiness(
            Settings(
                _env_file=None, gmail_auth_mode="app_password", dedicated_mailbox_attested=True
            ),
            now=NOW,
        )["email_send"]
        == "LOCAL_SMTP_AUTH_NOT_PROBED"
    )


def test_read_only_setup_readiness_does_not_touch_sender_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = demo_book()
    monkeypatch.setattr(runtime, "live_book", lambda *args: book)

    def forbidden(*args: Any) -> None:
        raise AssertionError("Read-only setup must not refresh sender credentials")

    monkeypatch.setattr(runtime, "gmail_access_token", forbidden)
    writes = book.write_requests
    checked = runtime.readiness(settings(), verify_write=False, verify_email=False, now=NOW)
    assert checked["sheet_read_write"] == "READ_VERIFIED_WRITE_NOT_PROBED"
    assert checked["email_send"] == "SETUP_REQUIRED" and book.write_requests == writes


def test_sheet_write_probe_restores_original_cell_and_business_state() -> None:
    book = demo_book()
    marker = next(row for row in book.tabs["_System_State"] if row["key"] == "project_marker")
    marker["updated_at"] = "original-marker-value"
    before = json.dumps(book.tabs, sort_keys=True)
    writes = book.write_requests
    runtime.verify_sheet_write(book)
    assert book.write_requests == writes + 2
    assert json.dumps(book.tabs, sort_keys=True) == before
    book.fail_on_write = book.write_requests + 1
    with pytest.raises(RuntimeError, match="INJECTED_WRITE_FAILURE"):
        runtime.verify_sheet_write(book)
    assert json.dumps(book.tabs, sort_keys=True) == before


def test_write_probe_readback_and_restore_failures_are_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = demo_book()
    actual_read = book.read_tab
    calls = 0

    def stale_read(name: str) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        rows = actual_read(name)
        if calls == 2:
            rows[0]["updated_at"] = "unexpected"
        return rows

    monkeypatch.setattr(book, "read_tab", stale_read)
    with pytest.raises(ValueError, match="WRITE_PROBE_READBACK_FAILED"):
        runtime.verify_sheet_write(book)
    assert actual_read("_System_State")[0].get("updated_at", "") == ""
    monkeypatch.setattr(book, "read_tab", actual_read)
    book.fail_on_write = book.write_requests + 2
    monkeypatch.setattr(runtime, "live_book", lambda *args: book)
    checked = runtime.readiness(settings(), verify_write=True, verify_email=False, now=NOW)
    assert checked["sheet_read_write"] == "AUTH_OR_SHEET_UNAVAILABLE"


def test_manual_doctor_requires_real_federated_credential_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "wif.json"
    good = {
        "type": "external_account",
        "audience": "//iam.googleapis.com/projects/123/locations/global/workloadIdentityPools/test/providers/test",
        "service_account_impersonation_url": "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/test@example.iam.gserviceaccount.com:generateAccessToken",
    }
    path.write_text(json.dumps(good))
    monkeypatch.setenv("SHEETS_AUTH_MODE", "wif")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))
    monkeypatch.setenv("WIF_PROVIDER", good["audience"].removeprefix("//iam.googleapis.com/"))
    monkeypatch.setenv("WIF_SERVICE_ACCOUNT", "test@example.iam.gserviceaccount.com")
    config = settings(github_actions=True)
    runtime.require_github_wif(config)
    for modified in (
        {"type": "authorized_user"},
        {"audience": "wrong"},
        {"service_account_impersonation_url": ""},
    ):
        path.write_text(json.dumps(good | modified))
        with pytest.raises(ValueError, match="DOCTOR_REQUIRES_GITHUB_WIF"):
            runtime.require_github_wif(config)
    for body in ("[]", "not-json", "x" * 100_001):
        path.write_text(body)
        with pytest.raises(ValueError, match="DOCTOR_REQUIRES_GITHUB_WIF"):
            runtime.require_github_wif(config)
    path.unlink()
    with pytest.raises(ValueError, match="DOCTOR_REQUIRES_GITHUB_WIF"):
        runtime.require_github_wif(config)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS")
    with pytest.raises(ValueError, match="DOCTOR_REQUIRES_GITHUB_WIF"):
        runtime.require_github_wif(config)
    for modified in (
        settings(github_actions=False),
        settings(github_actions=True, google_service_account_json_b64="synthetic"),
    ):
        with pytest.raises(ValueError, match="DOCTOR_REQUIRES_GITHUB_WIF"):
            runtime.require_github_wif(modified)
    monkeypatch.setenv("SHEETS_AUTH_MODE", "key")
    with pytest.raises(ValueError, match="DOCTOR_REQUIRES_GITHUB_WIF"):
        runtime.require_github_wif(config)


def test_google_credentials_and_sheet_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    monkeypatch.setattr(runtime, "google_authenticated_session", lambda: sentinel)
    assert runtime.google_session(settings()) is sentinel
    calls: list[Any] = []
    monkeypatch.setattr(
        runtime.service_account.Credentials,
        "from_service_account_info",
        lambda data, **kwargs: calls.append((data, kwargs)) or sentinel,
    )
    monkeypatch.setattr(runtime, "AuthorizedSession", lambda credentials: credentials)
    encoded = base64.b64encode(b'{"type":"service_account"}').decode()
    assert runtime.google_session(settings(google_service_account_json_b64=encoded)) is sentinel
    assert calls[0][1]["scopes"] == ["https://www.googleapis.com/auth/spreadsheets"]
    with pytest.raises(ValueError):
        runtime.google_session(settings(google_service_account_json_b64="invalid!"))
    monkeypatch.setattr(runtime, "google_session", lambda config: sentinel)
    book = runtime.live_book(settings(), "incremental")
    assert book.max_read_requests == 20 and book.max_write_requests == 20
    with pytest.raises(ValueError, match="SETUP_REQUIRED"):
        runtime.live_book(settings(google_sheet_id=""))


def token_client(
    *,
    token_status: int = 200,
    scope: str | None = None,
    token: str = "access",
    identity_status: int = 200,
    email: str = "candidate@example.com",
    verified: bool = True,
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(
                token_status,
                json={
                    "access_token": token,
                    "scope": scope
                    if scope is not None
                    else "openid email https://www.googleapis.com/auth/gmail.send",
                },
            )
        assert str(request.url) == USERINFO_URL
        assert request.headers["Authorization"] == f"Bearer {token}"
        return httpx.Response(identity_status, json={"email": email, "email_verified": verified})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_oauth_exact_scope_and_verified_authenticated_sender() -> None:
    with token_client() as client:
        assert gmail_access_token(settings(), "send", client) == ("access", "candidate@example.com")
        assert not client.is_closed
    with token_client(
        scope="openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/gmail.readonly"
    ) as client:
        assert (
            gmail_access_token(
                settings(
                    gmail_read_client_id="read",
                    gmail_read_client_secret="secret",
                    gmail_read_refresh_token="token",
                ),
                "read",
                client,
            )[1]
            == "candidate@example.com"
        )
    for options, error in (
        ({"token_status": 401}, "AUTH_REQUIRED"),
        ({"scope": "openid email https://mail.google.com/"}, "SCOPE_MISMATCH"),
        ({"token": ""}, "EMPTY_ACCESS_TOKEN"),
        ({"identity_status": 401}, "IDENTITY_UNVERIFIED"),
        ({"verified": False}, "IDENTITY_MISMATCH"),
        ({"email": "different@example.com"}, "IDENTITY_MISMATCH"),
    ):
        with token_client(**options) as client, pytest.raises(ValueError, match=error):
            gmail_access_token(settings(), "send", client)
    with pytest.raises(ValueError, match="OAUTH_REQUIRED"):
        gmail_access_token(settings(gmail_auth_mode="disabled"), "send")
    with pytest.raises(ValueError, match="CREDENTIALS_REQUIRED"):
        gmail_access_token(settings(gmail_send_refresh_token=""), "send")


def test_live_scan_source_isolation_and_backoff_propagation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = demo_book()
    sources = [
        SourceDefinition(
            source_id=identity,
            company="Example",
            provider="greenhouse",
            url="https://example.com/jobs",
            enabled=True,
            policy_state="APPROVED",
            cost_class="free",
            access_mode="OFFICIAL_API",
        )
        for identity in ("healthy", "bad", "backoff")
    ]
    book.transaction(
        {
            "Sources": [
                {**source.model_dump(mode="json"), "model_json": source.model_dump_json()}
                for source in sources
            ]
        },
        "seed",
    )
    monkeypatch.setattr(runtime, "live_book", lambda *args: book)

    def collect(source: SourceDefinition, budget: Any) -> Any:
        if source.source_id == "bad":
            raise ValueError("private exception details")
        return FetchResult(
            source_id=source.source_id,
            url=source.url,
            outcome="BACKOFF" if source.source_id == "backoff" else "SUCCESS_EMPTY",
            snapshot_complete=source.source_id == "healthy",
            headers={"retry-after": "120"},
        ), []

    monkeypatch.setattr(runtime, "collect", collect)
    result = runtime.scan_live(settings(), mode="incremental", dry_run=False, send_alerts=False)
    assert result.summary.source_counts == {"FAILED_TRANSIENT": 1, "BACKOFF": 1, "SUCCESS_EMPTY": 1}
    backoff = next(row for row in book.tabs["Sources"] if row["source_id"] == "backoff")
    assert backoff["backoff_until"]
    assert "private exception" not in str(result.summary)
    with pytest.raises(ValueError, match="DRY_RUN"):
        runtime.scan_live(settings(), mode="incremental", dry_run=True, send_alerts=True)
    with pytest.raises(ValueError, match="UNKNOWN_SOURCE"):
        runtime.scan_live(
            settings(), mode="incremental", dry_run=True, send_alerts=False, source_id="unknown"
        )


def test_configured_source_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    book = demo_book()
    monkeypatch.setattr(runtime, "live_book", lambda *args: book)
    assert runtime.configured_sources(settings())[1] == "PRIVATE_SHEET"
    rows, origin = runtime.configured_sources(settings(google_sheet_id=""))
    assert origin == "BOOTSTRAP_CATALOGUE_READ_ONLY" and rows
    assert not any(source.enabled for source in rows)


def test_local_smtp_uses_authenticated_sender_and_explicit_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import smtplib

    calls: list[Any] = []

    class Connection:
        def login(self, sender: str, password: str) -> None:
            calls.append((sender, password))

        def close(self) -> None:
            calls.append("closed")

    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *args, **kwargs: Connection())
    config = Settings(
        _env_file=None,
        gmail_auth_mode="app_password",
        dedicated_mailbox_attested=True,
        gmail_address="candidate@example.com",
        gmail_app_password="synthetic-app-password",
        alert_recipient_email="candidate@example.com",
        privacy_hmac_key=KEY,
    )
    outbox = runtime.live_outbox(demo_book(), config)  # type: ignore[arg-type]
    assert outbox.transport and outbox.transport.name == "smtp"
    assert calls == [("candidate@example.com", "synthetic-app-password")]
    with pytest.raises(ValueError, match="APP_PASSWORD_REQUIRED"):
        runtime.live_outbox(
            demo_book(),
            config.model_copy(update={"gmail_app_password": config.gmail_read_refresh_token}),
        )  # type: ignore[arg-type]

    class Broken(Connection):
        def login(self, sender: str, password: str) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"private provider error")

    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *args, **kwargs: Broken())
    with pytest.raises(ValueError, match="SMTP_AUTH_REQUIRED"):
        runtime.live_outbox(demo_book(), config)  # type: ignore[arg-type]
    assert calls[-1] == "closed"


@pytest.mark.parametrize("retry_header", ["relative", "absolute", "missing"])
def test_collection_receipt_clock_preserves_new_jobs_and_full_retry_after(
    monkeypatch: pytest.MonkeyPatch, retry_header: str
) -> None:
    book = demo_book()
    started = NOW
    received = NOW + timedelta(minutes=2)

    class Clock(datetime):
        values = [started, received]

        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return cls.values.pop(0)

    monkeypatch.setattr(runtime, "datetime", Clock)
    sources = [
        SourceDefinition(
            source_id=name,
            company="Example",
            provider="greenhouse",
            url="https://example.com/jobs",
            enabled=True,
            policy_state="APPROVED",
            cost_class="free",
            access_mode="OFFICIAL_API",
        )
        for name in ("fresh", "backoff")
    ]
    book.transaction(
        {
            "Sources": [
                {**source.model_dump(mode="json"), "model_json": source.model_dump_json()}
                for source in sources
            ]
        },
        "seed",
    )
    monkeypatch.setattr(runtime, "live_book", lambda *args: book)

    def collect(source: SourceDefinition, budget: Any) -> Any:
        if source.source_id == "backoff":
            headers = (
                {
                    "retry-after": "120"
                    if retry_header == "relative"
                    else format_datetime(received + timedelta(seconds=120))
                }
                if retry_header != "missing"
                else {}
            )
            return FetchResult(
                source_id="backoff",
                url=source.url,
                fetched_at=received,
                outcome="BACKOFF",
                snapshot_complete=False,
                headers=headers,
            ), []
        posting = RawJob(
            source_id="fresh",
            provider="greenhouse",
            provider_job_id="1",
            title="Backend Engineer",
            company="Example",
            location="Bengaluru, India",
            description="Develop Java and Redis APIs. Required 0-2 years of software experience.",
            url="https://example.com/jobs/1",
            apply_url="https://example.com/jobs/1/apply",
            official_link_state="VERIFIED_OFFICIAL",
            posted_at_raw=(received - timedelta(seconds=30)).isoformat(),
            fetched_at=received,
        )
        return FetchResult(source_id="fresh", url=source.url, fetched_at=received), [posting]

    monkeypatch.setattr(runtime, "collect", collect)
    result = runtime.scan_live(settings(), mode="incremental", dry_run=False, send_alerts=False)
    assert not result.jobs[0].quarantine_reason and result.jobs[0].age_hours_max == 30 / 3600
    assert result.jobs[0].first_seen_at == received
    stored = next(row for row in book.tabs["Sources"] if row["source_id"] == "backoff")
    expected = received + timedelta(seconds=60 if retry_header == "missing" else 120)
    assert datetime.fromisoformat(stored["backoff_until"].replace("Z", "+00:00")) == expected
