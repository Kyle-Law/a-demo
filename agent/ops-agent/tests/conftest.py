"""Shared test helpers: a scripted fake HTTP server for the OpenAI-compatible, Anthropic and Jira APIs."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeServer:
    """Answers POST/GET/PUT with a handler(method, path, body) -> (status, json); records requests."""

    def __init__(self, handler):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self, method):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                outer.requests.append({"method": method, "path": self.path, "body": body,
                                       "headers": {k.lower(): v for k, v in self.headers.items()}})
                status, payload = handler(method, self.path, body)
                raw = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def do_PUT(self):
                self._do("PUT")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def fake_server():
    servers = []

    def make(handler):
        s = FakeServer(handler)
        servers.append(s)
        return s
    yield make
    for s in servers:
        s.close()


def echo_dispatch(log):
    def dispatch(name, args):
        log.append((name, args))
        return f"ran {name}", False
    return dispatch
