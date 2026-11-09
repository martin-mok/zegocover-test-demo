import asyncio
import io
import logging
from pathlib import Path

import httpx
import pytest

from zegocover_test.cli import CrawlerCLI
from zegocover_test.config import CrawlConfig
from zegocover_test.engine import CrawlEngine
from zegocover_test.fetcher import HttpFetcher
from zegocover_test.logging_context import current_url, current_worker, log_to_stream
from zegocover_test.parser import HtmlParser
from zegocover_test.scheduler import Scheduler
from zegocover_test.urls import HttpURLPolicy
from zegocover_test.writer import HtmlWriter

ROOT = "https://example.test/"


@pytest.mark.parametrize(
    ("extra", "expected_context"),
    [
        ({}, "[worker=- url=-]"),
        ({"worker": "custom-worker"}, "[worker=custom-worker url=-]"),
        ({"fetch_url": ROOT}, f"[worker=- url={ROOT}]"),
        (
            {"worker": "custom-worker", "fetch_url": ROOT},
            f"[worker=custom-worker url={ROOT}]",
        ),
    ],
)
def test_plain_logger_formats_missing_context_fields(
    extra: dict[str, str], expected_context: str
) -> None:
    stream = io.StringIO()
    logger = logging.getLogger("zegocover_test.plain")
    with log_to_stream("INFO", stream):
        logger.info("Message from a plain logger", extra=extra)
    assert f"{expected_context} Message from a plain logger" in stream.getvalue()


def test_interleaved_workers_keep_url_context_and_caught_exception_tracebacks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="zegocover_test")

    async def scenario() -> None:
        first_started, second_started = asyncio.Event(), asyncio.Event()

        async def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/first":
                first_started.set()
                await second_started.wait()
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    content=b'<a href="/first">Self</a><a href="http://[bad">Invalid</a>',
                )
            await first_started.wait()
            second_started.set()
            await asyncio.sleep(0)
            raise httpx.ConnectError("connection unavailable", request=request)

        urls = HttpURLPolicy()
        config = CrawlConfig((ROOT + "first", ROOT + "second"), concurrency=2)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            engine = CrawlEngine(
                config,
                scheduler=Scheduler(config, urls),
                fetcher=HttpFetcher(client, urls),
                parser=HtmlParser(urls),
                html_writer=HtmlWriter(tmp_path),
            )
            async with asyncio.timeout(2):
                report = await engine.run()
        assert report.summary.attempted == 2 and report.summary.failed == 1
        assert current_worker.get() == "-" and current_url.get() == "-"

    asyncio.run(scenario())
    processing = {
        record.__dict__["fetch_url"]: record.__dict__["worker"]
        for record in caplog.records
        if record.getMessage().startswith("Processing task:")
    }
    assert set(processing) == {ROOT + "first", ROOT + "second"}
    assert len(set(processing.values())) == 2
    fetch_records = [
        record for record in caplog.records if record.name == "zegocover_test.fetcher"
    ]
    assert fetch_records
    assert all(
        record.__dict__["worker"] == processing[record.__dict__["fetch_url"]]
        for record in fetch_records
    )
    request_failure = next(
        record
        for record in fetch_records
        if record.getMessage().startswith("Request failed:")
    )
    assert request_failure.__dict__["fetch_url"] == ROOT + "second"
    assert request_failure.exc_info is not None
    assert request_failure.exc_info[0] is httpx.ConnectError
    url_failure = next(
        record
        for record in caplog.records
        if "Could not resolve hyperlink" in record.getMessage()
    )
    assert url_failure.__dict__["fetch_url"] == ROOT + "first"
    assert url_failure.__dict__["worker"] == processing[ROOT + "first"]
    assert url_failure.exc_info is not None
    assert "http://[bad" in url_failure.getMessage()
    failures = [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert len(failures) == 1
    assert failures[0].__dict__["fetch_url"] == ROOT + "second"
    assert "connection unavailable" in failures[0].getMessage()
    stopped = [
        record for record in caplog.records if record.getMessage() == "Worker stopped"
    ]
    assert len(stopped) == 2
    assert all(record.__dict__["fetch_url"] == "-" for record in stopped)


def test_html_writer_error_logs_active_worker_url_and_traceback_and_completes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingHtmlWriter(HtmlWriter):
        def write(self, fetch_url: str, html: bytes) -> str:
            raise OSError("disk unavailable")

    monkeypatch.setattr("zegocover_test.cli.HtmlWriter", FailingHtmlWriter)
    stderr = io.StringIO()
    cli = CrawlerCLI(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "text/html"})
        ),
        stdout=io.StringIO(),
        stderr=stderr,
    )
    assert cli.run([ROOT, "--output-dir", str(tmp_path)]) == 1
    failure = next(
        record
        for record in caplog.records
        if record.getMessage() == "Page processing failed"
    )
    assert failure.levelno == logging.ERROR
    assert failure.__dict__["fetch_url"] == ROOT
    assert failure.__dict__["worker"] == "crawl-fetch-0"
    assert failure.exc_info is not None and failure.exc_info[0] is OSError
    assert f"worker=crawl-fetch-0 url={ROOT}" in stderr.getvalue()
    assert "Traceback" in stderr.getvalue() and "disk unavailable" in stderr.getvalue()
    assert (tmp_path / "report.json").exists()
    assert stderr.getvalue().endswith("Attempted: 1; failed: 1\n")


@pytest.mark.parametrize("level", ["info", "DEBUG", "WARNING"])
def test_cli_log_levels_and_logging_configuration_cleanup(
    tmp_path: Path, level: str
) -> None:
    package_logger = logging.getLogger("zegocover_test")
    root_logger = logging.getLogger()
    original_level, original_handlers = package_logger.level, package_logger.handlers[:]
    root_level, root_handlers = root_logger.level, root_logger.handlers[:]
    stdout, stderr = io.StringIO(), io.StringIO()
    cli = CrawlerCLI(
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        stdout=stdout,
        stderr=stderr,
    )
    arguments = [ROOT, "--output-dir", str(tmp_path), "--log-level", level]
    assert cli.run(arguments) == 0
    first_output = stderr.getvalue()
    if level == "WARNING":
        assert first_output == "Attempted: 1; failed: 0\n"
    else:
        assert first_output.count("Processing task:") == 1
        assert f"[worker=crawl-fetch-0 url={ROOT}]" in first_output
        assert ("Worker waiting for task" in first_output) == (level == "DEBUG")
    assert "Page: " in stdout.getvalue() and "Processing task:" not in stdout.getvalue()
    assert (
        package_logger.level == original_level
        and package_logger.handlers == original_handlers
    )
    assert root_logger.level == root_level and root_logger.handlers == root_handlers
    assert not stderr.closed
    stderr.seek(0)
    stderr.truncate()
    assert cli.run(arguments) == 0
    assert stderr.getvalue().count("Processing task:") == (
        0 if level == "WARNING" else 1
    )


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
@pytest.mark.parametrize("failure", [False, True])
def test_cli_logs_escape_response_and_exception_controls(
    tmp_path: Path, level: str, failure: bool
) -> None:
    text = "untrusted\x1b[2J\x07\rtext"

    def respond(request: httpx.Request) -> httpx.Response:
        if failure:
            raise ValueError(text)
        return httpx.Response(200, headers={"content-type": "application/" + text})

    stdout, stderr = io.StringIO(), io.StringIO()
    cli = CrawlerCLI(
        transport=httpx.MockTransport(respond), stdout=stdout, stderr=stderr
    )
    assert cli.run([ROOT, "--output-dir", str(tmp_path), "--log-level", level]) == int(
        failure
    )
    for output in (stdout.getvalue(), stderr.getvalue()):
        assert all(control not in output for control in ("\x1b", "\x07", "\r"))
        assert r"untrusted\x1b[2J\x07\rtext" in output
    if failure:
        assert "Traceback (most recent call last):\n" in stderr.getvalue()
        assert "\nValueError: untrusted" in stderr.getvalue()
