# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project overview

A three-cluster GPU platform demo built for a GPU Infrastructure interview. It shows:

- Multi-tenant LLM inference on heterogeneous accelerators (A100 MIG, MI300X, A6000)
- Model-aware routing with the Gateway API Inference Extension
- Cross-cluster failover with Cilium Cluster Mesh
- Slurm-on-Kubernetes (Slinky) for fine-tuning, with a train-to-serve loop
- An AI ops agent that triages Jira tickets, runs reviewed runbooks with short-lived credentials, and proposes new runbooks via PR

Priority order when trading off effort: ops agent loop > Slurm depth > GPU stack correctness > networking polish > tenancy polish.

## Clusters

| Cluster | kube context | Hardware | Role |
|---|---|---|---|
| C1 | `c1` | CPU + A100 80GB (MIG) + MI300X | Multi-tenant inference |
| C2 | `c2` | CPU + A6000 48GB | Inference replica + Slinky training |
| C3 | `c3` | 3 CPU nodes | Ops control plane: agent, executor, Argo CD, observability |

Hosts are provisioned by hand (MI300X: HotAisle; CPU/A100/A6000: Prime Intellect) and reached over SSH on public IPs. Kubeconfigs: `~/.kube/a-demo/<cluster>.yaml`. Addressing plan: `infra/ansible/README.md`.

### C1 accelerator layout

- A100 MIG profiles `14,14,14,15` = 3x `2g.20gb` + 1x `1g.20gb`, allocated through DRA (`mig.nvidia.com` DeviceClass, CEL on `profile`; see decision 10).
  - 2x `2g.20gb`: model A replicas (same model, two pods; never tensor-parallel across MIG slices — not possible)
  - 1x `2g.20gb`: model B
  - 1x `1g.20gb`: embedding + reranker (agent retrieval over runbooks/tickets), both pods reference one shared
    ResourceClaim (`inference/models/c1/gpu-claims.yaml`); each caps itself at 40% of the slice's memory
- Models (`inference/models/`): A = Qwen3-4B-Instruct-2507, B = Qwen3.5-2B (vision), embed/rerank =
  Qwen3-Embedding-0.6B / Qwen3-Reranker-0.6B, MI300X = INTELLECT-3-FP8. Served under stable names
  (`model-a`, `model-b`, `embed`, `rerank`, `intellect-3`).
- Measured 2026-10-27 (Massed Compute A100 80GB PCIe, driver 580): each slice has **19.5 GiB** usable,
  not 20. BF16 GEMM: 2g.20gb ≈ 77 TFLOPS alone, ≈ 61 with all four slices busy (the 300 W board
  power is shared, so MIG isolates memory and SMs but not throughput); 1g.20gb ≈ 33.
- MIG mode *and* the slices are set by Ansible (`mig_layout`, `roles/nvidia_mig`; reboots once). GPU
  Operator's DRA mode has no mig-manager, and the DRA driver only discovers slices that exist when
  it starts, so a boot-time unit recreates them before kubelet. MIG mode on with no slices strands the GPU.
- MI300X 192GB: large FP8 MoE (target: INTELLECT-3-FP8), also the agent's primary LLM. The agent must keep an external fallback LLM — it must not depend solely on infrastructure it heals.

### C2

- A6000: model A replica + dynamic LoRA adapters.
- Slinky slurm-operator: LoRA fine-tune jobs. The A6000 is time-shared — the Slurm prolog scales down the vLLM replica; traffic fails over to C1 via Cluster Mesh.

## Key design decisions (don't undo without discussion)

1. **Two-tier routing.** Each GPU cluster has its own Gateway + InferencePool + EPP. Cross-cluster balancing uses a Cilium global Service in front of the *gateways* with `service.cilium.io/affinity: local`. Do not point a global Service directly at model pods — it bypasses the EPP. InferencePoolImport is alpha; do not depend on it.
2. **Gateway implementation is agentgateway** (v1.6; GIE-conformant, while Envoy Gateway and kgateway are not on GIE v1.6.2's conformant list). Cilium is the CNI only; do not use Cilium's built-in Gateway API controller for InferencePools. Model selection uses an `AgentgatewayPolicy` (PreRouting CEL copies the body's `model` into `X-Gateway-Base-Model-Name` through an allowlist map) plus standard HTTPRoutes matching that header; no payload processor. Replica selection is a per-model InferencePool plus the llm-d router EPP (the EPP moved from GIE to `llm-d/llm-d-router` in GIE v1.6).
3. **Model A runs in both C1 and C2** — it's the only model that load-balances across clusters.
4. **A100 has no FP8 tensor cores.** Use BF16 (≤4B) or AWQ/GPTQ on MIG slices. FP8 is fine on MI300X.
5. **Egress gateway is per-cluster** (Cilium requires the gateway node to be in the same cluster as the selected pods). Avoid HTTP L7 policies on egress-gateway flows.
6. **Agent never holds standing write credentials.** Read-only ServiceAccounts for triage; the executor obtains short-lived, runbook-scoped tokens (Vault Kubernetes secrets engine).
7. **Agent-authored runbooks are never executable until a human merges the PR.**
8. **Runbooks live in a separate repo** so the agent's GitHub token is scoped to that repo only.
9. **Host WireGuard mesh under everything.** All nodes of all clusters share `wg0` (`172.30.0.0/16`); the wg IP is the node IP. Cilium uses VXLAN over `wg0` with Cilium encryption off. Needed because C1 spans two providers and public IPs may be NATed. Pod CIDRs (`10.{1,2,3}.0.0/16`) and Cilium cluster IDs (1–3) must stay unique for Cluster Mesh.
10. **GPUs are allocated with DRA wherever supported**, not the device plugin. NVIDIA: GPU Operator in `GPUCluster` (DRA) mode with the host's pre-installed driver; workloads use ResourceClaims against `gpu.nvidia.com` / `mig.nvidia.com`, never `nvidia.com/gpu` or `nvidia.com/mig-*` limits. Pods that share a device (embed + rerank on the 1g slice; vLLM + Slurm on the A6000) reference one shared ResourceClaim; claims are namespaced, so sharers must live in one namespace. MIG is static (host unit); DynamicMIG is alpha and a later demo only. `GPUCluster` cannot coexist with or migrate in place from `ClusterPolicy`, so flipping back means reinstalling the GPU stack. Use the device plugin only where a component can't consume DRA (check Slinky in B11, the AMD DRA driver on MI300X in B4).

## Repository layout

```
infra/        ansible (OS tuning, WireGuard mesh, kubeadm + cilium bootstrap); no terraform for compute
clusters/     Argo CD app-of-apps: all/ (every cluster), c1/ c2/ (one cluster), c3/ (hub); see clusters/README.md
platform/     cilium, gpu-operator, amd-gpu, observability, egress
inference/    models/, gateways/, pools/
tenancy/      namespaces, rbac, quotas, kueue, kyverno
slurm/        slinky values, slurm.conf/gres.conf, prolog/epilog, job scripts
agent/        service, tools (k8s, prometheus, loki, jira, github), prompts
chaos/        fault-injection scripts, one per scenario
docs/         architecture, demo script
```

Runbooks: separate repo (TODO: repo URL).

## Building blocks

Track status here. Each block must meet its "done when" before it's marked done.

| ID | Block | Depends on | Done when | Status |
|---|---|---|---|---|
| B0 | Provisioning (Ansible, kubeadm, Cilium) | — | `cilium status` green on all 3 clusters | in progress (C2 verified 2026-10-27; C1, C3 pending) |
| B1 | GitOps hub (Argo CD on C3) | B0 | Git change lands on all 3 clusters | in progress (manifests written; not yet run) |
| B2 | Observability baseline (push to C3: Prometheus agents + Alloy → Prometheus + Loki + Grafana) | B1 | One dashboard shows all clusters' nodes | in progress (hub + agents written; not yet run) |
| B3 | NVIDIA stack (GPU Operator in DRA mode, static MIG, DCGM) | B1 | C1's ResourceSlice has 3x 2g.20gb + 1x 1g.20gb MIG devices, C2's the A6000; a claim-based test pod runs on each | in progress (device-plugin variant verified on hardware 2026-10-27; DRA variant not yet run) |
| B4 | AMD stack (AMD DRA driver if it supports MI300X, else AMD GPU Operator; exporter) | B1 | MI300X allocatable (ResourceSlice, or `amd.com/gpu` as fallback); ROCm smoke pod runs | todo |
| B5 | Cluster Mesh + Hubble Relay | B0 | Echo global service fails over C1→C2, visible in Hubble | todo |
| B6 | Egress gateways + default-deny + FQDN allowlist | B5 | Agent ns reaches only Jira/GitHub/LLM API | todo |
| B7 | Model serving (vLLM CUDA/ROCm, model cache, registry mirror) | B3, B4 | Each model answers on its pod IP | in progress (manifests written; not yet run; registry mirror not started) |
| B8 | Inference gateway (GIE, InferencePool, EPP, routes) | B7 | Load spreads across MIG replicas by queue/KV state | in progress (manifests written; not yet run) |
| B9 | Cross-cluster failover | B5, B8 | Model A serves with C1 replicas scaled to 0 | todo |
| B10 | Soft multi-tenancy on C1 | B8 | Tenant B blocked from disallowed profile; lower priority under load | todo |
| B11 | Slinky (operator, slurmdbd, GRES, Pyxis/Enroot, DCGM prolog) | B3 | Containerized GPU job runs; failed diag drains node | todo |
| B12 | Train-to-serve loop (LoRA → MinIO → dynamic adapter) | B9, B11 | One command: sbatch → new adapter answering | todo |
| B13 | Incident intake (Alertmanager → Jira) + chaos scripts | B2 | Each fault yields exactly one ticket | in progress (bridge + Alertmanager routing written, not yet run; alert rules + chaos scripts next) |
| B14 | Read-only agent (triage + RCA) | B13 | Correct RCA with evidence on every scripted fault | todo |
| B15 | Runbook repo + executor (Vault short-lived tokens, approval gate) | B14 | Merged runbook fixes a fault; ticket auto-closes | todo |
| B16 | Runbook authoring (agent opens PRs) | B15 | Agent PR passes CI; executable after merge | todo |

Build order: vertical slice on C2 first (B0, B3, B7, B8, B2, B13, B14), then C1 hardware, then mesh (B5, B9), then B15–B16, then training (B11–B12), then B6 and B10. Cut B10, then B6, if short on time.

## Contracts between blocks

- **Labels:** every pod, metric and log carries `cluster`, `tenant`, `model`.
- **Inference API:** OpenAI-compatible; clients use stable model names and never see cluster or hardware.
- **Alert → ticket schema:** `cluster`, `namespace`, `resource`, `signal`, `severity`, `runbook_hint` (optional).
- **Runbook schema:** `id`, `matches` (signal patterns), `preconditions`, `steps`, `verify`, `rollback`, `required_role`, `requires_approval`. Validated in CI.
- **Identity:** one ServiceAccount per agent function; read and execute are always separate identities.

## Conventions

- All cluster state goes through Git and Argo CD. No hand-applied manifests, except bootstrap done by `infra/ansible`: B0 (hosts, Cilium) and B1 (Argo CD install, spoke credentials, root ApplicationSet).
- Pin every Helm chart and image version; no `latest`.
- Secrets: never commit. Use TODO (SOPS / External Secrets) — confirm before adding anything secret-shaped.
- Python for the agent and tooling (TODO: confirm version, package manager); Bash for chaos scripts with `set -euo pipefail`.
- Every chaos script has a matching `--revert`.
- Commit messages: `<block-id>: <summary>`, e.g. `B8: add InferencePool for model-a`.

## Rules for Claude Code

- **Never run mutating commands against a live cluster** (`kubectl apply/delete/patch/scale/drain/cordon`, `helm install/upgrade`, `nvidia-smi mig`, `scontrol update`, `terraform apply`) without asking first. Read-only commands (`get`, `describe`, `logs`, `top`, `sinfo`, `squeue`, `terraform plan`) are fine.
- Always pass `--context` explicitly to `kubectl`/`helm`; never rely on the current context.
- Before changing MIG layout or Slurm node state, state the impact on running inference and tenants.
- Prefer editing manifests in Git and letting Argo CD sync over direct changes.
- When adding a block, update the status table above.
- When unsure whether an upstream API (Gateway API Inference Extension, Slinky, Cilium, vLLM ROCm) supports something, check the docs for the pinned version rather than assuming.

## Common commands

```bash
# TODO: fill in as blocks land
export KUBECONFIG=~/.kube/a-demo/c1.yaml:~/.kube/a-demo/c2.yaml:~/.kube/a-demo/c3.yaml
(cd infra/ansible && ansible-playbook site.yml --limit c2)   # B0; mutates hosts — ask first
cilium --context c2 status --wait
(cd infra/ansible && ansible-playbook gitops.yml)            # B1; mutates clusters — ask first
kubectl --context c3 -n argocd get applications
kubectl --context c1 get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable
cilium --context c1 clustermesh status
hubble observe --since 5m
kubectl --context c2 exec -it <slurm-login-pod> -- sinfo
```
