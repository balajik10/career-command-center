"""Fair bounded source selection and independent operational-health transitions."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from career_radar.domain import SourceDefinition


@dataclass(frozen=True)
class RunBudget:
    seconds: int
    sources: int
    requests: int
    sheet_reads: int
    sheet_writes: int


BUDGETS = {
    "incremental": RunBudget(180, 40, 80, 20, 20),
    "full": RunBudget(480, 250, 500, 30, 30),
    "morning": RunBudget(120, 0, 0, 10, 10),
    "evening": RunBudget(120, 0, 0, 10, 10),
    "maintenance": RunBudget(360, 50, 100, 20, 20),
}

SCHEDULES = {
    "weekday_rapid": ("47 6,9,12,15,18,21 * * 1-5", "17 1,4,7,10,13,16 * * 1-5", "incremental", 4),
    "weekend_rapid": ("47 9,15,21 * * 0,6", "17 4,10,16 * * 0,6", "incremental", 4),
    "full": ("23 2 * * *", "53 20 * * *", "full", 10),
    "morning": ("11 8 * * *", "41 2 * * *", "morning", 3),
    "evening": ("11 19 * * *", "41 13 * * *", "evening", 3),
    "maintenance": ("43 3 * * 0", "13 22 * * 6", "maintenance", 8),
}


def route_schedule(cron: str) -> str:
    for ist, utc, mode, _ in SCHEDULES.values():
        if cron in {ist, utc}:
            return mode
    raise ValueError("UNKNOWN_SCHEDULE")


def plan_sources(
    sources: list[SourceDefinition],
    now: datetime,
    *,
    mode: str = "incremental",
    mandatory: tuple[str, ...] = (),
) -> tuple[list[SourceDefinition], list[SourceDefinition]]:
    budget = BUDGETS[mode]
    if now.tzinfo is None:
        raise ValueError("AWARE_RUN_TIME_REQUIRED")
    due = [
        s
        for s in sources
        if s.enabled
        and not (s.backoff_until and s.backoff_until > now)
        and s.health_state not in {"PAUSED", "AUTH_REQUIRED"}
        and (
            mode == "full"
            or s.source_id in mandatory
            or s.next_due_at is None
            or s.next_due_at <= now
        )
    ]
    due.sort(
        key=lambda s: (
            s.source_id not in mandatory,
            s.metadata.get("last_outcome") != "PARTIAL_BUDGET",
            s.next_due_at or datetime.min.replace(tzinfo=UTC),
            s.priority_tier,
            s.source_id,
        )
    )
    return due[: budget.sources], due[budget.sources :]


def displayed_health(
    source: SourceDefinition, now: datetime, *, auth_required: bool = False, anomaly: bool = False
) -> str:
    if not source.enabled or source.health_state == "PAUSED":
        return "PAUSED"
    if auth_required or source.health_state == "AUTH_REQUIRED":
        return "AUTH_REQUIRED"
    age = (now - source.last_success_at).total_seconds() / 3600 if source.last_success_at else 0
    if source.consecutive_failures >= 3 or age > 4 * source.cadence_hours:
        return "DOWN"
    if age > 2 * source.cadence_hours:
        return "STALE"
    if source.backoff_until and source.backoff_until > now:
        return "BACKOFF"
    if anomaly or source.consecutive_failures:
        return "DEGRADED"
    if source.last_success_at:
        return "HEALTHY"
    return "UNKNOWN"


def finish_source(
    source: SourceDefinition,
    now: datetime,
    outcome: str,
    *,
    complete: bool,
    backoff_until: datetime | None = None,
    anomaly: bool = False,
) -> SourceDefinition:
    if now.tzinfo is None:
        raise ValueError("AWARE_RUN_TIME_REQUIRED")
    result = source.model_copy(deep=True)
    result.metadata["last_outcome"] = outcome
    if outcome in {"POLICY_BLOCKED", "LOGIN_REQUIRED"}:
        result.policy_state = "POLICY_BLOCKED" if outcome == "POLICY_BLOCKED" else "LOGIN_REQUIRED"
        result.metadata["setup_required"] = "NATIVE_ALERT_OR_MANUAL_ROUTE"
        result.metadata["policy_stop"] = outcome
        result.last_attempt_at = now
        return result
    if outcome == "BACKOFF":
        result.last_attempt_at = now
        result.backoff_until = (
            backoff_until
            if backoff_until is not None and backoff_until > now
            else now + timedelta(seconds=60)
        )
        result.health_state = displayed_health(result, now)  # type: ignore[assignment]
        return result
    if outcome == "AUTH_REQUIRED":
        result.last_attempt_at = now
        result.health_state = "AUTH_REQUIRED"
        return result
    if outcome == "PAUSE":
        result.health_state = "PAUSED"
        return result
    if outcome in {"UNPAUSE", "REPAIR"}:
        result.health_state = "UNKNOWN"
        result.consecutive_failures = 0
        result.backoff_until = None
        result.last_success_at = None
        return result
    if outcome in {"DEFERRED_BUDGET", "BACKOFF_SKIPPED", "POLICY_SKIPPED", "AUTH_SKIPPED"}:
        result.health_state = displayed_health(result, now)  # type: ignore[assignment]
        return result
    result.last_attempt_at = now
    if anomaly:
        result.metadata["anomaly_count"] = int(source.metadata.get("anomaly_count", 0)) + 1
        result.health_state = displayed_health(result, now, anomaly=True)  # type: ignore[assignment]
        return result
    if (
        outcome == "PARTIAL_BUDGET"
        or not complete
        and outcome in {"SUCCESS_COMPLETE", "SUCCESS_EMPTY", "NO_CHANGE"}
    ):
        result.metadata["last_outcome"] = "PARTIAL_BUDGET"
        result.baseline_state = (
            "IN_PROGRESS" if source.baseline_state != "COMPLETED" else "COMPLETED"
        )
        return result
    if complete and outcome in {"SUCCESS_COMPLETE", "SUCCESS_EMPTY", "NO_CHANGE"}:
        result.last_success_at = now
        result.consecutive_failures = 0
        result.backoff_until = None
        result.metadata["anomaly_count"] = 0
        result.metadata.pop("yield_anomaly", None)
        result.health_state = "HEALTHY"
        result.baseline_state = "COMPLETED"
        result.baseline_completed_at = result.baseline_completed_at or now
        result.next_due_at = now + timedelta(hours=result.cadence_hours)
    else:
        result.consecutive_failures += 1
        if backoff_until:
            result.backoff_until = backoff_until
        state = displayed_health(result, now, auth_required=outcome == "AUTH_REQUIRED")
        result.health_state = state  # type: ignore[assignment]
    return result


def monthly_budget(year: int, month: int) -> dict[str, int]:
    days = calendar.monthrange(year, month)[1]
    weekday_count = sum(datetime(year, month, day).weekday() < 5 for day in range(1, days + 1))
    sundays = sum(datetime(year, month, day).weekday() == 6 for day in range(1, days + 1))
    rapid = (weekday_count * 6 + (days - weekday_count) * 3) * 4
    scheduled = rapid + days * 10 + days * 2 * 3 + sundays * 8
    return {
        "scheduled_minutes": scheduled,
        "manual_reserve_minutes": 200,
        "planned_minutes": scheduled + 200,
        "public_ci_allowance": 300,
    }


def maximum_monthly_budget() -> dict[str, int]:
    # Gregorian weekday/leap-year combinations repeat after 400 years.
    return max(
        (monthly_budget(y, m) for y in range(2000, 2400) for m in range(1, 13)),
        key=lambda row: row["scheduled_minutes"],
    )


def scheduler_ready(configured: Literal["github", "local", "disabled"], is_github: bool) -> bool:
    return configured == ("github" if is_github else "local")
