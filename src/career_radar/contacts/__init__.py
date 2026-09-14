"""User-owned imports and explicitly observed recruiting channels; no people search."""

import csv
import io
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from career_radar.domain import Contact, JobContact, NormalizedJob, Provenance
from career_radar.normalize import normalize_company
from career_radar.security import canonical_url, plain_text, private_token

ALIASES = {
    "first": ("first name", "given name", "firstname"),
    "last": ("last name", "family name", "lastname"),
    "name": ("name", "full name"),
    "company": ("company", "organization 1 - name", "organization name", "current company"),
    "title": ("position", "title", "organization 1 - title", "job title"),
    "email": ("email address", "e-mail 1 - value", "email", "work email"),
    "linkedin": ("url", "linkedin url", "linkedin"),
    "phone": ("phone", "phone 1 - value", "phone number"),
    "relationship": ("relationship basis", "relationship"),
}
USER_ORIGINS = {"LINKEDIN_CONNECTIONS_EXPORT", "GOOGLE_CONTACTS_EXPORT", "USER_CSV", "USER_ENTERED"}


def _value(row: dict[str, str], field: str) -> str:
    return next((row[name].strip() for name in ALIASES[field] if row.get(name, "").strip()), "")


def contact_token(
    key: bytes, *, email: str = "", linkedin: str = "", name: str = "", company: str = ""
) -> str:
    identity = (
        email.casefold().strip()
        or linkedin.casefold().strip()
        or f"{name.casefold().strip()}|{normalize_company(company)}"
    )
    return private_token(key, "suppression", identity)


def import_contacts(
    path: Path | str,
    format: str,
    key: bytes,
    now: datetime,
    *,
    suppressed_tokens: set[str] | None = None,
) -> list[Contact]:
    if now.tzinfo is None:
        raise ValueError("import datetime must be timezone-aware")
    origin = {
        "linkedin-connections": "LINKEDIN_CONNECTIONS_EXPORT",
        "google-contacts": "GOOGLE_CONTACTS_EXPORT",
        "csv": "USER_CSV",
        "generic": "USER_CSV",
    }.get(format)
    if origin is None:
        raise ValueError("unsupported contacts format")
    source = Path(path)
    if source.stat().st_size > 5_000_000:
        raise ValueError("contacts file exceeds 5 MB cap")
    lines = source.read_text(encoding="utf-8-sig").splitlines()
    # LinkedIn exports may contain a human-readable preamble before their CSV header.
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if "," in line
            and re.search(r"first name|given name|full name|email|company|^name,", line, re.I)
        ),
        None,
    )
    if start is None:
        raise ValueError("missing recognized contacts header")
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    contacts: dict[str, Contact] = {}
    suppressed = suppressed_tokens or set()
    for raw in reader:
        row = {
            str(header).strip().casefold(): plain_text(str(value or ""))
            for header, value in raw.items()
            if header is not None
        }
        name = _value(row, "name") or " ".join(
            filter(None, (_value(row, "first"), _value(row, "last")))
        )
        company, title, email, linkedin, phone = (
            _value(row, item) for item in ("company", "title", "email", "linkedin", "phone")
        )
        if not name and not email and not linkedin:
            continue
        if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            email = ""
        if linkedin:
            linkedin = canonical_url(linkedin)
            if not linkedin.startswith("https://www.linkedin.com/in/") and not linkedin.startswith(
                "https://linkedin.com/in/"
            ):
                linkedin = ""
        if phone and not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
            phone = ""  # Never guess a country code or channel permission.
        token = contact_token(key, email=email, linkedin=linkedin, name=name, company=company)
        if token in suppressed:
            continue
        uid = "contact_" + token.split(":")[-1]
        ambiguous = bool(re.search(r"former|previous|past|until|ex-", f"{company} {title}", re.I))
        confidence = (
            "UNKNOWN" if not company else "AMBIGUOUS_PAST" if ambiguous else "USER_SUPPLIED_CURRENT"
        )
        relationship = _value(row, "relationship") or (
            "FIRST_DEGREE_IMPORTED"
            if origin == "LINKEDIN_CONNECTIONS_EXPORT"
            else "USER_OWNED_CONTACT"
        )
        values = {
            "name": name,
            "company": company,
            "title": title,
            "work_email": email,
            "linkedin_url": linkedin,
            "phone": phone,
        }
        provenance = {
            field: Provenance(source_id=origin, url=source.name, fetched_at=now, evidence=value)
            for field, value in values.items()
            if value
        }
        contacts[uid] = Contact(
            contact_uid=uid,
            name=name or None,
            company=company or None,
            title=title or None,
            work_email=email or None,
            linkedin_url=linkedin or None,
            phone=phone or None,
            contact_origin=origin,
            evidence_url=source.name,
            evidence_fetched_at=now,
            field_provenance=provenance,
            relationship_basis=relationship,
            current_company_confidence=confidence,
            contactability_basis="USER_SUPPLIED_CONTACT_CHANNEL",
            outreach_allowed=False,
            suppression_token=token,
            hmac_key_version="v1",
        )
    return list(contacts.values())


def permitted_public_contact(
    *,
    key: bytes,
    source_url: str,
    evidence: str,
    observed: dict[str, str],
    now: datetime,
    origin: Literal["PERMITTED_JOB_POSTING", "APPROVED_OFFICIAL_RECRUITING_PAGE"],
    approved: bool,
) -> Contact | None:
    """Observe only caller-approved recruiting text; return no invented person fields."""
    if not approved or not re.search(
        r"recruit|hiring|applications? (?:to|at)|apply (?:to|at)", evidence, re.I
    ):
        return None
    values = {
        field: value.strip()
        for field, value in observed.items()
        if field in {"name", "company", "title", "work_email", "linkedin_url", "phone"}
        and value.strip()
        and value.strip() in evidence
    }
    if "phone" in values and not re.search(
        r"(?:recruit|hiring|apply|whatsapp).{0,80}" + re.escape(values["phone"]), evidence, re.I
    ):
        values.pop("phone")
    if not any(values.get(field) for field in ("work_email", "linkedin_url", "phone")):
        return None
    email = values.get("work_email", "")
    if re.match(r"(?:careers|jobs|recruiting|recruitment|talent|hr)@", email, re.I):
        values.pop("name", None)
        values.pop("title", None)
    token = contact_token(
        key,
        email=email,
        linkedin=values.get("linkedin_url", ""),
        name=values.get("name", ""),
        company=values.get("company", ""),
    )
    url = canonical_url(source_url)
    return Contact(
        contact_uid="contact_" + token.split(":")[-1],
        name=values.get("name"),
        company=values.get("company"),
        title=values.get("title"),
        work_email=values.get("work_email"),
        linkedin_url=values.get("linkedin_url"),
        phone=values.get("phone"),
        contact_origin=origin,
        evidence_url=url,
        evidence_fetched_at=now,
        field_provenance={
            field: Provenance(source_id=origin, url=url, fetched_at=now, evidence=value)
            for field, value in values.items()
        },
        contactability_basis="EXPLICIT_RECRUITING_CHANNEL: " + plain_text(evidence)[:500],
        outreach_allowed=False,
        whatsapp_allowed=bool(
            values.get("phone")
            and re.search(r"whatsapp.{0,80}" + re.escape(values["phone"]), evidence, re.I)
        ),
        retention_expires_at=now + timedelta(days=30),
        suppression_token=token,
        hmac_key_version="v1",
    )


def rank_contacts(job: NormalizedJob, contacts: list[Contact], now: datetime) -> list[JobContact]:
    candidates: list[tuple[int, JobContact]] = []
    for contact in contacts:
        if (
            contact.do_not_contact
            or not contact.company
            or normalize_company(contact.company) != job.normalized_company
        ):
            continue
        if contact.retention_expires_at and contact.retention_expires_at <= now:
            continue
        if contact.contact_origin in USER_ORIGINS:
            if (
                contact.current_company_confidence != "USER_SUPPLIED_CURRENT"
                or now - contact.evidence_fetched_at > timedelta(days=180)
            ):
                continue
            strength = (
                0
                if contact.relationship_basis
                in {"CLOSE_CONNECTION", "CURRENT_COLLEAGUE", "FORMER_COLLEAGUE"}
                else 1
                if contact.relationship_basis == "FIRST_DEGREE_IMPORTED"
                else 2
            )
            candidate_type = (
                "CONFIRMED_REFERRER"
                if contact.relationship_basis == "AGREED_REFERRAL"
                else "WARM_CONNECTION"
            )
            why = "User-owned relationship with observed current employment at the company; willingness is not inferred."
        elif contact.contactability_basis.startswith("EXPLICIT_RECRUITING_CHANNEL:"):
            strength, candidate_type, why = (
                3,
                "PERMITTED_RECRUITER",
                "Approved official source explicitly publishes this recruiting channel.",
            )
        else:
            continue
        channel = (
            "LINKEDIN"
            if contact.linkedin_url
            else "EMAIL"
            if contact.work_email
            else "WHATSAPP"
            if contact.whatsapp_allowed
            else ""
        )
        candidates.append(
            (
                strength,
                JobContact(
                    job_uid=job.job_uid,
                    contact_uid=contact.contact_uid,
                    candidate_type=candidate_type,
                    why_this_contact=why,
                    recommended_channel=channel,
                    agreed=candidate_type == "CONFIRMED_REFERRER",
                ),
            )
        )
    result = [
        item[1] for item in sorted(candidates, key=lambda item: (item[0], item[1].contact_uid))
    ]
    for index, candidate in enumerate(result, 1):
        candidate.rank = index
    return result


def redact_contact(contact: Contact, key: bytes) -> Contact:
    token = contact.suppression_token or contact_token(
        key,
        email=contact.work_email or "",
        linkedin=contact.linkedin_url or "",
        name=contact.name or "",
        company=contact.company or "",
    )
    return Contact(
        contact_uid="suppressed_" + token.split(":")[-1],
        contact_origin="REDACTED",
        do_not_contact=True,
        suppression_token=token,
        hmac_key_version=contact.hmac_key_version,
        evidence_fetched_at=contact.evidence_fetched_at,
    )


def purge_expired(contacts: list[Contact], key: bytes, now: datetime) -> list[Contact]:
    return [
        redact_contact(contact, key)
        if contact.retention_expires_at and contact.retention_expires_at <= now
        else contact.model_copy(deep=True)
        for contact in contacts
    ]
