# platform/observability — B2

Push design: every cluster sends its metrics and logs to one hub on C3, where Grafana,
Alertmanager (B13) and the ops agent (B14) query a single place.

```
C1, C2  Prometheus in agent mode ──remote_write──► 172.30.3.10:30090 ─┐   (B2 part 2)
        Grafana Alloy (pod logs) ──push─────────► 172.30.3.10:31100 ─┤   over wg0
C3      Prometheus (scrapes C3 + receives) ◄───────────────────────────┤
        Loki (single binary)               ◄───────────────────────────┘
        Grafana (Prometheus + Loki datasources), Alertmanager
```

| Piece | Where | Version |
|---|---|---|
| kube-prometheus-stack (hub: Prometheus + receiver, Alertmanager, Grafana) | `clusters/c3/observability.yaml`, `kube-prometheus-stack/values{,-hub}.yaml` | 92.14.0 |
| Loki, single binary, filesystem | `clusters/c3/observability.yaml`, `loki/values.yaml` | 7.5.0 (Loki 3.6.16) |
| Loki NodePort for log push | `hub/loki-ingest.yaml` | |
| Grafana admin Secret (random, not in Git) | `infra/ansible/roles/grafana_admin` (run by `gitops.yml`) | |

**The `cluster` label.** Every target gets `cluster=<name>` at scrape time from a default
Prometheus Operator *scrape class*, set per cluster in the Argo CD Application (`helm.valuesObject`).
`externalLabels` alone would not work for the hub: it only labels data that leaves Prometheus, so
C3's own series would have no `cluster` label in queries.

**Storage.** emptyDir for Prometheus and Loki (no StorageClass yet), so data is lost when those pods
restart. Retention: metrics 7 days, logs 72 hours. Persistent storage comes with Vault in B15.

**Reaching it.** NodePorts are reachable only over the WireGuard mesh (ufw blocks them on public
interfaces). From your machine:

```bash
kubectl --context c3 -n monitoring port-forward svc/kps-grafana 3000:80        # http://localhost:3000
kubectl --context c3 -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d
```

## Verify ("done when": one dashboard shows all clusters' nodes)

```bash
kubectl --context c3 -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090 &
curl -s 'localhost:9090/api/v1/query' --data-urlencode 'query=count by (cluster) (node_uname_info)'
# expect c1, c2 and c3 once part 2 (the agents) is in; c3 alone after this part
```

In Grafana: *Node Exporter / USE Method / Cluster* (or *Kubernetes / Compute Resources /
Multi-Cluster*) with the `cluster` selector showing all clusters.
