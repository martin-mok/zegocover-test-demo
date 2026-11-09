"""Command-line configuration, resource ownership and process exit codes."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

import httpx

from zegocover_test.config import (
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_RETRIES,
    DEFAULT_OUTPUT_DIR,
    HTTP_TIMEOUT,
    CrawlConfig,
)
from zegocover_test.engine import CrawlEngine
from zegocover_test.fetcher import HttpFetcher
from zegocover_test.logging_context import CrawlLogger, log_to_stream
from zegocover_test.models import CrawlReport, CrawlSummary
from zegocover_test.parser import HtmlParser
from zegocover_test.scheduler import Scheduler
from zegocover_test.urls import HttpURLPolicy, URLPolicy
from zegocover_test.writer import (
    HtmlWriter,
    JsonWriter,
    PageWriter,
    print_result,
    terminal_text,
)

logger = CrawlLogger(logging.getLogger(__name__))


class Arguments(argparse.Namespace):
    seed_urls: list[str]
    concurrency: int
    max_pages: int
    max_depth: int | None
    max_retries: int
    output_dir: Path
    follow_external: bool
    log_level: str


def positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def nonnegative_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return number


class CrawlerCLI:
    """Assemble one crawl and own HTTP/file lifetimes around its workers.

    Dependencies are ordinary constructor arguments. The engine can also be
    assembled directly by library callers that need different implementations.
    """

    def __init__(
        self,
        *,
        urls: URLPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self._urls = urls if urls is not None else HttpURLPolicy()
        self._transport = transport
        self._stdout = stdout if stdout is not None else sys.stdout
        self._stderr = stderr if stderr is not None else sys.stderr

    async def crawl(self, config: CrawlConfig, html_writer: PageWriter) -> CrawlReport:
        """Fetch and save HTML; return metadata after all workers and HTTP close."""
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=config.concurrency,
                max_keepalive_connections=config.concurrency,
            ),
            transport=self._transport,
        ) as client:
            engine = CrawlEngine(
                config,
                scheduler=Scheduler(config, self._urls),
                fetcher=HttpFetcher(client, self._urls),
                parser=HtmlParser(self._urls),
                html_writer=html_writer,
            )
            return await engine.run()

    def _seed_url(self, value: str) -> str:
        try:
            return self._urls.normalize(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    def parse_args(self, argv: Sequence[str] | None = None) -> Arguments:
        parser = argparse.ArgumentParser(
            description="Crawl static hyperlinks from one or more starting URLs.",
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            allow_abbrev=False,
        )
        parser.add_argument(
            "seed_urls",
            nargs="+",
            metavar="URL",
            type=self._seed_url,
            help="absolute HTTP(S) starting URLs; each starts at depth 0",
        )
        parser.add_argument(
            "--follow-external",
            action="store_true",
            help="follow HTTP(S) links and redirects outside the seed hostnames",
        )
        parser.add_argument(
            "--concurrency",
            type=positive_integer,
            default=DEFAULT_CONCURRENCY,
            help="maximum active requests",
        )
        parser.add_argument(
            "--max-pages",
            type=positive_integer,
            default=DEFAULT_MAX_PAGES,
            help="maximum unique request URLs across all seeds, including redirects",
        )
        parser.add_argument(
            "--max-depth",
            type=nonnegative_integer,
            default=None,
            help="maximum link/redirect depth; seed is 0, omitted means unlimited",
        )
        parser.add_argument(
            "--max-retries",
            type=nonnegative_integer,
            default=DEFAULT_MAX_RETRIES,
            help="additional attempts per URL after retryable fetch errors; 0 disables retries",
        )
        parser.add_argument(
            "--log-level",
            type=str.upper,
            choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
            default="INFO",
            help="crawler logging verbosity on stderr",
        )
        parser.add_argument(
            "--output-dir",
            type=Path,
            default=DEFAULT_OUTPUT_DIR,
            metavar="DIR",
            help="save HTML files and report.json here; existing page files and reports are overwritten",
        )
        arguments = Arguments()
        parser.parse_intermixed_args(argv, namespace=arguments)
        return arguments

    def _report_summary(self, summary: CrawlSummary) -> int:
        suffix = (
            "; page limit reached: eligible URLs omitted" if summary.truncated else ""
        )
        print(
            f"Attempted: {summary.attempted}; failed: {summary.failed}{suffix}",
            file=self._stderr,
        )
        return 1 if summary.failed or summary.truncated else 0

    def run(self, argv: Sequence[str] | None = None) -> int:
        arguments = self.parse_args(argv)
        with log_to_stream(arguments.log_level, self._stderr):
            return self._execute(arguments)

    def _execute(self, arguments: Arguments) -> int:
        try:
            config = CrawlConfig(
                seed_urls=tuple(arguments.seed_urls),
                concurrency=arguments.concurrency,
                max_pages=arguments.max_pages,
                max_depth=arguments.max_depth,
                output_dir=arguments.output_dir,
                follow_external=arguments.follow_external,
                max_retries=arguments.max_retries,
            )
            json_writer = JsonWriter(config.output_dir)
            logger.debug("Preparing output directory: %s", config.output_dir)
            html_writer = HtmlWriter(config.output_dir)
            report = asyncio.run(self.crawl(config, html_writer))
            logger.debug("Writing final report: %s", json_writer.path)
            path = json_writer.write(report)
            logger.info("Report written: %s", path)
            for result in report.results:
                print_result(result, self._stdout)
            print(f"Report: {terminal_text(str(path))}", file=self._stdout)
            return self._report_summary(report.summary)
        except KeyboardInterrupt:
            logger.info("Crawl interrupted by user")
            print("Interrupted.", file=self._stderr)
            return 130
        except OSError as error:
            logger.exception("Output failed")
            try:
                print(f"Output failed: {terminal_text(str(error))}", file=self._stderr)
            except OSError:
                pass  # stderr itself may be closed.
            return 1


def parse_args(argv: Sequence[str] | None = None) -> Arguments:
    """Convenience wrapper for callers only interested in argument validation."""
    return CrawlerCLI().parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point; all ownership lives in CrawlerCLI."""
    return CrawlerCLI().run(argv)
