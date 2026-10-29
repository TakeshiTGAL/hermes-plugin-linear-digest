"""Loopback stand-in for the offline check. It does not call Linear."""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EMPTY = "--empty" in sys.argv

ISSUES = [
    {
        "identifier": "LIN-14",
        "title": "Ship the\nlist",
        "url": "https://linear.app/acme/issue/LIN-14",
        "dueDate": "2026-10-28",
        "updatedAt": "2026-10-01T00:00:00.000Z",
        "state": {"name": "Todo", "type": "unstarted"},
    },
    {
        "identifier": "LIN-15",
        "title": "Review **today**",
        "url": "https://linear.app/acme/issue/LIN-15",
        "dueDate": "2026-10-29",
        "updatedAt": "2026-10-29T00:00:00.000Z",
        "state": {"name": "Doing", "type": "started"},
    },
    {
        "identifier": "LIN-3",
        "title": "Quiet one",
        "url": "https://linear.app/acme/issue/LIN-3",
        "dueDate": None,
        "updatedAt": "2026-09-01T00:00:00.000Z",
        "state": {"name": "Todo", "type": "unstarted"},
    },
]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        self.rfile.read(min(length, 1_000_000))
        if self.path != "/graphql":
            self.send_response(404)
            self.end_headers()
            return
        body = {
            "data": {
                "viewer": {
                    "assignedIssues": {
                        "nodes": [] if EMPTY else ISSUES,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):  # noqa: A003
        return


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host, port = server.server_address
    print(f"LINEAR_DIGEST_REHEARSAL=http://{host}:{port}/graphql", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
