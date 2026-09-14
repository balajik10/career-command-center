"""Private-owner notifications with a Sheet-backed, at-most-one-attempt outbox."""

import base64
import hashlib
import json
import smtplib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from career_radar.domain import AlertDecision, JobScore, NormalizedJob
from career_radar.security import canonical_url, plain_text, private_token, redact
from career_radar.sheets.workbook import BaseWorkbook, Session

IST = ZoneInfo("Asia/Kolkata")
TEMPLATE_VERSION = "1"
UPDATE_EVENTS = {
    "APPLY_URL_REPLACED",
    "EARLIER_DEADLINE",
    "ELIGIBILITY_CHANGED",
    "LOCATION_CHANGED",
    "CLOSED",
    "REOPENED",
}


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def material_version(
    job: NormalizedJob,
    *,
    non_clock_band_transition: str = "",
    template_version: str = TEMPLATE_VERSION,
) -> str:
    """No age, current time, fetch timestamp or numeric score may enter this key."""
    return _sha(
        {
            "publication": str(job.posted_at_source),
            "publication_confidence": job.posted_at_confidence,
            "priority_transition": non_clock_band_transition,
            "apply_url": _sha(canonical_url(job.apply_url)) if job.apply_url else "",
            "active_state": job.active_state,
            "deadline": str(job.deadline),
            "eligibility": _sha(
                [
                    job.location,
                    job.work_mode,
                    job.visa_text,
                    job.min_years,
                    job.max_years,
                    job.education_constraint,
                ]
            ),
            "description_sequence": job.material_change_sequence,
            "template": template_version,
        }
    )


def select_immediate(
    jobs: Sequence[NormalizedJob],
    scores: Sequence[JobScore] | Mapping[str, JobScore],
    existing: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    hmac_key: bytes,
    recipient: str,
    change_events: Mapping[str, str] | None = None,
) -> list[AlertDecision]:
    if now.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    score_index = (
        dict(scores) if isinstance(scores, Mapping) else {score.job_uid: score for score in scores}
    )
    existing_ids = {str(row.get("alert_id")) for row in existing}
    previous_jobs = {
        str(row.get("job_uid")) for row in existing if row.get("alert_type") in {"P0", "UPDATE"}
    }
    decisions: list[AlertDecision] = []
    for job in sorted(
        jobs, key=lambda item: (-score_index[item.job_uid].priority_score, item.job_uid)
    ):
        score = score_index[job.job_uid]
        if score.action_priority in {"PIPELINE", "EXCLUDED"} or job.quarantine_reason:
            continue
        event = (change_events or {}).get(job.job_uid, "")
        is_update = job.job_uid in previous_jobs and event in UPDATE_EVENTS
        if not is_update and (score.action_priority != "P0" or job.job_uid in previous_jobs):
            continue
        # Baseline historical/undated imports cannot flood an immediate inbox.
        if (
            not is_update
            and job.baseline
            and (
                job.posted_at_source is None or job.age_hours_max is None or job.age_hours_max > 48
            )
        ):
            continue
        version = material_version(job)
        alert_type = "UPDATE" if is_update else "P0"
        alert_id = _sha([job.job_uid, alert_type, version])
        if alert_id not in existing_ids:
            decisions.append(
                AlertDecision(
                    alert_id=alert_id,
                    job_uid=job.job_uid,
                    alert_type=alert_type,
                    material_version=version,
                    recipient_token=private_token(hmac_key, "recipient", recipient),
                    hmac_key_version="v1",
                    created_at=now,
                    message_id=f"<{alert_id}@career-command-center.local>",
                )
            )
    return decisions


@dataclass(frozen=True)
class DeliveryPlan:
    members: tuple[AlertDecision, ...]
    bundle_id: str = ""


def _day(value: Any) -> str:
    instant = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if instant.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    return instant.astimezone(IST).date().isoformat()


def plan_messages(
    decisions: Sequence[AlertDecision], existing: Sequence[Mapping[str, Any]], *, now: datetime
) -> list[DeliveryPlan]:
    sent = {
        str(row.get("bundle_id") or row.get("alert_id"))
        for row in existing
        if row.get("state") != "PENDING"
        and row.get("alert_type") in {"P0", "UPDATE"}
        and _day(row.get("sending_at") or row.get("created_at")) == _day(now)
    }
    capacity = max(0, min(5, 18 - len(sent)))
    if capacity == 0 or not decisions:
        return []
    pending = [decision for decision in decisions if decision.state == "PENDING"]
    if len(pending) <= capacity:
        return [DeliveryPlan((item,)) for item in pending]
    individual = capacity - 1
    bundled = tuple(pending[individual:])
    bundle_id = _sha([sorted(item.alert_id for item in bundled), TEMPLATE_VERSION])
    return [
        *(DeliveryPlan((item,)) for item in pending[:individual]),
        DeliveryPlan(bundled, bundle_id),
    ]


@dataclass(frozen=True)
class SendResult:
    state: Literal["SENT", "AMBIGUOUS", "FAILED"]
    provider_message_id: str | None = None
    acceptance_status: str = ""
    error: str = ""


class Transport(Protocol):
    name: str
    authenticated_sender: str

    def send(self, message: EmailMessage) -> SendResult: ...


def owner_address(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("INVALID_OWNER_ADDRESS")
    addresses = getaddresses([value])
    if (
        len(addresses) != 1
        or addresses[0][0]
        or "@" not in addresses[0][1]
        or addresses[0][1] != value.strip()
    ):
        raise ValueError("INVALID_OWNER_ADDRESS")
    return addresses[0][1].strip().lower()


def assert_owner_message(message: EmailMessage, owner: str, sender: str) -> None:
    to_headers = message.get_all("To", [])
    if (
        len(to_headers) != 1
        or len(getaddresses(to_headers)) != 1
        or message.get_all("Cc")
        or message.get_all("Bcc")
    ):
        raise ValueError("OWNER_ONLY_RECIPIENT_REQUIRED")
    if owner_address(str(message["To"])) != owner_address(owner):
        raise ValueError("RECIPIENT_NOT_CONFIGURED_OWNER")
    if len(message.get_all("From", [])) != 1 or owner_address(
        str(message["From"])
    ) != owner_address(sender):
        raise ValueError("FROM_NOT_VERIFIED_AUTHENTICATED_SENDER")


def render_job(
    job: NormalizedJob,
    score: JobScore,
    *,
    sheet_url: str = "",
    draft_preview: str = "",
    contact_summary: str = "No confirmed contact yet",
) -> str:
    posted = (
        job.posted_at_source.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
        if job.posted_at_source
        else f"POSTED DATE UNKNOWN; first-seen proxy {job.first_seen_at.astimezone(IST):%Y-%m-%d %H:%M IST}"
    )
    ids = (
        ", ".join(
            identity.provider_job_id for identity in job.identities if identity.provider_job_id
        )
        or "Not supplied"
    )
    pay = (
        f"{job.salary_currency} {job.salary_base_min}–{job.salary_base_max} {job.salary_period} ({job.salary_source})"
        if job.salary_base_min is not None
        else "Not disclosed"
    )
    return plain_text(
        f"{job.company} — {job.title}\nPosting: {posted}\nJob ID: {ids}\nOfficial apply: {job.apply_url}\nFit: {score.fit_score:g}; priority: {score.priority_score:g}\nReasons: {'; '.join(score.reasons[:3])}\nGaps: {'; '.join(score.gaps) or 'No recorded hard/soft gaps'}\nAction by: {score.action_by_ist}\nApply/referral order: {score.next_action}\nContact: {contact_summary}\nDraft preview: {draft_preview or 'Review draft in Sheet'}\nCompensation: {pay}\nCanonical Sheet / brief: {sheet_url}\nConfidence: {job.posted_at_confidence}; {job.official_link_state}; verify the current posting before applying."
    )


def scheduled_digest_window(period: str, now: datetime) -> datetime:
    """Stable period boundary across workflow retries and manual invocations."""
    if period not in {"morning", "evening"} or now.tzinfo is None:
        raise ValueError("INVALID_DIGEST_WINDOW")
    local = now.astimezone(IST)
    return local.replace(
        hour=8 if period == "morning" else 19, minute=11, second=0, microsecond=0
    ).astimezone(UTC)


def render_digest(
    jobs: Sequence[NormalizedJob],
    scores: Sequence[JobScore],
    *,
    sheet_url: str = "",
    overdue: Sequence[str] = (),
    followups: Sequence[str] = (),
    changes: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> str:
    score_index = {score.job_uid: score for score in scores}
    eligible = [
        job
        for job in jobs
        if job.job_uid in score_index and score_index[job.job_uid].action_priority in {"P0", "P1"}
    ]
    eligible.sort(key=lambda job: (-score_index[job.job_uid].priority_score, job.job_uid))
    entries = [f"OVERDUE: {value}" for value in overdue] + [
        f"FOLLOW-UP: {value}" for value in followups
    ]
    entries += [f"CHANGED: {value}" for value in changes] + [
        f"ACTION REQUIRED: {value}" for value in warnings
    ]
    for job in eligible:
        score = score_index[job.job_uid]
        freshness = (
            job.posted_at_source.astimezone(IST).strftime("Posted %Y-%m-%d %H:%M IST")
            if job.posted_at_source
            else "POSTED DATE UNKNOWN; first-seen proxy"
        )
        entries.append(
            f"{score.action_priority} · {job.company} — {job.title}\n{freshness}; fit {score.fit_score:g}; act by {score.action_by_ist}\n{score.next_action}\n{job.apply_url}"
        )
    if not entries:
        return "No new priority jobs or actionable updates."
    remaining = max(0, len(entries) - 20)
    footer = (
        f"\n\n{remaining} more items in the private Sheet. {sheet_url}"
        if remaining
        else f"\n\nPrivate Sheet: {sheet_url}"
    )
    return plain_text("\n\n".join(entries[:20]) + footer)


class Outbox:
    def __init__(
        self,
        workbook: BaseWorkbook,
        recipient: str,
        hmac_key: bytes,
        transport: Transport | None = None,
    ) -> None:
        self.workbook = workbook
        self.recipient = owner_address(recipient)
        self.hmac_key = hmac_key
        self.transport = transport
        self.recipient_token = private_token(hmac_key, "recipient", self.recipient)

    def enqueue(
        self, decisions: Sequence[AlertDecision], run_uid: str, *, dry_run: bool = False
    ) -> int:
        if dry_run:
            return 0
        existing = {row["alert_id"] for row in self.workbook.read_tab("_Alerts")}
        rows = [
            {
                **decision.model_dump(mode="json"),
                "recipient_token": self.recipient_token,
                "run_uid": run_uid,
                "template_version": TEMPLATE_VERSION,
            }
            for decision in decisions
            if decision.alert_id not in existing
        ]
        # AlertDecision calls its human-facing error field `error`; all storage
        # fields are explicitly allowlisted in the protected outbox schema.
        return self.workbook.upsert("_Alerts", "alert_id", rows, run_uid) if rows else 0

    def deliver(
        self,
        plan: DeliveryPlan,
        subject: str,
        body: str,
        run_uid: str,
        *,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> SendResult | None:
        if dry_run:
            return None
        if self.transport is None:
            raise ValueError("NOTIFICATION_TRANSPORT_REQUIRED")
        now = now or datetime.now(UTC)
        current = {row["alert_id"]: row for row in self.workbook.read_tab("_Alerts")}
        if not plan.members or any(
            item.alert_id not in current
            or current[item.alert_id].get("state") != "PENDING"
            or current[item.alert_id].get("automatic_attempts", 0)
            for item in plan.members
        ):
            return None
        message_key = plan.bundle_id or plan.members[0].alert_id
        message = EmailMessage()
        message["To"] = self.recipient
        message["From"] = self.transport.authenticated_sender
        message["Subject"] = plain_text(subject).replace("\n", " ").replace("\r", " ")[:240]
        message["Message-ID"] = f"<{message_key}@career-command-center.local>"
        message["X-Career-Alert-ID"] = message_key
        message.set_content(plain_text(body))
        assert_owner_message(message, self.recipient, self.transport.authenticated_sender)
        members = [item.alert_id for item in plan.members]
        claimed = [
            {
                "alert_id": item.alert_id,
                "state": "SENDING",
                "sending_at": now.isoformat(),
                "automatic_attempts": 1,
                "bundle_id": plan.bundle_id,
                "member_alert_ids": current[item.alert_id].get("member_alert_ids", members)
                if item.alert_type == "DIGEST"
                else members,
                "transport": self.transport.name,
                "message_id": str(message["Message-ID"]),
                "run_uid": run_uid,
            }
            for item in plan.members
        ]
        self.workbook.transaction({"_Alerts": claimed}, run_uid)
        try:
            outcome = self.transport.send(message)
        except Exception:
            # No provider response can prove absence of acceptance. Never retry.
            outcome = SendResult("AMBIGUOUS", error="TRANSPORT_RESPONSE_LOST")
        updates = [
            {
                "alert_id": item.alert_id,
                "state": outcome.state,
                "sent_at": now.isoformat() if outcome.state == "SENT" else "",
                "provider_message_id": outcome.provider_message_id,
                "acceptance_status": outcome.acceptance_status,
                "error": redact(outcome.error),
            }
            for item in plan.members
        ]
        self.workbook.transaction({"_Alerts": updates}, run_uid)
        return outcome

    def reconcile_interrupted(self, run_uid: str) -> int:
        """A prior SENDING claim may have reached the provider: manual review only."""
        rows = [
            {
                "alert_id": row["alert_id"],
                "state": "AMBIGUOUS",
                "error": "INTERRUPTED_AFTER_SEND_CLAIM",
            }
            for row in self.workbook.read_tab("_Alerts")
            if row.get("state") == "SENDING"
        ]
        if rows:
            self.workbook.transaction({"_Alerts": rows}, run_uid)
        return len(rows)

    def failure_notice(
        self,
        condition: str,
        active: bool,
        body: str,
        run_uid: str,
        *,
        now: datetime | None = None,
        due_failures: int | None = None,
        no_send: bool = False,
    ) -> SendResult | None:
        """Deduplicate a persistent failure and send one recovery per episode.

        Callers pass a stable condition code. A single-source condition is eligible
        only after three consecutive due-run failures. If Sheets is unavailable,
        callers fail the workflow rather than pretend this dedupe state persisted.
        """
        if no_send or (active and due_failures is not None and due_failures < 3):
            return None
        now = now or datetime.now(UTC)
        key = "failure:" + _sha(condition)
        state = next(
            (
                row.get("value")
                for row in self.workbook.read_tab("_System_State")
                if row.get("key") == key
            ),
            "",
        )
        previous = json.loads(str(state)) if state else {"active": False, "episode": 0}
        if bool(previous["active"]) == active:
            return None
        episode = int(previous["episode"]) + int(active)
        kind = "FAILURE" if active else "RECOVERY"
        identity = _sha([key, episode, kind, TEMPLATE_VERSION])
        decision = AlertDecision(
            alert_id=identity,
            job_uid="SYSTEM",
            alert_type=kind,
            material_version=str(episode),
            recipient_token=self.recipient_token,
            created_at=now,
        )
        self.enqueue([decision], run_uid)
        self.workbook.transaction(
            {
                "_System_State": [
                    {
                        "key": key,
                        "value": {"active": active, "episode": episode, "alert_id": identity},
                        "run_uid": run_uid,
                    }
                ]
            },
            run_uid,
        )
        return self.deliver(
            DeliveryPlan((decision,)),
            f"[CAREER {kind}] System status",
            redact(body),
            run_uid,
            now=now,
        )

    def test(
        self, recipient_override: str, run_uid: str, *, now: datetime | None = None
    ) -> SendResult | None:
        if owner_address(recipient_override) != self.recipient:
            raise ValueError("RECIPIENT_NOT_CONFIGURED_OWNER")
        now = now or datetime.now(UTC)
        identity = _sha(["SETUP_TEST", self.recipient_token, TEMPLATE_VERSION])
        decision = AlertDecision(
            alert_id=identity,
            job_uid="SETUP_TEST",
            alert_type="SETUP_TEST",
            material_version=TEMPLATE_VERSION,
            recipient_token=self.recipient_token,
            created_at=now,
        )
        self.enqueue([decision], run_uid)
        return self.deliver(
            DeliveryPlan((decision,)),
            "[CAREER COMMAND CENTER TEST] Owner notification setup",
            "This is the single setup notification attempt to the configured owner. No job application or third-party outreach was sent.",
            run_uid,
            now=now,
        )

    def digest(
        self,
        period: str,
        window_end: datetime,
        members: Sequence[AlertDecision],
        body: str,
        run_uid: str,
        *,
        actionable: bool = False,
        no_send: bool = False,
    ) -> SendResult | None:
        if period not in {"morning", "evening"} or window_end.tzinfo is None:
            raise ValueError("INVALID_DIGEST_WINDOW")
        if no_send:
            return None
        states = {
            row["key"]: row.get("value", "") for row in self.workbook.read_tab("_System_State")
        }
        cursor_key = "digest_cursor"
        previous = str(states.get(cursor_key, "INITIAL"))
        end = window_end.isoformat()
        # An exact scheduled window receives at most one automatic transport attempt,
        # even if its previous cursor changes after a successful delivery.
        existing = self.workbook.read_tab("_Alerts")
        prior = [
            row
            for row in existing
            if row.get("alert_type") in {"DIGEST", "NO_CONTENT"}
            and row.get("scheduled_window_end") == end
        ]
        if prior:
            return None
        digest_id = _sha(
            [period, previous, end, sorted(item.alert_id for item in members), TEMPLATE_VERSION]
        )
        if not members and not actionable:
            self.workbook.transaction(
                {
                    "_Alerts": [
                        {
                            "alert_id": digest_id,
                            "alert_type": "NO_CONTENT",
                            "digest_id": digest_id,
                            "state": "SENT",
                            "acceptance_status": "NO_CONTENT",
                            "digest_cursor": previous,
                            "scheduled_window_end": end,
                            "created_at": end,
                            "run_uid": run_uid,
                        }
                    ],
                    "_System_State": [{"key": cursor_key, "value": end, "run_uid": run_uid}],
                },
                run_uid,
            )
            return None
        decision = AlertDecision(
            alert_id=digest_id,
            job_uid="DIGEST",
            alert_type="DIGEST",
            material_version=TEMPLATE_VERSION,
            recipient_token=self.recipient_token,
            created_at=window_end,
        )
        self.enqueue([decision], run_uid)
        self.workbook.transaction(
            {
                "_Alerts": [
                    {
                        "alert_id": digest_id,
                        "digest_id": digest_id,
                        "member_alert_ids": [item.alert_id for item in members],
                        "digest_cursor": previous,
                        "scheduled_window_end": end,
                    }
                ]
            },
            run_uid,
        )
        outcome = self.deliver(
            DeliveryPlan((decision,)),
            f"[CAREER DIGEST] {period.title()} — {window_end.astimezone(IST):%Y-%m-%d}",
            body,
            run_uid,
            now=window_end,
        )
        if outcome and outcome.state == "SENT":
            self.workbook.transaction(
                {"_System_State": [{"key": cursor_key, "value": end, "run_uid": run_uid}]}, run_uid
            )
        return outcome


class GmailTransport:
    name = "gmail"

    def __init__(self, session: Session, access_token: str, authenticated_sender: str) -> None:
        self.session = session
        self._access_token = access_token
        self.authenticated_sender = owner_address(authenticated_sender)

    def send(self, message: EmailMessage) -> SendResult:
        response = self.session.request(
            "POST",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            timeout=30,
            headers={"Authorization": f"Bearer {self._access_token}"},
            json={"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()},
        )
        if response.status_code >= 400:
            return SendResult(
                "FAILED",
                acceptance_status=str(response.status_code),
                error=f"GMAIL_HTTP_{response.status_code}",
            )
        provider_id = response.json().get("id")
        return (
            SendResult("SENT", str(provider_id), "ACCEPTED")
            if provider_id
            else SendResult("AMBIGUOUS", error="GMAIL_MESSAGE_ID_MISSING")
        )


class SMTPTransport:
    name = "smtp"

    def __init__(self, connection: smtplib.SMTP, authenticated_sender: str) -> None:
        self.connection = connection
        self.authenticated_sender = owner_address(authenticated_sender)

    def send(self, message: EmailMessage) -> SendResult:
        try:
            refused = self.connection.send_message(message)
        except smtplib.SMTPRecipientsRefused:
            return SendResult("FAILED", error="SMTP_RECIPIENT_REFUSED")
        except smtplib.SMTPResponseException as error:
            return SendResult(
                "FAILED", acceptance_status=str(error.smtp_code), error="SMTP_DEFINITIVE_REJECTION"
            )
        except smtplib.SMTPServerDisconnected:
            return SendResult("AMBIGUOUS", error="SMTP_ACCEPTANCE_UNKNOWN")
        if refused:
            return SendResult("FAILED", error="SMTP_RECIPIENT_REFUSED")
        return SendResult("SENT", acceptance_status="250")


@dataclass
class FakeTransport:
    authenticated_sender: str = "candidate@example.com"
    name: str = "fake"
    result: SendResult = field(
        default_factory=lambda: SendResult("SENT", "fake-message-id", "ACCEPTED")
    )
    messages: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> SendResult:
        self.messages.append(message)
        return self.result


__all__ = [
    "DeliveryPlan",
    "FakeTransport",
    "GmailTransport",
    "Outbox",
    "SMTPTransport",
    "SendResult",
    "assert_owner_message",
    "material_version",
    "owner_address",
    "plan_messages",
    "render_job",
    "render_digest",
    "scheduled_digest_window",
    "select_immediate",
]
