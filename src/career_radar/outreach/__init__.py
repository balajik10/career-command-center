"""Human-reviewed drafts only. No send transport and no inferred channels."""

import hashlib
import re
from collections.abc import Iterable
from datetime import datetime, timedelta

from jinja2 import Environment, StrictUndefined

from career_radar.contacts import USER_ORIGINS
from career_radar.domain import (
    CandidateEvidence,
    CandidateProfile,
    Contact,
    JobScore,
    NormalizedJob,
    OutreachDraft,
)
from career_radar.normalize import normalize_company
from career_radar.scoring import select_evidence

# These templates produce text fields, never HTML. HTML/email renderers must escape
# the final field at their own output boundary; HTML entities here corrupt text drafts.
ENV = Environment(undefined=StrictUndefined, autoescape=False)  # nosec B701
LIMITS = {
    "LINKEDIN_MESSAGE": (80, 160),
    "EMAIL": (130, 190),
    "WHATSAPP": (50, 90),
    "APPLICATION_NOTE": (120, 220),
    "FOLLOW_UP": (0, 90),
}
UNSAFE_CLAIMS = (
    "stealth",
    "bypassed akamai",
    "defeated bot protection",
    "exactly-once kafka",
    "exactly once kafka",
    "10x faster",
    "10x lower latency",
    "10x throughput",
)


def approved_claims(evidence: list[CandidateEvidence]) -> tuple[list[str], list[str]]:
    claims: list[str] = []
    ids: list[str] = []
    for entry in evidence[:2]:
        if not entry.allowed_in_outreach:
            continue
        claim = next(
            (
                claim.strip()
                for claim in entry.allowed_atomic_claims
                if 1 <= len(claim.split()) <= 32
                and not any(
                    term.casefold() in claim.casefold()
                    for term in (*UNSAFE_CLAIMS, *entry.forbidden_extrapolations)
                )
            ),
            "",
        )
        if claim and claim not in claims:
            claims.append(claim)
            ids.append(entry.evidence_id)
    return claims, ids


def five_business_days(start: datetime) -> datetime:
    current = start
    remaining = 5
    while remaining:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def can_contact(
    contact: Contact, now: datetime, *, follow_up: bool = False, override_reason: str = ""
) -> bool:
    if now.tzinfo is None:
        raise ValueError("run datetime must be timezone-aware")
    if (
        contact.do_not_contact
        or not contact.outreach_allowed
        or contact.reply_state not in {"NONE", "NO_REPLY"}
    ):
        return False
    if contact.retention_expires_at and contact.retention_expires_at <= now:
        return False
    if contact.contact_origin not in USER_ORIGINS and not contact.contactability_basis.startswith(
        "EXPLICIT_RECRUITING_CHANNEL:"
    ):
        return False
    if follow_up:
        return bool(
            contact.last_contacted_at
            and contact.follow_up_count == 0
            and now >= five_business_days(contact.last_contacted_at)
        )
    return not (
        contact.last_contacted_at
        and now - contact.last_contacted_at < timedelta(days=30)
        and not override_reason.strip()
    )


def validate_draft(draft: OutreachDraft, *, connection_limit: int = 250) -> None:
    if re.search(
        r"\{\{|\}\}|\{(?:company|name|role)\}|\[(?:Name|Company|Role|Link)\]",
        draft.body + draft.subject,
        re.I,
    ):
        raise ValueError("unresolved draft placeholder")
    if any(term in draft.body.casefold() for term in UNSAFE_CLAIMS):
        raise ValueError("unsupported or unsafe claim")
    if draft.channel == "LINKEDIN_CONNECTION":
        if len(draft.body) > connection_limit:
            raise ValueError("connection note too long")
    else:
        minimum, maximum = LIMITS[draft.channel]
        if not minimum <= len(draft.body.split()) <= maximum:
            raise ValueError("draft word count outside channel limit")
    if len(draft.subject) > 60:
        raise ValueError("email subject too long")


APPLICATION = """I am applying for {{ title }} at {{ company }} (job {{ job_id }}). The posting's focus on {{ focus }} makes this a role I would like to discuss. I would welcome the opportunity to understand the team's priorities and the problems this position is expected to solve.

My relevant evidence is: {{ claims }} These are specific examples I can explain in an interview, including the implementation decisions, limitations, and how the outcomes were checked. I would tailor the discussion to the responsibilities in the posting and clearly distinguish direct experience from areas I am preparing to learn.

Please consider my application for the advertised role. I am happy to walk through the relevant work and discuss any stated requirements that need clarification. Official posting: {{ url }}. Thank you for reviewing my application.
{{ candidate }}"""
LINKEDIN = """{{ greeting }} I am considering {{ title }} at {{ company }} (job {{ job_id }}). The posting mentions {{ focus }}, which is why I am reaching out about this specific opening.

My relevant evidence is: {{ claims }} I can share the context and implementation details if useful. {{ ask }} There is no pressure to help, and I understand if the role is outside your team or you do not have enough context. I will follow the official application process and do not want to delay applying while waiting for a response. Posting: {{ url }}. Thank you for your time."""
EMAIL = """{{ greeting }}

I am writing about {{ title }} at {{ company }} (job {{ job_id }}). The advertised work on {{ focus }} is the reason for this specific inquiry. I would appreciate clarification on whether this opening is the appropriate route for my application.

My relevant evidence is: {{ claims }} I would be happy to explain the context, engineering decisions, and limitations of those examples. I have kept this note focused on the posting and would welcome a discussion of the team's immediate priorities and how the advertised requirements are evaluated.

{{ ask }} I understand that you may not be the right person for this role. There is no obligation to respond, and I will use the official process for the application. Official posting: {{ url }}.

Thank you for your time and consideration.
{{ candidate }}
{{ signature }}"""
WHATSAPP = """{{ greeting }} I am considering {{ title }} at {{ company }} (job {{ job_id }}). One relevant example: {{ short_claim }} {{ short_ask }} No pressure if this is outside your team or inconvenient. I will use the official application route and can share more context if useful. Thank you for your time."""


def generate_drafts(
    job: NormalizedJob,
    score: JobScore,
    profile: CandidateProfile,
    now: datetime,
    contact: Contact | None = None,
    existing_ids: Iterable[str] = (),
    *,
    connection_limit: int = 250,
    override_reason: str = "",
) -> list[OutreachDraft]:
    if score.action_priority not in {"P0", "P1"} or score.action_eligibility != "READY":
        return []
    evidence = select_evidence(job, profile, outreach_only=True)
    claims, evidence_ids = approved_claims(evidence)
    if not claims:
        return []
    job_id = next(
        (identity.provider_job_id for identity in job.identities if identity.provider_job_id),
        job.job_uid,
    )
    focus = ", ".join(job.required_skills[:3]) or job.role_family.lower().replace("_", " ")
    warm = (
        contact is not None
        and contact.contact_origin in USER_ORIGINS
        and contact.relationship_basis != "UNKNOWN"
        and normalize_company(contact.company or "") == job.normalized_company
        and contact.current_company_confidence == "USER_SUPPLIED_CURRENT"
        and now - contact.evidence_fetched_at <= timedelta(days=180)
    )
    ask = (
        "Would you be open to referring me, only if you're comfortable and believe the fit is appropriate?"
        if warm
        else "Could you point me to the appropriate application route or clarify the team's requirements?"
    )
    greeting = f"Hello {contact.name}," if contact and contact.name else "Hello,"
    data = {
        "title": job.title,
        "company": job.company,
        "job_id": job_id,
        "focus": focus,
        "claims": " ".join(claim.rstrip(".") + "." for claim in claims),
        "url": job.apply_url,
        "candidate": profile.name,
        "greeting": greeting,
        "ask": ask,
        "short_claim": claims[0].rstrip(".") + ".",
        "short_ask": "Would you consider a referral, only if comfortable?"
        if warm
        else "Could you clarify the application route?",
        "signature": " ".join(filter(None, (profile.linkedin_url, profile.github_url))),
    }
    # The application note has no third-party recipient and uses the verified official form.
    templates = [("APPLICATION_NOTE", APPLICATION, "APPLICATION")]
    if contact is not None and can_contact(contact, now, override_reason=override_reason):
        if contact.linkedin_url and "linkedin_url" in contact.field_provenance:
            templates.append(("LINKEDIN_MESSAGE", LINKEDIN, "REFERRAL" if warm else "ROLE_INQUIRY"))
            note = "Hello, I saw {{ title }} at {{ company }}. The {{ focus }} work caught my interest; I would welcome connecting about the advertised role."
            if len(ENV.from_string(note).render(**data)) <= connection_limit:
                templates.append(("LINKEDIN_CONNECTION", note, "CONNECT"))
        if contact.work_email and "work_email" in contact.field_provenance:
            templates.append(("EMAIL", EMAIL, "REFERRAL" if warm else "ROLE_INQUIRY"))
        if contact.phone and contact.whatsapp_allowed and "phone" in contact.field_provenance:
            templates.append(("WHATSAPP", WHATSAPP, "REFERRAL" if warm else "ROLE_INQUIRY"))
    result: list[OutreachDraft] = []
    existing = set(existing_ids)
    for channel, template, purpose in templates:
        contact_uid = (
            "" if channel == "APPLICATION_NOTE" or contact is None else contact.contact_uid
        )
        seed = f"{job.job_uid}|{contact_uid}|{channel}|{purpose}|{job.material_change_sequence}"
        uid = "draft_" + hashlib.sha256(seed.encode()).hexdigest()[:24]
        if uid in existing:
            continue
        body = ENV.from_string(template).render(**data)
        subject = f"{job.title} — {job_id}"[:60] if channel == "EMAIL" else ""
        draft = OutreachDraft(
            draft_id=uid,
            job_uid=job.job_uid,
            contact_uid=contact_uid,
            channel=channel,
            purpose=purpose,
            subject=subject,
            body=body,
            evidence_ids=evidence_ids[:1]
            if channel == "WHATSAPP"
            else []
            if channel == "LINKEDIN_CONNECTION"
            else evidence_ids,
            word_count=len(body.split()),
            character_count=len(body),
            generated_at=now,
        )
        validate_draft(draft, connection_limit=connection_limit)
        result.append(draft)
    return result


def generate_follow_up(job: NormalizedJob, contact: Contact, now: datetime) -> OutreachDraft | None:
    if not can_contact(contact, now, follow_up=True):
        return None
    body = f"Hello{(' ' + contact.name) if contact.name else ''}, a brief follow-up on my earlier note about {job.title} at {job.company}. If you have relevant guidance, I would appreciate it. No pressure to respond; I will not send another follow-up. Thank you for your time."
    uid = (
        "draft_"
        + hashlib.sha256(f"{job.job_uid}|{contact.contact_uid}|FOLLOW_UP".encode()).hexdigest()[:24]
    )
    return OutreachDraft(
        draft_id=uid,
        job_uid=job.job_uid,
        contact_uid=contact.contact_uid,
        channel="FOLLOW_UP",
        purpose="FOLLOW_UP",
        body=body,
        word_count=len(body.split()),
        character_count=len(body),
        generated_at=now,
    )
