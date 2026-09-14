"""Deterministic parsers for documented public sources and user-owned imports."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from career_radar.domain import RawJob
from career_radar.security import (
    MAX_BYTES,
    SecurityError,
    canonical_url,
    plain_text,
    private_token,
    safe_xml,
)

PARSER_VERSION = "1.0.0"


class ParseError(ValueError):
    """Source schema changed or input is malformed; quarantine the snapshot."""


def _string(value: Any, field: str, *, optional: bool = False) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ParseError(f"INVALID_{field.upper()}")
    result = str(value).strip()
    if not result and not optional:
        raise ParseError(f"MISSING_{field.upper()}")
    return result


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ParseError("EXPECTED_OBJECT")
    return value


def _json(content: bytes) -> Any:
    if len(content) > MAX_BYTES:
        raise ParseError("RESPONSE_TOO_LARGE")
    try:
        return json.loads(content)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ParseError("MALFORMED_JSON") from exc


def _unique(jobs: list[RawJob]) -> list[RawJob]:
    return list({(job.source_id, job.provider_job_id): job for job in jobs}.values())


def _job(
    data: dict[str, Any],
    source_id: str,
    provider: str,
    company: str,
    source_url: str,
    fetched_at: datetime,
) -> RawJob:
    try:
        url = canonical_url(_string(data.get("url"), "url"))
        apply_url = canonical_url(_string(data.get("apply_url") or url, "apply_url"))
    except SecurityError as exc:
        raise ParseError("INVALID_JOB_URL") from exc
    return RawJob(
        source_id=source_id,
        provider=provider,
        provider_job_id=_string(data.get("id") or url, "id"),
        title=plain_text(_string(data.get("title"), "title")),
        company=plain_text(_string(data.get("company") or company, "company")),
        location=plain_text(_string(data.get("location"), "location", optional=True)),
        description=plain_text(_string(data.get("description"), "description", optional=True)),
        url=url,
        apply_url=apply_url,
        fetched_at=fetched_at,
        posted_at_raw=_string(data.get("posted_at"), "posted_at", optional=True) or None,
        parser_version=PARSER_VERSION,
        updated_at_raw=_string(data.get("updated_at"), "updated_at", optional=True) or None,
        deadline_raw=_string(data.get("deadline"), "deadline", optional=True) or None,
        salary_text=_string(data.get("salary_text"), "salary_text", optional=True),
        requisition_id=_string(data.get("requisition_id"), "requisition_id", optional=True),
        metadata={
            "source_url": source_url,
            "date_basis": data.get("date_basis", "UNKNOWN"),
            "salary_text": data.get("salary_text", ""),
        },
    )


def parse_greenhouse(
    content: bytes, source_id: str, company: str, source_url: str, fetched_at: datetime
) -> list[RawJob]:
    obj = _object(_json(content))
    records = obj.get("jobs") if "jobs" in obj else [obj] if "id" in obj else None
    if not isinstance(records, list):
        raise ParseError("MISSING_JOBS_ARRAY")
    jobs = []
    seen: dict[str, dict[str, Any]] = {}
    for raw in records:
        row = _object(raw)
        external_id = _string(row.get("id"), "id")
        if external_id in seen and seen[external_id] != row:
            raise ParseError("CONFLICTING_JOB_ID")
        seen[external_id] = row
        location = _object(row.get("location", {}))
        jobs.append(
            _job(
                {
                    "id": row.get("id"),
                    "title": row.get("title"),
                    "location": location.get("name"),
                    "description": row.get("content"),
                    "url": row.get("absolute_url"),
                    "posted_at": row.get("first_published"),
                    "updated_at": row.get("updated_at"),
                    "deadline": row.get("application_deadline"),
                    "requisition_id": row.get("requisition_id"),
                    "date_basis": "FIRST_PUBLISHED" if row.get("first_published") else "UNKNOWN",
                    "salary_text": json.dumps(row["pay_input_ranges"])
                    if row.get("pay_input_ranges")
                    else "",
                },
                source_id,
                "greenhouse",
                company,
                source_url,
                fetched_at,
            )
        )
    unique = _unique(jobs)
    if "jobs" in obj and "meta" in obj:
        metadata = _object(obj["meta"])
        if "total" in metadata:
            total = metadata["total"]
            if isinstance(total, bool) or not isinstance(total, int) or total != len(unique):
                raise ParseError("INCOMPLETE_GREENHOUSE_INVENTORY")
    return unique


def parse_lever(
    content: bytes, source_id: str, company: str, source_url: str, fetched_at: datetime
) -> list[RawJob]:
    records = _json(content)
    if not isinstance(records, list):
        raise ParseError("EXPECTED_POSTINGS_ARRAY")
    jobs = []
    for raw in records:
        row = _object(raw)
        categories = _object(row.get("categories", {}))
        sections = row.get("lists", [])
        if not isinstance(sections, list):
            raise ParseError("INVALID_LISTS")
        description = _string(
            row.get("descriptionPlain") or row.get("description"), "description", optional=True
        )
        for section in sections:
            item = _object(section)
            description += (
                " "
                + _string(item.get("text"), "text", optional=True)
                + " "
                + _string(item.get("content"), "content", optional=True)
            )
        jobs.append(
            _job(
                {
                    "id": row.get("id"),
                    "title": row.get("text"),
                    "location": categories.get("location"),
                    "description": description,
                    "url": row.get("hostedUrl"),
                    "apply_url": row.get("applyUrl"),
                    "salary_text": json.dumps(row["salaryRange"]) if row.get("salaryRange") else "",
                },
                source_id,
                "lever",
                company,
                source_url,
                fetched_at,
            )
        )
    return _unique(jobs)


def parse_ashby(
    content: bytes, source_id: str, company: str, source_url: str, fetched_at: datetime
) -> list[RawJob]:
    obj = _object(_json(content))
    records = obj.get("jobs")
    if not isinstance(records, list):
        raise ParseError("MISSING_JOBS_ARRAY")
    jobs = []
    for raw in records:
        row = _object(raw)
        if "isListed" not in row or not isinstance(row["isListed"], bool):
            raise ParseError("MISSING_LISTED_FLAG")
        if not row["isListed"]:
            continue
        compensation = _object(row.get("compensation") or {})
        jobs.append(
            _job(
                {
                    "id": row.get("jobUrl"),
                    "title": row.get("title"),
                    "location": row.get("location"),
                    "description": row.get("descriptionPlain") or row.get("descriptionHtml"),
                    "url": row.get("jobUrl"),
                    "apply_url": row.get("applyUrl"),
                    "posted_at": row.get("publishedAt"),
                    "date_basis": "LAST_PUBLISHED",
                    "salary_text": compensation.get("scrapeableCompensationSalarySummary", ""),
                },
                source_id,
                "ashby",
                company,
                source_url,
                fetched_at,
            )
        )
    return _unique(jobs)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(
    content: bytes, source_id: str, company: str, source_url: str, fetched_at: datetime
) -> list[RawJob]:
    root = safe_xml(content)
    if _local(root.tag) not in {"rss", "feed", "RDF"}:
        raise ParseError("UNKNOWN_FEED_ROOT")
    jobs = []
    for entry in root.iter():
        if _local(entry.tag) not in {"item", "entry"}:
            continue
        fields: dict[str, str] = {}
        for child in entry:
            name = _local(child.tag)
            if name == "link" and child.attrib.get("rel", "alternate") == "alternate":
                fields["link"] = child.attrib.get("href") or (child.text or "")
            elif name != "link":
                fields[name] = "".join(child.itertext())
        jobs.append(
            _job(
                {
                    "id": fields.get("guid") or fields.get("id") or fields.get("link"),
                    "title": fields.get("title"),
                    "url": fields.get("link"),
                    "description": fields.get("description")
                    or fields.get("content")
                    or fields.get("summary"),
                    "posted_at": fields.get("pubDate") or fields.get("published"),
                    "date_basis": "FEED_PUBLISHED",
                },
                source_id,
                "feed",
                company,
                source_url,
                fetched_at,
            )
        )
    return _unique(jobs)


class _JSONLD(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.active = False
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and (dict(attrs).get("type") or "").lower() == "application/ld+json":
            self.active = True
            self.blocks.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.active = False

    def handle_data(self, data: str) -> None:
        if self.active:
            self.blocks[-1] += data


def parse_jsonld(
    content: bytes, source_id: str, company: str, source_url: str, fetched_at: datetime
) -> list[RawJob]:
    if len(content) > MAX_BYTES:
        raise ParseError("RESPONSE_TOO_LARGE")
    parser = _JSONLD()
    try:
        parser.feed(content.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ParseError("INVALID_ENCODING") from exc
    pending: list[Any] = [_json(block.encode()) for block in parser.blocks]
    jobs = []
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
            continue
        obj = _object(value)
        if "@graph" in obj:
            graph = obj["@graph"]
            if not isinstance(graph, list):
                raise ParseError("INVALID_GRAPH")
            pending.extend(graph)
        types = obj.get("@type", [])
        if types != "JobPosting" and (not isinstance(types, list) or "JobPosting" not in types):
            continue
        org = _object(obj.get("hiringOrganization", {}))
        location = obj.get("jobLocation", {})
        locations = location if isinstance(location, list) else [location]
        names = []
        for item in locations:
            address = _object(_object(item).get("address", {}))
            names.append(
                ", ".join(
                    str(address[field])
                    for field in ("addressLocality", "addressRegion", "addressCountry")
                    if address.get(field)
                )
            )
        identifier = obj.get("identifier")
        if isinstance(identifier, dict):
            identifier = identifier.get("value")
        jobs.append(
            _job(
                {
                    "id": identifier or obj.get("url") or source_url,
                    "title": obj.get("title"),
                    "company": org.get("name"),
                    "location": "; ".join(names),
                    "description": obj.get("description"),
                    "url": obj.get("url") or source_url,
                    "posted_at": obj.get("datePosted"),
                    "date_basis": "DATE_POSTED",
                    "salary_text": json.dumps(obj["baseSalary"]) if obj.get("baseSalary") else "",
                },
                source_id,
                "jsonld",
                company,
                source_url,
                fetched_at,
            )
        )
    return _unique(jobs)


def parse_sitemap(content: bytes, source_url: str) -> list[str]:
    root = safe_xml(content)
    if _local(root.tag) != "urlset":
        raise ParseError("SITEMAP_INDEX_REQUIRES_EXPLICIT_SOURCE")
    origin = urlsplit(source_url).netloc
    urls = []
    for node in root:
        for child in node:
            if _local(child.tag) == "loc" and child.text:
                url = canonical_url(child.text)
                if urlsplit(url).netloc != origin:
                    raise ParseError("SITEMAP_CROSS_ORIGIN")
                urls.append(url)
    return sorted(set(urls))


def parse_import(
    content: bytes, source_id: str, fetched_at: datetime, format: str = "json"
) -> list[RawJob]:
    if len(content) > MAX_BYTES:
        raise ParseError("IMPORT_TOO_LARGE")
    if format == "json":
        records = _json(content)
    elif format in {"csv", "tsv"}:
        records = list(
            csv.DictReader(
                io.StringIO(content.decode("utf-8-sig")), delimiter="\t" if format == "tsv" else ","
            )
        )
    else:
        raise ParseError("UNSUPPORTED_IMPORT_FORMAT")
    if not isinstance(records, list):
        raise ParseError("EXPECTED_JOBS_ARRAY")
    return _unique(
        [_job(_object(row), source_id, "manual", "", "user-import", fetched_at) for row in records]
    )


class _EmailLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.href = ""
        self.label = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.href = dict(attrs).get("href") or ""
            self.label = ""

    def handle_data(self, data: str) -> None:
        if self.href:
            self.label += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.href:
            self.links.append((self.href, self.label))
            self.href = ""


def parse_eml(
    content: bytes,
    source_id: str,
    company: str,
    fetched_at: datetime,
    privacy_key: bytes,
    allowed_senders: set[str],
) -> tuple[str, list[RawJob]]:
    if len(content) > MAX_BYTES:
        raise ParseError("EMAIL_TOO_LARGE")
    message = BytesParser(policy=policy.default).parsebytes(content)
    sender = str(message.get("From", ""))
    from email.utils import parseaddr

    if parseaddr(sender)[1].lower() not in {value.lower() for value in allowed_senders}:
        raise ParseError("SENDER_NOT_ALLOWLISTED")
    token = private_token(privacy_key, "eml", content)
    body = message.get_body(preferencelist=("html", "plain"))
    if body is None:
        return token, []
    text = body.get_content()
    if not isinstance(text, str):
        raise ParseError("EMAIL_BODY_NOT_TEXT")
    parser = _EmailLinks()
    parser.feed(text)
    jobs = []
    for url, label in parser.links:
        if not label.strip():
            continue
        try:
            canonical_url(url)
        except SecurityError:
            continue
        jobs.append(
            _job(
                {"id": url, "title": label, "company": company, "url": url},
                source_id,
                "email",
                company,
                "eml:" + token,
                fetched_at,
            )
        )
    return token, _unique(jobs)
