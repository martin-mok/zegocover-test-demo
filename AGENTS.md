# Repository Guidelines

## Project Structure & Module Organization

- `src/zegocover_test/` contains the Python package: `cli.py` handles arguments, `config.py` holds validated settings, `engine.py` coordinates workers, `scheduler.py` owns crawl admission and pending/seen tasks, `fetcher.py` performs HTTP, `parser.py` extracts hyperlinks, `urls.py` defines URL policy, `models.py` holds shared data, and `writer.py` contains `HtmlWriter` for synchronous HTML saving, `JsonWriter` for final JSON export, and console output.
- `tests/` contains pytest modules mirroring those responsibilities. Add tests alongside new functionality and use mocked HTTP or local fixtures.
- `pyproject.toml` defines dependencies, the console script, and the `uv_build` backend. `uv.lock` records resolved dependencies; `.python-version` selects Python 3.13.
- `README.md` holds the exercise requirements.

## Build, Test, and Development Commands

Run these commands from the repository root with uv installed:

- `uv sync --locked`: install the project and development dependencies using the lockfile.
- `uv run zegocover-test`: run the console entry point.
- `uv run pytest`: run the full test suite.
- `uv run pytest tests/test_engine.py`: run a single test module.
- `uv run mypy`: check source and tests with strict typing.
- `uv run ruff check .`: lint Python code.
- `uv run ruff format .`: format Python code; use `--check` to check without modifying files.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` for modules, functions, and variables, and `PascalCase` for classes. Follow the existing type-annotated function style. Keep CLI handling, fetching, and URL processing in focused modules as the implementation grows. Ruff is included as a development dependency; no custom rules are configured.

Use focused component classes and explicit constructor injection. Do not define
application functions inside other functions; put each responsibility in a named
class method or module-level helper. Avoid a dependency-injection container or
inheritance hierarchy. See `ARCHITECTURE.md` for component and lifecycle diagrams.

## Testing Guidelines

Use pytest with `test_*.py` filenames and `test_*` functions. No coverage threshold is configured. Add tests for new behavior and regressions, especially URL resolution, duplicate links, domain restrictions, and request failures. Prefer local HTML fixtures or mocked HTTP responses to public network requests.

## Crawler Constraints

Accept one or more seed URLs and report each visited page plus its discovered URLs. By default, crawl only exact hostnames from the seeds; --follow-external allows any valid HTTP(S) hostname. Do not use Scrapy or Playwright. Keep concurrency bounded to balance speed, accuracy, and resource use.

URLPolicy owns stateless URL validation, normalization, resolution and scope
comparison. Scheduler uses that policy and CrawlConfig to decide admission,
caching normalized seed hosts and applying depth, scope, deduplication and a shared
page budget before creating tasks. All seeds are submitted at depth 0 before
workers start. External following bypasses only the hostname restriction.
The engine submits seed/link/redirect candidates through scheduler.admit(), owns
worker orchestration and has no direct URLPolicy dependency. Use
`raw_href`, `resolved_url`, and `fetch_url` for the three URL stages. Each task stores
its first-discovered depth (seed 0); boundary pages are fetched/reported without
expanding their links. Depth limits are normal completion, with no reporting flag
or special exit code. One bounded fetch-worker pool runs in a TaskGroup. Each
worker saves complete HTML synchronously through an injected PageWriter using
write(fetch_url, html) before parsing, then collects lightweight CrawlResult
metadata and relative HTML file references in a result list local to run().
Completion requires the scheduler queue to drain and workers
to exit. The engine takes typed collaborators and is single-use. CrawlerCLI
assembles them, owns the HTTP client context, and closes it after workers exit.
TaskGroup handles error/cancellation cleanup. After crawling completes, JsonWriter
synchronously writes one report.json in the same output directory as the HTML.
Console output uses a synchronous function. There is no result queue or writer
pool. Saving HTML overwrites that URL's existing file under the same stable ID;
report.json is overwritten on each completed crawl. Unrelated files remain.
JSON files are exports; resume remains future work.
