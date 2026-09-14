"""Strict shared contracts. UTC-aware datetimes and explicit provenance throughout."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Provenance(Model):
    source_id: str
    url: str = ""
    fetched_at: AwareDatetime = Field(default_factory=utcnow)
    parser_version: str = "1"
    confidence: str = "OBSERVED"
    evidence: str = ""


class SourceDefinition(Model):
    source_id: str
    company: str
    provider: str
    url: str
    tenant: str = ""
    enabled: bool = False
    access_mode: Literal[
        "OFFICIAL_API",
        "RSS_ATOM",
        "EMAIL_ALERT",
        "PUBLIC_HTML_APPROVED",
        "USER_IMPORT",
        "MANUAL_REVIEW",
    ] = "MANUAL_REVIEW"
    policy_state: Literal[
        "PENDING_REVIEW", "APPROVED", "MANUAL_ONLY", "LOGIN_REQUIRED", "POLICY_BLOCKED", "DISABLED"
    ] = "PENDING_REVIEW"
    coverage_state: Literal[
        "UNVALIDATED", "VERIFIED_ACTIVE", "VERIFIED_EMPTY", "NATIVE_ALERT_ONLY"
    ] = "UNVALIDATED"
    health_state: Literal[
        "UNKNOWN", "HEALTHY", "DEGRADED", "BACKOFF", "STALE", "DOWN", "AUTH_REQUIRED", "PAUSED"
    ] = "UNKNOWN"
    cost_class: Literal["free", "paid", "unknown"] = "unknown"
    policy_reviewed_at: AwareDatetime | None = None
    policy_review_due_at: AwareDatetime | None = None
    policy_evidence_url: str = ""
    policy_evidence_hash: str = ""
    human_reviewer: str = ""
    robots_allowed: bool | None = None
    allowed_hosts: list[str] = Field(default_factory=list)
    priority_tier: int = 2
    cadence_hours: float = 24
    next_due_at: AwareDatetime | None = None
    last_success_at: AwareDatetime | None = None
    last_attempt_at: AwareDatetime | None = None
    backoff_until: AwareDatetime | None = None
    consecutive_failures: int = 0
    baseline_state: Literal["NOT_STARTED", "IN_PROGRESS", "COMPLETED"] = "NOT_STARTED"
    baseline_completed_at: AwareDatetime | None = None
    etag: str = ""
    last_modified: str = ""
    cursor: str = ""
    notes: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class FetchResult(Model):
    source_id: str
    url: str
    status_code: int = 200
    body: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    fetched_at: AwareDatetime = Field(default_factory=utcnow)
    bytes_received: int = 0
    request_count: int = 1
    snapshot_complete: bool = True
    outcome: str = "SUCCESS_COMPLETE"
    error_code: str = ""
    checkpoint: dict[str, str] = Field(default_factory=dict)
    listed_ids: list[str] = Field(default_factory=list)
    listing_complete: bool = False


class RawJob(Model):
    source_id: str
    provider: str
    provider_job_id: str = ""
    tenant: str = ""
    title: str
    company: str
    location: str = ""
    description: str = ""
    url: str = ""
    apply_url: str = ""
    fetched_at: AwareDatetime = Field(default_factory=utcnow)
    posted_at_raw: str | None = None
    updated_at_raw: str | None = None
    deadline_raw: str | None = None
    employment_type: str = ""
    salary_text: str = ""
    requisition_id: str = ""
    requisition_namespace: str = ""
    official_link_state: Literal[
        "VERIFIED_OFFICIAL", "OFFICIAL_LINK_UNVERIFIED", "NOT_AVAILABLE", "INVALID"
    ] = "OFFICIAL_LINK_UNVERIFIED"
    metadata: dict[str, Any] = Field(default_factory=dict)
    parser_version: str = "1"


class JobIdentity(Model):
    provider: str
    tenant: str = ""
    provider_job_id: str = ""
    canonical_url: str = ""
    requisition_id: str = ""
    requisition_namespace: str = ""


class NormalizedJob(Model):
    job_uid: str
    dedupe_group_id: str = ""
    company: str
    normalized_company: str = ""
    title: str
    normalized_title: str = ""
    role_family: str = "UNKNOWN"
    seniority: str = "UNKNOWN"
    location: str = ""
    work_mode: str = "UNKNOWN"
    location_eligible: bool | None = None
    visa_text: str = ""
    description: str = ""
    description_hash: str = ""
    summary: str = ""
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    experience_text: str = ""
    min_years: float | None = None
    max_years: float | None = None
    experience_confidence: str = "MISSING"
    internship_allowed: bool = False
    education_constraint: str = ""
    employment_type: str = ""
    canonical_url: str = ""
    apply_url: str = ""
    official_link_state: str = "OFFICIAL_LINK_UNVERIFIED"
    identities: list[JobIdentity] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)
    posted_at_source: AwareDatetime | None = None
    posted_at_confidence: str = "MISSING"
    age_hours_min: float | None = None
    age_hours_max: float | None = None
    updated_at_source: AwareDatetime | None = None
    deadline: AwareDatetime | None = None
    first_seen_at: AwareDatetime = Field(default_factory=utcnow)
    last_seen_at: AwareDatetime = Field(default_factory=utcnow)
    last_changed_at: AwareDatetime | None = None
    freshness_basis: str = "FIRST_SEEN_PROXY"
    freshness_band: str = "BASELINE_DATE_UNKNOWN"
    baseline: bool = True
    active_state: str = "NEW"
    salary_base_min: float | None = None
    salary_base_max: float | None = None
    salary_total_min: float | None = None
    salary_total_max: float | None = None
    salary_currency: str = ""
    salary_period: str = ""
    salary_source: str = ""
    salary_confidence: str = "MISSING"
    source_health: str = "UNKNOWN"
    quarantine_reason: str = ""
    exclusion_reason: str = ""
    material_change_sequence: int = 0
    missing_snapshots: int = 0
    last_missing_at: AwareDatetime | None = None
    do_not_merge: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class CandidateEvidence(Model):
    evidence_id: str
    resume_verbatim: str = ""
    outreach_safe_paraphrase: str
    allowed_atomic_claims: list[str] = Field(default_factory=list)
    forbidden_extrapolations: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    metrics: dict[str, str] = Field(default_factory=dict)
    source: str = "resume"
    allowed_in_outreach: bool = False


class CandidateProfile(Model):
    name: str = "Example Candidate"
    location: str = "Bengaluru, India"
    full_time_start: str = "2025-01"
    internship_start: str | None = None
    internship_end: str | None = None
    graduation_year: int = 2024
    skills: list[str] = Field(default_factory=list)
    evidence: list[CandidateEvidence] = Field(default_factory=list)
    current_base_lpa: float | None = None
    target_base_lpa: float | None = None
    company_tiers: dict[str, float] = Field(default_factory=dict)
    github_url: str = ""
    linkedin_url: str = ""
    mandatory_rapid_source_ids: list[str] = Field(default_factory=list)


class JobScore(Model):
    job_uid: str
    fit_score: float = Field(ge=0, le=100)
    priority_score: float = Field(ge=0, le=100)
    dimensions: dict[str, float] = Field(default_factory=dict)
    components: dict[str, float] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    experience_fit: str = "AMBIGUOUS"
    tenure_min_years: float = 0
    tenure_max_years: float = 0
    score_band: str
    action_priority: str
    action_eligibility: str
    exclusion_reason: str = ""
    next_action: str = "Review posting"
    action_by_ist: str = ""


class Contact(Model):
    contact_uid: str
    name: str | None = None
    company: str | None = None
    title: str | None = None
    linkedin_url: str | None = None
    work_email: str | None = None
    phone: str | None = None
    contact_origin: str
    evidence_url: str = ""
    evidence_fetched_at: AwareDatetime = Field(default_factory=utcnow)
    field_provenance: dict[str, Provenance] = Field(default_factory=dict)
    relationship_basis: str = "UNKNOWN"
    current_company_confidence: str = "UNKNOWN"
    contactability_basis: str = ""
    outreach_allowed: bool = False
    whatsapp_allowed: bool = False
    do_not_contact: bool = False
    retention_expires_at: AwareDatetime | None = None
    suppression_token: str = ""
    hmac_key_version: str = "1"
    last_contacted_at: AwareDatetime | None = None
    follow_up_count: int = 0
    reply_state: str = "NONE"
    notes: str = ""


class JobContact(Model):
    job_uid: str
    contact_uid: str
    candidate_type: str
    rank: int = 1
    confidence: str = "OBSERVED"
    why_this_contact: str
    recommended_channel: str = ""
    agreed: bool = False


class OutreachDraft(Model):
    draft_id: str
    job_uid: str
    contact_uid: str = ""
    channel: str
    purpose: str
    subject: str = ""
    body: str
    evidence_ids: list[str] = Field(default_factory=list)
    word_count: int = 0
    character_count: int = 0
    generated_at: AwareDatetime = Field(default_factory=utcnow)
    outreach_stage: str = "DRAFTED"
    sent_at: AwareDatetime | None = None
    follow_up_due: AwareDatetime | None = None


class ApplicationEvent(Model):
    event_uid: str
    application_uid: str
    job_uid: str
    event_type: str = "STAGE_CHANGED"
    from_stage: str
    to_stage: str
    event_at: AwareDatetime = Field(default_factory=utcnow)
    actor: str = "user"
    source: str = "Action_Updates"
    reason: str = ""
    details: str = ""
    run_uid: str = ""


class AlertDecision(Model):
    alert_id: str
    job_uid: str
    alert_type: str = "P0"
    material_version: str
    state: Literal["PENDING", "SENDING", "SENT", "AMBIGUOUS", "FAILED"] = "PENDING"
    bundle_id: str = ""
    recipient_token: str = ""
    hmac_key_version: str = "1"
    transport: str = ""
    provider_message_id: str | None = None
    message_id: str = ""
    created_at: AwareDatetime = Field(default_factory=utcnow)
    sent_at: AwareDatetime | None = None
    error: str = ""
    run_uid: str = ""


class RunSummary(Model):
    run_uid: str
    status: str = "COMMITTED"
    mode: str = "incremental"
    started_at: AwareDatetime = Field(default_factory=utcnow)
    completed_at: AwareDatetime | None = None
    observations: int = 0
    canonical_jobs: int = 0
    new_jobs: int = 0
    changed_jobs: int = 0
    new_drafts: int = 0
    alerts: int = 0
    source_counts: dict[str, int] = Field(default_factory=dict)
    priority_counts: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


__all__ = [
    "AlertDecision",
    "ApplicationEvent",
    "CandidateEvidence",
    "CandidateProfile",
    "Contact",
    "FetchResult",
    "JobContact",
    "JobIdentity",
    "JobScore",
    "Model",
    "NormalizedJob",
    "OutreachDraft",
    "Provenance",
    "RawJob",
    "RunSummary",
    "SourceDefinition",
    "utcnow",
]
