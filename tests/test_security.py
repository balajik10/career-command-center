"""Adversarial security tests; no real DNS or HTTP calls."""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from career_radar.domain import SourceDefinition
from career_radar.security import (
    MAX_BYTES,
    SecurityError,
    canonical_url,
    plain_text,
    private_token,
    redact,
    resolve_host,
    safe_cell,
    safe_xml,
    validate_public_url,
)
from career_radar.sources.registry import check_policy, health_state, load_catalogue, record_outcome

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def approved(**updates):
    base = SourceDefinition(
        source_id="example",
        company="Example",
        provider="greenhouse",
        url="https://jobs.example.com/api",
        enabled=True,
        access_mode="OFFICIAL_API",
        policy_state="APPROVED",
        coverage_state="VERIFIED_ACTIVE",
        cost_class="free",
        policy_reviewed_at=NOW - timedelta(days=1),
        policy_review_due_at=NOW + timedelta(days=30),
        human_reviewer="Example reviewer",
        policy_evidence_url="https://jobs.example.com/terms",
        policy_evidence_hash="observed-evidence",
    )
    return SourceDefinition.model_validate(base.model_dump() | updates)


@pytest.mark.parametrize(
    "value", ["=SUM(A1)", "+1", "-1", "@import", "\ttext", "\rtext", "\ntext", "  =IMPORTXML()"]
)
def test_formula_injection(value):
    assert safe_cell(value) == "'" + value


def test_text_and_html():
    assert safe_cell("safe") == "safe"
    assert safe_cell("") == ""
    assert (
        plain_text(
            "<p>Java &amp; SQL</p><script>secret()</script><style>invisible</style><div>Spring<br>Boot</div>"
        )
        == "Java & SQL Spring Boot"
    )
    assert plain_text("&amp;lt;p&amp;gt;Hello&amp;lt;/p&amp;gt;") == "Hello"
    with pytest.raises(SecurityError, match="TEXT_TOO_LARGE"):
        plain_text("1234", 3)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:x",
        "https://",
        "https://name:pass@example.com/",
        "https://example.com:9000/",
        "https://example.com:bad/",
        "https://example.com/\\a",
        "https://example.com/\n",
        "https://example.com/?access_token=x",
        "https://example.com/?sessionToken=x",
    ],
)
def test_reject_bad_urls(url):
    with pytest.raises(SecurityError):
        canonical_url(url)


def test_canonical_retains_identity():
    assert (
        canonical_url("https://EXAMPLE.com:443/job?gh_jid=12&utm_campaign=abc&b=2&a=1#top")
        == "https://example.com/job?a=1&b=2&gh_jid=12"
    )
    assert canonical_url("http://example.com:443") == "http://example.com:443/"
    assert canonical_url("https://[2606:4700:4700::1111]/") == "https://[2606:4700:4700::1111]/"
    assert canonical_url("https://example.com/?token=x", reject_secrets=False).endswith("?token=x")


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "169.254.169.254",
        "10.1.2.3",
        "192.168.1.1",
        "0.0.0.0",
        "[::1]",
        "[::ffff:127.0.0.1]",
        "localhost",
        "foo.local",
        "metadata.google.internal",
        "linkedin.com",
        "www.naukri.com",
        "www.indeed.com",
    ],
)
def test_ssrf_and_restricted_denied(host):
    with pytest.raises(SecurityError):
        validate_public_url("https://" + host + "/", lambda _: ["1.1.1.1"])


def test_dns_every_address_must_be_public():
    assert validate_public_url("https://example.com/", lambda _: ["1.1.1.1"])[1] == ["1.1.1.1"]
    assert validate_public_url("https://1.1.1.1/")[1] == ["1.1.1.1"]
    assert validate_public_url(
        "https://linkedin.com/", lambda _: ["1.1.1.1"], allow_restricted=True
    )[0]
    for addresses in ([], ["1.1.1.1", "127.0.0.1"], ["bad address"]):
        with pytest.raises(SecurityError):
            validate_public_url("https://example.com/", lambda _, addresses=addresses: addresses)
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("1.1.1.1", 0))]):
        assert resolve_host("example.com") == ["1.1.1.1"]


def test_private_hmac_not_unkeyed_hash():
    key = bytes(range(32))
    token = private_token(key, "email", " Candidate@Example.com ", "1")
    assert (
        token
        == hmac.new(key, b"career-radar:1:email:candidate@example.com", hashlib.sha256).hexdigest()
    )
    assert token != private_token(key, "recipient", "candidate@example.com", "1")
    assert token != private_token(key, "email", "candidate@example.com", "2")
    assert private_token(key, "phone", "+1 (234) 567-8901") == private_token(
        key, "phone", "+12345678901"
    )
    assert private_token(key, "eml", b"raw bytes")
    assert private_token(key, "gmail", "mailbox:msg")
    with pytest.raises(SecurityError, match="KEY_TOO_SHORT"):
        private_token(b"small", "email", "candidate@example.com")
    with pytest.raises(SecurityError, match="TOKEN_NAMESPACE"):
        private_token(key, "a:b", "x")
    with pytest.raises(SecurityError, match="E164"):
        private_token(key, "phone", "12345")


def test_redaction():
    output = redact(
        "candidate@example.com +1 234 567 8901 token=abcd&job=2 Bearer abcdef password:example"
    )
    assert "candidate@example.com" not in output
    assert "234" not in output
    assert "abcd" not in output
    assert "abcdef" not in output
    assert "password:example" not in output


@pytest.mark.parametrize(
    "xml",
    [
        b"<!DOCTYPE root><root/>",
        b'<!DOCTYPE root [<!ENTITY e SYSTEM "file:///etc/passwd">]><root>&e;</root>',
        b'<root xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="file:///x"/></root>',
        b"<root>",
        b'<!DOCTYPE lolz [<!ENTITY a "abc"><!ENTITY b "&a;&a;">]><x>&b;</x>',
    ],
)
def test_hostile_xml(xml):
    with pytest.raises(SecurityError):
        safe_xml(xml)


def test_xml_structure_and_bytes_limits():
    assert safe_xml(b"<root><item id='1'>ok</item></root>").tag == "root"
    for xml, kwargs in [
        (b"x" * (MAX_BYTES + 1), {}),
        (b"<a>" * 65 + b"</a>" * 65, {}),
        (b"<r><a/><b/></r>", {"max_nodes": 2}),
        (b'<r a="1" b="2"/>', {"max_attributes": 1}),
    ]:
        with pytest.raises(SecurityError):
            safe_xml(xml, **kwargs)


def test_policy_probe_exact_user_url_and_no_autoapproval():
    source = approved(enabled=False, policy_state="PENDING_REVIEW")
    for url in (
        source.url,
        "https://jobs.example.com/robots.txt",
        "https://docs.example.com/terms",
    ):
        check_policy(
            source,
            url,
            NOW,
            "VALIDATION_PROBE",
            user_initiated=True,
            documentation_urls=("https://docs.example.com/terms",),
        )
    assert source.policy_state == "PENDING_REVIEW" and not source.enabled
    for url in ("https://evil.example.com/api", "https://jobs.example.com/api?foo=1"):
        with pytest.raises(SecurityError, match="EXACT_URL"):
            check_policy(source, url, NOW, "VALIDATION_PROBE", user_initiated=True)
    with pytest.raises(SecurityError):
        check_policy(source, source.url, NOW, "VALIDATION_PROBE")
    with pytest.raises(SecurityError):
        check_policy(approved(), source.url, NOW, "VALIDATION_PROBE", user_initiated=True)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"enabled": False}, "SOURCE_NOT_APPROVED"),
        ({"policy_state": "PENDING_REVIEW"}, "SOURCE_NOT_APPROVED"),
        ({"access_mode": "USER_IMPORT"}, "ACCESS_MODE"),
        ({"coverage_state": "UNVALIDATED"}, "NOT_VERIFIED"),
        ({"health_state": "DOWN"}, "HEALTH_BLOCKED"),
        ({"backoff_until": NOW + timedelta(seconds=1)}, "BACKOFF_ACTIVE"),
        ({"cost_class": "unknown"}, "ZERO_COST"),
        ({"human_reviewer": ""}, "POLICY_EVIDENCE"),
        ({"policy_reviewed_at": None}, "POLICY_EVIDENCE"),
        ({"policy_review_due_at": NOW}, "POLICY_EVIDENCE"),
        ({"access_mode": "PUBLIC_HTML_APPROVED"}, "ROBOTS_NOT_APPROVED"),
    ],
)
def test_policy_fail_closed(change, code):
    source = approved(**change)
    with pytest.raises(SecurityError, match=code):
        check_policy(source, source.url, NOW)


def test_policy_valid_approved_host_and_cost_modes():
    source = approved(allowed_hosts=["api.example.com"])
    check_policy(source, "https://api.example.com/jobs", NOW)
    check_policy(approved(access_mode="RSS_ATOM", robots_allowed=True), source.url, NOW)
    check_policy(approved(cost_class="paid"), source.url, NOW, zero_cost_mode=False)
    with pytest.raises(SecurityError, match="UNAPPROVED_HOST"):
        check_policy(source, "https://evil.example.com/", NOW)
    with pytest.raises(SecurityError, match="NAIVE_TIME"):
        check_policy(source, source.url, datetime(2026, 1, 1))
    with pytest.raises(SecurityError, match="UNKNOWN_REQUEST"):
        check_policy(source, source.url, NOW, "OTHER")


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, "UNKNOWN"),
        ({"last_success_at": NOW}, "HEALTHY"),
        ({"consecutive_failures": 1}, "DEGRADED"),
        ({"backoff_until": NOW + timedelta(hours=1), "consecutive_failures": 1}, "BACKOFF"),
        (
            {
                "last_success_at": NOW - timedelta(hours=49),
                "backoff_until": NOW + timedelta(hours=1),
            },
            "STALE",
        ),
        (
            {
                "last_success_at": NOW - timedelta(hours=97),
                "backoff_until": NOW + timedelta(hours=1),
            },
            "DOWN",
        ),
        ({"consecutive_failures": 3}, "DOWN"),
        ({"health_state": "AUTH_REQUIRED", "consecutive_failures": 4}, "AUTH_REQUIRED"),
        ({"health_state": "PAUSED", "consecutive_failures": 4}, "PAUSED"),
    ],
)
def test_health_precedence(changes, expected):
    assert health_state(approved(**changes), NOW) == expected


def test_source_transitions_and_no_partial_watermark():
    source = approved()
    for outcome in ["PARTIAL_BUDGET", "DEFERRED_BUDGET"]:
        result = record_outcome(source, outcome, NOW)
        assert result.last_success_at is None and result.baseline_state == "NOT_STARTED"
    for outcome in ["SUCCESS_COMPLETE", "SUCCESS_EMPTY", "NO_CHANGE"]:
        result = record_outcome(source, outcome, NOW)
        assert result.health_state == "HEALTHY" and result.baseline_state == "COMPLETED"
        assert record_outcome(result, outcome, NOW).baseline_completed_at == NOW
    assert record_outcome(source, "BACKOFF", NOW, 5).health_state == "BACKOFF"
    assert record_outcome(source, "AUTH_REQUIRED", NOW).health_state == "AUTH_REQUIRED"
    assert record_outcome(source, "FAILED_TRANSIENT", NOW).health_state == "DEGRADED"
    assert record_outcome(source, "POLICY_BLOCKED", NOW).policy_state == "POLICY_BLOCKED"
    assert record_outcome(source, "LOGIN_REQUIRED", NOW).policy_state == "LOGIN_REQUIRED"
    paused = record_outcome(source, "PAUSE", NOW)
    assert paused.health_state == "PAUSED"
    assert record_outcome(paused, "UNPAUSE", NOW).health_state == "UNKNOWN"
    assert health_state(source, NOW, anomalous=True) == "DEGRADED"
    assert health_state(source, NOW, paused=True) == "PAUSED"
    assert health_state(source, NOW, auth_required=True) == "AUTH_REQUIRED"


def test_neutral_catalogue(tmp_path):
    rows = load_catalogue(Path("config/companies.yaml"))
    assert len(rows) == 280
    assert sum(row.policy_state == "APPROVED" for row in rows) == 0
    assert sum(row.coverage_state == "NATIVE_ALERT_ONLY" for row in rows) == 3
    assert {row.company for row in rows if row.policy_state == "MANUAL_ONLY"} == {
        "Amazon",
        "Google",
        "JPMorgan Chase",
    }
    malformed = tmp_path / "bad.yaml"
    malformed.write_text("invalid: true")
    with pytest.raises(ValueError):
        load_catalogue(malformed)
    malformed.write_text(
        "companies:\n- source_id: test\n  company: Example\n  provider: manual\n  url: ''\n- source_id: test\n  company: Example\n  provider: manual\n  url: ''\n"
    )
    with pytest.raises(ValueError, match="DUPLICATE"):
        load_catalogue(malformed)
