"""The executor's state machine end to end, with in-memory Jira, Prometheus, Vault and clusters."""

import json
import re
from types import SimpleNamespace

import pytest
from conftest import FIXTURE, repo_tarball

from runbook_executor import workflow as wf
from runbook_executor.actions import KubeError
from runbook_executor.runbooks import RunbookSource

SHA, SHA2 = "a" * 40, "b" * 40
ME, ALICE, MALLORY = "acct-executor", "acct-alice", "acct-mallory"


class FakeJira:
    def __init__(self, alert):
        self.labels = {"ai-triage", "ai-triaged"}
        self.comments = []  # (id, author, name, body)
        self.closed = None
        self.description = ("ALERT_JSON_BEGIN\n{code:json}\n" + json.dumps(alert) + "\n{code}\nALERT_JSON_END") if alert else "hi"

    def add_comment(self, author, body):
        self.comments.append({"id": len(self.comments) + 1, "author": {"accountId": author, "displayName": author},
                              "body": body})

    # the Jira interface the workflow uses
    def queue(self, ready, finished):
        return [{"key": "SUP-1"}] if ready in self.labels and not self.labels & set(finished) else []

    def get_issue(self, key):
        return {"key": key, "fields": {"description": self.description, "labels": sorted(self.labels),
                                       "comment": {"comments": list(self.comments)}}}

    def my_account_id(self):
        return ME

    def comment(self, key, body):
        self.add_comment(ME, body)

    def update_labels(self, key, add=(), remove=()):
        self.labels |= set(add)
        self.labels -= set(remove)

    def close(self, key):
        self.closed = "Done"
        return "Done"


class FakeProm:
    """series(query) answers from {regex: [series]}; the first matching regex wins."""

    def __init__(self, answers):
        self.answers = answers
        self.queries = []

    def series(self, query):
        self.queries.append(query)
        for pattern, result in self.answers.items():
            if re.search(pattern, query):
                return result() if callable(result) else result
        return []


class FakeVault:
    def __init__(self):
        self.calls = []

    def login(self):
        self.calls.append("login")
        return "vt"

    def kube_token(self, token, cluster, role, namespace):
        self.calls.append(f"creds {cluster}/{role}/{namespace}")
        return {"token": f"k8s-{cluster}", "service_account": f"{namespace}/runbook-{role}-x1", "lease_id": "l", "ttl": 600}

    def revoke(self, token):
        self.calls.append("revoke")


class FakeKube:
    def __init__(self, log, routes):
        self.log, self.routes = log, routes

    def request(self, method, path, body=None, query=None, content_type="application/json"):
        self.log.append((method, path, body))
        result = self.routes.get((method, path), {})
        if isinstance(result, Exception):
            raise result
        return result

    def merge_patch(self, path, body):
        return self.request("PATCH", path, body)


class FakeClusters:
    def __init__(self, routes=None):
        self.log, self.routes, self.tokens = [], routes or {}, []

    def client(self, cluster, token):
        self.tokens.append((cluster, token))
        return FakeKube(self.log, self.routes)


def alert(signal="PodUnschedulable", resource="pod/chaos-gpu-claim", cluster="c1"):
    return {"alertname": signal, "ticket": {"cluster": cluster, "namespace": "inference", "resource": resource,
                                            "signal": signal, "severity": "warning", "runbook_hint": None}}


def make(alert_payload=alert(), prom=None, routes=None, approvers=(), overrides=None):
    tarballs = {SHA: repo_tarball(SHA), SHA2: repo_tarball(SHA2, overrides)}
    head = {"sha": SHA}

    def get(url, accept):
        return head["sha"].encode() if "api.github.com" in url else tarballs[url.rsplit("/", 1)[1]]

    jira, vault, clusters = FakeJira(alert_payload), FakeVault(), FakeClusters(routes)
    cfg = SimpleNamespace(ready_label="ai-triaged", approvers=list(approvers), verify_interval_seconds=1)
    source = RunbookSource("Kyle-Law/a-demo-runbooks", "main", get)
    source.refresh()
    now = [0.0]  # verify waits on a fake clock that sleep advances
    ex = wf.Executor(jira, source, prom or FakeProm({}), vault, clusters, cfg,
                     sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
    return SimpleNamespace(ex=ex, jira=jira, vault=vault, clusters=clusters, head=head, source=source)


POD = "/api/v1/namespaces/inference/pods/chaos-gpu-claim"


def unschedulable(state):
    """Prometheus that reports the pod unschedulable while state["stuck"] is true."""
    return FakeProm({r"kube_pod_status_unschedulable": lambda: [{"metric": {}, "value": [0, "1"]}] if state["stuck"] else []})


def test_no_runbook_escalates_to_a_human():
    t = make(alert("NodeNotReady", "node/c1-a100"))
    assert t.ex.run_once() == {"SUP-1": "none"}
    assert "runbook-none" in t.jira.labels and "No runbook to run" in t.jira.comments[-1]["body"]
    assert t.vault.calls == []
    assert t.ex.run_once() == {}  # finished tickets leave the queue


def test_fault_already_gone_runs_nothing():
    t = make(prom=unschedulable({"stuck": False}))
    assert t.ex.process("SUP-1") == "not-needed"
    assert "runbook-not-needed" in t.jira.labels and t.vault.calls == []


def test_plan_waits_for_approval_and_mints_nothing():
    t = make(prom=unschedulable({"stuck": True}))
    assert t.ex.process("SUP-1") == "proposed"
    body = t.jira.comments[-1]["body"]
    assert "Runbook proposed: pod-unschedulable" in body and "/approve" in body
    assert json.loads(wf.PLAN_RE.search(body).group(1)) == {"runbook": "pod-unschedulable", "sha": SHA}
    assert t.ex.process("SUP-1") == "waiting"
    t.jira.add_comment(ME, "/approve")  # the executor's own account can't approve
    assert t.ex.process("SUP-1") == "waiting"
    assert t.vault.calls == [] and t.clusters.log == []


def test_approved_runbook_runs_verifies_and_closes():
    state = {"stuck": True}
    routes = {("GET", POD): {"metadata": {"uid": "u-1"}}}
    t = make(prom=unschedulable(state), routes=routes)
    t.ex.process("SUP-1")
    t.jira.add_comment(ALICE, "/approve")
    real_run = wf.actions.run

    def run_and_clear(step, kube, notify):
        out = real_run(step, kube, notify)
        state["stuck"] = False  # the fault clears once the pod is gone
        return out
    wf.actions.run = run_and_clear
    try:
        assert t.ex.process("SUP-1") == "succeeded"
    finally:
        wf.actions.run = real_run
    assert ("DELETE", POD, {"preconditions": {"uid": "u-1"}}) in t.clusters.log
    assert t.vault.calls == ["login", "creds c1/inference-pod-janitor/inference", "revoke"]
    assert t.clusters.tokens == [("c1", "k8s-c1")]
    assert t.jira.labels >= {"runbook-succeeded"} and not t.jira.labels & {"runbook-running", "runbook-proposed"}
    assert t.jira.closed == "Done"
    report = t.jira.comments[-1]["body"]
    assert "Fixed by runbook" in report and "acct-alice" in report


def test_reject_closes_the_proposal():
    t = make(prom=unschedulable({"stuck": True}))
    t.ex.process("SUP-1")
    t.jira.add_comment(ALICE, "/reject not now")
    assert t.ex.process("SUP-1") == "rejected"
    assert "runbook-rejected" in t.jira.labels and t.vault.calls == []


def test_only_listed_approvers_count():
    t = make(prom=unschedulable({"stuck": True}), approvers=[ALICE],
             routes={("GET", POD): {"metadata": {"uid": "u-1"}}})
    t.ex.process("SUP-1")
    t.jira.add_comment(MALLORY, "/approve")
    assert t.ex.process("SUP-1") == "waiting"
    t.jira.add_comment(ALICE, "/approve")
    assert t.ex.process("SUP-1") in {"succeeded", "failed"}  # it ran


def test_refused_step_rolls_back_and_leaves_the_ticket_open():
    owned = {"metadata": {"uid": "u-1", "ownerReferences": [{"kind": "ReplicaSet", "name": "rs"}]}}
    t = make(prom=unschedulable({"stuck": True}), routes={("GET", POD): owned})
    t.ex.process("SUP-1")
    t.jira.add_comment(ALICE, "/approve")
    assert t.ex.process("SUP-1") == "failed"
    report = t.jira.comments[-1]["body"]
    assert "rollback ran" in report and "refused" in report
    assert not any(m == "DELETE" for m, _, _ in t.clusters.log)
    assert "runbook-failed" in t.jira.labels and t.jira.closed is None
    # The step's token is revoked after the steps; the rollback (notify only) needs none, so no login.
    assert t.vault.calls == ["login", "creds c1/inference-pod-janitor/inference", "revoke"]


def test_failed_verify_rolls_back():
    t = make(prom=unschedulable({"stuck": True}), routes={("GET", POD): {"metadata": {"uid": "u-1"}}})
    t.ex.process("SUP-1")
    t.jira.add_comment(ALICE, "/approve")
    assert t.ex.process("SUP-1") == "failed"  # the pod never stops being unschedulable
    assert any("still unschedulable" in c["body"] for c in t.jira.comments)  # the rollback's notify


def test_kube_error_fails_the_step():
    t = make(prom=unschedulable({"stuck": True}), routes={("GET", POD): KubeError(403, "forbidden")})
    t.ex.process("SUP-1")
    t.jira.add_comment(ALICE, "/approve")
    assert t.ex.process("SUP-1") == "failed"
    assert "HTTP 403: forbidden" in t.jira.comments[-1]["body"]


def test_approval_runs_the_approved_commit_not_the_new_head():
    text = (FIXTURE / "runbooks/pod-unschedulable.yaml").read_text()
    changed = text.replace("action: k8s.delete_unowned_pod", "action: notify\n    message: changed").replace(
        '    namespace: "{{namespace}}"\n    name: "{{name}}"\n', "")
    t = make(prom=unschedulable({"stuck": True}), routes={("GET", POD): {"metadata": {"uid": "u-1"}}},
             overrides={"runbooks/pod-unschedulable.yaml": changed})
    t.ex.process("SUP-1")  # planned at SHA
    t.head["sha"] = SHA2
    t.source.refresh()
    assert t.source.head.runbooks["pod-unschedulable"].doc["steps"][0]["action"] == "notify"
    t.jira.add_comment(ALICE, "/approve")
    t.ex.process("SUP-1")
    assert any(m == "DELETE" for m, _, _ in t.clusters.log)  # SHA's step, not SHA2's


def test_restart_mid_run_never_runs_twice():
    t = make(prom=unschedulable({"stuck": True}))
    t.jira.labels.add("runbook-running")
    assert t.ex.process("SUP-1") == "interrupted"
    assert "runbook-failed" in t.jira.labels and "runbook-running" not in t.jira.labels
    assert t.vault.calls == []


@pytest.mark.parametrize("payload", [None, alert(resource="pod/Bad Name")])
def test_tickets_without_a_usable_alert_get_no_runbook(payload):
    t = make(payload)
    assert t.ex.process("SUP-1") == "none"
    assert t.vault.calls == []
