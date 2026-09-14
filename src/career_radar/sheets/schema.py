"""Versioned Sheet schema, ownership and bootstrap presentation requests."""

from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "1"
PROJECT_MARKER = "career-command-center:v1"
INPUT_RESERVE = 100


@dataclass(frozen=True)
class Tab:
    columns: tuple[str, ...]
    key: str
    user_fields: frozenset[str] = frozenset()
    hidden: bool = False
    append_only: bool = False
    view: bool = False


def tab(
    columns: str,
    *,
    user: str = "",
    hidden: bool = False,
    append_only: bool = False,
    view: bool = False,
) -> Tab:
    names = tuple(columns.split())
    return Tab(names, names[0], frozenset(user.split()), hidden, append_only, view)


SCHEMA: dict[str, Tab] = {
    "START_HERE": tab("section instruction", view=True),
    "Dashboard": tab(
        "metric value description interview_rate sent_outreach replied_outreach reply_rate",
        view=True,
    ),
    "Action_Queue": tab(
        "job_uid action_priority overdue action_by_ist company title posted_at_source first_seen_at fit_score next_action apply_url best_contact application_stage referral_stage outreach_stage canonical_link update_link freshness_basis salary_state",
        view=True,
    ),
    "Action_Updates": tab(
        "row_uid update_uid job_uid application_stage_update referral_stage_update outreach_stage_update update_note reason submit submitted_at processed_payload_hash processed_at result event_uid",
        user="job_uid application_stage_update referral_stage_update outreach_stage_update update_note reason submit",
    ),
    "Today": tab(
        "job_uid company title action_priority fit_score apply_url first_seen_at posted_at_source freshness_basis next_action application_stage referral_stage salary_state canonical_link section",
        view=True,
    ),
    "This_Week": tab(
        "job_uid company title action_priority fit_score apply_url first_seen_at posted_at_source freshness_basis next_action application_stage referral_stage salary_state canonical_link",
        view=True,
    ),
    "This_Month": tab(
        "job_uid company title action_priority fit_score apply_url first_seen_at posted_at_source freshness_basis next_action application_stage referral_stage salary_state canonical_link",
        view=True,
    ),
    "Jobs_Master": tab(
        "job_uid dedupe_group_id company normalized_company title normalized_title role_family seniority department source_id source_type source_job_id requisition_id canonical_url apply_url source_urls source_confidence location city country work_mode visa_text employment_type experience_text min_years max_years experience_fit education_constraint posted_at_source posted_at_confidence first_seen_at last_seen_at last_changed_at deadline age_hours_min age_hours_max freshness_basis freshness_band primary_source_policy_state primary_source_coverage_state primary_source_health_state active_state last_verified summary responsibilities_summary required_skills preferred_skills description_hash salary_base_min salary_base_max salary_total_min salary_total_max salary_currency salary_period salary_source salary_confidence fit_score dimensions reasons gaps priority_score score_band action_priority action_eligibility ethical_edge_summary next_action action_by_ist official_link_state exclusion_reason application_stage referral_stage best_contact outreach_stage resume_version latest_brief_id latest_draft_id notes schema_version parser_version created_at updated_at merge_confidence manual_override_flags do_not_merge description identities provenance metadata baseline material_change_sequence missing_snapshots last_missing_at location_eligible internship_allowed education_constraint_detail source_health quarantine_reason model_json score_json",
        user="notes manual_override_flags do_not_merge",
    ),
    "Applications": tab(
        "application_uid job_uid application_stage applied_at application_channel resume_version cover_note_used referral_timing referral_stage confirmation_id next_step next_step_due interview_dates outcome rejection_stage rejection_reason manual_override_metadata notes created_at updated_at last_update_uid",
        user="application_stage applied_at application_channel resume_version cover_note_used referral_timing referral_stage confirmation_id next_step next_step_due interview_dates outcome rejection_stage rejection_reason manual_override_metadata notes",
    ),
    "Application_Events": tab(
        "event_uid application_uid job_uid event_type from_stage to_stage event_at actor source reason details run_uid created_at",
        append_only=True,
    ),
    "Job_History": tab(
        "job_event_uid job_uid event_type changed_fields before_hash after_hash before_excerpt after_excerpt source_id reason confidence occurred_at run_uid created_at",
        append_only=True,
    ),
    "Job_Briefs": tab(
        "brief_id job_uid overview fit_summary evidence gaps application_strategy interview_prep resume_tailoring ethical_edge generated_at body metadata body_json"
    ),
    "Contacts": tab(
        "contact_uid name company title team relationship_basis relationship_evidence current_company_confidence linkedin_url work_email phone whatsapp_allowed contactability_basis outreach_allowed contact_origin field_provenance evidence_url evidence_fetched_at last_verified retention_expires_at do_not_contact suppression_token hmac_key_version last_contacted_at follow_up_count reply_state notes created_at updated_at",
        user="relationship_basis whatsapp_allowed outreach_allowed do_not_contact last_contacted_at follow_up_count reply_state notes",
    ),
    "Job_Contacts": tab(
        "job_contact_uid job_uid contact_uid candidate_type rank confidence why_this_contact recommended_channel recommended_action status agreed manual_override",
        user="status agreed manual_override",
    ),
    "Outreach": tab(
        "draft_id job_uid contact_uid channel purpose subject body word_count character_count evidence_ids generated_at review_status outreach_stage sent_at follow_up_due reply_state referral_result attempt_count notes",
        user="review_status outreach_stage sent_at follow_up_due reply_state referral_result attempt_count notes",
    ),
    "Companies": tab(
        "company_uid company career_url company_tier company_onboarding_state enabled notes",
        user="company career_url company_tier company_onboarding_state enabled notes",
    ),
    "Sources": tab(
        "source_id company provider url tenant enabled access_mode policy_state coverage_state health_state cost_class policy_reviewed_at policy_review_due_at policy_evidence_url policy_evidence_hash human_reviewer robots_allowed allowed_hosts priority_tier cadence_hours next_due_at last_success_at last_attempt_at backoff_until consecutive_failures baseline_state baseline_completed_at etag last_modified cursor notes metadata last_outcome model_json",
        user="enabled policy_state cost_class policy_reviewed_at policy_review_due_at policy_evidence_url policy_evidence_hash human_reviewer robots_allowed allowed_hosts priority_tier cadence_hours notes",
    ),
    "Inbox": tab(
        "message_uid source_id message_hash received_at parsed_at job_uids state reason run_uid"
    ),
    "Quarantine": tab(
        "quarantine_uid source_id reason excerpt state retries created_at updated_at run_uid",
        user="state",
    ),
    "Run_Log": tab(
        "run_uid status mode started_at completed_at observations canonical_jobs new_jobs changed_jobs new_drafts alerts source_counts priority_counts errors github_run_attempt chunks"
    ),
    "Profile": tab(
        "evidence_id kind value source resume_section technologies domains tags allowed_in_outreach resume_verbatim outreach_safe_paraphrase allowed_atomic_claims forbidden_extrapolations metrics",
        user="kind value source resume_section technologies domains tags allowed_in_outreach resume_verbatim outreach_safe_paraphrase allowed_atomic_claims forbidden_extrapolations metrics",
    ),
    "Audit": tab("check_uid check state offending_ids checked_at details", view=True),
    "Config": tab("key value description", user="value"),
    "_Job_Sources": tab(
        "job_source_uid job_uid source_id source_name external_job_id requisition_id listing_url apply_url raw_posted_at normalized_posted_at first_seen_at last_seen_at last_verified primary active content_hash repost last_http_category run_uid created_at updated_at",
        hidden=True,
    ),
    "_Alerts": tab(
        "alert_id job_uid alert_type template_version material_version bundle_id digest_id member_alert_ids digest_cursor scheduled_window_end priority recipient_token hmac_key_version state created_at sending_at sent_at transport message_id provider_message_id acceptance_status error run_uid github_run_attempt automatic_attempts",
        hidden=True,
    ),
    "_System_State": tab("key value updated_at run_uid", hidden=True),
    "_Lists": tab("list_name value", hidden=True),
    "_Source_Attempts": tab(
        "attempt_uid run_uid source_id snapshot_kind attempt_outcome started_at completed_at due_at pages requests bytes jobs snapshot_complete reason cursor_before cursor_after watermark_advanced missing_counter_advanced health_before health_after backoff_until error created_at",
        hidden=True,
        append_only=True,
    ),
}

APPLICATION_STAGES = (
    "NOT_REVIEWED",
    "REVIEWING",
    "READY_TO_APPLY",
    "SKIPPED",
    "APPLIED",
    "OA",
    "RECRUITER_SCREEN",
    "TECH_SCREEN",
    "HM_ROUND",
    "INTERVIEW_LOOP",
    "OFFER",
    "ACCEPTED",
    "DECLINED",
    "REJECTED",
    "WITHDRAWN",
    "ROLE_CLOSED",
)
REFERRAL_STAGES = (
    "NOT_STARTED",
    "WARM_CONNECTION_FOUND",
    "DRAFT_READY",
    "ASK_SENT",
    "AGREED",
    "REFERRED",
    "DECLINED",
    "NO_RESPONSE",
    "NOT_NEEDED",
    "STOPPED",
)
OUTREACH_STAGES = (
    "DRAFTED",
    "REVIEWED",
    "SENT_MANUALLY",
    "REPLIED",
    "FOLLOW_UP_DUE",
    "CLOSED",
    "DO_NOT_CONTACT",
)
STATE_LISTS = {
    "application_stage": APPLICATION_STAGES,
    "application_stage_update": APPLICATION_STAGES,
    "referral_stage": REFERRAL_STAGES,
    "referral_stage_update": REFERRAL_STAGES,
    "outreach_stage": OUTREACH_STAGES,
    "outreach_stage_update": OUTREACH_STAGES,
    "policy_state": (
        "APPROVED",
        "PENDING_REVIEW",
        "MANUAL_ONLY",
        "LOGIN_REQUIRED",
        "POLICY_BLOCKED",
        "DISABLED",
    ),
    "coverage_state": ("UNVALIDATED", "VERIFIED_ACTIVE", "VERIFIED_EMPTY", "NATIVE_ALERT_ONLY"),
    "health_state": (
        "UNKNOWN",
        "HEALTHY",
        "DEGRADED",
        "BACKOFF",
        "STALE",
        "DOWN",
        "AUTH_REQUIRED",
        "PAUSED",
    ),
    "action_priority": ("P0", "P1", "P2", "P3", "PIPELINE", "EXCLUDED"),
}
BOOLEAN_FIELDS = {
    "submit",
    "enabled",
    "whatsapp_allowed",
    "outreach_allowed",
    "do_not_contact",
    "agreed",
    "do_not_merge",
    "allowed_in_outreach",
    "robots_allowed",
}


def column_letter(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def presentation_requests(sheet_ids: dict[str, int]) -> list[dict[str, Any]]:
    """Request-level atomic bootstrap; existing user values are never included."""
    requests: list[dict[str, Any]] = [
        {
            "updateSpreadsheetProperties": {
                "properties": {"timeZone": "Asia/Kolkata", "locale": "en_IN"},
                "fields": "timeZone,locale",
            }
        }
    ]
    for name, spec in SCHEMA.items():
        sid = sheet_ids[name]
        grid = {"sheetId": sid}
        requests.extend(
            [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sid,
                            "hidden": spec.hidden,
                            "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 1},
                        },
                        "fields": "hidden,gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
                    }
                },
                {
                    "repeatCell": {
                        "range": {**grid, "endRowIndex": 1},
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.12, "green": 0.2, "blue": 0.3},
                                "textFormat": {
                                    "bold": True,
                                    "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                },
                            }
                        },
                        "fields": "userEnteredFormat",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sid,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": len(spec.columns),
                        },
                        "properties": {"pixelSize": 155},
                        "fields": "pixelSize",
                    }
                },
            ]
        )
        if not spec.view:
            requests.append(
                {
                    "setBasicFilter": {
                        "filter": {"range": {**grid, "endColumnIndex": len(spec.columns)}}
                    }
                }
            )
        unprotected = [
            {**grid, "startRowIndex": 1, "startColumnIndex": i, "endColumnIndex": i + 1}
            for i, field in enumerate(spec.columns)
            if field in spec.user_fields
        ]
        protection: dict[str, Any] = {
            "range": grid,
            "description": f"Career Command Center v{SCHEMA_VERSION}: {name}",
            "warningOnly": False,
        }
        if unprotected:
            protection["unprotectedRanges"] = unprotected
        requests.append({"addProtectedRange": {"protectedRange": protection}})
        for i, field in enumerate(spec.columns):
            cell_range = {
                **grid,
                "startRowIndex": 1,
                "startColumnIndex": i,
                "endColumnIndex": i + 1,
            }
            requests.append(
                {
                    "updateCells": {
                        "range": {
                            **grid,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": i,
                            "endColumnIndex": i + 1,
                        },
                        "rows": [
                            {
                                "values": [
                                    {
                                        "note": f"{field}: {'user editable' if field in spec.user_fields else 'computed/protected'}; dates use IST"
                                    }
                                ]
                            }
                        ],
                        "fields": "note",
                    }
                }
            )
            if field in spec.user_fields:
                requests.append(
                    {
                        "repeatCell": {
                            "range": cell_range,
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": {"red": 1, "green": 0.97, "blue": 0.8}
                                }
                            },
                            "fields": "userEnteredFormat.backgroundColor",
                        }
                    }
                )
            if field in STATE_LISTS or field in BOOLEAN_FIELDS:
                condition: dict[str, Any] = (
                    {"type": "BOOLEAN"}
                    if field in BOOLEAN_FIELDS
                    else {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": item} for item in STATE_LISTS[field]],
                    }
                )
                requests.append(
                    {
                        "setDataValidation": {
                            "range": cell_range,
                            "rule": {"condition": condition, "strict": True, "showCustomUi": True},
                        }
                    }
                )
            if field in {
                "work_email",
                "phone",
                "field_provenance",
                "identities",
                "provenance",
                "metadata",
                "description_hash",
                "processed_payload_hash",
                "model_json",
                "score_json",
                "body_json",
            }:
                requests.append(
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sid,
                                "dimension": "COLUMNS",
                                "startIndex": i,
                                "endIndex": i + 1,
                            },
                            "properties": {"hiddenByUser": True},
                            "fields": "hiddenByUser",
                        }
                    }
                )
            if field in {"action_priority", "application_stage", "active_state"}:
                for label, color in (
                    ("P0", {"red": 1, "green": 0.8, "blue": 0.8}),
                    ("P1", {"red": 1, "green": 0.9, "blue": 0.65}),
                    ("APPLIED", {"red": 0.75, "green": 0.85, "blue": 1}),
                    ("INTERVIEW_LOOP", {"red": 0.85, "green": 0.75, "blue": 1}),
                    ("OFFER", {"red": 0.7, "green": 0.95, "blue": 0.75}),
                    ("CLOSED", {"red": 0.85, "green": 0.85, "blue": 0.85}),
                ):
                    requests.append(
                        {
                            "addConditionalFormatRule": {
                                "rule": {
                                    "ranges": [cell_range],
                                    "booleanRule": {
                                        "condition": {
                                            "type": "TEXT_EQ",
                                            "values": [{"userEnteredValue": label}],
                                        },
                                        "format": {"backgroundColor": color},
                                    },
                                },
                                "index": 0,
                            }
                        }
                    )
    return requests
