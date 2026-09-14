"""Offline metric-oracle, formula-lineage, and real REST bootstrap contracts."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_sheets import SheetsHTTP

from career_radar.sheets import FakeWorkbook, GoogleSheetsWorkbook
from career_radar.sheets.schema import SCHEMA
from career_radar.sheets.views import (
    ATTRIBUTION_START,
    CHART_PREFIX,
    FRESHNESS_START,
    FUNNEL_START,
    ROLE_START,
    _cohort_formula,
    _reference_time,
    dashboard_chart_requests,
    dashboard_reference,
    install_formulas,
)

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
MIDNIGHT = NOW.replace(hour=0)


def canonical_snapshot() -> dict[str, list[dict[str, Any]]]:
    return {
        "Jobs_Master": [
            {
                "job_uid": "j1",
                "company": "Atlas",
                "role_family": "BACKEND",
                "first_seen_at": MIDNIGHT.isoformat(),
                "freshness_band": "POSTED_24H",
            },
            {
                "job_uid": "j2",
                "company": "Atlas",
                "role_family": "BACKEND",
                "first_seen_at": MIDNIGHT.isoformat(),
                "freshness_band": "POSTED_24H",
            },
            {
                "job_uid": "j3",
                "company": "Beta",
                "role_family": "PLATFORM",
                "first_seen_at": MIDNIGHT.isoformat(),
            },
            {"job_uid": "j4", "company": "Unassigned"},
            {"job_uid": "j5", "company": "Unassigned", "first_seen_at": "invalid"},
        ],
        "Applications": [
            {
                "job_uid": "j1",
                "application_stage": "TECH_SCREEN",
                "applied_at": (MIDNIGHT + timedelta(hours=2)).isoformat(),
            },
            {
                "job_uid": "j2",
                "application_stage": "APPLIED",
                "applied_at": (MIDNIGHT + timedelta(hours=6)).isoformat(),
            },
            # Applied before discovery: valid application, not a valid timing sample.
            {
                "job_uid": "j3",
                "application_stage": "OFFER",
                "applied_at": (MIDNIGHT - timedelta(hours=1)).isoformat(),
            },
            {
                "job_uid": "j5",
                "application_stage": "TECH_SCREEN",
                "applied_at": (NOW + timedelta(days=1)).isoformat(),
            },
        ],
        "Application_Events": [
            {"job_uid": "j1", "to_stage": "RECRUITER_SCREEN"},
            {"job_uid": "j1", "to_stage": "TECH_SCREEN"},
            {"job_uid": "j1", "to_stage": "TECH_SCREEN"},
            {"job_uid": "j3", "to_stage": "OFFER"},
        ],
        "Outreach": [
            {
                "draft_id": "d1",
                "job_uid": "j1",
                "sent_at": MIDNIGHT.isoformat(),
                "outreach_stage": "REPLIED",
            },
            {
                "draft_id": "d2",
                "job_uid": "j1",
                "sent_at": MIDNIGHT.isoformat(),
                "outreach_stage": "CLOSED",
                "reply_state": "POSITIVE",
            },
            {
                "draft_id": "d3",
                "job_uid": "j2",
                "sent_at": MIDNIGHT.isoformat(),
                "outreach_stage": "SENT_MANUALLY",
            },
            {"draft_id": "d4", "job_uid": "j3", "sent_at": "", "outreach_stage": "REPLIED"},
            {
                "draft_id": "d5",
                "job_uid": "j4",
                "sent_at": (NOW + timedelta(days=1)).isoformat(),
                "outreach_stage": "REPLIED",
            },
            {
                "draft_id": "d6",
                "job_uid": "orphan",
                "sent_at": MIDNIGHT.isoformat(),
                "outreach_stage": "REPLIED",
            },
        ],
        "_Job_Sources": [
            {"job_uid": "j1", "source_id": "first-alert"},
            {"job_uid": "j1", "source_id": "later-official"},
            {"job_uid": "j2", "source_id": "first-alert"},
            {"job_uid": "j3", "source_id": "official-board"},
        ],
        "Companies": [
            {"company": "ATLAS", "company_tier": 1},
            {"company": "Beta", "company_tier": 0},
        ],
    }


def test_reference_matches_reviewed_numeric_oracle_without_invented_steps() -> None:
    report = dashboard_reference(canonical_snapshot(), NOW)
    assert report["median_apply_hours"] == 4
    assert report["median_samples"] == 2
    assert report["funnel"] == {"applications": 3, "interviews": 1, "offers": 1, "accepted": 0}
    first = report["cohorts"]["source"]["first-alert"]
    assert first == {
        "applications": 2,
        "interviews": 1,
        "sent": 3,
        "replied": 2,
        "interview_rate": 0.5,
        "reply_rate": pytest.approx(2 / 3),
    }
    # The second appearance and repeated interview events never multiply jobs.
    assert "later-official" not in report["cohorts"]["source"]
    assert report["cohorts"]["role"]["BACKEND"] == first
    assert report["cohorts"]["tier"]["Tier 1"] == first
    assert report["cohorts"]["tier"]["Tier 0"]["interviews"] == 0
    assert report["cohorts"]["tier"]["UNASSIGNED"]["interview_rate"] is None
    assert report["cohorts"]["source"]["official-board"]["reply_rate"] is None
    assert report["freshness"] == {"POSTED_24H": 2, "UNKNOWN": 3}
    assert sum(report["role_families"].values()) == 5
    no_outreach = canonical_snapshot()
    no_outreach["Outreach"] = []
    without_messages = dashboard_reference(no_outreach, NOW)["cohorts"]["source"]["first-alert"]
    assert without_messages["applications"] == 2
    assert without_messages["interview_rate"] == 0.5
    assert without_messages["reply_rate"] is None


def test_empty_missing_timestamps_and_rejected_duplicates_are_honest() -> None:
    empty = dashboard_reference({}, NOW)
    assert empty["median_apply_hours"] is None and empty["median_samples"] == 0
    assert empty["cohorts"] == {"source": {}, "role": {}, "tier": {}}
    assert all(value == 0 for value in empty["funnel"].values())
    assert _reference_time("invalid") is None
    assert _reference_time(datetime(2026, 1, 1)) is None
    assert _reference_time(NOW) == NOW
    assert _reference_time("2026-09-14T12:00:00Z") == NOW
    with pytest.raises(ValueError, match="AWARE_TIMESTAMP"):
        dashboard_reference({}, datetime(2026, 1, 1))
    duplicate = canonical_snapshot()
    duplicate["Jobs_Master"].append(duplicate["Jobs_Master"][0])
    with pytest.raises(ValueError, match="DUPLICATE_CANONICAL"):
        dashboard_reference(duplicate, NOW)
    with pytest.raises(ValueError, match="UNKNOWN_COHORT"):
        _cohort_formula("invented")


def assert_balanced_formula(formula: str) -> None:
    """Validate nesting while honoring Sheets' doubled-quote string escaping."""
    depth = 0
    quoted = False
    index = 0
    while index < len(formula):
        character = formula[index]
        if character == '"':
            if quoted and index + 1 < len(formula) and formula[index + 1] == '"':
                index += 2
                continue
            quoted = not quoted
        elif not quoted:
            depth += (character == "(") - (character == ")")
            assert depth >= 0, formula
        index += 1
    assert depth == 0 and not quoted, formula


def test_formulas_reconcile_to_canonical_keys_and_explicit_denominators() -> None:
    book = FakeWorkbook()
    book.bootstrap()
    before = book.write_requests
    install_formulas(book, {name: index for index, name in enumerate(SCHEMA)})
    assert book.write_requests == before + 1  # all view formulas in one request
    for rows in book.tabs.values():
        for row in rows:
            for value in row.values():
                if isinstance(value, str) and value.startswith("="):
                    assert len(value) < 40_000  # below the 50k-character cell ceiling
                    assert_balanced_formula(value)
                    assert "Audit!" not in value and "'Audit'!" not in value
    metrics = {
        row.get("metric"): row.get("value") for row in book.tabs["Dashboard"] if row.get("metric")
    }
    timing = metrics["Median time to apply (hours)"]
    assert "MEDIAN(FILTER(" in timing and "*24" in timing
    assert "'Applications'!" in timing and "'Jobs_Master'!" in timing
    assert ">=" in timing and "<=NOW()" in timing and "NO RECORDED APPLICATIONS" in timing
    assert "^(incremental|full)$" in metrics["Last successful scan"]
    assert '" IST"' in metrics["Last successful scan"]
    assert "NO JOB DATA" in metrics["Compensation-known rate"]
    target = metrics["Source-backed target-band opportunities"]
    for evidence in (
        "target_base_lpa",
        "*100000",
        '"INR"',
        "ANNUAL|YEAR",
        "OBSERVED|SOURCE_BACKED|EXACT",
        "TARGET NOT CONFIGURED",
    ):
        assert evidence in target
    combined = book.tabs["Dashboard"][ATTRIBUTION_START - 1]["metric"]
    assert combined.startswith("=ARRAYFORMULA(VSTACK(")
    for lineage in (
        "'_Job_Sources'!",
        "'Jobs_Master'!",
        "'Applications'!",
        "'Application_Events'!",
        "'Outreach'!",
        "'Companies'!",
        "'Sources'!",
    ):
        assert lineage in combined
    assert combined.count("NO APPLICATIONS") >= 3 and combined.count("NO SENT OUTREACH") >= 3
    assert "applications*" in combined and "XLOOKUP" in combined
    assert "outgoing,IFERROR(QUERY(" in combined
    assert "REGEXMATCH" in metrics["Referral asks"] and "UNIQUE" in metrics["Referral asks"]
    assert "FALSE" in combined and "UNATTRIBUTED" in combined


def test_charts_have_exact_table_ranges_and_rebootstrap_preserves_user_chart() -> None:
    http = SheetsHTTP()
    google = GoogleSheetsWorkbook("synthetic", http, sleeper=lambda _: None)
    google.bootstrap()
    assert len(http.charts) == 3
    assert google.write_requests <= 20
    managed = list(http.charts.values())
    assert all(chart["spec"]["title"].startswith(CHART_PREFIX) for chart in managed)
    expected = {FUNNEL_START - 1, FRESHNESS_START - 1, ROLE_START - 1}
    actual = {
        chart["spec"]["basicChart"]["domains"][0]["domain"]["sourceRange"]["sources"][0][
            "startRowIndex"
        ]
        for chart in managed
    }
    assert actual == expected
    for chart in managed:
        basic = chart["spec"]["basicChart"]
        assert basic["headerCount"] == 1 and basic["chartType"] == "COLUMN"
        assert basic["series"][0]["series"]["sourceRange"]["sources"][0]["startColumnIndex"] == 1
        assert "no recorded data" in chart["spec"]["subtitle"]
    manual = copy.deepcopy(managed[0])
    manual["chartId"] = 9999
    manual["spec"]["title"] = "User's own chart"
    http.charts[9999] = manual
    google.bootstrap()
    assert len(http.charts) == 4 and http.charts[9999] == manual
    assert (
        sum(chart["spec"]["title"].startswith(CHART_PREFIX) for chart in http.charts.values()) == 3
    )
    for _, _, request in http.calls:
        if "requests" in request.get("json", {}):
            assert len(request["json"]["requests"]) <= 250
    assert any(
        "deleteEmbeddedObject" in request
        for _, _, payload in http.calls
        for request in payload.get("json", {}).get("requests", [])
    )
    formats = [
        request["repeatCell"]
        for request in dashboard_chart_requests(http.sheets["Dashboard"])
        if "repeatCell" in request
    ]
    assert any(
        rule["cell"]["userEnteredFormat"].get("numberFormat", {}).get("type") == "PERCENT"
        for rule in formats
    )
