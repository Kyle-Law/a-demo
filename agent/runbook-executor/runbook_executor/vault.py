"""Vault: the executor's only way to get Kubernetes write access (CLAUDE.md decision 6).

It logs in with its projected ServiceAccount token (Kubernetes auth, role runbook-executor) and
asks kubernetes-<cluster>/creds/<required_role> for a token bound to one namespace. Vault creates
a ServiceAccount + RoleBinding for it and deletes them when the lease ends. `revoke` ends the
executor's Vault token and with it every lease it created, so a run's Kubernetes tokens die when
the run does, not when their TTL runs out. See platform/vault/README.md."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request


log = logging.getLogger("runbook-executor")


class VaultError(RuntimeError):
    pass


class Vault:
    def __init__(self, addr: str, role: str, jwt_path: str):
        self.addr, self.role, self.jwt_path = addr.rstrip("/"), role, jwt_path

    def _req(self, method: str, path: str, body: dict | None = None, token: str | None = None) -> dict:
        headers = {"Content-Type": "application/json"} | ({"X-Vault-Token": token} if token else {})
        req = urllib.request.Request(f"{self.addr}/v1/{path}", method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                errors = json.loads(exc.read()).get("errors", [])
            except Exception:
                errors = []
            raise VaultError(f"Vault {method} {path} -> {exc.code}: {'; '.join(errors) or exc.reason}") from exc

    def login(self) -> str:
        with open(self.jwt_path) as f:
            jwt = f.read().strip()
        return self._req("POST", "auth/kubernetes/login", {"role": self.role, "jwt": jwt})["auth"]["client_token"]

    def kube_token(self, token: str, cluster: str, role: str, namespace: str) -> dict:
        """{token, service_account, lease_id, ttl} for `role` in `namespace` of `cluster`."""
        res = self._req("POST", f"kubernetes-{cluster}/creds/{role}", {"kubernetes_namespace": namespace}, token)
        data = res["data"]
        return {"token": data["service_account_token"],
                "service_account": f"{data['service_account_namespace']}/{data['service_account_name']}",
                "lease_id": res.get("lease_id", ""), "ttl": res.get("lease_duration", 0)}

    def revoke(self, token: str) -> None:
        self._req("POST", "auth/token/revoke-self", {}, token)


class RunCredentials:
    """Kubernetes tokens for one runbook phase, minted lazily per (cluster, namespace) and revoked
    together. Use as a context manager."""

    def __init__(self, vault: Vault, role: str):
        self.vault, self.role = vault, role
        self._vault_token: str | None = None
        self._tokens: dict[tuple[str, str], dict] = {}
        self.issued: list[str] = []  # "cluster: namespace/serviceaccount" for the ticket

    def __enter__(self) -> "RunCredentials":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def token(self, cluster: str, namespace: str) -> str:
        if (cluster, namespace) not in self._tokens:
            if self._vault_token is None:
                self._vault_token = self.vault.login()
            creds = self.vault.kube_token(self._vault_token, cluster, self.role, namespace)
            self._tokens[(cluster, namespace)] = creds
            self.issued.append(f"{cluster}: {creds['service_account']} ({creds['ttl']}s)")
        return self._tokens[(cluster, namespace)]["token"]

    def close(self) -> None:
        if self._vault_token is not None:
            token, self._vault_token = self._vault_token, None
            try:
                self.vault.revoke(token)
            except Exception as exc:  # the leases still end with their TTL (minutes)
                log.warning("could not revoke the run's Vault token: %s", exc)
