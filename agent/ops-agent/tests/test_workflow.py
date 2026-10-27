"""Triage loop against a fake Jira, on a ticket written by the real alert-bridge code."""

import importlib.util
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ops_agent.config import Config
from ops_agent.jira import Jira
from ops_agent.llm import LoopResult, ProviderUnavailable
from ops_agent.workflow import run_once

BRIDGE = Path(__file__).resolve().parents[2] / "alert-bridge" / "bridge.py"
FINDINGS = {"summary": "model-b crash-loops on an invalid vLLM flag", "root_cause": "--chaos-invalid-flag",
            "evidence": [{"source": "kubectl", "query": "logs model-b-vllm-x -n inference --previous",
                          "finding": "error: unrecognized arguments: --chaos-invalid-flag"}],
            "confidence": "high", "suggested_runbook": "model-server-down", "next_steps": ["revert the flag"]}


def bridge_description():
    spec = importlib.util.spec_from_file_location("bridge", BRIDGE)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    return bridge.describe_alert({
        "status": "firing", "fingerprint": "f1", "startsAt": "2026-10-27T06:00:00Z",
        "labels": {"alertname": "ModelServerDown", "signal": "ModelServerDown", "cluster": "c1",
                   "namespace": "inference", "resource": "deployment/model-b-vllm", "severity": "critical",
                   "ticket": "true"},
        "annotations": {"summary": "1 replica not available", "runbook_hint": "model-server-down"}})


class FakeJira:
    def __init__(self, issues):
        self.issues = issues  # key -> {"summary", "description", "labels", "comments"}

    def handler(self, method, path, body):
        u = urlparse(path)
        if method == "GET" and u.path == "/rest/api/3/search/jql":
            jql = parse_qs(u.query)["jql"][0]
            want, skip = re.findall(r'labels (?:=|!=) "([^"]+)"', jql)
            hits = [{"key": k} for k, v in self.issues.items() if want in v["labels"] and skip not in v["labels"]]
            return 200, {"issues": hits}
        m = re.match(r"/rest/api/2/issue/([A-Z]+-\d+)(/comment)?$", u.path)
        issue = self.issues[m.group(1)]
        if method == "GET":
            return 200, {"key": m.group(1), "fields": {"summary": issue["summary"], "description": issue["description"],
                                                       "labels": issue["labels"],
                                                       "comment": {"comments": [{"body": c} for c in issue["comments"]]}}}
        if m.group(2):
            issue["comments"].append(body["body"])
            return 201, {}
        issue["labels"] += [op["add"] for op in body["update"]["labels"]]
        return 204, None


class NoTools:
    """The scripted providers below never call tools."""

    @staticmethod
    def dispatch(name, args):
        return "", False


class FixedProvider:
    def __init__(self, name, outcome):
        self.name, self.outcome = name, outcome
        self.prompts = []

    def run(self, system, user, tools, dispatch, finish_tool, max_turns):
        self.prompts.append(user)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def setup(fake_server, issues):
    fj = FakeJira(issues)
    srv = fake_server(fj.handler)
    cfg = Config(jira_api_url=srv.url, jira_email="bot@x", jira_api_token="t", jira_project="SUP")
    return fj, Jira(srv.url, "bot@x", "t", "SUP"), cfg


def test_triages_bridge_ticket_once(fake_server):
    fj, jira, cfg = setup(fake_server, {"SUP-9": {"summary": "[CRITICAL] ModelServerDown: c1/inference/deployment/model-b-vllm",
                                                  "description": bridge_description(), "labels": ["ai-triage"], "comments": []}})
    provider = FixedProvider("intellect-3", LoopResult(final_args=FINDINGS, provider="intellect-3",
                                                       tool_log=[{"tool": "kubectl"}], turns=3))
    assert run_once(jira, [provider], NoTools(), cfg) == {"SUP-9": "triaged"}
    issue = fj.issues["SUP-9"]
    assert {"ai-triaged", "rca-by-intellect-3"} <= set(issue["labels"])
    assert "unrecognized arguments" in issue["comments"][0] and "high confidence" in issue["comments"][0]
    # the model saw the contract the bridge wrote
    assert '"resource": "deployment/model-b-vllm"' in provider.prompts[0]
    # second pass: already triaged, nothing happens
    assert run_once(jira, [provider], NoTools(), cfg) == {}


def test_all_providers_down_comments_once_and_stops(fake_server):
    fj, jira, cfg = setup(fake_server, {"SUP-10": {"summary": "x", "description": bridge_description(),
                                                   "labels": ["ai-triage"], "comments": []}})
    assert run_once(jira, [FixedProvider("a", ProviderUnavailable("down"))], NoTools(), cfg) == {"SUP-10": "failed"}
    issue = fj.issues["SUP-10"]
    assert {"ai-triaged", "ai-triage-failed"} <= set(issue["labels"]) and "could not complete" in issue["comments"][0]
    assert run_once(jira, [FixedProvider("a", ProviderUnavailable("down"))], NoTools(), cfg) == {}


def test_non_bridge_ticket_skipped(fake_server):
    fj, jira, cfg = setup(fake_server, {"SUP-11": {"summary": "hand-written", "description": "please help",
                                                   "labels": ["ai-triage"], "comments": []}})
    assert run_once(jira, [], NoTools(), cfg) == {"SUP-11": "skipped"}
