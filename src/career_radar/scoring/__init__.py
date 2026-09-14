"""Explainable independent fit and operational priority scores."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from career_radar.domain import CandidateEvidence, CandidateProfile, JobScore, NormalizedJob
from career_radar.normalize import detect_skills, freshness_band, month_bounds, normalized_text

FIT_WEIGHTS = {
    "role": 20.0,
    "experience": 15.0,
    "skills": 25.0,
    "evidence": 15.0,
    "location": 10.0,
    "responsibility": 10.0,
    "education": 5.0,
}
PRIORITY_WEIGHTS = {
    "fit": 55.0,
    "freshness": 15.0,
    "experience": 8.0,
    "official_route": 7.0,
    "target_tier": 5.0,
    "compensation": 4.0,
    "contact": 4.0,
    "deadline": 2.0,
}
FRESHNESS = {
    "HOT_0_6H": 1.0,
    "EARLY_6_24H": 0.9,
    "FRESH_1_3D": 0.65,
    "OPEN_4_7D": 0.35,
    "AGING_8_14D": 0.1,
    "STALE_15D_PLUS": 0.0,
    "OBSERVED_NEW_DATE_UNKNOWN": 0.75,
    "BASELINE_DATE_UNKNOWN": 0.0,
}
EXPERIENCE = {
    "IN_RANGE": 1.0,
    "MEETS": 1.0,
    "AMBIGUOUS": 0.75,
    "NEAR_MATCH_IN_SCOPE": 0.55,
    "STRETCH": 0.25,
    "OUT_OF_SCOPE": 0.0,
}
CONTACT = {
    "CONFIRMED_REFERRER": 1.0,
    "WARM_CONNECTION": 0.75,
    "PERMITTED_RECRUITER": 0.4,
    "PERMITTED_TEAM_CHANNEL": 0.4,
    "NONE": 0.0,
}


@dataclass(frozen=True)
class ScoringConfig:
    version: str = "1"
    fit_weights: dict[str, float] = field(default_factory=lambda: dict(FIT_WEIGHTS))
    priority_weights: dict[str, float] = field(default_factory=lambda: dict(PRIORITY_WEIGHTS))
    freshness: dict[str, float] = field(default_factory=lambda: dict(FRESHNESS))
    experience: dict[str, float] = field(default_factory=lambda: dict(EXPERIENCE))
    contact: dict[str, float] = field(default_factory=lambda: dict(CONTACT))
    p0_fit: float = 75
    p0_priority: float = 85
    p1_fit: float = 60
    p1_priority: float = 70
    p2_priority: float = 50
    p0_age_hours: float = 24
    p1_age_hours: float = 192
    stale_penalty: float = 10
    unverified_penalty: float = 10
    duplicate_penalty: float = 25

    def __post_init__(self) -> None:
        if (
            abs(sum(self.fit_weights.values()) - 100) > 0.001
            or abs(sum(self.priority_weights.values()) - 100) > 0.001
        ):
            raise ValueError("positive score weights must each total exactly 100")


def tenure_range(
    profile: CandidateProfile, now: datetime, *, internship_allowed: bool = False
) -> tuple[float, float]:
    if now.tzinfo is None:
        raise ValueError("run datetime must be timezone-aware")
    first, last = month_bounds(profile.full_time_start)
    lower = max(0.0, (now - last).total_seconds())
    upper = max(0.0, (now - first).total_seconds())
    if internship_allowed and profile.internship_start and profile.internship_end:
        start_first, start_last = month_bounds(profile.internship_start)
        end_first, end_last = month_bounds(profile.internship_end)
        # Clamp internship completion to now and prevent overlap with full-time work.
        lower += max(0.0, (min(end_first, first, now) - start_last).total_seconds())
        upper += max(0.0, (min(end_last, first, now) - start_first).total_seconds())
    return lower / (365.2425 * 86400), upper / (365.2425 * 86400)


def experience_fit(job: NormalizedJob, minimum_tenure: float, maximum_tenure: float) -> str:
    if job.min_years is None:
        return "AMBIGUOUS"
    requirement = job.min_years
    if requirement >= 4:
        return "OUT_OF_SCOPE"
    if requirement >= 3:
        return "STRETCH"
    if requirement == 0:
        return "IN_RANGE"
    if minimum_tenure >= requirement:
        return "MEETS"
    if maximum_tenure >= requirement:
        return "AMBIGUOUS"
    return "NEAR_MATCH_IN_SCOPE"


def select_evidence(
    job: NormalizedJob, profile: CandidateProfile, *, outreach_only: bool = False
) -> list[CandidateEvidence]:
    terms = set(normalized_text(job.description + " " + job.title).split())
    ranked: list[tuple[int, str, CandidateEvidence]] = []
    for evidence in profile.evidence:
        if outreach_only and (
            not evidence.allowed_in_outreach or not evidence.allowed_atomic_claims
        ):
            continue
        tags = set(normalized_text(" ".join(evidence.tags + evidence.technologies)).split())
        overlap = len(tags & terms)
        if overlap:
            ranked.append((overlap, evidence.evidence_id, evidence))
    return [item[2] for item in sorted(ranked, key=lambda item: (-item[0], item[1]))[:2]]


def compensation_signal(job: NormalizedJob, profile: CandidateProfile) -> float:
    if (
        job.salary_currency != "INR"
        or job.salary_period not in {"ANNUAL", "YEAR", "year"}
        or job.salary_base_min is None
        or job.salary_confidence not in {"OBSERVED", "SOURCE_BACKED", "EXACT"}
        or not job.salary_source
        or profile.target_base_lpa is None
        or profile.current_base_lpa is None
    ):
        return 0.5
    base_lpa = job.salary_base_min / 100000
    if base_lpa >= profile.target_base_lpa:
        return 1.0
    if base_lpa > profile.current_base_lpa:
        return 0.75
    return 0.25


def assign_priority(
    *,
    fit: float,
    priority: float,
    job: NormalizedJob,
    age_max: float | None,
    exclusion: str = "",
    application_stage: str = "",
    config: ScoringConfig | None = None,
) -> tuple[str, str]:
    cfg = config or ScoringConfig()
    if application_stage and application_stage not in {
        "NOT_APPLIED",
        "NOT_STARTED",
        "NOT_REVIEWED",
        "REVIEWING",
        "READY_TO_APPLY",
        "NEW",
        "NONE",
    }:
        return "PIPELINE", "TRACKING"
    if exclusion:
        return "EXCLUDED", "BLOCKED_HARD"
    route = job.official_link_state == "VERIFIED_OFFICIAL" and job.apply_url.startswith("https://")
    quarantine = bool(job.quarantine_reason)
    observed_new = not job.baseline and job.freshness_band == "OBSERVED_NEW_DATE_UNKNOWN"
    fresh0 = age_max is not None and 0 <= age_max < cfg.p0_age_hours or observed_new
    fresh1 = (
        age_max is not None
        and 0 <= age_max < cfg.p1_age_hours
        or observed_new
        or job.active_state == "CHANGED"
        and (
            not job.baseline
            or job.last_changed_at is not None
            and job.last_changed_at > job.first_seen_at
        )
    )
    if fit >= cfg.p0_fit and priority >= cfg.p0_priority and route and fresh0 and not quarantine:
        return "P0", "READY"
    if fit >= cfg.p1_fit and priority >= cfg.p1_priority and route and fresh1 and not quarantine:
        return "P1", "READY"
    eligibility = (
        "QUARANTINED" if quarantine else "NEEDS_OFFICIAL_LINK" if not route else "NEEDS_REVIEW"
    )
    return ("P2" if priority >= cfg.p2_priority else "P3"), eligibility


def score_job(
    job: NormalizedJob,
    profile: CandidateProfile,
    now: datetime,
    *,
    contact_readiness: str = "NONE",
    application_stage: str = "",
    manual_include: bool = False,
    manual_priority: float | None = None,
    config: ScoringConfig | None = None,
) -> JobScore:
    cfg = config or ScoringConfig()
    minimum, maximum = tenure_range(profile, now, internship_allowed=job.internship_allowed)
    experience = experience_fit(job, minimum, maximum)
    candidate_skills = set(detect_skills("; ".join(profile.skills))) | set(profile.skills)
    missing = sorted(set(job.required_skills) - candidate_skills)
    preferred_missing = sorted(set(job.preferred_skills) - candidate_skills)
    skill_ratio = (
        (len(job.required_skills) - len(missing)) / len(job.required_skills)
        if job.required_skills
        else 0.5
    )
    if job.preferred_skills:
        skill_ratio = 0.9 * skill_ratio + 0.1 * (
            len(job.preferred_skills) - len(preferred_missing)
        ) / len(job.preferred_skills)
    evidence = select_evidence(job, profile)
    exclusion = job.exclusion_reason
    if job.active_state == "CLOSED":
        exclusion = "VERIFIED_CLOSED"
    elif job.location_eligible is False:
        exclusion = "LOCATION_INELIGIBLE"
    elif not manual_include and (job.role_family == "EXCLUDED" or experience == "OUT_OF_SCOPE"):
        exclusion = "OUT_OF_SCOPE_ROLE_OR_EXPERIENCE"
    constraint_year = re.search(
        r"(?:class of|graduates? (?:of|from)|graduat(?:ing|ion) (?:in|year))\s*(20\d{2})",
        job.education_constraint,
        re.I,
    )
    education = 1.0
    if constraint_year and int(constraint_year.group(1)) != profile.graduation_year:
        exclusion, education = "GRADUATION_YEAR_INELIGIBLE", 0.0
    if job.metadata.get("hard_blocker_verified") is True and job.metadata.get(
        "hard_blocker_evidence"
    ):
        exclusion = "VERIFIED_HARD_CONSTRAINT: " + str(job.metadata["hard_blocker_evidence"])
    dimensions = {
        "role": 0.8
        if job.seniority == "STRETCH"
        else 0.4
        if job.role_family in {"UNKNOWN", "EXCLUDED"} or job.seniority == "REVIEW_SCOPE"
        else 1.0,
        "experience": cfg.experience[experience],
        "skills": skill_ratio,
        "evidence": min(1.0, len(evidence) / 2),
        "location": 1.0
        if job.location_eligible is True
        else 0.5
        if job.location_eligible is None
        else 0.0,
        "responsibility": 1.0
        if re.search(r"develop|build|design|software|backend|api", job.description, re.I)
        else 0.5,
        "education": education,
    }
    fit = round(sum(dimensions[key] * cfg.fit_weights[key] for key in dimensions), 4)
    if experience == "STRETCH" and fit < 75 and not manual_include:
        exclusion = "THREE_YEAR_STRETCH_BELOW_FIT_75"
    elapsed = max(0.0, (now - job.last_seen_at).total_seconds() / 3600)
    age_max = None if job.age_hours_max is None else job.age_hours_max + elapsed
    band = freshness_band(age_max, job.baseline)
    deadline_hours = None if job.deadline is None else (job.deadline - now).total_seconds() / 3600
    deadline_signal = (
        1.0
        if deadline_hours is not None and 0 <= deadline_hours <= 48
        else 0.5
        if deadline_hours is not None and 0 <= deadline_hours <= 168
        else 0.0
    )
    tier = max(0.0, min(1.0, profile.company_tiers.get(job.normalized_company, 0.0)))
    signals = {
        "fit": fit / 100,
        "freshness": cfg.freshness[band],
        "experience": cfg.experience[experience],
        "official_route": float(
            job.official_link_state == "VERIFIED_OFFICIAL" and job.apply_url.startswith("https://")
        ),
        "target_tier": tier,
        "compensation": compensation_signal(job, profile),
        "contact": cfg.contact.get(contact_readiness, 0.0),
        "deadline": deadline_signal,
    }
    components = {key: value * cfg.priority_weights[key] for key, value in signals.items()}
    if job.source_health in {"STALE", "DOWN"}:
        components["stale_source_penalty"] = -cfg.stale_penalty
    if job.official_link_state != "VERIFIED_OFFICIAL":
        components["unverified_link_penalty"] = -cfg.unverified_penalty
    if job.quarantine_reason == "UNRESOLVED_DUPLICATE_GRAY_ZONE":
        components["duplicate_gray_zone_penalty"] = -cfg.duplicate_penalty
    score = round(
        max(
            0.0,
            min(100.0, sum(components.values()) if manual_priority is None else manual_priority),
        ),
        4,
    )
    score_band = "S0" if score >= 85 else "S1" if score >= 70 else "S2" if score >= 50 else "S3"
    priority, eligibility = assign_priority(
        fit=fit,
        priority=score,
        job=job,
        age_max=age_max,
        exclusion=exclusion,
        application_stage=application_stage,
        config=cfg,
    )
    reasons = [
        f"{key}: {value * cfg.fit_weights[key]:.2f}/{cfg.fit_weights[key]:g}"
        for key, value in dimensions.items()
    ]
    reasons += [
        f"Full-time tenure interval: {minimum:.3f}–{maximum:.3f} years; internships {'counted by explicit posting rule' if job.internship_allowed else 'excluded'}.",
        f"Experience evidence: {job.experience_text or 'not stated'}",
        f"Freshness: {band}; maximum plausible source age {age_max if age_max is not None else 'UNKNOWN'} hours.",
    ]
    if manual_priority is not None:
        reasons.append("Manual numeric priority override; operational gates still enforced.")
    gaps = [f"Required skill absent from profile: {skill}" for skill in missing] + [
        f"Preferred skill gap: {skill}" for skill in preferred_missing
    ]
    if job.min_years is not None and minimum < job.min_years:
        gaps.append(
            f"Required experience {job.min_years:g} years; possible shortfall {max(0, job.min_years - maximum):.3f}–{job.min_years - minimum:.3f} years ({experience})."
        )
    action = {
        "P0": "Verify official posting and apply within two hours; prepare a warm referral in parallel.",
        "P1": "Prepare evidence and apply within twelve hours.",
        "P2": "Review evidence, eligibility and official link within forty-eight hours.",
        "P3": "Watch for useful changes; review within seven days.",
        "PIPELINE": f"Continue existing {application_stage} workflow; source status never overwrites it.",
        "EXCLUDED": "Retain exclusion evidence; no application alert.",
    }[priority]
    hours = {"P0": 2, "P1": 12, "P2": 48, "P3": 168}.get(priority)
    due = (
        ""
        if hours is None
        else (job.first_seen_at + timedelta(hours=hours))
        .astimezone(ZoneInfo("Asia/Kolkata"))
        .strftime("%Y-%m-%d %H:%M IST")
    )
    return JobScore(
        job_uid=job.job_uid,
        fit_score=fit,
        priority_score=score,
        dimensions=dimensions,
        components=components,
        evidence_ids=[item.evidence_id for item in evidence],
        reasons=reasons,
        gaps=gaps,
        experience_fit=experience,
        tenure_min_years=minimum,
        tenure_max_years=maximum,
        score_band=score_band,
        action_priority=priority,
        action_eligibility=eligibility,
        exclusion_reason=exclusion,
        next_action=action,
        action_by_ist=due,
    )
