"""Synchronous HTML saving, final JSON export and simple terminal output."""

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Protocol, TextIO

from zegocover_test.models import CrawlReport, CrawlResult


class PageWriter(Protocol):
    def write(self, fetch_url: str, html: bytes) -> str:
        """Save HTML synchronously and return a path relative to the output directory."""
        ...


class HtmlWriter:
    """Save the latest HTML under the URL's stable SHA-256 file ID."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self._directory = directory

    def write(self, fetch_url: str, html: bytes) -> str:
        name = hashlib.sha256(fetch_url.encode("utf-8")).hexdigest() + ".html"
        with (self._directory / name).open("wb") as file:
            file.write(html)
        return name


def terminal_text(value: str) -> str:
    """Escape controls in untrusted text without obscuring ordinary URLs."""
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in value)


def print_result(result: CrawlResult, stream: TextIO) -> None:
    """Print one page's useful metadata and discovered links."""
    lines = [f"Page: {terminal_text(result.fetch_url)}", f"  Depth: {result.depth}"]
    lines.append(f"  Success: {str(result.success).lower()}")
    lines.append(f"  Retries: {result.retry_count}")
    lines.append(
        f"  Status: {result.status_code if result.status_code is not None else 'unavailable'}"
    )
    if result.html_file is not None:
        lines.append(f"  HTML: {terminal_text(result.html_file)}")
    if result.error:
        lines.append(f"  Error: {terminal_text(result.error)}")
    if result.redirect_target:
        lines.append(f"  Redirect: {terminal_text(result.redirect_target)}")
    if result.skip_reason:
        lines.append(f"  Skipped: {terminal_text(result.skip_reason)}")
    if result.links is not None:
        lines.append(f"  Links ({len(result.links)}):")
        lines.extend(f"    {terminal_text(link)}" for link in result.links)
    else:
        lines.append("  HTML not parsed.")
    try:
        print("\n".join(lines) + "\n", file=stream, flush=True)
    except BrokenPipeError:
        # Prevent Python's shutdown flush from replacing exit 1 with exit 120.
        sink_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(sink_fd, stream.fileno())
        finally:
            os.close(sink_fd)
        raise


class JsonWriter:
    """Write the completed report, replacing any previous JSON export."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "report.json"

    def write(self, report: CrawlReport) -> Path:
        with self.path.open("w", encoding="utf-8") as file:
            json.dump(
                {
                    "schema_version": 1,
                    "results": [
                        {**asdict(result), "success": result.success}
                        for result in report.results
                    ],
                    "summary": asdict(report.summary),
                },
                file,
                ensure_ascii=False,
                indent=2,
            )
            file.write("\n")
        return self.path
