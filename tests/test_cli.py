from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from career_radar import cli
from career_radar.contacts import import_contacts
from career_radar.demo import demo_book, run_demo
from career_radar.domain import Contact, Provenance
from career_radar.notifications import FakeTransport, Outbox
from career_radar.settings import Settings

RUNNER = CliRunner()
KEY = "test" * 8  # Synthetic HMAC key; never a credential.
NOW = datetime(2026, 9, 14, tzinfo=UTC)


def settings(**overrides: Any) -> Settings:
    return Settings(
        **(
            {
                "_env_file": None,
                "privacy_hmac_key": KEY,
                "alert_recipient_email": "candidate@example.com",
            }
            | overrides
        )
    )


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    monkeypatch.delenv("CAREER_SCHEDULED_RUN", raising=False)
    monkeypatch.setattr(cli, "Settings", lambda: settings())


def invoke(args: list[str], *, expected: int = 0) -> dict[str, Any]:
    result = RUNNER.invoke(cli.app, args)
    assert result.exit_code == expected, (result.stdout, result.exception)
    return json.loads(result.stdout)


def test_documented_offline_commands_and_help(tmp_path: Path) -> None:
    assert RUNNER.invoke(cli.app, ["--help"]).exit_code == 0
    assert invoke(["version"])["version"]
    assert invoke(["doctor", "--json"])["offline_demo"] == "READY"
    result = RUNNER.invoke(cli.app, ["doctor", "--require", "sheet"])
    assert result.exit_code == 1
    assert invoke(["bootstrap-sheet", "--offline"])["tabs"] == 28
    assert invoke(["sheets", "verify", "--offline"])["status"] == "PASS"
    summary = invoke(["scan", "--dry-run", "--offline-fixtures", "--json"])
    assert summary["observations"] == 12 and summary["canonical_jobs"] == 9
    assert summary["alerts"] == 2 and summary["data_mode"] == "SYNTHETIC"
    digest = invoke(["digest", "--period", "morning", "--no-send"])
    assert digest["send_attempted"] is False and digest["items"] == 4
    assert "P0" in digest["body"]
    assert invoke(["source", "check", "--all"])["sources"] > 0
    assert invoke(["source", "list", "--health-state", "DEGRADED"])["sources"] == []
    backup = invoke(["export", "backup", str(tmp_path / "backup"), "--offline"])
    assert Path(backup["file"]).exists()


@pytest.mark.parametrize(
    "args,code",
    [
        (["scan", "--mode", "invalid"], "INVALID_SCAN_MODE"),
        (["scan", "--dry-run", "--send-alerts"], "DRY_RUN_OR_FIXTURES_CANNOT_SEND"),
        (["scan", "--offline-fixtures", "--send-alerts"], "DRY_RUN_OR_FIXTURES_CANNOT_SEND"),
        (["digest", "--period", "invalid"], "INVALID_DIGEST_PERIOD"),
        (["bootstrap-sheet"], "SETUP_REQUIRED"),
        (
            ["notify", "test", "--recipient", "different@example.com"],
            "RECIPIENT_NOT_CONFIGURED_OWNER",
        ),
        (["import", "eml", "."], "GMAIL_SENDER_ALLOWLIST_REQUIRED"),
        (["privacy", "redact-contact", "unknown"], "SETUP_REQUIRED"),
    ],
)
def test_useful_failure_codes_without_credentials(args: list[str], code: str) -> None:
    assert invoke(args, expected=1)["error"] == code


def test_staged_contacts_and_jobs_are_private_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "contacts.csv"
    path.write_text(
        "First Name,Last Name,Company,Email Address\nExample,Person,Example,candidate@example.com\n"
    )
    first = invoke(["import", "contacts", str(path), "--format", "linkedin-connections"])
    second = invoke(["import", "contacts", str(path), "--format", "linkedin-connections"])
    assert first["status"] == "STAGED_PRIVATE_NOT_AUTHORITATIVE"
    # Parsed provenance fetch times can differ; identity remains stable inside
    # each staged dataset and the eventual canonical import performs the upsert.
    assert first["records"] == second["records"] == 1
    assert Path(first["staging_file"]).stat().st_mode & 0o777 == 0o600
    assert "candidate@example.com" not in json.dumps(first)
    jobs = tmp_path / "jobs.json"
    jobs.write_text(
        '[{"title":"Backend Engineer","company":"Example","url":"https://example.com/jobs/1"}]'
    )
    assert invoke(["import", "jobs", str(jobs)])["status"] == "STAGED_PRIVATE_NOT_AUTHORITATIVE"


def test_scheduled_no_send_logs_only_aggregates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    result = invoke(["digest", "--no-send", "--offline-fixtures"])
    assert "body" not in result
    assert "https://" not in json.dumps(result)
    assert result["send_attempted"] is False


def test_live_digest_stable_period_id_and_no_send_has_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = settings(google_sheet_id="private-sheet-id")
    book = demo_book()
    run_demo(book, dry_run=False)
    transport = FakeTransport()
    outbox = Outbox(book, config.recipient(), config.hmac_key(), transport)
    monkeypatch.setattr(cli, "Settings", lambda: config)
    monkeypatch.setattr(cli, "live_book", lambda *args: book)
    monkeypatch.setattr(cli, "live_outbox", lambda *args: outbox)
    before = book.write_requests
    assert invoke(["digest", "--no-send"])["send_attempted"] is False
    assert book.write_requests == before
    first = invoke(["digest", "--period", "morning"])
    assert first["status"] == "SENT"
    assert invoke(["digest", "--period", "morning"])["status"] == "NO_CONTENT_OR_ALREADY_ATTEMPTED"
    assert len(transport.messages) == 1
    assert any(
        row.get("scheduled_window_end", "").endswith("T02:41:00+00:00")
        for row in book.read_tab("_Alerts")
    )
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    output = invoke(["digest", "--no-send"])
    assert "body" not in output and "private-sheet-id" not in json.dumps(output)


def test_redaction_clears_original_and_secondary_data_and_blocks_reimport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = settings(google_sheet_id="synthetic")
    book = demo_book()
    path = tmp_path / "contacts.csv"
    path.write_text("Name,Company,Email\nExample Person,Example,candidate@example.com\n")
    contact = import_contacts(path, "csv", config.hmac_key(), NOW)[0]
    contact.notes = "Private original note"
    book.upsert("Contacts", "contact_uid", [contact.model_dump(mode="json")], "r", actor="user")
    book.upsert(
        "Outreach",
        "draft_id",
        [
            {
                "draft_id": "d1",
                "job_uid": "j1",
                "contact_uid": contact.contact_uid,
                "subject": "Example Person",
                "body": "Private draft to candidate@example.com",
            }
        ],
        "r",
    )
    book.upsert(
        "Job_Contacts",
        "job_contact_uid",
        [
            {
                "job_contact_uid": "jc1",
                "job_uid": "j1",
                "contact_uid": contact.contact_uid,
                "why_this_contact": "Example Person relationship",
            }
        ],
        "r",
    )
    book.upsert(
        "Jobs_Master", "job_uid", [{"job_uid": "j1", "best_contact": "Example Person"}], "r"
    )
    monkeypatch.setattr(cli, "Settings", lambda: config)
    monkeypatch.setattr(cli, "live_book", lambda *args: book)
    assert invoke(["privacy", "redact-contact", contact.contact_uid])["status"] == "REDACTED"
    contacts = book.read_tab("Contacts")
    assert len(contacts) == 1 and contacts[0]["contact_uid"] == contact.contact_uid
    assert (
        contacts[0]["do_not_contact"] is True
        and contacts[0]["suppression_token"] == contact.suppression_token
    )
    for tab in ("Contacts", "Outreach", "Job_Contacts", "Jobs_Master"):
        content = json.dumps(book.read_tab(tab))
        assert (
            "Example Person" not in content
            and "candidate@example.com" not in content
            and "Private original" not in content
        )
    assert invoke(["import", "contacts", str(path), "--format", "csv"])["records"] == 0
    assert len(book.read_tab("Contacts")) == 1
    assert (
        invoke(["privacy", "redact-contact", "unknown"], expected=1)["error"] == "CONTACT_NOT_FOUND"
    )


def test_expiry_redacts_only_expired_contacts(monkeypatch: pytest.MonkeyPatch) -> None:
    config = settings(google_sheet_id="synthetic")
    book = demo_book()
    old = Contact(
        contact_uid="old",
        name="Expired Contact",
        contact_origin="APPROVED_OFFICIAL_RECRUITING_PAGE",
        retention_expires_at=NOW - timedelta(days=1),
        field_provenance={"name": Provenance(source_id="example", evidence="Expired Contact")},
    )
    current = Contact(contact_uid="current", name="Current Contact", contact_origin="USER_ENTERED")
    book.upsert(
        "Contacts",
        "contact_uid",
        [old.model_dump(mode="json"), current.model_dump(mode="json")],
        "r",
        actor="user",
    )
    monkeypatch.setattr(cli, "Settings", lambda: config)
    monkeypatch.setattr(cli, "live_book", lambda *args: book)
    assert invoke(["privacy", "purge-expired"])["records_redacted"] == 1
    assert "Expired Contact" not in json.dumps(book.read_tab("Contacts"))
    assert book.read_tab("Contacts")[1]["name"] == "Current Contact"


def test_eml_import_ledger_only_after_success_and_no_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = settings(google_sheet_id="synthetic", gmail_sender_allowlist="candidate@example.com")
    book = demo_book()
    directory = tmp_path / "emails"
    directory.mkdir()
    (directory / "alert.eml").write_text(
        'From: candidate@example.com\nTo: candidate@example.com\nSubject: Synthetic job alert\nContent-Type: text/html; charset=utf-8\n\n<a href="https://example.com/jobs/123">Backend Engineer</a>'
    )
    monkeypatch.setattr(cli, "Settings", lambda: config)
    monkeypatch.setattr(cli, "live_book", lambda *args: book)
    first = invoke(["import", "eml", str(directory)])
    assert first["messages"] == 1 and first["status"] == "COMMITTED"
    assert len(book.read_tab("Inbox")) == 1
    assert invoke(["import", "eml", str(directory)])["messages"] == 0
    assert len(book.read_tab("Inbox")) == 1
    assert "candidate@example.com" not in json.dumps(book.read_tab("Inbox"))
    (directory / "new.eml").write_text((directory / "alert.eml").read_text().replace("123", "456"))
    book.fail_on_write = book.write_requests + 1
    assert RUNNER.invoke(cli.app, ["import", "eml", str(directory)]).exit_code == 1
    assert len(book.read_tab("Inbox")) == 1


def test_schedule_disabled_and_environment_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    assert invoke(["run-scheduled"])["status"] == "TRACKER_DISABLED"
    monkeypatch.setattr(cli, "Settings", lambda: settings(tracker_enabled=True, scheduler="local"))
    monkeypatch.setattr(cli, "readiness", lambda *args, **kwargs: {"scheduled_run": "READY"})
    for field, value, error in (
        ("INPUT_MODE", "bad", "INVALID_MODE"),
        ("INPUT_DRY_RUN", "yes", "INVALID_BOOLEAN_INPUT"),
        ("INPUT_LOOKBACK_HOURS", "72", "LOOKBACK_OVERRIDE_UNSUPPORTED_USE_FULL_RECONCILIATION"),
        ("INPUT_SOURCE", "$(bad)", "INVALID_SOURCE_ID"),
    ):
        monkeypatch.setenv(field, value)
        assert invoke(["run-scheduled"], expected=1)["error"] == error
        monkeypatch.delenv(field)
    monkeypatch.setenv("INPUT_DRY_RUN", "true")
    assert invoke(["run-scheduled"], expected=1)["error"] == "DRY_RUN_CANNOT_SEND"


def test_disabled_manual_dry_run_executes_without_sender_or_write_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    config = settings(
        github_actions=True,
        tracker_enabled=False,
        scheduler="disabled",
        gmail_auth_mode="disabled",
        budget_verified_at=datetime.now(UTC),
        included_private_minutes_remaining=2000,
        paid_overage_disabled=True,
    )
    monkeypatch.setattr(cli, "Settings", lambda: config)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("INPUT_DRY_RUN", "true")
    monkeypatch.setenv("INPUT_SEND_ALERTS", "false")

    def inspect(*args: Any, **kwargs: Any) -> dict[str, str]:
        calls.append(kwargs)
        return {
            "private_identity": "CONFIGURED",
            "sheet_read_write": "READ_VERIFIED_WRITE_NOT_PROBED",
        }

    def scan(*args: Any) -> None:
        calls.append(args)
        cli.emit({"status": "DRY_RUN_EXECUTED"})

    monkeypatch.setattr(cli, "readiness", inspect)
    monkeypatch.setattr(cli, "scan", scan)
    assert invoke(["run-scheduled"])["status"] == "DRY_RUN_EXECUTED"
    assert calls == [
        {"verify_write": False, "verify_email": False},
        ("incremental", True, False, False, ""),
    ]
    assert not config.tracker_enabled and config.scheduler == "disabled"
    monkeypatch.setenv("INPUT_DRY_RUN", "false")
    assert (
        invoke(["run-scheduled"], expected=1)["error"]
        == "TRACKER_DISABLED_REQUIRES_MANUAL_DRY_RUN_NO_SEND"
    )


@pytest.mark.parametrize(
    ("values", "read_result", "error"),
    [
        ({"zero_cost_mode": False}, {}, "MANUAL_DRY_RUN_REQUIRES_ZERO_COST_MODE"),
        ({"paid_overage_disabled": False}, {}, "MANUAL_DRY_RUN_BUDGET_BLOCKED"),
        (
            {},
            {"private_identity": "CONFIGURED", "sheet_read_write": "AUTH_OR_SHEET_UNAVAILABLE"},
            "MANUAL_DRY_RUN_SHEET_READINESS_BLOCKED",
        ),
        (
            {},
            {
                "private_identity": "SETUP_REQUIRED",
                "sheet_read_write": "READ_VERIFIED_WRITE_NOT_PROBED",
            },
            "MANUAL_DRY_RUN_SHEET_READINESS_BLOCKED",
        ),
    ],
)
def test_disabled_manual_dry_run_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, Any], read_result: dict[str, str], error: str
) -> None:
    config = settings(
        **(
            {
                "github_actions": True,
                "tracker_enabled": False,
                "budget_verified_at": datetime.now(UTC),
                "included_private_minutes_remaining": 2000,
                "paid_overage_disabled": True,
            }
            | values
        )
    )
    monkeypatch.setattr(cli, "Settings", lambda: config)
    monkeypatch.setattr(cli, "readiness", lambda *args, **kwargs: read_result)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("INPUT_DRY_RUN", "true")
    monkeypatch.setenv("INPUT_SEND_ALERTS", "false")
    assert invoke(["run-scheduled"], expected=1)["error"] == error


def test_enabled_production_retains_readiness_and_scheduler_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli, "Settings", lambda: settings(tracker_enabled=True, scheduler="disabled")
    )
    assert invoke(["run-scheduled"], expected=1)["error"] == "SINGLE_SCHEDULER_MISMATCH"
    monkeypatch.setattr(cli, "Settings", lambda: settings(tracker_enabled=True, scheduler="local"))
    monkeypatch.setattr(
        cli,
        "readiness",
        lambda *args, **kwargs: {"scheduled_run": "BLOCKED_WRITE_VERIFICATION_REQUIRED"},
    )
    assert "SCHEDULED_READINESS_BLOCKED" in invoke(["run-scheduled"], expected=1)["error"]


def test_validation_error_does_not_echo_private_input(monkeypatch: pytest.MonkeyPatch) -> None:
    def bad() -> Settings:
        return Settings(_env_file=None, monthly_plan_minutes="private-string-never-log")

    monkeypatch.setattr(cli, "Settings", bad)
    output = RUNNER.invoke(cli.app, ["doctor"])
    assert output.exit_code == 1
    assert "private-string-never-log" not in output.stdout
    assert "monthly_plan_minutes" in output.stdout


def test_bootstrap_seeds_company_config_and_preserves_user_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = settings(google_sheet_id="synthetic")
    book = demo_book()
    monkeypatch.setattr(cli, "Settings", lambda: config)
    monkeypatch.setattr(cli, "live_book", lambda *args: book)
    assert invoke(["bootstrap-sheet"])["tabs"] == 28
    assert book.read_tab("Companies") and book.read_tab("Config") and book.read_tab("Sources")
    book.tabs["Config"][0]["value"] = "human override"
    book.tabs["Companies"][0]["notes"] = "human company note"
    assert invoke(["bootstrap-sheet"])["tabs"] == 28
    assert book.tabs["Config"][0]["value"] == "human override"
    assert book.tabs["Companies"][0]["notes"] == "human company note"
    assert book.read_tab("Run_Log")[-1]["status"] == "COMMITTED"


def test_contact_reimport_preserves_manual_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = settings(google_sheet_id="synthetic")
    book = demo_book()
    path = tmp_path / "contacts.csv"
    path.write_text("Name,Company,Email\nExample Person,Example,candidate@example.com\n")
    contact = import_contacts(path, "csv", config.hmac_key(), NOW)[0]
    contact.outreach_allowed = True
    contact.notes = "Keep this user note"
    book.upsert("Contacts", "contact_uid", [contact.model_dump(mode="json")], "r", actor="user")
    monkeypatch.setattr(cli, "Settings", lambda: config)
    monkeypatch.setattr(cli, "live_book", lambda *args: book)
    assert invoke(["import", "contacts", str(path), "--format", "csv"])["records"] == 1
    assert len(book.read_tab("Contacts")) == 1
    assert book.read_tab("Contacts")[0]["outreach_allowed"] is True
    assert book.read_tab("Contacts")[0]["notes"] == "Keep this user note"


def test_invalid_readiness_and_dates() -> None:
    assert (
        invoke(["doctor", "--require", "other"], expected=1)["error"]
        == "INVALID_READINESS_REQUIREMENT"
    )
    assert cli.due_now("2026-09-13 12:00 IST", NOW)
    assert cli.due_now("2026-09-13T12:00:00Z", NOW)
    assert not cli.due_now("2026-09-13T12:00:00", NOW)
    assert not cli.due_now("unparseable", NOW)
    assert not cli.due_now(None, NOW)
