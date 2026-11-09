"""Exercise the installed CLI against a small local HTTP graph."""

import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture
def local_site() -> Iterator[tuple[str, list[str]]]:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/a")
                self.end_headers()
                return
            if self.path == "/":
                html = (
                    '<a href="/a#one">A</a><a href="/a#two">A again</a>'
                    '<a href="/redirect">Redirect</a>'
                    f'<a href="http://localhost:{server.server_port}/external">Other host</a>'
                )
            elif self.path == "/a":
                html = '<a href="/deep">Deep</a><a href="/">Cycle</a>'
            else:
                html = ""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("scope_mode", ["single", "multiple", "external"])
@pytest.mark.parametrize("max_depth", [None, 1])
def test_installed_cli_saves_html_and_final_report(
    local_site: tuple[str, list[str]],
    tmp_path: Path,
    max_depth: int | None,
    scope_mode: str,
) -> None:
    seed, requests = local_site
    external = seed.replace("127.0.0.1", "localhost") + "external"
    directory = tmp_path / "crawl"
    command = [
        str(Path(sys.executable).with_name("zegocover-test")),
        seed,
        "--output-dir",
        str(directory),
    ]
    if scope_mode == "multiple":
        command.append(external)
    elif scope_mode == "external":
        command.append("--follow-external")
    if max_depth is not None:
        command.extend(["--max-depth", str(max_depth)])
    process = subprocess.run(
        command, capture_output=True, text=True, timeout=10, check=False
    )
    assert len(requests) == len(set(requests))
    report = json.loads((directory / "report.json").read_text())
    records = report["results"]
    assert report["summary"]["attempted"] == len(requests) == len(records)
    assert process.stdout.count("Page: ") == len(records)
    assert f"Report: {directory / 'report.json'}" in process.stdout
    assert process.returncode == 0
    assert process.stderr.endswith(f"Attempted: {len(records)}; failed: 0\n")
    assert f"worker=crawl-fetch-0 url={seed}" in process.stderr
    extra = {"/external"} if scope_mode != "single" else set()
    if max_depth is not None:
        assert set(requests) == {"/", "/a", "/redirect"} | extra
        assert all(record["depth"] <= 1 for record in records)
    else:
        assert set(requests) == {"/", "/a", "/redirect", "/deep"} | extra
        assert "Depth: 2" in process.stdout
    redirected = [record for record in records if record["status_code"] == 302]
    assert len(redirected) == 1 and redirected[0]["redirect_target"] == seed + "a"
    assert redirected[0]["html_file"] is None
    root = next(record for record in records if record["fetch_url"] == seed)
    assert root["links"][:2] == [seed + "a#one", seed + "a#two"]
    assert len(list(directory.glob("*.html"))) == len(records) - 1
    for record in records:
        assert "html" not in record
        if record["html_file"] is not None:
            path = directory / record["html_file"]
            assert path.is_file() and len(path.stem) == 64
            if record["fetch_url"] == seed:
                assert b"/a#one" in path.read_bytes()

    external_records = [record for record in records if record["fetch_url"] == external]
    if scope_mode == "single":
        assert external_records == []
    else:
        assert len(external_records) == 1
        assert external_records[0]["depth"] == (0 if scope_mode == "multiple" else 1)
