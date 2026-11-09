"""Immutable data passed between scheduling, fetching, parsing and output."""

from dataclasses import dataclass
from enum import Enum


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class CrawlTask:
    """One URL, its first-discovered depth and retries beyond the initial fetch."""

    fetch_url: str
    depth: int
    retry_count: int = 0


@dataclass(frozen=True)
class TaskRecord:
    """Read-only snapshot of a URL's current task and status."""

    task: CrawlTask
    status: TaskStatus


@dataclass(frozen=True)
class FetchResult:
    """One HTTP attempt, including failures and complete bounded HTML bytes."""

    fetch_url: str
    status_code: int | None = None
    content_type: str | None = None
    html: bytes | None = None
    redirect_target: str | None = None
    error: str | None = None
    skip_reason: str | None = None
    retryable: bool = False


@dataclass(frozen=True)
class ParsedPage:
    """Distinct resolved hyperlinks in document order, retaining fragments."""

    links: tuple[str, ...] = ()


@dataclass(frozen=True)
class CrawlAttempt:
    """One request and its processing outcome; numbering starts at 1."""

    attempt: int
    status_code: int | None
    error: str | None


@dataclass(frozen=True)
class CrawlResult:
    """Lightweight page metadata; HTML lives only in the referenced file."""

    fetch_url: str
    depth: int
    status_code: int | None = None
    content_type: str | None = None
    html_file: str | None = None
    # None means unparsed; an empty tuple means parsing found no links.
    links: tuple[str, ...] | None = None
    redirect_target: str | None = None
    error: str | None = None
    skip_reason: str | None = None
    retry_count: int = 0
    attempts: tuple[CrawlAttempt, ...] = ()

    @property
    def success(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class CrawlSummary:
    attempted: int
    failed: int
    truncated: bool


@dataclass(frozen=True)
class CrawlReport:
    """Completed crawl, ready for synchronous console and JSON output."""

    results: tuple[CrawlResult, ...]
    summary: CrawlSummary
