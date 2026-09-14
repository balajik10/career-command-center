from __future__ import annotations

import copy
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from career_radar.sheets import FakeWorkbook, GoogleSheetsWorkbook, SheetError
from career_radar.sheets.actions import (
    ALLOWED_TRANSITIONS,
    payload_hash,
    process_action_updates,
    update_identity,
    valid_transition,
)
from career_radar.sheets.schema import (
    PROJECT_MARKER,
    SCHEMA,
    SCHEMA_VERSION,
    column_letter,
    presentation_requests,
)
from career_radar.sheets.views import install_formulas, refresh_action_queue
from career_radar.sheets.workbook import BaseWorkbook, bootstrap_google_sheet, encode_cell

NOW = datetime(2026, 9, 14, 6, tzinfo=UTC)


class Response:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self.payload, self.status_code = payload, status

    def json(self) -> Any:
        return self.payload


class SheetsHTTP:
    """In-memory HTTP server contract, including physical coordinates and RAW cells."""

    def __init__(self) -> None:
        self.sheets: dict[str, int] = {}
        self.metadata: list[Any] = []
        self.grid: dict[str, list[list[Any]]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.responses: list[Response] = []
        self.charts: dict[int, dict[str, Any]] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        self.calls.append((method, url, kwargs))
        if self.responses:
            return self.responses.pop(0)
        if method == "POST" and url.endswith("/spreadsheets"):
            return Response({"spreadsheetId": "synthetic-id"})
        if method == "GET" and ":batchGet" not in url:
            return Response(
                {
                    "sheets": [
                        {
                            "properties": {"title": name, "sheetId": sid},
                            "protectedRanges": [
                                {
                                    "description": "Career Command Center v1: protection",
                                    "protectedRangeId": sid,
                                }
                            ],
                            "charts": [
                                chart
                                for chart in self.charts.values()
                                if chart["position"]["overlayPosition"]["anchorCell"]["sheetId"]
                                == sid
                            ],
                        }
                        for name, sid in self.sheets.items()
                    ],
                    "developerMetadata": self.metadata,
                }
            )
        if method == "POST" and url.endswith(":batchUpdate") and "/values" not in url:
            for req in kwargs["json"]["requests"]:
                if "addSheet" in req:
                    props = req["addSheet"]["properties"]
                    self.sheets[props["title"]] = props["sheetId"]
                if "createDeveloperMetadata" in req:
                    self.metadata.append(req["createDeveloperMetadata"]["developerMetadata"])
                if "addChart" in req:
                    chart = copy.deepcopy(req["addChart"]["chart"])
                    chart["chartId"] = max(self.charts, default=1000) + 1
                    self.charts[chart["chartId"]] = chart
                if "deleteEmbeddedObject" in req:
                    self.charts.pop(req["deleteEmbeddedObject"]["objectId"])
            return Response({})
        if method == "GET":
            ranges = [value for key, value in kwargs["params"] if key == "ranges"]
            blocks = []
            for item in ranges:
                name = item.split("'")[1]
                rows = copy.deepcopy(self.grid.get(name, []))
                while rows and not any(value != "" for value in rows[-1]):
                    rows.pop()
                blocks.append({"range": item, "values": rows})
            return Response({"valueRanges": blocks})
        for data in kwargs["json"]["data"]:
            name, cell = data["range"].split("!")
            name = name.strip("'")
            match = re.fullmatch(r"([A-Z]+)(\d+)", cell)
            assert match
            col = 0
            for character in match[1]:
                col = col * 26 + ord(character) - 64
            row = int(match[2])
            grid = self.grid.setdefault(name, [])
            for offset, values in enumerate(data["values"]):
                while len(grid) < row + offset:
                    grid.append([])
                while len(grid[row + offset - 1]) < col + len(values) - 1:
                    grid[row + offset - 1].append("")
                for i, value in enumerate(values):
                    grid[row + offset - 1][col + i - 1] = value
        return Response({})


@pytest.fixture
def wb() -> FakeWorkbook:
    result = FakeWorkbook()
    result.bootstrap()
    return result


def test_schema_bootstrap_ownership_and_unknown_adoption(wb: FakeWorkbook) -> None:
    assert len(SCHEMA) == 28
    assert sum(spec.hidden for spec in SCHEMA.values()) == 5
    assert len(wb.tabs["Action_Updates"]) == 100
    assert wb.bootstrap()["schema_version"] == SCHEMA_VERSION
    assert len(wb.tabs["Action_Updates"]) == 100
    assert len({row["row_uid"] for row in wb.tabs["Action_Updates"]}) == 100
    assert wb.verify() == []
    wb.upsert(
        "Jobs_Master", "job_uid", [{"job_uid": "j1", "title": "old", "notes": "machine notes"}], "r"
    )
    assert "notes" not in wb.tabs["Jobs_Master"][0]
    wb.tabs["Jobs_Master"][0]["notes"] = "human note"
    wb.before_write = lambda current: current.tabs["Jobs_Master"][0].update(
        notes="interleaved human edit"
    )
    wb.upsert("Jobs_Master", "job_uid", [{"job_uid": "j1", "title": "new", "notes": "bad"}], "r")
    assert wb.tabs["Jobs_Master"][0]["notes"] == "interleaved human edit"
    assert wb.upsert("Jobs_Master", "job_uid", [{"job_uid": "j1", "title": "new"}], "r") == 0
    with pytest.raises(SheetError, match="USER_OWNED"):
        wb.upsert("Applications", "application_uid", [{"application_uid": "x"}], "r")
    with pytest.raises(SheetError, match="PROTECTED_VIEW"):
        wb.upsert("Action_Queue", "job_uid", [{"job_uid": "x"}], "r")
    with pytest.raises(SheetError, match="INVALID_KEY"):
        wb.upsert("Jobs_Master", "wrong", [], "r")
    for bad in ({"job_uid": ""}, {"job_uid": "x", "unknown": "v"}):
        with pytest.raises(SheetError, match="INVALID_ROW"):
            wb.upsert("Jobs_Master", "job_uid", [bad], "r")
    with pytest.raises(SheetError, match="RUN_UID_REQUIRED"):
        wb.upsert("Jobs_Master", "job_uid", [], "")
    wb.tabs["_System_State"] = []
    assert len(wb.verify()) == 2
    with pytest.raises(SheetError):
        wb.bootstrap()


def test_chunk_failure_and_recovery_single_request_atomicity(wb: FakeWorkbook) -> None:
    rows = [
        {
            "job_uid": f"j{i}",
            "title": f"Job {i}",
            "company": "Example",
            "fit_score": 90,
            "apply_url": "https://example.com/apply",
        }
        for i in range(150)
    ]
    wb.fail_on_write = 2
    with pytest.raises(SheetError, match="INJECTED"):
        wb.upsert("Jobs_Master", "job_uid", rows, "interrupted")
    assert 0 < len(wb.tabs["Jobs_Master"]) < 150
    assert wb.tabs["Run_Log"][0]["status"] == "IN_PROGRESS"
    wb.fail_on_write = None
    wb.upsert("Jobs_Master", "job_uid", rows, "interrupted")
    wb.finalize_run("interrupted", {"new_jobs": 150})
    assert len(wb.tabs["Jobs_Master"]) == 150
    assert len({row["job_uid"] for row in wb.tabs["Jobs_Master"]}) == 150
    assert wb.tabs["Run_Log"][0]["status"] == "COMMITTED"
    event = {"event_uid": "e1", "job_uid": "j1", "details": "original"}
    wb.transaction({"Application_Events": [event]}, "r")
    wb.transaction({"Application_Events": [event]}, "r")
    assert len(wb.tabs["Application_Events"]) == 1
    with pytest.raises(SheetError, match="APPEND_ONLY"):
        wb.transaction({"Application_Events": [{**event, "details": "changed"}]}, "r")
    wb.tabs["Jobs_Master"].append(wb.tabs["Jobs_Master"][0])
    with pytest.raises(SheetError, match="DUPLICATE"):
        wb.finalize_run("bad")


def test_cached_snapshot_minimal_reads_and_error_cleanup(wb: FakeWorkbook) -> None:
    before = wb.read_requests
    with wb.snapshot(["Jobs_Master", "Application_Events"]):
        for index in range(3):
            wb.upsert("Jobs_Master", "job_uid", [{"job_uid": f"j{index}"}], "r")
        wb.finalize_run("r")
        assert wb.read_requests == before + 1
        with pytest.raises(SheetError, match="NESTED"), wb.snapshot([]):
            pass
        assert wb.read_tab("Jobs_Master")[1]["job_uid"] == "j1"
        wb.read_tab("Action_Updates")
        assert wb.read_requests == before + 2
    assert wb._snapshot is None
    with pytest.raises(RuntimeError), wb.snapshot([]):
        raise RuntimeError("stop")
    assert wb._snapshot is None


def test_persistence_export_serialization(tmp_path: Path, wb: FakeWorkbook) -> None:
    assert encode_cell(None) == ""
    assert encode_cell(True) is True
    assert encode_cell(NOW) == NOW.isoformat()
    assert encode_cell({"a": "b"}) == '{"a": "b"}'
    assert str(encode_cell('=IMPORTXML("x")')).startswith("'")
    path = tmp_path / "private" / "workbook.json"
    wb.save(path)
    assert FakeWorkbook.load(path).verify() == []
    assert wb.export(tmp_path).exists()
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(NotImplementedError):
        BaseWorkbook()._read_tabs([])
    with pytest.raises(NotImplementedError):
        BaseWorkbook()._write_cells([])


def submit(wb: FakeWorkbook, **updates: Any) -> None:
    wb.tabs["Action_Updates"][0].update(
        job_uid="j1", application_stage_update="APPLIED", submit=True, **updates
    )


def test_action_submission_event_replay_and_immutable_payload(wb: FakeWorkbook) -> None:
    wb.upsert("Jobs_Master", "job_uid", [{"job_uid": "j1"}], "r")
    submit(wb, referral_stage_update="AGREED", update_note="Applied on official site")
    row = wb.tabs["Action_Updates"][0]
    assert len(payload_hash(row)) == len(update_identity(row)) == 64
    assert process_action_updates(wb, "r", now=NOW)["APPLIED"] == 1
    assert wb.tabs["Applications"][0]["applied_at"] == NOW.isoformat()
    assert wb.tabs["Applications"][0]["referral_stage"] == "AGREED"
    assert len(wb.tabs["Application_Events"]) == 1
    assert process_action_updates(wb, "r", now=NOW)["REPLAY"] == 1
    wb.tabs["Action_Updates"][0]["job_uid"] = "j2"
    assert process_action_updates(wb, "r", now=NOW)["CHANGED_AFTER_SUBMIT"] == 1
    assert wb.tabs["Applications"][0]["job_uid"] == "j1"
    assert len(wb.tabs["Application_Events"]) == 1


def test_interleaved_action_edit_is_never_applied_to_new_job(wb: FakeWorkbook) -> None:
    wb.upsert("Jobs_Master", "job_uid", [{"job_uid": "j1"}, {"job_uid": "j2"}], "r")
    submit(wb)
    wb.before_write = lambda current: current.tabs["Action_Updates"][0].update(job_uid="j2")
    assert process_action_updates(wb, "r", now=NOW)["CHANGED_AFTER_SUBMIT"] == 1
    assert not wb.tabs["Applications"]
    assert not wb.tabs["Application_Events"]
    assert wb.tabs["Action_Updates"][0]["job_uid"] == "j2"


def test_late_edit_at_acknowledgement_keeps_original_event_and_flags_review(
    wb: FakeWorkbook,
) -> None:
    wb.upsert("Jobs_Master", "job_uid", [{"job_uid": "j1"}, {"job_uid": "j2"}], "r")
    submit(wb)

    def after_claim(current: FakeWorkbook) -> None:
        current.before_write = lambda active: active.tabs["Action_Updates"][0].update(job_uid="j2")

    wb.before_write = after_claim
    assert process_action_updates(wb, "r", now=NOW)["CHANGED_AFTER_SUBMIT"] == 1
    assert wb.tabs["Applications"][0]["job_uid"] == "j1"
    assert wb.tabs["Application_Events"][0]["job_uid"] == "j1"
    assert wb.tabs["Action_Updates"][0]["result"] == "CHANGED_AFTER_SUBMIT"


@pytest.mark.parametrize(
    "change,result",
    [
        ({"row_uid": "tampered"}, "REJECTED"),
        ({"update_uid": "tampered"}, "REJECTED"),
        ({"job_uid": "unknown"}, "REJECTED"),
        ({"application_stage_update": "WRONG"}, "REJECTED"),
        ({"referral_stage_update": "WRONG"}, "REJECTED"),
        ({"outreach_stage_update": "WRONG"}, "REJECTED"),
        ({"outreach_stage_update": "REPLIED"}, "REJECTED"),
    ],
)
def test_action_invalid_inputs(wb: FakeWorkbook, change: dict[str, Any], result: str) -> None:
    wb.upsert("Jobs_Master", "job_uid", [{"job_uid": "j1"}], "r")
    submit(wb)
    wb.tabs["Action_Updates"][0].update(change)
    assert process_action_updates(wb, "r", now=NOW)[result] == 1
    assert not wb.tabs["Application_Events"]


def test_outreach_manual_state_and_correction(wb: FakeWorkbook) -> None:
    wb.upsert("Jobs_Master", "job_uid", [{"job_uid": "j1"}], "r")
    wb.upsert("Outreach", "draft_id", [{"draft_id": "d1", "job_uid": "j1", "body": "Draft"}], "r")
    submit(wb, outreach_stage_update="SENT_MANUALLY", reason="CORRECTION: actual stage")
    assert process_action_updates(wb, "r", now=NOW)["APPLIED"] == 1
    assert wb.tabs["Outreach"][0]["sent_at"] == NOW.isoformat()
    assert wb.tabs["Application_Events"][0]["event_type"] == "MANUAL_CORRECTION"
    assert valid_transition("REJECTED", "APPLIED", "reason", correction=True)
    assert not valid_transition("REJECTED", "APPLIED")
    assert not valid_transition("BAD", "APPLIED")
    assert not valid_transition("NOT_REVIEWED", "SKIPPED")
    assert not valid_transition("REJECTED", "APPLIED", correction=True)
    assert valid_transition("NOT_REVIEWED", "SKIPPED", "not relevant")
    assert set(ALLOWED_TRANSITIONS) >= {"APPLIED", "REJECTED", "OFFER"}
    with pytest.raises(ValueError, match="timezone"):
        process_action_updates(wb, "r", now=datetime(2026, 1, 1))


def test_google_rest_bootstrap_headers_protection_and_idempotency() -> None:
    http = SheetsHTTP()
    google = GoogleSheetsWorkbook("synthetic", http, sleeper=lambda _: None)
    result = google.bootstrap()
    assert result["tabs"] == 28
    assert len(http.sheets) == 28
    assert google.verify() == []
    google.upsert("Jobs_Master", "job_uid", [{"job_uid": "j1", "title": "=unsafe"}], "r")
    assert google.read_tab("Jobs_Master")[0]["title"] == "'=unsafe"
    google.bootstrap()
    assert len(google.read_tab("Action_Updates")) == 100
    assert google.read_tab("Jobs_Master")[0]["title"] == "'=unsafe"
    assert len(http.metadata) == 1
    assert http.metadata[0]["metadataValue"] == PROJECT_MARKER
    requests = presentation_requests(http.sheets)
    assert sum("addProtectedRange" in req for req in requests) == 28
    assert any("setDataValidation" in req for req in requests)
    assert any("addConditionalFormatRule" in req for req in requests)
    assert column_letter(1) == "A" and column_letter(27) == "AA" and column_letter(0) == ""
    assert any(
        data["json"].get("valueInputOption") == "USER_ENTERED"
        for method, url, data in http.calls
        if method == "POST" and "/values" in url
    )


def test_google_unknown_nonempty_mismatch_and_http_budget() -> None:
    http = SheetsHTTP()
    http.sheets["Existing"] = 0
    http.grid["Existing"] = [["user value"]]
    google = GoogleSheetsWorkbook("synthetic", http, sleeper=lambda _: None)
    with pytest.raises(SheetError, match="REFUSE_NONEMPTY"):
        google.bootstrap()
    with pytest.raises(SheetError, match="HEADERS_MISMATCH"):
        google.read_tab("Jobs_Master")
    http.responses = [Response({}, 429), Response({}, 429), Response({}, 200)]
    sleeps: list[float] = []
    google.sleeper = sleeps.append
    assert google._request("GET", "") == {}
    assert sleeps == [1, 2]
    http.responses = [Response({}, 403)]
    with pytest.raises(SheetError, match="HTTP_403"):
        google._request("GET", "")
    limited = GoogleSheetsWorkbook("synthetic", http, max_read_requests=0, max_write_requests=0)
    with pytest.raises(SheetError, match="READ_BUDGET"):
        limited._request("GET", "")
    with pytest.raises(SheetError, match="WRITE_BUDGET"):
        limited._request("POST", "")
    google._request_times.clear()
    google._request_times.extend([0.0] * 45)
    google._request("GET", "")
    assert len(google._request_times) == 1
    import time

    google._request_times.extend([time.monotonic()] * 45)
    google._request("GET", "")
    assert len(sleeps) == 3


def test_create_google_spreadsheet_and_creation_failure() -> None:
    http = SheetsHTTP()
    assert (
        bootstrap_google_sheet(http, "Synthetic Career Command Center").spreadsheet_id
        == "synthetic-id"
    )
    http.responses = [Response({}, 403)]
    with pytest.raises(SheetError, match="CREATE_HTTP_403"):
        bootstrap_google_sheet(http, "Synthetic")


def test_protected_views_use_canonical_and_clear_finished_rows(wb: FakeWorkbook) -> None:
    wb.upsert(
        "Jobs_Master",
        "job_uid",
        [
            {
                "job_uid": "j1",
                "title": "Backend",
                "action_priority": "P0",
                "action_by_ist": "2026-09-13 10:00 IST",
            },
            {"job_uid": "j2", "action_priority": "P3"},
            {"job_uid": "j3", "action_priority": "P1"},
        ],
        "r",
    )
    assert refresh_action_queue(wb, "r", now=NOW) == 2
    assert wb.tabs["Action_Queue"][0]["overdue"] == "OVERDUE"
    assert wb.tabs["Action_Queue"][0]["posted_at_source"] == "POSTED DATE UNKNOWN"
    wb.transaction(
        {
            "Applications": [
                {"application_uid": "a1", "job_uid": "j1", "application_stage": "REJECTED"}
            ]
        },
        "r",
    )
    assert refresh_action_queue(wb, "r", now=NOW) == 1
    assert wb.tabs["Action_Queue"][1]["job_uid"] == ""
    install_formulas(wb, {name: i for i, name in enumerate(SCHEMA)})
    assert wb.tabs["Dashboard"][0]["value"].startswith("=SUMPRODUCT")
    assert "TIME(5,30,0)" in wb.tabs["Today"][0]["job_uid"]
    assert "CHECK" not in wb.tabs["START_HERE"][0]["instruction"]
