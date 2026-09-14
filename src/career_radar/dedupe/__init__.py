"""Immutable canonical identities, conservative merges, and complete-snapshot lifecycle."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from rapidfuzz.fuzz import ratio

from career_radar.domain import NormalizedJob
from career_radar.normalize import normalized_text


@dataclass(frozen=True)
class DedupeConfig:
    title_threshold: float = 94
    description_threshold: float = 92
    gray_title_threshold: float = 88
    gray_description_threshold: float = 80
    max_days: int = 45


@dataclass
class DedupeResult:
    jobs: list[NormalizedJob]
    job: NormalizedJob
    new: bool
    changed: bool
    audit: list[dict[str, Any]]


def comparable_conflict(left: NormalizedJob, right: NormalizedJob) -> bool:
    for a in left.identities:
        for b in right.identities:
            if (
                a.provider_job_id
                and b.provider_job_id
                and (a.provider, a.tenant) == (b.provider, b.tenant)
                and a.provider_job_id != b.provider_job_id
            ):
                return True
            if (
                left.normalized_company == right.normalized_company
                and a.requisition_namespace
                and a.requisition_namespace == b.requisition_namespace
                and a.requisition_id
                and b.requisition_id
                and a.requisition_id != b.requisition_id
            ):
                return True
    return False


def match_reason(
    left: NormalizedJob, right: NormalizedJob, config: DedupeConfig | None = None
) -> str:
    cfg = config or DedupeConfig()
    if left.do_not_merge or right.do_not_merge or comparable_conflict(left, right):
        return ""
    for a in left.identities:
        for b in right.identities:
            if a.provider_job_id and (a.provider, a.tenant, a.provider_job_id) == (
                b.provider,
                b.tenant,
                b.provider_job_id,
            ):
                return "EXACT_PROVIDER_ID"
    if left.canonical_url and left.canonical_url == right.canonical_url:
        return "EXACT_CANONICAL_URL"
    if not left.normalized_company or left.normalized_company != right.normalized_company:
        return ""
    for a in left.identities:
        for b in right.identities:
            if (
                a.requisition_namespace
                and a.requisition_id
                and (a.requisition_namespace, a.requisition_id)
                == (b.requisition_namespace, b.requisition_id)
            ):
                return "EXACT_REQUISITION"
    if (
        not left.location
        or not right.location
        or normalized_text(left.location) != normalized_text(right.location)
        or left.work_mode != right.work_mode
    ):
        return ""
    if (
        not left.description
        or not right.description
        or not left.normalized_title
        or not right.normalized_title
    ):
        return ""
    date_a = left.posted_at_source or left.first_seen_at
    date_b = right.posted_at_source or right.first_seen_at
    if abs((date_a - date_b).total_seconds()) > cfg.max_days * 86400:
        return ""
    title_similarity = ratio(left.normalized_title, right.normalized_title)
    left_tokens = set(normalized_text(left.description).split())
    right_tokens = set(normalized_text(right.description).split())
    description_similarity = (
        100 * len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
    )
    if (
        title_similarity >= cfg.title_threshold
        and description_similarity >= cfg.description_threshold
    ):
        return (
            "EXACT_FINGERPRINT"
            if left.description_hash == right.description_hash
            and left.normalized_title == right.normalized_title
            else "FUZZY_HIGH_CONFIDENCE"
        )
    if (
        title_similarity >= cfg.gray_title_threshold
        and description_similarity >= cfg.gray_description_threshold
    ):
        return "GRAY_ZONE"
    return ""


MATERIAL_FIELDS = (
    "title",
    "location",
    "experience_text",
    "description_hash",
    "deadline",
    "apply_url",
)


def merge_jobs(
    existing: NormalizedJob, incoming: NormalizedJob, reason: str
) -> tuple[NormalizedJob, dict[str, Any]]:
    prefer_incoming = (
        incoming.official_link_state == "VERIFIED_OFFICIAL"
        or existing.official_link_state != "VERIFIED_OFFICIAL"
    )
    result = (incoming if prefer_incoming else existing).model_copy(deep=True)
    result.job_uid = existing.job_uid
    result.dedupe_group_id = existing.dedupe_group_id or existing.job_uid
    result.first_seen_at = min(existing.first_seen_at, incoming.first_seen_at)
    result.last_seen_at = max(existing.last_seen_at, incoming.last_seen_at)
    result.baseline = existing.baseline
    result.identities = list(existing.identities)
    for identity in incoming.identities:
        if identity not in result.identities:
            result.identities.append(identity)
    result.provenance = list(existing.provenance)
    for evidence in incoming.provenance:
        if not any(
            (entry.source_id, entry.url) == (evidence.source_id, evidence.url)
            for entry in result.provenance
        ):
            result.provenance.append(evidence)
    if existing.posted_at_source and (
        result.posted_at_source is None or existing.posted_at_source < result.posted_at_source
    ):
        result.posted_at_source = existing.posted_at_source
        result.posted_at_confidence = existing.posted_at_confidence
        elapsed = (result.last_seen_at - existing.last_seen_at).total_seconds() / 3600
        result.age_hours_min = (
            None if existing.age_hours_min is None else existing.age_hours_min + elapsed
        )
        result.age_hours_max = (
            None if existing.age_hours_max is None else existing.age_hours_max + elapsed
        )
    result.missing_snapshots = 0
    result.last_missing_at = None
    changed = {
        field: {"before": str(getattr(existing, field)), "after": str(getattr(result, field))}
        for field in MATERIAL_FIELDS
        if getattr(existing, field) != getattr(result, field)
    }
    result.material_change_sequence = existing.material_change_sequence + int(bool(changed))
    result.last_changed_at = incoming.last_seen_at if changed else existing.last_changed_at
    result.active_state = (
        "REOPENED"
        if existing.active_state in {"CLOSED", "POSSIBLY_CLOSED"}
        else "CHANGED"
        if changed
        else "ACTIVE"
    )
    return result, {
        "job_uid": result.job_uid,
        "event": "REOPENED"
        if result.active_state == "REOPENED"
        else "CHANGED"
        if changed
        else "MERGED",
        "reason": reason,
        "diff": changed,
    }


def upsert_job(
    existing: list[NormalizedJob], incoming: NormalizedJob, config: DedupeConfig | None = None
) -> DedupeResult:
    jobs = list(existing)
    gray = False
    for index, candidate in enumerate(jobs):
        reason = (
            "PERSISTED_UID"
            if candidate.job_uid == incoming.job_uid
            else match_reason(candidate, incoming, config)
        )
        if reason == "GRAY_ZONE":
            gray = True
            continue
        if reason:
            merged, audit = merge_jobs(candidate, incoming, reason)
            jobs[index] = merged
            return DedupeResult(
                jobs,
                merged,
                False,
                bool(audit["diff"]),
                [audit]
                if audit["diff"]
                or audit["event"] == "REOPENED"
                or len(merged.identities) != len(candidate.identities)
                else [],
            )
    result = incoming.model_copy(deep=True)
    if gray:
        result.quarantine_reason = "UNRESOLVED_DUPLICATE_GRAY_ZONE"
    jobs.append(result)
    return DedupeResult(
        jobs,
        result,
        True,
        False,
        [
            {
                "job_uid": result.job_uid,
                "event": "NEW",
                "reason": "GRAY_ZONE" if gray else "DISTINCT_IDENTITY",
                "diff": {},
            }
        ],
    )


def mark_missing(
    job: NormalizedJob,
    now: datetime,
    *,
    snapshot_complete: bool,
    seen: bool = False,
    explicit_closed: bool = False,
) -> NormalizedJob:
    if now.tzinfo is None:
        raise ValueError("run datetime must be timezone-aware")
    result = job.model_copy(deep=True)
    if seen:
        result.active_state = (
            "REOPENED" if job.active_state in {"CLOSED", "POSSIBLY_CLOSED"} else "ACTIVE"
        )
        result.missing_snapshots = 0
        result.last_missing_at = None
        result.last_seen_at = now
    elif explicit_closed:
        result.active_state = "CLOSED"
    elif snapshot_complete and (
        job.last_missing_at is None or now - job.last_missing_at >= timedelta(hours=12)
    ):
        result.missing_snapshots += 1
        result.last_missing_at = now
        if result.missing_snapshots >= 3:
            result.active_state = "CLOSED"
        elif result.missing_snapshots >= 2:
            result.active_state = "POSSIBLY_CLOSED"
    return result
