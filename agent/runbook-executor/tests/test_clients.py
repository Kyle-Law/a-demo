"""The HTTP clients against a fake server: Kubernetes actions, Vault, Jira."""

import pytest

from runbook_executor import actions
from runbook_executor.actions import ActionError, Kube
from runbook_executor.jira import Jira
from runbook_executor.vault import RunCredentials, Vault, VaultError

POD = "/api/v1/namespaces/inference/pods/chaos-gpu-claim"


def kube_for(fake_server, routes):
    """routes: {(method, path-without-query): (status, body)}"""
    server = fake_server(lambda m, p, b: routes.get((m, p.split("?")[0]), (404, {"message": "not found"})))
    return Kube(server.url, None, "minted-token"), server


def test_delete_unowned_pod_deletes_with_uid_precondition(fake_server):
    kube, server = kube_for(fake_server, {("GET", POD): (200, {"metadata": {"uid": "u-1"}}), ("DELETE", POD): (200, {})})
    out = actions.run({"action": "k8s.delete_unowned_pod", "namespace": "inference", "name": "chaos-gpu-claim"}, kube, None)
    assert out == "deleted unowned pod inference/chaos-gpu-claim"
    delete = server.requests[1]
    assert delete["body"] == {"preconditions": {"uid": "u-1"}}
    assert delete["headers"]["authorization"] == "Bearer minted-token"


def test_delete_unowned_pod_refuses_an_owned_pod(fake_server):
    owned = {"metadata": {"uid": "u-1", "ownerReferences": [{"kind": "ReplicaSet", "name": "model-b-vllm-5d9"}]}}
    kube, server = kube_for(fake_server, {("GET", POD): (200, owned)})
    with pytest.raises(ActionError, match="owned by ReplicaSet/model-b-vllm-5d9"):
        actions.run({"action": "k8s.delete_unowned_pod", "namespace": "inference", "name": "chaos-gpu-claim"}, kube, None)
    assert [r["method"] for r in server.requests] == ["GET"]


def test_delete_unowned_pod_that_is_gone_succeeds(fake_server):
    kube, _ = kube_for(fake_server, {})
    out = actions.run({"action": "k8s.delete_unowned_pod", "namespace": "inference", "name": "chaos-gpu-claim"}, kube, None)
    assert "already gone" in out


def test_delete_jobs_by_selector(fake_server):
    jobs = "/apis/batch/v1/namespaces/inference/jobs"
    kube, server = kube_for(fake_server, {
        ("GET", jobs): (200, {"items": [{"metadata": {"name": "chaos-queue-backlog"}}]}),
        ("DELETE", f"{jobs}/chaos-queue-backlog"): (200, {})})
    out = actions.run({"action": "k8s.delete_jobs", "namespace": "inference", "label_selector": "load-class=batch"}, kube, None)
    assert "deleted 1 Job(s)" in out
    assert "labelSelector=load-class%3Dbatch" in server.requests[0]["path"]
    assert server.requests[1]["body"] == {"propagationPolicy": "Background"}


def test_argocd_actions_patch_only_sync_fields(fake_server):
    app = f"{actions.APPLICATIONS}/models-c1"
    kube, server = kube_for(fake_server, {("PATCH", app): (200, {}), ("GET", app): (200, {"spec": {}})})
    actions.run({"action": "argocd.resume_autosync", "app": "models-c1"}, kube, None)
    actions.run({"action": "argocd.sync", "app": "models-c1"}, kube, None)
    patches = [r for r in server.requests if r["method"] == "PATCH"]
    assert patches[0]["headers"]["content-type"] == "application/merge-patch+json"
    assert patches[0]["body"] == {"spec": {"syncPolicy": {"automated": {"prune": True, "selfHeal": True}}}}
    assert set(patches[1]["body"]) == {"operation"}  # runbook-guard.yaml rejects anything else under spec


def test_argocd_sync_skips_when_one_is_running(fake_server):
    app = f"{actions.APPLICATIONS}/models-c1"
    kube, server = kube_for(fake_server, {("GET", app): (200, {"operation": {"sync": {}}})})
    assert "already has a sync" in actions.run({"action": "argocd.sync", "app": "models-c1"}, kube, None)
    assert all(r["method"] == "GET" for r in server.requests)


def test_actions_refuse_names_that_are_not_plain():
    with pytest.raises(ActionError):
        actions.run({"action": "argocd.sync", "app": "../../secrets"}, object(), None)


def test_targets():
    ctx = {"cluster": "c1"}
    assert actions.target({"action": "argocd.sync", "app": "models-c1"}, ctx) == ("c3", "argocd")
    assert actions.target({"action": "k8s.delete_jobs", "namespace": "inference", "label_selector": "a=b"}, ctx) == ("c1", "inference")
    assert actions.target({"action": "notify", "message": "hi"}, ctx) is None


def vault_server(fake_server, fail_revoke=False):
    def handler(method, path, body):
        if path == "/v1/auth/kubernetes/login":
            return (200, {"auth": {"client_token": "vt-1"}}) if body["jwt"] == "sa-jwt" else (403, {"errors": ["permission denied"]})
        if path.startswith("/v1/kubernetes-"):
            return 200, {"lease_id": "lease-1", "lease_duration": 600, "data": {
                "service_account_token": f"k8s-{path.split('/')[2]}-{body['kubernetes_namespace']}",
                "service_account_name": "runbook-inference-pod-janitor-abc", "service_account_namespace": body["kubernetes_namespace"]}}
        if path == "/v1/auth/token/revoke-self":
            return (500, {"errors": ["sealed"]}) if fail_revoke else (204, None)
        return 404, {"errors": ["no handler"]}
    return fake_server(handler)


def test_run_credentials_mint_once_per_target_and_revoke(fake_server, tmp_path):
    jwt = tmp_path / "token"
    jwt.write_text("sa-jwt\n")
    server = vault_server(fake_server)
    with RunCredentials(Vault(server.url, "runbook-executor", str(jwt)), "inference-pod-janitor") as creds:
        assert creds.token("c1", "inference") == "k8s-kubernetes-c1-inference"
        creds.token("c1", "inference")
    paths = [r["path"] for r in server.requests]
    assert paths == ["/v1/auth/kubernetes/login", "/v1/kubernetes-c1/creds/inference-pod-janitor", "/v1/auth/token/revoke-self"]
    assert server.requests[1]["headers"]["x-vault-token"] == "vt-1"
    assert creds.issued == ["c1: inference/runbook-inference-pod-janitor-abc (600s)"]


def test_revoke_failure_does_not_raise(fake_server, tmp_path):
    jwt = tmp_path / "token"
    jwt.write_text("sa-jwt")
    with RunCredentials(Vault(vault_server(fake_server, fail_revoke=True).url, "r", str(jwt)), "x") as creds:
        creds.token("c1", "inference")


def test_vault_errors_carry_vaults_message(fake_server, tmp_path):
    jwt = tmp_path / "token"
    jwt.write_text("wrong")
    with pytest.raises(VaultError, match="403: permission denied"):
        Vault(vault_server(fake_server).url, "r", str(jwt)).login()


def test_jira_close_uses_a_done_transition(fake_server):
    def handler(method, path, body):
        if path.endswith("/transitions") and method == "GET":
            return 200, {"transitions": [{"id": "11", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
                                         {"id": "31", "to": {"name": "Done", "statusCategory": {"key": "done"}}}]}
        return 204, None
    server = fake_server(handler)
    assert Jira(server.url, "", "t", "SUP").close("SUP-1") == "Done"
    assert server.requests[-1]["body"] == {"transition": {"id": "31"}}


def test_jira_queue_excludes_finished_tickets(fake_server):
    server = fake_server(lambda m, p, b: (200, {"issues": []}))
    Jira(server.url, "", "t", "SUP").queue("ai-triaged", ["runbook-succeeded", "runbook-none"])
    assert "labels+not+in+%28%22runbook-succeeded%22%2C+%22runbook-none%22%29" in server.requests[0]["path"]
