"""The runbook action vocabulary (runbooks repo schema), implemented against the Kubernetes API.

Each action runs with a token Vault minted for the runbook's required_role in one namespace; the
executor itself holds no Kubernetes permissions. Argo CD actions go to the hub (C3), where the
Applications live; k8s.* actions go to the ticket's cluster."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from .runbooks import SAFE_VALUE

HUB_CLUSTER, ARGOCD_NAMESPACE = "c3", "argocd"
APPLICATIONS = "/apis/argoproj.io/v1alpha1/namespaces/argocd/applications"


class ActionError(RuntimeError):
    """The action did not do what it promised; the runbook fails and rolls back."""


class KubeError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class Clusters:
    """API endpoints and CAs of every cluster: endpoints.json ({"c1": "https://..."}) and <cluster>.crt."""

    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.endpoints = json.loads((self.dir / "endpoints.json").read_text())

    def client(self, cluster: str, token: str) -> "Kube":
        if cluster not in self.endpoints:
            raise ActionError(f"no API endpoint configured for cluster {cluster}")
        return Kube(self.endpoints[cluster], str(self.dir / f"{cluster}.crt"), token)


class Kube:
    def __init__(self, server: str, ca_file: str | None, token: str):
        self.server, self.token = server.rstrip("/"), token
        self.ctx = ssl.create_default_context(cafile=ca_file)  # ca_file None: system CAs (tests)

    def request(self, method: str, path: str, body: dict | None = None, query: dict | None = None,
                content_type: str = "application/json") -> dict:
        url = f"{self.server}{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": content_type,
                                              "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30, context=self.ctx) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("message", exc.reason)
            except Exception:
                message = exc.reason
            raise KubeError(exc.code, message) from exc

    def merge_patch(self, path: str, body: dict) -> dict:
        return self.request("PATCH", path, body, content_type="application/merge-patch+json")


def target(step: dict, ctx: dict) -> tuple[str, str] | None:
    """(cluster, namespace) the step's token must be minted for; None for notify."""
    action = step["action"]
    if action.startswith("argocd."):
        return HUB_CLUSTER, ARGOCD_NAMESPACE
    if action.startswith("k8s."):
        return ctx["cluster"], step["namespace"]
    return None


def _name(value: str, what: str) -> str:
    if not SAFE_VALUE.match(value):
        raise ActionError(f"{what} {value!r} is not a plain Kubernetes name")
    return value


def run(step: dict, kube: Kube | None, notify: Callable[[str], None]) -> str:
    """Runs one rendered step; returns what it did. Raises ActionError or KubeError on failure."""
    action = step["action"]
    if action == "notify":
        notify(step["message"])
        return "commented on the ticket"
    assert kube is not None

    if action == "argocd.resume_autosync":
        app = _name(step["app"], "Application")
        kube.merge_patch(f"{APPLICATIONS}/{app}", {"spec": {"syncPolicy": {"automated": {"prune": True, "selfHeal": True}}}})
        return f"auto-sync (prune, self-heal) is on for Application {app}"

    if action == "argocd.sync":
        app = _name(step["app"], "Application")
        current = kube.request("GET", f"{APPLICATIONS}/{app}")
        if current.get("operation"):
            return f"Application {app} already has a sync in progress"
        kube.merge_patch(f"{APPLICATIONS}/{app}", {"operation": {
            "initiatedBy": {"username": "runbook-executor"}, "sync": {"prune": True}}})
        return f"requested a sync of Application {app}"

    if action == "k8s.delete_unowned_pod":
        ns, name = _name(step["namespace"], "namespace"), _name(step["name"], "pod")
        path = f"/api/v1/namespaces/{ns}/pods/{name}"
        try:
            pod = kube.request("GET", path)
        except KubeError as exc:
            if exc.status == 404:
                return f"pod {ns}/{name} is already gone"
            raise
        owners = pod["metadata"].get("ownerReferences") or []
        if owners:
            raise ActionError(f"refused: pod {ns}/{name} is owned by "
                              f"{', '.join(o['kind'] + '/' + o['name'] for o in owners)}; its controller would recreate it")
        # The uid precondition makes sure this deletes the pod that was inspected, not a namesake.
        kube.request("DELETE", path, {"preconditions": {"uid": pod["metadata"]["uid"]}})
        return f"deleted unowned pod {ns}/{name}"

    if action == "k8s.delete_jobs":
        ns = _name(step["namespace"], "namespace")
        path = f"/apis/batch/v1/namespaces/{ns}/jobs"
        jobs = kube.request("GET", path, query={"labelSelector": step["label_selector"]}).get("items", [])
        for job in jobs:
            kube.request("DELETE", f"{path}/{job['metadata']['name']}", {"propagationPolicy": "Background"})
        names = ", ".join(j["metadata"]["name"] for j in jobs) or "none"
        return f"deleted {len(jobs)} Job(s) matching {step['label_selector']} in {ns}: {names}"

    if action == "k8s.rollout_restart":
        ns, name = _name(step["namespace"], "namespace"), _name(step["deployment"], "Deployment")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        kube.merge_patch(f"/apis/apps/v1/namespaces/{ns}/deployments/{name}",
                         {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}}}})
        return f"restarted Deployment {ns}/{name}"

    raise ActionError(f"unknown action {action}")
