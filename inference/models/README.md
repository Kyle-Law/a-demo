# inference/models — B7 model serving

One vLLM server per model (vLLM v0.32.0; CUDA image for NVIDIA, ROCm image for the MI300X). Kustomize:

| Directory | Holds |
|---|---|
| `base/` | The vLLM Deployment and Service shared by every model |
| `model-a/`, `model-b/`, `embed/`, `rerank/`, `intellect-3/` | One model each: Hugging Face repo pinned to a commit, served name, vLLM flags |
| `c1/`, `c2/` | What runs in each cluster, on which accelerator (`gpu-claims.yaml`), how many replicas. Synced by `clusters/<c>/models.yaml` |

| Served name | Model | C1 | C2 |
|---|---|---|---|
| `model-a` | Qwen/Qwen3-4B-Instruct-2507 (BF16, LoRA) | 2x 2g.20gb slice (claim per pod) | 1x A6000 |
| `model-b` | Qwen/Qwen3.5-2B (vision) | 1x 2g.20gb slice | — |
| `embed` | Qwen/Qwen3-Embedding-0.6B | the 1g.20gb slice, **shared claim** with rerank | — |
| `rerank` | Qwen/Qwen3-Reranker-0.6B | the 1g.20gb slice, **shared claim** with embed | — |
| `intellect-3` | PrimeIntellect/INTELLECT-3-FP8 | 1x MI300X | — |

Clients use the served name, never the Hugging Face id or the hardware.

**Accelerators come from DRA ResourceClaims** (CLAUDE.md decision 10), declared in `c1/gpu-claims.yaml` and `c2/gpu-claims.yaml`:

- A `ResourceClaimTemplate` gives every pod its own claim, so model A's two replicas get two different 2g.20gb slices. The CEL selector picks the MIG profile.
- A plain `ResourceClaim` is one claim that several pods reference. embed and rerank both point at `mig-1g-20gb-shared`, so they run on the same 1g.20gb slice. Each caps its memory with `--gpu-memory-utilization=0.40`. Claims are namespaced, so pods that share one must be in the same namespace.
- See what got allocated with `kubectl --context c1 -n inference get resourceclaims` (the ALLOCATED and RESERVED-FOR columns).

**Model cache.** An init container downloads the pinned revision into `/var/lib/a-demo/models` on the node, then vLLM starts with `HF_HUB_OFFLINE=1`. The first pod on a fresh node pays for the download (INTELLECT-3 is 101 GiB); later pods and restarts reuse it.

**Adding a model.** Copy a model directory, change the pinned id, revision (`curl -s https://huggingface.co/api/models/<id> | jq -r .sha`), served name and flags, add the `a-demo.io/api` annotation (`chat`, `embed` or `rerank`), then list it in a cluster overlay with its accelerator.

## Verify ("done when": each model answers on its pod IP)

```bash
kubectl --context c1 -n inference get pods -l app.kubernetes.io/name=vllm -o wide
inference/models/tests/smoke.sh c1
inference/models/tests/smoke.sh c2
```

Render without a cluster: `kubectl kustomize inference/models/c1`.
