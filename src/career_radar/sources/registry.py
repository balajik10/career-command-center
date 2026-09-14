"""Independent access-policy decisions and operational health precedence."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml

from career_radar.domain import SourceDefinition
from career_radar.security import SecurityError, canonical_url
from career_radar.sources.google_careers import valid_search_url

RequestPurpose = Literal["COLLECTION", "VALIDATION_PROBE"]


def check_policy(
    source: SourceDefinition,
    url: str,
    now: datetime,
    purpose: RequestPurpose = "COLLECTION",
    *,
    user_initiated: bool = False,
    documentation_urls: tuple[str, ...] = (),
    zero_cost_mode: bool = True,
) -> None:
    """Every network hop must pass this check. A probe cannot approve a source."""
    if now.tzinfo is None:
        raise SecurityError("NAIVE_TIME")
    canonical_url(url)
    if purpose == "VALIDATION_PROBE":
        origin = urlsplit(source.url)
        robots = f"{origin.scheme}://{origin.netloc}/robots.txt"
        if not user_initiated or source.policy_state != "PENDING_REVIEW":
            raise SecurityError("PROBE_REQUIRES_USER_PENDING_SOURCE")
        if url not in (source.url, robots, *documentation_urls):
            raise SecurityError("PROBE_EXACT_URL_ONLY")
        return
    if purpose != "COLLECTION":
        raise SecurityError("UNKNOWN_REQUEST_PURPOSE")
    if source.provider == "google_careers" and not valid_search_url(url):
        raise SecurityError("GOOGLE_FIRST_PAGE_ONLY")
    if not source.enabled or source.policy_state != "APPROVED":
        raise SecurityError("SOURCE_NOT_APPROVED")
    if source.access_mode not in {"OFFICIAL_API", "RSS_ATOM", "PUBLIC_HTML_APPROVED"}:
        raise SecurityError("ACCESS_MODE_NOT_AUTOMATED")
    if source.coverage_state not in {"VERIFIED_ACTIVE", "VERIFIED_EMPTY"}:
        raise SecurityError("SOURCE_NOT_VERIFIED")
    if source.health_state in {"PAUSED", "AUTH_REQUIRED", "DOWN"}:
        raise SecurityError("SOURCE_HEALTH_BLOCKED")
    if source.backoff_until is not None and source.backoff_until > now:
        raise SecurityError("BACKOFF_ACTIVE")
    if zero_cost_mode and source.cost_class != "free":
        raise SecurityError("ZERO_COST_REJECTED")
    if (
        source.policy_reviewed_at is None
        or source.policy_reviewed_at > now
        or source.policy_review_due_at is None
        or source.policy_review_due_at <= now
        or not source.policy_evidence_url
        or not source.policy_evidence_hash
        or not source.human_reviewer
    ):
        raise SecurityError("POLICY_EVIDENCE_MISSING_OR_OVERDUE")
    if (
        source.access_mode in {"PUBLIC_HTML_APPROVED", "RSS_ATOM"}
        and source.robots_allowed is not True
    ):
        raise SecurityError("ROBOTS_NOT_APPROVED")
    allowed = {urlsplit(source.url).hostname, *source.allowed_hosts}
    if urlsplit(url).hostname not in allowed:
        raise SecurityError("UNAPPROVED_HOST")


def health_state(
    source: SourceDefinition,
    now: datetime,
    *,
    paused: bool = False,
    auth_required: bool = False,
    anomalous: bool = False,
) -> str:
    if paused or source.health_state == "PAUSED":
        return "PAUSED"
    if auth_required or source.health_state == "AUTH_REQUIRED":
        return "AUTH_REQUIRED"
    age = (now - source.last_success_at).total_seconds() / 3600 if source.last_success_at else 0
    if source.consecutive_failures >= 3 or age > source.cadence_hours * 4:
        return "DOWN"
    if age > source.cadence_hours * 2:
        return "STALE"
    if source.backoff_until and source.backoff_until > now:
        return "BACKOFF"
    if anomalous or source.consecutive_failures:
        return "DEGRADED"
    return "HEALTHY" if source.last_success_at else "UNKNOWN"


def record_outcome(
    source: SourceDefinition, outcome: str, now: datetime, retry_after: float | None = None
) -> SourceDefinition:
    """Return an updated source; partial snapshots cannot advance success or baselines."""
    update: dict[str, Any] = {}
    if outcome in {"SUCCESS_COMPLETE", "SUCCESS_EMPTY", "NO_CHANGE"}:
        update = {
            "last_success_at": now,
            "last_attempt_at": now,
            "consecutive_failures": 0,
            "health_state": "HEALTHY",
            "backoff_until": None,
            "baseline_state": "COMPLETED",
        }
        if source.baseline_completed_at is None:
            update["baseline_completed_at"] = now
    elif outcome == "BACKOFF":
        update = {
            "last_attempt_at": now,
            "backoff_until": now + timedelta(seconds=max(1, retry_after or 60)),
        }
    elif outcome == "AUTH_REQUIRED":
        update = {"health_state": "AUTH_REQUIRED", "last_attempt_at": now}
    elif outcome in {"FAILED_TRANSIENT", "FAILED_PERMANENT", "QUARANTINED_SCHEMA"}:
        update = {"consecutive_failures": source.consecutive_failures + 1, "last_attempt_at": now}
    elif outcome == "POLICY_BLOCKED":
        update = {"policy_state": "POLICY_BLOCKED", "last_attempt_at": now}
    elif outcome == "LOGIN_REQUIRED":
        update = {"policy_state": "LOGIN_REQUIRED", "last_attempt_at": now}
    elif outcome == "UNPAUSE":
        update = {
            "health_state": "UNKNOWN",
            "last_success_at": None,
            "consecutive_failures": 0,
            "backoff_until": None,
        }
    elif outcome == "PAUSE":
        update = {"health_state": "PAUSED"}
    result = SourceDefinition.model_validate(source.model_dump() | update)
    result = SourceDefinition.model_validate(
        result.model_dump() | {"health_state": health_state(result, now)}
    )
    return result


def load_catalogue(path: Path) -> list[SourceDefinition]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("companies"), list):
        raise ValueError("INVALID_COMPANY_CATALOGUE")
    rows = []
    for value in data["companies"]:
        row = dict(value)
        category = row.pop("category", "")
        row["metadata"] = {"category": category}
        rows.append(SourceDefinition.model_validate(row))
    if len({row.source_id for row in rows}) != len(rows):
        raise ValueError("DUPLICATE_SOURCE_ID")
    return rows
