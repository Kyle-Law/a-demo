"""Alertmanager -> Jira alert bridge (B13).

Receives Alertmanager webhooks from the hub on C3 and turns them into Jira tickets:

* FIRING alert, no open ticket for that resource  -> create a ticket
* FIRING alert, open ticket for the same resource -> add a "correlated alert"
  comment + label to the existing ticket (one fault, one ticket)
* RESOLVED alert                                  -> comment on the ticket

Only alerts labelled ticket="true" are processed (the default kube-prometheus-stack
rules never are). Ticketed alerts must carry the alert -> ticket contract from
CLAUDE.md: cluster, namespace, resource, signal, severity, and optionally a
runbook_hint annotation. A resource is identified by cluster/namespace/resource, so
alerts from different clusters never merge.

Tickets get the JIRA_TRIAGE_LABEL label (default "ai-triage"), which is what the
ops agent (B14) polls for. The ticket fields and the full alert are embedded in
the description inside a {code:json} block between ALERT_JSON markers so the
agent can parse them.

Ported from Kyle-Law/ai-sre-demo (08-jira-integration), which took Grafana webhooks.
Stdlib only (runs on python:3.13-slim with the file mounted from a ConfigMap).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
# Scoped tokens (Jira service accounts) only work through the API gateway:
# https://api.atlassian.com/ex/jira/<cloudId>. Classic tokens use the site itself.
JIRA_API_URL = (os.environ.get("JIRA_API_URL") or JIRA_BASE_URL).rstrip("/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "")
JIRA_ISSUE_TYPE = os.environ.get("JIRA_ISSUE_TYPE", "Task")
TRIAGE_LABEL = os.environ.get("JIRA_TRIAGE_LABEL", "ai-triage")
LISTEN_PORT = int(os.environ.get("PORT", "8080"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# The alert -> ticket contract (CLAUDE.md "Contracts between blocks").
CONTRACT_LABELS = ("cluster", "namespace", "resource", "signal", "severity")

logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}')
log = logging.getLogger("alert-bridge")

# Alertmanager may deliver several notification groups at once; serialise ticket
# creation so two groups for the same resource never create two tickets.
_lock = threading.Lock()


def jlog(level: int, msg: str, **fields) -> None:
    log.log(level, json.dumps({"event": msg, **fields}))


def label_safe(value: str) -> str:
    """Jira labels cannot contain spaces; keep them short and predictable."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")[:120] or "unknown"


class Jira:
    def __init__(self, base_url: str, email: str, token: str):
        self.base_url = base_url
        # With an email: Basic auth. Without (service account scoped token): Bearer.
        auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode() if email else f"Bearer {token}"
        self.headers = {
            "Authorization": auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body: dict | None = None, query: dict | None = None):
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(4):
            req = urllib.request.Request(url, data=data, method=method, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(int(exc.headers.get("Retry-After", "2")))
                    continue
                detail = exc.read().decode(errors="replace")[:500]
                raise RuntimeError(f"Jira {method} {path} -> {exc.code}: {detail}") from exc
        raise RuntimeError(f"Jira {method} {path}: retries exhausted")

    def search(self, jql: str) -> list[dict]:
        res = self._request(
            "GET",
            "/rest/api/3/search/jql",
            query={"jql": jql, "fields": "summary,labels,status", "maxResults": "20"},
        )
        return res.get("issues", [])

    def create_issue(self, summary: str, description: str, labels: list[str]) -> str:
        body = {
            "fields": {
                "project": {"key": JIRA_PROJECT_KEY},
                "issuetype": {"name": JIRA_ISSUE_TYPE},
                "summary": summary[:250],
                "description": description,
                "labels": labels,
            }
        }
        if DRY_RUN:
            jlog(logging.INFO, "dry_run_create_issue", body=body)
            return "DRY-1"
        return self._request("POST", "/rest/api/2/issue", body)["key"]

    def comment(self, key: str, text: str) -> None:
        if DRY_RUN:
            jlog(logging.INFO, "dry_run_comment", key=key, text=text)
            return
        self._request("POST", f"/rest/api/2/issue/{key}/comment", {"body": text[:30000]})

    def add_labels(self, key: str, labels: list[str]) -> None:
        if DRY_RUN:
            jlog(logging.INFO, "dry_run_add_labels", key=key, labels=labels)
            return
        self._request("PUT", f"/rest/api/2/issue/{key}", {"update": {"labels": [{"add": l} for l in labels]}})


def open_issues_with_label(jira: Jira, label: str) -> list[dict]:
    jql = f'project = "{JIRA_PROJECT_KEY}" AND labels = "{label}" AND statusCategory != Done ORDER BY created ASC'
    return jira.search(jql)


def ticket_fields(alert: dict) -> dict:
    """The alert -> ticket contract, as the agent reads it."""
    labels = alert.get("labels", {})
    fields = {name: labels.get(name) for name in CONTRACT_LABELS}
    fields["runbook_hint"] = alert.get("annotations", {}).get("runbook_hint")
    return fields


def describe_alert(alert: dict) -> str:
    labels = alert.get("labels", {})
    ann = alert.get("annotations", {})
    ticket = ticket_fields(alert)
    alert_json = {
        "alertname": labels.get("alertname"),
        "status": alert.get("status"),
        "fingerprint": alert.get("fingerprint"),
        "startsAt": alert.get("startsAt"),
        "ticket": ticket,
        "labels": labels,
        "annotations": ann,
        "generatorURL": alert.get("generatorURL"),
    }
    lines = [
        f"h3. {ticket['signal']} is FIRING",
        "",
        f"*Summary:* {ann.get('summary', '-')}",
        f"*Description:* {ann.get('description', '-')}",
        f"*Severity:* {ticket['severity']}",
        f"*Cluster / namespace / resource:* {ticket['cluster']} / {ticket['namespace']} / {ticket['resource']}",
        f"*Runbook hint:* {ticket['runbook_hint'] or '-'}",
        f"*Started at (UTC):* {alert.get('startsAt', '-')}",
    ]
    if alert.get("generatorURL"):
        lines.append(f"*Prometheus:* [Open the alert expression|{alert['generatorURL']}]")
    lines += [
        "",
        "h4. Alert payload (machine readable)",
        "ALERT_JSON_BEGIN",
        "{code:json}",
        json.dumps(alert_json, indent=2),
        "{code}",
        "ALERT_JSON_END",
        "",
        f"_Created automatically by alert-bridge. The label {{{{{TRIAGE_LABEL}}}}} hands this "
        "ticket to the ops agent, which has read-only cluster access and runs only reviewed "
        "runbooks, with short-lived credentials._",
    ]
    return "\n".join(lines)


def handle_alert(jira: Jira, alert: dict) -> dict:
    labels = alert.get("labels", {})
    if labels.get("ticket") != "true":
        return {"skipped": "not ticket=true"}
    missing = [name for name in CONTRACT_LABELS if not labels.get(name)]
    if missing:
        # An "unknown" resource would correlate unrelated faults into one ticket, so fix the
        # alert rule instead of guessing.
        jlog(logging.ERROR, "contract_violation", missing=missing, labels=labels)
        return {"skipped": f"missing contract labels: {', '.join(missing)}"}

    ticket = ticket_fields(alert)
    alertname = labels.get("alertname", ticket["signal"])
    where = f"{ticket['cluster']}/{ticket['namespace']}/{ticket['resource']}"
    fingerprint = alert.get("fingerprint") or label_safe(f"{alertname}-{where}")
    fp_label = label_safe(f"fp-{fingerprint}")
    res_label = label_safe(f"res-{ticket['cluster']}-{ticket['namespace']}-{ticket['resource']}")
    signal_label = label_safe(f"signal-{ticket['signal']}")
    status = alert.get("status", "firing")

    if status == "firing":
        if open_issues_with_label(jira, fp_label):
            return {"action": "noop", "reason": "ticket already open for fingerprint"}
        related = open_issues_with_label(jira, res_label)
        if related:
            key = related[0]["key"]
            jira.add_labels(key, [fp_label, signal_label])
            jira.comment(
                key,
                f"(!) *Correlated alert firing:* {ticket['signal']} on {where} "
                f"since {alert.get('startsAt', '-')}\n"
                f"{alert.get('annotations', {}).get('summary', '')}\n"
                "ALERT_JSON_BEGIN\n{code:json}\n"
                + json.dumps({"alertname": alertname, "ticket": ticket, "labels": labels,
                              "startsAt": alert.get("startsAt")}, indent=2)
                + "\n{code}\nALERT_JSON_END",
            )
            return {"action": "correlated", "key": key}
        summary = f"[{ticket['severity'].upper()}] {ticket['signal']}: {where}"
        key = jira.create_issue(
            summary,
            describe_alert(alert),
            [TRIAGE_LABEL, "incident", fp_label, res_label, signal_label,
             label_safe(f"cluster-{ticket['cluster']}")],
        )
        return {"action": "created", "key": key}

    # resolved
    issues = open_issues_with_label(jira, fp_label)
    for issue in issues:
        jira.comment(
            issue["key"],
            f"(/) *Alertmanager reports {ticket['signal']} RESOLVED* for {where} "
            f"at {alert.get('endsAt', '-')} (UTC).",
        )
    return {"action": "resolved", "keys": [i["key"] for i in issues]}


def process_payload(jira: Jira, payload: dict) -> list[dict]:
    results = []
    with _lock:
        for alert in payload.get("alerts", []):
            try:
                res = handle_alert(jira, alert)
            except Exception as exc:  # keep processing the other alerts
                res = {"error": str(exc)}
                jlog(logging.ERROR, "alert_failed", error=str(exc), labels=alert.get("labels"))
            res["alertname"] = alert.get("labels", {}).get("alertname")
            res["status"] = alert.get("status")
            jlog(logging.INFO, "alert_processed", **res)
            results.append(res)
    return results


class Handler(BaseHTTPRequestHandler):
    jira: Jira | None = None

    def log_message(self, fmt, *args):  # silence default access log
        pass

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/alertmanager":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                self._send(413, {"error": "payload too large"})
                return
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "invalid json"})
            return
        results = process_payload(self.jira, payload)
        # Return 200 even on partial failure so Alertmanager does not retry-storm;
        # failures are logged and the next repeat_interval re-sends.
        self._send(200, {"results": results})


def main() -> None:
    missing = [n for n in ("JIRA_API_TOKEN", "JIRA_PROJECT_KEY") if not os.environ.get(n)]
    if not JIRA_API_URL:
        missing.append("JIRA_BASE_URL or JIRA_API_URL")
    if not JIRA_EMAIL and not os.environ.get("JIRA_API_URL"):
        missing.append("JIRA_EMAIL (or JIRA_API_URL with a scoped token)")
    if missing and not DRY_RUN:
        raise SystemExit(f"missing required env vars: {', '.join(missing)}")
    Handler.jira = Jira(JIRA_API_URL, JIRA_EMAIL, JIRA_API_TOKEN)
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    jlog(logging.INFO, "listening", port=LISTEN_PORT, project=JIRA_PROJECT_KEY, dry_run=DRY_RUN)
    server.serve_forever()


if __name__ == "__main__":
    main()
