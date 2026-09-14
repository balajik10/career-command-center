"""Supported adapter entry points. Parsing has no network side effects."""

from career_radar.domain import FetchResult, RawJob, SourceDefinition
from career_radar.sources.google_careers import parse_google_careers
from career_radar.sources.parsers import (
    ParseError,
    parse_ashby,
    parse_feed,
    parse_greenhouse,
    parse_jsonld,
    parse_lever,
)

PARSERS = {
    "google_careers": parse_google_careers,
    "greenhouse": parse_greenhouse,
    "lever": parse_lever,
    "ashby": parse_ashby,
    "feed": parse_feed,
    "rss": parse_feed,
    "atom": parse_feed,
    "jsonld": parse_jsonld,
}


def parse(source: SourceDefinition, result: FetchResult) -> list[RawJob]:
    if result.source_id != source.source_id:
        raise ParseError("SOURCE_ID_MISMATCH")
    if result.status_code != 200 or result.error_code:
        raise ParseError("UNSUCCESSFUL_FETCH")
    if result.outcome == "VALIDATION_PROBE":
        raise ParseError("PROBE_EXTRACTION_FORBIDDEN")
    parser = PARSERS.get(source.provider)
    if parser is None:
        raise ParseError("UNSUPPORTED_PROVIDER")
    jobs = parser(
        result.body.encode(), source.source_id, source.company, result.url, result.fetched_at
    )
    for job in jobs:
        job.tenant = source.tenant
        if source.policy_state == "APPROVED" and source.access_mode in {
            "OFFICIAL_API",
            "PUBLIC_HTML_APPROVED",
        }:
            job.official_link_state = "VERIFIED_OFFICIAL"
    return jobs


__all__ = ["PARSERS", "ParseError", "parse"]
