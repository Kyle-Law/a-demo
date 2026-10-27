"""The kubectl guard refuses writes, Secrets, connection overrides and unknown clusters."""

import pytest

from ops_agent.tools.kube import Kubectl, KubectlRefused, validate_args


@pytest.mark.parametrize("args", [
    ["get", "pods", "-n", "inference", "-o", "wide"],
    ["logs", "model-b-vllm-abc", "-n", "inference", "--previous"],
    ["get", "resourceclaims,resourceslices", "-A"],
    ["rollout", "status", "deploy/model-a-vllm", "-n", "inference"],
    ["auth", "can-i", "get", "secrets"],
])
def test_read_only_allowed(args):
    validate_args(args)


@pytest.mark.parametrize("args, why", [
    (["delete", "pod", "x"], "not read-only"),
    (["scale", "deploy/model-a-vllm", "--replicas=0"], "not read-only"),
    (["rollout", "restart", "deploy/x"], "rollout"),
    (["get", "secrets", "-n", "ops"], "Secrets"),
    (["get", "pods,secret/jira-credentials"], "Secrets"),
    (["get", "pods", "--as=system:admin"], "--as"),
    (["get", "pods", "--context", "kubernetes-admin@c1"], "--context"),
    (["logs", "x", "-f"], "-f"),
])
def test_refused(args, why):
    with pytest.raises(KubectlRefused, match=why):
        validate_args(args)


def test_unknown_cluster_and_missing_kubeconfig(tmp_path):
    k = Kubectl(str(tmp_path), ["c1", "c2", "c3"])
    with pytest.raises(KubectlRefused, match="unknown cluster"):
        k.run("prod", ["get", "pods"])
    code, out = k.run("c1", ["get", "pods"])
    assert code == 1 and "no read-only kubeconfig" in out
