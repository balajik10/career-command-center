from __future__ import annotations

import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any, cast

import pytest

from career_radar.domain import AlertDecision, JobIdentity, JobScore, NormalizedJob
from career_radar.notifications import (
    DeliveryPlan,
    FakeTransport,
    GmailTransport,
    Outbox,
    SendResult,
    SMTPTransport,
    assert_owner_message,
    material_version,
    owner_address,
    plan_messages,
    render_digest,
    render_job,
    scheduled_digest_window,
    select_immediate,
)
from career_radar.sheets import FakeWorkbook, SheetError

KEY = b"synthetic-test-key-32-byte-minimum!"
OWNER = "candidate@example.com"
NOW = datetime(2026, 9, 14, 6, tzinfo=UTC)


def job(index: int = 0, **kwargs: Any) -> NormalizedJob:
    return NormalizedJob(
        job_uid=f"j{index}",
        company="Example",
        title="Backend Engineer",
        apply_url="https://example.com/jobs/1",
        posted_at_source=NOW - timedelta(hours=1),
        age_hours_max=1,
        **kwargs,
    )


def score(index: int = 0, *, priority: str = "P0") -> JobScore:
    return JobScore(
        job_uid=f"j{index}",
        fit_score=92,
        priority_score=90 - index,
        score_band="S0",
        action_priority=priority,
        action_eligibility="ELIGIBLE",
        reasons=["Evidence-backed match"],
        gaps=["Verify one requirement"],
        next_action="Apply, then request a warm referral",
        action_by_ist="2026-09-14 15:30 IST",
    )


def decisions(count: int) -> list[AlertDecision]:
    return select_immediate(
        [job(i) for i in range(count)],
        [score(i) for i in range(count)],
        [],
        now=NOW,
        hmac_key=KEY,
        recipient=OWNER,
    )


@pytest.fixture
def outbox() -> Outbox:
    wb = FakeWorkbook()
    wb.bootstrap()
    return Outbox(wb, OWNER, KEY, FakeTransport())


def test_material_version_excludes_clock_and_numeric_score_drift() -> None:
    original = job()
    elapsed = original.model_copy(
        update={
            "age_hours_min": 2,
            "age_hours_max": 3,
            "last_seen_at": NOW + timedelta(days=2),
            "freshness_band": "OLD",
            "first_seen_at": NOW,
        }
    )
    assert material_version(original) == material_version(elapsed)
    for updates in (
        {"material_change_sequence": 1},
        {"deadline": NOW},
        {"apply_url": "https://example.com/jobs/2"},
        {"location": "India remote"},
        {"active_state": "CLOSED"},
    ):
        assert material_version(original) != material_version(original.model_copy(update=updates))
    assert material_version(
        original, non_clock_band_transition="ELIGIBILITY_P0"
    ) != material_version(original)
    assert material_version(original, template_version="2") != material_version(original)
    assert material_version(original.model_copy(update={"apply_url": ""}))


def test_selection_baseline_idempotency_and_allowlisted_updates() -> None:
    selected = decisions(2)
    assert len(selected) == 2
    existing = [item.model_dump(mode="json") for item in selected]
    assert (
        select_immediate(
            [job(0), job(1)],
            [score(0), score(1)],
            existing,
            now=NOW,
            hmac_key=KEY,
            recipient=OWNER,
        )
        == []
    )
    assert OWNER not in str(existing)
    unknown = job().model_copy(update={"posted_at_source": None, "age_hours_max": None})
    old = job().model_copy(update={"age_hours_max": 49})
    for record in (unknown, old):
        assert not select_immediate([record], [score()], [], now=NOW, hmac_key=KEY, recipient=OWNER)
    assert select_immediate(
        [unknown.model_copy(update={"baseline": False})],
        {"j0": score()},
        [],
        now=NOW,
        hmac_key=KEY,
        recipient=OWNER,
    )
    assert not select_immediate(
        [job()],
        [score(priority="P1")],
        [],
        now=NOW,
        hmac_key=KEY,
        recipient=OWNER,
    )
    changed = job().model_copy(update={"apply_url": "https://example.com/jobs/replacement"})
    assert not select_immediate(
        [changed],
        [score()],
        existing,
        now=NOW,
        hmac_key=KEY,
        recipient=OWNER,
        change_events={"j0": "SCORE_DRIFT"},
    )
    updates = select_immediate(
        [changed],
        [score()],
        existing,
        now=NOW,
        hmac_key=KEY,
        recipient=OWNER,
        change_events={"j0": "APPLY_URL_REPLACED"},
    )
    assert len(updates) == 1 and updates[0].alert_type == "UPDATE"
    existing.append(updates[0].model_dump(mode="json"))
    assert not select_immediate(
        [changed],
        [score()],
        existing,
        now=NOW,
        hmac_key=KEY,
        recipient=OWNER,
        change_events={"j0": "APPLY_URL_REPLACED"},
    )
    with pytest.raises(ValueError, match="timezone"):
        select_immediate(
            [job()],
            [score()],
            [],
            now=datetime(2026, 1, 1),
            hmac_key=KEY,
            recipient=OWNER,
        )


@pytest.mark.parametrize(
    "count,individual,bundled", [(0, 0, 0), (1, 1, 0), (5, 5, 0), (6, 4, 2), (10, 4, 6)]
)
def test_exact_five_limit_and_ranked_bundle(count: int, individual: int, bundled: int) -> None:
    plan = plan_messages(decisions(count), [], now=NOW)
    assert sum(not item.bundle_id for item in plan) == individual
    assert sum(len(item.members) for item in plan if item.bundle_id) == bundled
    assert sum(len(item.members) for item in plan) == count


def test_daily_cap_reserves_two_digests_and_ist_midnight() -> None:
    prior = [
        {"alert_id": f"old{i}", "state": "SENT", "alert_type": "P0", "sending_at": NOW.isoformat()}
        for i in range(17)
    ]
    plan = plan_messages(decisions(4), prior, now=NOW)
    assert len(plan) == 1 and len(plan[0].members) == 4 and plan[0].bundle_id
    prior.append(
        {
            "alert_id": "last",
            "state": "AMBIGUOUS",
            "alert_type": "P0",
            "created_at": NOW.isoformat(),
        }
    )
    assert not plan_messages(decisions(4), prior, now=NOW)
    assert len(plan_messages(decisions(4), prior, now=NOW + timedelta(days=1))) == 4
    duplicate_bundle = [
        {
            "alert_id": f"member{i}",
            "bundle_id": "same",
            "state": "SENT",
            "alert_type": "P0",
            "created_at": NOW.isoformat(),
        }
        for i in range(100)
    ]
    assert len(plan_messages(decisions(5), duplicate_bundle, now=NOW)) == 5
    pending = decisions(1)
    pending[0].state = "SENT"
    assert not plan_messages(pending, [], now=NOW)
    assert (
        len(plan_messages(decisions(5), prior, now=datetime(2026, 9, 14, 18, 31, tzinfo=UTC))) == 5
    )
    with pytest.raises(ValueError, match="timezone"):
        plan_messages(decisions(1), [{**prior[0], "sending_at": "2026-09-14T12:00:00"}], now=NOW)


def test_outbox_bundled_claim_idempotency_and_definitive_acceptance(outbox: Outbox) -> None:
    selected = decisions(8)
    assert outbox.enqueue(selected, "r") == 8
    assert outbox.enqueue(selected, "r") == 0
    plans = plan_messages(selected, [], now=NOW)
    for plan in plans:
        result = outbox.deliver(
            plan, "[P0 APPLY NOW] Example - Backend - Posted recently", "Safe body", "r", now=NOW
        )
        assert result and result.state == "SENT"
        assert outbox.deliver(plan, "Repeat", "No", "r", now=NOW) is None
    transport = cast(FakeTransport, outbox.transport)
    assert len(transport.messages) == 5
    rows = outbox.workbook.read_tab("_Alerts")
    assert all(
        row["automatic_attempts"] == 1
        and row["state"] == "SENT"
        and row["provider_message_id"] == "fake-message-id"
        for row in rows
    )
    assert len([row for row in rows if row.get("bundle_id")]) == 4
    assert all(OWNER not in str(row) for row in rows)
    assert transport.messages[0]["From"] == OWNER
    assert transport.messages[0]["To"] == OWNER
    assert "@career-command-center.local>" in str(transport.messages[0]["Message-ID"])
    assert outbox.reconcile_interrupted("r") == 0


def test_ambiguous_and_failed_never_automatically_resend(outbox: Outbox) -> None:
    selected = decisions(1)
    transport = cast(FakeTransport, outbox.transport)
    transport.result = SendResult("AMBIGUOUS", error="acceptance unknown")
    outbox.enqueue(selected, "r")
    plan = DeliveryPlan(tuple(selected))
    assert outbox.deliver(plan, "s", "b", "r", now=NOW).state == "AMBIGUOUS"  # type: ignore[union-attr]
    assert outbox.deliver(plan, "s", "b", "r", now=NOW) is None
    assert len(transport.messages) == 1
    assert outbox.workbook.read_tab("_Alerts")[0].get("sent_at", "") == ""
    outbox.workbook.transaction(
        {"_Alerts": [{"alert_id": selected[0].alert_id, "state": "SENDING"}]}, "r"
    )
    assert outbox.reconcile_interrupted("r") == 1
    assert outbox.workbook.read_tab("_Alerts")[0]["state"] == "AMBIGUOUS"
    with pytest.raises(ValueError, match="TRANSPORT"):
        Outbox(outbox.workbook, OWNER, KEY).deliver(plan, "s", "b", "r")
    assert outbox.deliver(DeliveryPlan(()), "s", "b", "r") is None
    assert outbox.deliver(DeliveryPlan(tuple(decisions(2)[1:])), "s", "b", "r") is None


def test_transport_response_lost_and_sheet_completion_lost(outbox: Outbox) -> None:
    class Broken(FakeTransport):
        def send(self, message: EmailMessage) -> SendResult:
            raise TimeoutError("secret provider details never logged")

    outbox.transport = Broken()
    selected = decisions(1)
    outbox.enqueue(selected, "r")
    result = outbox.deliver(DeliveryPlan(tuple(selected)), "s", "b", "r")
    assert result and result.state == "AMBIGUOUS" and result.error == "TRANSPORT_RESPONSE_LOST"
    wb = cast(FakeWorkbook, outbox.workbook)
    second = decisions(2)[1:]
    outbox.enqueue(second, "r")
    outbox.transport = FakeTransport()
    wb.fail_on_write = wb.write_requests + 2
    with pytest.raises(SheetError):
        outbox.deliver(DeliveryPlan(tuple(second)), "s", "b", "r")
    assert wb.tabs["_Alerts"][1]["state"] == "SENDING"
    wb.fail_on_write = None
    assert outbox.reconcile_interrupted("repair") == 1
    assert outbox.deliver(DeliveryPlan(tuple(second)), "s", "b", "r") is None


def test_dry_run_zero_writes_and_one_setup_transport_attempt(outbox: Outbox) -> None:
    wb = cast(FakeWorkbook, outbox.workbook)
    before = wb.write_requests
    selected = decisions(1)
    assert outbox.enqueue(selected, "r", dry_run=True) == 0
    assert outbox.deliver(DeliveryPlan(tuple(selected)), "s", "b", "r", dry_run=True) is None
    assert outbox.digest("morning", NOW, selected, "preview", "r", no_send=True) is None
    assert wb.write_requests == before
    assert not cast(FakeTransport, outbox.transport).messages
    with pytest.raises(ValueError, match="CONFIGURED_OWNER"):
        outbox.test("another@example.com", "r")
    assert outbox.test(OWNER.upper(), "setup", now=NOW).state == "SENT"  # type: ignore[union-attr]
    assert outbox.test(OWNER, "setup2", now=NOW) is None
    assert len(cast(FakeTransport, outbox.transport).messages) == 1


def test_digest_cursor_success_ambiguity_empty_and_window_replay(outbox: Outbox) -> None:
    members = decisions(2)
    result = outbox.digest("morning", NOW, members, "Top jobs", "r")
    assert result and result.state == "SENT"
    assert outbox.digest("morning", NOW, members, "Repeat", "r") is None
    states = {row["key"]: row.get("value") for row in outbox.workbook.read_tab("_System_State")}
    assert states["digest_cursor"] == NOW.isoformat()
    later = NOW + timedelta(hours=11)
    cast(FakeTransport, outbox.transport).result = SendResult("AMBIGUOUS")
    assert outbox.digest("evening", later, members, "Top jobs", "r").state == "AMBIGUOUS"  # type: ignore[union-attr]
    states = {row["key"]: row.get("value") for row in outbox.workbook.read_tab("_System_State")}
    assert states["digest_cursor"] == NOW.isoformat()
    empty_end = NOW + timedelta(days=1)
    assert outbox.digest("morning", empty_end, [], "", "r") is None
    assert outbox.workbook.read_tab("_Alerts")[-1]["alert_type"] == "NO_CONTENT"
    assert outbox.digest("morning", empty_end, [], "", "r") is None
    assert (
        outbox.digest(
            "evening", empty_end + timedelta(hours=11), [], "Action required", "r", actionable=True
        ).state
        == "AMBIGUOUS"
    )  # type: ignore[union-attr]
    for period, end in (("afternoon", NOW), ("morning", datetime(2026, 1, 1))):
        with pytest.raises(ValueError, match="DIGEST_WINDOW"):
            outbox.digest(period, end, [], "", "r")


@pytest.mark.parametrize(
    "address",
    [
        "Name <candidate@example.com>",
        "one@example.com,two@example.com",
        "missing-at",
        "candidate@example.com\nBcc: x@example.com",
        "",
    ],
)
def test_owner_address_rejects_ambiguous_or_injected_addresses(address: str) -> None:
    with pytest.raises(ValueError, match="OWNER_ADDRESS"):
        owner_address(address)


def test_only_exact_owner_and_authenticated_sender_allowed() -> None:
    message = EmailMessage()
    message["To"] = OWNER
    message["From"] = "verified@example.com"
    assert_owner_message(message, OWNER, "verified@example.com")
    message["Cc"] = OWNER
    with pytest.raises(ValueError, match="OWNER_ONLY"):
        assert_owner_message(message, OWNER, "verified@example.com")
    del message["Cc"]
    with pytest.raises(ValueError, match="CONFIGURED_OWNER"):
        assert_owner_message(message, "another@example.com", "verified@example.com")
    with pytest.raises(ValueError, match="VERIFIED_AUTHENTICATED"):
        assert_owner_message(message, OWNER, "different@example.com")
    del message["To"]
    with pytest.raises(ValueError, match="OWNER_ONLY"):
        assert_owner_message(message, OWNER, "verified@example.com")


def test_job_rendering_uses_labels_provenance_and_source_compensation() -> None:
    record = job(
        identities=[JobIdentity(provider="example", provider_job_id="123")],
        salary_base_min=10,
        salary_base_max=20,
        salary_currency="INR",
        salary_period="year",
        salary_source="https://example.com/job",
    )
    body = render_job(record, score(), sheet_url="https://docs.google.com/spreadsheets/d/synthetic")
    assert "123" in body and "INR" in body and "No confirmed contact yet" in body
    assert "IST" in body and "Evidence-backed match" in body
    unknown = record.model_copy(
        update={"posted_at_source": None, "salary_base_min": None, "identities": []}
    )
    body = render_job(
        unknown, score(), draft_preview="Approved draft", contact_summary="Warm connection"
    )
    assert "POSTED DATE UNKNOWN; first-seen proxy" in body and "Not disclosed" in body


class HTTP:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self.payload, self.status_code = payload, status
        self.calls: list[Any] = []

    def request(self, method: str, url: str, **kwargs: Any) -> HTTP:
        self.calls.append((method, url, kwargs))
        return self

    def json(self) -> dict[str, Any]:
        return self.payload


@pytest.mark.parametrize(
    "payload,status,expected",
    [({"id": "gmail-id"}, 200, "SENT"), ({}, 200, "AMBIGUOUS"), ({"error": "bad"}, 401, "FAILED")],
)
def test_gmail_provider_acceptance(payload: dict[str, Any], status: int, expected: str) -> None:
    http = HTTP(payload, status)
    transport = GmailTransport(http, "synthetic-token", OWNER)
    message = EmailMessage()
    message.set_content("Example")
    result = transport.send(message)
    assert result.state == expected
    assert http.calls[0][2]["json"]["raw"]
    if expected == "SENT":
        assert result.provider_message_id == "gmail-id"


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ({}, "SENT"),
        ({"candidate@example.com": (550, "No")}, "FAILED"),
        (smtplib.SMTPRecipientsRefused({}), "FAILED"),
        (smtplib.SMTPDataError(550, b"No"), "FAILED"),
        (smtplib.SMTPServerDisconnected("Lost"), "AMBIGUOUS"),
    ],
)
def test_smtp_provider_acceptance_and_ambiguity(outcome: Any, expected: str) -> None:
    class Connection:
        def send_message(self, message: EmailMessage) -> Any:
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    transport = SMTPTransport(cast(smtplib.SMTP, Connection()), OWNER)
    result = transport.send(EmailMessage())
    assert result.state == expected
    if expected == "SENT":
        assert result.provider_message_id is None and result.acceptance_status == "250"


def test_digest_stable_boundary_and_twenty_item_limit() -> None:
    assert scheduled_digest_window("morning", NOW).isoformat() == "2026-09-14T02:41:00+00:00"
    assert scheduled_digest_window("morning", NOW + timedelta(hours=1)) == scheduled_digest_window(
        "morning", NOW
    )
    assert scheduled_digest_window("evening", NOW).isoformat() == "2026-09-14T13:41:00+00:00"
    for period, now in (("other", NOW), ("morning", datetime(2026, 1, 1))):
        with pytest.raises(ValueError, match="DIGEST_WINDOW"):
            scheduled_digest_window(period, now)
    assert render_digest([], []) == "No new priority jobs or actionable updates."
    records = [job(i) for i in range(22)]
    scores = [score(i) for i in range(22)]
    body = render_digest(
        records,
        scores,
        sheet_url="https://example.com/private",
        overdue=["Apply now"],
        followups=["Reply"],
        changes=["Role closed"],
        warnings=["Repair auth"],
    )
    assert "6 more items" in body
    assert (
        "OVERDUE" in body
        and "FOLLOW-UP" in body
        and "CHANGED" in body
        and "ACTION REQUIRED" in body
    )
    assert body.count("Official") == 0
    unknown = job().model_copy(update={"posted_at_source": None})
    assert "POSTED DATE UNKNOWN" in render_digest([unknown], [score()])
    assert "No new" in render_digest([job()], [score(priority="P2")])


def test_failure_episode_dedupe_recovery_and_isolated_source_gate(outbox: Outbox) -> None:
    assert outbox.failure_notice("source:test", True, "One failure", "r", due_failures=1) is None
    assert outbox.failure_notice("auth", True, "Preview", "r", no_send=True) is None
    assert outbox.failure_notice("source:test", False, "Healthy", "r") is None
    result = outbox.failure_notice(
        "source:test", True, "Repair source auth", "r", due_failures=3, now=NOW
    )
    assert result and result.state == "SENT"
    assert outbox.failure_notice("source:test", True, "Still broken", "r", due_failures=4) is None
    assert outbox.failure_notice("source:test", False, "Recovered", "r", now=NOW).state == "SENT"  # type: ignore[union-attr]
    assert outbox.failure_notice("source:test", False, "Still healthy", "r") is None
    assert (
        outbox.failure_notice("source:test", True, "New failure episode", "r", now=NOW).state
        == "SENT"
    )  # type: ignore[union-attr]
    assert len(cast(FakeTransport, outbox.transport).messages) == 3
    assert [row["alert_type"] for row in outbox.workbook.read_tab("_Alerts")] == [
        "FAILURE",
        "RECOVERY",
        "FAILURE",
    ]


def test_tracked_or_quarantined_jobs_never_trigger_apply_update() -> None:
    existing = [decision.model_dump(mode="json") for decision in decisions(1)]
    for record, scored in (
        (job(), score(priority="PIPELINE")),
        (job(), score(priority="EXCLUDED")),
        (job(quarantine_reason="AMBIGUOUS_IDENTITY"), score()),
    ):
        assert not select_immediate(
            [record],
            [scored],
            existing,
            now=NOW,
            hmac_key=KEY,
            recipient=OWNER,
            change_events={"j0": "APPLY_URL_REPLACED"},
        )
