"""Alert bridge against an in-process fake Jira (create / dedupe / correlate / resolve / contract).

Ported from Kyle-Law/ai-sre-demo (08-jira-integration/tests). Run: pytest agent/alert-bridge/tests
"""

import importlib.util
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

BRIDGE = Path(__file__).resolve().parents[1] / "bridge.py"


class FakeJiraState:
    def __init__(self):
        self.issues = {}  # key -> {"labels": [...], "comments": [...], "fields": {...}}


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, body):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/rest/api/3/search/jql":
                jql = parse_qs(u.query)["jql"][0]
                label = re.search(r'labels = "([^"]+)"', jql).group(1)
                hits = [{"key": k, "fields": {"labels": v["labels"]}} for k, v in state.issues.items()
                        if label in v["labels"] and not v.get("done")]
                return self._json(200, {"issues": hits, "isLast": True})
            self._json(404, {})

        def do_POST(self):
            body = self._body()
            if self.path == "/rest/api/2/issue":
                key = f"SUP-{len(state.issues) + 1}"
                state.issues[key] = {"labels": body["fields"]["labels"], "comments": [], "fields": body["fields"]}
                return self._json(201, {"key": key})
            m = re.match(r"/rest/api/2/issue/([A-Z]+-\d+)/comment", self.path)
            if m:
                state.issues[m.group(1)]["comments"].append(body["body"])
                return self._json(201, {})
            self._json(404, {})

        def do_PUT(self):
            body = self._body()
            m = re.match(r"/rest/api/2/issue/([A-Z]+-\d+)$", self.path)
            if m:
                for op in body["update"]["labels"]:
                    state.issues[m.group(1)]["labels"].append(op["add"])
                self.send_response(204)
                self.end_headers()
                return
            self._json(404, {})
    return H


@pytest.fixture()
def bridge():
    state = FakeJiraState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ.update(JIRA_BASE_URL=f"http://127.0.0.1:{server.server_port}", JIRA_EMAIL="bot@x",
                      JIRA_API_TOKEN="t", JIRA_PROJECT_KEY="SUP", DRY_RUN="false")
    os.environ.pop("JIRA_API_URL", None)
    spec = importlib.util.spec_from_file_location("bridge", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    jira = mod.Jira(mod.JIRA_BASE_URL, "bot@x", "t")
    yield mod, jira, state
    server.shutdown()


def alert(signal, status="firing", fp="aaa111", cluster="c1", resource="deployment/model-a-vllm"):
    return {"status": status, "fingerprint": fp, "startsAt": "2026-10-27T06:00:00Z",
            "endsAt": "2026-10-27T06:20:00Z", "generatorURL": "http://prometheus/graph?g0.expr=x",
            "labels": {"alertname": signal, "signal": signal, "cluster": cluster, "namespace": "inference",
                       "resource": resource, "severity": "critical", "ticket": "true"},
            "annotations": {"summary": f"{cluster}/inference/{resource} {signal}",
                            "runbook_hint": "restart-model-server"}}


def test_create_dedupe_correlate_resolve(bridge):
    mod, jira, state = bridge
    r = mod.process_payload(jira, {"alerts": [alert("ModelServerDown")]})
    assert r[0]["action"] == "created" and r[0]["key"] == "SUP-1"
    issue = state.issues["SUP-1"]
    assert {"ai-triage", "res-c1-inference-deployment-model-a-vllm", "cluster-c1"} <= set(issue["labels"])
    assert issue["fields"]["summary"] == "[CRITICAL] ModelServerDown: c1/inference/deployment/model-a-vllm"

    # Alertmanager repeat notification for the same alert -> no new ticket
    r = mod.process_payload(jira, {"alerts": [alert("ModelServerDown")]})
    assert r[0]["action"] == "noop" and len(state.issues) == 1

    # Different signal, same resource -> correlated onto SUP-1 (one fault, one ticket)
    r = mod.process_payload(jira, {"alerts": [alert("InferenceQueueBacklog", fp="bbb222")]})
    assert r[0]["action"] == "correlated" and r[0]["key"] == "SUP-1"
    assert "fp-bbb222" in state.issues["SUP-1"]["labels"]
    assert "Correlated alert" in state.issues["SUP-1"]["comments"][-1]

    # Same resource name in another cluster -> its own ticket
    r = mod.process_payload(jira, {"alerts": [alert("ModelServerDown", fp="ccc333", cluster="c2")]})
    assert r[0]["key"] == "SUP-2"

    # Resolution -> comment on SUP-1
    r = mod.process_payload(jira, {"alerts": [alert("ModelServerDown", status="resolved")]})
    assert r[0]["keys"] == ["SUP-1"] and "RESOLVED" in state.issues["SUP-1"]["comments"][-1]


def test_ticket_contract_is_machine_readable(bridge):
    """The agent reads the CLAUDE.md alert -> ticket contract from the ALERT_JSON block."""
    mod, jira, state = bridge
    mod.process_payload(jira, {"alerts": [alert("ModelServerDown")]})
    desc = state.issues["SUP-1"]["fields"]["description"]
    block = re.search(r"ALERT_JSON_BEGIN\n\{code:json\}\n(.*?)\n\{code\}\nALERT_JSON_END", desc, re.S).group(1)
    ticket = json.loads(block)["ticket"]
    assert ticket == {"cluster": "c1", "namespace": "inference", "resource": "deployment/model-a-vllm",
                      "signal": "ModelServerDown", "severity": "critical", "runbook_hint": "restart-model-server"}


def test_non_ticket_alerts_ignored(bridge):
    mod, jira, state = bridge
    a = alert("Watchdog")
    del a["labels"]["ticket"]
    r = mod.process_payload(jira, {"alerts": [a]})
    assert r[0]["skipped"] and not state.issues


def test_contract_violation_skipped(bridge):
    """Without a resource label, correlation would merge unrelated faults; refuse instead."""
    mod, jira, state = bridge
    a = alert("ModelServerDown")
    del a["labels"]["resource"]
    r = mod.process_payload(jira, {"alerts": [a]})
    assert "resource" in r[0]["skipped"] and not state.issues


def test_auth_header_user_token_vs_service_account(bridge):
    mod, _, _ = bridge
    assert mod.Jira("u", "bot@x", "t").headers["Authorization"].startswith("Basic ")
    assert mod.Jira("u", "", "ATSTT-scoped").headers["Authorization"] == "Bearer ATSTT-scoped"
