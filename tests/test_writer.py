import hashlib
import io
import json
from pathlib import Path

import pytest

from zegocover_test.models import CrawlAttempt, CrawlReport, CrawlResult, CrawlSummary
from zegocover_test.writer import HtmlWriter, JsonWriter, print_result


@pytest.mark.parametrize("html", [b"", b"<html>\xe9\x00</html>"])
def test_html_writer_preserves_bytes_and_uses_safe_stable_ids(
    tmp_path: Path, html: bytes
) -> None:
    directory = tmp_path / "nested" / "pages"
    html_writer = HtmlWriter(directory)
    url = "https://example.test/../../outside?name=../../other"
    name = html_writer.write(url, html)
    assert name == hashlib.sha256(url.encode()).hexdigest() + ".html"
    assert (directory / name).read_bytes() == html
    assert list(directory.iterdir()) == [directory / name]


@pytest.mark.parametrize("replacement", [b"new", b""])
def test_html_writer_overwrites_page_and_preserves_unrelated_files(
    tmp_path: Path, replacement: bytes
) -> None:
    marker = tmp_path / "keep.txt"
    marker.write_text("keep")
    html_writer = HtmlWriter(tmp_path)
    name = html_writer.write("https://example.test/?x=1", b"first")
    other = html_writer.write("https://example.test/?x=2", b"second")
    assert name != other
    assert HtmlWriter(tmp_path).write("https://example.test/?x=1", replacement) == name
    assert (tmp_path / name).read_bytes() == replacement
    assert (tmp_path / other).read_bytes() == b"second"
    assert marker.read_text() == "keep"


def test_json_writer_persists_metadata_links_failures_and_skips(tmp_path: Path) -> None:
    report = CrawlReport(
        results=(
            CrawlResult(
                "https://example.test/",
                0,
                status_code=200,
                retry_count=2,
                attempts=(
                    CrawlAttempt(1, 408, "HTTP 408"),
                    CrawlAttempt(2, None, "ReadTimeout: timed out"),
                    CrawlAttempt(3, 200, None),
                ),
                content_type="text/html; charset=iso-8859-1",
                html_file="abc.html",
                links=("https://example.test/next#part", "mailto:café@example.test"),
            ),
            CrawlResult("https://example.test/bad", 1, error="offline", retry_count=3),
            CrawlResult(
                "https://example.test/image",
                1,
                status_code=200,
                content_type="image/png",
                skip_reason="Non-HTML content type: image/png",
            ),
            CrawlResult(
                "https://example.test/empty",
                1,
                status_code=200,
                content_type="text/html",
                html_file="empty.html",
                links=(),
            ),
            CrawlResult(
                "https://example.test/go",
                1,
                status_code=302,
                redirect_target="https://example.test/next",
            ),
        ),
        summary=CrawlSummary(5, 1, True),
    )
    path = JsonWriter(tmp_path).write(report)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path == tmp_path / "report.json"
    assert data["schema_version"] == 1
    assert data["summary"] == {
        "attempted": 5,
        "failed": 1,
        "truncated": True,
    }
    records = data["results"]
    assert len(records) == 5
    assert records[0] == {
        "fetch_url": "https://example.test/",
        "depth": 0,
        "retry_count": 2,
        "success": True,
        "attempts": [
            {"attempt": 1, "status_code": 408, "error": "HTTP 408"},
            {"attempt": 2, "status_code": None, "error": "ReadTimeout: timed out"},
            {"attempt": 3, "status_code": 200, "error": None},
        ],
        "status_code": 200,
        "content_type": "text/html; charset=iso-8859-1",
        "html_file": "abc.html",
        "links": ["https://example.test/next#part", "mailto:café@example.test"],
        "redirect_target": None,
        "error": None,
        "skip_reason": None,
    }
    assert records[1]["error"] == "offline" and records[1]["html_file"] is None
    assert records[1]["retry_count"] == 3
    assert records[1]["success"] is False
    assert records[2]["retry_count"] == 0
    assert records[2]["links"] is None and records[2]["skip_reason"]
    assert records[3]["links"] == []
    assert records[4]["redirect_target"] == "https://example.test/next"
    assert all(record["success"] for record in records[2:])


def test_json_writer_replaces_existing_report(
    tmp_path: Path,
) -> None:
    writer = JsonWriter(tmp_path)
    assert not writer.path.exists()
    report = CrawlReport((), CrawlSummary(0, 0, False))
    writer.write(report)
    assert json.loads(writer.path.read_text())["results"] == []
    replacement = CrawlReport((), CrawlSummary(1, 1, False))
    JsonWriter(tmp_path).write(replacement)
    assert json.loads(writer.path.read_text())["summary"]["failed"] == 1
    writer.write(report)
    assert json.loads(writer.path.read_text())["summary"]["failed"] == 0


def test_console_escapes_untrusted_text_and_borrows_stream() -> None:
    stream = io.StringIO()
    print_result(
        CrawlResult(
            "https://example.test/",
            0,
            error="bad\x1b[31m\nmessage",
            retry_count=1,
            links=("mailto:test\x00",),
        ),
        stream,
    )
    output = stream.getvalue()
    assert "\x1b" not in output and "\x00" not in output
    assert r"bad\x1b[31m\nmessage" in output
    assert "Retries: 1" in output
    assert "Success: false" in output
    assert not stream.closed
