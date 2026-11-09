"""Coordinate one fetch-worker pool and collect metadata for a final report."""

import asyncio
import logging
from dataclasses import replace
from functools import partial

from zegocover_test.config import CrawlConfig
from zegocover_test.fetcher import PageFetcher
from zegocover_test.logging_context import CrawlLogger, current_url, current_worker
from zegocover_test.models import (
    CrawlAttempt,
    CrawlReport,
    CrawlResult,
    CrawlSummary,
    CrawlTask,
    FetchResult,
)
from zegocover_test.parser import PageParser
from zegocover_test.scheduler import TaskScheduler
from zegocover_test.writer import PageWriter

logger = CrawlLogger(logging.getLogger(__name__))


class CrawlEngine:
    """Single-use coordinator; the caller owns the HTTP client and HTML writer."""

    def __init__(
        self,
        config: CrawlConfig,
        *,
        scheduler: TaskScheduler,
        fetcher: PageFetcher,
        parser: PageParser,
        html_writer: PageWriter,
    ) -> None:
        self._config = config
        self._scheduler = scheduler
        self._fetcher = fetcher
        self._parser = parser
        self._html_writer = html_writer
        self._started = False

    async def run(self) -> CrawlReport:
        """Finish all admitted work and close workers before returning results."""
        if self._started:
            raise RuntimeError(
                "CrawlEngine is single-use; create a new engine and scheduler"
            )
        self._started = True
        results: list[CrawlResult] = []
        attempts: dict[str, list[CrawlAttempt]] = {}
        coordinator = asyncio.current_task()
        assert coordinator is not None
        worker_count = min(self._config.concurrency, self._config.max_pages)
        logger.info(
            "Starting crawl: seeds=%d workers=%d max_pages=%d max_depth=%s follow_external=%s",
            len(self._config.seed_urls),
            worker_count,
            self._config.max_pages,
            self._config.max_depth,
            self._config.follow_external,
        )
        try:
            for seed in self._config.seed_urls:
                self._scheduler.admit(seed, depth=0)
            async with asyncio.TaskGroup() as group:
                for index in range(worker_count):
                    worker = group.create_task(
                        self._fetch_worker(results, attempts, f"crawl-fetch-{index}"),
                        name=f"crawl-fetch-{index}",
                    )
                    worker.add_done_callback(partial(self._worker_done, coordinator))
                # Retries are queued before acknowledging the failed attempt,
                # so queue.join() also waits for retries and discovered children.
                await self._scheduler.join()
                logger.debug("All admitted tasks completed; shutting down workers")
                self._scheduler.shutdown()
        except asyncio.CancelledError:
            self._scheduler.shutdown(immediate=True)
            logger.info("Crawl cancelled; workers stopped")
            raise
        except BaseException:
            # TaskGroup has already cancelled and joined all workers.
            self._scheduler.shutdown(immediate=True)
            logger.debug("Crawl aborted; workers stopped", exc_info=True)
            raise
        report = CrawlReport(
            results=tuple(results),
            summary=CrawlSummary(
                attempted=len(results),
                failed=sum(not result.success for result in results),
                truncated=self._scheduler.truncated,
            ),
        )
        logger.info(
            "Crawl completed: attempted=%d failed=%d truncated=%s",
            report.summary.attempted,
            report.summary.failed,
            report.summary.truncated,
        )
        return report

    def _worker_done(
        self, coordinator: asyncio.Task[object], worker: asyncio.Task[None]
    ) -> None:
        # TaskGroup propagates exceptions but treats a cancelled child as normal.
        # Losing a worker independently must abort this single-use crawl, even if
        # it was cancelled before starting or while waiting for its next task.
        if (
            worker.cancelled()
            and not coordinator.done()
            and not coordinator.cancelling()
        ):
            coordinator.cancel()

    async def _fetch_worker(
        self,
        results: list[CrawlResult],
        attempts: dict[str, list[CrawlAttempt]],
        worker_name: str,
    ) -> None:
        worker_token = current_worker.set(worker_name)
        logger.debug("Worker started")
        try:
            while True:
                logger.debug("Worker waiting for task")
                try:
                    task = await self._scheduler.next_task()
                except asyncio.QueueShutDown:
                    logger.debug("Worker queue drained")
                    return
                url_token = current_url.set(task.fetch_url)
                try:
                    logger.info(
                        "Processing task: depth=%d retry_count=%d",
                        task.depth,
                        task.retry_count,
                    )
                    await self._run_task(task, results, attempts)
                except asyncio.CancelledError:
                    logger.info("Task cancelled")
                    raise
                except BaseException:
                    logger.exception("Task processing raised an exception")
                    raise
                finally:
                    current_url.reset(url_token)
        finally:
            logger.debug("Worker stopped")
            current_worker.reset(worker_token)

    async def _run_task(
        self,
        task: CrawlTask,
        results: list[CrawlResult],
        attempts: dict[str, list[CrawlAttempt]],
    ) -> None:
        """Handle one attempt as a success, retry, or final failure."""
        retryable = False
        try:
            fetched = await self._fetcher.fetch(task.fetch_url)
        except Exception as exc:
            logger.exception("Fetch raised an exception")
            result = CrawlResult(
                fetch_url=task.fetch_url,
                depth=task.depth,
                retry_count=task.retry_count,
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            result = self._process(task, fetched)
            retryable = fetched.error is not None and fetched.retryable

        # Record this outcome before requeueing, so another worker can pick up
        # the next attempt without losing earlier errors or response statuses.
        attempts.setdefault(task.fetch_url, []).append(
            CrawlAttempt(task.retry_count + 1, result.status_code, result.error)
        )
        if result.success:
            results.append(
                replace(result, attempts=tuple(attempts.pop(task.fetch_url)))
            )
            self._scheduler.complete(task)
            logger.info(
                "Task completed: status=%s links=%d redirect=%s skipped=%s",
                result.status_code,
                len(result.links or ()),
                result.redirect_target,
                result.skip_reason,
            )
        elif self._scheduler.fail(task, retryable=retryable):
            logger.warning(
                "Task retry scheduled: retry_count=%d error=%s",
                task.retry_count + 1,
                result.error,
            )
        else:
            results.append(
                replace(result, attempts=tuple(attempts.pop(task.fetch_url)))
            )
            logger.warning(
                "Task failed: status=%s error=%s", result.status_code, result.error
            )

    def _process(self, task: CrawlTask, fetched: FetchResult) -> CrawlResult:
        """Save and parse a page, retaining metadata when either operation fails."""
        html_file = None
        links = None
        error = fetched.error
        if error is None and fetched.html is not None:
            try:
                logger.debug("Saving HTML: bytes=%d", len(fetched.html))
                html_file = self._html_writer.write(task.fetch_url, fetched.html)
                logger.debug("HTML saved: file=%s; parsing links", html_file)
                links = self._parser.parse(
                    fetched.html, task.fetch_url, fetched.content_type
                ).links
                logger.debug("Parsed HTML: links=%d", len(links))
            except Exception as exc:
                logger.exception("Page processing failed")
                error = f"{type(exc).__name__}: {exc}"
        if error is None and fetched.redirect_target is not None:
            self._scheduler.admit(fetched.redirect_target, task.depth + 1)
        for resolved_url in links or ():
            self._scheduler.admit(resolved_url, task.depth + 1)
        # Only metadata leaves this method; the body is released before the
        # worker waits for another task.
        return CrawlResult(
            fetch_url=task.fetch_url,
            depth=task.depth,
            retry_count=task.retry_count,
            status_code=fetched.status_code,
            content_type=fetched.content_type,
            html_file=html_file,
            links=links,
            redirect_target=fetched.redirect_target,
            error=error,
            skip_reason=fetched.skip_reason,
        )
