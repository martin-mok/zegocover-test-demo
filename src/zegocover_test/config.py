"""Validated crawl settings and fixed resource limits."""

from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONCURRENCY = 8
DEFAULT_MAX_PAGES = 20
DEFAULT_OUTPUT_DIR = Path("crawl-output")
DEFAULT_MAX_RETRIES = 0
HTTP_TIMEOUT = 10.0
MAX_BODY_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class CrawlConfig:
    """Immutable per-run settings, passed explicitly to the engine/assembly code."""

    seed_urls: tuple[str, ...]
    concurrency: int = DEFAULT_CONCURRENCY
    max_pages: int = DEFAULT_MAX_PAGES
    max_depth: int | None = None
    output_dir: Path = DEFAULT_OUTPUT_DIR
    follow_external: bool = False
    max_retries: int = DEFAULT_MAX_RETRIES

    def __post_init__(self) -> None:
        if isinstance(self.seed_urls, str) or not self.seed_urls:
            raise ValueError("seed_urls must be a nonempty collection of URLs")
        if self.concurrency <= 0:
            raise ValueError("concurrency must be positive")
        if self.max_pages <= 0:
            raise ValueError("max_pages must be positive")
        if self.max_depth is not None and self.max_depth < 0:
            raise ValueError("max_depth must be nonnegative")
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
