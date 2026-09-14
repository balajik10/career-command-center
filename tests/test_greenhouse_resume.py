"""A budget-limited board must make durable progress without false missing jobs."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from test_pipeline import book_for
from test_sources import FIXTURES, fake_wire, response, source

from career_radar.fetch import FetchBudget, collect, greenhouse_listing_version
from career_radar.orchestration.pipeline import (
    SourceBatch,
    commit_rows,
    hydrate_source_checkpoints,
    load_sources,
    run_scan,
    uid,
)
from career_radar.sheets.workbook import SheetError
from career_radar.sources import ParseError, parse


def board(count=3):
    template = json.loads((FIXTURES / "greenhouse_detail.json").read_text())
    return [
        template | {"id": str(i), "absolute_url": f"https://jobs.example.com/{i}"}
        for i in range(count)
    ]


def collect_board(row, records, *, limit=2, details=None):
    requests = []

    def wire(url, address, headers, timeout, max_bytes):
        requests.append((url, headers))
        if url == row.url:
            return response(json.dumps({"jobs": records}).encode())
        external_id = url.split("?", 1)[0].rsplit("/", 1)[1]
        record = next(value for value in (details or records) if value["id"] == external_id)
        return response(json.dumps(record).encode())

    result, jobs = collect(
        row,
        FetchBudget(max_requests=limit),
        wire=wire,
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    batch = SourceBatch(
        row,
        jobs,
        result.outcome,
        result.snapshot_complete,
        result.request_count,
        result.bytes_received,
        result.error_code,
        checkpoint=result.checkpoint,
        listed_ids=result.listed_ids,
        listing_complete=result.listing_complete,
    )
    return result, batch, requests


def checkpoint_rows(book):
    return [
        row for row in book.tabs["_System_State"] if row["key"].startswith("greenhouse_checkpoint:")
    ]


def test_partial_board_resumes_committed_details_and_cached_presence_ages_correctly():
    book = book_for()
    row = source(etag="older-board", last_modified="Mon, 14 Sep 2026 00:00:00 GMT")
    records = board()
    start = datetime.now(UTC) + timedelta(minutes=1)
    for index in range(3):
        hydrate_source_checkpoints(
            [row],
            book.tabs["_System_State"],
            jobs=book.tabs["Jobs_Master"],
            appearances=book.tabs["_Job_Sources"],
        )
        outcome, batch, requests = collect_board(row, records)
        assert [raw.provider_job_id for raw in batch.jobs] == [str(index)]
        assert outcome.listing_complete and outcome.listed_ids == ["0", "1", "2"]
        assert outcome.snapshot_complete is (index == 2)
        assert len(requests) == outcome.request_count == 2
        assert "If-None-Match" not in requests[0][1]
        assert "If-Modified-Since" not in requests[0][1]
        result = run_scan(
            book, [batch], now=start + timedelta(hours=index), run_uid=f"part-{index}"
        )
        assert len(checkpoint_rows(book)) == index + 1
        row = load_sources(book.tabs["Sources"])[0]
        assert "_greenhouse_seen_versions" not in row.metadata
        assert row.baseline_state == ("COMPLETED" if index == 2 else "IN_PROGRESS")
        assert len(result.jobs) == index + 1
        assert all(job.missing_snapshots == 0 for job in result.jobs)
    original_age = result.jobs[0].age_hours_max
    for index in range(3, 7):
        hydrate_source_checkpoints(
            [row],
            book.tabs["_System_State"],
            jobs=book.tabs["Jobs_Master"],
            appearances=book.tabs["_Job_Sources"],
        )
        outcome, batch, requests = collect_board(row, records, limit=1)
        assert outcome.outcome == "SUCCESS_COMPLETE" and not batch.jobs and len(requests) == 1
        result = run_scan(
            book, [batch], now=start + timedelta(hours=index), run_uid=f"cached-{index}"
        )
        row = load_sources(book.tabs["Sources"])[0]
        assert result.summary.new_jobs == result.summary.observations == 0
        assert all(job.missing_snapshots == 0 for job in result.jobs)
        assert all(job.last_seen_at == start + timedelta(hours=index) for job in result.jobs)
    assert result.jobs[0].age_hours_max == pytest.approx(original_age + 4)
    assert {attempt["jobs"] for attempt in book.tabs["_Source_Attempts"]} == {3}
    assert len(checkpoint_rows(book)) == 3


def test_changed_listing_revalidates_only_changed_job_even_after_title_leaves_scope():
    book = book_for()
    row = source()
    records = board(2)
    now = datetime.now(UTC) + timedelta(minutes=1)
    _, batch, _ = collect_board(row, records, limit=3)
    run_scan(book, [batch], now=now, run_uid="first")
    hydrate_source_checkpoints(
        [row],
        book.tabs["_System_State"],
        jobs=book.tabs["Jobs_Master"],
        appearances=book.tabs["_Job_Sources"],
    )
    records[1] |= {"title": "Marketing Specialist", "updated_at": "2026-09-14T02:00:00Z"}
    outcome, batch, requests = collect_board(row, records)
    assert outcome.snapshot_complete and [raw.provider_job_id for raw in batch.jobs] == ["1"]
    assert len(requests) == 2
    result = run_scan(book, [batch], now=now + timedelta(hours=1), run_uid="changed")
    assert result.summary.changed_jobs == 1 and all(
        job.missing_snapshots == 0 for job in result.jobs
    )


def test_removed_jobs_advance_missing_only_on_complete_inventory_and_reopen_from_presence():
    book = book_for()
    row = source()
    records = board(2)
    now = datetime.now(UTC) + timedelta(minutes=1)
    _, batch, _ = collect_board(row, records, limit=3)
    run_scan(book, [batch], now=now, run_uid="initial")
    hydrate_source_checkpoints(
        [row],
        book.tabs["_System_State"],
        jobs=book.tabs["Jobs_Master"],
        appearances=book.tabs["_Job_Sources"],
    )
    # An unseen third listing needs details: its absence of job 1 cannot close job 1 yet.
    pending = [records[0], board(3)[2]]
    _, batch, _ = collect_board(row, pending, limit=1)
    result = run_scan(book, [batch], now=now + timedelta(hours=1), run_uid="partial")
    assert all(job.missing_snapshots == 0 for job in result.jobs)
    for index in range(3):
        _, batch, _ = collect_board(row, records[:1], limit=1)
        result = run_scan(
            book, [batch], now=now + timedelta(hours=12 * (index + 1)), run_uid=f"missing-{index}"
        )
    by_id = {job.identities[0].provider_job_id: job for job in result.jobs}
    assert by_id["0"].missing_snapshots == 0 and by_id["1"].active_state == "CLOSED"
    _, batch, requests = collect_board(row, records, limit=1)
    assert len(requests) == 1 and not batch.jobs
    result = run_scan(book, [batch], now=now + timedelta(hours=40), run_uid="reappeared")
    by_id = {job.identities[0].provider_job_id: job for job in result.jobs}
    assert by_id["1"].active_state == "REOPENED" and by_id["1"].missing_snapshots == 0
    assert any(event["event_type"] == "REOPENED" for event in book.tabs["Job_History"])


def test_detail_failure_and_listing_race_preserve_successful_progress():
    records = board(2)
    row = source()
    outcome, jobs = collect(
        row,
        FetchBudget(),
        wire=fake_wire(
            [
                response(json.dumps({"jobs": records}).encode()),
                response(json.dumps(records[0]).encode()),
                response(status=403),
            ]
        ),
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    assert outcome.listing_complete and not outcome.snapshot_complete
    assert list(outcome.checkpoint) == ["0"] and len(jobs) == 1
    changed = [records[0], records[1] | {"updated_at": "2026-09-14T02:00:00Z"}]
    outcome, batch, _ = collect_board(row, records, limit=3, details=changed)
    assert outcome.error_code == "LISTING_CHANGED_DURING_DETAILS"
    assert outcome.listing_complete and not outcome.snapshot_complete
    assert list(outcome.checkpoint) == ["0"] and len(batch.jobs) == 2


def test_checkpoint_waits_for_canonical_commit_and_excludes_invalid_normalization():
    book = book_for()
    row = source()
    _, batch, _ = collect_board(row, board(1))
    now = datetime.now(UTC) + timedelta(minutes=1)
    with (
        patch.object(book, "finalize_run", side_effect=SheetError("INJECTED_FINALIZE_FAILURE")),
        pytest.raises(SheetError, match="INJECTED_FINALIZE_FAILURE"),
    ):
        run_scan(book, [batch], now=now, run_uid="crashed")
    assert not checkpoint_rows(book)
    hydrate_source_checkpoints(
        [row],
        book.tabs["_System_State"],
        jobs=book.tabs["Jobs_Master"],
        appearances=book.tabs["_Job_Sources"],
    )
    _, retried, requests = collect_board(row, board(1))
    assert len(requests) == 2
    run_scan(book, [retried], now=now, run_uid="recovered")
    assert len(book.tabs["Jobs_Master"]) == len(checkpoint_rows(book)) == 1
    book = book_for()
    with patch(
        "career_radar.orchestration.pipeline.normalize_job", side_effect=ValueError("INVALID")
    ):
        result = run_scan(book, [batch], now=now, run_uid="invalid")
    assert not result.jobs and not checkpoint_rows(book)


def test_cached_inventory_does_not_trigger_yield_drop():
    book = book_for()
    row = source()
    records = board(12)
    now = datetime.now(UTC) + timedelta(minutes=1)
    _, batch, _ = collect_board(row, records, limit=13)
    run_scan(book, [batch], now=now, run_uid="initial")
    hydrate_source_checkpoints(
        [row],
        book.tabs["_System_State"],
        jobs=book.tabs["Jobs_Master"],
        appearances=book.tabs["_Job_Sources"],
    )
    for index in range(1, 5):
        outcome, batch, _ = collect_board(row, records, limit=1)
        result = run_scan(
            book, [batch], now=now + timedelta(hours=index), run_uid=f"cached-{index}"
        )
        assert outcome.outcome == "SUCCESS_COMPLETE"
        assert result.summary.source_counts == {"SUCCESS_COMPLETE": 1}
        assert not result.summary.errors


def test_restored_checkpoint_without_canonical_row_or_appearance_refetches():
    book = book_for()
    row = source()
    _, batch, _ = collect_board(row, board(1))
    run_scan(book, [batch], now=datetime.now(UTC) + timedelta(minutes=1), run_uid="committed")
    entries = checkpoint_rows(book)
    for jobs, appearances in [
        ([], book.tabs["_Job_Sources"]),
        (book.tabs["Jobs_Master"], []),
        (
            book.tabs["Jobs_Master"],
            [book.tabs["_Job_Sources"][0] | {"external_job_id": "different"}],
        ),
    ]:
        hydrate_source_checkpoints([row], entries, jobs=jobs, appearances=appearances)
        _, batch, requests = collect_board(row, board(1))
        assert len(requests) == 2 and len(batch.jobs) == 1


def test_checkpoint_write_failure_replays_already_committed_job_without_duplicate():
    book = book_for()
    row = source()
    _, batch, _ = collect_board(row, board(1))
    now = datetime.now(UTC) + timedelta(minutes=1)

    def fail_checkpoint(workbook, updates, run_uid):
        if set(updates) == {"_System_State"}:
            raise SheetError("CHECKPOINT_WRITE_FAILURE")
        commit_rows(workbook, updates, run_uid)

    with (
        patch("career_radar.orchestration.pipeline.commit_rows", side_effect=fail_checkpoint),
        pytest.raises(SheetError, match="CHECKPOINT_WRITE_FAILURE"),
    ):
        run_scan(book, [batch], now=now, run_uid="checkpoint-failed")
    assert len(book.tabs["Jobs_Master"]) == 1 and not checkpoint_rows(book)
    hydrate_source_checkpoints(
        [row],
        book.tabs["_System_State"],
        jobs=book.tabs["Jobs_Master"],
        appearances=book.tabs["_Job_Sources"],
    )
    _, batch, requests = collect_board(row, board(1))
    assert len(requests) == 2
    result = run_scan(book, [batch], now=now, run_uid="checkpoint-recovered")
    assert result.summary.new_jobs == 0
    assert len(book.tabs["Jobs_Master"]) == len(checkpoint_rows(book)) == 1


@pytest.mark.parametrize("total", [2, -1, "1", True])
def test_supplied_inventory_total_must_match_unique_ids(total):
    outcome, jobs = collect(
        source(),
        FetchBudget(),
        wire=fake_wire(
            [response(json.dumps({"jobs": board(1), "meta": {"total": total}}).encode())]
        ),
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    assert outcome.outcome == "QUARANTINED_SCHEMA"
    assert not outcome.listing_complete and not jobs


def test_valid_inventory_totals_and_parse_failure_after_successful_detail():
    records = board(2)
    for metadata in [{"total": 2}, {}]:
        outcome, jobs = collect(
            source(),
            FetchBudget(),
            wire=fake_wire(
                [
                    response(json.dumps({"jobs": records, "meta": metadata}).encode()),
                    response(json.dumps(records[0]).encode()),
                    response(b"invalid-json"),
                ]
            ),
            resolver=lambda _: ["1.1.1.1"],
            sleep=lambda _: None,
        )
        assert outcome.outcome == "QUARANTINED_SCHEMA" and outcome.listing_complete
        assert len(jobs) == 1 and list(outcome.checkpoint) == ["0"]


@pytest.mark.parametrize("updated", [None, "not-a-date", "2026-01-01"])
def test_unversioned_inventory_is_quarantined_without_checkpoint(updated):
    records = board(1)
    records[0]["updated_at"] = updated
    outcome, batch, _ = collect_board(source(), records)
    assert outcome.outcome == "QUARANTINED_SCHEMA"
    assert not outcome.snapshot_complete and not outcome.checkpoint and not batch.jobs


def test_conflicting_inventory_and_wrong_detail_id_cannot_checkpoint():
    records = board(1)
    conflicting = records + [records[0] | {"updated_at": "2026-09-14T02:00:00Z"}]
    outcome, _, _ = collect_board(source(), conflicting)
    assert outcome.outcome == "QUARANTINED_SCHEMA" and not outcome.listing_complete
    outcome, jobs = collect(
        source(),
        FetchBudget(),
        wire=fake_wire(
            [
                response(json.dumps({"jobs": records}).encode()),
                response(json.dumps(records[0] | {"id": 999}).encode()),
            ]
        ),
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    assert outcome.outcome == "QUARANTINED_SCHEMA" and not jobs and not outcome.checkpoint


def test_checkpoint_hydration_rejects_malformed_or_foreign_rows():
    row = source(metadata={"_greenhouse_seen_versions": {"untrusted": "a" * 64}})
    value = {
        "source_id": row.source_id,
        "external_job_id": "1",
        "version": "a" * 64,
        "job_uid": "committed",
    }
    key = "greenhouse_checkpoint:" + uid(row.source_id, "1")
    entries = [
        {"key": "unrelated", "value": value},
        {"key": key, "value": "invalid-json"},
        {"key": key, "value": "[]"},
        *(
            {"key": key, "value": value | change}
            for change in [
                {"source_id": "foreign"},
                {"external_job_id": ""},
                {"version": "bad"},
                {"version": "g" * 64},
                {"job_uid": ""},
            ]
        ),
        {"key": key + "bad", "value": value},
        {"key": key, "value": json.dumps(value)},
    ]
    hydrate_source_checkpoints(
        [row],
        entries,
        jobs=[{"job_uid": "committed"}],
        appearances=[{"source_id": row.source_id, "external_job_id": "1", "job_uid": "committed"}],
    )
    assert row.metadata["_greenhouse_seen_versions"] == {"1": "a" * 64}
    from career_radar.domain import FetchResult

    raw = parse(
        row, FetchResult(source_id=row.source_id, url=row.url, body=json.dumps(board(1)[0]))
    )[0]
    raw.updated_at_raw = None
    with pytest.raises(ParseError, match="VERSION_REQUIRED"):
        greenhouse_listing_version(raw)
