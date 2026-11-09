"""Task-local crawl context and CLI-owned logging configuration."""

import logging
from collections.abc import Generator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TextIO

from zegocover_test.writer import terminal_text

current_worker: ContextVar[str] = ContextVar("crawl_worker", default="-")
current_url: ContextVar[str] = ContextVar("crawl_url", default="-")


class CrawlLogger(logging.LoggerAdapter[logging.Logger]):
    """Attach worker and URL context without sharing mutable worker state."""

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        kwargs["extra"] = {
            "worker": current_worker.get(),
            "fetch_url": current_url.get(),
            **(kwargs.get("extra") or {}),
        }
        return msg, kwargs


class TerminalFormatter(logging.Formatter):
    """Escape terminal controls in messages and tracebacks, retaining newlines."""

    def format(self, record: logging.LogRecord) -> str:
        return "\n".join(
            terminal_text(line) for line in super().format(record).split("\n")
        )


@contextmanager
def log_to_stream(level: str, stream: TextIO) -> Generator[None]:
    """Configure only crawler logs and restore the caller's setup on exit."""
    logger = logging.getLogger("zegocover_test")
    previous_level = logger.level
    handler = logging.StreamHandler(stream)
    handler.setLevel(level)
    handler.setFormatter(
        TerminalFormatter(
            "%(asctime)s %(levelname)s %(name)s "
            "[worker=%(worker)s url=%(fetch_url)s] %(message)s",
            defaults={"worker": "-", "fetch_url": "-"},
        )
    )
    logger.setLevel(level)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)
