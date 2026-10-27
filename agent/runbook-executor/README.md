# Runbook executor (B15)

Runs reviewed runbooks from [a-demo-runbooks](https://github.com/Kyle-Law/a-demo-runbooks) for alert
tickets, after a human approves on the Jira ticket. Every Kubernetes write uses a token Vault mints
for that one run, scoped to the runbook's `required_role` in one namespace (CLAUDE.md decisions 6–8).
The executor holds no standing Kubernetes permissions, and the ops agent (B14) holds no write
access at all.

## A ticket's path

```
alert-bridge ticket ──► ops agent posts its diagnosis (label ai-triaged)
  ──► executor selects a runbook from main ──► preconditions (PromQL on the hub)
        no match / ambiguous ─► runbook-none        (a human takes over)
        preconditions false  ─► runbook-not-needed  (the fault cleared)
  ──► plan comment (runbook, commit, steps, verify) ─► runbook-proposed
  ──► "/approve" from a human ──► preconditions again
  ──► Vault: login, mint a token per (cluster, namespace) ──► steps ──► revoke
  ──► verify until it passes or its window ends
        passed ─► report, runbook-succeeded, ticket moved to Done
        failed ─► rollback (fresh tokens), report, runbook-failed, ticket stays open
```

- **Selection** is deterministic and uses no LLM. A runbook matches when its `signals` contain the
  ticket's signal, and its optional `resource` regex and `clusters` list also match. If several
  match, the alert's `runbook_hint` breaks the tie; otherwise nothing runs. A hint never selects a
  runbook that doesn't match.
- **Only merged runbooks run.** The executor reads only `main` of the protected runbooks repo,
  validates each runbook with the schema from the same commit (the checks of the repo's
  `tools/validate.py`), and skips invalid ones. The plan names the commit; an approval runs exactly
  that commit, even if `main` has moved since.
- **Approval:** a comment starting with `/approve` or `/reject`, posted after the plan. Comments
  from the executor's own Jira account are ignored. With `EXECUTOR_APPROVERS` set (Jira accountIds),
  only those people count.
- **Ticket values are names, not text.** `cluster`, `namespace` and the resource's kind and name
  must be plain Kubernetes names before they are put into PromQL or API paths.
- **Restarts:** state lives in Jira labels. If the executor finds `runbook-running` when it starts a
  pass, it died mid-run. It says so and stops; it never runs the steps twice.
- **Where actions go:** `argocd.*` runs on C3 in `argocd`, where the Applications live (the
  admission guard in `platform/runbook-roles` limits the change to sync settings). `k8s.*` runs on
  the ticket's cluster.

## Run

```bash
python -m runbook_executor runbooks       # what's loadable at main, and what's invalid
python -m runbook_executor plan SUP-12    # which runbook would run, preconditions now; writes nothing
python -m runbook_executor once           # one pass (needs Vault and cluster endpoints)
pytest -q                                 # tests: fake Jira, Prometheus, Vault and clusters
```

Configuration (environment): `JIRA_*` (same Secret as the alert bridge), `PROMETHEUS_URL`,
`VAULT_ADDR`, `VAULT_ROLE`, `VAULT_JWT_PATH` (a projected ServiceAccount token with audience
`vault`), `EXECUTOR_CLUSTERS_DIR` (`endpoints.json` plus a `<cluster>.crt` CA per cluster),
`EXECUTOR_APPROVERS`, `RUNBOOKS_REPO`/`RUNBOOKS_REF`.

## On C3

`deploy/` (Argo CD app `clusters/c3/runbook-executor.yaml`), in namespace `ops`:
- ServiceAccount `runbook-executor` with **no RBAC at all** and no Kubernetes API token mounted.
  The pod gets one projected token with audience `vault`, which lasts 10 minutes; that's all it can
  log in to Vault with.
- `jira-credentials` (B13) and the ConfigMap `runbook-executor-clusters` come from
  `infra/ansible/gitops.yml` (`roles/vault_config`, tag `vault`). The ConfigMap holds each cluster's
  API endpoint (the hub's in-cluster, the spokes' over the WireGuard mesh) and its CA. A run that
  reaches only some clusters updates their entries and keeps the others.
- To restrict who can approve, set `EXECUTOR_APPROVERS` in `deploy/deployment.yaml` to Jira accountIds.

`tests/fixtures/a-demo-runbooks` is a copy of the runbooks repo at cd67a2e (schema and the three
runbooks), so the tests exercise the real runbooks.
