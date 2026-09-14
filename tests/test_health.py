from datetime import datetime, timedelta

import pytest
from test_core import NOW
from test_pipeline import book_for, raw, source

from career_radar.domain import SourceDefinition
from career_radar.orchestration.pipeline import (
    SourceBatch,
    YieldPolicy,
    _yield_anomaly,
    load_sources,
    run_scan,
)
from career_radar.orchestration.planner import displayed_health, finish_source, plan_sources


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({}, "UNKNOWN"),
        ({"enabled": False, "health_state": "AUTH_REQUIRED", "consecutive_failures": 5}, "PAUSED"),
        ({"health_state": "PAUSED", "consecutive_failures": 5}, "PAUSED"),
        ({"health_state": "AUTH_REQUIRED", "consecutive_failures": 5}, "AUTH_REQUIRED"),
        ({"consecutive_failures": 3, "backoff_until": NOW + timedelta(hours=2)}, "DOWN"),
        (
            {"last_success_at": NOW - timedelta(days=5), "backoff_until": NOW + timedelta(hours=2)},
            "DOWN",
        ),
        (
            {"last_success_at": NOW - timedelta(days=3), "backoff_until": NOW + timedelta(hours=2)},
            "STALE",
        ),
        ({"consecutive_failures": 2, "backoff_until": NOW + timedelta(hours=2)}, "BACKOFF"),
        ({"consecutive_failures": 1, "last_success_at": NOW}, "DEGRADED"),
        ({"last_success_at": NOW}, "HEALTHY"),
    ],
)
def test_health_precedence_collisions(updates: dict[str, object], expected: str) -> None:
    assert displayed_health(source().model_copy(update=updates), NOW) == expected


def test_health_transitions_policy_dimensions_and_backoff() -> None:
    original = source()
    assert displayed_health(original, NOW, anomaly=True) == "DEGRADED"
    assert displayed_health(original, NOW, auth_required=True) == "AUTH_REQUIRED"
    for outcome in ("POLICY_BLOCKED", "LOGIN_REQUIRED"):
        result = finish_source(original, NOW, outcome, complete=False)
        assert result.policy_state == outcome and result.health_state == "UNKNOWN"
        assert (
            result.metadata["setup_required"] == "NATIVE_ALERT_OR_MANUAL_ROUTE"
            and result.consecutive_failures == 0
        )
    backoff = finish_source(original, NOW, "BACKOFF", complete=False)
    assert backoff.health_state == "BACKOFF" and backoff.backoff_until == NOW + timedelta(
        seconds=60
    )
    assert backoff.consecutive_failures == 0
    explicit = finish_source(
        original.model_copy(update={"consecutive_failures": 3}),
        NOW,
        "BACKOFF",
        complete=False,
        backoff_until=NOW + timedelta(hours=2),
    )
    assert explicit.health_state == "DOWN" and explicit.backoff_until == NOW + timedelta(hours=2)
    assert plan_sources([explicit], NOW, mode="full") == ([], [])
    repaired = finish_source(explicit, NOW, "REPAIR", complete=False)
    assert repaired.health_state == "UNKNOWN" and repaired.backoff_until is None
    assert finish_source(original, NOW, "PAUSE", complete=False).health_state == "PAUSED"
    assert finish_source(original, NOW, "UNPAUSE", complete=False).health_state == "UNKNOWN"
    auth = finish_source(original, NOW, "AUTH_REQUIRED", complete=False)
    assert (
        auth.health_state == "AUTH_REQUIRED"
        and auth.policy_state == "APPROVED"
        and auth.consecutive_failures == 0
    )
    for outcome in ("DEFERRED_BUDGET", "BACKOFF_SKIPPED", "POLICY_SKIPPED", "AUTH_SKIPPED"):
        deferred = finish_source(original, NOW, outcome, complete=False)
        assert deferred.last_attempt_at is None and deferred.last_success_at is None
    partial = finish_source(original, NOW, "SUCCESS_COMPLETE", complete=False)
    assert partial.baseline_state == "IN_PROGRESS" and partial.last_success_at is None
    completed = finish_source(original, NOW, "SUCCESS_EMPTY", complete=True)
    assert (
        completed.health_state == "HEALTHY"
        and completed.baseline_completed_at == NOW
        and completed.next_due_at == NOW + timedelta(days=1)
    )
    assert (
        finish_source(completed, NOW, "PARTIAL_BUDGET", complete=False).baseline_state
        == "COMPLETED"
    )
    assert (
        finish_source(completed, NOW, "FAILED_TRANSIENT", complete=False).consecutive_failures == 1
    )
    assert (
        finish_source(
            completed,
            NOW,
            "FAILED_TRANSIENT",
            complete=False,
            backoff_until=NOW + timedelta(hours=1),
        ).health_state
        == "BACKOFF"
    )
    with pytest.raises(ValueError):
        finish_source(original, datetime(2026, 1, 1), "SUCCESS_COMPLETE", complete=True)
    with pytest.raises(ValueError):
        plan_sources([original], datetime(2026, 1, 1))


def test_batch_policy_stop_and_backoff_persist_visibly() -> None:
    book = book_for()
    approved = source()
    book.upsert(
        "Sources",
        "source_id",
        [approved.model_dump(mode="json") | {"model_json": approved.model_dump_json()}],
        "seed",
        actor="user",
    )
    run_scan(
        book,
        [SourceBatch(approved, [], outcome="POLICY_BLOCKED", complete=False)],
        now=NOW,
        run_uid="blocked",
    )
    assert book.tabs["Sources"][0]["policy_state"] == "POLICY_BLOCKED"
    visible = load_sources(book.tabs["Sources"])[0]
    assert visible.policy_state == "POLICY_BLOCKED" and visible.health_state == "UNKNOWN"
    backoff_source = source("backoff")
    run_scan(
        book,
        [
            SourceBatch(
                backoff_source,
                [],
                outcome="BACKOFF",
                complete=False,
                backoff_until=NOW + timedelta(minutes=3),
            )
        ],
        now=NOW,
        run_uid="backoff",
    )
    stored = next(
        item for item in load_sources(book.tabs["Sources"]) if item.source_id == "backoff"
    )
    assert stored.backoff_until == NOW + timedelta(minutes=3) and stored.health_state == "BACKOFF"
    assert plan_sources([stored], NOW)[0] == []


def test_imports_and_email_are_never_complete_job_inventories() -> None:
    book = book_for()
    manual = source().model_copy(
        update={"access_mode": "USER_IMPORT", "policy_state": "MANUAL_ONLY"}
    )
    run_scan(book, [SourceBatch(manual, [raw()])], now=NOW, run_uid="init")
    for number in range(4):
        result = run_scan(
            book,
            [SourceBatch(manual, [], outcome="SUCCESS_EMPTY")],
            now=NOW + timedelta(days=number + 1),
            run_uid=f"import-{number}",
        )
    assert result.jobs[0].missing_snapshots == 0 and result.jobs[0].active_state != "CLOSED"


def test_planner_bounds_due_fairness_and_partial_priority() -> None:
    rows = [
        source(str(i)).model_copy(update={"next_due_at": NOW - timedelta(days=i % 5)})
        for i in range(45)
    ]
    rows[-1].metadata = {"last_outcome": "PARTIAL_BUDGET"}
    selected, deferred = plan_sources(rows, NOW, mandatory=("0",))
    assert (
        len(selected) == 40
        and len(deferred) == 5
        and selected[0].source_id == "0"
        and selected[1].source_id == "44"
    )
    future = source("future").model_copy(update={"next_due_at": NOW + timedelta(days=1)})
    assert not plan_sources([future], NOW)[0]
    assert plan_sources([future], NOW, mode="full")[0] == [future]
    paused = SourceDefinition.model_validate(source().model_dump() | {"health_state": "PAUSED"})
    assert not plan_sources([paused], NOW)[0]


def test_mandatory_rapid_sources_cannot_be_starved_by_partial_resumes() -> None:
    partials = [
        source(f"partial-{index:02}").model_copy(
            update={
                "next_due_at": NOW - timedelta(hours=index + 1),
                "metadata": {"last_outcome": "PARTIAL_BUDGET"},
            }
        )
        for index in range(45)
    ]
    mandatory = [
        source(f"rapid-{index}").model_copy(update={"next_due_at": NOW + timedelta(days=1)})
        for index in range(2)
    ]
    selected, deferred = plan_sources(
        [*partials, *mandatory], NOW, mandatory=tuple(row.source_id for row in mandatory)
    )
    assert selected[:2] == mandatory
    assert len(selected) == 40 and len(deferred) == 7
    assert selected[2:] == list(reversed(partials))[:38]
    assert deferred == list(reversed(partials))[38:]


def test_parse_yield_drop_preserves_jobs_watermark_and_recovers() -> None:
    book = book_for()
    postings = [
        raw(provider_job_id=str(index), url=f"https://example.com/jobs/{index}")
        for index in range(10)
    ]
    tracked = source()
    for hour in range(3):
        run_scan(
            book,
            [SourceBatch(tracked, postings)],
            now=NOW + timedelta(hours=hour),
            run_uid=f"normal-{hour}",
        )
        tracked = load_sources(book.tabs["Sources"])[0]
    success = tracked.last_success_at
    for hour in (3, 15, 27):
        result = run_scan(
            book,
            [SourceBatch(tracked, postings[:2])],
            now=NOW + timedelta(hours=hour),
            run_uid=f"drop-{hour}",
        )
        tracked = load_sources(book.tabs["Sources"])[0]
        assert tracked.health_state == "DEGRADED" and tracked.consecutive_failures == 0
        assert tracked.last_success_at == success
        assert all(
            item.missing_snapshots == 0 and item.active_state != "CLOSED" for item in result.jobs
        )
        assert result.summary.source_counts == {"QUARANTINED_SCHEMA": 1}
    assert len(book.tabs["Quarantine"]) == 3 and tracked.metadata["anomaly_count"] == 3
    assert tracked.metadata["yield_anomaly"]["drop_fraction"] == 0.8
    recovered = run_scan(
        book, [SourceBatch(tracked, postings)], now=NOW + timedelta(hours=28), run_uid="restored"
    )
    tracked = load_sources(book.tabs["Sources"])[0]
    assert tracked.health_state == "HEALTHY" and tracked.last_success_at == NOW + timedelta(
        hours=28
    )
    assert tracked.metadata["anomaly_count"] == 0 and "yield_anomaly" not in tracked.metadata
    assert recovered.summary.new_jobs == 0


def test_yield_threshold_history_and_private_configuration() -> None:
    normal = {
        "source_id": "fixture",
        "attempt_outcome": "SUCCESS_COMPLETE",
        "snapshot_complete": True,
        "completed_at": NOW.isoformat(),
        "jobs": 10,
    }
    history = [dict(normal) for _ in range(3)]
    three = [raw(provider_job_id=str(index)) for index in range(3)]
    assert not _yield_anomaly(
        SourceBatch(source(), three), history, NOW, YieldPolicy()
    )  # Exactly70% is allowed.
    assert _yield_anomaly(SourceBatch(source(), three[:2]), history, NOW, YieldPolicy())
    rss_source = source().model_copy(update={"access_mode": "RSS_ATOM"})
    assert _yield_anomaly(SourceBatch(rss_source, three[:2]), history, NOW, YieldPolicy())
    for modified in (
        {"source_id": "other"},
        {"attempt_outcome": "NO_CHANGE"},
        {"snapshot_complete": False},
        {"completed_at": "invalid"},
        {"completed_at": (NOW - timedelta(days=15)).isoformat()},
        {"completed_at": "2026-08-15T08:00:00"},
        {"jobs": -1},
        {"jobs": 2},
    ):
        assert not _yield_anomaly(
            SourceBatch(source(), []), [normal | modified for _ in range(3)], NOW, YieldPolicy()
        )
    assert not _yield_anomaly(
        SourceBatch(source(), [], complete=False), history, NOW, YieldPolicy()
    )
    assert not _yield_anomaly(SourceBatch(source(), []), history[:2], NOW, YieldPolicy())
    with pytest.raises(ValueError):
        YieldPolicy(drop_threshold=1)
    book = book_for()
    book.tabs["Config"].append(
        {
            "key": "data_quality",
            "value": '{"drop_threshold":0.8,"min_completed_samples":2,"min_baseline_jobs":5}',
        }
    )
    assert run_scan(book, [], now=NOW, run_uid="quality-config").summary.new_jobs == 0
