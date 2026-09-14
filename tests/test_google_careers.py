"""Visible-page contracts and regression protection against false job closures."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from test_sources import fake_wire, source

from career_radar.domain import FetchResult
from career_radar.fetch import FetchBudget, Response, collect
from career_radar.orchestration.pipeline import SourceBatch, _update_missing
from career_radar.security import MAX_BYTES, SecurityError
from career_radar.sources import ParseError, parse
from career_radar.sources.registry import check_policy

NOW = datetime.now(UTC)
URL = "https://www.google.com/about/careers/applications/jobs/results/?location=India&sort_by=date"
CARD = """<div><h3>Software Engineer</h3><span class="r0wTof">Bengaluru, India</span>
<div class="Xsxa1e"><h4>Minimum qualifications</h4><ul><li>2 years of Java experience.</li></ul></div>
<a aria-label="Learn more about Software Engineer" href="jobs/results/123-example-engineer"></a></div>"""
HTML = "<h1>Search Jobs</h1><p>1 jobs matched</p>" + CARD


def google(**kwargs):
    return source(
        "google_careers", url=URL, access_mode="PUBLIC_HTML_APPROVED", robots_allowed=True, **kwargs
    )


def parsed(html=HTML, url=URL):
    return parse(google(), FetchResult(source_id="synthetic", url=url, body=html, fetched_at=NOW))


def test_visible_jobs_preserve_evidence_and_deduplicate():
    jobs = parsed(HTML + CARD)
    assert len(jobs) == 1
    j = jobs[0]
    assert j.provider_job_id == "123"
    assert j.location == "Bengaluru, India"
    assert "2 years" in j.description
    assert j.posted_at_raw is None and j.updated_at_raw is None
    assert (
        j.url
        == "https://www.google.com/about/careers/applications/jobs/results/123-example-engineer"
    )
    assert j.metadata["coverage_scope"] == "FIRST_SEARCH_PAGE_ONLY"
    assert j.metadata["description_scope"] == "MINIMUM_QUALIFICATIONS_ONLY"
    assert j.official_link_state == "VERIFIED_OFFICIAL"
    assert parsed("<h1>Search Jobs</h1><p>0 jobs matched</p>") == []


@pytest.mark.parametrize(
    "html",
    [
        "<html>Sign in</html>",
        HTML.replace("1 jobs matched", "2 jobs matched"),
        HTML.replace("1 jobs matched", "0 jobs matched"),
        HTML.replace('class="r0wTof"', 'class="changed"'),
        HTML.replace('class="Xsxa1e"', 'class="changed"'),
        HTML.replace("<h3>Software Engineer</h3>", ""),
        HTML.replace("Learn more about Software Engineer", "Learn more about Wrong Title"),
        HTML.replace("jobs/results/123-example-engineer", "https://evil.example/jobs/123"),
        HTML + CARD.replace("Bengaluru, India", "Paris, France"),
        HTML + "<p>2 jobs matched</p>",
        "x" * (MAX_BYTES + 1),
    ],
)
def test_schema_changes_never_become_empty_success(html):
    with pytest.raises(ParseError):
        parsed(html)


@pytest.mark.parametrize(
    "url", [URL + "&page=2", URL + "&%70age=1", "https://evil.example/jobs", URL + "#fragment"]
)
def test_prohibited_or_unreviewed_paths_rejected_before_request(url):
    with pytest.raises(SecurityError, match="GOOGLE_FIRST_PAGE_ONLY"):
        check_policy(google(), url, NOW)
    with pytest.raises(ParseError, match="GOOGLE_FIRST_PAGE_ONLY"):
        parsed(url=url)


def test_first_page_is_bounded_and_never_follows_pagination():
    body = HTML + '<a href="?page=2">Next page</a>'
    calls = []

    def wire(url, *args):
        calls.append(url)
        return Response(200, {"content-type": "text/html"}, body.encode())

    result, jobs = collect(
        google(),
        FetchBudget(deadline=time.monotonic() + 60),
        wire=wire,
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    assert result.snapshot_complete and result.outcome == "SUCCESS_COMPLETE"
    assert len(jobs) == 1 and calls == [URL]
    assert result.listing_complete is False


@pytest.mark.parametrize("status", [403, 429])
def test_blocked_and_rate_limited_collection_do_not_complete(status):
    result, jobs = collect(
        google(),
        FetchBudget(),
        wire=fake_wire([Response(status, {"content-type": "text/html"}, b"blocked")]),
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    assert not result.snapshot_complete and not jobs


def test_jobs_leaving_discovery_window_are_never_marked_closed():
    appearances = {"a": {"source_id": "synthetic", "job_uid": "old", "active": True}}
    counters = {}
    for day in range(4):
        batch = SourceBatch(google(), [], outcome="SUCCESS_EMPTY", complete=True)
        _update_missing(batch, set(), appearances, counters, NOW + timedelta(days=day))
    assert appearances["a"]["active"] is True and not counters
    # An observed appearance still clears an old counter.
    counters["a"] = {"count": 2, "last_missing_at": NOW.isoformat()}
    _update_missing(batch, {"old"}, appearances, counters, NOW)
    assert counters["a"]["count"] == 0
