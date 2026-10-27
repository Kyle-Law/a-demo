#!/usr/bin/env bash
# Fault: a model server crash-loops (a bad vLLM flag, as if a broken config shipped).
#
#   chaos/model-server-crashloop.sh [--context c1] [--revert]
#
# Pauses Argo CD auto-sync for models-<context> (otherwise self-heal undoes the change within
# seconds), then adds an invalid flag to model-b-vllm. Expected: ModelServerDown fires after
# 2 minutes -> one ticket for <context>/inference/deployment/model-b-vllm.
# --revert resumes auto-sync; Argo CD restores the Deployment from Git.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
parse_args "$@"
app=models-$CONTEXT
deploy=model-b-vllm
[ "$CONTEXT" = c1 ] || { echo "model-b only runs on c1" >&2; exit 1; }

if $REVERT; then
  say "Resuming auto-sync for $app; Argo CD restores $deploy from Git"
  hub -n argocd patch application "$app" --type merge \
    -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
  k -n inference rollout status deploy/"$deploy" --timeout=15m
  exit 0
fi

say "Pausing auto-sync for $app"
hub -n argocd patch application "$app" --type json -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
say "Adding an invalid vLLM flag to $deploy"
k -n inference patch deploy "$deploy" --type json \
  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--chaos-invalid-flag"}]'
say "Expect ModelServerDown for $CONTEXT/inference/deployment/$deploy in ~2-3 minutes. Revert: $0 --context $CONTEXT --revert"
