"""Shared test helpers: a scripted fake HTTP server (Jira, Vault, Kubernetes, Prometheus) and the
runbooks repo fixture (a copy of Kyle-Law/a-demo-runbooks at a known commit)."""

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

            def do_PATCH(self):
                self._do("PATCH")

            def do_DELETE(self):
                self._do("DELETE")

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



FIXTURE = Path(__file__).resolve().parent / "fixtures" / "a-demo-runbooks"


def repo_tarball(sha: str, overrides: dict[str, str | None] | None = None) -> bytes:
    """A GitHub-style tarball ("<repo>-<sha>/..." paths) of the fixture repo; overrides replace
    (str) or drop (None) files by repo path."""
    import io
    import tarfile
    files = {str(p.relative_to(FIXTURE)): p.read_text() for p in FIXTURE.rglob("*") if p.is_file()}
    for path, text in (overrides or {}).items():
        if text is None:
            files.pop(path, None)
        else:
            files[path] = text
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"a-demo-runbooks-{sha}/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()
