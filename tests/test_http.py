"""Loopback HTTP: pagination, 429, 401, 5xx, timeout, size, and a dirty title."""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import service


def _network_blocked() -> bool:
    try:
        sock = socket.socket()
        try:
            sock.connect(("127.0.0.1", 1))
        finally:
            sock.close()
    except RuntimeError as exc:
        return "network blocked" in str(exc)
    except OSError:
        return False
    return False


if _network_blocked():
    pytest.skip("socket connect is blocked", allow_module_level=True)

KEY = "lin_api_TESTKEYVALUE123456"


class Scripted(BaseHTTPRequestHandler):
    responses = []
    seen = []
    delay = 0.0

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length)
        Scripted.seen.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": body,
        })
        if Scripted.delay:
            import time
            time.sleep(Scripted.delay)
        status, payload = Scripted.responses.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if status in {301, 302, 303, 307, 308}:
            self.send_header("Location", "https://example.invalid/stolen")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):  # noqa: A003
        return


def serve(responses, delay=0.0):
    Scripted.responses = list(responses)
    Scripted.seen = []
    Scripted.delay = delay
    server = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def box(tmp_path, server, **kwargs):
    host, port = server.server_address
    deps = service.Deps()
    deps.data_dir = tmp_path / "data"
    deps.secrets = lambda name: (KEY, None) if name == "LINEAR_API_KEY" else (f"http://{host}:{port}/graphql", None)
    deps.idle_seconds = kwargs.get("idle", 2)
    deps.total_seconds = kwargs.get("total", 3)
    deps.body_cap = kwargs.get("cap", 1_000_000)
    deps.show_limit = 30
    deps.max_pages = 4
    return deps


def node(identifier="LIN-1", title="Title"):
    return {
        "identifier": identifier,
        "title": title,
        "url": f"https://linear.app/acme/issue/{identifier}",
        "dueDate": "2026-10-28",
        "updatedAt": "2026-10-01T00:00:00.000Z",
        "state": {"name": "Todo", "type": "unstarted"},
    }


def envelope(nodes, has_next=False, cursor=None):
    return {"data": {"viewer": {"assignedIssues": {"nodes": nodes, "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}}}}}


def test_pagination_authorization_and_dirty_title(tmp_path):
    server = serve([
        (200, envelope([node("LIN-1", "Line\n**one**\x00")], True, "c1")),
        (200, envelope([node("LIN-2", "Two")])),
    ])
    try:
        text, error = service.digest_stdout(box(tmp_path, server))
    finally:
        server.shutdown()
    assert error is None
    assert "LIN-1" in text and "LIN-2" in text
    assert "\n**" not in text
    row = next(line for line in text.splitlines() if line.startswith("LIN-1\t"))
    assert row.split("\t")[1] == "Line **one**"
    assert len(Scripted.seen) == 2
    assert all(item["authorization"] == KEY for item in Scripted.seen)
    assert all(not item["authorization"].startswith("Bearer ") for item in Scripted.seen)
    assert KEY not in text


def test_401_429_and_5xx_are_one_try_and_hide_the_key(tmp_path):
    for status in (401, 429, 503):
        server = serve([(status, {"errors": [{"message": KEY, "extensions": {"code": "RATELIMITED"}}]})])
        try:
            text, error = service.digest_stdout(box(tmp_path, server))
        finally:
            server.shutdown()
        assert text == ""
        assert KEY not in error
        assert len(Scripted.seen) == 1
        if status == 401:
            assert "401" in error
        elif status == 429:
            assert "429" in error
        else:
            assert "503" in error


def test_redirect_is_not_followed(tmp_path):
    server = serve([(302, b"")])
    try:
        text, error = service.digest_stdout(box(tmp_path, server))
    finally:
        server.shutdown()
    assert text == ""
    assert "redirect" in error.lower()
    assert len(Scripted.seen) == 1
    assert KEY not in error


def test_timeout(tmp_path):
    server = serve([(200, envelope([node()]))], delay=2)
    try:
        text, error = service.digest_stdout(box(tmp_path, server, idle=0.2, total=0.4))
    finally:
        server.shutdown()
    assert text == ""
    assert "timed out" in error
    assert KEY not in error


def test_oversize_body(tmp_path):
    fat = {"data": {"viewer": {"assignedIssues": {"nodes": [node(title="x" * 400)], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
    server = serve([(200, fat)])
    try:
        text, error = service.digest_stdout(box(tmp_path, server, cap=80))
    finally:
        server.shutdown()
    assert text == ""
    assert "larger than the limit" in error


def test_http_200_with_errors_is_not_success(tmp_path):
    body = {
        "errors": [{"message": KEY, "path": ["viewer"], "extensions": {"code": "AUTHENTICATION_ERROR"}}],
        "data": {"viewer": {"assignedIssues": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}},
    }
    server = serve([(200, body)])
    try:
        text, error = service.digest_stdout(box(tmp_path, server))
    finally:
        server.shutdown()
    assert text == ""
    assert "errors" in error
    assert KEY not in error


def test_proxy_env_is_not_used(tmp_path, monkeypatch):
    proxy_seen = []

    class Proxy(BaseHTTPRequestHandler):
        def do_CONNECT(self):  # noqa: N802
            proxy_seen.append(self.path)
            self.send_error(500)

        def do_POST(self):  # noqa: N802
            proxy_seen.append(self.path)
            self.send_error(500)

        def log_message(self, fmt, *args):  # noqa: A003
            return

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    phost, pport = proxy.server_address
    proxy_url = f"http://{phost}:{pport}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy", "ftp_proxy", "FTP_PROXY"):
        monkeypatch.setenv(name, proxy_url)
    server = serve([(200, envelope([node("LIN-9")]))])
    try:
        text, error = service.digest_stdout(box(tmp_path, server))
    finally:
        server.shutdown()
        proxy.shutdown()
    assert error is None
    assert "LIN-9" in text
    assert proxy_seen == []
    assert len(Scripted.seen) == 1
