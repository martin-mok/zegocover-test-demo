import gzip
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from zegocover_test.cli import CrawlerCLI, main, parse_args
from zegocover_test.config import CrawlConfig
from zegocover_test.models import CrawlReport
from zegocover_test.urls import HttpURLPolicy
from zegocover_test.writer import HtmlWriter, JsonWriter, PageWriter

ROOT = "https://example.test/"


@pytest.fixture(autouse=True)
def isolated_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def test_cli_shares_injected_url_policy_and_borrows_output_streams() -> None:
    requested: list[str] = []
    resolutions: list[str] = []
    stdout, stderr = io.StringIO(), io.StringIO()

    class Policy(HttpURLPolicy):
        def resolve_href(self, base_url: str, raw_href: str) -> str | None:
            resolutions.append(raw_href)
            return super().resolve_href(base_url, raw_href)

        def has_allowed_host(
            self, fetch_url: str, allowed_hosts: frozenset[bytes]
        ) -> bool:
            return super().has_allowed_host(
                fetch_url, allowed_hosts
            ) and not fetch_url.endswith("blocked")

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/go":
            return httpx.Response(302, headers={"location": "/target"})
        html = (
            b'<a href="/go">Go</a><a href="/blocked">Blocked</a>'
            if request.url.path == "/"
            else b""
        )
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html)

    cli = CrawlerCLI(
        urls=Policy(),
        transport=httpx.MockTransport(respond),
        stdout=stdout,
        stderr=stderr,
    )
    assert cli.run([ROOT]) == 0
    assert requested == [ROOT, ROOT + "go", ROOT + "target"]
    assert resolutions == ["/go", "/blocked", "/target"]
    assert ROOT + "blocked" in stdout.getvalue()
    assert stderr.getvalue().endswith("Attempted: 3; failed: 0\n")
    assert not stdout.closed and not stderr.closed


def test_cli_summary_follows_http_and_json_cleanup(tmp_path: Path) -> None:
    path = tmp_path / "report.json"

    class ObservedTransport(httpx.MockTransport):
        closed = False

        async def aclose(self) -> None:
            self.closed = True
            await super().aclose()

    transport = ObservedTransport(lambda request: httpx.Response(200))

    class SummaryStream(io.StringIO):
        def write(self, text: str) -> int:
            if text.startswith("Attempted:"):
                assert transport.closed
                assert len(json.loads(path.read_text())["results"]) == 1
            return super().write(text)

    stderr = SummaryStream()
    assert (
        CrawlerCLI(transport=transport, stderr=stderr).run(
            [ROOT, "--output-dir", str(tmp_path)]
        )
        == 0
    )
    assert stderr.getvalue().endswith("Attempted: 1; failed: 0\n")


def test_cli_help_and_defaults(capsys: pytest.CaptureFixture[str]) -> None:
    arguments = parse_args([ROOT])
    assert arguments.seed_urls == [ROOT]
    assert not arguments.follow_external
    assert arguments.max_pages == 20
    assert arguments.concurrency == 8
    assert arguments.max_depth is None
    assert arguments.max_retries == 0
    assert arguments.output_dir == Path("crawl-output")
    assert arguments.log_level == "INFO"
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0
    assert "--max-pages" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["example.test"],
        [ROOT, "not-a-url"],
        [ROOT, "--follow-external", "not-a-url"],
        [ROOT, "--concurrency", "0"],
        [ROOT, "--max-pages", "-1"],
        [ROOT, "--max-pages", "abc"],
        [ROOT, "--max-depth", "-1"],
        [ROOT, "--max-depth", "abc"],
        [ROOT, "--max-retries", "-1"],
        [ROOT, "--max-retries", "1.5"],
        [ROOT, "--retry-delay", "0"],
        [ROOT, "--log-level", "invalid"],
        [ROOT, "--writers", "0"],
        [ROOT, "--writers", "2"],
        [ROOT, "--writer-queue-size", "0"],
        [ROOT, "--output", "out.json", "--output-dir", "out"],
        [ROOT, "--output", "out.json", "--writers", "2"],
        ["https://user:pw@example.test/"],
    ],
)
def test_cli_invalid_input_never_requests(arguments: list[str]) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid CLI input made an HTTP request")

    cli = CrawlerCLI(transport=httpx.MockTransport(respond))
    with pytest.raises(SystemExit) as error:
        cli.run(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize(
    "statuses", [(408, 503, 200), (408, 408, 408, 408), (408, 404), (200,)]
)
def test_cli_retry_options_export_all_attempts_and_final_success(
    tmp_path: Path, statuses: tuple[int, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(statuses[calls - 1])

    cli = CrawlerCLI(transport=httpx.MockTransport(respond))
    assert cli.run(
        [
            ROOT,
            "--max-retries",
            "3",
            "--max-pages",
            "1",
            "--output-dir",
            str(tmp_path),
        ]
    ) == int(statuses[-1] != 200)
    assert calls == len(statuses)
    report = json.loads((tmp_path / "report.json").read_text())
    success = statuses[-1] == 200
    assert report["summary"] == {
        "attempted": 1,
        "failed": int(not success),
        "truncated": False,
    }
    assert len(report["results"]) == 1
    result = report["results"][0]
    assert result["success"] is success
    assert result["retry_count"] == len(statuses) - 1
    assert result["attempts"] == [
        {
            "attempt": index,
            "status_code": status,
            "error": None if status == 200 else f"HTTP {status}",
        }
        for index, status in enumerate(statuses, start=1)
    ]
    assert result["status_code"] == result["attempts"][-1]["status_code"]
    assert result["error"] == result["attempts"][-1]["error"]
    assert f"Success: {str(success).lower()}" in capsys.readouterr().out


def test_cli_successful_crawl(capsys: pytest.CaptureFixture[str]) -> None:
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        html = (
            b'<a href="/next#one">Next</a><a href="https://other.test/">External</a>'
            if request.url.path == "/"
            else b""
        )
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html)

    cli = CrawlerCLI(transport=httpx.MockTransport(respond))
    assert cli.run([ROOT]) == 0
    output = capsys.readouterr()
    assert requested == [ROOT, ROOT + "next"]
    assert output.out.count("Page: ") == 2
    assert "https://other.test/" in output.out and ROOT + "next#one" in output.out
    assert "Links (0):" in output.out
    assert output.err.endswith("Attempted: 2; failed: 0\n")


@pytest.mark.parametrize(
    ("status", "max_pages", "diagnostic"),
    [(200, 1, "page limit reached"), (404, 20, "failed: 1")],
)
def test_cli_cap_and_error_reporting(
    status: int,
    max_pages: int,
    diagnostic: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            status if request.url.path == "/" else 200,
            headers={"content-type": "text/html"},
            content=b'<a href="/next">Next</a>',
        )

    cli = CrawlerCLI(transport=httpx.MockTransport(respond))
    assert cli.run([ROOT, "--max-pages", str(max_pages)]) == 1
    output = capsys.readouterr()
    assert diagnostic in output.err
    assert requested == [ROOT]
    if status == 404:
        assert "Error: HTTP 404" in output.out
        assert "HTML not parsed." in output.out
        assert ROOT + "next" not in output.out
    else:
        assert ROOT + "next" in output.out


@pytest.mark.parametrize("status", [200, 302])
def test_cli_skip_and_redirect_are_explicit(
    status: int, capsys: pytest.CaptureFixture[str]
) -> None:
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            status,
            headers={
                "location": "https://external.test/",
                "content-type": "text/plain",
            },
        )

    cli = CrawlerCLI(transport=httpx.MockTransport(respond))
    assert cli.run([ROOT]) == 0
    output = capsys.readouterr()
    assert requested == [ROOT]
    if status == 302:
        assert "Redirect: https://external.test/" in output.out
        assert "Skipped:" not in output.out
    else:
        assert "Redirect:" not in output.out
        assert "Skipped: Non-HTML" in output.out
    assert "HTML not parsed." in output.out
    assert "Links (0)" not in output.out


class FailingOutput(io.StringIO):
    def write(self, text: str) -> int:
        raise OSError("output unavailable")


def test_cli_keyboard_interrupt_returns_130(capsys: pytest.CaptureFixture[str]) -> None:
    class InterruptedCLI(CrawlerCLI):
        async def crawl(
            self, config: CrawlConfig, html_writer: PageWriter
        ) -> CrawlReport:
            raise KeyboardInterrupt

    assert InterruptedCLI().run([ROOT]) == 130
    assert capsys.readouterr().err.endswith("Interrupted.\n")


def test_cli_output_failure_returns_one(capsys: pytest.CaptureFixture[str]) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"")

    cli = CrawlerCLI(transport=httpx.MockTransport(respond), stdout=FailingOutput())
    exit_code = cli.run([ROOT])
    assert exit_code == 1
    assert "Output failed: output unavailable" in capsys.readouterr().err


def test_cli_broken_pipe_preserves_exit_code() -> None:
    # A real subprocess is necessary: Python may fail again while flushing at
    # shutdown, after main() has already returned the intended exit code.
    script = """
from zegocover_test.cli import CrawlerCLI
from zegocover_test.models import CrawlReport, CrawlResult, CrawlSummary

class StubCLI(CrawlerCLI):
    async def crawl(self, config, html_writer):
        return CrawlReport((CrawlResult(config.seed_urls[0], 0, status_code=200, links=()),), CrawlSummary(1, 0, False))

raise SystemExit(StubCLI().run(["https://example.test/"]))
"""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            stdout=write_fd,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    finally:
        os.close(write_fd)
    assert result.returncode == 1
    assert "Output failed:" in result.stderr
    assert "Exception ignored" not in result.stderr


def test_cli_depth_limit_completes_normally_and_reports_boundary_links(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    requested: list[str] = []
    output_path = tmp_path / "report.json"

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b'<a href="/child#part">Child</a>',
        )

    cli = CrawlerCLI(transport=httpx.MockTransport(respond))
    assert cli.run([ROOT, "--max-depth", "0", "--output-dir", str(tmp_path)]) == 0
    output = capsys.readouterr()
    assert requested == [ROOT]
    assert "Page: " in output.out
    assert output.err.endswith("Attempted: 1; failed: 0\n")
    report = json.loads(output_path.read_text())
    assert report["summary"] == {"attempted": 1, "failed": 0, "truncated": False}
    records = report["results"]
    assert len(records) == 1
    assert records[0]["depth"] == 0
    assert records[0]["fetch_url"] == ROOT
    assert records[0]["links"] == [ROOT + "child#part"]


def test_cli_recrawl_replaces_html_and_report_under_the_same_file_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "report.json"
    output_path.write_text("keep this")

    bodies = (
        b'<a href="https://external.test/old">Old content</a>',
        b"<p>New</p>",
    )
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = bodies[len(requested)]
        requested.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "text/html"}, content=body)

    filenames: list[str] = []
    for body in bodies:
        cli = CrawlerCLI(transport=httpx.MockTransport(respond))
        assert cli.run([ROOT, "--output-dir", str(tmp_path)]) == 0
        record = json.loads(output_path.read_text())["results"][0]
        assert record["fetch_url"] == ROOT
        filenames.append(record["html_file"])
        assert (tmp_path / record["html_file"]).read_bytes() == body
    assert requested == [ROOT, ROOT]
    assert filenames[0] == filenames[1]
    assert record["links"] == []
    assert len(list(tmp_path.glob("*.html"))) == 1
    assert "Output failed:" not in capsys.readouterr().err


def test_cli_json_write_failure_happens_after_crawling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requested: list[str] = []

    class FailingWriter(JsonWriter):
        def write(self, report: CrawlReport) -> Path:
            assert len(requested) == len(report.results) == 3
            assert len(list(tmp_path.glob("*.html"))) == 3
            raise OSError("disk unavailable")

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b'<a href="/child">Child</a><a href="/other">Other</a>',
        )

    cli = CrawlerCLI(transport=httpx.MockTransport(respond))
    monkeypatch.setattr("zegocover_test.cli.JsonWriter", FailingWriter)
    assert cli.run([ROOT, "--output-dir", str(tmp_path)]) == 1
    assert "disk unavailable" in capsys.readouterr().err
    assert not (tmp_path / "report.json").exists()


def test_cli_html_write_failure_is_reported_without_parsing_failed_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requested: list[str] = []

    class FailingHtmlWriter(HtmlWriter):
        def write(self, fetch_url: str, html: bytes) -> str:
            if fetch_url == ROOT:
                raise OSError("HTML disk unavailable")
            return super().write(fetch_url, html)

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b'<a href="/child">Child</a>',
        )

    monkeypatch.setattr("zegocover_test.cli.HtmlWriter", FailingHtmlWriter)
    cli = CrawlerCLI(transport=httpx.MockTransport(respond))
    assert (
        cli.run(
            [ROOT, ROOT + "other", "--max-retries", "2", "--output-dir", str(tmp_path)]
        )
        == 1
    )
    assert sorted(requested) == sorted([ROOT, ROOT + "other", ROOT + "child"])
    output = capsys.readouterr()
    assert output.err.endswith("Attempted: 3; failed: 1\n")
    report = json.loads((tmp_path / "report.json").read_text())
    failed = next(result for result in report["results"] if result["error"])
    assert failed["fetch_url"] == ROOT
    assert failed["error"] == "OSError: HTML disk unavailable"
    assert failed["html_file"] is None and failed["links"] is None
    assert failed["retry_count"] == 0
    assert failed["success"] is False
    assert failed["attempts"] == [
        {"attempt": 1, "status_code": 200, "error": "OSError: HTML disk unavailable"}
    ]


def test_cli_existing_output_directory_is_preserved_and_receives_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "keep.txt"
    marker.write_text("keep this")
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"")

    cli = CrawlerCLI(transport=httpx.MockTransport(respond))
    assert cli.run([ROOT, "--output-dir", str(tmp_path)]) == 0
    assert requested == [ROOT]
    assert marker.read_text() == "keep this"
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["results"][0]["fetch_url"] == ROOT
    output = capsys.readouterr()
    assert "Page: " in output.out
    assert output.err.endswith("Attempted: 1; failed: 0\n")


@pytest.mark.parametrize("follow_external", [False, True])
def test_cli_multiple_seeds_and_external_option(
    tmp_path: Path, follow_external: bool
) -> None:
    other = "https://python.test/"
    external = "https://external.test/"
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=f'<a href="{external}">External</a>'.encode(),
        )

    arguments = [ROOT, "--max-pages", "3", other, "--output-dir", str(tmp_path)]
    if follow_external:
        arguments.insert(1, "--follow-external")
    cli = CrawlerCLI(transport=httpx.MockTransport(respond))
    assert cli.run(arguments) == 0
    assert set(requested) == {ROOT, other, *([external] if follow_external else [])}
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["summary"]["attempted"] == (3 if follow_external else 2)
    assert {
        result["fetch_url"] for result in report["results"] if result["depth"] == 0
    } == {ROOT, other}


@pytest.mark.parametrize("cut", [8, 25])
def test_cli_rejects_compressed_html_without_saving_or_discovering_links(
    tmp_path: Path, cut: int
) -> None:
    html = b'<a href="/first">First</a>' + b"x" * 1000 + b'<a href="/last">Last</a>'
    compressed = gzip.compress(html, mtime=0)[:-cut]
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/good":
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"OK"
            )
        return httpx.Response(
            200,
            headers={
                "content-type": "text/html",
                "content-encoding": "gzip",
                "content-length": str(len(compressed)),
            },
            stream=httpx.ByteStream(compressed),
        )

    cli = CrawlerCLI(
        transport=httpx.MockTransport(respond),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert (
        cli.run(
            [ROOT, ROOT + "good", "--output-dir", str(tmp_path), "--max-retries", "2"]
        )
        == 1
    )
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["summary"] == {"attempted": 2, "failed": 1, "truncated": False}
    assert set(requests) == {ROOT, ROOT + "good"} and len(requests) == 2
    bad = next(result for result in report["results"] if result["fetch_url"] == ROOT)
    assert bad["success"] is False and bad["retry_count"] == 0
    assert bad["error"] == "Unsupported HTML content encoding: gzip"
    assert bad["html_file"] is None and bad["links"] is None
    assert len(list(tmp_path.glob("*.html"))) == 1
