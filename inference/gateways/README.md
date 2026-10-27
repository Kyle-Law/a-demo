# inference/gateways — B8 inference gateway

One Gateway per GPU cluster (CLAUDE.md decision 1), implemented by **agentgateway** (decision 2).
A request takes this path:

```
client ──► Gateway inference-gateway (agentgateway, ClusterIP :80)
             │ 1. AgentgatewayPolicy model-routing (PreRouting):
             │    X-Gateway-Base-Model-Name = body.model, via an allowlist map
             ▼
           HTTPRoute models: match on that header
             ├─ model-a / model-b / intellect-3 ──► InferencePool <model>
             │                                        │ 2. ext-proc to the pool's EPP (llm-d router):
             │                                        │    score replicas on queue depth (2),
             │                                        │    KV-cache utilization (2), prefix-cache hits (3)
             │                                        ▼
             │                                      the best vLLM replica (inference/models)
             └─ embed / rerank ──► Service <model>-vllm (one replica each, nothing to pick)
```

| Piece | Where | Version |
|---|---|---|
| Gateway API CRDs (standard) + InferencePool CRD | `clusters/<c>/gateway-api-crds.yaml` | Gateway API v1.6.3, GIE v1.6.2 |
| agentgateway controller + CRDs | `clusters/<c>/agentgateway.yaml`, `platform/agentgateway/` | v1.6.0 |
| Gateway, model-routing policy | `base/` | |
| HTTPRoute per cluster | `c1/`, `c2/` | |
| InferencePool + EPP per chat model | `clusters/<c>/inference-pools.yaml`, `../pools/values.yaml` | llm-d router v1.0.0 |

As of GIE v1.6, the full Endpoint Picker lives in **llm-d** (`llm-d/llm-d-router`). The GIE
repository keeps the InferencePool API and conformance tests. Model-name routing doesn't need the
llm-d payload processor (formerly Body-Based Router): agentgateway reads the body itself, so there
is no extra ext-proc hop.

**Adding a model:** add it to the map in `base/model-routing.yaml`, add a rule to `c<N>/httproute.yaml`,
and for a chat model add it to that cluster's `inference-pools` ApplicationSet.

For B6 (default-deny): the gateway must reach each EPP on 9002 and the vLLM pods on 8000, and each
EPP must reach its vLLM pods on 8000 to scrape `/metrics`.

## Verify ("done when": load spreads across MIG replicas by queue/KV state)

```bash
kubectl --context c1 -n inference get gateway,httproute,inferencepool
kubectl --context c1 -n inference port-forward svc/inference-gateway 8080:80 &

# A model the map doesn't know: 404, so routing is model-aware
curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' -d '{"model": "nope", "messages": [{"role": "user", "content": "hi"}]}'

# 64 concurrent requests for model-a, then see which of the two replicas served them
seq 64 | xargs -P 16 -I{} curl -s -o /dev/null localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "model-a", "max_tokens": 256, "messages": [{"role": "user", "content": "Write a haiku about request {}"}]}'
for p in $(kubectl --context c1 -n inference get pods -l model=model-a -o name); do
  echo "$p $(kubectl --context c1 -n inference exec $p -c vllm -- python3 -c \
    'import urllib.request; print(urllib.request.urlopen("http://localhost:8000/metrics").read().decode())' \
    | grep -E '^vllm:request_success_total' | awk '{s+=$2} END {print s}')"
done
```

Both replicas should show successes. The split follows queue and KV-cache state, not round-robin.
