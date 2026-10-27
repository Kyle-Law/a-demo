"""Jira for the executor: the ready queue, tickets with comment authors, comments, labels and the
Done transition. Same auth as agent/alert-bridge and agent/ops-agent (Basic with an email, Bearer
without)."""

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
        self._me: str | None = None

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

    def queue(self, ready_label: str, finished_labels: list[str]) -> list[dict]:
        """Open, diagnosed tickets without a final executor outcome, oldest first."""
        done = ", ".join(f'"{l}"' for l in finished_labels)
        jql = (f'project = "{self.project}" AND labels = "{ready_label}" AND labels not in ({done}) '
               "AND statusCategory != Done ORDER BY created ASC")
        res = self._req("GET", "/rest/api/3/search/jql", query={"jql": jql, "fields": "summary,labels", "maxResults": "20"})
        return res.get("issues", [])

    def get_issue(self, key: str) -> dict:
        return self._req("GET", f"/rest/api/2/issue/{key}", query={"fields": "summary,description,labels,comment"})

    def my_account_id(self) -> str:
        if self._me is None:
            self._me = self._req("GET", "/rest/api/2/myself").get("accountId", "")
        return self._me

    @staticmethod
    def labels(issue: dict) -> set[str]:
        return set(issue.get("fields", {}).get("labels") or [])

    @staticmethod
    def comments(issue: dict) -> list[dict]:
        """Comments oldest first, each with id, body, author accountId and displayName."""
        out = []
        for c in (issue.get("fields", {}).get("comment") or {}).get("comments", []):
            author = c.get("author") or {}
            out.append({"id": int(c.get("id", 0)), "body": c.get("body") or "",
                        "author": author.get("accountId", ""), "author_name": author.get("displayName", "")})
        return sorted(out, key=lambda c: c["id"])

    @staticmethod
    def extract_alerts(issue: dict) -> list[dict]:
        """Every ALERT_JSON block in the description and comments, oldest first."""
        texts = [issue.get("fields", {}).get("description") or ""] + [c["body"] for c in Jira.comments(issue)]
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

    def update_labels(self, key: str, add: list[str] = (), remove: list[str] = ()) -> None:
        ops = [{"add": l} for l in add] + [{"remove": l} for l in remove]
        self._req("PUT", f"/rest/api/2/issue/{key}", {"update": {"labels": ops}})

    def close(self, key: str) -> str:
        """Moves the ticket to the first status in the Done category; returns its name."""
        transitions = self._req("GET", f"/rest/api/2/issue/{key}/transitions").get("transitions", [])
        for t in transitions:
            if ((t.get("to") or {}).get("statusCategory") or {}).get("key") == "done":
                self._req("POST", f"/rest/api/2/issue/{key}/transitions", {"transition": {"id": t["id"]}})
                return t["to"].get("name", "Done")
        raise RuntimeError(f"{key} has no transition into a Done status")
