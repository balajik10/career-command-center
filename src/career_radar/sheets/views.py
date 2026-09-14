"""Protected decision views backed only by canonical rows."""

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from .schema import SCHEMA, column_letter
from .workbook import BaseWorkbook, Cell, encode_cell

INTERVIEW_STAGES = ("RECRUITER_SCREEN", "TECH_SCREEN", "HM_ROUND", "INTERVIEW_LOOP")
REPLY_STATES = ("REPLIED", "RECEIVED", "POSITIVE", "NEGATIVE")
FUNNEL_START = 60
FRESHNESS_START = 85
ROLE_START = 105
ATTRIBUTION_START = 130
CHART_PREFIX = "Career Command Center — "


def _col(tab: str, field: str) -> str:
    return column_letter(SCHEMA[tab].columns.index(field) + 1)


def _range(tab: str, field: str) -> str:
    column = _col(tab, field)
    return f"'{tab}'!{column}2:{column}6000"


def _instant_formula(value_range: str) -> str:
    return f"IFERROR(DATEVALUE(LEFT({value_range},10))+TIMEVALUE(MID({value_range},12,8))+TIME(5,30,0),0)"


def _ever_stage_formula(stages: tuple[str, ...]) -> str:
    job_ids = _range("Jobs_Master", "job_uid")
    current = f'IFNA(XLOOKUP({job_ids},{_range("Applications", "job_uid")},{_range("Applications", "application_stage")}),"")'
    previous = "+".join(
        f'COUNTIFS({_range("Application_Events", "job_uid")},{job_ids},{_range("Application_Events", "to_stage")},"{stage}")'
        for stage in stages
    )
    return f'N((N(REGEXMATCH({current},"^({"|".join(stages)})$"))+{previous})>0)'


def _application_flags() -> str:
    times = _instant_formula(
        f'IFNA(XLOOKUP({_range("Jobs_Master", "job_uid")},{_range("Applications", "job_uid")},{_range("Applications", "applied_at")}),"")'
    )
    return f"N({times}>0)*N({times}<=NOW())"


def _referral_count(stage: str) -> str:
    current_jobs = _range("Applications", "job_uid")
    event_jobs = _range("Application_Events", "job_uid")
    pattern = f'"referral_stage_update"\\s*:\\s*"{stage}"'.replace('"', '""')
    return f'=IFERROR(ROWS(UNIQUE(FILTER(VSTACK({current_jobs},{event_jobs}),VSTACK({_range("Applications", "referral_stage")}="{stage}",REGEXMATCH({_range("Application_Events", "details")},"{pattern}")),VSTACK({current_jobs},{event_jobs})<>""))),0)'


def _cohort_formula(dimension: str) -> str:
    """One row per job prevents duplicate source appearances inflating rates."""
    jobs = _range("Jobs_Master", "job_uid")
    if dimension == "source":
        source = f'IFNA(XLOOKUP({jobs},{_range("_Job_Sources", "job_uid")},{_range("_Job_Sources", "source_id")}),"")'
        cohort = f'IF({source}="","UNATTRIBUTED",{source})'
    elif dimension == "role":
        role = _range("Jobs_Master", "role_family")
        cohort = f'IF({role}="","UNKNOWN",{role})'
    elif dimension == "tier":
        tier = f'IFNA(XLOOKUP({_range("Jobs_Master", "company")},{_range("Companies", "company")},{_range("Companies", "company_tier")}),"")'
        cohort = f'IF({tier}="","UNASSIGNED","Tier "&{tier})'
    else:
        raise ValueError("UNKNOWN_COHORT_DIMENSION")
    sent_date = _instant_formula(_range("Outreach", "sent_at"))
    sent = f"N({sent_date}>0)*N({sent_date}<=NOW())"
    reply = f'N((N({_range("Outreach", "outreach_stage")}="REPLIED")+N(REGEXMATCH(UPPER({_range("Outreach", "reply_state")}),"^({"|".join(REPLY_STATES)})$")))>0)'
    outgoing = f"QUERY(HSTACK({_range('Outreach', 'job_uid')},{sent},{sent}*{reply}),\"select Col1,sum(Col2),sum(Col3) where Col1 is not null group by Col1 label Col1 '',sum(Col2) '',sum(Col3) ''\",0)"
    # LET binds expressions once; IFNA turns a job with no outreach into zero
    # sent/reply counts. Zero-denominator rates stay explicit text, not 0%.
    return (
        f"IFERROR(LET(jobids,{jobs},cohort,{cohort},applications,{_application_flags()},"
        f"interviews,applications*{_ever_stage_formula(INTERVIEW_STAGES)},"
        f'outgoing,IFERROR({outgoing},HSTACK("",0,0)),sentcounts,IFNA(VLOOKUP(jobids,outgoing,2,FALSE),0),'
        "replycounts,IFNA(VLOOKUP(jobids,outgoing,3,FALSE),0),"
        'population,FILTER(HSTACK(cohort,applications,interviews,sentcounts,replycounts),jobids<>""),'
        "grouped,QUERY(population,\"select Col1,sum(Col2),sum(Col3),sum(Col4),sum(Col5) group by Col1 order by sum(Col2) desc,Col1 label Col1 '',sum(Col2) '',sum(Col3) '',sum(Col4) '',sum(Col5) ''\",0),"
        'groups,FILTER(grouped,INDEX(grouped,,1)<>""),'
        "HSTACK(INDEX(groups,,1),INDEX(groups,,2),INDEX(groups,,3),"
        'IF(INDEX(groups,,2)>0,INDEX(groups,,3)/INDEX(groups,,2),"NO APPLICATIONS"),'
        "INDEX(groups,,4),INDEX(groups,,5),"
        'IF(INDEX(groups,,4)>0,INDEX(groups,,5)/INDEX(groups,,4),"NO SENT OUTREACH"))),'
        'HSTACK("NO COHORT DATA",0,0,"NO APPLICATIONS",0,0,"NO SENT OUTREACH"))'
    )


def _advanced_dashboard_cells() -> list[Cell]:
    application_ids = _range("Applications", "job_uid")
    applications = _application_flags()
    applied = _instant_formula(_range("Applications", "applied_at"))
    joined_first = _instant_formula(
        f'IFNA(XLOOKUP({application_ids},{_range("Jobs_Master", "job_uid")},{_range("Jobs_Master", "first_seen_at")}),"")'
    )
    target = (
        f'IFNA(XLOOKUP("target_base_lpa",{_range("Config", "key")},{_range("Config", "value")}),"")'
    )
    salary = _range("Jobs_Master", "salary_base_min")
    source_success = _instant_formula(_range("Sources", "last_success_at"))
    enabled = _range("Sources", "enabled")
    metrics = [
        (
            "Median time to apply (hours)",
            f'=IFERROR(MEDIAN(FILTER(({applied}-{joined_first})*24,{application_ids}<>"",{applied}>0,{joined_first}>0,{applied}>={joined_first},{applied}<=NOW())),"NO RECORDED APPLICATIONS")',
            "All-time median from first observation to manual applied time; missing, future and negative intervals excluded.",
        ),
        (
            "Maximum target coverage interval (hours)",
            f'=IFERROR(MAX(FILTER({_range("Sources", "cadence_hours")},{enabled}=TRUE)),"NO ENABLED SOURCES")',
            "Largest configured cadence among enabled sources; a target, not a collection guarantee.",
        ),
        (
            "Maximum observed source age (hours)",
            f'=IFERROR(MAX(FILTER((NOW()-{source_success})*24,{enabled}=TRUE,{source_success}>0,{source_success}<=NOW())),"NO SUCCESSFUL SOURCE CHECK")',
            "Elapsed since the oldest last complete successful check; includes backoff and downtime.",
        ),
        (
            "Source-backed target-band opportunities",
            f'=IF(OR({target}="",NOT(ISNUMBER({target})),{target}<=0),"TARGET NOT CONFIGURED",SUMPRODUCT(N({_range("Jobs_Master", "job_uid")}<>""),N({salary}<>""),N({salary}>={target}*100000),N({_range("Jobs_Master", "salary_currency")}="INR"),N(REGEXMATCH(UPPER({_range("Jobs_Master", "salary_period")}),"^(ANNUAL|YEAR)$")),N(REGEXMATCH({_range("Jobs_Master", "salary_confidence")},"^(OBSERVED|SOURCE_BACKED|EXACT)$")),N({_range("Jobs_Master", "salary_source")}<>""),N({_range("Jobs_Master", "active_state")}<>"CLOSED"),N({_range("Jobs_Master", "action_priority")}<>"EXCLUDED")))',
            "Known INR annual base minimum reaches the private target; source evidence required. Never a hard eligibility filter.",
        ),
        (
            "Attribution and denominator policy",
            "First recorded source appearance; current company tier; canonical jobs counted once.",
            "Interview rate = applied jobs with an ever-recorded interview stage / applied jobs. Reply rate = replied drafts with sent times / sent drafts. These are descriptive, not causal, rates.",
        ),
        (
            "Dashboard data limit",
            "Canonical rows 2–6000; archive/review before exceeding the view capacity.",
            "Empty data is labeled. A skipped interview stage is never invented. Corrections remain visible in immutable event history.",
        ),
    ]
    cells: list[Cell] = []
    for index, (label, value, description) in enumerate(metrics, 44):
        cells.extend(
            [
                ("Dashboard", index, 1, label),
                ("Dashboard", index, 2, value),
                ("Dashboard", index, 3, description),
            ]
        )
    funnel = (
        ("Applications", applications),
        ("Recorded interviews", f"{applications}*{_ever_stage_formula(INTERVIEW_STAGES)}"),
        (
            "Recorded offers",
            f"{applications}*{_ever_stage_formula(('OFFER', 'ACCEPTED', 'DECLINED'))}",
        ),
        ("Accepted", f"{applications}*{_ever_stage_formula(('ACCEPTED',))}"),
    )
    cells.extend(
        [
            ("Dashboard", FUNNEL_START, 1, "Recorded-stage funnel"),
            ("Dashboard", FUNNEL_START, 2, "Canonical jobs"),
            ("Dashboard", FUNNEL_START, 3, "Share of applications"),
        ]
    )
    for row, (label, flags) in enumerate(funnel, FUNNEL_START + 1):
        cells.extend(
            [
                ("Dashboard", row, 1, label),
                ("Dashboard", row, 2, f"=SUMPRODUCT({flags})"),
                (
                    "Dashboard",
                    row,
                    3,
                    f'=IF(B{FUNNEL_START + 1}>0,B{row}/B{FUNNEL_START + 1},"NO APPLICATIONS")',
                ),
            ]
        )
    cells.extend(
        [
            ("Dashboard", 70, 1, "Current interview/application stages"),
            ("Dashboard", 70, 2, "Current applications"),
        ]
    )
    for row, stage in enumerate(("OA", *INTERVIEW_STAGES, "OFFER", "ACCEPTED"), 71):
        cells.extend(
            [
                ("Dashboard", row, 1, stage),
                (
                    "Dashboard",
                    row,
                    2,
                    f'=COUNTIF({_range("Applications", "application_stage")},"{stage}")',
                ),
            ]
        )
    for row, field, title, suffix in (
        (FRESHNESS_START, "freshness_band", "Freshness distribution", ""),
        (ROLE_START, "role_family", "Top role families", " limit 10"),
    ):
        field_range = _range("Jobs_Master", field)
        cells.extend(
            [
                ("Dashboard", row, 1, title),
                ("Dashboard", row, 2, "Canonical jobs"),
                (
                    "Dashboard",
                    row + 1,
                    1,
                    f'=IFERROR(QUERY(FILTER(HSTACK(IF({field_range}="","UNKNOWN",{field_range}),{_range("Jobs_Master", "job_uid")}),{_range("Jobs_Master", "job_uid")}<>""),"select Col1,count(Col2) group by Col1 order by count(Col2) desc{suffix} label Col1 \'\',count(Col2) \'\'",0),HSTACK("NO JOB DATA",""))',
                ),
            ]
        )
    headings = (
        "Source cohort (first recorded)",
        "Applied jobs",
        "Interviewed jobs",
        "Interview rate",
        "Sent drafts",
        "Replied drafts",
        "Reply rate",
    )
    for column, heading in enumerate(headings, 1):
        cells.append(("Dashboard", ATTRIBUTION_START, column, heading))

    def other_headings(title: str) -> str:
        return 'HSTACK("' + '","'.join((title, *headings[1:])) + '")'

    blank = 'HSTACK("","","","","","","")'
    coverage = f'IFERROR(FILTER(HSTACK({_range("Sources", "source_id")},{_range("Sources", "cadence_hours")},IF({source_success}>0,ROUND((NOW()-{source_success})*24,1),"NOT RUN"),{_range("Sources", "health_state")},{_range("Sources", "policy_state")},{_range("Sources", "coverage_state")},IF({enabled}=TRUE,"ENABLED","PAUSED")),{_range("Sources", "source_id")}<>""),HSTACK("NO SOURCE DATA","","","","","",""))'
    expression = f'=ARRAYFORMULA(VSTACK({_cohort_formula("source")},{blank},{other_headings("Role cohort")},{_cohort_formula("role")},{blank},{other_headings("Company tier cohort (user assigned)")},{_cohort_formula("tier")},{blank},HSTACK("Source coverage","Target hours","Hours since success","Health","Policy","Coverage","Enabled"),{coverage}))'
    cells.append(("Dashboard", ATTRIBUTION_START + 1, 1, expression))
    return cells


def dashboard_chart_requests(sheet_id: int) -> list[dict[str, Any]]:
    """Small overview charts; empty source tables contain no fake numeric points."""
    result: list[dict[str, Any]] = []
    for title, start, end, anchor in (
        ("Recorded-stage funnel", FUNNEL_START - 1, FUNNEL_START + 4, 1),
        ("Freshness distribution", FRESHNESS_START - 1, FRESHNESS_START + 13, 18),
        ("Top role families", ROLE_START - 1, ROLE_START + 11, 35),
    ):
        domain = {
            "sheetId": sheet_id,
            "startRowIndex": start,
            "endRowIndex": end,
            "startColumnIndex": 0,
            "endColumnIndex": 1,
        }
        series = {**domain, "startColumnIndex": 1, "endColumnIndex": 2}
        result.append(
            {
                "addChart": {
                    "chart": {
                        "spec": {
                            "title": CHART_PREFIX + title,
                            "subtitle": "Canonical records; an empty series means no recorded data",
                            "basicChart": {
                                "chartType": "COLUMN",
                                "legendPosition": "NO_LEGEND",
                                "headerCount": 1,
                                "axis": [
                                    {"position": "BOTTOM_AXIS", "title": "Recorded category"},
                                    {"position": "LEFT_AXIS", "title": "Jobs"},
                                ],
                                "domains": [{"domain": {"sourceRange": {"sources": [domain]}}}],
                                "series": [
                                    {
                                        "series": {"sourceRange": {"sources": [series]}},
                                        "targetAxis": "LEFT_AXIS",
                                    }
                                ],
                            },
                        },
                        "position": {
                            "overlayPosition": {
                                "anchorCell": {
                                    "sheetId": sheet_id,
                                    "rowIndex": anchor,
                                    "columnIndex": 8,
                                },
                                "widthPixels": 580,
                                "heightPixels": 290,
                            }
                        },
                    }
                }
            }
        )
    result.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": FUNNEL_START,
                    "endRowIndex": FUNNEL_START + 4,
                    "startColumnIndex": 2,
                    "endColumnIndex": 3,
                },
                "cell": {
                    "userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        }
    )
    for column in (3, 6):
        result.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": ATTRIBUTION_START,
                        "endRowIndex": 6000,
                        "startColumnIndex": column,
                        "endColumnIndex": column + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {"type": "PERCENT", "pattern": "0.0%"}
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
        )
    result.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 13,
                    "endRowIndex": 14,
                    "startColumnIndex": 1,
                    "endColumnIndex": 2,
                },
                "cell": {
                    "userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        }
    )
    for row in (FUNNEL_START, 70, FRESHNESS_START, ROLE_START, ATTRIBUTION_START):
        result.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row - 1,
                        "endRowIndex": row,
                        "startColumnIndex": 0,
                        "endColumnIndex": 7,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"bold": True},
                            "backgroundColor": {"red": 0.88, "green": 0.92, "blue": 0.96},
                        }
                    },
                    "fields": "userEnteredFormat.textFormat,userEnteredFormat.backgroundColor",
                }
            }
        )
    return result


def dashboard_reference(
    tabs: Mapping[str, Sequence[Mapping[str, Any]]], now: datetime
) -> dict[str, Any]:
    """Offline numerical reference for metric audits and private backup analysis.

    This reads canonical snapshots, never cached Dashboard or Audit output. It
    shares the published denominator rules with the live formulas and provides
    reproducible numbers when Google formula evaluation is unavailable offline.
    """
    if now.tzinfo is None:
        raise ValueError("AWARE_TIMESTAMP_REQUIRED")
    job_rows = [row for row in tabs.get("Jobs_Master", []) if row.get("job_uid")]
    jobs = {str(row["job_uid"]): row for row in job_rows}
    if len(jobs) != len(job_rows):
        raise ValueError("DUPLICATE_CANONICAL_JOB_UID")
    applications = {
        str(row["job_uid"]): row for row in tabs.get("Applications", []) if row.get("job_uid")
    }
    sources: dict[str, str] = {}
    for row in tabs.get("_Job_Sources", []):
        sources.setdefault(str(row.get("job_uid", "")), str(row.get("source_id") or "UNATTRIBUTED"))
    tiers = {
        str(row.get("company", "")).casefold(): row.get("company_tier", "")
        for row in tabs.get("Companies", [])
    }
    history: defaultdict[str, set[str]] = defaultdict(set)
    for row in tabs.get("Application_Events", []):
        history[str(row.get("job_uid", ""))].add(str(row.get("to_stage", "")))
    sent: Counter[str] = Counter()
    replied: Counter[str] = Counter()
    for row in tabs.get("Outreach", []):
        timestamp = _reference_time(row.get("sent_at"))
        if timestamp and timestamp <= now:
            identity = str(row.get("job_uid", ""))
            sent[identity] += 1
            if (
                row.get("outreach_stage") == "REPLIED"
                or str(row.get("reply_state", "")).upper() in REPLY_STATES
            ):
                replied[identity] += 1
    cohorts: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in ("source", "role", "tier")}
    elapsed: list[float] = []
    funnel = {key: 0 for key in ("applications", "interviews", "offers", "accepted")}
    for identity, row in jobs.items():
        app = applications.get(identity, {})
        applied_at = _reference_time(app.get("applied_at"))
        first_seen = _reference_time(row.get("first_seen_at"))
        applied = bool(applied_at and applied_at <= now)
        stages = history[identity] | {str(app.get("application_stage", ""))}
        interviews = applied and bool(stages & set(INTERVIEW_STAGES))
        funnel["applications"] += applied
        funnel["interviews"] += interviews
        funnel["offers"] += applied and bool(stages & {"OFFER", "ACCEPTED", "DECLINED"})
        funnel["accepted"] += applied and "ACCEPTED" in stages
        if applied and first_seen and applied_at and applied_at >= first_seen:
            elapsed.append((applied_at - first_seen).total_seconds() / 3600)
        tier = tiers.get(str(row.get("company", "")).casefold(), "")
        labels = {
            "source": sources.get(identity, "UNATTRIBUTED"),
            "role": str(row.get("role_family") or "UNKNOWN"),
            "tier": f"Tier {tier}" if tier != "" else "UNASSIGNED",
        }
        for dimension, label in labels.items():
            values = cohorts[dimension].setdefault(
                label, {"applications": 0, "interviews": 0, "sent": 0, "replied": 0}
            )
            values["applications"] += applied
            values["interviews"] += interviews
            values["sent"] += sent[identity]
            values["replied"] += replied[identity]
    for groups in cohorts.values():
        for values in groups.values():
            values["interview_rate"] = (
                values["interviews"] / values["applications"] if values["applications"] else None
            )
            values["reply_rate"] = values["replied"] / values["sent"] if values["sent"] else None
    return {
        "median_apply_hours": median(elapsed) if elapsed else None,
        "median_samples": len(elapsed),
        "funnel": funnel,
        "cohorts": cohorts,
        "freshness": dict(
            Counter(str(row.get("freshness_band") or "UNKNOWN") for row in jobs.values())
        ),
        "role_families": dict(
            Counter(str(row.get("role_family") or "UNKNOWN") for row in jobs.values()).most_common(
                10
            )
        ),
    }


def _reference_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        timestamp = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except ValueError:
        return None
    return timestamp if timestamp.tzinfo is not None else None


def install_formulas(workbook: BaseWorkbook, sheet_ids: Mapping[str, int]) -> None:
    first = _instant_formula(_range("Jobs_Master", "first_seen_at"))
    posted = _instant_formula(_range("Jobs_Master", "posted_at_source"))
    priority = _range("Jobs_Master", "action_priority")
    job_uid = _range("Jobs_Master", "job_uid")
    applied = _instant_formula(_range("Applications", "applied_at"))
    stage = _range("Applications", "application_stage")
    known_compensation = "+".join(
        f"N(ISNUMBER({_range('Jobs_Master', field)}))"
        for field in ("salary_base_min", "salary_base_max", "salary_total_min", "salary_total_max")
    )
    metric_formulas = [
        ("New today", f"=SUMPRODUCT(N({first}>=TODAY()),N({first}<=NOW()))", "Observed today IST"),
        (
            "New this week",
            f"=SUMPRODUCT(N({first}>=TODAY()-WEEKDAY(TODAY(),2)+1),N({first}<=NOW()))",
            "Monday 00:00 IST through now",
        ),
        (
            "New this month",
            f"=SUMPRODUCT(N({first}>=EOMONTH(TODAY(),-1)+1),N({first}<=NOW()))",
            "Current calendar month IST",
        ),
        ("P0 awaiting action", f'=COUNTIF({priority},"P0")', "Protected Action Queue"),
        ("P1 awaiting action", f'=COUNTIF({priority},"P1")', "Protected Action Queue"),
        ("Overdue actions", '=COUNTIF(Action_Queue!C2:C6000,"OVERDUE")', "Unfinished only"),
        (
            "Applications this week",
            f"=SUMPRODUCT(N({applied}>=TODAY()-WEEKDAY(TODAY(),2)+1),N({applied}<=NOW()))",
            "Manual application state",
        ),
        (
            "Applications this month",
            f"=SUMPRODUCT(N({applied}>=EOMONTH(TODAY(),-1)+1),N({applied}<=NOW()))",
            "Manual application state",
        ),
        (
            "Offers (current stage)",
            f'=COUNTIF({stage},"OFFER")',
            "The recorded-stage funnel below also preserves accepted/declined offers.",
        ),
        (
            "Referrals obtained",
            _referral_count("REFERRED"),
            "Distinct jobs with user-confirmed current or historical referrals; never inferred from a draft.",
        ),
        (
            "Referral asks",
            _referral_count("ASK_SENT"),
            "Distinct jobs with an explicitly recorded ask; later stages do not erase the milestone.",
        ),
        (
            "Outreach replies",
            f'=COUNTIF({_range("Outreach", "outreach_stage")},"REPLIED")',
            "Manual replies",
        ),
        (
            "Compensation-known rate",
            f'=IFERROR(SUMPRODUCT(N({job_uid}<>""),N(({known_compensation})>0),N({_range("Jobs_Master", "salary_source")}<>""))/COUNTA({job_uid}),"NO JOB DATA")',
            "Canonical jobs with a known base/total amount and an evidence source; no guessed pay.",
        ),
        (
            "Last successful scan",
            f'=IFERROR(TEXT(MAX(FILTER({_instant_formula(_range("Run_Log", "completed_at"))},{_range("Run_Log", "status")}="COMMITTED",REGEXMATCH({_range("Run_Log", "mode")},"^(incremental|full)$"))),"yyyy-mm-dd hh:mm")&" IST","Not run")',
            "Latest committed discovery scan, excluding bootstrap/doctor/digest runs; IST",
        ),
        ("Policy bypass attempts", "=0", "No bypass implementation permitted"),
    ]
    for field, states in (
        (
            "policy_state",
            (
                "APPROVED",
                "PENDING_REVIEW",
                "MANUAL_ONLY",
                "LOGIN_REQUIRED",
                "POLICY_BLOCKED",
                "DISABLED",
            ),
        ),
        (
            "coverage_state",
            ("UNVALIDATED", "VERIFIED_ACTIVE", "VERIFIED_EMPTY", "NATIVE_ALERT_ONLY"),
        ),
        (
            "health_state",
            (
                "UNKNOWN",
                "HEALTHY",
                "DEGRADED",
                "BACKOFF",
                "STALE",
                "DOWN",
                "AUTH_REQUIRED",
                "PAUSED",
            ),
        ),
    ):
        metric_formulas.extend(
            (
                f"{field}: {state}",
                f'=COUNTIF({_range("Sources", field)},"{state}")',
                "Independent source dimension",
            )
            for state in states
        )
    cells: list[Cell] = _advanced_dashboard_cells()
    for index, (label, formula, note) in enumerate(metric_formulas, 2):
        cells.extend(
            [
                ("Dashboard", index, 1, label),
                ("Dashboard", index, 2, formula),
                ("Dashboard", index, 3, note),
            ]
        )
    instructions = [
        (
            "Five-minute routine",
            "Open Action Queue → apply to P0/P1 → review permitted outreach → record status in a NEW Action Updates row → handle follow-ups.",
        ),
        (
            "Legend",
            "Pale yellow cells accept user input. Gray/system cells and all sorted views are protected. Dates explicitly use IST.",
        ),
        (
            "Action Updates",
            "Fill a new blank row, select the stable job_uid, enter the update and reason, then check submit LAST. Submitted payload and row UID are immutable; changes are rejected for manual review.",
        ),
        (
            "Ownership",
            "Jobs Master owns job facts; Applications owns workflow; Contacts owns contact facts; Outreach owns drafts. Discovery never changes user workflow.",
        ),
        (
            "Freshness",
            "POSTED DATE UNKNOWN means only first-seen evidence exists. Baseline imported jobs do not imply new publication.",
        ),
        (
            "Recovery",
            "SENDING or AMBIGUOUS emails are never automatically retried. Review the outbox and use a deliberate manual retry only after checking delivery.",
        ),
        (
            "Privacy",
            "The workbook is private. Never publish its URL or data. Channel permissions are manual; no third-party message is sent.",
        ),
    ]
    for index, (section, instruction) in enumerate(instructions, 2):
        cells.extend([("START_HERE", index, 1, section), ("START_HERE", index, 2, instruction)])
    selected = [
        "job_uid",
        "company",
        "title",
        "action_priority",
        "fit_score",
        "apply_url",
        "first_seen_at",
        "posted_at_source",
        "freshness_basis",
        "next_action",
        "application_stage",
        "referral_stage",
        "salary_confidence",
    ]
    projection = (
        "HSTACK("
        + ",".join(_range("Jobs_Master", field) for field in selected)
        + f',IF({job_uid}<>"",HYPERLINK("#gid={sheet_ids["Jobs_Master"]}&range=A"&ROW({job_uid}),"Canonical row"),""))'
    )
    last = column_letter(len(SCHEMA["Action_Queue"].columns))
    for name, condition in (
        ("Today", f"(({first}>=TODAY())*({first}<=NOW())+({posted}>=TODAY())*({posted}<=NOW()))>0"),
        ("This_Week", f"({first}>=TODAY()-WEEKDAY(TODAY(),2)+1)*({first}<=NOW())"),
        ("This_Month", f"({first}>=EOMONTH(TODAY(),-1)+1)*({first}<=NOW())"),
    ):
        cells.append(
            (
                name,
                2,
                1,
                f'=IFERROR(SORT(FILTER({projection},{job_uid}<>"",{condition}),4,TRUE,7,FALSE),"No matching jobs")',
            )
        )
    queue_range = f"Action_Queue!A2:{last}6000"
    # Independent spill sections leave room for up to 999 records each.
    for row, label, condition in (
        (1003, "ACTIONS DUE TODAY", 'LEFT(Action_Queue!D2:D6000,10)=TEXT(TODAY(),"yyyy-mm-dd")'),
        (2005, "EVERY OVERDUE ACTION", 'Action_Queue!C2:C6000="OVERDUE"'),
        (3007, "P0/P1 NOT YET APPLIED", 'REGEXMATCH(Action_Queue!B2:B6000,"^P[01]$")'),
        (4009, "FOLLOW-UPS DUE TODAY", 'REGEXMATCH(Action_Queue!J2:J6000,"(?i)follow.up")'),
    ):
        cells.extend(
            [
                ("Today", row, 1, label),
                (
                    "Today",
                    row + 1,
                    1,
                    f'=IFERROR(FILTER({queue_range},{condition}),"No matching actions")',
                ),
            ]
        )
    workbook.write_cells(cells, formulas=True)


def refresh_action_queue(
    workbook: BaseWorkbook, run_uid: str, *, sheet_url: str = "", now: datetime | None = None
) -> int:
    now = (now or datetime.now(UTC)).astimezone(ZoneInfo("Asia/Kolkata"))
    data = workbook.read_tabs(
        ["Jobs_Master", "Applications", "Outreach", "Action_Queue", "_System_State"]
    )
    state = {row["key"]: row.get("value") for row in data["_System_State"]}
    jobs_gid = state.get("sheet_id:Jobs_Master", "")
    updates_gid = state.get("sheet_id:Action_Updates", "")
    applications = {row["job_uid"]: row for row in data["Applications"]}
    drafts = {row["job_uid"]: row for row in data["Outreach"]}
    rows: list[dict[str, Any]] = []
    for job_position, job in enumerate(data["Jobs_Master"], 2):
        app = applications.get(job["job_uid"], {})
        draft = drafts.get(job["job_uid"], {})
        stage = app.get("application_stage", "NOT_STARTED")
        if stage in {
            "ACCEPTED",
            "DECLINED",
            "REJECTED",
            "WITHDRAWN",
            "ROLE_CLOSED",
            "SKIPPED",
        } or job.get("action_priority") in {"EXCLUDED", "P3"}:
            continue
        due = str(
            app.get("next_step_due") or draft.get("follow_up_due") or job.get("action_by_ist", "")
        )
        overdue = "OVERDUE" if due and due[:16] < now.strftime("%Y-%m-%d %H:%M") else ""
        rows.append(
            {
                "job_uid": job["job_uid"],
                "action_priority": job.get("action_priority", "P2"),
                "overdue": overdue,
                "action_by_ist": due,
                "company": job.get("company", ""),
                "title": job.get("title", ""),
                "posted_at_source": job.get("posted_at_source") or "POSTED DATE UNKNOWN",
                "first_seen_at": job.get("first_seen_at", ""),
                "fit_score": job.get("fit_score", ""),
                "next_action": app.get("next_step") or job.get("next_action", ""),
                "apply_url": job.get("apply_url", ""),
                "best_contact": job.get("best_contact", "No confirmed contact yet"),
                "application_stage": stage,
                "referral_stage": app.get("referral_stage", "NOT_STARTED"),
                "outreach_stage": draft.get("outreach_stage", ""),
                "canonical_link": f"{sheet_url.split('#')[0]}#gid={jobs_gid}&range=A{job_position}",
                "update_link": f"{sheet_url.split('#')[0]}#gid={updates_gid}",
                "freshness_basis": job.get("freshness_basis", ""),
                "salary_state": job.get("salary_confidence", "MISSING"),
            }
        )
    rows.sort(
        key=lambda row: (
            row["action_priority"],
            row["overdue"] != "OVERDUE",
            row["action_by_ist"],
            row["job_uid"],
        )
    )
    # This is the protected materialized queue; user input never shares its rows.
    cells = [
        ("Action_Queue", index + 2, col + 1, encode_cell(row.get(field, "")))
        for index, row in enumerate(rows)
        for col, field in enumerate(SCHEMA["Action_Queue"].columns)
    ]
    cells += [
        ("Action_Queue", index + 2, col + 1, "")
        for index in range(len(rows), len(data["Action_Queue"]))
        for col in range(len(SCHEMA["Action_Queue"].columns))
    ]
    workbook.require_schema()
    workbook.write_cells(cells)
    return len(rows)
