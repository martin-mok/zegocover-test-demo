import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from zegocover_test.cli import CrawlerCLI
from zegocover_test.config import CrawlConfig
from zegocover_test.engine import CrawlEngine
from zegocover_test.fetcher import HttpFetcher
from zegocover_test.models import (
    CrawlAttempt,
    CrawlTask,
    FetchResult,
    ParsedPage,
    TaskStatus,
)
from zegocover_test.parser import HtmlParser
from zegocover_test.scheduler import Scheduler
from zegocover_test.urls import HttpURLPolicy
from zegocover_test.writer import HtmlWriter

ROOT = "https://example.test/"
GRAPH = {
    "/": b'<a href="/a">A</a><a href="/b">B</a>',
    "/a": b'<a href="/shared#one">Shared</a>',
    "/b": b'<a href="/shared#two">Shared</a>',
    "/shared": b'<a href="/">Cycle</a>',
}


def test_crawl_preserves_query_reset_and_empty_path_segments(tmp_path: Path) -> None:
    seed = ROOT + "catalog?sort=price"
    requests: list[str] = []
    bodies = {
        seed: b'<a href="?">Reset</a><a href="?#top">Reset again</a><a href="a//b">Child</a>',
        ROOT + "catalog?": b'<a href="catalog?sort=price">Back</a>',
        ROOT + "a//b": b"",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=bodies[str(request.url)]
        )

    report = asyncio.run(
        CrawlerCLI(transport=httpx.MockTransport(respond)).crawl(
            CrawlConfig((seed,)), HtmlWriter(tmp_path)
        )
    )
    assert set(requests) == set(bodies) and len(requests) == 3
    assert report.summary.attempted == 3 and report.summary.failed == 0
    assert not report.summary.truncated
    results = {result.fetch_url: result for result in report.results}
    assert results[seed].links == (
        ROOT + "catalog?",
        ROOT + "catalog?#top",
        ROOT + "a//b",
    )
    for url, result in results.items():
        assert result.html_file is not None
        assert (tmp_path / result.html_file).read_bytes() == bodies[url]


@pytest.mark.parametrize(
    "outcome", ["recover", "exhaust", "http404", "becomes_permanent", "transport"]
)
def test_engine_retries_with_one_final_result_per_url(
    tmp_path: Path, outcome: str
) -> None:
    async def scenario() -> None:
        requests: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            if request.url.path == "/child":
                return httpx.Response(200, headers={"content-type": "text/html"})
            attempt = requests.count(ROOT)
            if outcome == "http404" or (outcome == "becomes_permanent" and attempt > 1):
                return httpx.Response(404)
            if outcome == "transport" and attempt < 3:
                raise httpx.ReadTimeout("temporarily unavailable", request=request)
            if outcome == "exhaust" or attempt < 3:
                return httpx.Response(503)
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b'<a href="/child">Child</a><a href="/#again">Self</a>',
            )

        config = CrawlConfig((ROOT,), concurrency=3, max_pages=2, max_retries=2)
        urls = HttpURLPolicy()
        scheduler = Scheduler(config, urls)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            engine = CrawlEngine(
                config,
                scheduler=scheduler,
                fetcher=HttpFetcher(client, urls),
                parser=HtmlParser(urls),
                html_writer=HtmlWriter(tmp_path),
            )
            async with asyncio.timeout(2):
                report = await engine.run()
        recovered = outcome in {"recover", "transport"}
        expected_attempts = {"http404": 1, "becomes_permanent": 2}.get(outcome, 3)
        assert requests.count(ROOT) == expected_attempts
        assert requests.count(ROOT + "child") == int(recovered)
        assert report.summary.attempted == (2 if recovered else 1)
        assert report.summary.failed == (0 if recovered else 1)
        assert not report.summary.truncated
        assert len({result.fetch_url for result in report.results}) == len(
            report.results
        )
        assert len(list(tmp_path.glob("*.html"))) == (2 if recovered else 0)
        record = scheduler.get_record(ROOT)
        assert record is not None
        assert record.status is (
            TaskStatus.COMPLETED if recovered else TaskStatus.FAILED
        )
        assert (
            record.task.retry_count
            == report.results[0].retry_count
            == expected_attempts - 1
        )
        assert report.results[0].depth == 0
        assert report.results[0].status_code == (
            200
            if recovered
            else (404 if outcome in {"http404", "becomes_permanent"} else 503)
        )
        result = report.results[0]
        assert result.success is recovered
        if outcome == "transport":
            assert result.attempts == (
                CrawlAttempt(1, None, "ReadTimeout: temporarily unavailable"),
                CrawlAttempt(2, None, "ReadTimeout: temporarily unavailable"),
                CrawlAttempt(3, 200, None),
            )
        else:
            statuses = [503] * (expected_attempts - 1) + [result.status_code]
            assert result.attempts == tuple(
                CrawlAttempt(index, status, None if status == 200 else f"HTTP {status}")
                for index, status in enumerate(statuses, start=1)
            )
        if recovered:
            assert report.results[0].error is None
            assert report.results[1].depth == 1
            assert report.results[1].retry_count == 0
            assert report.results[1].success
            assert report.results[1].attempts == (CrawlAttempt(1, 200, None),)

    asyncio.run(scenario())


def test_attempt_history_follows_url_when_a_different_worker_takes_the_retry(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first, second, third = ROOT + "a", ROOT + "b", ROOT + "c"
        second_started, third_started, first_retried = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        workers: dict[str, list[str]] = {}

        async def respond(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            worker = asyncio.current_task()
            assert worker is not None
            workers.setdefault(url, []).append(worker.get_name())
            if url == first:
                if len(workers[url]) == 1:
                    await second_started.wait()
                    return httpx.Response(408)
                first_retried.set()
            elif url == second:
                if len(workers[url]) == 1:
                    second_started.set()
                    await third_started.wait()
                    return httpx.Response(503)
                return httpx.Response(404)
            else:
                # The first worker takes this seed before its queued retry.
                # Holding it here forces the second worker to handle that retry.
                third_started.set()
                await first_retried.wait()
            return httpx.Response(200)

        async with asyncio.timeout(2):
            report = await CrawlerCLI(transport=httpx.MockTransport(respond)).crawl(
                CrawlConfig((first, second, third), concurrency=2, max_retries=1),
                HtmlWriter(tmp_path),
            )
        assert workers[first][0] != workers[first][1]
        results = {result.fetch_url: result for result in report.results}
        assert results[first].attempts == (
            CrawlAttempt(1, 408, "HTTP 408"),
            CrawlAttempt(2, 200, None),
        )
        assert results[second].attempts == (
            CrawlAttempt(1, 503, "HTTP 503"),
            CrawlAttempt(2, 404, "HTTP 404"),
        )
        assert results[third].attempts == (CrawlAttempt(1, 200, None),)
        assert results[first].success and results[third].success
        assert not results[second].success
        assert report.summary.attempted == 3 and report.summary.failed == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("concurrency", [1, 3])
@pytest.mark.parametrize("failure_stage", ["fetch", "html_writer", "parser"])
@pytest.mark.parametrize("retry_first", [False, True])
def test_page_errors_finish_without_retrying_or_stopping_other_tasks(
    tmp_path: Path, concurrency: int, failure_stage: str, retry_first: bool
) -> None:
    async def scenario() -> None:
        requested: list[str] = []
        parsed: list[str] = []
        bad, good, child = ROOT + "bad", ROOT + "good", ROOT + "child"

        def respond(request: httpx.Request) -> httpx.Response:
            fetch_url = str(request.url)
            requested.append(fetch_url)
            if fetch_url == bad:
                if retry_first and requested.count(bad) == 1:
                    return httpx.Response(503)
                if failure_stage == "fetch":
                    raise ValueError("fetch failed")
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b'<a href="/unwanted">Unwanted</a>'
                if fetch_url == bad
                else b'<a href="/child">Child</a>',
            )

        class FailingHtmlWriter(HtmlWriter):
            def write(self, fetch_url: str, html: bytes) -> str:
                if fetch_url == bad and failure_stage == "html_writer":
                    raise OSError("html_writer failed")
                return super().write(fetch_url, html)

        class FailingParser(HtmlParser):
            def parse(
                self, html: bytes, page_url: str, content_type: str | None = None
            ) -> ParsedPage:
                parsed.append(page_url)
                if page_url == bad and failure_stage == "parser":
                    raise ValueError("parser failed")
                return super().parse(html, page_url, content_type)

        config = CrawlConfig((bad, good), concurrency=concurrency, max_retries=3)
        urls = HttpURLPolicy()
        scheduler = Scheduler(config, urls)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            engine = CrawlEngine(
                config,
                scheduler=scheduler,
                fetcher=HttpFetcher(client, urls),
                parser=FailingParser(urls),
                html_writer=FailingHtmlWriter(tmp_path),
            )
            async with asyncio.timeout(2):
                report = await engine.run()
                await scheduler.join()
        assert set(requested) == {bad, good, child}
        assert requested.count(bad) == 1 + int(retry_first)
        assert requested.count(good) == requested.count(child) == 1
        assert report.summary.attempted == 3 and report.summary.failed == 1
        assert not report.summary.truncated
        results = {result.fetch_url: result for result in report.results}
        failed = results[bad]
        assert failed.error is not None and f"{failure_stage} failed" in failed.error
        assert failed.retry_count == int(retry_first) and failed.links is None
        assert failed.status_code == (None if failure_stage == "fetch" else 200)
        assert not failed.success
        assert failed.attempts == (
            *((CrawlAttempt(1, 503, "HTTP 503"),) if retry_first else ()),
            CrawlAttempt(1 + int(retry_first), failed.status_code, failed.error),
        )
        if failure_stage == "parser":
            assert failed.html_file is not None
            assert (
                tmp_path / failed.html_file
            ).read_bytes() == b'<a href="/unwanted">Unwanted</a>'
        else:
            assert failed.html_file is None and bad not in parsed
        for url, result in results.items():
            record = scheduler.get_record(url)
            assert record is not None
            assert record.status is (
                TaskStatus.FAILED if url == bad else TaskStatus.COMPLETED
            )
            assert record.task.retry_count == result.retry_count
            assert result.success is (url != bad)

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_worker", [False, True])
def test_engine_cancellation_terminalizes_pending_and_active_work(
    tmp_path: Path, cancel_worker: bool
) -> None:
    async def scenario() -> None:
        baseline = asyncio.all_tasks()
        started = asyncio.Event()

        class FakeFetcher:
            async def fetch(self, fetch_url: str) -> FetchResult:
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        config = CrawlConfig((ROOT, ROOT + "queued"), concurrency=1, max_retries=3)
        urls = HttpURLPolicy()
        scheduler = Scheduler(config, urls)
        engine = CrawlEngine(
            config,
            scheduler=scheduler,
            fetcher=FakeFetcher(),
            parser=HtmlParser(urls),
            html_writer=HtmlWriter(tmp_path),
        )
        async with asyncio.timeout(2):
            running = asyncio.create_task(engine.run())
            await started.wait()
            active = scheduler.get_record(ROOT)
            assert active is not None
            assert active.status is TaskStatus.IN_PROGRESS
            if cancel_worker:
                worker = next(
                    task
                    for task in asyncio.all_tasks()
                    if task.get_name() == "crawl-fetch-0"
                )
                worker.cancel()
            else:
                running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            await scheduler.join()
        for url in config.seed_urls:
            record = scheduler.get_record(url)
            assert record is not None and record.status is TaskStatus.FAILED
            assert record.task.retry_count == 0
        assert asyncio.all_tasks() == baseline

    asyncio.run(scenario())


def test_engine_saves_before_parsing_with_injected_services() -> None:
    events: list[str] = []
    submitted: list[tuple[str, int]] = []
    links = (ROOT + "#self", "https://other.test/", "mailto:hello@example.test")

    class RecordingScheduler(Scheduler):
        def admit(self, resolved_url: str, depth: int) -> bool:
            submitted.append((resolved_url, depth))
            return super().admit(resolved_url, depth)

    class FakeFetcher:
        async def fetch(self, fetch_url: str) -> FetchResult:
            events.append("fetch")
            return FetchResult(fetch_url, content_type="text/html", html=b"fixture")

    class FakeHtmlWriter:
        def write(self, fetch_url: str, html: bytes) -> str:
            assert html == b"fixture" and fetch_url == ROOT
            events.append("save")
            return "fixture.html"

    class FakeParser:
        def parse(
            self, html: bytes, page_url: str, content_type: str | None = None
        ) -> ParsedPage:
            assert events == ["fetch", "save"]
            assert html == b"fixture" and page_url == ROOT
            assert content_type == "text/html"
            events.append("parse")
            return ParsedPage(links)

    engine = CrawlEngine(
        CrawlConfig((ROOT,)),
        scheduler=RecordingScheduler(CrawlConfig((ROOT,)), HttpURLPolicy()),
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        html_writer=FakeHtmlWriter(),
    )
    report = asyncio.run(engine.run())
    assert events == ["fetch", "save", "parse"]
    assert report.summary.attempted == 1 and report.summary.failed == 0
    result = report.results[0]
    assert result.html_file == "fixture.html" and result.links == links
    assert submitted == [(ROOT, 0), *((link, 1) for link in links)]
    assert not hasattr(result, "html") and not hasattr(result, "fetch")
    with pytest.raises(RuntimeError, match="single-use"):
        asyncio.run(engine.run())


@pytest.mark.parametrize("outcome", ["complete", "failure", "cancel"])
def test_engine_joins_workers_and_rejects_reuse(tmp_path: Path, outcome: str) -> None:
    async def scenario() -> None:
        baseline_tasks = asyncio.all_tasks()
        started, release = asyncio.Event(), asyncio.Event()
        normal_exits = 0

        class ObservedScheduler(Scheduler):
            async def next_task(self) -> CrawlTask:
                nonlocal normal_exits
                try:
                    return await super().next_task()
                except asyncio.QueueShutDown:
                    normal_exits += 1
                    raise

        class FakeFetcher:
            async def fetch(self, fetch_url: str) -> FetchResult:
                started.set()
                await release.wait()
                if outcome == "failure":
                    raise ValueError("programming error")
                return FetchResult(fetch_url, html=b"")

        config = CrawlConfig((ROOT,), concurrency=3, max_retries=2)
        scheduler = ObservedScheduler(config, HttpURLPolicy())
        engine = CrawlEngine(
            config,
            scheduler=scheduler,
            fetcher=FakeFetcher(),
            parser=HtmlParser(HttpURLPolicy()),
            html_writer=HtmlWriter(tmp_path),
        )
        async with asyncio.timeout(2):
            task = asyncio.create_task(engine.run())
            try:
                await started.wait()
                assert not task.done()
                with pytest.raises(RuntimeError, match="single-use"):
                    await engine.run()
                if outcome == "cancel":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    release.set()
                    report = await task
                    assert report.summary.attempted == 1
                    assert report.summary.failed == int(outcome == "failure")
                    if outcome == "failure":
                        assert (
                            report.results[0].error == "ValueError: programming error"
                        )
                    assert normal_exits == 3
                with pytest.raises(RuntimeError, match="single-use"):
                    await engine.run()
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        assert asyncio.all_tasks() == baseline_tasks
        record = scheduler.get_record(ROOT)
        assert record is not None
        assert record.status is (
            TaskStatus.COMPLETED if outcome == "complete" else TaskStatus.FAILED
        )
        assert (
            record.task.retry_count == 0
        )  # Cancellation/programming errors never retry.

    asyncio.run(scenario())


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        await self.release.wait()
        yield b""

    async def aclose(self) -> None:
        self.closed = True


class ClosingTransport(httpx.MockTransport):
    closed = False

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()


@pytest.mark.parametrize("html_writer_failure", [False, True])
def test_cancellation_and_html_writer_failure_close_resources(
    tmp_path: Path,
    html_writer_failure: bool,
) -> None:
    async def scenario() -> None:
        baseline_tasks = asyncio.all_tasks()
        stream = BlockingStream()
        failed = asyncio.Event()

        async def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    content=b'<a href="/blocked">Block</a><a href="/fail">Fail</a>',
                )
            if request.url.path == "/blocked":
                return httpx.Response(
                    200, headers={"content-type": "text/html"}, stream=stream
                )
            await stream.started.wait()
            if not html_writer_failure:
                await asyncio.Event().wait()
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=b""
            )

        class FailingHtmlWriter(HtmlWriter):
            def write(self, fetch_url: str, html: bytes) -> str:
                if html_writer_failure and fetch_url == ROOT + "fail":
                    failed.set()
                    raise OSError("disk unavailable")
                return super().write(fetch_url, html)

        transport = ClosingTransport(respond)
        async with asyncio.timeout(2):
            task = asyncio.create_task(
                CrawlerCLI(transport=transport).crawl(
                    CrawlConfig((ROOT,), concurrency=2),
                    FailingHtmlWriter(tmp_path),
                )
            )
            try:
                await stream.started.wait()
                # The seed is already on disk while other requests remain active.
                assert len(list(tmp_path.glob("*.html"))) == 1
                assert not (tmp_path / "report.json").exists()
                if html_writer_failure:
                    await failed.wait()
                    assert not task.done() and not stream.closed
                    stream.release.set()
                    report = await task
                    assert report.summary.attempted == 3
                    assert report.summary.failed == 1
                    failure = next(result for result in report.results if result.error)
                    assert failure.fetch_url == ROOT + "fail"
                    assert failure.error == "OSError: disk unavailable"
                else:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        assert stream.closed and transport.closed
        assert asyncio.all_tasks() == baseline_tasks

    asyncio.run(scenario())


@pytest.mark.parametrize("concurrency", [1, 3])
def test_crawl_finite_graph_stores_html_and_returns_metadata(
    tmp_path: Path, concurrency: int
) -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=GRAPH[request.url.path]
        )

    report = asyncio.run(
        CrawlerCLI(transport=httpx.MockTransport(respond)).crawl(
            CrawlConfig((ROOT,), concurrency=concurrency),
            HtmlWriter(tmp_path),
        )
    )
    assert set(requests) == {ROOT, ROOT + "a", ROOT + "b", ROOT + "shared"}
    assert len(requests) == len(report.results) == report.summary.attempted == 4
    assert report.summary.failed == 0 and not report.summary.truncated
    for result in report.results:
        assert result.html_file is not None
        assert (tmp_path / result.html_file).read_bytes() == GRAPH[
            httpx.URL(result.fetch_url).path
        ]
        assert result.links
    assert not (tmp_path / "report.json").exists()  # Export is the caller's job.


def test_crawl_scope_redirects_and_failures(tmp_path: Path) -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        match request.url.path:
            case "/":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    content=b"""
                    <a href="/redirect">Redirect</a><a href="/bad">Fail</a><a href="/good">Good</a>
                    <a href="https://sub.example.test/">Sub</a><a href="https://example.test.other.test/">Other</a>
                    <a href="mailto:hi@example.test">Mail</a>""",
                )
            case "/redirect":
                return httpx.Response(
                    302, headers={"location": "https://sub.example.test/"}
                )
            case "/bad":
                raise httpx.ConnectError("offline", request=request)
            case "/good":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    content=b'<a href="/bad#again">Again</a>',
                )
            case _:
                pytest.fail(f"Unexpected request: {request.url}")

    report = asyncio.run(
        CrawlerCLI(transport=httpx.MockTransport(respond)).crawl(
            CrawlConfig((ROOT,)), HtmlWriter(tmp_path)
        )
    )
    assert set(requests) == {ROOT, ROOT + "redirect", ROOT + "bad", ROOT + "good"}
    assert len(requests) == len(report.results) == 4
    assert report.summary.failed == 1 and not report.summary.truncated
    assert (
        report.results[0].links
        and "https://sub.example.test/" in report.results[0].links
    )
    assert any(
        result.redirect_target == "https://sub.example.test/"
        for result in report.results
    )
    assert len(list(tmp_path.glob("*.html"))) == 2


@pytest.mark.parametrize(
    ("max_pages", "truncated"), [(1, True), (2, True), (4, False), (5, False)]
)
def test_crawl_page_budget(tmp_path: Path, max_pages: int, truncated: bool) -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=GRAPH[request.url.path]
        )

    report = asyncio.run(
        CrawlerCLI(transport=httpx.MockTransport(respond)).crawl(
            CrawlConfig((ROOT,), max_pages=max_pages), HtmlWriter(tmp_path)
        )
    )
    assert (
        report.summary.attempted
        == len(requests)
        == len(set(requests))
        == min(max_pages, 4)
    )
    assert report.summary.truncated is truncated
    assert report.results[0].links == (ROOT + "a", ROOT + "b")


@pytest.mark.parametrize(
    ("max_depth", "count"),
    [(0, 1), (1, 3), (2, 4), (None, 4)],
)
def test_crawl_depth_boundaries(
    tmp_path: Path, max_depth: int | None, count: int
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=GRAPH[request.url.path]
        )

    report = asyncio.run(
        CrawlerCLI(transport=httpx.MockTransport(respond)).crawl(
            CrawlConfig((ROOT,), max_depth=max_depth), HtmlWriter(tmp_path)
        )
    )
    assert report.summary.attempted == count
    assert not report.summary.truncated
    expected_depths = {ROOT: 0, ROOT + "a": 1, ROOT + "b": 1, ROOT + "shared": 2}
    for result in report.results:
        assert result.depth == expected_depths[result.fetch_url]
        assert result.links and result.html_file


@pytest.mark.parametrize("max_depth", [0, None])
def test_redirects_share_depth_admission_and_budget(
    tmp_path: Path, max_depth: int | None
) -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302, headers={"location": "/next" if request.url.path == "/" else "/#back"}
        )

    report = asyncio.run(
        CrawlerCLI(transport=httpx.MockTransport(respond)).crawl(
            CrawlConfig((ROOT,), max_pages=2, max_depth=max_depth),
            HtmlWriter(tmp_path),
        )
    )
    assert requests == ([ROOT] if max_depth == 0 else [ROOT, ROOT + "next"])
    assert not report.summary.truncated
    assert all(
        result.redirect_target and result.links is None and result.html_file is None
        for result in report.results
    )


def test_crawl_overlaps_requests_and_waits_for_active_discovery(tmp_path: Path) -> None:
    async def scenario() -> None:
        started, release = asyncio.Event(), asyncio.Event()
        active = peak = 0
        requests: list[str] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            requests.append(request.url.path)
            if request.url.path == "/":
                html = b'<a href="/a">A</a><a href="/b">B</a><a href="/c">C</a>'
            else:
                active += 1
                peak = max(peak, active)
                if active == 3:
                    started.set()
                try:
                    await release.wait()
                finally:
                    active -= 1
                html = (
                    b'<a href="/shared">Shared</a>'
                    if request.url.path != "/shared"
                    else b""
                )
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=html
            )

        async with asyncio.timeout(2), asyncio.TaskGroup() as group:
            task = group.create_task(
                CrawlerCLI(transport=httpx.MockTransport(respond)).crawl(
                    CrawlConfig((ROOT,), concurrency=3), HtmlWriter(tmp_path)
                )
            )
            await started.wait()
            assert (
                len(requests) == 4 and not task.done()
            )  # Pending is empty, requests can discover more.
            assert (
                len(
                    [
                        task
                        for task in asyncio.all_tasks()
                        if task.get_name().startswith("crawl-")
                    ]
                )
                == 3
            )
            release.set()
        assert task.result().summary.attempted == 5
        assert peak == 3 and active == 0 and requests.count("/shared") == 1

    asyncio.run(scenario())


def test_crawl_first_discovered_depth_is_not_reassigned(tmp_path: Path) -> None:
    async def scenario() -> None:
        shared_saved = asyncio.Event()
        bodies = {
            "/": b'<a href="/fast">Fast</a><a href="/slow">Slow</a>',
            "/fast": b'<a href="/middle">Middle</a>',
            "/middle": b'<a href="/shared">Shared</a>',
            "/slow": b'<a href="/shared">Shared</a>',
            "/shared": b'<a href="/child">Child</a>',
        }

        class ObservedHtmlWriter(HtmlWriter):
            def write(self, fetch_url: str, html: bytes) -> str:
                name = super().write(fetch_url, html)
                if fetch_url == ROOT + "shared":
                    shared_saved.set()
                return name

        async def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/slow":
                await shared_saved.wait()
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=bodies[request.url.path],
            )

        async with asyncio.timeout(2):
            report = await CrawlerCLI(transport=httpx.MockTransport(respond)).crawl(
                CrawlConfig((ROOT,), concurrency=2, max_depth=3),
                ObservedHtmlWriter(tmp_path),
            )
        assert len(report.results) == 5
        shared = [
            result for result in report.results if result.fetch_url == ROOT + "shared"
        ]
        assert len(shared) == 1 and shared[0].depth == 3
        assert all(result.fetch_url != ROOT + "child" for result in report.results)

    asyncio.run(scenario())


@pytest.mark.parametrize("follow_external", [False, True])
def test_multiple_seeds_share_scope_deduplication_and_external_redirect_policy(
    tmp_path: Path, follow_external: bool
) -> None:
    other = "https://python.test/"
    external = "https://external.test/"
    subdomain = "https://sub.example.test/"
    requested: list[str] = []
    bodies = {
        ROOT: f'<a href="{other}">Other seed</a><a href="/go">Redirect</a><a href="/shared">Shared</a>'.encode(),
        other: f'<a href="{ROOT}shared#again">Shared</a><a href="{external}direct">External</a><a href="{subdomain}">Subdomain</a><a href="mailto:test@example.test">Mail</a>'.encode(),
        ROOT + "shared": b"",
        external + "direct": b'<a href="/next">Next</a>',
        external + "next": b"",
        subdomain: b"",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url == ROOT + "go":
            return httpx.Response(302, headers={"location": external + "landing"})
        if url == external + "landing":
            return httpx.Response(
                302, headers={"location": external + "direct#duplicate"}
            )
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=bodies[url]
        )

    report = asyncio.run(
        CrawlerCLI(transport=httpx.MockTransport(respond)).crawl(
            CrawlConfig(
                (ROOT, other, ROOT + "#duplicate"), follow_external=follow_external
            ),
            HtmlWriter(tmp_path),
        )
    )
    expected = {ROOT, other, ROOT + "go", ROOT + "shared"}
    if follow_external:
        expected |= {
            external + "direct",
            external + "next",
            external + "landing",
            subdomain,
        }
    assert set(requested) == expected and len(requested) == len(expected)
    assert report.summary.attempted == len(expected) and report.summary.failed == 0
    results = {result.fetch_url: result for result in report.results}
    assert results[ROOT].depth == results[other].depth == 0
    assert results[ROOT + "go"].redirect_target == external + "landing"
    other_links = results[other].links
    assert other_links is not None and external + "direct" in other_links
    assert len(list(tmp_path.glob("*.html"))) == len(expected) - (
        2 if follow_external else 1
    )


@pytest.mark.parametrize(
    ("max_pages", "count", "truncated"), [(1, 1, True), (2, 2, False), (3, 2, False)]
)
def test_multiple_seeds_start_at_zero_and_share_one_page_budget(
    tmp_path: Path, max_pages: int, count: int, truncated: bool
) -> None:
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b'<a href="/child">Child</a>',
        )

    report = asyncio.run(
        CrawlerCLI(transport=httpx.MockTransport(respond)).crawl(
            CrawlConfig(
                (ROOT, ROOT + "#duplicate", "https://python.test/"),
                max_pages=max_pages,
                max_depth=0,
            ),
            HtmlWriter(tmp_path),
        )
    )
    assert requested == [ROOT, "https://python.test/"][:count]
    assert report.summary.attempted == count and report.summary.truncated is truncated
    assert all(result.depth == 0 and result.links for result in report.results)
