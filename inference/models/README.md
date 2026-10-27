# inference/models — B7 model serving

One vLLM server per model (vLLM v0.32.0; CUDA image for NVIDIA, ROCm image for the MI300X). Kustomize:

| Directory | Holds |
|---|---|
| `base/` | The vLLM Deployment and Service shared by every model |
| `model-a/`, `model-b/`, `embed/`, `rerank/`, `intellect-3/` | One model each: Hugging Face repo pinned to a commit, served name, vLLM flags |
| `c1/`, `c2/` | What runs in each cluster, on which accelerator, how many replicas. Synced by `clusters/<c>/models.yaml` |

| Served name | Model | C1 | C2 |
|---|---|---|---|
| `model-a` | Qwen/Qwen3-4B-Instruct-2507 (BF16, LoRA) | 2x `mig-2g.20gb` | 1x A6000 |
| `model-b` | Qwen/Qwen3.5-2B (vision) | 1x `mig-2g.20gb` | — |
| `embed` | Qwen/Qwen3-Embedding-0.6B | `mig-1g.20gb.shared` | — |
| `rerank` | Qwen/Qwen3-Reranker-0.6B | `mig-1g.20gb.shared` | — |
| `intellect-3` | PrimeIntellect/INTELLECT-3-FP8 | 1x MI300X | — |

Clients use the served name, never the Hugging Face id or the hardware.

**Model cache.** An init container downloads the pinned revision into `/var/lib/a-demo/models` on the node, then vLLM starts with `HF_HUB_OFFLINE=1`. The first pod on a fresh node pays for the download (INTELLECT-3 is 101 GiB); later pods and restarts reuse it.

**Adding a model.** Copy a model directory, change the pinned id, revision (`curl -s https://huggingface.co/api/models/<id> | jq -r .sha`), served name and flags, add the `a-demo.io/api` annotation (`chat`, `embed` or `rerank`), then list it in a cluster overlay with its accelerator.

## Verify ("done when": each model answers on its pod IP)

```bash
kubectl --context c1 -n inference get pods -l app.kubernetes.io/name=vllm -o wide
inference/models/tests/smoke.sh c1
inference/models/tests/smoke.sh c2
```

Render without a cluster: `kubectl kustomize inference/models/c1`.
