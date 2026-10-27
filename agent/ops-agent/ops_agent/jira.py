"""Jira for the agent: read the triage queue and tickets, post findings, add labels.

Ported from ai-sre-demo (11-agent/jira.py), trimmed to what a read-only triage agent needs. Same
service-account support as agent/alert-bridge (Basic with an email, Bearer without)."""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request

# The block agent/alert-bridge writes into descriptions and correlation comments.
ALERT_JSON_RE = re.compile(r"ALERT_JSON_BEGIN\s*\{code(?::json)?\}(.*?)\{code\}\s*ALERT_JSON_END", re.S)


class Jira:
    def __init__(self, api_url: str, email: str, token: str, project: str):
        self.api_url = api_url.rstrip("/")
        self.project = project
        auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode() if email else f"Bearer {token}"
        self.headers = {"Authorization": auth, "Accept": "application/json", "Content-Type": "application/json"}

    def _req(self, method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
        url = f"{self.api_url}{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Jira {method} {path} -> {exc.code}: {exc.read().decode(errors='replace')[:300]}") from exc

    def triage_queue(self, label: str, done_label: str) -> list[dict]:
        """Open tickets handed to the agent that it hasn't triaged yet, oldest first."""
        jql = (f'project = "{self.project}" AND labels = "{label}" AND labels != "{done_label}" '
               "AND statusCategory != Done ORDER BY created ASC")
        res = self._req("GET", "/rest/api/3/search/jql", query={"jql": jql, "fields": "summary,labels", "maxResults": "20"})
        return res.get("issues", [])

    def get_issue(self, key: str) -> dict:
        return self._req("GET", f"/rest/api/2/issue/{key}", query={"fields": "summary,description,labels,comment"})

    @staticmethod
    def extract_alerts(issue: dict) -> list[dict]:
        """Every ALERT_JSON block in the description and comments, oldest first."""
        fields = issue.get("fields", {})
        texts = [fields.get("description") or ""]
        texts += [c.get("body") or "" for c in (fields.get("comment") or {}).get("comments", [])]
        alerts = []
        for text in texts:
            for block in ALERT_JSON_RE.findall(text):
                try:
                    alerts.append(json.loads(block))
                except json.JSONDecodeError:
                    continue
        return alerts

    def comment(self, key: str, body: str) -> None:
        self._req("POST", f"/rest/api/2/issue/{key}/comment", {"body": body[:30000]})

    def add_labels(self, key: str, labels: list[str]) -> None:
        self._req("PUT", f"/rest/api/2/issue/{key}", {"update": {"labels": [{"add": l} for l in labels]}})
