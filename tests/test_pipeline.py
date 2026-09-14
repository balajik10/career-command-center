import copy
import json
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_core import NOW, job, profile

from career_radar.demo import demo_book, oracle, run_demo
from career_radar.domain import CandidateProfile, RawJob, SourceDefinition
from career_radar.notifications import FakeTransport, Outbox
from career_radar.orchestration.pipeline import (
    SourceBatch,
    _change_event,
    commit_rows,
    json_value,
    load_contacts,
    load_jobs,
    load_profile,
    load_sources,
    row_for,
    run_scan,
)
from career_radar.scoring import ScoringConfig
from career_radar.sheets import SCHEMA, FakeWorkbook
from career_radar.sheets.workbook import SheetError

KEY = b"synthetic-test-key-32-bytes-only!!"


def source(source_id: str = "fixture", provider: str = "greenhouse") -> SourceDefinition:
    return SourceDefinition(
        source_id=source_id,
        company="Example Labs",
        provider=provider,
        tenant="example",
        url="https://example.com/jobs",
        access_mode="OFFICIAL_API",
        policy_state="APPROVED",
        cost_class="free",
        enabled=True,
    )


def raw(**kwargs: object) -> RawJob:
    data = {
        "source_id": "fixture",
        "provider": "greenhouse",
        "tenant": "example",
        "provider_job_id": "1",
        "title": "Backend Engineer",
        "company": "Example Labs",
        "location": "Bengaluru, India",
        "description": "Develop Java and Redis APIs. Required 0-2 years software experience.",
        "url": "https://example.com/jobs/1",
        "apply_url": "https://example.com/jobs/1/apply",
        "posted_at_raw": (NOW - timedelta(hours=2)).isoformat(),
        "fetched_at": NOW,
        "official_link_state": "VERIFIED_OFFICIAL",
    }
    data.update(kwargs)
    return RawJob.model_validate(data)


def book_for(p: CandidateProfile | None = None) -> FakeWorkbook:
    book = FakeWorkbook()
    book.bootstrap()
    book.upsert(
        "Profile",
        "evidence_id",
        [{"evidence_id": "profile", "value": (p or profile()).model_dump_json()}],
        "bootstrap",
        actor="user",
    )
    return book


def test_fixed_offline_oracle_twice_and_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("Offline oracle attempted networking")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    book = demo_book()
    first = run_demo(book, dry_run=False, run_uid="first")
    expected = oracle()["expected"]
    assert first.summary.observations == expected["observations"] == 12
    assert first.summary.canonical_jobs == expected["canonical_jobs"] == 9
    assert (
        first.summary.priority_counts
        == expected["priority_counts"]
        == {"P0": 2, "P1": 2, "P2": 3, "P3": 1, "EXCLUDED": 1}
    )
    assert first.summary.alerts == expected["first_alerts"] == 2
    assert len(first.drafts) == 4 and len(first.briefs) == 4
    assert "IST" in first.email_preview and len(book.tabs["Action_Queue"]) == 7
    assert all(row["state"] == "PENDING" for row in book.tabs["_Alerts"])
    second = run_demo(book, dry_run=False, run_uid="second")
    assert second.summary.new_jobs == expected["second_new_jobs"] == 0
    assert second.summary.new_drafts == expected["second_new_drafts"] == 0
    assert second.summary.alerts == expected["second_alerts"] == 0
    assert len(book.tabs["Jobs_Master"]) == 9 and len(book.tabs["_Job_Sources"]) == 12
    assert len(book.tabs["_Alerts"]) == 2
    assert {
        row["status"] for row in book.tabs["Run_Log"] if row["run_uid"] in {"first", "second"}
    } == {"COMMITTED"}


def test_dry_run_zero_mutation_and_model_roundtrip(tmp_path: Path) -> None:
    book = demo_book()
    before = copy.deepcopy(book.tabs)
    writes = book.write_requests
    result = run_demo(book, dry_run=True)
    assert result.summary.alerts == 2 and book.tabs == before and book.write_requests == writes
    assert result.workbook is not book
    path = tmp_path / "fixture-workbook.json"
    result.workbook.export(tmp_path)
    assert isinstance(result.workbook, FakeWorkbook)
    result.workbook.save(path)
    recovered = FakeWorkbook.load(path)
    assert len(load_jobs(recovered.tabs["Jobs_Master"])) == 9
    assert len(run_demo(recovered, dry_run=True).jobs) == 9


def test_baseline_unknown_and_post_baseline_new() -> None:
    book = book_for()
    first = run_scan(
        book, [SourceBatch(source(), [raw(posted_at_raw=None)])], now=NOW, run_uid="baseline"
    )
    assert (
        first.scores[0].action_priority == "P2"
        and first.jobs[0].freshness_band == "BASELINE_DATE_UNKNOWN"
        and first.summary.alerts == 0
    )
    saved_source = load_sources(book.tabs["Sources"])[0]
    assert saved_source.baseline_state == "COMPLETED"
    second = run_scan(
        book,
        [
            SourceBatch(
                saved_source,
                [raw(provider_job_id="2", url="https://example.com/jobs/2", posted_at_raw=None)],
            )
        ],
        now=NOW + timedelta(hours=1),
        run_uid="new",
    )
    new_job = next(item for item in second.jobs if item.job_uid != first.jobs[0].job_uid)
    assert not new_job.baseline and new_job.freshness_band == "OBSERVED_NEW_DATE_UNKNOWN"
    assert (
        next(score for score in second.scores if score.job_uid == new_job.job_uid).action_priority
        == "P1"
    )


def test_source_failure_isolation_partial_and_closure_reopen() -> None:
    book = book_for()
    initial = run_scan(book, [SourceBatch(source(), [raw()])], now=NOW, run_uid="first")
    item = initial.jobs[0]
    healthy = source("healthy", "lever")
    bad = SourceBatch(source(), [], "PARTIAL_BUDGET", False, error="DEADLINE_REACHED")
    other = SourceBatch(
        healthy,
        [
            raw(
                source_id="healthy",
                provider="lever",
                provider_job_id="other",
                company="Other Company",
                url="https://example.com/other",
            )
        ],
    )
    partial = run_scan(book, [bad, other], now=NOW + timedelta(hours=12), run_uid="partial")
    assert len(partial.jobs) == 2 and partial.summary.errors == ["fixture:DEADLINE_REACHED"]
    assert next(job for job in partial.jobs if job.job_uid == item.job_uid).missing_snapshots == 0
    assert bad.outcome == "PARTIAL_BUDGET" and not bad.complete
    for offset in range(1, 4):
        result = run_scan(
            book,
            [SourceBatch(source(), [], "SUCCESS_EMPTY")],
            now=NOW + timedelta(days=offset),
            run_uid=f"miss-{offset}",
        )
        stored = next(job for job in result.jobs if job.job_uid == item.job_uid)
        assert stored.missing_snapshots == offset
    assert stored.active_state == "CLOSED"
    reopened = run_scan(
        book, [SourceBatch(source(), [raw()])], now=NOW + timedelta(days=4), run_uid="reopened"
    )
    stored = next(job for job in reopened.jobs if job.job_uid == item.job_uid)
    assert stored.active_state == "REOPENED" and stored.first_seen_at == item.first_seen_at


def test_multiple_sources_must_both_confirm_missing() -> None:
    book = book_for()
    primary, secondary = source(), source("second", "lever")
    run_scan(
        book,
        [
            SourceBatch(primary, [raw()]),
            SourceBatch(secondary, [raw(source_id="second", provider="lever")]),
        ],
        now=NOW,
        run_uid="init",
    )
    for number in range(3):
        result = run_scan(
            book,
            [SourceBatch(primary, [], "SUCCESS_EMPTY")],
            now=NOW + timedelta(days=number + 1),
            run_uid=f"one-{number}",
        )
    assert result.jobs[0].active_state != "CLOSED" and result.jobs[0].missing_snapshots == 0
    same_day = run_scan(
        book,
        [SourceBatch(secondary, [], "SUCCESS_EMPTY")],
        now=NOW + timedelta(days=4),
        run_uid="second-first",
    )
    repeated = run_scan(
        book,
        [SourceBatch(secondary, [], "SUCCESS_EMPTY")],
        now=NOW + timedelta(days=4, hours=2),
        run_uid="second-fast",
    )
    assert same_day.jobs[0].missing_snapshots == repeated.jobs[0].missing_snapshots == 1
    for number in range(2):
        result = run_scan(
            book,
            [SourceBatch(secondary, [], "SUCCESS_EMPTY")],
            now=NOW + timedelta(days=5 + number),
            run_uid=f"both-{number}",
        )
    assert result.jobs[0].active_state == "CLOSED"


def test_schema_invalid_record_quarantined_without_aborting_healthy_source() -> None:
    book = book_for()
    bad = raw(url="javascript:alert(1)")
    mismatched = raw(source_id="different", provider_job_id="mismatch")
    batches = [
        SourceBatch(source(), [bad, mismatched]),
        SourceBatch(
            source("healthy"),
            [raw(source_id="healthy", provider_job_id="healthy", company="Different Company")],
        ),
    ]
    result = run_scan(book, batches, now=NOW, run_uid="mixed")
    assert result.summary.observations == 3 and result.summary.new_jobs == 1
    assert (
        len(book.tabs["Quarantine"]) == 2
        and result.summary.source_counts["QUARANTINED_SCHEMA"] == 1
    )
    assert batches[0].complete and batches[0].outcome == "SUCCESS_COMPLETE"
    assert load_sources(book.tabs["Sources"])[0].baseline_state != "COMPLETED"


def test_material_updates_events_drafts_and_alert_idempotency() -> None:
    book = book_for()
    initial = run_scan(book, [SourceBatch(source(), [raw()])], now=NOW, run_uid="first")
    changed = raw(
        description="Develop Java and Redis APIs. Required 1+ years of software experience.",
        deadline_raw=(NOW + timedelta(hours=48)).isoformat(),
    )
    second = run_scan(
        book, [SourceBatch(source(), [changed])], now=NOW + timedelta(minutes=1), run_uid="changed"
    )
    assert second.summary.changed_jobs == 1 and second.summary.new_drafts == 1
    assert second.summary.alerts <= 1
    assert (
        len([event for event in book.tabs["Job_History"] if event["event_type"] == "CHANGED"]) == 1
    )
    assert second.jobs[0].job_uid == initial.jobs[0].job_uid
    repeated = run_scan(
        book, [SourceBatch(source(), [changed])], now=NOW + timedelta(minutes=2), run_uid="repeat"
    )
    assert (
        repeated.summary.changed_jobs == 0
        and repeated.summary.new_drafts == 0
        and repeated.summary.alerts == 0
    )


def test_human_state_and_interleaved_edit_survive() -> None:
    book = book_for()
    initial = run_scan(book, [SourceBatch(source(), [raw()])], now=NOW, run_uid="first")
    job_uid = initial.jobs[0].job_uid
    book.upsert(
        "Applications",
        "application_uid",
        [
            {
                "application_uid": "application-1",
                "job_uid": job_uid,
                "application_stage": "APPLIED",
                "notes": "Owner notes",
            }
        ],
        "user-update",
        actor="user",
    )

    def interleave(current: FakeWorkbook) -> None:
        current.tabs["Applications"][0]["notes"] = "Changed while scan was running"
        current.tabs["Jobs_Master"][0]["notes"] = "Do not overwrite"

    book.before_write = interleave
    result = run_scan(
        book, [SourceBatch(source(), [raw()])], now=NOW + timedelta(hours=1), run_uid="after-apply"
    )
    assert result.scores[0].action_priority == "PIPELINE" and result.summary.alerts == 0
    assert book.tabs["Applications"][0]["notes"] == "Changed while scan was running"
    assert book.tabs["Jobs_Master"][0]["notes"] == "Do not overwrite"
    assert book.tabs["Jobs_Master"][0]["application_stage"] == "APPLIED"
    for stage in ("NOT_REVIEWED", "REVIEWING", "READY_TO_APPLY"):
        book.tabs["Applications"][0]["application_stage"] = stage
        result = run_scan(book, [], now=NOW, run_uid=stage)
        assert result.scores[0].action_priority == "P0"


def test_manual_reason_gated_override_and_sheet_scoring() -> None:
    book = book_for()
    run_scan(book, [SourceBatch(source(), [raw()])], now=NOW, run_uid="first")
    book.tabs["Jobs_Master"][0]["manual_override_flags"] = json.dumps({"priority": 30})
    assert run_scan(book, [], now=NOW, run_uid="no-reason").scores[0].action_priority == "P0"
    book.tabs["Jobs_Master"][0]["manual_override_flags"] = json.dumps(
        {"priority": 30, "reason": "Owner prioritizes other roles"}
    )
    assert run_scan(book, [], now=NOW, run_uid="override").scores[0].action_priority == "P3"
    book.tabs["Jobs_Master"][0]["manual_override_flags"] = ""
    configuration = asdict(ScoringConfig(p0_priority=100))
    book.tabs["Config"].append({"key": "scoring", "value": json.dumps(configuration)})
    assert run_scan(book, [], now=NOW, run_uid="custom-config").scores[0].action_priority == "P1"


def test_read_budget_and_interrupted_chunk_repair() -> None:
    book = book_for()
    before = book.read_requests
    run_scan(book, [SourceBatch(source(), [raw()])], now=NOW, run_uid="scan")
    assert book.read_requests - before <= 4
    many = [
        {
            "job_uid": f"synthetic-{index}",
            **{
                field: "value"
                for field in SCHEMA["Jobs_Master"].columns
                if field not in {"job_uid", "notes", "do_not_merge", "manual_override_flags"}
            },
        }
        for index in range(100)
    ]
    book.fail_on_write = book.write_requests + 2
    with pytest.raises(SheetError, match="INJECTED_WRITE_FAILURE"):
        commit_rows(book, {"Jobs_Master": many}, "interrupted")
    count = len(book.tabs["Jobs_Master"])
    assert 1 < count < 101
    book.fail_on_write = None
    commit_rows(book, {"Jobs_Master": many}, "interrupted")
    assert len(book.tabs["Jobs_Master"]) == 101
    assert any(row["key"].startswith("chunk:interrupted:") for row in book.tabs["_System_State"])


def test_run_replay_and_notification_send() -> None:
    book = book_for()
    transport = FakeTransport()
    outbox = Outbox(book, "candidate@example.com", KEY, transport)
    batches = [SourceBatch(source(), [raw()])]
    result = run_scan(
        book, batches, now=NOW, run_uid="same-run", send_alerts=True, outbox=outbox, hmac_key=KEY
    )
    assert result.summary.alerts == 1 and len(transport.messages) == 1
    assert book.tabs["_Alerts"][0]["state"] == "SENT"
    rerun = run_scan(
        book, batches, now=NOW, run_uid="same-run", send_alerts=True, outbox=outbox, hmac_key=KEY
    )
    assert rerun.summary.alerts == 0 and len(transport.messages) == 1


def test_pending_outbox_recovery_and_commit_before_transport() -> None:
    book = book_for()
    batches = [SourceBatch(source(), [raw()])]
    run_scan(book, batches, now=NOW, run_uid="queued-only")
    assert book.tabs["_Alerts"][0]["state"] == "PENDING"
    transport = FakeTransport()
    outbox = Outbox(book, "candidate@example.com", KEY, transport)
    # PENDING means no transport attempt occurred and may safely be attempted.
    recovered = run_scan(
        book,
        batches,
        now=NOW + timedelta(minutes=1),
        run_uid="recovery",
        send_alerts=True,
        outbox=outbox,
        hmac_key=KEY,
    )
    assert recovered.summary.alerts == 0 and len(transport.messages) == 1
    assert book.tabs["_Alerts"][0]["state"] == "SENT"
    blocked = book_for()
    blocked_transport = FakeTransport()
    blocked_outbox = Outbox(blocked, "candidate@example.com", KEY, blocked_transport)
    original_finalize = blocked.finalize_run

    def reject_commit(run_uid: str, summary: object = None) -> None:
        raise SheetError("POST_WRITE_VALIDATION_FAILED")

    blocked.finalize_run = reject_commit
    with pytest.raises(SheetError, match="POST_WRITE_VALIDATION_FAILED"):
        run_scan(
            blocked,
            batches,
            now=NOW,
            run_uid="not-committed",
            send_alerts=True,
            outbox=blocked_outbox,
            hmac_key=KEY,
        )
    assert blocked_transport.messages == [] and blocked.tabs["_Alerts"] == []
    blocked.finalize_run = original_finalize


def test_many_alerts_bundle_transport_and_draft_validation_isolation() -> None:
    book = book_for()
    transport = FakeTransport()
    outbox = Outbox(book, "candidate@example.com", KEY, transport)
    observations = [
        raw(
            provider_job_id=str(index),
            company=f"Company {index}",
            url=f"https://example.com/{index}",
        )
        for index in range(6)
    ]
    result = run_scan(
        book,
        [SourceBatch(source(), observations)],
        now=NOW,
        run_uid="six",
        send_alerts=True,
        outbox=outbox,
        hmac_key=KEY,
    )
    assert result.summary.alerts == 6 and len(transport.messages) == 5
    assert "2 opportunities" in str(transport.messages[-1]["Subject"])
    blocked = book_for()
    result = run_scan(
        blocked,
        [SourceBatch(source(), [raw(title="Backend Engineer " + "Unusual " * 230)])],
        now=NOW,
        run_uid="long-title",
    )
    assert result.summary.new_jobs == 1 and result.summary.new_drafts == 0
    assert blocked.tabs["Quarantine"][0]["reason"] == "DRAFT_VALIDATION_FAILED"


@pytest.mark.parametrize(
    "options",
    [
        {"dry_run": True, "send_alerts": True},
        {"now": datetime(2026, 1, 1)},
        {"run_uid": ""},
        {"mode": "invalid"},
    ],
)
def test_scan_invalid_runtime_inputs(options: dict[str, object]) -> None:
    values: dict[str, object] = {"now": NOW, "run_uid": "invalid", **options}
    with pytest.raises(ValueError):
        run_scan(book_for(), [], **values)


def test_counter_budgets_and_missing_profile_fail_closed() -> None:
    for batch in (
        SourceBatch(source(), [], requests=81),
        SourceBatch(source(), [], requests=-1),
        SourceBatch(source(), [], byte_count=-1),
    ):
        with pytest.raises(ValueError):
            run_scan(book_for(), [batch], now=NOW, run_uid="budget")
    with pytest.raises(ValueError, match="PRIVATE_SHEET_PROFILE_REQUIRED"):
        load_profile([])
    assert json_value({"value": 1}) == {"value": 1}
    assert row_for("Profile", {"evidence_id": "1", "unexpected": 2}) == {"evidence_id": "1"}


def test_loader_overrides_current_manual_cells() -> None:
    original = source()
    rows = [
        {
            "source_id": "fixture",
            "model_json": original.model_dump_json(),
            "enabled": False,
            "cadence_hours": 4,
            "allowed_hosts": '["example.com"]',
            "metadata": '{"fixture":true}',
            "notes": "User notes",
        }
    ]
    parsed = load_sources(rows)[0]
    assert not parsed.enabled and parsed.cadence_hours == 4 and parsed.metadata == {"fixture": True}
    assert parsed.allowed_hosts == ["example.com"]
    direct = load_sources(
        [
            {
                "source_id": "manual",
                "provider": "manual",
                "company": "Example",
                "url": "https://example.com",
            }
        ]
    )[0]
    assert direct.source_id == "manual"
    contacts = load_contacts(
        [
            {
                "contact_uid": "c",
                "contact_origin": "USER_ENTERED",
                "field_provenance": "{}",
                "outreach_allowed": "TRUE",
                "phone": "",
            }
        ]
    )
    assert contacts[0].outreach_allowed and contacts[0].phone is None
    assert load_jobs([{"model_json": job().model_dump_json(), "do_not_merge": "TRUE"}])[
        0
    ].do_not_merge
    assert _change_event(None, job()) == ""
    assert (
        _change_event(job(), job().model_copy(update={"apply_url": "https://example.com/new"}))
        == "APPLY_URL_REPLACED"
    )
    assert _change_event(job(), job().model_copy(update={"location": "Pune"})) == "LOCATION_CHANGED"
    assert _change_event(job(), job().model_copy(update={"min_years": 1})) == "ELIGIBILITY_CHANGED"
