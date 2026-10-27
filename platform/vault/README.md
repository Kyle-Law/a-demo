# Vault (B15)

Vault on C3 is where the runbook executor gets its credentials. For each runbook it gets a
Kubernetes token that is short-lived and scoped to that runbook's `required_role`, so no component
holds standing write access (CLAUDE.md decisions 6 and 8).

| Piece | Where |
|---|---|
| Chart 0.34.1 (Vault 2.0.4), 3 Raft replicas, file audit log | `clusters/c3/vault.yaml`, `values.yaml` |
| Storage: `local-path`, C3's default StorageClass | `clusters/c3/local-path-provisioner.yaml` |
| Init and unseal | `infra/ansible/roles/vault_unseal` (`gitops.yml`, tag `vault`) |
| Kubernetes secrets engines, runbook roles, executor auth | next B15 PR |

## Operating it

```bash
(cd infra/ansible && ansible-playbook gitops.yml --tags vault)   # init once; unseal after any restart
kubectl --context c3 -n vault exec vault-0 -- vault status
```

- The first run initializes Vault (3 key shares, threshold 2) and writes the keys and root token to
  `.vault-init.json` in the repo root on the controller. That file is git-ignored and mode 0600, and it
  is the only copy: **losing it loses Vault's data.** Ansible refuses to continue if the file and Vault
  disagree, for example a leftover file next to a new Vault.
- Vault starts **sealed** after every pod restart, because there's no cloud KMS here for auto-unseal.
  A sealed replica isn't Ready (`kubectl --context c3 -n vault get pods`); re-run the play to unseal
  it. Raft needs 2 of 3 replicas unsealed to serve.
- Each replica keeps its Raft copy on its own node's disk (local-path), and the third replica runs on
  the control plane because C3 has only two workers. The data survives a node loss while 2 copies
  remain; a lost node's volume is gone with it.
- TLS is off inside the cluster: pod traffic between nodes runs over the WireGuard mesh, and only
  C3's executor talks to Vault. B6's default-deny will narrow who can reach port 8200.
