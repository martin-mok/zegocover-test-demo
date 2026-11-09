import asyncio

import pytest

from zegocover_test.config import CrawlConfig
from zegocover_test.models import CrawlTask, TaskStatus
from zegocover_test.scheduler import Scheduler
from zegocover_test.urls import HttpURLPolicy

ROOT = "https://example.test/"


def test_scheduler_shutdown_wakes_consumers_and_rejects_admission() -> None:
    async def scenario() -> None:
        scheduler = Scheduler(CrawlConfig((ROOT,), max_pages=2), HttpURLPolicy())
        async with asyncio.timeout(2):
            waiter = asyncio.create_task(scheduler.next_task())
            await asyncio.sleep(0)
            scheduler.shutdown()
            with pytest.raises(asyncio.QueueShutDown):
                await waiter
            with pytest.raises(asyncio.QueueShutDown):
                scheduler.admit(ROOT + "late", depth=0)
            # Rejection must not mark the URL seen and turn a retry into a no-op.
            with pytest.raises(asyncio.QueueShutDown):
                scheduler.admit(ROOT + "late", depth=0)
            await scheduler.join()

    asyncio.run(scenario())


def test_scheduler_normalizes_deduplicates_and_preserves_first_depth() -> None:
    async def scenario() -> None:
        scheduler = Scheduler(
            CrawlConfig(("https://EXAMPLE.test",), max_pages=2), HttpURLPolicy()
        )
        assert scheduler.admit("https://EXAMPLE.test:443/a#one", depth=2)
        assert not scheduler.admit(ROOT + "a#two", depth=1)
        assert scheduler.admit(ROOT + "b", depth=3)
        assert not scheduler.truncated
        assert not scheduler.admit(ROOT + "c", depth=0)
        assert scheduler.truncated
        first = await scheduler.next_task()
        assert first == CrawlTask(ROOT + "a", 2)
        scheduler.complete(first)
        second = await scheduler.next_task()
        assert second == CrawlTask(ROOT + "b", 3)
        scheduler.complete(second)
        assert not scheduler.admit(ROOT + "a", depth=0)
        await scheduler.join()

    asyncio.run(scenario())


def test_scheduler_rejected_urls_do_not_consume_budget_or_signal_truncation() -> None:
    scheduler = Scheduler(CrawlConfig((ROOT,), max_pages=1), HttpURLPolicy())
    rejected = (
        "/relative",
        "http://[bad",
        "mailto:hello@example.test",
        "ftp://example.test/",
        "https://user:password@example.test/",
        "https://other.test/",
        "https://sub.example.test/",
        "https://example.test.other.test/",
    )
    for resolved_url in rejected:
        assert not scheduler.admit(resolved_url, depth=1)
    assert not scheduler.truncated
    assert scheduler.admit(ROOT, depth=0)  # Rejections left the only slot available.
    for resolved_url in rejected:
        assert not scheduler.admit(resolved_url, depth=1)
    assert not scheduler.admit(ROOT + "#duplicate", depth=1)
    assert not scheduler.truncated  # Only eligible unseen URLs can exhaust the budget.


def test_scheduler_depth_rejection_does_not_mark_seen_or_truncated() -> None:
    async def scenario() -> None:
        scheduler = Scheduler(
            CrawlConfig((ROOT,), max_pages=2, max_depth=1), HttpURLPolicy()
        )
        assert scheduler.admit(ROOT, depth=0)
        assert not scheduler.admit(ROOT + "child", depth=2)
        assert scheduler.admit(ROOT + "child", depth=1)
        assert not scheduler.admit(ROOT + "grandchild", depth=2)
        assert not scheduler.truncated
        first = await scheduler.next_task()
        assert first == CrawlTask(ROOT, 0)
        scheduler.complete(first)
        second = await scheduler.next_task()
        assert second == CrawlTask(ROOT + "child", 1)
        scheduler.complete(second)
        await scheduler.join()

    asyncio.run(scenario())


def test_scheduler_uses_injected_url_policy() -> None:
    class RestrictedPolicy(HttpURLPolicy):
        def has_allowed_host(
            self, fetch_url: str, allowed_hosts: frozenset[bytes]
        ) -> bool:
            assert allowed_hosts == frozenset({b"example.test"})
            return super().has_allowed_host(
                fetch_url, allowed_hosts
            ) and not fetch_url.endswith("blocked")

    scheduler = Scheduler(
        CrawlConfig(("https://EXAMPLE.test",), max_pages=1), RestrictedPolicy()
    )
    assert not scheduler.admit(ROOT + "blocked#part", depth=1)
    assert scheduler.admit(ROOT, depth=0)
    assert not scheduler.truncated


@pytest.mark.parametrize(
    "base_url",
    ["not-a-url", "https://user:password@example.test/", "https://999.999.9999/"],
)
def test_scheduler_rejects_invalid_seed(base_url: str) -> None:
    with pytest.raises(ValueError):
        Scheduler(CrawlConfig((base_url,)), HttpURLPolicy())


def test_scheduler_join_includes_active_task_and_discovered_children() -> None:
    async def scenario() -> None:
        scheduler = Scheduler(CrawlConfig((ROOT,), max_pages=2), HttpURLPolicy())
        started = asyncio.Event()

        async def wait_until_done() -> None:
            started.set()
            await scheduler.join()

        scheduler.admit(ROOT, depth=0)
        first = await scheduler.next_task()
        async with asyncio.timeout(2), asyncio.TaskGroup() as group:
            waiting = group.create_task(wait_until_done())
            await started.wait()
            assert not waiting.done()  # Queue empty, but root is still active.
            scheduler.admit(ROOT + "child", depth=1)
            scheduler.complete(first)
            assert not waiting.done()
            second = await scheduler.next_task()
            scheduler.complete(second)
        assert waiting.done()

    asyncio.run(scenario())


def test_scheduler_caches_seed_hosts_without_repeated_normalization() -> None:
    normalized: list[str] = []

    class CountingPolicy(HttpURLPolicy):
        def normalize(self, value: str, *, preserve_fragment: bool = False) -> str:
            normalized.append(value)
            return super().normalize(value, preserve_fragment=preserve_fragment)

    seeds = ("https://EXAMPLE.test./", "https://python.test/")
    scheduler = Scheduler(CrawlConfig(seeds), CountingPolicy())
    assert normalized == list(seeds)
    candidates = (
        ROOT + "a",
        "http://python.test:8080/a",
        "https://other.test/",
        ROOT + "a#again",
    )
    assert scheduler.admit(candidates[0], depth=1)
    assert scheduler.admit(candidates[1], depth=1)
    assert not scheduler.admit(candidates[2], depth=1)
    assert not scheduler.admit(candidates[3], depth=1)
    assert normalized == [
        *seeds,
        *candidates,
    ]  # Exactly one normalization per candidate.


@pytest.mark.parametrize("follow_external", [False, True])
def test_scheduler_external_scope_keeps_validation_depth_and_page_budget(
    follow_external: bool,
) -> None:
    scheduler = Scheduler(
        CrawlConfig((ROOT,), max_pages=2, max_depth=1, follow_external=follow_external),
        HttpURLPolicy(),
    )
    assert scheduler.admit(ROOT, depth=0)
    for candidate in (
        "mailto:hello@example.test",
        "https://user:pw@other.test/",
        "http://[bad",
        "/relative",
        "https://999.999.9999/",
        "https://127.1/",
        "https://0x7f000001/",
    ):
        assert not scheduler.admit(candidate, depth=1)
    assert not scheduler.admit("https://external.test/deep", depth=2)
    assert scheduler.admit("https://external.test/", depth=1) is follow_external
    assert not scheduler.truncated
    assert scheduler.admit(ROOT + "child", depth=1) is (not follow_external)
    assert scheduler.truncated is follow_external


@pytest.mark.parametrize("follow_external", [False, True])
def test_scheduler_validates_every_seed_even_when_external_following_is_enabled(
    follow_external: bool,
) -> None:
    with pytest.raises(ValueError):
        Scheduler(
            CrawlConfig((ROOT, "not-a-url"), follow_external=follow_external),
            HttpURLPolicy(),
        )


def test_lifecycle_records_are_normalized_immutable_snapshots() -> None:
    async def scenario() -> None:
        scheduler = Scheduler(CrawlConfig((ROOT,)), HttpURLPolicy())
        assert scheduler.get_record(ROOT) is None
        assert scheduler.get_record("not-a-url") is None
        scheduler.admit(ROOT, depth=0)
        pending = scheduler.get_record("https://EXAMPLE.test:443/#part")
        assert pending is not None and pending.status is TaskStatus.PENDING
        assert pending.task.retry_count == 0
        task = await scheduler.next_task()
        active = scheduler.get_record(ROOT)
        assert active is not None and active.status is TaskStatus.IN_PROGRESS
        scheduler.complete(task)
        completed = scheduler.get_record(ROOT)
        assert completed is not None and completed.status is TaskStatus.COMPLETED
        assert completed.task.retry_count == 0
        assert pending.status is TaskStatus.PENDING  # Snapshots never change.
        await scheduler.join()

    asyncio.run(scenario())


@pytest.mark.parametrize("max_retries", [0, 1, 3])
def test_retry_exhaustion_preserves_depth_budget_and_join(max_retries: int) -> None:
    async def scenario() -> None:
        scheduler = Scheduler(
            CrawlConfig((ROOT,), max_pages=1, max_retries=max_retries), HttpURLPolicy()
        )
        scheduler.admit(ROOT, depth=2)
        async with asyncio.timeout(2), asyncio.TaskGroup() as group:
            joined = group.create_task(scheduler.join())
            for retry_count in range(max_retries + 1):
                task = await scheduler.next_task()
                assert task == CrawlTask(ROOT, depth=2, retry_count=retry_count)
                await asyncio.sleep(0)
                assert not joined.done()
                retry = scheduler.fail(task, retryable=True)
                assert retry is (retry_count < max_retries)
                assert not scheduler.admit(ROOT + "#again", depth=0)
                assert not scheduler.truncated
                record = scheduler.get_record(ROOT)
                assert record is not None
                assert record.status is (
                    TaskStatus.PENDING if retry else TaskStatus.FAILED
                )
                assert record.task.retry_count == retry_count + int(retry)
                await asyncio.sleep(0)
                assert joined.done() is (not retry)
            await joined

    asyncio.run(scenario())


@pytest.mark.parametrize("already_retried", [False, True])
def test_nonretryable_failure_finishes_immediately(already_retried: bool) -> None:
    async def scenario() -> None:
        scheduler = Scheduler(CrawlConfig((ROOT,), max_retries=3), HttpURLPolicy())
        scheduler.admit(ROOT, depth=0)
        task = await scheduler.next_task()
        if already_retried:
            assert scheduler.fail(task, retryable=True)
            task = await scheduler.next_task()
        assert not scheduler.fail(task, retryable=False)
        async with asyncio.timeout(2):
            await scheduler.join()
        record = scheduler.get_record(ROOT)
        assert record is not None and record.status is TaskStatus.FAILED
        assert record.task.retry_count == int(already_retried)
        scheduler.shutdown()
        with pytest.raises(asyncio.QueueShutDown):
            await scheduler.next_task()

    asyncio.run(scenario())


def test_retry_goes_to_queue_tail_and_success_preserves_its_count() -> None:
    async def scenario() -> None:
        scheduler = Scheduler(
            CrawlConfig((ROOT,), max_pages=2, max_retries=1), HttpURLPolicy()
        )
        scheduler.admit(ROOT, depth=0)
        first = await scheduler.next_task()
        scheduler.admit(ROOT + "child", depth=1)
        assert scheduler.fail(first, retryable=True)
        async with asyncio.timeout(2):
            child = await scheduler.next_task()
            assert child == CrawlTask(ROOT + "child", 1, retry_count=0)
            scheduler.complete(child)
            retry = await scheduler.next_task()
            assert retry == CrawlTask(ROOT, 0, retry_count=1)
            scheduler.complete(retry)
            await scheduler.join()
        record = scheduler.get_record(ROOT)
        assert record is not None and record.status is TaskStatus.COMPLETED
        assert record.task.retry_count == 1
        assert not scheduler.admit(ROOT, depth=1)
        assert not scheduler.truncated

    asyncio.run(scenario())


def test_invalid_duplicate_and_stale_acknowledgements_do_not_finish_work() -> None:
    async def scenario() -> None:
        scheduler = Scheduler(CrawlConfig((ROOT,), max_retries=1), HttpURLPolicy())
        scheduler.admit(ROOT, depth=0)
        with pytest.raises(ValueError, match="in-progress"):
            scheduler.complete(CrawlTask(ROOT, 0))
        first = await scheduler.next_task()
        with pytest.raises(ValueError):
            scheduler.fail(CrawlTask(ROOT + "unknown", 0), retryable=True)
        with pytest.raises(ValueError):
            scheduler.complete(CrawlTask(ROOT, 3))
        scheduler.fail(first, retryable=True)
        retry = await scheduler.next_task()
        with pytest.raises(ValueError):
            scheduler.complete(first)
        with pytest.raises(ValueError):
            scheduler.fail(first, retryable=True)
        assert not scheduler.fail(retry, retryable=True)
        with pytest.raises(ValueError):
            scheduler.complete(retry)
        with pytest.raises(ValueError):
            scheduler.fail(retry, retryable=True)
        await scheduler.join()

    asyncio.run(scenario())


def test_shutdown_fails_pending_and_active_tasks_and_drains_queue() -> None:
    async def scenario() -> None:
        scheduler = Scheduler(CrawlConfig((ROOT,), max_retries=1), HttpURLPolicy())
        scheduler.admit(ROOT, depth=0)
        scheduler.complete(await scheduler.next_task())
        scheduler.admit(ROOT + "active", depth=1)
        await scheduler.next_task()
        scheduler.admit(ROOT + "retry", depth=1)
        scheduler.fail(await scheduler.next_task(), retryable=True)
        async with asyncio.timeout(2):
            scheduler.shutdown(immediate=True)
            scheduler.shutdown(immediate=True)  # Idempotent accounting.
            with pytest.raises(asyncio.QueueShutDown):
                await scheduler.next_task()
            await scheduler.join()
        for url in (ROOT + "active", ROOT + "retry"):
            record = scheduler.get_record(url)
            assert record is not None and record.status is TaskStatus.FAILED
            assert record.task.retry_count == int(url.endswith("retry"))
        completed = scheduler.get_record(ROOT)
        assert completed is not None and completed.status is TaskStatus.COMPLETED

    asyncio.run(scenario())


def test_graceful_shutdown_drains_pending_work_and_disables_retries() -> None:
    async def scenario() -> None:
        scheduler = Scheduler(CrawlConfig((ROOT,), max_retries=3), HttpURLPolicy())
        scheduler.admit(ROOT, depth=0)
        scheduler.admit(ROOT + "child", depth=1)
        scheduler.shutdown()
        scheduler.complete(await scheduler.next_task())
        assert not scheduler.fail(await scheduler.next_task(), retryable=True)
        with pytest.raises(asyncio.QueueShutDown):
            await scheduler.next_task()
        await scheduler.join()

    asyncio.run(scenario())
