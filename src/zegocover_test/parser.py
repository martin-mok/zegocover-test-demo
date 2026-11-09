"""Pure extraction of static hyperlinks from bounded HTML bytes."""

import logging
from email.message import Message
from typing import Protocol

from bs4 import BeautifulSoup

from zegocover_test.logging_context import CrawlLogger
from zegocover_test.models import ParsedPage
from zegocover_test.urls import URLPolicy

logger = CrawlLogger(logging.getLogger(__name__))


class PageParser(Protocol):
    """Turn a complete HTML body into resolved, reportable hyperlinks."""

    def parse(
        self, html: bytes, page_url: str, content_type: str | None = None
    ) -> ParsedPage: ...


class HtmlParser:
    """Stateless HTML extraction with an injected URL policy."""

    def __init__(self, urls: URLPolicy) -> None:
        self._urls = urls

    def parse(
        self, html: bytes, page_url: str, content_type: str | None = None
    ) -> ParsedPage:
        """Parse a complete body using a valid declared charset when available."""
        if not html:
            return ParsedPage()
        headers = Message()
        headers["content-type"] = content_type or ""
        encoding = headers.get_content_charset()
        if encoding is not None:
            try:
                b"".decode(encoding)
            except (LookupError, UnicodeError):
                logger.debug(
                    "Ignoring invalid charset: encoding=%r",
                    encoding,
                    exc_info=True,
                    extra={"fetch_url": page_url},
                )
                encoding = None
        soup = BeautifulSoup(html, "html.parser", from_encoding=encoding)
        base_url = page_url
        base = soup.find("base", href=True)
        if base is not None:
            raw_href = base.get("href")
            if isinstance(raw_href, str):
                resolved_url = self._urls.resolve_href(page_url, raw_href)
                if (
                    resolved_url is not None
                    and self._urls.to_fetch_url(resolved_url) is not None
                ):
                    base_url = resolved_url
        # dictionary is used as an ordered set
        # It's used to remove duplicate URLs and preserve the order links appear in the document
        links: dict[str, None] = {}
        for tag in soup.find_all(["a", "area"], href=True):
            raw_href = tag.get("href")
            if isinstance(raw_href, str):
                resolved_url = self._urls.resolve_href(base_url, raw_href)
                if resolved_url is not None:
                    links[resolved_url] = None
        return ParsedPage(links=tuple(links))
