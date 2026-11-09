"""Shared URL validation, display resolution, request identity, and scope."""

import ipaddress
import logging
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

from zegocover_test.logging_context import CrawlLogger

logger = CrawlLogger(logging.getLogger(__name__))


class URLPolicy(Protocol):
    """URL operations shared by admission, parsing, and redirect handling."""

    def normalize(self, value: str, *, preserve_fragment: bool = False) -> str: ...

    def resolve_href(self, base_url: str, raw_href: str) -> str | None: ...

    def to_fetch_url(self, resolved_url: str) -> str | None: ...

    def host(self, fetch_url: str) -> bytes: ...

    def has_allowed_host(
        self, fetch_url: str, allowed_hosts: frozenset[bytes]
    ) -> bool: ...


def _check_controls(value: str) -> None:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("URL contains control characters")


def _parse_http_url(value: str) -> SplitResult:
    """Check URL structure before either parser can discard input."""
    _check_controls(value)
    if value.startswith(" "):
        raise ValueError("URL must not start with whitespace")
    # urlsplit parses components; it does not validate an HTTP URL or its host.
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL must be absolute HTTP(S) with a hostname")
    if parts.username is not None:
        raise ValueError("URL credentials are not supported")
    # Accessing .port rejects nonnumeric and out-of-range ports; also reject 0.
    if parts.port == 0:
        raise ValueError("URL port must be between 1 and 65535")
    return parts


def _remove_dot_segments(path: str) -> str:
    """Remove dot segments from an absolute path, retaining empty segments."""
    segments: list[str] = []
    for segment in path.split("/"):
        if segment == "..":
            if len(segments) > 1:
                segments.pop()
        elif segment != ".":
            segments.append(segment)
    if path.endswith(("/.", "/..")):
        segments.append("")
    return "/".join(segments)


def _resolve_reference(base_url: str, raw_href: str) -> str:
    """Merge references without urljoin's loss of empty queries/path segments."""
    parts = urlsplit(raw_href)
    if parts.scheme:
        return raw_href
    base = _parse_http_url(base_url)
    if raw_href.startswith("//"):
        return f"{base.scheme}:{raw_href}"

    reference, fragment_separator, fragment = raw_href.partition("#")
    _, query_separator, query = reference.partition("?")
    path = parts.path
    if not path:
        path = base.path
        if not query_separator:
            _, query_separator, query = base_url.partition("#")[0].partition("?")
    elif not path.startswith("/"):
        path = base.path.rpartition("/")[0] + "/" + path

    # Dot segments are removed by normalize(), after resolving the full path.
    resolved_url = urlunsplit((base.scheme, base.netloc, path, "", ""))
    return resolved_url + query_separator + query + fragment_separator + fragment


def _is_ip_number(label: str) -> bool:
    """Recognize decimal and legacy hexadecimal IPv4 components."""
    if label.startswith("0x"):
        digits = label[2:]
        return bool(digits) and all(char in "0123456789abcdef" for char in digits)
    return label.isdecimal()


def _normalize_host(host: str) -> str:
    """Validate an HTTPX ASCII host as an IP literal or a DNS hostname."""
    host = host.removesuffix(".")
    if ":" in host:
        if "%" in host:
            raise ValueError("IPv6 zone identifiers are not supported")
        return str(ipaddress.IPv6Address(host))

    labels = host.split(".")
    if all(_is_ip_number(label) for label in labels):
        # Require dotted decimal IPv4. Failed IP validation must not fall back
        # to DNS: resolvers can interpret short, integer, octal or hex forms.
        return str(ipaddress.IPv4Address(host))

    if len(host) > 253:
        raise ValueError("URL hostname is too long")
    for label in labels:
        if not 1 <= len(label) <= 63 or label.startswith("-") or label.endswith("-"):
            raise ValueError("URL has an invalid hostname label")
        if not label.isascii() or not label.replace("-", "").isalnum():
            raise ValueError(
                "URL hostname labels must contain letters, digits or hyphens"
            )
    return host


class HttpURLPolicy:
    """Stateless HTTP(S) identity and exact-host scope policy.

    Inject one policy into the scheduler, fetcher, and parser so all stages agree.
    Resolution, request identity, and scope share the same normalizer.
    """

    def normalize(self, value: str, *, preserve_fragment: bool = False) -> str:
        """Validate an absolute HTTP(S) URL and serialize it as HTTPX will send it."""
        parts = _parse_http_url(value)
        try:
            url = httpx.URL(value)
            url = url.copy_with(
                host=_normalize_host(url.raw_host.decode("ascii")),
                path=_remove_dot_segments(parts.path) or "/",
            )
            if not preserve_fragment:
                url = url.copy_with(fragment=None)
            return str(url)
        except httpx.InvalidURL as exc:
            raise ValueError(f"Invalid URL: {exc}") from exc

    def to_fetch_url(self, resolved_url: str) -> str | None:
        """Return the fragment-free HTTP(S) URL to fetch and deduplicate."""
        try:
            return self.normalize(resolved_url)
        except ValueError:
            logger.debug(
                "Invalid fetch candidate: resolved_url=%r", resolved_url, exc_info=True
            )
            return None

    def resolve_href(self, base_url: str, raw_href: str) -> str | None:
        """Resolve an HTML href to a reportable URL, retaining fragments."""
        try:
            _check_controls(base_url)
            _check_controls(raw_href)
            cleaned_href = raw_href.strip()
            resolved_url = _resolve_reference(base_url, cleaned_href)
            if urlsplit(resolved_url).scheme.lower() in {"http", "https"}:
                return self.normalize(resolved_url, preserve_fragment=True)
            return resolved_url
        except ValueError:
            logger.debug(
                "Could not resolve hyperlink: base_url=%r raw_href=%r",
                base_url,
                raw_href,
                exc_info=True,
            )
            return None

    def host(self, fetch_url: str) -> bytes:
        """Extract an ASCII host from an already validated, normalized fetch URL."""
        return httpx.URL(fetch_url).raw_host

    def has_allowed_host(self, fetch_url: str, allowed_hosts: frozenset[bytes]) -> bool:
        """Check exact host membership for a normalized URL, ignoring scheme/port."""
        return self.host(fetch_url) in allowed_hosts
