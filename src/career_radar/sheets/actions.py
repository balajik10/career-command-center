"""Immutable, user-submitted workflow commands with audited, idempotent application."""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from career_radar.security import plain_text

from .schema import APPLICATION_STAGES, OUTREACH_STAGES, REFERRAL_STAGES, SCHEMA
from .workbook import BaseWorkbook

PAYLOAD_FIELDS = (
    "job_uid",
    "application_stage_update",
    "referral_stage_update",
    "outreach_stage_update",
    "update_note",
    "reason",
)
TERMINAL = {"ACCEPTED", "DECLINED", "REJECTED", "WITHDRAWN", "ROLE_CLOSED"}
PRE_APPLY = {"NOT_REVIEWED", "REVIEWING", "READY_TO_APPLY", "SKIPPED"}
ACTIVE = set(APPLICATION_STAGES) - TERMINAL - PRE_APPLY
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    stage: frozenset(
        (set(APPLICATION_STAGES) - {"ACCEPTED", "DECLINED"})
        if stage in PRE_APPLY
        else (ACTIVE | TERMINAL)
    )
    for stage in APPLICATION_STAGES
    if stage not in TERMINAL
}
ALLOWED_TRANSITIONS["OFFER"] = frozenset(ACTIVE | TERMINAL)
ALLOWED_TRANSITIONS.update({stage: frozenset({stage}) for stage in TERMINAL})


def normalized_payload(row: Mapping[str, Any]) -> dict[str, str]:
    return {field: plain_text(str(row.get(field, ""))).strip() for field in PAYLOAD_FIELDS}


def payload_hash(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(normalized_payload(row), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def update_identity(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(f"{row.get('row_uid', '')}:{payload_hash(row)}".encode()).hexdigest()


def valid_transition(
    previous: str, following: str, reason: str = "", *, correction: bool = False
) -> bool:
    if following not in APPLICATION_STAGES or previous not in APPLICATION_STAGES:
        return False
    if following == "SKIPPED" and not reason:
        return False
    if correction:
        return bool(reason)
    return following in ALLOWED_TRANSITIONS[previous]


def process_action_updates(
    workbook: BaseWorkbook, run_uid: str, *, now: datetime | None = None
) -> dict[str, int]:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    workbook.require_schema()
    data = workbook.read_tabs(
        [
            "Action_Updates",
            "_System_State",
            "Jobs_Master",
            "Applications",
            "Outreach",
            "Application_Events",
        ]
    )
    registrations = {str(row["key"]): row.get("value") for row in data["_System_State"]}
    jobs = {row["job_uid"] for row in data["Jobs_Master"]}
    counts = {"APPLIED": 0, "REPLAY": 0, "REJECTED": 0, "CHANGED_AFTER_SUBMIT": 0}
    for position, row in enumerate(data["Action_Updates"], 2):
        if row.get("submit") not in (True, "TRUE", "true", 1):
            continue
        uid = str(row.get("row_uid", ""))
        if registrations.get(f"action_row:{position}") != uid:
            workbook.write_cells(
                [
                    (
                        "Action_Updates",
                        position,
                        SCHEMA["Action_Updates"].columns.index("result") + 1,
                        "INVALID_ROW_UID",
                    )
                ]
            )
            counts["REJECTED"] += 1
            # Never use a changed row identifier to find/write another input row.
            continue
        digest = payload_hash(row)
        update_uid = update_identity(row)
        processed = row.get("processed_payload_hash")
        if processed and processed != digest:
            workbook.transaction(
                {"Action_Updates": [{"row_uid": uid, "result": "CHANGED_AFTER_SUBMIT"}]}, run_uid
            )
            counts["CHANGED_AFTER_SUBMIT"] += 1
            continue
        if row.get("update_uid") and row["update_uid"] != update_uid:
            workbook.transaction(
                {"Action_Updates": [{"row_uid": uid, "result": "INVALID_UPDATE_UID"}]}, run_uid
            )
            counts["REJECTED"] += 1
            continue
        event_uid = f"action:{update_uid}"
        if any(event.get("event_uid") == event_uid for event in data["Application_Events"]):
            counts["REPLAY"] += 1
            continue
        payload = normalized_payload(row)
        if payload["job_uid"] not in jobs:
            workbook.transaction(
                {"Action_Updates": [{"row_uid": uid, "result": "UNKNOWN_JOB_UID"}]}, run_uid
            )
            counts["REJECTED"] += 1
            continue
        prior = next(
            (item for item in data["Applications"] if item.get("job_uid") == payload["job_uid"]), {}
        )
        previous = str(prior.get("application_stage", "NOT_REVIEWED"))
        following = payload["application_stage_update"] or previous
        correction = payload["reason"].startswith("CORRECTION:")
        referral = payload["referral_stage_update"]
        outreach = payload["outreach_stage_update"]
        if (
            not valid_transition(previous, following, payload["reason"], correction=correction)
            or (referral and referral not in REFERRAL_STAGES)
            or (outreach and outreach not in OUTREACH_STAGES)
        ):
            workbook.transaction(
                {"Action_Updates": [{"row_uid": uid, "result": "INVALID_TRANSITION"}]}, run_uid
            )
            counts["REJECTED"] += 1
            continue
        claim = {
            "row_uid": uid,
            "update_uid": update_uid,
            "submitted_at": row.get("submitted_at") or now.isoformat(),
            "processed_payload_hash": digest,
            "result": "PROCESSING",
        }
        workbook.transaction({"Action_Updates": [claim]}, run_uid)
        current_rows = workbook.read_tab("Action_Updates")
        current = current_rows[position - 2]
        if current.get("row_uid") != uid or payload_hash(current) != digest:
            # A protected UID is a contract; an owner may override protection.
            # Never rebind this command to a different job after its claim.
            workbook.write_cells(
                [
                    (
                        "Action_Updates",
                        position,
                        SCHEMA["Action_Updates"].columns.index("result") + 1,
                        "CHANGED_AFTER_SUBMIT",
                    )
                ]
            )
            counts["CHANGED_AFTER_SUBMIT"] += 1
            continue
        app_uid = str(prior.get("application_uid") or f"app:{payload['job_uid']}")
        application = {
            **prior,
            "application_uid": app_uid,
            "job_uid": payload["job_uid"],
            "application_stage": following,
            "updated_at": now.isoformat(),
            "created_at": prior.get("created_at") or now.isoformat(),
            "last_update_uid": update_uid,
        }
        if following in ACTIVE | {"ACCEPTED", "DECLINED"} and not application.get("applied_at"):
            application["applied_at"] = now.isoformat()
        if referral:
            application["referral_stage"] = referral
        if payload["update_note"]:
            application["notes"] = payload["update_note"]
        event = {
            "event_uid": event_uid,
            "application_uid": app_uid,
            "job_uid": payload["job_uid"],
            "event_type": "MANUAL_CORRECTION" if correction else "ACTION_UPDATE",
            "from_stage": previous,
            "to_stage": following,
            "event_at": now.isoformat(),
            "actor": "user",
            "source": "Action_Updates",
            "reason": payload["reason"],
            "details": json.dumps(payload, sort_keys=True),
            "run_uid": run_uid,
            "created_at": now.isoformat(),
        }
        updates: dict[str, list[dict[str, Any]]] = {
            "Applications": [application],
            "Application_Events": [event],
            "Action_Updates": [
                {
                    **claim,
                    "processed_at": now.isoformat(),
                    "result": "APPLIED",
                    "event_uid": event_uid,
                }
            ],
        }
        if outreach:
            drafts = [
                draft for draft in data["Outreach"] if draft.get("job_uid") == payload["job_uid"]
            ]
            if len(drafts) != 1:
                workbook.transaction(
                    {"Action_Updates": [{"row_uid": uid, "result": "OUTREACH_TARGET_AMBIGUOUS"}]},
                    run_uid,
                )
                counts["REJECTED"] += 1
                continue
            draft = {"draft_id": drafts[0]["draft_id"], "outreach_stage": outreach}
            if outreach == "SENT_MANUALLY":
                draft["sent_at"] = now.isoformat()
            updates["Outreach"] = [draft]
        workbook.transaction(updates, run_uid)
        data["Application_Events"].append(event)
        data["Applications"] = [
            item for item in data["Applications"] if item.get("job_uid") != payload["job_uid"]
        ] + [application]
        # Re-read after the atomic acknowledgement as well: owner edits do not
        # become a second command, even when they race the completion request.
        after = workbook.read_tab("Action_Updates")[position - 2]
        if after.get("row_uid") != uid or payload_hash(after) != digest:
            workbook.write_cells(
                [
                    (
                        "Action_Updates",
                        position,
                        SCHEMA["Action_Updates"].columns.index("result") + 1,
                        "CHANGED_AFTER_SUBMIT",
                    )
                ]
            )
            counts["CHANGED_AFTER_SUBMIT"] += 1
        else:
            counts["APPLIED"] += 1
    return counts
