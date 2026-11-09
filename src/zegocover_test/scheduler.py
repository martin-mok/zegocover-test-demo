"""Crawl admission, task status and bounded FIFO retries."""

import asyncio
import logging
from dataclasses import replace
from typing import Protocol

from zegocover_test.config import CrawlConfig
from zegocover_test.logging_context import CrawlLogger
from zegocover_test.models import CrawlTask, TaskRecord, TaskStatus
from zegocover_test.urls import URLPolicy

logger = CrawlLogger(logging.getLogger(__name__))


class TaskScheduler(Protocol):
    """Admit eligible URLs as tasks and track their completion for one crawl."""

    @property
    def truncated(self) -> bool: ...

    def admit(self, resolved_url: str, depth: int) -> bool: ...

    async def next_task(self) -> CrawlTask: ...

    def complete(self, task: CrawlTask) -> None: ...

    def fail(self, task: CrawlTask, *, retryable: bool) -> bool: ...

    def get_record(self, resolved_url: str) -> TaskRecord | None: ...

    async def join(self) -> None: ...

    def shutdown(self, *, immediate: bool = False) -> None: ...


class Scheduler:
    """Decide which URLs become tasks, using shared URL rules and crawl limits."""

    def __init__(self, config: CrawlConfig, urls: URLPolicy) -> None:
        self._config = config
        self._urls = urls
        # Validate every seed before starting work; never normalize seeds during
        # candidate scope checks. URL parsing stays in the shared URL policy.
        self._allowed_hosts = frozenset(
            urls.host(urls.normalize(seed)) for seed in config.seed_urls
        )
        self._pending: asyncio.Queue[CrawlTask] = asyncio.Queue(
            maxsize=config.max_pages
        )
        self._records: dict[str, TaskRecord] = {}
        self._closed = False
        self.truncated = False

    def admit(self, resolved_url: str, depth: int) -> bool:
        """Create and enqueue an eligible unseen task at its first-discovered depth."""
        if self._closed:
            raise asyncio.QueueShutDown
        if self._config.max_depth is not None and depth > self._config.max_depth:
            logger.debug(
                "Rejected candidate: resolved_url=%r depth=%d exceeds max_depth=%d",
                resolved_url,
                depth,
                self._config.max_depth,
            )
            return False
        fetch_url = self._urls.to_fetch_url(resolved_url)
        if fetch_url is None:
            logger.debug("Rejected invalid candidate: resolved_url=%r", resolved_url)
            return False
        if not self._config.follow_external and not self._urls.has_allowed_host(
            fetch_url, self._allowed_hosts
        ):
            logger.debug("Rejected out-of-scope candidate: fetch_url=%s", fetch_url)
            return False
        if fetch_url in self._records:
            logger.debug("Rejected duplicate candidate: fetch_url=%s", fetch_url)
            return False
        if len(self._records) >= self._config.max_pages:
            if not self.truncated:
                logger.warning(
                    "Page limit reached: max_pages=%d; eligible URLs will be omitted",
                    self._config.max_pages,
                )
            self.truncated = True
            logger.debug("Rejected candidate at page limit: fetch_url=%s", fetch_url)
            return False
        task = CrawlTask(fetch_url=fetch_url, depth=depth)
        self._pending.put_nowait(task)
        self._records[fetch_url] = TaskRecord(task, TaskStatus.PENDING)
        logger.debug(
            "Admitted task: fetch_url=%s depth=%d queued=%d admitted=%d",
            fetch_url,
            depth,
            self._pending.qsize(),
            len(self._records),
        )
        return True

    async def next_task(self) -> CrawlTask:
        """Take the next queued task and mark it active before returning it."""
        task = await self._pending.get()
        self._records[task.fetch_url] = TaskRecord(task, TaskStatus.IN_PROGRESS)
        return task

    def complete(self, task: CrawlTask) -> None:
        """Finish the current attempt after storage, discovery and result collection."""
        self._check_active(task)
        self._records[task.fetch_url] = TaskRecord(task, TaskStatus.COMPLETED)
        self._pending.task_done()

    def fail(self, task: CrawlTask, *, retryable: bool) -> bool:
        """Requeue only retryable failures with retries left; otherwise mark FAILED."""
        self._check_active(task)
        retry = (
            retryable
            and not self._closed
            and task.retry_count < self._config.max_retries
        )
        if retry:
            task = replace(task, retry_count=task.retry_count + 1)
            # Enqueue before acknowledging this attempt, so join cannot wake
            # between attempts. One task per URL keeps retries within capacity.
            self._pending.put_nowait(task)
        self._records[task.fetch_url] = TaskRecord(
            task, TaskStatus.PENDING if retry else TaskStatus.FAILED
        )
        self._pending.task_done()
        return retry

    def get_record(self, resolved_url: str) -> TaskRecord | None:
        """Return an immutable snapshot, or None for an invalid/unadmitted URL."""
        fetch_url = self._urls.to_fetch_url(resolved_url)
        return self._records.get(fetch_url) if fetch_url is not None else None

    def _check_active(self, task: CrawlTask) -> None:
        record = self._records.get(task.fetch_url)
        if (
            record is None
            or record.status is not TaskStatus.IN_PROGRESS
            or record.task != task
        ):
            raise ValueError("Task is not the current in-progress attempt")

    async def join(self) -> None:
        """Wait for all admitted tasks, including work discovered by active ones."""
        await self._pending.join()

    def shutdown(self, *, immediate: bool = False) -> None:
        """Stop admission/retries; drain normally or fail all unfinished work.

        The engine calls immediate shutdown only after TaskGroup has stopped its
        workers. Completed/failed records remain inspectable on this single-use
        scheduler; shutdown never silently forgets pending or active URLs.
        """
        logger.debug(
            "Scheduler shutdown: immediate=%s queued=%d",
            immediate,
            self._pending.qsize(),
        )
        self._closed = True
        self._pending.shutdown(immediate=immediate)
        if immediate:
            for record in self._records.values():
                if record.status is TaskStatus.IN_PROGRESS:
                    # Queue.shutdown drains pending items; active items still
                    # need an acknowledgement after their workers have stopped.
                    self._pending.task_done()
                if record.status in {TaskStatus.PENDING, TaskStatus.IN_PROGRESS}:
                    self._records[record.task.fetch_url] = TaskRecord(
                        record.task, TaskStatus.FAILED
                    )
