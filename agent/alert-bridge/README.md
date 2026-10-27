# agent/alert-bridge — B13 incident intake

Turns firing alerts into Jira tickets for the ops agent. Ported from
[ai-sre-demo](https://github.com/Kyle-Law/ai-sre-demo) (`08-jira-integration`), which took Grafana webhooks;
this one takes **Alertmanager** webhooks from the hub on C3.

```
Prometheus rule (ticket="true") ─► Alertmanager (C3) ─► alert-bridge ─► Jira (labelled ai-triage) ─► ops agent (B14)
       group_by: cluster, namespace, resource ┘
```

- **firing**, no open ticket for that `cluster/namespace/resource` → **create** a ticket
- **firing**, open ticket for the same resource → **comment** "correlated alert" on it (one fault, one ticket)
- **resolved** → comment `RESOLVED` (closing is B15's job, after its runbook's `verify` passes)

**Which alerts.** Only alerts labelled `ticket="true"`, so kube-prometheus-stack's default rules never open
tickets. A ticketed alert must carry the alert → ticket contract (CLAUDE.md): labels `cluster`, `namespace`,
`resource`, `signal`, `severity`, and optionally the annotation `runbook_hint`. Without them the bridge refuses
and logs `contract_violation`: an "unknown" resource would merge unrelated faults into one ticket.

**What the agent reads.** The ticket description has an `ALERT_JSON_BEGIN … ALERT_JSON_END` block whose `ticket`
object is exactly the contract (see `tests/test_bridge.py::test_ticket_contract_is_machine_readable`).

**Credentials.** `ops/jira-credentials` is created by `infra/ansible/gitops.yml` (role `jira_credentials`) from
the git-ignored `.jira.env` in the repo root, never from Git. The bridge supports a Jira **service account**:
with `JIRA_EMAIL` set it uses Basic auth, without it a Bearer scoped token, through `JIRA_API_URL`
(`https://api.atlassian.com/ex/jira/<cloudId>`).

**Hardening** (as in ai-sre-demo): stdlib only on a digest-pinned `python:3.13-slim`, code mounted from a
ConfigMap, no ServiceAccount token, non-root, read-only root filesystem, all capabilities dropped. A
NetworkPolicy admits only the `monitoring` namespace (Alertmanager).

## Test

```bash
python3 -m venv .venv && .venv/bin/pip install pytest && .venv/bin/pytest agent/alert-bridge/tests
```

Against a fake Jira: create, dedupe, correlate, per-cluster separation, resolve, the contract block, the
contract-violation refusal, and Basic vs Bearer auth.

## Look around (once C3 is up)

```bash
kubectl --context c3 -n ops logs deploy/alert-bridge -f      # one JSON line per alert
kubectl --context c3 -n monitoring port-forward svc/kps-kube-prometheus-stack-alertmanager 9093
```
