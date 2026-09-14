import json
from datetime import UTC, datetime, timedelta

import pytest

from career_radar.dedupe import (
    DedupeConfig,
    comparable_conflict,
    mark_missing,
    match_reason,
    upsert_job,
)
from career_radar.domain import CandidateEvidence, CandidateProfile, JobIdentity, RawJob
from career_radar.normalize import (
    classify_role,
    detect_skills,
    freshness_band,
    month_bounds,
    normalize_company,
    normalize_job,
    parse_experience,
    parse_posted_date,
    parse_salary,
)
from career_radar.scoring import (
    ScoringConfig,
    assign_priority,
    compensation_signal,
    experience_fit,
    score_job,
    select_evidence,
    tenure_range,
)
from career_radar.strategy import build_brief, ist, referral_plan

NOW = datetime(2026, 8, 15, 8, tzinfo=UTC)


def profile() -> CandidateProfile:
    return CandidateProfile(
        full_time_start="2024-01",
        skills=["Java", "Spring Boot", "Redis"],
        evidence=[
            CandidateEvidence(
                evidence_id="api",
                outreach_safe_paraphrase="Built an API.",
                allowed_atomic_claims=["Built a Java API and tested database constraints."],
                technologies=["Java"],
                tags=["api"],
                allowed_in_outreach=True,
            ),
            CandidateEvidence(
                evidence_id="cache",
                outreach_safe_paraphrase="Tested a cache.",
                allowed_atomic_claims=["Tested Redis cache failures."],
                technologies=["Redis"],
                allowed_in_outreach=True,
            ),
        ],
    )


def job(**kwargs: object):  # type: ignore[no-untyped-def]
    values = {
        "source_id": "fixture",
        "provider": "greenhouse",
        "tenant": "example",
        "provider_job_id": "1",
        "title": "Backend Engineer",
        "company": "Example Labs",
        "location": "Bengaluru, India",
        "description": "Develop Java and Redis APIs. Required 0-2 years of software experience.",
        "url": "https://example.com/jobs/1",
        "apply_url": "https://example.com/jobs/1/apply",
        "posted_at_raw": (NOW - timedelta(hours=2)).isoformat(),
        "fetched_at": NOW,
        "official_link_state": "VERIFIED_OFFICIAL",
    }
    values.update(kwargs)
    return normalize_job(RawJob.model_validate(values), NOW)


@pytest.mark.parametrize(
    "title",
    [
        "SDE-I",
        "SDE 1",
        "SDE I",
        "SWE I",
        "Software Engineer-I",
        "Software Engineer 1",
        "Associate Software Engineer",
        "Junior Software Engineer",
        "Graduate Software Engineer",
        "Backend Developer",
        "Java Backend Engineer",
        "Java Developer",
        "Java Engineer",
        "Spring Boot Engineer",
    ],
)
@pytest.mark.parametrize("experience", ["0-2", "1+", "2+", "2-4", "2-5"])
def test_retained_title_experience_matrix(title: str, experience: str) -> None:
    item = job(
        title=title,
        description=f"Develop Java and Redis APIs. Required {experience} years of software experience.",
    )
    result = score_job(item, profile(), NOW)
    assert result.action_priority != "EXCLUDED"
    early = profile().model_copy(update={"full_time_start": "2026-01"})
    if experience.startswith("2"):
        assert score_job(item, early, NOW).experience_fit == "NEAR_MATCH_IN_SCOPE"


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Lead Backend Engineer",
        "Staff Engineer",
        "Principal Engineer",
        "Engineering Manager",
        "Frontend Engineer",
        "Android Developer",
        "Sales Engineer",
        "Product Manager",
    ],
)
def test_out_of_scope_titles(title: str) -> None:
    assert classify_role(title)[0] == "EXCLUDED"


@pytest.mark.parametrize(
    ("title", "description", "minimum", "family", "seniority"),
    [
        ("Software Engineer II", "Develop APIs", 2, "SOFTWARE", "STRETCH"),
        ("SDE II", "Develop APIs", 3, "SOFTWARE", "STRETCH"),
        ("Software Engineer II", "Develop APIs", 4, "EXCLUDED", "LEVEL_II_UNCLEAR"),
        ("SDE II", "", None, "EXCLUDED", "LEVEL_II_UNCLEAR"),
        ("Cloud Engineer", "Run tickets", None, "PLATFORM", "REVIEW_SCOPE"),
        ("SRE", "Develop Python services", None, "RELIABILITY", "EARLY_CAREER"),
        ("Full-Stack Engineer", "Build backend APIs", None, "FULL_STACK", "EARLY_CAREER"),
        (
            "Distributed Systems Engineer",
            "Develop services",
            None,
            "DISTRIBUTED_SYSTEMS",
            "EARLY_CAREER",
        ),
        ("Mystery Position", "", None, "UNKNOWN", "UNKNOWN"),
        ("Software Engineer", "Lead a team of engineers", 1, "EXCLUDED", "SENIOR"),
    ],
)
def test_title_scope(
    title: str, description: str, minimum: float | None, family: str, seniority: str
) -> None:
    assert classify_role(title, description, minimum) == (family, seniority)


@pytest.mark.parametrize(
    ("text", "minimum", "maximum", "confidence"),
    [
        ("Requires 1+ years of software experience", 1, None, "PARSED"),
        ("Needs 0 to 3 years", 0, 3, "PARSED"),
        ("2–5 years of experience", 2, 5, "PARSED"),
        ("3 years of experience preferred", None, None, "MISSING"),
        ("2 years with Java", None, None, "MISSING"),
        ("2 years of experience in Redis", None, None, "MISSING"),
        ("4-2 years of software experience", None, None, "AMBIGUOUS"),
        ("No minimum specified", None, None, "MISSING"),
        ("1 year total experience; 2 years software experience", 2, None, "PARSED"),
    ],
)
def test_experience_parse(
    text: str, minimum: float | None, maximum: float | None, confidence: str
) -> None:
    result = parse_experience(text)
    assert (result.minimum, result.maximum, result.confidence) == (minimum, maximum, confidence)
    assert parse_experience(text + "; internships count").internship_allowed


def test_exact_skill_aliases_never_infer() -> None:
    assert detect_skills("JavaScript, Postgres, k8s, Spring-Boot") == [
        "JavaScript",
        "Kubernetes",
        "PostgreSQL",
        "Spring Boot",
    ]
    assert detect_skills("Java and Spring Framework") == ["Java", "Spring Framework"]
    assert not set(detect_skills("Java and Spring Boot")) & {
        "JPA",
        "Hibernate",
        "Kotlin",
        "Go",
        "C++",
    }
    assert normalize_company("Example Labs Inc") == "example labs"


@pytest.mark.parametrize(
    ("age", "band"),
    [
        (0, "HOT_0_6H"),
        (5.999, "HOT_0_6H"),
        (6, "EARLY_6_24H"),
        (24, "FRESH_1_3D"),
        (96, "OPEN_4_7D"),
        (192, "AGING_8_14D"),
        (360, "STALE_15D_PLUS"),
    ],
)
def test_age_boundaries(age: float, band: str) -> None:
    assert freshness_band(age, False) == band


def test_dates_conservative_and_ist_midnight() -> None:
    midnight = datetime(2026, 8, 15, 18, 30, tzinfo=UTC)
    exact = parse_posted_date("2026-08-16T00:00:00+05:30", midnight)
    assert exact.age_max == 0 and exact.confidence == "EXACT"
    date_only = parse_posted_date("2026-08-15", NOW, "Asia/Kolkata")
    assert date_only.age_min == 0 and date_only.age_max == 13.5
    assert freshness_band(date_only.age_max, True) == "EARLY_6_24H"
    relative = parse_posted_date("5 hours ago", NOW)
    assert relative.age_max == 6 and relative.age_min == 5
    assert parse_posted_date("1 day ago", NOW).age_max == 48
    assert parse_posted_date("1 week ago", NOW).age_max == 336
    assert parse_posted_date("Sat, 15 Aug 2026 06:00:00 +0000", NOW).age_max == 2
    assert parse_posted_date("Today", NOW, "Asia/Kolkata").age_max == 13.5
    assert parse_posted_date("Yesterday", NOW, "Asia/Kolkata").age_max == 37.5
    dst_now = datetime(2026, 11, 2, 6, tzinfo=UTC)
    dst = parse_posted_date("2026-11-01", dst_now, "America/New_York")
    assert dst.age_max == 26 and dst.age_min == 1
    assert parse_posted_date("2026-08-16T00:00:00Z", NOW).error == "FUTURE_POSTED_DATE"
    assert parse_posted_date("2026-08-16", NOW).error == "FUTURE_POSTED_DATE"
    assert parse_posted_date("not a date", NOW).error == "INVALID_POSTED_DATE"
    assert parse_posted_date("2026-08-14T12:00:00", NOW).error == "NAIVE_POSTED_DATE"
    assert parse_posted_date(None, NOW).confidence == "MISSING"
    assert freshness_band(None, True) == "BASELINE_DATE_UNKNOWN"
    assert freshness_band(None, False) == "OBSERVED_NEW_DATE_UNKNOWN"
    with pytest.raises(ValueError):
        parse_posted_date(None, datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        freshness_band(-1, False)


def test_normalization_provenance_and_metadata() -> None:
    item = job(
        description="<p>Develop Java. Redis preferred. Bachelor's degree. Visa sponsorship provided.</p>",
        posted_at_raw=None,
        updated_at_raw="2026-08-14T09:00:00Z",
        deadline_raw="2026-08-20T09:00:00Z",
        metadata={
            "salary": {
                "salary_base_min": 1_000_000,
                "salary_currency": "INR",
                "salary_confidence": "OBSERVED",
            }
        },
    )
    assert item.posted_at_source is None and item.updated_at_source is not None
    assert item.preferred_skills == ["Redis"] and item.required_skills == ["Java"]
    assert item.education_constraint == "Bachelor's degree"
    assert item.salary_base_min == 1_000_000 and item.salary_source == item.canonical_url
    assert "Visa" in item.visa_text and item.deadline is not None
    assert item.provenance[0].source_id == "fixture"
    assert job(deadline_raw="invalid").deadline is None
    assert job(deadline_raw="2026-08-20").deadline is None
    assert job(provider_job_id="", url="", apply_url="").job_uid.startswith("job_")
    assert job(provider_job_id="", url="https://example.com/role").job_uid.startswith("job_")
    for location, description, mode, eligible in [
        ("Remote — US only", "Develop APIs", "REMOTE", False),
        ("Bengaluru hybrid", "Develop APIs", "HYBRID", True),
        ("London", "Develop APIs; visa sponsorship available", "ONSITE", True),
        ("London", "Develop APIs", "ONSITE", None),
        ("", "Develop APIs", "UNKNOWN", None),
    ]:
        result = job(location=location, description=description)
        assert (result.work_mode, result.location_eligible) == (mode, eligible)
    with pytest.raises(ValueError):
        normalize_job(
            RawJob(source_id="s", provider="p", title="t", company="c"), datetime(2026, 1, 1)
        )


def test_tenure_keeps_month_precision() -> None:
    p = profile().model_copy(
        update={
            "full_time_start": "2025-08",
            "internship_start": "2025-01",
            "internship_end": "2025-06",
        }
    )
    minimum, maximum = tenure_range(p, NOW)
    assert minimum < 1 < maximum
    assert (
        experience_fit(job().model_copy(update={"min_years": 1}), minimum, maximum) == "AMBIGUOUS"
    )
    assert tenure_range(p, NOW, internship_allowed=True)[0] > minimum
    assert tenure_range(p.model_copy(update={"full_time_start": "2027-01"}), NOW) == (0, 0)
    assert month_bounds("2024-02")[1].day == 29
    with pytest.raises(ValueError):
        month_bounds("2024-02-01")
    with pytest.raises(ValueError):
        tenure_range(p, datetime(2026, 1, 1))
    assert experience_fit(job().model_copy(update={"min_years": None}), 1, 2) == "AMBIGUOUS"
    assert experience_fit(job().model_copy(update={"min_years": 3}), 1, 2) == "STRETCH"
    assert experience_fit(job().model_copy(update={"min_years": 4}), 1, 2) == "OUT_OF_SCOPE"


@pytest.mark.parametrize(
    ("priority", "fit", "age", "expected"),
    [
        (85, 75, 23.99, "P0"),
        (84.99, 75, 2, "P1"),
        (85, 74.99, 2, "P1"),
        (85, 75, 24, "P1"),
        (70, 60, 191.99, "P1"),
        (70, 60, 192, "P2"),
        (69.99, 60, 2, "P2"),
        (70, 59.99, 2, "P2"),
        (50, 0, 2, "P2"),
        (49.99, 100, 2, "P3"),
    ],
)
def test_priority_golden_boundaries(priority: float, fit: float, age: float, expected: str) -> None:
    assert assign_priority(fit=fit, priority=priority, job=job(), age_max=age)[0] == expected


def test_priority_operational_gates() -> None:
    item = job()
    assert assign_priority(
        fit=100, priority=100, job=item, age_max=2, exclusion="closed", application_stage="APPLIED"
    ) == ("PIPELINE", "TRACKING")
    assert assign_priority(fit=100, priority=100, job=item, age_max=2, exclusion="closed") == (
        "EXCLUDED",
        "BLOCKED_HARD",
    )
    assert assign_priority(
        fit=100, priority=100, job=item.model_copy(update={"quarantine_reason": "gray"}), age_max=2
    ) == ("P2", "QUARANTINED")
    assert assign_priority(
        fit=100,
        priority=100,
        job=item.model_copy(update={"official_link_state": "OFFICIAL_LINK_UNVERIFIED"}),
        age_max=2,
    ) == ("P2", "NEEDS_OFFICIAL_LINK")
    assert assign_priority(fit=100, priority=100, job=item, age_max=None) == ("P2", "NEEDS_REVIEW")
    observed = item.model_copy(
        update={"baseline": False, "freshness_band": "OBSERVED_NEW_DATE_UNKNOWN"}
    )
    assert assign_priority(fit=100, priority=100, job=observed, age_max=None)[0] == "P0"
    changed = item.model_copy(update={"baseline": False, "active_state": "CHANGED"})
    assert assign_priority(fit=100, priority=80, job=changed, age_max=500)[0] == "P1"


def test_full_scoring_and_gaps() -> None:
    p = profile()
    result = score_job(job(), p, NOW)
    assert (
        result.fit_score == 100 and result.priority_score == 87 and result.action_priority == "P0"
    )
    assert sum(result.components.values()) == 87 and result.action_by_ist.endswith("IST")
    assert score_job(job(posted_at_raw=None), p, NOW).action_priority == "P2"
    assert score_job(job(), p, NOW + timedelta(days=2)).action_priority == "P1"
    assert score_job(job(), p, NOW, manual_priority=49).score_band == "S3"
    assert score_job(job(), p, NOW, manual_priority=60).score_band == "S2"
    assert score_job(job(), p, NOW, manual_priority=75).score_band == "S1"
    assert (
        score_job(job(), p, NOW, contact_readiness="WARM_CONNECTION").fit_score == result.fit_score
    )
    unknown = job(description="Operate systems. Kotlin required. Go preferred.").model_copy(
        update={
            "source_health": "STALE",
            "official_link_state": "OFFICIAL_LINK_UNVERIFIED",
            "quarantine_reason": "UNRESOLVED_DUPLICATE_GRAY_ZONE",
            "location_eligible": None,
        }
    )
    weak = score_job(unknown, p, NOW)
    assert weak.action_priority == "P3" and weak.gaps and weak.action_eligibility == "QUARANTINED"
    assert (
        weak.components["stale_source_penalty"] == -10
        and weak.components["duplicate_gray_zone_penalty"] == -25
    )
    assert score_job(job(description="Develop APIs"), p, NOW).dimensions["skills"] == 0.5
    assert (
        score_job(job(title="Cloud Engineer", description="Operate tickets"), p, NOW).dimensions[
            "role"
        ]
        == 0.4
    )
    assert (
        score_job(
            job(
                title="Software Engineer II",
                description="Develop Java and Redis APIs; 2 years of software experience",
            ),
            p,
            NOW,
        ).dimensions["role"]
        == 0.8
    )
    for update in (
        {"active_state": "CLOSED"},
        {"location_eligible": False},
        {"min_years": 4},
        {"min_years": 3, "required_skills": ["Kotlin"]},
        {"education_constraint": "Graduating in 2027"},
        {
            "metadata": {
                "hard_blocker_verified": True,
                "hard_blocker_evidence": "Security clearance required",
            }
        },
    ):
        assert score_job(job().model_copy(update=update), p, NOW).action_priority == "EXCLUDED"
    assert (
        score_job(
            job().model_copy(update={"min_years": 4}), p, NOW, manual_include=True
        ).action_priority
        != "EXCLUDED"
    )
    assert (
        score_job(
            job().model_copy(update={"min_years": 3}), p, NOW, manual_include=True
        ).action_priority
        != "EXCLUDED"
    )
    assert (
        score_job(
            job().model_copy(update={"deadline": NOW + timedelta(hours=30)}), p, NOW
        ).components["deadline"]
        == 2
    )
    assert (
        score_job(
            job().model_copy(update={"deadline": NOW + timedelta(days=5)}), p, NOW
        ).components["deadline"]
        == 1
    )
    assert (
        select_evidence(
            job(),
            p.model_copy(
                update={
                    "evidence": [
                        CandidateEvidence(
                            evidence_id="no", outreach_safe_paraphrase="Unapproved", tags=["Java"]
                        )
                    ]
                }
            ),
            outreach_only=True,
        )
        == []
    )
    with pytest.raises(ValueError):
        ScoringConfig(priority_weights={"fit": 99})


@pytest.mark.parametrize(
    ("base", "expected"), [(1_500_000, 1), (1_250_000, 0.75), (1_000_000, 0.25)]
)
def test_compensation_base_only(base: float, expected: float) -> None:
    p = profile().model_copy(update={"current_base_lpa": 10, "target_base_lpa": 15})
    item = job().model_copy(
        update={
            "salary_currency": "INR",
            "salary_period": "ANNUAL",
            "salary_base_min": base,
            "salary_confidence": "OBSERVED",
            "salary_source": "https://example.com/role",
        }
    )
    assert compensation_signal(item, p) == expected
    assert compensation_signal(item.model_copy(update={"salary_currency": "USD"}), p) == 0.5
    assert (
        compensation_signal(
            item.model_copy(update={"salary_base_min": None, "salary_total_min": 5_000_000}), p
        )
        == 0.5
    )


def test_identity_namespace_fuzzy_and_no_false_merges() -> None:
    left = job()
    right = job(provider_job_id="2")
    assert comparable_conflict(left, right) and match_reason(left, right) == ""
    right = job(provider="lever", tenant="other", provider_job_id="2")
    assert match_reason(left, right) == "EXACT_CANONICAL_URL"
    right.canonical_url = "https://example.org/role"
    assert match_reason(left, right) == "EXACT_FINGERPRINT"
    assert match_reason(left, right.model_copy(update={"do_not_merge": True})) == ""
    assert match_reason(left, right.model_copy(update={"normalized_company": "another"})) == ""
    assert match_reason(left, right.model_copy(update={"location": "Pune"})) == ""
    assert match_reason(left, right.model_copy(update={"description": ""})) == ""
    assert (
        match_reason(left, right.model_copy(update={"posted_at_source": NOW - timedelta(days=60)}))
        == ""
    )
    assert (
        match_reason(
            left,
            right.model_copy(update={"description": "Quantum genetics research with no relation"}),
        )
        == ""
    )
    assert (
        match_reason(left, right.model_copy(update={"description_hash": "other"}))
        == "FUZZY_HIGH_CONFIDENCE"
    )
    gray = match_reason(left, right, DedupeConfig(title_threshold=101))
    assert gray == "GRAY_ZONE"
    result = upsert_job([left], right, DedupeConfig(title_threshold=101))
    assert result.new and result.job.quarantine_reason == "UNRESOLVED_DUPLICATE_GRAY_ZONE"
    right.identities = [
        JobIdentity(provider="lever", requisition_namespace="employer", requisition_id="REQ1")
    ]
    left.identities = [
        JobIdentity(provider="greenhouse", requisition_namespace="employer", requisition_id="REQ1")
    ]
    assert match_reason(left, right) == "EXACT_REQUISITION"
    right.identities[0].requisition_id = "REQ2"
    assert comparable_conflict(left, right)


def test_upsert_is_idempotent_and_uid_immutable() -> None:
    original = job(
        provider="email",
        tenant="alert",
        provider_job_id="alert-1",
        official_link_state="OFFICIAL_LINK_UNVERIFIED",
        posted_at_raw=None,
    )
    first = upsert_job([], original)
    assert first.new
    official = job()
    promoted = upsert_job(first.jobs, official)
    assert (
        promoted.job.job_uid == original.job_uid
        and promoted.job.official_link_state == "VERIFIED_OFFICIAL"
    )
    assert len(promoted.job.identities) == 2 and promoted.audit
    assert original.official_link_state == "OFFICIAL_LINK_UNVERIFIED"
    repeated = upsert_job(promoted.jobs, official)
    assert not repeated.new and not repeated.changed and not repeated.audit
    revised = job(
        description="Develop Java and Redis APIs. New requirements: PostgreSQL. Required 0-2 years of software experience."
    )
    changed = upsert_job(repeated.jobs, revised)
    assert (
        changed.changed
        and changed.job.active_state == "CHANGED"
        and changed.job.material_change_sequence == 1
    )
    assert len(changed.jobs) == 1
    newer = job(posted_at_raw=(NOW - timedelta(hours=1)).isoformat())
    preserved = upsert_job([job()], newer)
    assert preserved.job.posted_at_source == job().posted_at_source
    aggregator = original.model_copy(update={"title": "Incorrect title"})
    assert upsert_job([promoted.job], aggregator).job.title == official.title


def test_lifecycle_partial_miss_and_reopen() -> None:
    item = job()
    assert mark_missing(item, NOW, snapshot_complete=False).missing_snapshots == 0
    first = mark_missing(item, NOW, snapshot_complete=True)
    assert first.missing_snapshots == 1 and first.active_state == "NEW"
    assert (
        mark_missing(first, NOW + timedelta(hours=6), snapshot_complete=True).missing_snapshots == 1
    )
    second = mark_missing(first, NOW + timedelta(hours=12), snapshot_complete=True)
    assert second.active_state == "POSSIBLY_CLOSED"
    closed = mark_missing(second, NOW + timedelta(hours=24), snapshot_complete=True)
    assert closed.active_state == "CLOSED"
    reopened = mark_missing(closed, NOW + timedelta(days=3), snapshot_complete=True, seen=True)
    assert reopened.active_state == "REOPENED" and reopened.first_seen_at == item.first_seen_at
    assert (
        mark_missing(item, NOW, snapshot_complete=False, explicit_closed=True).active_state
        == "CLOSED"
    )
    assert mark_missing(item, NOW, snapshot_complete=True, seen=True).active_state == "ACTIVE"
    merged = upsert_job([closed], job())
    assert merged.job.active_state == "REOPENED" and merged.audit[0]["event"] == "REOPENED"
    with pytest.raises(ValueError):
        mark_missing(item, datetime(2026, 1, 1), snapshot_complete=True)


def test_brief_separates_evidence_inference_and_notes() -> None:
    p, item = profile(), job()
    p.github_url = "https://example.com/portfolio"
    score = score_job(item, p, NOW)
    brief = build_brief(item, score, p, NOW)
    assert brief["source_backed_facts"]["requirements"][0]["source_url"] == item.canonical_url
    assert brief["approved_evidence"] and brief["user_notes"] == ""
    assert len(brief["deterministic_inference"]["interviewer_questions"]) == 5
    assert len(brief["deterministic_inference"]["technical_themes"]) == 3
    assert "resume_verbatim" not in str(brief)
    for policy, wait, expected in [
        ("UNKNOWN", None, 1),
        ("APPLY_DIRECT", None, 0),
        ("REFERRAL_CAN_ATTACH_LATER", None, 0),
        ("REFERRAL_BEFORE_APPLY", 7, 2),
        ("REFERRAL_BEFORE_APPLY", None, 1),
    ]:
        assert (
            referral_plan(item, score, policy=policy, company_wait_hours=wait)[
                "maximum_referral_wait_hours"
            ]
            == expected
        )
    unknown = job(
        posted_at_raw=None, location="Unknown", official_link_state="OFFICIAL_LINK_UNVERIFIED"
    )
    assert (
        build_brief(unknown, score_job(unknown, p, NOW), p, NOW)["confidence"]
        == "REQUIRES_VERIFICATION"
    )
    paid = item.model_copy(
        update={"salary_confidence": "OBSERVED", "salary_source": item.canonical_url}
    )
    assert isinstance(build_brief(paid, score, p, NOW)["source_backed_facts"]["compensation"], dict)
    assert ist(None) == "UNKNOWN"


@pytest.mark.parametrize(
    ("provider", "payload", "field", "amount", "period"),
    [
        (
            "jsonld",
            {
                "currency": "INR",
                "value": {"minValue": 1000000, "maxValue": 1200000, "unitText": "YEAR"},
            },
            "salary_base_min",
            1000000,
            "ANNUAL",
        ),
        ("jsonld", {"currency": "USD", "value": 50000}, "salary_base_min", 50000, "UNKNOWN"),
        (
            "lever",
            {
                "currency": "INR",
                "min": 100000,
                "max": 120000,
                "interval": "month",
                "compensationType": "base",
            },
            "salary_base_min",
            100000,
            "MONTHLY",
        ),
        (
            "lever",
            {
                "currency": "USD",
                "min": 100000,
                "max": 120000,
                "interval": "year",
                "compensationType": "total",
            },
            "salary_total_min",
            100000,
            "ANNUAL",
        ),
        (
            "greenhouse",
            [
                {
                    "currency": "INR",
                    "min_cents": 100000000,
                    "max_cents": 120000000,
                    "title": "base salary",
                    "period": "year",
                }
            ],
            "salary_base_min",
            1000000,
            "ANNUAL",
        ),
    ],
)
def test_source_salary_components(
    provider: str, payload: object, field: str, amount: float, period: str
) -> None:
    raw = RawJob(
        source_id="s",
        provider=provider,
        title="Backend Engineer",
        company="Example",
        url="https://example.com/job",
        salary_text=json.dumps(payload),
    )
    result = parse_salary(raw)
    assert (
        result[field] == amount
        and result["salary_period"] == period
        and result["salary_confidence"] == "OBSERVED"
    )
    assert getattr(normalize_job(raw, NOW), field) == amount


@pytest.mark.parametrize(
    ("provider", "payload"),
    [
        ("greenhouse", []),
        ("greenhouse", [{}, {}]),
        ("lever", []),
        ("lever", {"currency": "INR", "min": 1000, "max": 2000}),
        ("jsonld", {"currency": "INR", "value": "not numeric"}),
        ("jsonld", {"currency": "INR", "value": True}),
        ("jsonld", {"currency": "INR", "value": {"minValue": -1}}),
        ("jsonld", {"currency": "INR", "value": {"minValue": float("nan")}}),
        ("jsonld", {"currency": "INR", "value": {"minValue": 2, "maxValue": 1}}),
        ("jsonld", {"currency": "rupees", "value": 500}),
    ],
)
def test_ambiguous_salary_not_invented(provider: str, payload: object) -> None:
    raw = RawJob(
        source_id="s", provider=provider, title="t", company="c", salary_text=json.dumps(payload)
    )
    assert parse_salary(raw) == {}
    assert parse_salary(raw.model_copy(update={"salary_text": "competitive pay"})) == {}
