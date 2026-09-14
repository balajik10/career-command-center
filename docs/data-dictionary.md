# Sheet data dictionary

Schema version: **1**. All identity keys are stable text; dates are ISO 8601 UTC in storage and explicitly IST in user-facing views. Numeric scores are bounded 0–100. Null/blank means unavailable, never zero or permission. Serialized `*_json`, provenance, evidence lists, dimensions and metadata are JSON text. External text is sanitized and written with RAW input mode; trusted application formulas use USER_ENTERED.

## Shared state machines

Application stages: `NOT_REVIEWED`, `REVIEWING`, `READY_TO_APPLY`, `SKIPPED`, `APPLIED`, `OA`, `RECRUITER_SCREEN`, `TECH_SCREEN`, `HM_ROUND`, `INTERVIEW_LOOP`, `OFFER`, `ACCEPTED`, `DECLINED`, `REJECTED`, `WITHDRAWN`, `ROLE_CLOSED`.

Pre-apply stages may move to active stages; interview steps can be skipped or reordered. Terminal stages require an explicitly reasoned correction to reopen. `SKIPPED` requires a reason. Every submitted mutation has one immutable Application_Events record. Referral state is independent: `NOT_STARTED`, `WARM_CONNECTION_FOUND`, `DRAFT_READY`, `ASK_SENT`, `AGREED`, `REFERRED`, `DECLINED`, `NO_RESPONSE`, `NOT_NEEDED`, `STOPPED`.

Outreach stages: `DRAFTED`, `REVIEWED`, `SENT_MANUALLY`, `REPLIED`, `FOLLOW_UP_DUE`, `CLOSED`, `DO_NOT_CONTACT`. Only user-driven changes may record sent/replied status. Notification delivery states are `PENDING → SENDING → SENT | FAILED | AMBIGUOUS`. An interrupted SENDING claim becomes AMBIGUOUS, never automatically PENDING.

Policy, coverage and operational health are independent source dimensions. Source attempts preserve outcomes even when the displayed health is overridden by higher-precedence stale/down states. Audit is terminal diagnostic output and never drives operational decisions.

## START_HERE

Key: `section`. Protected view; no user input.

| Column | Meaning / ownership |
| --- | --- |
| `section` | Section. Computed. |
| `instruction` | Instruction. Computed. |

## Dashboard

Key: `metric`. Protected view; no user input.

| Column | Meaning / ownership |
| --- | --- |
| `metric` | Metric. Computed. |
| `value` | Private configuration/evidence/state value, interpreted by its stable key. Computed. |
| `description` | Description. Computed. |
| `interview_rate` | Cohort interviewed jobs / applied jobs; NO APPLICATIONS when denominator is empty. Computed. |
| `sent_outreach` | Cohort drafts with recorded sent timestamps, excluding future timestamps. Computed. |
| `replied_outreach` | Sent drafts with an explicitly recorded reply; never inferred from generated text. Computed. |
| `reply_rate` | Replied drafts / sent drafts; NO SENT OUTREACH when denominator is empty. Computed. |

Dashboard sections reuse the protected first three columns for labels/counts and spill seven-column source, role and company-tier tables. The cohort tables count each canonical job once, attribute source to its first recorded appearance, and preserve explicitly recorded interview-stage history. Consult [Dashboard definitions](sheet-guide.md#dashboard-definitions) for time, denominator and compensation rules.

## Action_Queue

Key: `job_uid`. Protected view; no user input.

| Column | Meaning / ownership |
| --- | --- |
| `job_uid` | Canonical opening identity, shared across source appearances. Computed. |
| `action_priority` | Action priority. Computed. |
| `overdue` | Overdue. Computed. |
| `action_by_ist` | Action by ist. Computed. |
| `company` | Company. Computed. |
| `title` | Title. Computed. |
| `posted_at_source` | Posted at source. Computed. |
| `first_seen_at` | First seen at. Computed. |
| `fit_score` | Fit score. Computed. |
| `next_action` | Next action. Computed. |
| `apply_url` | Apply url. Computed. |
| `best_contact` | Best contact. Computed. |
| `application_stage` | User-controlled application state; no row means derived NOT_STARTED. Computed. |
| `referral_stage` | Independent, user-confirmed referral progress. Computed. |
| `outreach_stage` | Outreach stage. Computed. |
| `canonical_link` | Canonical link. Computed. |
| `update_link` | Update link. Computed. |
| `freshness_basis` | Freshness basis. Computed. |
| `salary_state` | Salary state. Computed. |

## Action_Updates

Key: `row_uid`. User-editable fields: `job_uid`, `application_stage_update`, `referral_stage_update`, `outreach_stage_update`, `update_note`, `reason`, `submit`. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `row_uid` | Protected random UUID; never recycled or rebound to another physical input row. Machine. |
| `update_uid` | SHA-256 of protected row UUID and normalized immutable submitted payload. Machine. |
| `job_uid` | Canonical opening identity, shared across source appearances. User. |
| `application_stage_update` | Application stage update. User. |
| `referral_stage_update` | Referral stage update. User. |
| `outreach_stage_update` | Outreach stage update. User. |
| `update_note` | Update note. User. |
| `reason` | Evidence/reason for decision or user correction. User. |
| `submit` | Submit. User. |
| `submitted_at` | Submitted at. Machine. |
| `processed_payload_hash` | Frozen submission hash used to detect changed submitted rows. Machine. |
| `processed_at` | Processed at. Machine. |
| `result` | Result. Machine. |
| `event_uid` | Event uid. Machine. |

## Today

Key: `job_uid`. Protected view; no user input.

| Column | Meaning / ownership |
| --- | --- |
| `job_uid` | Canonical opening identity, shared across source appearances. Computed. |
| `company` | Company. Computed. |
| `title` | Title. Computed. |
| `action_priority` | Action priority. Computed. |
| `fit_score` | Fit score. Computed. |
| `apply_url` | Apply url. Computed. |
| `first_seen_at` | First seen at. Computed. |
| `posted_at_source` | Posted at source. Computed. |
| `freshness_basis` | Freshness basis. Computed. |
| `next_action` | Next action. Computed. |
| `application_stage` | User-controlled application state; no row means derived NOT_STARTED. Computed. |
| `referral_stage` | Independent, user-confirmed referral progress. Computed. |
| `salary_state` | Salary state. Computed. |
| `canonical_link` | Canonical link. Computed. |
| `section` | Section. Computed. |

## This_Week

Key: `job_uid`. Protected view; no user input.

| Column | Meaning / ownership |
| --- | --- |
| `job_uid` | Canonical opening identity, shared across source appearances. Computed. |
| `company` | Company. Computed. |
| `title` | Title. Computed. |
| `action_priority` | Action priority. Computed. |
| `fit_score` | Fit score. Computed. |
| `apply_url` | Apply url. Computed. |
| `first_seen_at` | First seen at. Computed. |
| `posted_at_source` | Posted at source. Computed. |
| `freshness_basis` | Freshness basis. Computed. |
| `next_action` | Next action. Computed. |
| `application_stage` | User-controlled application state; no row means derived NOT_STARTED. Computed. |
| `referral_stage` | Independent, user-confirmed referral progress. Computed. |
| `salary_state` | Salary state. Computed. |
| `canonical_link` | Canonical link. Computed. |

## This_Month

Key: `job_uid`. Protected view; no user input.

| Column | Meaning / ownership |
| --- | --- |
| `job_uid` | Canonical opening identity, shared across source appearances. Computed. |
| `company` | Company. Computed. |
| `title` | Title. Computed. |
| `action_priority` | Action priority. Computed. |
| `fit_score` | Fit score. Computed. |
| `apply_url` | Apply url. Computed. |
| `first_seen_at` | First seen at. Computed. |
| `posted_at_source` | Posted at source. Computed. |
| `freshness_basis` | Freshness basis. Computed. |
| `next_action` | Next action. Computed. |
| `application_stage` | User-controlled application state; no row means derived NOT_STARTED. Computed. |
| `referral_stage` | Independent, user-confirmed referral progress. Computed. |
| `salary_state` | Salary state. Computed. |
| `canonical_link` | Canonical link. Computed. |

## Jobs_Master

Key: `job_uid`. User-editable fields: `notes`, `manual_override_flags`, `do_not_merge`. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `job_uid` | Canonical opening identity, shared across source appearances. Machine. |
| `dedupe_group_id` | Dedupe group id. Machine. |
| `company` | Company. Machine. |
| `normalized_company` | Normalized company. Machine. |
| `title` | Title. Machine. |
| `normalized_title` | Normalized title. Machine. |
| `role_family` | Role family. Machine. |
| `seniority` | Seniority. Machine. |
| `department` | Department. Machine. |
| `source_id` | Source id. Machine. |
| `source_type` | Source type. Machine. |
| `source_job_id` | Source job id. Machine. |
| `requisition_id` | Requisition id. Machine. |
| `canonical_url` | Canonical url. Machine. |
| `apply_url` | Apply url. Machine. |
| `source_urls` | Source urls. Machine. |
| `source_confidence` | Source confidence. Machine. |
| `location` | Location. Machine. |
| `city` | City. Machine. |
| `country` | Country. Machine. |
| `work_mode` | Work mode. Machine. |
| `visa_text` | Visa text. Machine. |
| `employment_type` | Employment type. Machine. |
| `experience_text` | Experience text. Machine. |
| `min_years` | Min years. Machine. |
| `max_years` | Max years. Machine. |
| `experience_fit` | Experience fit. Machine. |
| `education_constraint` | Education constraint. Machine. |
| `posted_at_source` | Posted at source. Machine. |
| `posted_at_confidence` | Posted at confidence. Machine. |
| `first_seen_at` | First seen at. Machine. |
| `last_seen_at` | Last seen at. Machine. |
| `last_changed_at` | Last changed at. Machine. |
| `deadline` | Deadline. Machine. |
| `age_hours_min` | Age hours min. Machine. |
| `age_hours_max` | Age hours max. Machine. |
| `freshness_basis` | Freshness basis. Machine. |
| `freshness_band` | Freshness band. Machine. |
| `primary_source_policy_state` | Primary source policy state. Machine. |
| `primary_source_coverage_state` | Primary source coverage state. Machine. |
| `primary_source_health_state` | Primary source health state. Machine. |
| `active_state` | Active state. Machine. |
| `last_verified` | Last verified. Machine. |
| `summary` | Summary. Machine. |
| `responsibilities_summary` | Responsibilities summary. Machine. |
| `required_skills` | Required skills. Machine. |
| `preferred_skills` | Preferred skills. Machine. |
| `description_hash` | Description hash. Machine. |
| `salary_base_min` | Salary base min. Machine. |
| `salary_base_max` | Salary base max. Machine. |
| `salary_total_min` | Salary total min. Machine. |
| `salary_total_max` | Salary total max. Machine. |
| `salary_currency` | Salary currency. Machine. |
| `salary_period` | Salary period. Machine. |
| `salary_source` | Salary source. Machine. |
| `salary_confidence` | Salary confidence. Machine. |
| `fit_score` | Fit score. Machine. |
| `dimensions` | Dimensions. Machine. |
| `reasons` | Reasons. Machine. |
| `gaps` | Gaps. Machine. |
| `priority_score` | Priority score. Machine. |
| `score_band` | Score band. Machine. |
| `action_priority` | Action priority. Machine. |
| `action_eligibility` | Action eligibility. Machine. |
| `ethical_edge_summary` | Ethical edge summary. Machine. |
| `next_action` | Next action. Machine. |
| `action_by_ist` | Action by ist. Machine. |
| `official_link_state` | Official link state. Machine. |
| `exclusion_reason` | Exclusion reason. Machine. |
| `application_stage` | User-controlled application state; no row means derived NOT_STARTED. Machine. |
| `referral_stage` | Independent, user-confirmed referral progress. Machine. |
| `best_contact` | Best contact. Machine. |
| `outreach_stage` | Outreach stage. Machine. |
| `resume_version` | Resume version. Machine. |
| `latest_brief_id` | Latest brief id. Machine. |
| `latest_draft_id` | Latest draft id. Machine. |
| `notes` | User notes; discovery writes never overwrite these. User. |
| `schema_version` | Required compatibility marker; mismatches fail closed. Machine. |
| `parser_version` | Parser version. Machine. |
| `created_at` | Created at. Machine. |
| `updated_at` | Updated at. Machine. |
| `merge_confidence` | Merge confidence. Machine. |
| `manual_override_flags` | Manual override flags. User. |
| `do_not_merge` | Do not merge. User. |
| `description` | Description. Machine. |
| `identities` | Identities. Machine. |
| `provenance` | Provenance. Machine. |
| `metadata` | Metadata. Machine. |
| `baseline` | Baseline. Machine. |
| `material_change_sequence` | Material change sequence. Machine. |
| `missing_snapshots` | Missing snapshots. Machine. |
| `last_missing_at` | Last missing at. Machine. |
| `location_eligible` | Location eligible. Machine. |
| `internship_allowed` | Internship allowed. Machine. |
| `education_constraint_detail` | Education constraint detail. Machine. |
| `source_health` | Source health. Machine. |
| `quarantine_reason` | Quarantine reason. Machine. |
| `model_json` | Machine-owned serialization for faithful typed-model reconstruction; manual visible fields override relevant stored values. Machine. |
| `score_json` | Explainable typed score snapshot; numerical drift does not cause an alert. Machine. |

## Applications

Key: `application_uid`. User-editable fields: `application_stage`, `applied_at`, `application_channel`, `resume_version`, `cover_note_used`, `referral_timing`, `referral_stage`, `confirmation_id`, `next_step`, `next_step_due`, `interview_dates`, `outcome`, `rejection_stage`, `rejection_reason`, `manual_override_metadata`, `notes`. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `application_uid` | Application uid. Machine. |
| `job_uid` | Canonical opening identity, shared across source appearances. Machine. |
| `application_stage` | User-controlled application state; no row means derived NOT_STARTED. User. |
| `applied_at` | User-recorded application milestone; never inferred by discovery. User. |
| `application_channel` | Application channel. User. |
| `resume_version` | Resume version. User. |
| `cover_note_used` | Cover note used. User. |
| `referral_timing` | Referral timing. User. |
| `referral_stage` | Independent, user-confirmed referral progress. User. |
| `confirmation_id` | Confirmation id. User. |
| `next_step` | Next step. User. |
| `next_step_due` | Next step due. User. |
| `interview_dates` | Interview dates. User. |
| `outcome` | Outcome. User. |
| `rejection_stage` | Rejection stage. User. |
| `rejection_reason` | Rejection reason. User. |
| `manual_override_metadata` | Manual override metadata. User. |
| `notes` | User notes; discovery writes never overwrite these. User. |
| `created_at` | Created at. Machine. |
| `updated_at` | Updated at. Machine. |
| `last_update_uid` | Last update uid. Machine. |

## Application_Events

Key: `event_uid`. Append-only immutable audit trail. User-editable fields: none. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `event_uid` | Event uid. Machine. |
| `application_uid` | Application uid. Machine. |
| `job_uid` | Canonical opening identity, shared across source appearances. Machine. |
| `event_type` | Event type. Machine. |
| `from_stage` | From stage. Machine. |
| `to_stage` | To stage. Machine. |
| `event_at` | Event at. Machine. |
| `actor` | Actor. Machine. |
| `source` | Source. Machine. |
| `reason` | Evidence/reason for decision or user correction. Machine. |
| `details` | Details. Machine. |
| `run_uid` | Logical run identifier used for checkpoints and recovery. Machine. |
| `created_at` | Created at. Machine. |

## Job_History

Key: `job_event_uid`. Append-only immutable audit trail. User-editable fields: none. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `job_event_uid` | Job event uid. Machine. |
| `job_uid` | Canonical opening identity, shared across source appearances. Machine. |
| `event_type` | Event type. Machine. |
| `changed_fields` | Changed fields. Machine. |
| `before_hash` | Before hash. Machine. |
| `after_hash` | After hash. Machine. |
| `before_excerpt` | Before excerpt. Machine. |
| `after_excerpt` | After excerpt. Machine. |
| `source_id` | Source id. Machine. |
| `reason` | Evidence/reason for decision or user correction. Machine. |
| `confidence` | Confidence. Machine. |
| `occurred_at` | Occurred at. Machine. |
| `run_uid` | Logical run identifier used for checkpoints and recovery. Machine. |
| `created_at` | Created at. Machine. |

## Job_Briefs

Key: `brief_id`. User-editable fields: none. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `brief_id` | Brief id. Machine. |
| `job_uid` | Canonical opening identity, shared across source appearances. Machine. |
| `overview` | Overview. Machine. |
| `fit_summary` | Fit summary. Machine. |
| `evidence` | Evidence. Machine. |
| `gaps` | Gaps. Machine. |
| `application_strategy` | Application strategy. Machine. |
| `interview_prep` | Interview prep. Machine. |
| `resume_tailoring` | Resume tailoring. Machine. |
| `ethical_edge` | Ethical edge. Machine. |
| `generated_at` | Generated at. Machine. |
| `body` | Body. Machine. |
| `metadata` | Metadata. Machine. |
| `body_json` | Complete deterministic brief payload. Machine. |

## Contacts

Key: `contact_uid`. User-editable fields: `relationship_basis`, `whatsapp_allowed`, `outreach_allowed`, `do_not_contact`, `last_contacted_at`, `follow_up_count`, `reply_state`, `notes`. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `contact_uid` | Contact uid. Machine. |
| `name` | Name. Machine. |
| `company` | Company. Machine. |
| `title` | Title. Machine. |
| `team` | Team. Machine. |
| `relationship_basis` | Relationship basis. User. |
| `relationship_evidence` | Relationship evidence. Machine. |
| `current_company_confidence` | Current company confidence. Machine. |
| `linkedin_url` | Linkedin url. Machine. |
| `work_email` | Work email. Machine. |
| `phone` | Phone. Machine. |
| `whatsapp_allowed` | Whatsapp allowed. User. |
| `contactability_basis` | Observed permission basis; public visibility alone is insufficient. Machine. |
| `outreach_allowed` | Outreach allowed. User. |
| `contact_origin` | Contact origin. Machine. |
| `field_provenance` | Source and observed evidence for each stored contact field. Machine. |
| `evidence_url` | Evidence url. Machine. |
| `evidence_fetched_at` | Evidence fetched at. Machine. |
| `last_verified` | Last verified. Machine. |
| `retention_expires_at` | Retention expires at. Machine. |
| `do_not_contact` | Do not contact. User. |
| `suppression_token` | Domain-separated keyed HMAC retained after contact redaction/opt-out. Machine. |
| `hmac_key_version` | Hmac key version. Machine. |
| `last_contacted_at` | Last contacted at. User. |
| `follow_up_count` | Follow up count. User. |
| `reply_state` | Reply state. User. |
| `notes` | User notes; discovery writes never overwrite these. User. |
| `created_at` | Created at. Machine. |
| `updated_at` | Updated at. Machine. |

## Job_Contacts

Key: `job_contact_uid`. User-editable fields: `status`, `agreed`, `manual_override`. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `job_contact_uid` | Job contact uid. Machine. |
| `job_uid` | Canonical opening identity, shared across source appearances. Machine. |
| `contact_uid` | Contact uid. Machine. |
| `candidate_type` | Candidate type. Machine. |
| `rank` | Rank. Machine. |
| `confidence` | Confidence. Machine. |
| `why_this_contact` | Why this contact. Machine. |
| `recommended_channel` | Recommended channel. Machine. |
| `recommended_action` | Recommended action. Machine. |
| `status` | Status. User. |
| `agreed` | Agreed. User. |
| `manual_override` | Manual override. User. |

## Outreach

Key: `draft_id`. User-editable fields: `review_status`, `outreach_stage`, `sent_at`, `follow_up_due`, `reply_state`, `referral_result`, `attempt_count`, `notes`. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `draft_id` | Draft id. Machine. |
| `job_uid` | Canonical opening identity, shared across source appearances. Machine. |
| `contact_uid` | Contact uid. Machine. |
| `channel` | Channel. Machine. |
| `purpose` | Purpose. Machine. |
| `subject` | Subject. Machine. |
| `body` | Body. Machine. |
| `word_count` | Word count. Machine. |
| `character_count` | Character count. Machine. |
| `evidence_ids` | Evidence ids. Machine. |
| `generated_at` | Generated at. Machine. |
| `review_status` | Review status. User. |
| `outreach_stage` | Outreach stage. User. |
| `sent_at` | Definitive notification acceptance or user-recorded outreach milestone. User. |
| `follow_up_due` | Follow up due. User. |
| `reply_state` | Reply state. User. |
| `referral_result` | Referral result. User. |
| `attempt_count` | Attempt count. User. |
| `notes` | User notes; discovery writes never overwrite these. User. |

## Companies

Key: `company_uid`. User-editable fields: `company`, `career_url`, `company_tier`, `company_onboarding_state`, `enabled`, `notes`. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `company_uid` | Company uid. Machine. |
| `company` | Company. User. |
| `career_url` | Career url. User. |
| `company_tier` | Company tier. User. |
| `company_onboarding_state` | Company onboarding state. User. |
| `enabled` | Enabled. User. |
| `notes` | User notes; discovery writes never overwrite these. User. |

## Sources

Key: `source_id`. User-editable fields: `enabled`, `policy_state`, `cost_class`, `policy_reviewed_at`, `policy_review_due_at`, `policy_evidence_url`, `policy_evidence_hash`, `human_reviewer`, `robots_allowed`, `allowed_hosts`, `priority_tier`, `cadence_hours`, `notes`. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `source_id` | Source id. Machine. |
| `company` | Company. Machine. |
| `provider` | Provider. Machine. |
| `url` | Url. Machine. |
| `tenant` | Tenant. Machine. |
| `enabled` | Enabled. User. |
| `access_mode` | Access mode. Machine. |
| `policy_state` | Permission/access decision; separate from operational health. User. |
| `coverage_state` | Verified source coverage extent; no inferred coverage. Machine. |
| `health_state` | Displayed operational state with documented precedence. Machine. |
| `cost_class` | Cost class. User. |
| `policy_reviewed_at` | Policy reviewed at. User. |
| `policy_review_due_at` | Policy review due at. User. |
| `policy_evidence_url` | Policy evidence url. User. |
| `policy_evidence_hash` | Policy evidence hash. User. |
| `human_reviewer` | Human reviewer. User. |
| `robots_allowed` | Robots allowed. User. |
| `allowed_hosts` | Allowed hosts. User. |
| `priority_tier` | Priority tier. User. |
| `cadence_hours` | Cadence hours. User. |
| `next_due_at` | Next due at. Machine. |
| `last_success_at` | Last success at. Machine. |
| `last_attempt_at` | Last attempt at. Machine. |
| `backoff_until` | Backoff until. Machine. |
| `consecutive_failures` | Consecutive failures. Machine. |
| `baseline_state` | Baseline state. Machine. |
| `baseline_completed_at` | Baseline completed at. Machine. |
| `etag` | Etag. Machine. |
| `last_modified` | Last modified. Machine. |
| `cursor` | Cursor. Machine. |
| `notes` | User notes; discovery writes never overwrite these. User. |
| `metadata` | Metadata. Machine. |
| `last_outcome` | Last outcome. Machine. |
| `model_json` | Machine-owned serialization for faithful typed-model reconstruction; manual visible fields override relevant stored values. Machine. |

## Inbox

Key: `message_uid`. User-editable fields: none. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `message_uid` | Message uid. Machine. |
| `source_id` | Source id. Machine. |
| `message_hash` | Message hash. Machine. |
| `received_at` | Received at. Machine. |
| `parsed_at` | Parsed at. Machine. |
| `job_uids` | Job uids. Machine. |
| `state` | State. Machine. |
| `reason` | Evidence/reason for decision or user correction. Machine. |
| `run_uid` | Logical run identifier used for checkpoints and recovery. Machine. |

## Quarantine

Key: `quarantine_uid`. User-editable fields: `state`. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `quarantine_uid` | Quarantine uid. Machine. |
| `source_id` | Source id. Machine. |
| `reason` | Evidence/reason for decision or user correction. Machine. |
| `excerpt` | Excerpt. Machine. |
| `state` | State. User. |
| `retries` | Retries. Machine. |
| `created_at` | Created at. Machine. |
| `updated_at` | Updated at. Machine. |
| `run_uid` | Logical run identifier used for checkpoints and recovery. Machine. |

## Run_Log

Key: `run_uid`. User-editable fields: none. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `run_uid` | Logical run identifier used for checkpoints and recovery. Machine. |
| `status` | Status. Machine. |
| `mode` | Mode. Machine. |
| `started_at` | Started at. Machine. |
| `completed_at` | Completed at. Machine. |
| `observations` | Observations. Machine. |
| `canonical_jobs` | Canonical jobs. Machine. |
| `new_jobs` | New jobs. Machine. |
| `changed_jobs` | Changed jobs. Machine. |
| `new_drafts` | New drafts. Machine. |
| `alerts` | Alerts. Machine. |
| `source_counts` | Source counts. Machine. |
| `priority_counts` | Priority counts. Machine. |
| `errors` | Errors. Machine. |
| `github_run_attempt` | Retry attempt, distinct from logical GitHub run ID. Machine. |
| `chunks` | Chunks. Machine. |

## Profile

Key: `evidence_id`. User-editable fields: `kind`, `value`, `source`, `resume_section`, `technologies`, `domains`, `tags`, `allowed_in_outreach`, `resume_verbatim`, `outreach_safe_paraphrase`, `allowed_atomic_claims`, `forbidden_extrapolations`, `metrics`. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `evidence_id` | Evidence id. Machine. |
| `kind` | Kind. User. |
| `value` | Private configuration/evidence/state value, interpreted by its stable key. User. |
| `source` | Source. User. |
| `resume_section` | Resume section. User. |
| `technologies` | Technologies. User. |
| `domains` | Domains. User. |
| `tags` | Tags. User. |
| `allowed_in_outreach` | Allowed in outreach. User. |
| `resume_verbatim` | Resume verbatim. User. |
| `outreach_safe_paraphrase` | Outreach safe paraphrase. User. |
| `allowed_atomic_claims` | Allowed atomic claims. User. |
| `forbidden_extrapolations` | Forbidden extrapolations. User. |
| `metrics` | Metrics. User. |

## Audit

Key: `check_uid`. Protected view; no user input.

| Column | Meaning / ownership |
| --- | --- |
| `check_uid` | Check uid. Computed. |
| `check` | Check. Computed. |
| `state` | State. Computed. |
| `offending_ids` | Offending ids. Computed. |
| `checked_at` | Checked at. Computed. |
| `details` | Details. Computed. |

## Config

Key: `key`. User-editable fields: `value`. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `key` | Key. Machine. |
| `value` | Private configuration/evidence/state value, interpreted by its stable key. User. |
| `description` | Description. Machine. |

## _Job_Sources

Key: `job_source_uid`. Hidden system tab. User-editable fields: none. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `job_source_uid` | Job source uid. Machine. |
| `job_uid` | Canonical opening identity, shared across source appearances. Machine. |
| `source_id` | Source id. Machine. |
| `source_name` | Source name. Machine. |
| `external_job_id` | External job id. Machine. |
| `requisition_id` | Requisition id. Machine. |
| `listing_url` | Listing url. Machine. |
| `apply_url` | Apply url. Machine. |
| `raw_posted_at` | Raw posted at. Machine. |
| `normalized_posted_at` | Normalized posted at. Machine. |
| `first_seen_at` | First seen at. Machine. |
| `last_seen_at` | Last seen at. Machine. |
| `last_verified` | Last verified. Machine. |
| `primary` | Primary. Machine. |
| `active` | Active. Machine. |
| `content_hash` | Content hash. Machine. |
| `repost` | Repost. Machine. |
| `last_http_category` | Last http category. Machine. |
| `run_uid` | Logical run identifier used for checkpoints and recovery. Machine. |
| `created_at` | Created at. Machine. |
| `updated_at` | Updated at. Machine. |

## _Alerts

Key: `alert_id`. Hidden system tab. User-editable fields: none. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `alert_id` | Alert id. Machine. |
| `job_uid` | Canonical opening identity, shared across source appearances. Machine. |
| `alert_type` | Alert type. Machine. |
| `template_version` | Template version. Machine. |
| `material_version` | Hash of non-clock material facts and template version. Machine. |
| `bundle_id` | Bundle id. Machine. |
| `digest_id` | Digest id. Machine. |
| `member_alert_ids` | Member alert ids. Machine. |
| `digest_cursor` | Digest cursor. Machine. |
| `scheduled_window_end` | Scheduled window end. Machine. |
| `priority` | Priority. Machine. |
| `recipient_token` | Domain-separated keyed HMAC; no cleartext owner address. Machine. |
| `hmac_key_version` | Hmac key version. Machine. |
| `state` | State. Machine. |
| `created_at` | Created at. Machine. |
| `sending_at` | Sending at. Machine. |
| `sent_at` | Definitive notification acceptance or user-recorded outreach milestone. Machine. |
| `transport` | Transport. Machine. |
| `message_id` | Message id. Machine. |
| `provider_message_id` | Provider message id. Machine. |
| `acceptance_status` | Acceptance status. Machine. |
| `error` | Error. Machine. |
| `run_uid` | Logical run identifier used for checkpoints and recovery. Machine. |
| `github_run_attempt` | Retry attempt, distinct from logical GitHub run ID. Machine. |
| `automatic_attempts` | Maximum one for each automatically delivered alert ID. Machine. |

## _System_State

Key: `key`. Hidden system tab. User-editable fields: none. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `key` | Key. Machine. |
| `value` | Private configuration/evidence/state value, interpreted by its stable key. Machine. |
| `updated_at` | Updated at. Machine. |
| `run_uid` | Logical run identifier used for checkpoints and recovery. Machine. |

## _Lists

Key: `list_name`. Hidden system tab. User-editable fields: none. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `list_name` | List name. Machine. |
| `value` | Private configuration/evidence/state value, interpreted by its stable key. Machine. |

## _Source_Attempts

Key: `attempt_uid`. Hidden system tab. Append-only immutable audit trail. User-editable fields: none. All other fields are machine-owned.

| Column | Meaning / ownership |
| --- | --- |
| `attempt_uid` | Attempt uid. Machine. |
| `run_uid` | Logical run identifier used for checkpoints and recovery. Machine. |
| `source_id` | Source id. Machine. |
| `snapshot_kind` | Snapshot kind. Machine. |
| `attempt_outcome` | Attempt outcome. Machine. |
| `started_at` | Started at. Machine. |
| `completed_at` | Completed at. Machine. |
| `due_at` | Due at. Machine. |
| `pages` | Pages. Machine. |
| `requests` | Requests. Machine. |
| `bytes` | Bytes. Machine. |
| `jobs` | Jobs. Machine. |
| `snapshot_complete` | Snapshot complete. Machine. |
| `reason` | Evidence/reason for decision or user correction. Machine. |
| `cursor_before` | Cursor before. Machine. |
| `cursor_after` | Cursor after. Machine. |
| `watermark_advanced` | Watermark advanced. Machine. |
| `missing_counter_advanced` | Missing counter advanced. Machine. |
| `health_before` | Health before. Machine. |
| `health_after` | Health after. Machine. |
| `backoff_until` | Backoff until. Machine. |
| `error` | Error. Machine. |
| `created_at` | Created at. Machine. |
