import asyncio
import gzip
from collections.abc import AsyncIterator

import httpx
import pytest

from zegocover_test.config import MAX_BODY_BYTES
from zegocover_test.fetcher import HttpFetcher
from zegocover_test.urls import HttpURLPolicy

PAGE = "https://example.test/docs/index.html"


def test_fetcher_uses_injected_policy_and_borrows_client() -> None:
    class NoRedirectPolicy(HttpURLPolicy):
        def resolve_href(self, base_url: str, raw_href: str) -> str | None:
            return None

    async def scenario() -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302,
                headers={"location": "/next", "content-type": "text/html"},
                content=b"large",
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await HttpFetcher(client, NoRedirectPolicy()).fetch(PAGE)
            assert not client.is_closed  # Fetcher borrows; the caller owns closure.
        assert result.redirect_target is None and result.html is None
        assert result.error is not None
        assert "invalid redirect" in result.error

    asyncio.run(scenario())


def test_fetch_html_exactly_once() -> None:
    requests: list[str] = []
    html = b'<a href="/next">Next</a>'

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200, headers={"Content-Type": "text/html; charset=utf-8"}, content=html
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await HttpFetcher(client, HttpURLPolicy()).fetch(PAGE)
        assert result.html == html
        assert result.content_type == "text/html; charset=utf-8"
        assert result.status_code == 200
        assert result.error is None

    asyncio.run(scenario())
    assert requests == [PAGE]


@pytest.mark.parametrize("location", ["../target", "https://external.test/target"])
def test_fetch_redirect_is_returned_without_following(location: str) -> None:
    requests: list[str] = []
    stream = RecordingStream((b'<a href="/body-link">Body</a>',))

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": location, "content-type": "text/html"},
            stream=stream,
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=True
        ) as client:
            result = await HttpFetcher(client, HttpURLPolicy()).fetch(PAGE)
        expected = (
            "https://example.test/target" if location.startswith("..") else location
        )
        assert result.redirect_target == expected
        assert result.html is None
        assert result.error is None

    asyncio.run(scenario())
    assert requests == [PAGE]
    assert stream.closed and stream.read_count == 0


@pytest.mark.parametrize(
    "location", [None, "http://[bad", "mailto:someone@example.test"]
)
def test_fetch_invalid_redirect(location: str | None) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            301, headers={"location": location} if location is not None else {}
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await HttpFetcher(client, HttpURLPolicy()).fetch(PAGE)
        assert result.error is not None and "redirect" in result.error
        assert result.redirect_target is None
        assert not result.retryable

    asyncio.run(scenario())


def test_fetch_http_error_skips_body_and_closes() -> None:
    stream = RecordingStream((b'<a href="/help">Help</a>',))

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            headers={"content-type": "text/html"},
            stream=stream,
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await HttpFetcher(client, HttpURLPolicy()).fetch(PAGE)
        assert result.status_code == 404
        assert result.error == "HTTP 404"
        assert result.html is None
        assert not result.retryable

    asyncio.run(scenario())
    assert stream.closed and stream.read_count == 0


def test_fetch_transport_failure() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await HttpFetcher(client, HttpURLPolicy()).fetch(PAGE)
        assert result.error == "ConnectError: offline"
        assert result.status_code is None
        assert result.html is None
        assert result.retryable

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (200, False),
        (302, False),
        (404, False),
        (408, True),
        (429, True),
        (500, True),
        (501, False),
        (502, True),
        (503, True),
        (504, True),
    ],
)
def test_fetch_classifies_retryable_statuses(status: int, retryable: bool) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(status, headers={"location": "/else"})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await HttpFetcher(client, HttpURLPolicy()).fetch(PAGE)
        assert result.retryable is retryable

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error_type", "retryable"),
    [
        (httpx.ReadTimeout, True),
        (httpx.RemoteProtocolError, True),
        (httpx.LocalProtocolError, False),
        (httpx.UnsupportedProtocol, False),
        (httpx.DecodingError, False),
    ],
)
def test_fetch_classifies_retryable_request_errors(
    error_type: type[httpx.RequestError], retryable: bool
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise error_type("request failed", request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await HttpFetcher(client, HttpURLPolicy()).fetch(PAGE)
        assert result.error and result.retryable is retryable

    asyncio.run(scenario())


class RecordingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...], fail: bool = False) -> None:
        self.chunks = chunks
        self.fail = fail
        self.closed = False
        self.read_count = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk
        if self.fail:
            raise httpx.ReadError("stream failed")

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("content_type", [None, "image/png"])
def test_fetch_non_html_skips_body_and_closes(content_type: str | None) -> None:
    stream = RecordingStream((b"not html",))

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": content_type} if content_type else {},
            stream=stream,
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await HttpFetcher(client, HttpURLPolicy()).fetch(PAGE)
        assert result.skip_reason
        assert result.html is None
        assert result.error is None

    asyncio.run(scenario())
    assert stream.closed and stream.read_count == 0


@pytest.mark.parametrize(
    "outcome", ["empty", "boundary", "oversized", "stream-failure"]
)
def test_fetch_stream_body_limit_and_cleanup(outcome: str) -> None:
    chunks = {
        "empty": (),
        "boundary": (b" " * MAX_BODY_BYTES,),
        "oversized": (b" " * MAX_BODY_BYTES, b"x", b"unread"),
        "stream-failure": (b"partial",),
    }[outcome]
    stream = RecordingStream(chunks, fail=outcome == "stream-failure")

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=stream)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await HttpFetcher(client, HttpURLPolicy()).fetch(PAGE)
        assert result.status_code == (None if outcome == "stream-failure" else 200)
        assert result.retryable is (outcome == "stream-failure")
        if outcome in {"oversized", "stream-failure"}:
            assert result.html is None
            assert result.error
        else:
            assert result.html == b"".join(chunks)
            assert result.error is None

    asyncio.run(scenario())
    assert stream.closed
    if outcome == "oversized":
        assert stream.read_count == 2


@pytest.mark.parametrize("max_body_bytes", [0, -1])
def test_fetcher_rejects_non_positive_body_limit(max_body_bytes: int) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="max_body_bytes must be positive"):
                HttpFetcher(client, HttpURLPolicy(), max_body_bytes=max_body_bytes)

    asyncio.run(scenario())


@pytest.mark.parametrize("body", [b"four", b"large"])
def test_fetcher_uses_injected_body_limit(body: bytes) -> None:
    stream = RecordingStream((body,))

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=stream)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await HttpFetcher(client, HttpURLPolicy(), max_body_bytes=4).fetch(
                PAGE
            )
        if len(body) == 4:
            assert result.html == body
            assert result.error is None
        else:
            assert result.html is None
            assert result.error == "HTML body exceeds size limit"

    asyncio.run(scenario())
    assert stream.closed


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("?", PAGE + "?"),
        ("?#top", PAGE + "?#top"),
        ("b//c", "https://example.test/docs/b//c"),
    ],
)
def test_redirect_preserves_query_reset_and_empty_path_segments(
    location: str, expected: str
) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": location})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await HttpFetcher(client, HttpURLPolicy()).fetch(
                PAGE + "?sort=price"
            )
        assert result.redirect_target == expected
        assert result.error is None

    asyncio.run(scenario())


@pytest.mark.parametrize("encoding", [None, "identity", " Identity "])
def test_fetcher_requests_and_accepts_identity_encoding(encoding: str | None) -> None:
    html = b'<a href="/child">Child</a>'
    stream = RecordingStream((html,))

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        headers = {"content-type": "text/html"}
        if encoding is not None:
            headers["content-encoding"] = encoding
        return httpx.Response(200, headers=headers, stream=stream)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
            headers={"accept-encoding": "gzip, deflate"},
        ) as client:
            result = await HttpFetcher(client, HttpURLPolicy()).fetch(PAGE)
            assert client.headers["accept-encoding"] == "gzip, deflate"
        assert result.html == html and result.error is None

    asyncio.run(scenario())
    assert stream.closed and stream.read_count == 1


@pytest.mark.parametrize("cut", [0, 8, 25])
def test_unexpected_gzip_is_rejected_before_decoding(cut: int) -> None:
    html = b'<a href="/first">First</a>' + b"x" * 1000 + b'<a href="/last">Last</a>'
    compressed = gzip.compress(html, mtime=0)
    body = compressed[:-cut] if cut else compressed
    stream = RecordingStream((body,))

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "text/html",
                "content-encoding": "gzip",
                "content-length": str(len(body)),
            },
            stream=stream,
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await HttpFetcher(client, HttpURLPolicy()).fetch(PAGE)
        assert result.error == "Unsupported HTML content encoding: gzip"
        assert result.status_code == 200 and result.html is None
        assert not result.retryable

    asyncio.run(scenario())
    assert stream.closed and stream.read_count == 0
