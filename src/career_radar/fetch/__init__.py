"""Bounded GET-only transport with IP pinning, per-hop policy and SSRF checks."""

from __future__ import annotations

import hashlib
import http.client
import json
import random
import socket
import ssl
import threading
import time
import zlib
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import quote, urljoin, urlsplit

from career_radar.domain import FetchResult, RawJob, SourceDefinition
from career_radar.security import MAX_BYTES, SecurityError, resolve_host, validate_public_url
from career_radar.sources import ParseError, parse
from career_radar.sources.parsers import parse_sitemap
from career_radar.sources.registry import RequestPurpose, check_policy

USER_AGENT = "CareerCommandCenter/0.1 (personal job discovery; contact configurable)"
GLOBAL_LIMIT = threading.BoundedSemaphore(12)
HOST_LOCKS: dict[str, threading.Lock] = {}
HOST_LAST: dict[str, float] = {}
HOST_GUARD = threading.Lock()


@dataclass
class FetchBudget:
    max_requests: int = 80
    deadline: float = field(default_factory=lambda: time.monotonic() + 180)
    requests: int = 0
    max_bytes: int = MAX_BYTES
    lock: threading.Lock = field(default_factory=threading.Lock)

    def claim(self, reserve_seconds: float = 30) -> None:
        with self.lock:
            if (
                self.requests >= self.max_requests
                or time.monotonic() + reserve_seconds > self.deadline
            ):
                raise SecurityError("DEFERRED_BUDGET")
            self.requests += 1


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    content: bytes


class Wire(Protocol):
    def __call__(
        self, url: str, address: str, headers: dict[str, str], timeout: float, max_bytes: int
    ) -> Response: ...


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, port: int, timeout: float) -> None:
        self.tls_context = ssl.create_default_context()
        self.request_timeout = timeout
        super().__init__(host, port, timeout=timeout, context=self.tls_context)
        self.address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self.address, self.port), timeout=min(5, self.request_timeout)
        )
        self.sock = self.tls_context.wrap_socket(self.sock, server_hostname=self.host)
        self.sock.settimeout(self.timeout)


def _shutdown(sock: socket.socket) -> None:
    """Interrupt a slow-drip response when the wall-clock request deadline expires."""
    with suppress(OSError):  # The peer may already have closed the socket.
        sock.shutdown(socket.SHUT_RDWR)


def wire_get(
    url: str, address: str, headers: dict[str, str], timeout: float, max_bytes: int
) -> Response:
    """Connect to the validated IP; TLS still verifies the original hostname."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    connection: http.client.HTTPConnection
    if parts.scheme == "https":
        connection = _PinnedHTTPS(host, address, parts.port or 443, timeout)
    else:
        connection = http.client.HTTPConnection(address, parts.port or 80, timeout=min(5, timeout))
    end = time.monotonic() + timeout
    timer: threading.Timer | None = None
    try:
        connection.request(
            "GET",
            (parts.path or "/") + ("?" + parts.query if parts.query else ""),
            headers={**headers, "Host": parts.netloc},
        )
        deadline_socket = connection.sock
        if deadline_socket is not None:
            timer = threading.Timer(max(0, end - time.monotonic()), _shutdown, (deadline_socket,))
            timer.daemon = True
            timer.start()
        response = connection.getresponse()
        result_headers = {key.lower(): value for key, value in response.getheaders()}
        size = int(result_headers.get("content-length", "0"))
        if size > max_bytes:
            raise SecurityError("RESPONSE_TOO_LARGE")
        encoding = result_headers.get("content-encoding", "identity").lower()
        if encoding not in {"identity", "gzip"}:
            raise SecurityError("UNSUPPORTED_CONTENT_ENCODING")
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
        received = 0
        output = bytearray()
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("REQUEST_DEADLINE")
            if connection.sock is not None:
                connection.sock.settimeout(min(20, remaining))
            chunk = response.read1(16_384)
            if not chunk:
                break
            received += len(chunk)
            if received > max_bytes:
                raise SecurityError("RESPONSE_TOO_LARGE")
            if inflater:
                chunk = inflater.decompress(chunk, max_bytes + 1 - len(output))
            output.extend(chunk)
            if len(output) > max_bytes:
                raise SecurityError("DECOMPRESSED_TOO_LARGE")
        if inflater and not inflater.eof:
            raise SecurityError("TRUNCATED_GZIP")
        return Response(response.status, result_headers, bytes(output))
    finally:
        if timer is not None:
            timer.cancel()
        connection.close()


def retry_after_seconds(value: str, now: datetime) -> float | None:
    try:
        seconds = float(value)
        if seconds >= 0 and seconds <= 86400:
            return seconds
        return None
    except ValueError:
        try:
            timestamp = parsedate_to_datetime(value)
            if timestamp.tzinfo is None:
                return None
            return max(0, min(86400, (timestamp - now).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return None


def _expected_type(source: SourceDefinition, content_type: str, purpose: str) -> bool:
    kind = content_type.split(";", 1)[0].strip().lower()
    if purpose == "VALIDATION_PROBE":
        return kind in {
            "text/plain",
            "text/html",
            "application/json",
            "application/xml",
            "text/xml",
        }
    if source.provider in {"greenhouse", "lever", "ashby"}:
        return kind in {"application/json", "application/ld+json"}
    if source.provider in {"feed", "rss", "atom", "sitemap"}:
        return kind in {
            "application/rss+xml",
            "application/atom+xml",
            "application/xml",
            "text/xml",
        }
    return kind in {"text/html", "application/xhtml+xml"}


def fetch(
    source: SourceDefinition,
    budget: FetchBudget,
    *,
    url: str | None = None,
    purpose: RequestPurpose = "COLLECTION",
    user_initiated: bool = False,
    documentation_urls: tuple[str, ...] = (),
    wire: Wire = wire_get,
    resolver: Callable[[str], Iterable[str]] = resolve_host,
    sleep: Callable[[float], None] = time.sleep,
) -> FetchResult:
    target = url or source.url
    local_requests = 0
    initial_target = target
    attempt = 0
    redirects = 0
    while True:
        now = datetime.now(UTC)
        try:
            check_policy(
                source,
                target,
                now,
                purpose,
                user_initiated=user_initiated,
                documentation_urls=documentation_urls,
            )
            if time.monotonic() + 30 > budget.deadline or budget.requests >= budget.max_requests:
                raise SecurityError("DEFERRED_BUDGET")
            target, addresses = validate_public_url(target, resolver)
            # Preserve exact probe URL including non-secret query order. No cross-URL redirects.
            if purpose == "VALIDATION_PROBE" and target != initial_target:
                raise SecurityError("PROBE_EXACT_URL_ONLY")
            host = urlsplit(target).hostname or ""
            with HOST_GUARD:
                lock = HOST_LOCKS.setdefault(host, threading.Lock())
            with GLOBAL_LIMIT, lock:
                wait = max(0, HOST_LAST.get(host, 0) + 2 - time.monotonic())
                if time.monotonic() + wait + 30 > budget.deadline:
                    raise SecurityError("DEFERRED_BUDGET")
                budget.claim()
                local_requests += 1
                sleep(wait)
                headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
                is_entry = initial_target in {source.url, endpoint(source)} and redirects == 0
                if source.etag and is_entry:
                    headers["If-None-Match"] = source.etag
                if source.last_modified and is_entry:
                    headers["If-Modified-Since"] = source.last_modified
                HOST_LAST[host] = time.monotonic()
                response = wire(
                    target,
                    addresses[0],
                    headers,
                    min(30, budget.deadline - time.monotonic()),
                    budget.max_bytes,
                )
        except SecurityError as exc:
            code = str(exc)
            return FetchResult(
                source_id=source.source_id,
                url=target,
                status_code=0,
                snapshot_complete=False,
                outcome="DEFERRED_BUDGET" if code == "DEFERRED_BUDGET" else "POLICY_SKIPPED",
                error_code=code,
                request_count=local_requests,
            )
        except (OSError, http.client.HTTPException, zlib.error, ValueError):
            attempt += 1
            delay = min(2**attempt, 8) + random.random()  # nosec B311
            if attempt < 3 and time.monotonic() + delay + 30 <= budget.deadline:
                sleep(delay)
                continue
            return FetchResult(
                source_id=source.source_id,
                url=target,
                status_code=0,
                snapshot_complete=False,
                outcome="FAILED_TRANSIENT",
                error_code="NETWORK_FAILURE",
                request_count=local_requests,
            )
        now = datetime.now(UTC)
        count = local_requests
        common: dict[str, Any] = {
            "source_id": source.source_id,
            "url": target,
            "status_code": response.status,
            "headers": response.headers,
            "request_count": count,
            "bytes_received": len(response.content),
            "fetched_at": now,
        }
        if response.status in {301, 302, 303, 307, 308}:
            redirects += 1
            location = response.headers.get("location")
            if not location or redirects > 5 or purpose == "VALIDATION_PROBE":
                return FetchResult(
                    **common,
                    snapshot_complete=False,
                    outcome="FAILED_PERMANENT",
                    error_code="REDIRECT_LIMIT_OR_PROBE_REDIRECT",
                )
            target = urljoin(target, location)
            continue
        if response.status == 304:
            return FetchResult(**common, outcome="NO_CHANGE")
        if response.status in {401, 403}:
            return FetchResult(
                **common,
                snapshot_complete=False,
                outcome="POLICY_BLOCKED",
                error_code="ACCESS_DENIED",
            )
        if response.status in {408, 425, 429, 500, 502, 503, 504}:
            attempt += 1
            retry_after = retry_after_seconds(response.headers.get("retry-after", ""), now)
            delay = retry_after if retry_after is not None else min(2**attempt, 8) + random.random()  # nosec B311
            if (
                attempt < 3
                and (response.status != 429 or retry_after is not None)
                and time.monotonic() + delay + 30 <= budget.deadline
            ):
                sleep(delay)
                continue
            return FetchResult(
                **common,
                snapshot_complete=False,
                outcome="BACKOFF" if response.status == 429 else "FAILED_TRANSIENT",
                error_code="RATE_LIMIT" if response.status == 429 else "RETRY_EXHAUSTED",
            )
        if response.status != 200:
            return FetchResult(
                **common,
                snapshot_complete=False,
                outcome="FAILED_PERMANENT",
                error_code="HTTP_ERROR",
            )
        if not _expected_type(source, response.headers.get("content-type", ""), purpose):
            return FetchResult(
                **common,
                snapshot_complete=False,
                outcome="QUARANTINED_SCHEMA",
                error_code="CONTENT_TYPE",
            )
        if len(response.content) > budget.max_bytes:
            return FetchResult(
                **common,
                snapshot_complete=False,
                outcome="QUARANTINED_SCHEMA",
                error_code="RESPONSE_TOO_LARGE",
            )
        try:
            body = response.content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return FetchResult(
                **common,
                snapshot_complete=False,
                outcome="QUARANTINED_SCHEMA",
                error_code="INVALID_ENCODING",
            )
        lowered = body[:100_000].lower()
        if any(
            marker in lowered
            for marker in ("g-recaptcha", "h-captcha", "cf-chl-", "verify you are human")
        ):
            return FetchResult(
                **common,
                snapshot_complete=False,
                outcome="POLICY_BLOCKED",
                error_code="CAPTCHA_STOP",
            )
        if '<input type="password"' in lowered or "sign in to continue" in lowered:
            return FetchResult(
                **common, snapshot_complete=False, outcome="LOGIN_REQUIRED", error_code="LOGIN_STOP"
            )
        return FetchResult(
            **common,
            body="" if purpose == "VALIDATION_PROBE" else body,
            outcome="VALIDATION_PROBE" if purpose == "VALIDATION_PROBE" else "SUCCESS_COMPLETE",
        )


def endpoint(source: SourceDefinition, offset: int = 0) -> str:
    """Tenant identifiers must be supplied by an official observed board URL."""
    tenant = quote(source.tenant, safe="")
    if not tenant:
        return source.url
    if source.provider == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs"
    if source.provider == "lever":
        host = "api.eu.lever.co" if source.metadata.get("region") == "eu" else "api.lever.co"
        return f"https://{host}/v0/postings/{tenant}?mode=json&skip={offset}&limit=100"
    if source.provider == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{tenant}?includeCompensation=true"
    return source.url


def greenhouse_listing_version(job: RawJob) -> str:
    """Fingerprint documented listing metadata, requiring an aware update timestamp."""
    if not job.updated_at_raw:
        raise ParseError("GREENHOUSE_VERSION_REQUIRED")
    updated = datetime.fromisoformat(job.updated_at_raw.replace("Z", "+00:00"))
    if updated.tzinfo is None:
        raise ParseError("GREENHOUSE_VERSION_REQUIRED")
    values = [
        job.provider_job_id,
        job.tenant,
        job.company,
        job.title,
        job.location,
        job.url,
        updated.astimezone(UTC).isoformat(),
        job.parser_version,
    ]
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode()).hexdigest()


def collect(
    source: SourceDefinition,
    budget: FetchBudget,
    *,
    wire: Wire = wire_get,
    resolver: Callable[[str], Iterable[str]] = resolve_host,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[FetchResult, list[RawJob]]:
    """Collect a bounded snapshot. No incomplete snapshot can imply missing/closed jobs."""
    jobs: list[RawJob] = []
    offset = 0
    request_count = 0
    bytes_received = 0
    checkpoint: dict[str, str] = {}
    listed_ids: list[str] = []
    listing_complete = False

    def request(row: SourceDefinition, url: str) -> FetchResult:
        nonlocal request_count, bytes_received
        fetched = fetch(row, budget, url=url, wire=wire, resolver=resolver, sleep=sleep)
        request_count += fetched.request_count
        bytes_received += fetched.bytes_received
        return fetched

    def finish(fetched: FetchResult, rows: list[RawJob]) -> tuple[FetchResult, list[RawJob]]:
        fetched.request_count = request_count
        fetched.bytes_received = bytes_received
        fetched.checkpoint = checkpoint
        fetched.listed_ids = listed_ids
        fetched.listing_complete = listing_complete
        return fetched, rows

    while True:
        # A fresh complete inventory is required even while resuming details. A 304
        # cannot prove list presence because raw inventories are deliberately not stored.
        listing_source = (
            source.model_copy(update={"etag": "", "last_modified": ""})
            if source.provider == "greenhouse"
            else source
        )
        result = request(listing_source, endpoint(source, offset))
        if result.status_code == 304:
            if source.provider == "greenhouse":
                result.outcome = "QUARANTINED_SCHEMA"
                result.error_code = "UNEXPECTED_NOT_MODIFIED"
                result.snapshot_complete = False
            return finish(result, jobs)
        if result.error_code:
            if jobs:
                result.outcome = (
                    "PARTIAL_BUDGET" if result.outcome == "DEFERRED_BUDGET" else result.outcome
                )
            return finish(result, jobs)
        try:
            if source.provider == "sitemap":
                for url in parse_sitemap(result.body.encode(), source.url):
                    detail_source = source.model_copy(update={"provider": "jsonld"})
                    detail = request(detail_source, url)
                    if detail.error_code:
                        detail.outcome = (
                            "PARTIAL_BUDGET"
                            if detail.outcome == "DEFERRED_BUDGET"
                            else detail.outcome
                        )
                        return finish(detail, jobs)
                    jobs.extend(parse(detail_source, detail))
                return finish(result, jobs)
            batch = parse(source, result)
            if source.provider == "greenhouse":
                inventory: dict[str, tuple[RawJob, str]] = {}
                for job in batch:
                    version = greenhouse_listing_version(job)
                    inventory[job.provider_job_id] = job, version
                listed_ids = sorted(inventory)
                listing_complete = True
                stored = source.metadata.get("_greenhouse_seen_versions", {})
                seen_versions = stored if isinstance(stored, dict) else {}
                for external_id in listed_ids:
                    job, version = inventory[external_id]
                    # List is metadata-only. Detail only engineering candidates and changed IDs.
                    in_scope = any(
                        term in job.title.lower()
                        for term in ("engineer", "developer", "sde", "mts", "technical staff")
                    )
                    if (not in_scope and external_id not in seen_versions) or (
                        seen_versions.get(external_id) == version
                    ):
                        continue
                    detail_url = (
                        endpoint(source).split("?", 1)[0]
                        + "/"
                        + quote(job.provider_job_id, safe="")
                        + "?pay_transparency=true"
                    )
                    detail = request(source, detail_url)
                    if detail.error_code:
                        detail.outcome = (
                            "PARTIAL_BUDGET"
                            if detail.outcome == "DEFERRED_BUDGET"
                            else detail.outcome
                        )
                        return finish(detail, jobs)
                    details = parse(source, detail)
                    if len(details) != 1 or details[0].provider_job_id != external_id:
                        raise ParseError("DETAIL_ID_MISMATCH")
                    detailed_job = details[0]
                    jobs.append(detailed_job)
                    if greenhouse_listing_version(detailed_job) != version:
                        result.snapshot_complete = False
                        result.outcome = "PARTIAL_BUDGET"
                        result.error_code = "LISTING_CHANGED_DURING_DETAILS"
                        return finish(result, jobs)
                    detailed_job.metadata["greenhouse_listing_version"] = version
                    checkpoint[external_id] = version
                batch = []
            jobs.extend(batch)
        except (ParseError, SecurityError, ValueError):
            result.outcome = "QUARANTINED_SCHEMA"
            result.error_code = "PARSER_CONTRACT"
            result.snapshot_complete = False
            return finish(result, jobs)
        if source.provider != "lever" or len(batch) < 100:
            result.outcome = "SUCCESS_COMPLETE" if jobs or listed_ids else "SUCCESS_EMPTY"
            return finish(
                result, list({(job.source_id, job.provider_job_id): job for job in jobs}.values())
            )
        offset += 100
