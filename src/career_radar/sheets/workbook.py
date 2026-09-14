"""Sheet authority with bounded, resumable writes and explicit field ownership."""

import copy
import hashlib
import json
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
from typing import Any, Protocol, cast

from career_radar.security import safe_cell

from .schema import (
    INPUT_RESERVE,
    PROJECT_MARKER,
    SCHEMA,
    SCHEMA_VERSION,
    column_letter,
    presentation_requests,
)

Row = dict[str, Any]
Cell = tuple[str, int, int, Any]


class SheetError(RuntimeError):
    """The authoritative Sheet could not safely complete a request."""


class Session(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


class Workbook(Protocol):
    def read_tab(self, name: str) -> list[Row]: ...
    def read_tabs(self, names: Sequence[str]) -> dict[str, list[Row]]: ...
    def upsert(
        self,
        tab: str,
        key: str,
        rows: Sequence[Mapping[str, Any]],
        run_uid: str,
        *,
        actor: str = "machine",
    ) -> int: ...
    def bootstrap(self) -> dict[str, Any]: ...
    def verify(self) -> list[str]: ...


def encode_cell(value: Any) -> str | int | float | bool:
    if value is None:
        return ""
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list | dict | tuple):
        return safe_cell(json.dumps(value, sort_keys=True, default=str))
    return safe_cell(str(value))


class BaseWorkbook:
    """Exactly one application writer is required; Sheets has no row CAS."""

    def __init__(self) -> None:
        self._snapshot: dict[str, list[Row]] | None = None

    def _read_tabs(self, names: Sequence[str]) -> dict[str, list[Row]]:
        raise NotImplementedError

    def read_tabs(self, names: Sequence[str]) -> dict[str, list[Row]]:
        if self._snapshot is None:
            return self._read_tabs(names)
        missing = [name for name in names if name not in self._snapshot or name == "Action_Updates"]
        if missing:
            self._snapshot.update(self._read_tabs(missing))
        return {name: copy.deepcopy(self._snapshot[name]) for name in names}

    @contextmanager
    def snapshot(self, names: Sequence[str]) -> Iterator["BaseWorkbook"]:
        """Batch-read indexes once for a serialized scan; input commands stay fresh."""
        if self._snapshot is not None:
            raise SheetError("NESTED_SNAPSHOT")
        self._snapshot = self._read_tabs(list(dict.fromkeys([*names, "_System_State", "Run_Log"])))
        try:
            self.require_schema()
            yield self
        finally:
            self._snapshot = None

    def read_tab(self, name: str) -> list[Row]:
        return self.read_tabs([name])[name]

    def _write_cells(self, cells: Sequence[Cell], *, formulas: bool = False) -> None:
        raise NotImplementedError

    def write_cells(self, cells: Sequence[Cell], *, formulas: bool = False) -> None:
        self._write_cells(cells, formulas=formulas)
        if self._snapshot is not None:
            for name, row, column, value in cells:
                if name in self._snapshot:
                    entries = self._snapshot[name]
                    while len(entries) < row - 1:
                        entries.append({})
                    entries[row - 2][SCHEMA[name].columns[column - 1]] = value

    def verify(self) -> list[str]:
        state = {row["key"]: str(row.get("value", "")) for row in self.read_tab("_System_State")}
        problems = []
        if state.get("project_marker") != PROJECT_MARKER:
            problems.append("UNRECOGNIZED_PROJECT_MARKER")
        if state.get("schema_version") != SCHEMA_VERSION:
            problems.append("SCHEMA_VERSION_MISMATCH")
        return problems

    def require_schema(self) -> None:
        problems = self.verify()
        if problems:
            raise SheetError(",".join(problems))

    def _cells_for_rows(
        self,
        name: str,
        key: str,
        rows: Sequence[Mapping[str, Any]],
        existing: Sequence[Row],
        actor: str,
    ) -> tuple[list[Cell], int]:
        spec = SCHEMA[name]
        if key != spec.key:
            raise SheetError(f"INVALID_KEY:{name}:{key}")
        if spec.view and actor != "system":
            raise SheetError("PROTECTED_VIEW")
        index = {str(row.get(key, "")): (i + 2, row) for i, row in enumerate(existing)}
        cells: list[Cell] = []
        changed = 0
        for row in rows:
            identity = str(row.get(key, ""))
            if not identity or set(row) - set(spec.columns):
                raise SheetError(f"INVALID_ROW:{name}")
            row_number, previous = index.get(identity, (len(index) + 2, {}))
            if spec.append_only and previous:
                if any(
                    encode_cell(value) != encode_cell(previous.get(field))
                    for field, value in row.items()
                ):
                    raise SheetError("APPEND_ONLY_EVENT_CHANGED")
                continue
            allowed = set(row)
            if actor == "machine":
                allowed -= spec.user_fields
                if name in {"Applications", "Action_Updates", "Profile", "Config", "Companies"}:
                    raise SheetError("USER_OWNED_TABLE")
            writes = [
                (name, row_number, spec.columns.index(field) + 1, encode_cell(row[field]))
                for field in sorted(allowed)
                if encode_cell(row[field]) != encode_cell(previous.get(field))
            ]
            cells.extend(writes)
            changed += bool(writes)
            index[identity] = (
                row_number,
                dict(previous) | {field: row[field] for field in allowed},
            )
        return cells, changed

    def upsert(
        self,
        tab: str,
        key: str,
        rows: Sequence[Mapping[str, Any]],
        run_uid: str,
        *,
        actor: str = "machine",
    ) -> int:
        if not run_uid:
            raise SheetError("RUN_UID_REQUIRED")
        self.require_schema()
        data = self.read_tabs([tab, "_System_State", "Run_Log"])
        cells, changed = self._cells_for_rows(tab, key, rows, data[tab], actor)
        # Chunks contain complete rows where practical. The stable chunk key is
        # written atomically with the data; replay recomputes only missing cells.
        chunks: list[list[Cell]] = []
        for _, group in groupby(cells, key=lambda cell: (cell[0], cell[1])):
            complete_row = list(group)
            if not chunks or len(chunks[-1]) + len(complete_row) > 400:
                chunks.append([])
            chunks[-1].extend(complete_row)
        for chunk in chunks:
            digest = hashlib.sha256(
                json.dumps(chunk, sort_keys=True, default=str).encode()
            ).hexdigest()
            marker = {"key": f"chunk:{run_uid}:{digest}", "value": "APPLIED", "run_uid": run_uid}
            mark_cells, _ = self._cells_for_rows(
                "_System_State", "key", [marker], data["_System_State"], "system"
            )
            run = {"run_uid": run_uid, "status": "IN_PROGRESS"}
            run_cells, _ = self._cells_for_rows(
                "Run_Log", "run_uid", [run], data["Run_Log"], "system"
            )
            self.write_cells([*chunk, *mark_cells, *run_cells])
            data["_System_State"].append(marker)
            if not any(row.get("run_uid") == run_uid for row in data["Run_Log"]):
                data["Run_Log"].append(run)
        return changed

    def transaction(
        self,
        updates: Mapping[str, Sequence[Mapping[str, Any]]],
        run_uid: str,
        *,
        actor: str = "system",
    ) -> None:
        """One bounded Sheets request, used for an outbox claim or a workflow event."""
        if not run_uid:
            raise SheetError("RUN_UID_REQUIRED")
        self.require_schema()
        current = self.read_tabs(list(updates))
        cells: list[Cell] = []
        for name, rows in updates.items():
            additions, _ = self._cells_for_rows(name, SCHEMA[name].key, rows, current[name], actor)
            cells.extend(additions)
        if len(cells) > 10000:
            raise SheetError("TRANSACTION_TOO_LARGE")
        if cells:
            self.write_cells(cells)

    def finalize_run(self, run_uid: str, summary: Mapping[str, Any] | None = None) -> None:
        self.require_schema()
        jobs = self.read_tab("Jobs_Master")
        if len({row["job_uid"] for row in jobs}) != len(jobs):
            raise SheetError("POST_WRITE_DUPLICATE_UID")
        self.transaction(
            {
                "Run_Log": [
                    {
                        "run_uid": run_uid,
                        **dict(summary or {}),
                        "status": "COMMITTED",
                        "completed_at": datetime.now(UTC).isoformat(),
                    }
                ]
            },
            run_uid,
        )

    def export(self, directory: Path) -> Path:
        self.require_schema()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"sheet-backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
        path.write_text(json.dumps(self.read_tabs(list(SCHEMA)), indent=2, default=str))
        path.chmod(0o600)
        return path


class FakeWorkbook(BaseWorkbook):
    """Disposable offline workbook; persisted explicitly, never a live fallback."""

    def __init__(self) -> None:
        super().__init__()
        self.tabs: dict[str, list[Row]] = {}
        self.read_requests = 0
        self.write_requests = 0
        self.before_write: Callable[[FakeWorkbook], None] | None = None
        self.fail_on_write: int | None = None

    def _read_tabs(self, names: Sequence[str]) -> dict[str, list[Row]]:
        self.read_requests += 1
        return {name: copy.deepcopy(self.tabs.get(name, [])) for name in names}

    def _write_cells(self, cells: Sequence[Cell], *, formulas: bool = False) -> None:
        self.write_requests += 1
        if self.fail_on_write == self.write_requests:
            raise SheetError("INJECTED_WRITE_FAILURE")
        if self.before_write:
            hook, self.before_write = self.before_write, None
            hook(self)
        revised = copy.deepcopy(self.tabs)
        for name, row, column, value in cells:
            entries = revised.setdefault(name, [])
            while len(entries) < row - 1:
                entries.append({})
            entries[row - 2][SCHEMA[name].columns[column - 1]] = value
        self.tabs = revised

    def bootstrap(self) -> dict[str, Any]:
        if any(self.tabs.values()):
            self.require_schema()
        for name in SCHEMA:
            self.tabs.setdefault(name, [])
        if not self.tabs["_System_State"]:
            self.tabs["_System_State"] = [
                {"key": "project_marker", "value": PROJECT_MARKER},
                {"key": "schema_version", "value": SCHEMA_VERSION},
            ]
        self._seed_input_rows()
        return {"tabs": len(self.tabs), "schema_version": SCHEMA_VERSION, "mode": "offline"}

    def _seed_input_rows(self) -> None:
        remaining = sum(not row.get("submit") for row in self.tabs["Action_Updates"])
        for _ in range(max(0, INPUT_RESERVE - remaining)):
            uid = str(uuid.uuid4())
            position = len(self.tabs["Action_Updates"]) + 2
            self.tabs["Action_Updates"].append({"row_uid": uid})
            self.tabs["_System_State"].append({"key": f"action_row:{position}", "value": uid})

    def save(self, path: Path | str) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(self.tabs, indent=2, default=str))
        destination.chmod(0o600)

    @classmethod
    def load(cls, path: Path | str) -> "FakeWorkbook":
        workbook = cls()
        workbook.tabs = json.loads(Path(path).read_text())
        workbook.require_schema()
        return workbook


class GoogleSheetsWorkbook(BaseWorkbook):
    URL = "https://sheets.googleapis.com/v4/spreadsheets"

    def __init__(
        self,
        spreadsheet_id: str,
        session: Session,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        max_read_requests: int = 60,
        max_write_requests: int = 60,
    ) -> None:
        super().__init__()
        self.spreadsheet_id = spreadsheet_id
        self.session = session
        self.sleeper = sleeper
        self.max_read_requests = max_read_requests
        self.max_write_requests = max_write_requests
        self.read_requests = 0
        self.write_requests = 0
        self._request_times: deque[float] = deque()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in range(3):
            if method == "GET":
                if self.read_requests >= self.max_read_requests:
                    raise SheetError("SHEETS_READ_BUDGET_EXHAUSTED")
                self.read_requests += 1
            else:
                if self.write_requests >= self.max_write_requests:
                    raise SheetError("SHEETS_WRITE_BUDGET_EXHAUSTED")
                self.write_requests += 1
            clock = time.monotonic()
            while self._request_times and clock - self._request_times[0] >= 60:
                self._request_times.popleft()
            if len(self._request_times) >= 45:
                self.sleeper(max(0, 60 - (clock - self._request_times.popleft())))
            self._request_times.append(time.monotonic())
            try:
                response = self.session.request(
                    method, f"{self.URL}/{self.spreadsheet_id}{path}", timeout=30, **kwargs
                )
            except Exception:
                raise SheetError("SHEETS_TRANSPORT_ERROR") from None
            if response.status_code == 429 and attempt < 2:
                self.sleeper(min(8.0, 2.0**attempt))
                continue
            if response.status_code >= 400:
                raise SheetError(f"SHEETS_HTTP_{response.status_code}")
            return cast(dict[str, Any], response.json())
        raise SheetError("SHEETS_RETRY_EXHAUSTED")

    def _read_tabs(self, names: Sequence[str]) -> dict[str, list[Row]]:
        ranges = [
            ("ranges", f"'{name}'!A1:{column_letter(len(SCHEMA[name].columns))}") for name in names
        ]
        payload = self._request(
            "GET", "/values:batchGet", params=[*ranges, ("valueRenderOption", "UNFORMATTED_VALUE")]
        )
        result: dict[str, list[Row]] = {}
        for name, block in zip(names, payload.get("valueRanges", []), strict=True):
            values = block.get("values", [])
            if not values or tuple(values[0]) != SCHEMA[name].columns:
                raise SheetError(f"SCHEMA_HEADERS_MISMATCH:{name}")
            result[name] = [
                dict(zip(SCHEMA[name].columns, row, strict=False)) for row in values[1:]
            ]
        return result

    def _write_cells(self, cells: Sequence[Cell], *, formulas: bool = False) -> None:
        data = [
            {"range": f"'{name}'!{column_letter(column)}{row}", "values": [[value]]}
            for name, row, column, value in cells
        ]
        if data:
            self._request(
                "POST",
                "/values:batchUpdate",
                json={"valueInputOption": "USER_ENTERED" if formulas else "RAW", "data": data},
            )

    def bootstrap(self) -> dict[str, Any]:
        metadata = self._request(
            "GET",
            "",
            params={
                "fields": "sheets(properties(sheetId,title),protectedRanges(protectedRangeId,description),charts(chartId,spec(title))),developerMetadata"
            },
        )
        sheets = {
            entry["properties"]["title"]: entry["properties"]["sheetId"]
            for entry in metadata.get("sheets", [])
        }
        markers = {
            item.get("metadataKey"): item.get("metadataValue")
            for item in metadata.get("developerMetadata", [])
        }
        if sheets and markers.get("career_command_center") != PROJECT_MARKER:
            # A newly created spreadsheet is empty; any cell in an unmarked sheet
            # means explicit migration/adoption is required, never overwrite it.
            existing = self._request(
                "GET", "/values:batchGet", params=[("ranges", f"'{name}'") for name in sheets]
            )
            if any(block.get("values") for block in existing.get("valueRanges", [])):
                raise SheetError("REFUSE_NONEMPTY_UNKNOWN_WORKBOOK")
        if markers.get("career_command_center") == PROJECT_MARKER:
            recognized = [name for name in sheets if name in SCHEMA]
            existing_headers = self._request(
                "GET",
                "/values:batchGet",
                params=[
                    ("ranges", f"'{name}'!A1:{column_letter(len(SCHEMA[name].columns))}1")
                    for name in recognized
                ],
            )
            for name, block in zip(
                recognized, existing_headers.get("valueRanges", []), strict=True
            ):
                values = block.get("values", [])
                if values and tuple(values[0]) != SCHEMA[name].columns:
                    raise SheetError(f"MIGRATION_REQUIRED_HEADERS:{name}")
            if "_System_State" in recognized and self.read_tab("_System_State"):
                self.require_schema()
        requests: list[dict[str, Any]] = []
        for index, name in enumerate(SCHEMA, 100):
            if name not in sheets:
                while index in sheets.values():
                    index += 100
                sheets[name] = index
                requests.append(
                    {
                        "addSheet": {
                            "properties": {
                                "sheetId": index,
                                "title": name,
                                "gridProperties": {
                                    "rowCount": 6000,
                                    "columnCount": max(26, len(SCHEMA[name].columns)),
                                },
                            }
                        }
                    }
                )
        if not markers.get("career_command_center"):
            requests.append(
                {
                    "createDeveloperMetadata": {
                        "developerMetadata": {
                            "metadataKey": "career_command_center",
                            "metadataValue": PROJECT_MARKER,
                            "visibility": "DOCUMENT",
                            "location": {"spreadsheet": True},
                        }
                    }
                }
            )
        # Replace only this application's own protections on a repeated bootstrap.
        for entry in metadata.get("sheets", []):
            for protected in entry.get("protectedRanges", []):
                if protected.get("description", "").startswith("Career Command Center v"):
                    requests.append(
                        {
                            "deleteProtectedRange": {
                                "protectedRangeId": protected["protectedRangeId"]
                            }
                        }
                    )
            if entry["properties"]["title"] == "Dashboard":
                from .views import CHART_PREFIX

                for chart in entry.get("charts", []):
                    if str(chart.get("spec", {}).get("title", "")).startswith(CHART_PREFIX):
                        requests.append({"deleteEmbeddedObject": {"objectId": chart["chartId"]}})
        requests.extend(presentation_requests(sheets))
        from .views import dashboard_chart_requests

        requests.extend(dashboard_chart_requests(sheets["Dashboard"]))
        for offset in range(0, len(requests), 250):
            self._request(
                "POST", ":batchUpdate", json={"requests": requests[offset : offset + 250]}
            )
        headers = [
            {"range": f"'{name}'!A1", "values": [list(spec.columns)]}
            for name, spec in SCHEMA.items()
        ]
        self._request(
            "POST", "/values:batchUpdate", json={"valueInputOption": "RAW", "data": headers}
        )
        state = self.read_tab("_System_State")
        marker_cells, _ = self._cells_for_rows(
            "_System_State",
            "key",
            [
                {"key": "project_marker", "value": PROJECT_MARKER},
                {"key": "schema_version", "value": SCHEMA_VERSION},
                *({"key": f"sheet_id:{name}", "value": sid} for name, sid in sheets.items()),
            ],
            state,
            "system",
        )
        self.write_cells(marker_cells)
        self.replenish_action_rows()
        from .views import install_formulas

        install_formulas(self, sheets)
        return {
            "tabs": len(SCHEMA),
            "schema_version": SCHEMA_VERSION,
            "sheet_url": f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/edit",
        }

    def replenish_action_rows(self) -> None:
        rows = self.read_tab("Action_Updates")
        remaining = sum(not row.get("submit") for row in rows)
        additions = [
            {"row_uid": str(uuid.uuid4())} for _ in range(max(0, INPUT_RESERVE - remaining))
        ]
        state = [
            {"key": f"action_row:{len(rows) + i + 2}", "value": row["row_uid"]}
            for i, row in enumerate(additions)
        ]
        self.transaction({"Action_Updates": additions, "_System_State": state}, "bootstrap")


def google_authenticated_session() -> Session:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets"])
    factory: Callable[[Any], Session] = AuthorizedSession
    return factory(credentials)


def bootstrap_google_sheet(session: Session, title: str) -> GoogleSheetsWorkbook:
    response = session.request(
        "POST",
        GoogleSheetsWorkbook.URL,
        timeout=30,
        json={"properties": {"title": title, "timeZone": "Asia/Kolkata", "locale": "en_IN"}},
    )
    if response.status_code >= 400:
        raise SheetError(f"SHEETS_CREATE_HTTP_{response.status_code}")
    result = GoogleSheetsWorkbook(str(response.json()["spreadsheetId"]), session)
    result.bootstrap()
    return result
