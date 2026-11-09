import pytest

from zegocover_test.parser import HtmlParser
from zegocover_test.urls import HttpURLPolicy

PAGE = "https://example.test/docs/index.html"
parser = HtmlParser(HttpURLPolicy())


def test_parser_uses_injected_url_policy() -> None:
    class FilteredPolicy(HttpURLPolicy):
        def resolve_href(self, base_url: str, raw_href: str) -> str | None:
            return (
                None
                if raw_href == "/blocked"
                else super().resolve_href(base_url, raw_href)
            )

    parser = HtmlParser(FilteredPolicy())
    assert parser.parse(
        b'<a href="/blocked">No</a><a href="/ok">Yes</a>', PAGE
    ).links == ("https://example.test/ok",)


def test_parse_empty_html_without_encoding_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert parser.parse(b"", PAGE, "text/html; charset=utf-8").links == ()
    assert not caplog.records


def test_parse_order_duplicates_fragments_and_external_links() -> None:
    html = b"""
        <a href="../about#one">About</a><a href="../about#one">Duplicate</a>
        <area href="../about#two"><a href="https://other.test/">External</a>
        <a href="mailto:hi@example.test">Mail</a><a href="">Self</a>
        <a href="http://[broken">Bad</a><a>No href</a><img src="/image.png">
    """
    assert parser.parse(html, PAGE).links == (
        "https://example.test/about#one",
        "https://example.test/about#two",
        "https://other.test/",
        "mailto:hi@example.test",
        PAGE,
    )


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("/guide/", "https://example.test/guide/chapter"),
        ("https://other.test/guide/", "https://other.test/guide/chapter"),
        ("mailto:hi@example.test", "https://example.test/docs/chapter"),
        ("http://[bad", "https://example.test/docs/chapter"),
    ],
)
def test_parse_first_document_base(base: str, expected: str) -> None:
    html = f'<base href="{base}"><base href="/ignored/"><a href="chapter">Next</a>'
    assert parser.parse(html.encode(), PAGE).links == (expected,)


def test_parse_declared_encoding_and_invalid_hint() -> None:
    assert parser.parse(
        b'<a href="/caf\xe9">Cafe</a>', PAGE, "text/html; charset=iso-8859-1"
    ).links == ("https://example.test/caf%C3%A9",)
    assert parser.parse(
        b'<a href="/ok">OK</a>', PAGE, "text/html; charset=not-an-encoding"
    ).links == ("https://example.test/ok",)
