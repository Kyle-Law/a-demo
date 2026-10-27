# chaos/ — B13 fault injection

One script per fault. Each fault should open **exactly one** Jira ticket (B13 "done when"), and each
script undoes its fault with `--revert`. They change live clusters, so run them deliberately.

| Script | Fault | Alert → ticket (`cluster/namespace/resource`) | Fires after |
|---|---|---|---|
| `model-server-crashloop.sh` | model-b crash-loops (an invalid vLLM flag). Pauses Argo CD auto-sync first, or self-heal would undo it in seconds; `--revert` resumes it and Git heals the Deployment | `ModelServerDown` → `c1/inference/deployment/model-b-vllm` | ~2–3 min |
| `inference-queue-backlog.sh` | A Job keeps 256 long model-a requests in flight through the gateway | `InferenceQueueBacklog` → `<c>/inference/deployment/model-a-vllm` | ~2–3 min |
| `gpu-claim-pending.sh` | A standalone pod claims a 2g.20gb slice when all three are taken | `PodUnschedulable` → `c1/inference/pod/chaos-gpu-claim` | ~3–4 min |
| `node-not-ready.sh` | kubelet stopped on a GPU node (through the Ansible inventory) | `NodeNotReady` → `<c>/cluster-scoped/node/<host>` | ~2–3 min |

The rules are in `platform/observability/rules/ticket-rules.yaml`, evaluated on the hub Prometheus (C3).

**Why one fault gives one ticket:**

- Alertmanager groups by `cluster, namespace, resource`, and the bridge correlates a second alert on the same resource
  into the open ticket (`agent/alert-bridge`).
- A NotReady node also takes its pods down. Alertmanager inhibits `ModelServerDown` and `PodUnschedulable` in that
  cluster while `NodeNotReady` fires, so the node ticket stays the only one.
- kube-prometheus-stack's default rules (e.g. `KubePodCrashLooping`) fire too, but have no `ticket="true"`, so they
  open nothing.

**Checks.** `agent/alert-bridge/tests/test_rules_contract.py` fails if a ticket rule misses a contract field, an
aggregation drops `cluster`, a ticket rule has no chaos script, or a script lacks `--revert` or strict mode.

```bash
chaos/gpu-claim-pending.sh --context c1                # inject
kubectl --context c3 -n ops logs deploy/alert-bridge -f # watch the ticket appear (~3-4 min)
chaos/gpu-claim-pending.sh --context c1 --revert       # undo; the ticket gets a RESOLVED comment
```
