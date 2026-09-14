"""Connector contracts, malformed documents, and fake-wire transport isolation."""

import gzip
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from career_radar.domain import FetchResult, SourceDefinition
from career_radar.fetch import (
    FetchBudget,
    Response,
    _PinnedHTTPS,
    collect,
    endpoint,
    fetch,
    greenhouse_listing_version,
    retry_after_seconds,
    wire_get,
)
from career_radar.security import MAX_BYTES, SecurityError
from career_radar.sources import ParseError, parse
from career_radar.sources.parsers import parse_eml, parse_import, parse_sitemap

FIXTURES = Path("tests/fixtures")
NOW = datetime.now(UTC)


def source(provider="greenhouse", **kwargs):
    values = {
        "source_id": "synthetic",
        "company": "Example Systems",
        "provider": provider,
        "url": "https://jobs.example.com/api",
        "enabled": True,
        "access_mode": "OFFICIAL_API",
        "policy_state": "APPROVED",
        "coverage_state": "VERIFIED_ACTIVE",
        "cost_class": "free",
        "policy_reviewed_at": NOW - timedelta(days=1),
        "policy_review_due_at": NOW + timedelta(days=30),
        "human_reviewer": "Example reviewer",
        "policy_evidence_url": "https://jobs.example.com/terms",
        "policy_evidence_hash": "fixture-observed",
    }
    return SourceDefinition.model_validate(values | kwargs)


def result(body, **kwargs):
    return FetchResult(
        source_id="synthetic", url="https://jobs.example.com/api", body=body, **kwargs
    )


def fake_wire(responses):
    iterator = iter(responses)

    def wire(url, address, headers, timeout, max_bytes):
        value = next(iterator)
        if isinstance(value, Exception):
            raise value
        return value

    return wire


def response(body=b'{"jobs":[]}', status=200, **headers):
    return Response(status, {"content-type": "application/json"} | headers, body)


def get(source_row, responses, **kwargs):
    return fetch(
        source_row,
        FetchBudget(),
        wire=fake_wire(responses),
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
        **kwargs,
    )


@pytest.mark.parametrize(
    "provider,extension",
    [
        ("greenhouse", "json"),
        ("lever", "json"),
        ("ashby", "json"),
        ("feed", "xml"),
        ("jsonld", "html"),
    ],
)
def test_every_supported_parser_success_empty_duplicate_schema_dates_size(provider, extension):
    data = (FIXTURES / f"{provider}_success.{extension}").read_text()
    jobs = parse(source(provider), result(data))
    assert len(jobs) == 1 and jobs[0].title and jobs[0].company and jobs[0].url
    assert jobs[0].parser_version and jobs[0].fetched_at.tzinfo
    assert (
        parse(source(provider), result((FIXTURES / f"{provider}_empty.{extension}").read_text()))
        == []
    )
    if extension == "json":
        assert (
            len(
                parse(
                    source(provider), result((FIXTURES / f"{provider}_duplicate.json").read_text())
                )
            )
            == 1
        )
        assert (
            len(
                parse(
                    source(provider),
                    result((FIXTURES / f"{provider}_invalid_date.json").read_text()),
                )
            )
            == 1
        )
        with pytest.raises(ParseError):
            parse(source(provider), result((FIXTURES / f"{provider}_schema.json").read_text()))
    else:
        with pytest.raises((ParseError, SecurityError)):
            parse(
                source(provider),
                result(
                    "<broken>"
                    if provider == "feed"
                    else '<script type="application/ld+json">broken</script>'
                ),
            )
    with pytest.raises((ParseError, SecurityError)):
        parse(source(provider), result("x" * (MAX_BYTES + 1)))
    for status in (403, 429):
        with pytest.raises(ParseError, match="UNSUCCESSFUL"):
            parse(source(provider), result(data, status_code=status))


def test_dates_and_listed_semantics():
    gh = parse(source(), result((FIXTURES / "greenhouse_detail.json").read_text()))[0]
    assert gh.posted_at_raw == "2026-09-14T01:00:00Z"
    assert gh.updated_at_raw == "2026-09-14T01:05:00Z"
    lever = parse(source("lever"), result((FIXTURES / "lever_success.json").read_text()))[0]
    assert lever.posted_at_raw is None  # undocumented createdAt is deliberately ignored
    ashby = json.loads((FIXTURES / "ashby_success.json").read_text())
    ashby["jobs"][0]["isListed"] = False
    assert parse(source("ashby"), result(json.dumps(ashby))) == []
    atom = parse(source("atom"), result((FIXTURES / "atom_success.xml").read_text()))
    assert len(atom) == 1 and atom[0].posted_at_raw


def test_parser_guards_and_variants():
    with pytest.raises(ParseError, match="SOURCE_ID"):
        parse(source(), FetchResult(source_id="wrong", url="https://jobs.example.com/"))
    with pytest.raises(ParseError, match="PROBE_EXTRACTION"):
        parse(source(), result("{}", outcome="VALIDATION_PROBE"))
    with pytest.raises(ParseError, match="UNSUPPORTED"):
        parse(source("workday"), result("{}"))
    cases = [
        ("greenhouse", "[]"),
        ("greenhouse", "{}"),
        ("greenhouse", "{"),
        ("lever", "{}"),
        ("lever", "[true]"),
        ("lever", "[{}]"),
        ("ashby", "{}"),
        ("jsonld", '<script type="application/ld+json">{"@graph":1}</script>'),
        ("feed", "<html/>"),
    ]
    for provider, body in cases:
        with pytest.raises(ParseError):
            parse(source(provider), result(body))
    raw = json.loads((FIXTURES / "greenhouse_detail.json").read_text())
    for change in [{"title": ""}, {"absolute_url": "file:///x"}, {"title": True}]:
        with pytest.raises(ParseError):
            parse(source(), result(json.dumps(raw | change)))
    payload = (
        '<script type="application/ld+json">[{"@type":"Organization"},'
        + json.dumps(
            {
                "@type": ["JobPosting"],
                "title": "Java Engineer",
                "jobLocation": [{"address": {"addressCountry": "India"}}],
                "identifier": "a",
                "description": "SQL",
            }
        )
        + "]</script>"
    )
    assert len(parse(source("jsonld"), result(payload))) == 1


def test_sitemap_boundaries():
    data = (FIXTURES / "sitemap_success.xml").read_bytes()
    assert parse_sitemap(data, "https://jobs.example.com/sitemap.xml") == [
        "https://jobs.example.com/101"
    ]
    assert parse_sitemap(b"<urlset/>", "https://jobs.example.com/") == []
    for data in [
        b"<sitemapindex/>",
        b"<urlset><url><loc>https://other.example.com/job</loc></url></urlset>",
    ]:
        with pytest.raises(ParseError):
            parse_sitemap(data, "https://jobs.example.com/")


@pytest.mark.parametrize(
    "format,content",
    [
        (
            "json",
            b'[{"id":"1","company":"Example","title":"Software Engineer","url":"https://jobs.example.com/1"}]',
        ),
        ("csv", b"id,company,title,url\n1,Example,Software Engineer,https://jobs.example.com/1\n"),
        (
            "tsv",
            b"id\tcompany\ttitle\turl\n1\tExample\tSoftware Engineer\thttps://jobs.example.com/1\n",
        ),
    ],
)
def test_manual_import_formats(format, content):
    jobs = parse_import(content, "import", NOW, format)
    assert len(jobs) == 1 and jobs[0].provider == "manual"


def test_import_bad_inputs():
    for content, format in [(b"{}", "json"), (b"[]", "xlsx"), (b"x" * (MAX_BYTES + 1), "csv")]:
        with pytest.raises(ParseError):
            parse_import(content, "import", NOW, format)


def test_email_local_only_minimal_hmac_and_idempotency():
    data = (FIXTURES / "alert_synthetic.eml").read_bytes()
    token, jobs = parse_eml(
        data, "email", "Example", NOW, bytes(range(32)), {"candidate@example.com"}
    )
    assert len(jobs) == 1
    assert jobs[0].url == "https://www.linkedin.com/jobs/view/123"
    assert jobs[0].metadata["source_url"] == "eml:" + token
    token2, jobs2 = parse_eml(
        data, "email", "Example", NOW, bytes(range(32)), {"candidate@example.com"}
    )
    assert token == token2 and jobs == jobs2
    with pytest.raises(ParseError, match="SENDER"):
        parse_eml(data, "email", "Example", NOW, bytes(range(32)), set())
    with pytest.raises(ParseError, match="EMAIL_TOO_LARGE"):
        parse_eml(b"x" * (MAX_BYTES + 1), "email", "Example", NOW, bytes(range(32)), set())
    data = b"From: candidate@example.com\nContent-Type: multipart/mixed; boundary=a\n\n--a--\n"
    assert (
        parse_eml(data, "email", "Example", NOW, bytes(range(32)), {"candidate@example.com"})[1]
        == []
    )


@pytest.mark.parametrize(
    "status,outcome",
    [
        (401, "POLICY_BLOCKED"),
        (403, "POLICY_BLOCKED"),
        (404, "FAILED_PERMANENT"),
        (429, "BACKOFF"),
        (304, "NO_CHANGE"),
    ],
)
def test_transport_stops(status, outcome):
    fetched = get(source(), [response(status=status)])
    assert fetched.outcome == outcome
    assert fetched.request_count == 1


def test_transport_retry_conditional_and_changed_headers():
    seen = []

    def wire(url, address, headers, timeout, max_bytes):
        seen.append((url, address, headers))
        return response(status=503) if len(seen) == 1 else response()

    fetched = fetch(
        source(etag="revision", last_modified="date"),
        FetchBudget(),
        wire=wire,
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    assert fetched.status_code == 200 and fetched.request_count == 2
    assert seen[0][2]["If-None-Match"] == "revision" and seen[0][2]["If-Modified-Since"] == "date"
    assert all(row[1] == "1.1.1.1" for row in seen)
    assert (
        get(source(), [response(status=429, **{"retry-after": "0"}), response()]).status_code == 200
    )
    assert get(source(), [OSError(), OSError(), OSError()]).outcome == "FAILED_TRANSIENT"
    assert get(source(), [response(status=500)] * 3).error_code == "RETRY_EXHAUSTED"


def test_transport_policy_dns_redirect_content_and_deadline():
    for row in [source(enabled=False), source(policy_state="PENDING_REVIEW")]:
        assert get(row, []).outcome == "POLICY_SKIPPED"
    result_bad = fetch(
        source(), FetchBudget(), resolver=lambda _: ["127.0.0.1"], sleep=lambda _: None
    )
    assert result_bad.error_code == "PRIVATE_ADDRESS"
    redirect = response(status=302, location="http://127.0.0.1/private")
    assert get(source(), [redirect]).error_code == "UNAPPROVED_HOST"
    redirect = response(status=302, location="/new")
    assert get(source(), [redirect, response()]).status_code == 200
    assert get(source(), [response(status=302)]).outcome == "FAILED_PERMANENT"
    assert get(source(), [redirect] * 6).error_code == "REDIRECT_LIMIT_OR_PROBE_REDIRECT"
    assert get(source(), [response(**{"content-type": "text/html"})]).error_code == "CONTENT_TYPE"
    assert get(source(), [response(b"x" * (MAX_BYTES + 1))]).error_code == "RESPONSE_TOO_LARGE"
    assert get(source(), [response(b"\xff")]).error_code == "INVALID_ENCODING"
    assert get(source(), [response(b"verify you are human")]).outcome == "POLICY_BLOCKED"
    assert get(source(), [response(b"sign in to continue")]).outcome == "LOGIN_REQUIRED"
    assert fetch(source(), FetchBudget(deadline=time.monotonic() + 1)).outcome == "DEFERRED_BUDGET"
    with pytest.raises(SecurityError, match="BUDGET"):
        FetchBudget(max_requests=0).claim()


def test_probe_never_returns_content_or_extracts_jobs():
    row = source(enabled=False, policy_state="PENDING_REVIEW")
    output = get(
        row, [response(b'{"jobs":["data"]}')], purpose="VALIDATION_PROBE", user_initiated=True
    )
    assert output.body == "" and output.outcome == "VALIDATION_PROBE"
    assert row.policy_state == "PENDING_REVIEW" and not row.enabled
    assert (
        get(
            row,
            [response(status=302, location="/other")],
            purpose="VALIDATION_PROBE",
            user_initiated=True,
        ).error_code
        == "REDIRECT_LIMIT_OR_PROBE_REDIRECT"
    )


def test_retry_after_and_endpoints():
    assert retry_after_seconds("5", NOW) == 5
    assert retry_after_seconds("-1", NOW) is None
    assert retry_after_seconds("garbage", NOW) is None
    assert retry_after_seconds("Mon, 14 Sep 2026 00:00:00", NOW) is None
    assert retry_after_seconds("Mon, 14 Sep 2026 00:00:00 GMT", NOW) >= 0
    assert (
        endpoint(source(tenant="verified"))
        == "https://boards-api.greenhouse.io/v1/boards/verified/jobs"
    )
    assert "skip=100" in endpoint(source("lever", tenant="verified"), 100)
    assert "api.eu.lever.co" in endpoint(
        source("lever", tenant="verified", metadata={"region": "eu"})
    )
    assert endpoint(source("ashby", tenant="verified")).endswith("?includeCompensation=true")
    assert endpoint(source("feed", tenant="verified")) == source().url


def test_collect_greenhouse_detail_and_empty_partial():
    listing = (FIXTURES / "greenhouse_success.json").read_bytes()
    detail = (FIXTURES / "greenhouse_detail.json").read_bytes()
    row = source()
    outcome, jobs = collect(
        row,
        FetchBudget(),
        wire=fake_wire([response(listing), response(detail)]),
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    assert outcome.snapshot_complete and len(jobs) == 1 and jobs[0].description
    outcome, jobs = collect(
        row,
        FetchBudget(max_requests=1),
        wire=fake_wire([response(listing)]),
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    assert not outcome.snapshot_complete and outcome.outcome == "PARTIAL_BUDGET"
    outcome, jobs = collect(
        source("ashby"),
        FetchBudget(),
        wire=fake_wire([response(b"{}")]),
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    assert outcome.outcome == "QUARANTINED_SCHEMA" and jobs == []
    outcome, jobs = collect(
        source("ashby"),
        FetchBudget(),
        wire=fake_wire([response()]),
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    assert outcome.outcome == "SUCCESS_EMPTY"


def test_collect_sitemap_and_lever_pagination():
    xml = (FIXTURES / "sitemap_success.xml").read_bytes()
    html = (FIXTURES / "jsonld_success.html").read_bytes()
    row = source("sitemap", access_mode="PUBLIC_HTML_APPROVED", robots_allowed=True)
    outcome, jobs = collect(
        row,
        FetchBudget(),
        wire=fake_wire(
            [
                response(xml, **{"content-type": "application/xml"}),
                response(html, **{"content-type": "text/html"}),
            ]
        ),
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    assert len(jobs) == 1 and outcome.snapshot_complete
    base = json.loads((FIXTURES / "lever_success.json").read_text())[0]
    page = json.dumps([base | {"id": str(i)} for i in range(100)]).encode()
    outcome, jobs = collect(
        source("lever"),
        FetchBudget(),
        wire=fake_wire([response(page), response(b"[]")]),
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    assert len(jobs) == 100 and outcome.snapshot_complete


def test_wire_ip_pinning_and_gzip_caps():
    conn = MagicMock()
    raw = MagicMock()
    conn.getresponse.return_value = raw
    raw.status = 200
    raw.getheaders.return_value = [
        ("Content-Type", "application/json"),
        ("Content-Encoding", "gzip"),
    ]
    raw.read1.side_effect = [gzip.compress(b'{"jobs":[]}'), b""]
    with patch("career_radar.fetch._PinnedHTTPS", return_value=conn):
        output = wire_get("https://jobs.example.com/path", "1.1.1.1", {}, 30, MAX_BYTES)
    assert output.content == b'{"jobs":[]}'
    assert conn.request.call_args.kwargs["headers"]["Host"] == "jobs.example.com"
    conn.close.assert_called_once()
    raw.read1.side_effect = [gzip.compress(b"x" * 1000), b""]
    with (
        patch("career_radar.fetch._PinnedHTTPS", return_value=conn),
        pytest.raises(SecurityError, match="DECOMPRESSED"),
    ):
        wire_get("https://jobs.example.com/", "1.1.1.1", {}, 30, 100)
    with (
        patch("socket.create_connection") as connect,
        patch("ssl.create_default_context") as context,
    ):
        ssl_socket = MagicMock()
        context.return_value.wrap_socket.return_value = ssl_socket
        connection = _PinnedHTTPS("jobs.example.com", "1.1.1.1", 443, 30)
        connection.connect()
        assert connect.call_args.args[0] == ("1.1.1.1", 443)
        assert (
            context.return_value.wrap_socket.call_args.kwargs["server_hostname"]
            == "jobs.example.com"
        )


def test_wire_slow_drip_deadline_shuts_down_socket():
    import threading

    stopped = threading.Event()
    conn = MagicMock()
    conn.sock.shutdown.side_effect = lambda _: stopped.set()
    raw = MagicMock()
    conn.getresponse.return_value = raw
    raw.getheaders.return_value = [("Content-Type", "application/json")]

    def slow_read(_):
        assert stopped.wait(0.5)
        return b"x"

    raw.read1.side_effect = slow_read
    started = time.monotonic()
    with (
        patch("career_radar.fetch._PinnedHTTPS", return_value=conn),
        pytest.raises(TimeoutError, match="DEADLINE"),
    ):
        wire_get("https://jobs.example.com/", "1.1.1.1", {}, 0.02, MAX_BYTES)
    assert time.monotonic() - started < 0.5
    conn.sock.shutdown.assert_called_once()


def test_wire_http_encoding_content_length_truncation_and_stream_caps():
    from career_radar.fetch import _shutdown

    sock = MagicMock()
    sock.shutdown.side_effect = OSError("already closed")
    _shutdown(sock)
    conn = MagicMock()
    conn.sock = None
    raw = MagicMock()
    raw.status = 200
    conn.getresponse.return_value = raw
    variants = [
        ([("Content-Length", "101")], [], "RESPONSE_TOO_LARGE"),
        ([("Content-Encoding", "br")], [], "UNSUPPORTED_CONTENT_ENCODING"),
        ([("Content-Encoding", "gzip")], [gzip.compress(b"{}")[:-4], b""], "TRUNCATED_GZIP"),
        ([], [b"x" * 101, b""], "RESPONSE_TOO_LARGE"),
    ]
    for headers, chunks, code in variants:
        raw.getheaders.return_value = headers
        raw.read1.side_effect = chunks
        with (
            patch("http.client.HTTPConnection", return_value=conn),
            pytest.raises(SecurityError, match=code),
        ):
            wire_get("http://jobs.example.com/?id=1", "1.1.1.1", {}, 30, 100)
    raw.getheaders.return_value = []
    raw.read1.side_effect = [b"{}", b""]
    with patch("http.client.HTTPConnection", return_value=conn):
        assert wire_get("http://jobs.example.com/", "1.1.1.1", {}, 30, 100).content == b"{}"


def test_collect_continues_and_preserves_partial_snapshots():
    listing = json.loads((FIXTURES / "greenhouse_success.json").read_text())
    listing["jobs"].append(listing["jobs"][0] | {"id": 102, "title": "Marketing Specialist"})
    listing["jobs"].append(listing["jobs"][0] | {"id": 103})
    row = source()
    cached = parse(row, result(json.dumps(listing)))[2]
    row.metadata["_greenhouse_seen_versions"] = {"103": greenhouse_listing_version(cached)}
    detail = (FIXTURES / "greenhouse_detail.json").read_bytes()
    outcome, jobs = collect(
        row,
        FetchBudget(),
        wire=fake_wire([response(json.dumps(listing).encode()), response(detail)]),
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    assert len(jobs) == 1 and outcome.snapshot_complete
    assert (
        collect(
            row,
            FetchBudget(),
            wire=fake_wire([response(status=304)]),
            resolver=lambda _: ["1.1.1.1"],
            sleep=lambda _: None,
        )[0].outcome
        == "QUARANTINED_SCHEMA"
    )
    xml = (FIXTURES / "sitemap_success.xml").read_bytes()
    row = source("sitemap", access_mode="PUBLIC_HTML_APPROVED", robots_allowed=True)
    outcome, jobs = collect(
        row,
        FetchBudget(max_requests=1),
        wire=fake_wire([response(xml, **{"content-type": "application/xml"})]),
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    assert outcome.outcome == "PARTIAL_BUDGET" and not outcome.snapshot_complete
    base = json.loads((FIXTURES / "lever_success.json").read_text())[0]
    page = json.dumps([base | {"id": str(i)} for i in range(100)]).encode()
    outcome, jobs = collect(
        source("lever"),
        FetchBudget(max_requests=1),
        wire=fake_wire([response(page)]),
        resolver=lambda _: ["1.1.1.1"],
        sleep=lambda _: None,
    )
    assert (
        len(jobs) == 100 and outcome.outcome == "PARTIAL_BUDGET" and not outcome.snapshot_complete
    )
