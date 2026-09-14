"""Trust boundaries for external strings, network destinations, and private identifiers."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree.ElementTree import Element  # nosec B405

# Element is a return-type annotation only; all parsing uses defusedxml below.
from defusedxml import ElementTree

MAX_BYTES = 5 * 1024 * 1024
TRACKING = {"source", "ref", "referrer", "fbclid", "gclid", "trk", "trackingid"}
SENSITIVE = {
    "token",
    "access_token",
    "refresh_token",
    "key",
    "api_key",
    "password",
    "code",
    "signature",
    "auth",
}
RESTRICTED_HOSTS = {"linkedin.com", "naukri.com", "indeed.com", "glassdoor.com", "wellfound.com"}


class SecurityError(ValueError):
    """Input violates a trust boundary; do not retry through another identity."""


def safe_cell(value: str) -> str:
    """Store external spreadsheet cells as literal strings, including leading controls."""
    return (
        "'" + value
        if value and (value[0] in "=+-@\t\r\n" or value.lstrip().startswith(("=", "+", "-", "@")))
        else value
    )


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "iframe", "object"}:
            self.hidden += 1
        elif tag in {"p", "br", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "iframe", "object"}:
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str, max_bytes: int = MAX_BYTES) -> str:
    if len(value.encode()) > max_bytes:
        raise SecurityError("TEXT_TOO_LARGE")
    parser = _PlainText()
    parser.feed(unescape(unescape(value)))
    return " ".join("".join(parser.parts).split())


def canonical_url(value: str, *, reject_secrets: bool = True) -> str:
    """Canonicalize URLs without resolving/fetching; identity parameters are preserved."""
    if any(ord(char) < 32 for char in value) or "\\" in value:
        raise SecurityError("INVALID_URL")
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError as exc:
        raise SecurityError("INVALID_URL") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise SecurityError("URL_SCHEME_OR_HOST")
    if parts.username or parts.password or port not in {None, 80, 443}:
        raise SecurityError("URL_CREDENTIALS_OR_PORT")
    host = parts.hostname.lower().rstrip(".").encode("idna").decode("ascii")
    if ":" in host:
        host = f"[{host}]"
    if port and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    params = parse_qsl(parts.query, keep_blank_values=True)
    if reject_secrets and any(
        key.lower() in SENSITIVE or key.lower().endswith("token") for key, _ in params
    ):
        raise SecurityError("TOKEN_BEARING_URL")
    clean = sorted(
        (key, val)
        for key, val in params
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING
    )
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", urlencode(clean), ""))


def resolve_host(host: str) -> list[str]:
    return sorted(
        {str(item[4][0]) for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    )


def validate_public_url(
    value: str,
    resolver: Callable[[str], Iterable[str]] = resolve_host,
    *,
    allow_restricted: bool = False,
) -> tuple[str, list[str]]:
    url = canonical_url(value)
    host = urlsplit(url).hostname or ""
    if not allow_restricted and any(
        host == domain or host.endswith("." + domain) for domain in RESTRICTED_HOSTS
    ):
        raise SecurityError("RESTRICTED_PLATFORM_MANUAL_ONLY")
    if host in {"localhost", "metadata.google.internal"} or host.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise SecurityError("PRIVATE_HOST")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        addresses = list(resolver(host))
    else:
        addresses = [str(literal)]
    if not addresses:
        raise SecurityError("DNS_EMPTY")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise SecurityError("DNS_INVALID") from exc
        if not ip.is_global or (
            isinstance(ip, ipaddress.IPv6Address)
            and ip.ipv4_mapped is not None
            and not ip.ipv4_mapped.is_global
        ):
            raise SecurityError("PRIVATE_ADDRESS")
    return url, addresses


def private_token(key: bytes, field_type: str, value: str | bytes, key_version: str = "v1") -> str:
    if len(key) < 32:
        raise SecurityError("PRIVACY_KEY_TOO_SHORT")
    if not re.fullmatch(r"[a-z0-9_-]+", field_type) or not re.fullmatch(
        r"[a-zA-Z0-9_-]+", key_version
    ):
        raise SecurityError("INVALID_TOKEN_NAMESPACE")
    if isinstance(value, str):
        normalized = (
            value.strip().lower()
            if field_type in {"email", "recipient", "suppression"}
            else value.strip()
        )
        if field_type == "phone":
            normalized = re.sub(r"[ ()-]", "", normalized)
            if not re.fullmatch(r"\+[1-9]\d{7,14}", normalized):
                raise SecurityError("PHONE_REQUIRES_E164")
        raw = normalized.encode()
    else:
        raw = value
    prefix = f"career-radar:{key_version}:{field_type}:".encode()
    return hmac.new(key, prefix + raw, hashlib.sha256).hexdigest()


def redact(value: str) -> str:
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", value)
    text = re.sub(
        r"(?i)((?:token|password|secret|api[_-]?key|authorization|code|signature)[=:\s]+)[^\s&;,]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
    return re.sub(r"(?<!\w)\+?\d[\d ()-]{7,}\d(?!\w)", "[PHONE]", text)


def safe_xml(
    content: bytes, *, max_depth: int = 64, max_nodes: int = 100_000, max_attributes: int = 100
) -> Element:
    if len(content) > MAX_BYTES:
        raise SecurityError("XML_TOO_LARGE")
    upper = content.upper()
    if any(
        marker in upper
        for marker in (b"<!DOCTYPE", b"<!ENTITY", b"XINCLUDE", b"HTTP://WWW.W3.ORG/2001/XINCLUDE")
    ):
        raise SecurityError("XML_FORBIDDEN_CONSTRUCT")
    try:
        root: Element = ElementTree.fromstring(
            content, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
    except Exception as exc:
        raise SecurityError("XML_MALFORMED") from exc
    pending = [(root, 1)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > max_depth or count > max_nodes or len(node.attrib) > max_attributes:
            raise SecurityError("XML_STRUCTURE_LIMIT")
        pending.extend((child, depth + 1) for child in node)
    return root
