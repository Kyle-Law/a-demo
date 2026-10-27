# agent/ops-agent — B14 read-only triage + RCA

Picks up the tickets `agent/alert-bridge` opens (label `ai-triage`), investigates with **read-only** tools,
and posts a root-cause analysis with evidence. It changes nothing: fixing is B15's job (reviewed runbooks,
short-lived credentials), and CLAUDE.md decision 6 keeps write credentials away from it.

Ported from [ai-sre-demo](https://github.com/Kyle-Law/ai-sre-demo) `11-agent` (Jira client, kubectl guard,
`ALERT_JSON` parsing, untrusted-data stance), cut down to triage. New here: the LLM chain, the fleet view,
and the platform vocabulary.

```
Jira (ai-triage, not ai-triaged) ─► ops-agent ─► LLM chain ─► tools: kubectl (c1|c2|c3, read-only),
                                        │                          PromQL + LogQL on the C3 hub
                                        └─► comment: summary, root cause, evidence (exact queries), next steps
                                            labels: ai-triaged, rca-by-<model>
```

## The LLM chain (`AGENT_LLM_CHAIN`)

Tried in order; the next one runs when an endpoint is unreachable or fails (connection, timeout, 5xx, auth),
or when the model never makes the finishing `submit_diagnosis` call:

1. **intellect-3** on the MI300X (C1), OpenAI-compatible through the inference gateway
2. **model-a** (Qwen3-4B) on the A100 MIG slices / A6000 (C1, C2), same API; tool calling is on
   (`--tool-call-parser=hermes`)
3. **Claude** through the Anthropic API, so diagnosis still works when the GPU clusters are what's broken
   (CLAUDE.md: the agent must not depend solely on infrastructure it heals). **Claude Haiku 5.5**
   (`claude-haiku-5-5`) by default for the demo: cheap and fast for a rarely used fallback. Use
   `anthropic|claude-opus-5-5` in `AGENT_LLM_CHAIN` for the most capable model.

Each provider owns its tool loop (`ops_agent/llm/openai_compat.py`, `ops_agent/llm/anthropic_fallback.py`;
one SDK per file). The Claude one uses effort `high` (RCA is intelligence-sensitive; Haiku 5.5 and Opus 5.5
default to `medium`), `strict` tool schemas and `tool_choice` auto (the prompt asks for the finishing call),
and passes thinking blocks back unchanged. On models that support it (Opus 5.5 and others, not Haiku 5.5)
it opts into **server-side refusal fallbacks** (`fallbacks="default"`); on Haiku a refusal moves the chain on.

**The finishing call is validated** against its schema by both providers: vLLM doesn't enforce tool schemas,
and intellect-3 once wrote evidence with `arguments` instead of `query`. A bad call goes back to the model
as a tool error listing what to fix.

**Tried on the MI300X (2026-10-27)**, real loop and prompt, scripted tool outputs: the crash-loop fault was
diagnosed correctly in 22 s (2 tool calls); the DRA fault (`gpu-claim-pending`) in 43 s, following
pod → describe → ResourceClaims → ResourceSlices.

If every provider fails, the agent comments once and labels the ticket `ai-triage-failed` instead of
retrying forever.

## Tools (all read-only)

| Tool | What | Guard |
|---|---|---|
| `kubectl` | `cluster` + args, run with that cluster's read-only kubeconfig from `AGENT_KUBECONFIG_DIR/<cluster>.yaml` | read-only verbs only; no Secrets; `--context`/`--kubeconfig`/`--as`/`-w`/`-f` refused; RBAC is the backstop |
| `prometheus_query` | PromQL on the hub (every cluster's metrics, `cluster` label) | GET only, 30 series max |
| `loki_query` | LogQL over the last N minutes on the hub | GET only, truncated |
| `submit_diagnosis` | the finishing call: summary, root cause, evidence (source, exact query, finding), confidence, runbook, next steps | schema-checked |

**Grounding.** The system prompt (`ops_agent/prompts.py`) lists the clusters, namespaces, DRA objects, labels
and metric names that exist, because intellect-3 invented a `gpu="true"` label when it wasn't told
(MI300X test, 2026-10-27). `tests/test_prompt_vocabulary.py` fails if a metric used by a ticket rule is
missing from the prompt.

## Test

```bash
python3 -m venv .venv && .venv/bin/pip install pytest pyyaml -r agent/ops-agent/requirements.txt
cd agent/ops-agent && ../../.venv/bin/pytest
```

33 tests, offline: both providers drive their **real SDK** against a local fake server (request shape, tool
round-trip, fallback triggers); the chain's order; the kubectl guard; the triage loop on a ticket written by
the real `agent/alert-bridge` code (triage once, fail once, skip non-bridge tickets); prompt vocabulary.

## Run locally (prints, writes nothing)

```bash
set -a; . ./.jira.env; set +a                         # Jira (git-ignored)
export ANTHROPIC_API_KEY=...                          # only for the Claude fallback
export AGENT_KUBECONFIG_DIR=~/.kube/a-demo-readonly   # read-only kubeconfigs, NOT your admin ones
cd agent/ops-agent && python -m ops_agent diagnose SUP-12
```

## Run on C3

| Piece | Where |
|---|---|
| Image `ghcr.io/kyle-law/a-demo-ops-agent:<version>` | `Dockerfile`, built by `.github/workflows/ci.yml` (version tags are immutable; bump `pyproject.toml` to release) |
| Read-only `kube-system/ops-agent-reader` on every cluster (get/list/watch, never Secrets) | `infra/ansible/roles/ops_agent_reader`, run by `gitops.yml` |
| `ops/ops-agent-kubeconfigs` (one per cluster), `ops/ops-agent-llm` (from `.agent.env`) | `infra/ansible/roles/ops_agent_secrets` |
| Deployment, `AGENT_LLM_CHAIN` | `deploy/`, synced by `clusters/c3/ops-agent.yaml` |
| C1/C2 gateways reachable from C3 | NodePort 30080 over the WireGuard mesh (`inference/gateways/base/mesh-nodeport.yaml`), until B9 |

The chain on C3: `intellect-3-c1` → `model-a-c1` → `model-a-c2` → `anthropic:claude-haiku-5-5`. Losing C1 falls
through to C2's model A; losing both reaches Claude. A gone endpoint fails over within ~5 s (connect
timeout); a reasoning model still gets minutes to answer.

For the Claude fallback, put `ANTHROPIC_API_KEY=...` in a git-ignored `.agent.env` in the repo root and re-run
`gitops.yml`. Without it, the chain ends at model A.

```bash
kubectl --context c3 -n ops logs deploy/ops-agent -f
```
