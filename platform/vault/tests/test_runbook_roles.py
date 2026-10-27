"""B15 contract: Vault's runbook roles, their ClusterRoles, the minter's bind list and the
admission guard's name prefix must agree. A mismatch fails at mint time on a live cluster
(Kubernetes refuses the binding) or, worse, lets a token past the guard."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def load_all(path: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((ROOT / path).read_text()) if d]


VAULT = yaml.safe_load((ROOT / "platform/vault/runbook-roles.yaml").read_text())
CLUSTER_ROLES = {d["metadata"]["name"] for d in load_all("platform/runbook-roles/runbook-roles.yaml")
                 if d["kind"] == "ClusterRole"}
MINTER = next(d for d in load_all("infra/ansible/roles/vault_minter/files/vault-minter.yaml")
              if d["kind"] == "ClusterRole")


def test_every_vault_role_has_its_cluster_role():
    assert {f"runbook-{r}" for r in VAULT["roles"]} == CLUSTER_ROLES


def test_minter_binds_exactly_the_runbook_cluster_roles():
    bind = [r for r in MINTER["rules"] if "bind" in r["verbs"]]
    assert len(bind) == 1 and bind[0]["resources"] == ["clusterroles"]
    assert set(bind[0]["resourceNames"]) == CLUSTER_ROLES


def test_minter_cannot_escalate_or_read_secrets():
    for rule in MINTER["rules"]:
        assert "escalate" not in rule["verbs"] and "*" not in rule["verbs"]
        assert "secrets" not in rule["resources"] and "*" not in rule["resources"]


def test_clusters_and_namespaces_are_known():
    for role, spec in VAULT["roles"].items():
        assert spec["clusters"] and set(spec["clusters"]) <= {"c1", "c2", "c3"}, role
        assert spec["namespaces"] and "*" not in spec["namespaces"], role


def test_generated_names_match_the_guard_prefix():
    template = (ROOT / "infra/ansible/roles/vault_config/defaults/main.yml").read_text()
    prefix = re.search(r'printf "([a-z-]+)%s', template).group(1)
    guard = next(d for d in load_all("platform/runbook-roles/runbook-guard.yaml")
                 if d["kind"] == "ValidatingAdmissionPolicy")
    namespace = VAULT["roles"]["gitops-operator"]["namespaces"][0]
    expected = f"system:serviceaccount:{namespace}:{prefix}"
    assert f"startsWith('{expected}')" in guard["spec"]["matchConditions"][0]["expression"]
