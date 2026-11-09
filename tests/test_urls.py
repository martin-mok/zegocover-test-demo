import httpx
import pytest

from zegocover_test.urls import HttpURLPolicy

urls = HttpURLPolicy()

SEED = "https://example.test/docs/index.html"


@pytest.mark.parametrize(
    ("base", "raw_href", "expected"),
    [
        (SEED + "?sort=price", "?", SEED + "?"),
        (SEED + "?sort=price", "?#top", SEED + "?#top"),
        (SEED + "?sort=price", "child?", "https://example.test/docs/child?"),
        (SEED + "?sort=price", "/child?", "https://example.test/child?"),
        (SEED, "https://other.test/path?", "https://other.test/path?"),
        (SEED, "//other.test/path?#top", "https://other.test/path?#top"),
        (SEED + "?", "#top", SEED + "?#top"),
        (SEED + "?sort=price", "#", SEED + "?sort=price#"),
        (SEED + "?sort=price#old", "", SEED + "?sort=price"),
        (SEED, "b//c#top", "https://example.test/docs/b//c#top"),
        ("https://example.test/a//b/index", "child", "https://example.test/a//b/child"),
        ("https://example.test//a/index", "child", "https://example.test//a/child"),
        (SEED, "/b//c", "https://example.test/b//c"),
        (SEED, "b//../c", "https://example.test/docs/b/c"),
        (SEED, "b/./c/../", "https://example.test/docs/b/"),
        (SEED, "b/.", "https://example.test/docs/b/"),
        (SEED, "b/..", "https://example.test/docs/"),
        (SEED, "../../../child", "https://example.test/child"),
        (SEED, "b%2Fc//d?x=%2F&x=1", "https://example.test/docs/b%2Fc//d?x=%2F&x=1"),
        ("https://example.test", "child", "https://example.test/child"),
    ],
)
def test_resolution_preserves_query_delimiters_and_path_segments(
    base: str, raw_href: str, expected: str
) -> None:
    assert urls.resolve_href(base, raw_href) == expected
    fetch_url = urls.to_fetch_url(expected)
    assert fetch_url == expected.partition("#")[0]
    assert str(httpx.Request("GET", fetch_url).url) == fetch_url


@pytest.mark.parametrize(
    ("raw_href", "resolved_url", "fetch_url", "allowed"),
    [
        (
            "../about#team",
            "https://example.test/about#team",
            "https://example.test/about",
            True,
        ),
        (
            "https://EXAMPLE.test:443/about",
            "https://example.test/about",
            "https://example.test/about",
            True,
        ),
        ("#top", SEED + "#top", SEED, True),
        ("?a=1&a=2", SEED + "?a=1&a=2", SEED + "?a=1&a=2", True),
        (
            "https://sub.example.test/",
            "https://sub.example.test/",
            "https://sub.example.test/",
            False,
        ),
        (
            "https://example.test.other.test/",
            "https://example.test.other.test/",
            "https://example.test.other.test/",
            False,
        ),
        (
            "http://example.test:8080/",
            "http://example.test:8080/",
            "http://example.test:8080/",
            True,
        ),
        ("mailto:hello@example.test", "mailto:hello@example.test", None, False),
    ],
)
def test_href_resolution_and_scope(
    raw_href: str, resolved_url: str, fetch_url: str | None, allowed: bool
) -> None:
    assert urls.resolve_href(SEED, raw_href) == resolved_url
    assert urls.to_fetch_url(resolved_url) == fetch_url
    if fetch_url is not None:
        assert (
            urls.has_allowed_host(
                fetch_url, frozenset({urls.host(urls.normalize(SEED))})
            )
            is allowed
        )
    else:
        assert not allowed
    if fetch_url is not None:
        assert str(httpx.Request("GET", fetch_url).url) == fetch_url


@pytest.mark.parametrize(
    "value",
    [
        "example.test",
        "ftp://example.test/",
        "https:///path",
        "https://user:pw@example.test/",
        "https://@example.test/",
        "https://example.test:bad/",
        "https://example.test:+80/",
        "https://example.test:-1/",
        "https://example.test:１２３/",
        "https://example.test:65536/",
        "https://example.test:0/",
        "https://[oops/",
        "https://bad host/",
        "https://exa\nmple.test/",
        " https://example.test/",
    ],
)
def test_invalid_seed_and_discovery(value: str) -> None:
    with pytest.raises(ValueError):
        urls.normalize(value)
    assert urls.to_fetch_url(value) is None


def test_identity_preserves_path_and_query_data() -> None:
    assert urls.normalize("HTTPS://EXAMPLE.test.:443#top") == "https://example.test/"
    assert (
        urls.normalize("https://example.test/A%2Fb/?z=2&z=1#x")
        == "https://example.test/A%2Fb/?z=2&z=1"
    )
    assert urls.normalize("https://example.test/a") != urls.normalize(
        "https://example.test/a/"
    )
    assert urls.normalize("https://example.test/A") != urls.normalize(
        "https://example.test/a"
    )
    assert urls.resolve_href(SEED, "http://[invalid") is None


@pytest.mark.parametrize(
    "host",
    [
        "999.999.9999",
        "999.999.9999.",
        "256.1.1.1",
        "1.2.3.4.5",
        "127.1",
        "2130706433",
        "0177.0.0.1",
        "0x7f000001",
        "0x7f.0.0.1",
        "[2001:db8::gg]",
        "[fe80::1%25eth0]",
        "[::1%25foo bar]",
        "[::1]extra",
        "example..test",
        "-example.test",
        "example-.test",
        "example.test..",
        "bad_host.test",
        "example%2etest",
        "example\\other.test",
        "a" * 64 + ".test",
        ".".join(["a" * 63] * 4),
    ],
)
def test_invalid_hosts_are_rejected_for_seeds_and_links(host: str) -> None:
    value = f"https://{host}/"
    with pytest.raises(ValueError):
        urls.normalize(value)
    assert urls.to_fetch_url(value) is None
    assert urls.resolve_href(SEED, value) is None
    assert urls.resolve_href(SEED, f"//{host}/") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://127.0.0.1:80", "http://127.0.0.1/"),
        ("http://192.0.2.1.:8080", "http://192.0.2.1:8080/"),
        ("https://[2001:0DB8:0:0:0:0:0:1]:443", "https://[2001:db8::1]/"),
        ("https://BÜCHER.test./", "https://xn--bcher-kva.test/"),
        ("http://localhost:8000", "http://localhost:8000/"),
        ("https://123.example.test", "https://123.example.test/"),
        ("https://0xff.example.test", "https://0xff.example.test/"),
        ("https://my-site.test:1", "https://my-site.test:1/"),
        ("https://example.test:65535", "https://example.test:65535/"),
        (
            "https://example.test/a;b?x=%2F&x=1#top",
            "https://example.test/a;b?x=%2F&x=1",
        ),
    ],
)
def test_normalization_is_stable_and_matches_request_url(
    value: str, expected: str
) -> None:
    fetch_url = urls.normalize(value)
    assert fetch_url == expected
    assert urls.normalize(fetch_url) == fetch_url
    assert str(httpx.Request("GET", fetch_url).url) == fetch_url


def test_ipv4_mapped_ipv6_forms_share_request_identity() -> None:
    assert urls.normalize("http://[::ffff:192.0.2.1]/") == urls.normalize(
        "http://[::ffff:c000:201]/"
    )


def test_hostname_length_boundary() -> None:
    host = ".".join(["a" * 63] * 3 + ["b" * 61])
    assert urls.normalize(f"https://{host}.") == f"https://{host}/"


@pytest.mark.parametrize("control", ["\x00", "\t", "\n", "\r", "\x1f", "\x7f"])
@pytest.mark.parametrize("position", [0, 12, len(SEED)])
def test_control_characters_are_rejected_before_parsing_or_trimming(
    control: str, position: int
) -> None:
    value = SEED[:position] + control + SEED[position:]
    with pytest.raises(ValueError, match="control characters"):
        urls.normalize(value)
    assert urls.to_fetch_url(value) is None
    assert urls.resolve_href(SEED, value) is None
    assert urls.resolve_href(value, "child") is None


def test_href_spaces_are_trimmed_and_fragments_are_retained() -> None:
    assert (
        urls.resolve_href(SEED, "  ../about#team  ")
        == "https://example.test/about#team"
    )
    assert urls.normalize(SEED + "#top", preserve_fragment=True) == SEED + "#top"
    assert urls.normalize(SEED, preserve_fragment=True) == SEED


@pytest.mark.parametrize(
    ("candidate", "allowed"),
    [
        ("https://example.com/about", True),
        ("http://python.org:8080/", True),
        ("https://sub.python.org/", False),
        ("https://python.org.other.test/", False),
        ("https://external.test/", False),
        ("https://xn--bcher-kva.test/", True),
        ("https://bücher.test:8443/", True),
        ("https://[2001:db8::1]:8443/", True),
        ("http://[2001:db8::2]/", False),
    ],
)
def test_cached_host_scope_handles_normalized_seed_variants(
    candidate: str, allowed: bool
) -> None:
    seeds = (
        "HTTPS://EXAMPLE.com.:443/start",
        "https://python.org",
        "https://bücher.test",
        "http://[2001:0db8::1]/",
    )
    allowed_hosts = frozenset(urls.host(urls.normalize(seed)) for seed in seeds)
    fetch_url = urls.normalize(candidate)
    assert urls.has_allowed_host(fetch_url, allowed_hosts) is allowed
