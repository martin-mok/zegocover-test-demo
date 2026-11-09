import pytest

from zegocover_test.config import CrawlConfig


@pytest.mark.parametrize(
    ("concurrency", "max_pages", "max_depth"),
    [(0, 20, None), (8, 0, None), (8, 20, -1)],
)
def test_config_rejects_invalid_limits(
    concurrency: int, max_pages: int, max_depth: int | None
) -> None:
    with pytest.raises(ValueError):
        CrawlConfig(("https://example.test/",), concurrency, max_pages, max_depth)


def test_config_requires_at_least_one_seed() -> None:
    with pytest.raises(ValueError, match="seed_urls"):
        CrawlConfig(())


@pytest.mark.parametrize("max_retries", [-1, True, 1.5, float("nan")])
def test_config_rejects_invalid_retry_count(max_retries: int) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        CrawlConfig(("https://example.test/",), max_retries=max_retries)
