"""One HTTPS POST to api.linear.app. No redirect, no proxy, no second host.

A personal API key is sent as the Authorization value itself. The Linear
GraphQL page shows Bearer only for OAuth access tokens.
"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

if __package__:
    from .query import ASSIGNED_QUERY
else:
    from query import ASSIGNED_QUERY

API_HOST = "api.linear.app"
API_PATH = "/graphql"
# The request URL is this join, and only POST is used.
API_URL = "https://" + API_HOST + API_PATH
USER_AGENT = "hermes-plugin-linear-digest/0.1.0"
IDLE_SECONDS = 15.0
TOTAL_SECONDS = 20.0
BODY_CAP = 1_000_000
READ_CHUNK = 65_536
REDIRECTS = frozenset({301, 302, 303, 307, 308})

# status, response headers, body
Transport = Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]]

FAILURES = (
    {"ok": False, "error_type": "missing_key", "message": "LINEAR_API_KEY is not set for this profile. Nothing was fetched. Set it in this profile's .env."},
    {"ok": False, "error_type": "unscoped", "message": "The profile secret scope was not active, so the key was not read. Nothing was fetched."},
    {"ok": False, "error_type": "no_reader", "message": "The secret reader was not available, so the key was not read. Nothing was fetched."},
    {"ok": False, "error_type": "bad_key", "message": "LINEAR_API_KEY is not usable as a header value. Nothing was fetched."},
    {"ok": False, "error_type": "rehearsal", "message": "LINEAR_DIGEST_REHEARSAL is set but is not an http loopback URL. Nothing was fetched."},
    {"ok": False, "error_type": "timeout", "message": "The Linear request timed out. Nothing was fetched."},
    {"ok": False, "error_type": "network", "message": "The Linear request failed before a response. Nothing was fetched."},
    {"ok": False, "error_type": "redirect", "message": "Linear redirected the request, and the redirect was not followed. Nothing was fetched."},
    {"ok": False, "error_type": "oversize", "message": "The Linear response was larger than the limit. Nothing was fetched."},
    {"ok": False, "error_type": "http_401", "message": "Linear refused the key (HTTP 401). Nothing was fetched. Set LINEAR_API_KEY in this profile's .env."},
    {"ok": False, "error_type": "http_429", "message": "Linear rate limited the request (HTTP 429). Nothing was fetched. Try again later."},
    {"ok": False, "error_type": "bad_body", "message": "Linear returned a response this plugin could not read. Nothing was fetched."},
    {"ok": False, "error_type": "graphql", "message": "Linear returned errors in the response. Nothing was fetched."},
)
MESSAGES = {row["error_type"]: row["message"] for row in FAILURES}


class ClientError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or MESSAGES.get(code) or "The Linear request failed. Nothing was fetched."
        super().__init__(self.message)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None

    def http_error_301(self, req, fp, code, msg, headers):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


def read_secret(name: str) -> tuple[str | None, str | None]:
    """Return (value, error code). Never reads os.environ itself.

    ``get_secret`` is the profile-scoped reader. A missing module, a raised
    read, or an unscoped multiplexed process does not fall through.
    """
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret
    except Exception:
        return None, "no_reader"
    try:
        value = get_secret(name)
    except UnscopedSecretError:
        return None, "unscoped"
    except Exception:
        return None, "unscoped"
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, None
    return value, None


def header_safe(key: str) -> bool:
    if not key or len(key) > 500:
        return False
    return all(33 <= ord(char) <= 126 for char in key)


def _loopback_http(raw: str) -> bool:
    """True only for loopback http on 127.0.0.1, localhost, or ::1, with a port."""
    text = raw.strip()
    if any(ord(char) < 33 or ord(char) == 127 for char in text):
        return False
    parts = urllib.parse.urlsplit(text)
    if parts.scheme != "http" or parts.username or parts.password or parts.query or parts.fragment:
        return False
    if parts.path not in {"", "/graphql"}:
        return False
    host = (parts.hostname or "").rstrip(".").lower()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        return False
    if parts.port is None or not (1 <= parts.port <= 65535):
        return False
    return True


def endpoint(rehearsal: str | None) -> str:
    """Production URL, or a refused rehearsal. Unset rehearsal is production."""
    if rehearsal is None or rehearsal.strip() == "":
        return API_URL
    if _loopback_http(rehearsal):
        return rehearsal.strip()
    raise ClientError("rehearsal")


def _opener(box: dict[str, Any]):
    lock = box["lock"]

    class HTTP(http.client.HTTPConnection):
        def connect(self):
            super().connect()
            with lock:
                box["sock"] = self.sock

    class HTTPS(http.client.HTTPSConnection):
        def connect(self):
            super().connect()
            with lock:
                box["sock"] = self.sock

    class HTTPOpen(urllib.request.HTTPHandler):
        def http_open(self, req):  # noqa: ANN001
            return self.do_open(HTTP, req)

    class HTTPSOpen(urllib.request.HTTPSHandler):
        def https_open(self, req):  # noqa: ANN001
            return self.do_open(HTTPS, req)

    # An explicit empty proxy handler replaces the env-based default.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        HTTPOpen(),
        HTTPSOpen(),
    )


def _close_socket(sock) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _read_response(response, cap: int, deadline: float) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        if time.monotonic() > deadline:
            raise ClientError("timeout")
        try:
            block = response.read(READ_CHUNK)
        except socket.timeout as exc:
            raise ClientError("timeout") from exc
        if not block:
            return b"".join(chunks)
        size += len(block)
        if size > cap:
            raise ClientError("oversize")
        chunks.append(block)


def _status_error(status: int) -> ClientError | None:
    if status in REDIRECTS:
        return ClientError("redirect")
    if status == 401:
        return ClientError("http_401")
    if status == 429:
        return ClientError("http_429")
    if 500 <= status <= 599:
        return ClientError("http_5xx", f"Linear returned HTTP {status}. Nothing was fetched.")
    if status != 200:
        return ClientError("http_other", f"Linear returned HTTP {status}. Nothing was fetched.")
    return None


def _parse_body(payload: bytes) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8")
        body = json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ClientError("bad_body") from exc
    if not isinstance(body, dict):
        raise ClientError("bad_body")
    errors = body.get("errors")
    if errors:
        raise ClientError("graphql")
    data = body.get("data")
    if not isinstance(data, dict):
        raise ClientError("bad_body")
    return data


def _exchange(url: str, header_key: str, variables: dict[str, Any], idle: float, deadline: float, cap: int, box: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps({"query": ASSIGNED_QUERY, "variables": variables}, ensure_ascii=True).encode("utf-8")
    request = urllib.request.Request(url, data=raw, method="POST")
    request.add_header("Authorization", header_key)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    opener = _opener(box)
    try:
        response = opener.open(request, timeout=idle)
    except urllib.error.HTTPError as exc:
        status_error = _status_error(exc.code)
        try:
            exc.read(1024)
        except Exception:
            pass
        try:
            exc.close()
        except Exception:
            pass
        if status_error is not None:
            raise status_error from None
        raise ClientError("network") from None
    except (socket.timeout, TimeoutError, urllib.error.URLError) as exc:
        if isinstance(exc, urllib.error.URLError) and not isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
            raise ClientError("network") from None
        raise ClientError("timeout") from None
    except ClientError:
        raise
    except Exception as exc:
        raise ClientError("network") from exc
    try:
        status_error = _status_error(getattr(response, "status", 200))
        if status_error is not None:
            raise status_error
        payload = _read_response(response, cap, deadline)
    finally:
        try:
            response.close()
        except Exception:
            pass
    return _parse_body(payload)


def post_assigned(url: str, header_key: str, variables: dict[str, Any], *, idle: float, total: float, cap: int, transport: Transport | None) -> dict[str, Any]:
    """POST the assigned-issues query. The key stays in the header of this call."""
    if transport is not None:
        status, _headers, payload = transport(url, {"Authorization": header_key, "Content-Type": "application/json"})
        status_error = _status_error(status)
        if status_error is not None:
            raise status_error
        if len(payload) > cap:
            raise ClientError("oversize")
        return _parse_body(payload)
    box: dict[str, Any] = {"sock": None, "lock": threading.Lock(), "err": None, "data": None}
    deadline = time.monotonic() + total

    def run() -> None:
        try:
            box["data"] = _exchange(url, header_key, variables, idle, deadline, cap, box)
        except BaseException as exc:
            box["err"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(max(0.05, total))
    if worker.is_alive():
        with box["lock"]:
            sock = box.get("sock")
        _close_socket(sock)
        worker.join(0.2)
        raise ClientError("timeout")
    if box["err"] is not None:
        err = box["err"]
        if isinstance(err, ClientError):
            raise err
        raise ClientError("network") from None
    data = box["data"]
    if not isinstance(data, dict):
        raise ClientError("bad_body")
    return data
