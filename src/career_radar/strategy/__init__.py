"""Durable structured briefing data with facts separated from deterministic suggestions."""

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from career_radar.domain import CandidateProfile, JobScore, NormalizedJob
from career_radar.outreach import approved_claims, five_business_days
from career_radar.scoring import select_evidence


def ist(value: datetime | None) -> str:
    return (
        "UNKNOWN"
        if value is None
        else value.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M IST")
    )


def manual_discovery(job: NormalizedJob) -> dict[str, list[str]]:
    return {
        "manual_search_phrases": [
            f"site:linkedin.com/in {job.company} recruiter {job.role_family} India",
            f"site:linkedin.com/in {job.company} engineering manager Bengaluru",
            f"{job.company} careers recruiting contact",
            f"{job.company} engineering blog {job.role_family}",
        ],
        "checklist": [
            "Review user-owned contacts for a current-company relationship.",
            "Inspect the official posting for an explicitly published recruiting channel.",
            "Record field-level provenance and manually approve a channel before drafting outreach.",
            "Do not delay the application while looking for a contact.",
        ],
    }


def referral_plan(
    job: NormalizedJob,
    score: JobScore,
    *,
    policy: str = "UNKNOWN",
    company_wait_hours: float | None = None,
) -> dict[str, str | float]:
    application_hours = {"P0": 2.0, "P1": 12.0, "P2": 48.0, "P3": 168.0}.get(
        score.action_priority, 0.0
    )
    if policy == "APPLY_DIRECT" or policy == "REFERRAL_CAN_ATTACH_LATER":
        wait = 0.0
    elif policy == "REFERRAL_BEFORE_APPLY":
        wait = min(
            application_hours,
            max(
                0.0, company_wait_hours if company_wait_hours is not None else application_hours / 2
            ),
        )
    else:
        wait = application_hours / 2
    return {
        "company_referral_policy": policy,
        "maximum_referral_wait_hours": wait,
        "application_due": score.action_by_ist,
        "referral_review_due": ist(
            job.first_seen_at + timedelta(minutes=30 if score.action_priority == "P0" else 120)
        ),
        "sequence": "Verify the official posting; prepare approved evidence and a genuine warm referral in parallel; apply by the application SLA even if no contact replies.",
        "stopping_rule": "At most one follow-up after five business days, only after a manually recorded send, no reply, and no opt-out.",
    }


def build_brief(
    job: NormalizedJob, score: JobScore, profile: CandidateProfile, now: datetime
) -> dict[str, Any]:
    evidence = select_evidence(job, profile, outreach_only=True)
    claims, ids = approved_claims(evidence)
    sentences = [part.strip() for part in re.split(r"[\n.;]", job.description) if part.strip()]
    requirements = [
        sentence
        for sentence in sentences
        if re.search(
            r"require|experience|skill|develop|build|design|degree|knowledge", sentence, re.I
        )
    ][:5]
    themes = job.required_skills[:3]
    themes += [
        theme
        for theme in (
            "API design and reliability",
            "Testing failure modes",
            "Engineering trade-offs",
        )
        if len(themes) < 3
    ]
    introduction = (
        f"I am {profile.name}. I am considering {job.title} at {job.company}. "
        + " ".join(claims)
        + " I can explain the context, decisions, and limitations behind these examples, and discuss how they relate to the advertised responsibilities. I would also like to understand the team's immediate priorities and clarify any experience requirements where my background is a near match."
    )
    confidence = (
        "SOURCE_BACKED"
        if job.posted_at_source and job.official_link_state == "VERIFIED_OFFICIAL"
        else "REQUIRES_VERIFICATION"
    )
    salary: dict[str, Any] | str = "UNKNOWN - ASK RECRUITER AT APPROPRIATE STAGE"
    if job.salary_source and job.salary_confidence != "MISSING":
        salary = {
            "base_min": job.salary_base_min,
            "base_max": job.salary_base_max,
            "total_min": job.salary_total_min,
            "total_max": job.salary_total_max,
            "currency": job.salary_currency,
            "period": job.salary_period,
            "source": job.salary_source,
            "confidence": job.salary_confidence,
        }
    return {
        "brief_uid": f"brief_{job.job_uid}_{job.material_change_sequence}",
        "job_uid": job.job_uid,
        "generated_at": now.isoformat(),
        "source_backed_facts": {
            "company": job.company,
            "title": job.title,
            "job_ids": [identity.provider_job_id for identity in job.identities],
            "official_url": job.apply_url,
            "official_link_state": job.official_link_state,
            "location": job.location,
            "posted_at": ist(job.posted_at_source),
            "posted_at_confidence": job.posted_at_confidence,
            "first_seen": ist(job.first_seen_at),
            "deadline": ist(job.deadline),
            "source_status": job.active_state,
            "requirements": [
                {"quote": sentence[:250], "source_url": job.canonical_url}
                for sentence in requirements
            ],
            "role_summary": [sentence[:200] for sentence in sentences[:5]],
            "compensation": salary,
        },
        "approved_evidence": [
            {
                "evidence_id": entry.evidence_id,
                "claim": claim,
                "reason": "Tags or technologies overlap the source posting.",
            }
            for entry_id, claim in zip(ids, claims, strict=True)
            for entry in evidence
            if entry.evidence_id == entry_id
        ],
        "deterministic_inference": {
            "value_proposition": f"Interest in {job.title}, grounded in the following approved evidence: "
            + " ".join(claims),
            "resume_headline_emphasis": f"Emphasize verified {', '.join(job.required_skills[:3]) or job.role_family} work only.",
            "resume_suggestions": [
                f"Move approved evidence {evidence_id} earlier because its tags overlap the posting; preserve all limitations."
                for evidence_id in ids
            ][:3],
            "project_links": [
                {
                    "url": profile.github_url,
                    "reason": "User-supplied portfolio; manually select a project supporting the role requirements.",
                }
            ]
            if profile.github_url
            else [],
            "interview_introduction": introduction,
            "introduction_target_seconds": "45–60; rehearse and adjust pace without adding claims",
            "technical_themes": themes[:3],
            "behavioral_questions": [
                "Describe a difficult engineering trade-off and its outcome.",
                "How did you validate a change under uncertainty?",
                "Describe a collaboration challenge and what you learned.",
            ],
            "star_story_evidence_ids": ids,
            "interviewer_questions": [
                f"Which {themes[0]} problems would this role address first?",
                "How does the team validate changes before production?",
                "What would success look like in the first three months?",
                "Which responsibilities in the posting take most of the team's time?",
                "How are early-career engineers supported when learning unfamiliar parts of the system?",
            ],
            "soft_gaps_and_preparation": score.gaps,
            "hard_gaps": [score.exclusion_reason] if score.exclusion_reason else [],
            "application_sequence": referral_plan(job, score),
            "earliest_follow_up_if_sent_now": ist(five_business_days(now)),
            "manual_contact_discovery": manual_discovery(job),
        },
        "score": score.model_dump(mode="json"),
        "confidence": confidence,
        "risk_note": "; ".join(
            filter(
                None,
                (
                    job.quarantine_reason,
                    score.exclusion_reason,
                    "Posting age is unknown; first seen is only an observation proxy."
                    if job.posted_at_source is None
                    else "Source age is an interval; no applicant rank is asserted.",
                    "Location/visa eligibility needs review."
                    if job.location_eligible is None
                    else "",
                    "Official link needs verification."
                    if job.official_link_state != "VERIFIED_OFFICIAL"
                    else "",
                ),
            )
        ),
        "user_notes": "",
    }
