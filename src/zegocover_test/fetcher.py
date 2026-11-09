"""One bounded HTTP request, with redirects returned as data."""

import logging
from typing import Protocol

import httpx

from zegocover_test.config import MAX_BODY_BYTES
from zegocover_test.logging_context import CrawlLogger
from zegocover_test.models import FetchResult
from zegocover_test.urls import URLPolicy

HTML_TYPES = {"text/html", "application/xhtml+xml"}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
logger = CrawlLogger(logging.getLogger(__name__))


class PageFetcher(Protocol):
    """Fetch one URL; expected HTTP errors are returned as result data."""

    async def fetch(self, fetch_url: str) -> FetchResult: ...


class HttpFetcher:
    """Borrow an open client; its caller owns connection-pool lifetime."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        urls: URLPolicy,
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")

        self.client = client
        self.urls = urls
        self.max_body_bytes = max_body_bytes

    async def fetch(self, url: str) -> FetchResult:
        context = {"fetch_url": url}
        logger.debug("Sending GET request", extra=context)
        try:
            async with self.client.stream(
                "GET",
                url,
                follow_redirects=False,
                headers={"Accept-Encoding": "identity"},
            ) as response:
                status = response.status_code
                content_type = response.headers.get("content-type")
                logger.debug(
                    "Received response: status=%d content_type=%s",
                    status,
                    content_type,
                    extra=context,
                )

                if 300 <= status < 400:
                    if status not in REDIRECT_STATUSES:
                        return FetchResult(
                            fetch_url=url,
                            status_code=status,
                            content_type=content_type,
                            error=f"Unsupported HTTP status {status}",
                        )

                    location = response.headers.get("location")
                    target = self.urls.resolve_href(url, location) if location else None
                    logger.debug(
                        "Redirect: location=%r resolved_url=%r",
                        location,
                        target,
                        extra=context,
                    )

                    if target and self.urls.to_fetch_url(target):
                        return FetchResult(
                            fetch_url=url,
                            status_code=status,
                            content_type=content_type,
                            redirect_target=target,
                        )

                    return FetchResult(
                        fetch_url=url,
                        status_code=status,
                        content_type=content_type,
                        error="Missing or invalid redirect Location",
                    )

                if not 200 <= status < 300:
                    return FetchResult(
                        fetch_url=url,
                        status_code=status,
                        content_type=content_type,
                        error=f"HTTP {status}",
                        retryable=status in RETRYABLE_STATUSES,
                    )

                media_type = (content_type or "").split(";")[0].strip().lower()

                if media_type not in HTML_TYPES:
                    return FetchResult(
                        fetch_url=url,
                        status_code=status,
                        content_type=content_type,
                        skip_reason=(
                            f"Non-HTML content type: {content_type}"
                            if content_type
                            else "Missing content type"
                        ),
                    )

                # HTTPX's gzip decoder can finish without detecting a missing
                # compression trailer. Accept only the identity we requested.
                encoding = response.headers.get("content-encoding", "").strip().lower()
                if encoding not in {"", "identity"}:
                    return FetchResult(
                        fetch_url=url,
                        status_code=status,
                        content_type=content_type,
                        error=f"Unsupported HTML content encoding: {encoding}",
                    )

                body = bytearray()

                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self.max_body_bytes:
                        logger.debug(
                            "Discarding HTML body: size limit=%d bytes",
                            self.max_body_bytes,
                            extra=context,
                        )
                        return FetchResult(
                            fetch_url=url,
                            status_code=status,
                            content_type=content_type,
                            error="HTML body exceeds size limit",
                        )

                    body.extend(chunk)

                logger.debug(
                    "Received complete HTML: bytes=%d", len(body), extra=context
                )
                return FetchResult(
                    fetch_url=url,
                    status_code=status,
                    content_type=content_type,
                    html=bytes(body),
                )

        except httpx.RequestError as exc:
            logger.debug("Request failed: %s", exc, exc_info=True, extra=context)
            return FetchResult(
                fetch_url=url,
                error=f"{type(exc).__name__}: {exc}",
                retryable=isinstance(exc, httpx.TransportError)
                and not isinstance(
                    exc, (httpx.LocalProtocolError, httpx.UnsupportedProtocol)
                ),
            )

        except httpx.InvalidURL as exc:
            logger.debug("Invalid request URL: %s", exc, exc_info=True, extra=context)
            return FetchResult(
                fetch_url=url, error=f"Invalid request or redirect URL: {exc}"
            )
