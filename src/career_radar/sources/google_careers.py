"""Read the visible first search page; never paginate or execute site scripts."""

import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from career_radar.domain import RawJob
from career_radar.security import MAX_BYTES
from career_radar.sources.parsers import ParseError, _job

SEARCH_PATH = "/about/careers/applications/jobs/results/"
JOB_PATH = re.compile(r"^/about/careers/applications/jobs/results/(\d+)-[^/]+/?$")


def valid_search_url(url: str) -> bool:
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    return (
        parts.scheme == "https"
        and parts.netloc == "www.google.com"
        and parts.path.rstrip("/") == SEARCH_PATH.rstrip("/")
        and "page" not in query
        and not parts.fragment
    )


def parse_google_careers(
    content: bytes, source_id: str, company: str, source_url: str, fetched_at: datetime
) -> list[RawJob]:
    if len(content) > MAX_BYTES:
        raise ParseError("RESPONSE_TOO_LARGE")
    if not valid_search_url(source_url):
        raise ParseError("GOOGLE_FIRST_PAGE_ONLY")
    try:
        soup = BeautifulSoup(content.decode("utf-8"), "html.parser")
    except UnicodeDecodeError as exc:
        raise ParseError("INVALID_ENCODING") from exc
    for tag in soup.select("script, style"):
        tag.decompose()
    counts = re.findall(r"\b([\d,]+) jobs? matched\b", soup.get_text(" ", strip=True))
    if not counts or len(set(counts)) != 1:
        raise ParseError("GOOGLE_RESULT_COUNT_MISSING")
    total = int(counts[0].replace(",", ""))
    jobs: dict[str, RawJob] = {}
    for link in soup.select('a[aria-label^="Learn more about "][href]'):
        url = urljoin("https://www.google.com/about/careers/applications/", str(link["href"]))
        parts = urlsplit(url)
        match = JOB_PATH.fullmatch(parts.path)
        if parts.scheme != "https" or parts.netloc != "www.google.com" or not match:
            raise ParseError("GOOGLE_JOB_LINK_INVALID")
        card = next(
            (parent for parent in link.parents if isinstance(parent, Tag) and parent.find("h3")),
            None,
        )
        if card is None or len(card.find_all("h3")) != 1:
            raise ParseError("GOOGLE_CARD_MISSING")
        heading = card.find("h3")
        qualifications = card.select_one(".Xsxa1e")
        locations = list(dict.fromkeys(t.get_text(" ", strip=True) for t in card.select(".r0wTof")))
        if heading is None or qualifications is None or not locations:
            raise ParseError("GOOGLE_CARD_FIELDS_MISSING")
        title = heading.get_text(" ", strip=True)
        if str(link.get("aria-label")) != "Learn more about " + title:
            raise ParseError("GOOGLE_TITLE_MISMATCH")
        job = _job(
            {
                "id": match[1],
                "title": title,
                "company": company,
                "location": "; ".join(locations),
                "description": qualifications.get_text(" ", strip=True),
                "url": url.split("?", 1)[0],
            },
            source_id,
            "google_careers",
            company,
            source_url,
            fetched_at,
        )
        job.metadata.update(
            coverage_scope="FIRST_SEARCH_PAGE_ONLY",
            description_scope="MINIMUM_QUALIFICATIONS_ONLY",
            search_total=total,
        )
        if job.provider_job_id in jobs and jobs[job.provider_job_id] != job:
            raise ParseError("CONFLICTING_JOB_ID")
        jobs[job.provider_job_id] = job
    if len(jobs) != min(total, 20):
        raise ParseError("GOOGLE_VISIBLE_PAGE_INCOMPLETE")
    return list(jobs.values())
