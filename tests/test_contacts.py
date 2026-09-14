from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_core import NOW, job, profile

from career_radar.contacts import (
    contact_token,
    import_contacts,
    permitted_public_contact,
    purge_expired,
    rank_contacts,
    redact_contact,
)
from career_radar.domain import CandidateEvidence, Contact, OutreachDraft, Provenance
from career_radar.outreach import (
    approved_claims,
    can_contact,
    five_business_days,
    generate_drafts,
    generate_follow_up,
    validate_draft,
)
from career_radar.scoring import score_job

KEY = b"synthetic-test-key-32-bytes-only!!"


def contact(**kwargs: object) -> Contact:
    values = {
        "contact_uid": "synthetic-contact",
        "name": "Example Connection",
        "company": "Example Labs",
        "title": "Software Engineer",
        "linkedin_url": "https://www.linkedin.com/in/synthetic-person",
        "contact_origin": "LINKEDIN_CONNECTIONS_EXPORT",
        "evidence_fetched_at": NOW,
        "relationship_basis": "FIRST_DEGREE_IMPORTED",
        "current_company_confidence": "USER_SUPPLIED_CURRENT",
        "outreach_allowed": True,
        "field_provenance": {"linkedin_url": Provenance(source_id="USER_ENTERED", fetched_at=NOW)},
    }
    values.update(kwargs)
    return Contact.model_validate(values)


def test_linkedin_import_alias_preamble_duplicates_and_suppression(tmp_path: Path) -> None:
    path = tmp_path / "contacts.csv"
    path.write_text(
        "Notes:\nExported by the user\n\nFirst Name,Last Name,URL,Email Address,Company,Position\nExample,Connection,https://www.linkedin.com/in/synthetic-person,,Example Labs,Engineer\nExample,Connection,https://www.linkedin.com/in/synthetic-person,,Example Labs,Engineer\n"
    )
    contacts = import_contacts(path, "linkedin-connections", KEY, NOW)
    assert len(contacts) == 1 and contacts[0].name == "Example Connection"
    assert contacts[0].current_company_confidence == "USER_SUPPLIED_CURRENT"
    assert contacts[0].work_email is None and not contacts[0].outreach_allowed
    assert "company" in contacts[0].field_provenance
    assert rank_contacts(job(), contacts, NOW)[0].candidate_type == "WARM_CONNECTION"
    redacted = redact_contact(contacts[0], KEY)
    assert redacted.name is None and redacted.linkedin_url is None and redacted.do_not_contact
    assert (
        import_contacts(
            path, "linkedin-connections", KEY, NOW, suppressed_tokens={redacted.suppression_token}
        )
        == []
    )
    assert contacts[0].name is not None


def test_google_generic_import_and_invalid_fields(tmp_path: Path) -> None:
    google = tmp_path / "google.csv"
    google.write_text(
        "Name,Organization 1 - Name,Organization 1 - Title,E-mail 1 - Value,Phone 1 - Value\nExample Connection,Example Labs,Engineer,candidate@example.com,+12025550123\n"
    )
    parsed = import_contacts(google, "google-contacts", KEY, NOW)[0]
    assert (
        parsed.work_email == "candidate@example.com"
        and parsed.phone == "+12025550123"
        and not parsed.whatsapp_allowed
    )
    generic = tmp_path / "generic.csv"
    generic.write_text(
        "Name,Company,Title,Email,LinkedIn,Phone\nPast Person,Example Labs,Former Engineer,invalid,https://example.com/profile,12345\nUnknown Person,,,,,\n,,,,,\n"
    )
    result = import_contacts(generic, "generic", KEY, NOW)
    assert (
        result[0].current_company_confidence == "AMBIGUOUS_PAST"
        and result[0].work_email is None
        and result[0].linkedin_url is None
        and result[0].phone is None
    )
    assert result[1].current_company_confidence == "UNKNOWN"
    assert rank_contacts(job(), result, NOW) == []
    assert len(import_contacts(generic, "csv", KEY, NOW)) == 2
    with pytest.raises(ValueError):
        import_contacts(google, "unsupported", KEY, NOW)
    with pytest.raises(ValueError):
        import_contacts(google, "csv", KEY, datetime(2026, 1, 1))
    missing = tmp_path / "missing.csv"
    missing.write_text("unrecognized header")
    with pytest.raises(ValueError):
        import_contacts(missing, "csv", KEY, NOW)
    with missing.open("wb") as stream:
        stream.truncate(5_000_001)
    with pytest.raises(ValueError):
        import_contacts(missing, "csv", KEY, NOW)


def test_public_contact_requires_explicit_observed_recruiting_channel() -> None:
    options = {
        "key": KEY,
        "source_url": "https://example.com/careers",
        "now": NOW,
        "origin": "APPROVED_OFFICIAL_RECRUITING_PAGE",
        "approved": True,
    }
    public = permitted_public_contact(
        **options,
        evidence="For recruiting at Example Labs: careers@example.com",
        observed={
            "company": "Example Labs",
            "work_email": "careers@example.com",
            "name": "Invented Name",
        },
    )
    assert public and public.name is None and not public.outreach_allowed
    assert public.retention_expires_at == NOW + timedelta(days=30)
    assert rank_contacts(job(), [public], NOW)[0].candidate_type == "PERMITTED_RECRUITER"
    assert (
        permitted_public_contact(
            **options, evidence="Example Person enjoys sports", observed={"name": "Example Person"}
        )
        is None
    )
    assert (
        permitted_public_contact(
            **{**options, "approved": False},
            evidence="For recruiting: careers@example.com",
            observed={"work_email": "careers@example.com"},
        )
        is None
    )
    assert (
        permitted_public_contact(
            **options,
            evidence="Recruiting team Example Person",
            observed={"name": "Example Person"},
        )
        is None
    )
    evidence = "Example Person, Recruiter for Example Labs. Recruiting WhatsApp +12025550123. candidate@example.com"
    whatsapp = permitted_public_contact(
        **options,
        evidence=evidence,
        observed={
            "name": "Example Person",
            "company": "Example Labs",
            "title": "Recruiter",
            "phone": "+12025550123",
            "work_email": "candidate@example.com",
        },
    )
    assert whatsapp and whatsapp.whatsapp_allowed and whatsapp.name == "Example Person"
    personal_phone = permitted_public_contact(
        **options,
        evidence="+12025550123 is personal. Recruiting email: candidate@example.com",
        observed={"phone": "+12025550123", "work_email": "candidate@example.com"},
    )
    assert personal_phone and personal_phone.phone is None
    role_address = permitted_public_contact(
        **options,
        evidence="Name Title recruiting careers@example.com",
        observed={"name": "Name", "title": "Title", "work_email": "careers@example.com"},
    )
    assert role_address and role_address.name is None and role_address.title is None


def test_rank_and_expiry_privacy() -> None:
    people = [
        contact(contact_uid="a", relationship_basis="CLOSE_CONNECTION"),
        contact(contact_uid="b", relationship_basis="AGREED_REFERRAL"),
        contact(contact_uid="c", relationship_basis="ALUMNI"),
        contact(contact_uid="bad", do_not_contact=True),
        contact(contact_uid="other", company="Other"),
        contact(contact_uid="past", current_company_confidence="AMBIGUOUS_PAST"),
        contact(contact_uid="stale", evidence_fetched_at=NOW - timedelta(days=181)),
        contact(contact_uid="expired", retention_expires_at=NOW),
        contact(contact_uid="public", contact_origin="PUBLIC_UNKNOWN"),
    ]
    ranked = rank_contacts(job(), people, NOW)
    assert len(ranked) == 3 and ranked[0].contact_uid == "a"
    assert (
        next(candidate for candidate in ranked if candidate.contact_uid == "b").candidate_type
        == "CONFIRMED_REFERRER"
    )
    assert [candidate.rank for candidate in ranked] == [1, 2, 3]
    email = contact(linkedin_url=None, work_email="candidate@example.com")
    assert rank_contacts(job(), [email], NOW)[0].recommended_channel == "EMAIL"
    phone = contact(linkedin_url=None, phone="+12025550123", whatsapp_allowed=True)
    assert rank_contacts(job(), [phone], NOW)[0].recommended_channel == "WHATSAPP"
    assert (
        rank_contacts(job(), [phone.model_copy(update={"whatsapp_allowed": False})], NOW)[
            0
        ].recommended_channel
        == ""
    )
    redacted = redact_contact(contact(), KEY)
    assert redacted.name is None and redacted.field_provenance == {}
    expired, unchanged = purge_expired([people[-2], people[0]], KEY, NOW)
    assert expired.contact_origin == "REDACTED" and unchanged.name == people[0].name
    assert contact_token(KEY, email="Candidate@Example.com") == contact_token(
        KEY, email="candidate@example.com"
    )


def test_drafts_channel_provenance_limits_and_idempotency() -> None:
    item, p = job(), profile()
    score = score_job(item, p, NOW)
    person = contact()
    drafts = generate_drafts(item, score, p, NOW, person)
    assert {draft.channel for draft in drafts} == {
        "APPLICATION_NOTE",
        "LINKEDIN_MESSAGE",
        "LINKEDIN_CONNECTION",
    }
    assert all(
        draft.contact_uid == person.contact_uid
        for draft in drafts
        if draft.channel != "APPLICATION_NOTE"
    )
    assert all(
        "api" in draft.evidence_ids for draft in drafts if draft.channel != "LINKEDIN_CONNECTION"
    )
    for draft in drafts:
        validate_draft(draft)
        assert draft.word_count == len(draft.body.split())
    assert generate_drafts(item, score, p, NOW, person, [draft.draft_id for draft in drafts]) == []
    assert [draft.body for draft in drafts] == [
        draft.body for draft in generate_drafts(item, score, p, NOW, person)
    ]
    changed = item.model_copy(update={"material_change_sequence": 1})
    assert generate_drafts(changed, score, p, NOW, person, [draft.draft_id for draft in drafts])
    assert {draft.channel for draft in generate_drafts(item, score, p, NOW)} == {"APPLICATION_NOTE"}
    assert generate_drafts(item, score.model_copy(update={"action_priority": "P2"}), p, NOW) == []
    assert generate_drafts(item, score, p.model_copy(update={"evidence": []}), NOW) == []
    no_channel = person.model_copy(update={"field_provenance": {}})
    assert {draft.channel for draft in generate_drafts(item, score, p, NOW, no_channel)} == {
        "APPLICATION_NOTE"
    }
    limited = generate_drafts(item, score, p, NOW, person, connection_limit=10)
    assert "LINKEDIN_CONNECTION" not in {draft.channel for draft in limited}


def test_email_whatsapp_and_stranger_inquiry() -> None:
    item, p = job(), profile()
    score = score_job(item, p, NOW)
    provenance = {
        field: Provenance(source_id="USER_ENTERED", fetched_at=NOW)
        for field in ("work_email", "phone")
    }
    person = contact(
        linkedin_url=None,
        work_email="candidate@example.com",
        phone="+12025550123",
        whatsapp_allowed=True,
        field_provenance=provenance,
    )
    drafts = generate_drafts(item, score, p, NOW, person)
    assert {draft.channel for draft in drafts} == {"APPLICATION_NOTE", "EMAIL", "WHATSAPP"}
    assert next(draft for draft in drafts if draft.channel == "EMAIL").subject
    assert len(next(draft for draft in drafts if draft.channel == "WHATSAPP").evidence_ids) == 1
    for draft in drafts:
        validate_draft(draft)
    stranger = person.model_copy(
        update={
            "name": None,
            "contact_origin": "APPROVED_OFFICIAL_RECRUITING_PAGE",
            "contactability_basis": "EXPLICIT_RECRUITING_CHANNEL: approved job",
        }
    )
    strangers = generate_drafts(item, score, p, NOW, stranger)
    assert all(
        "referring" not in draft.body and "consider a referral" not in draft.body
        for draft in strangers
    )
    gated = person.model_copy(update={"whatsapp_allowed": False})
    assert "WHATSAPP" not in {
        draft.channel for draft in generate_drafts(item, score, p, NOW, gated)
    }


def test_approved_atomic_claims_only() -> None:
    safe = CandidateEvidence(
        evidence_id="safe",
        resume_verbatim="Private full text must never be used",
        outreach_safe_paraphrase="Unapproved text",
        allowed_atomic_claims=["Invented performance", "Built a small API."],
        forbidden_extrapolations=["Invented"],
        allowed_in_outreach=True,
    )
    blocked = safe.model_copy(
        update={
            "evidence_id": "unsafe",
            "allowed_atomic_claims": ["10x faster"],
            "outreach_safe_paraphrase": "Do not use this",
        }
    )
    claims, ids = approved_claims([safe, blocked])
    assert claims == ["Built a small API."] and ids == ["safe"]
    assert approved_claims([safe.model_copy(update={"allowed_in_outreach": False})]) == ([], [])
    assert approved_claims([safe, safe]) == (["Built a small API."], ["safe"])
    assert approved_claims([safe.model_copy(update={"allowed_atomic_claims": ["word " * 33]})]) == (
        [],
        [],
    )


@pytest.mark.parametrize(
    ("channel", "body", "subject"),
    [
        ("EMAIL", "Too short", ""),
        ("LINKEDIN_CONNECTION", "x" * 251, ""),
        ("FOLLOW_UP", "Hello [Name]", ""),
        ("FOLLOW_UP", "I made it 10x faster", ""),
        ("FOLLOW_UP", "Valid text", "s" * 61),
    ],
)
def test_invalid_draft_rejected(channel: str, body: str, subject: str) -> None:
    with pytest.raises(ValueError):
        validate_draft(
            OutreachDraft(
                draft_id="id",
                job_uid="job",
                channel=channel,
                purpose="test",
                body=body,
                subject=subject,
            )
        )


def test_cooldown_follow_up_and_stop_rules() -> None:
    person = contact()
    assert can_contact(person, NOW)
    assert not can_contact(person.model_copy(update={"do_not_contact": True}), NOW)
    assert not can_contact(person.model_copy(update={"outreach_allowed": False}), NOW)
    assert not can_contact(person.model_copy(update={"reply_state": "REPLIED"}), NOW)
    assert not can_contact(person.model_copy(update={"retention_expires_at": NOW}), NOW)
    assert not can_contact(person.model_copy(update={"contact_origin": "PUBLIC_UNKNOWN"}), NOW)
    assert not can_contact(
        person.model_copy(update={"last_contacted_at": NOW - timedelta(days=2)}), NOW
    )
    assert can_contact(
        person.model_copy(update={"last_contacted_at": NOW - timedelta(days=2)}),
        NOW,
        override_reason="Owner explicitly requested a new role inquiry",
    )
    assert can_contact(
        person.model_copy(update={"last_contacted_at": NOW - timedelta(days=31)}), NOW
    )
    assert not can_contact(person, NOW, follow_up=True)
    sent = person.model_copy(update={"last_contacted_at": NOW - timedelta(days=8)})
    assert can_contact(sent, NOW, follow_up=True)
    assert generate_follow_up(job(), sent, NOW) is not None
    assert generate_follow_up(job(), sent.model_copy(update={"follow_up_count": 1}), NOW) is None
    assert (
        generate_follow_up(
            job(), sent.model_copy(update={"last_contacted_at": NOW - timedelta(days=1)}), NOW
        )
        is None
    )
    friday = NOW - timedelta(days=1)
    assert five_business_days(friday) == friday + timedelta(days=7)
    with pytest.raises(ValueError):
        can_contact(person, datetime(2026, 1, 1))
