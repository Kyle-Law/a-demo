#!/usr/bin/env bash
# B7 "done when": each model answers on its pod IP.
# Sends one real request to every vLLM pod in the inference namespace, straight to the pod IP
# (no Service, no gateway), using the API named by the pod's a-demo.io/api annotation.
# Usage: inference/models/tests/smoke.sh <kube-context>     e.g. smoke.sh c1
set -euo pipefail

ctx=${1:?usage: smoke.sh <kube-context>}
k() { kubectl --context "$ctx" -n inference "$@"; }

client=smoke-client-$$
k run "$client" --image=curlimages/curl:8.22.0 --restart=Never --command -- sleep 900 >/dev/null
trap 'k delete pod "$client" --wait=false >/dev/null 2>&1 || true' EXIT
k wait pod "$client" --for=condition=Ready --timeout=120s >/dev/null

pods=$(k get pods -l app.kubernetes.io/name=vllm --field-selector=status.phase=Running \
  -o jsonpath='{range .items[*]}{.metadata.name} {.status.podIP} {.metadata.labels.model} {.metadata.annotations.a-demo\.io/api}{"\n"}{end}')
[ -n "$pods" ] || { echo "no running vLLM pods in $ctx/inference" >&2; exit 1; }

failed=0
while read -r pod ip model api; do
  case "$api" in
    chat)
      path=/v1/chat/completions expect='"choices"'
      body='{"model": "'"$model"'", "messages": [{"role": "user", "content": "Reply with one word: ready?"}], "max_tokens": 32}' ;;
    embed)
      path=/v1/embeddings expect='"embedding"'
      body='{"model": "'"$model"'", "input": ["GPU node NotReady after driver upgrade"]}' ;;
    rerank)
      path=/v1/rerank expect='"relevance_score"'
      body='{"model": "'"$model"'", "query": "node NotReady", "documents": ["Runbook: drain and reboot a NotReady GPU node.", "Recipe: banana bread."]}' ;;
    *)
      echo "FAIL $pod: unknown a-demo.io/api '$api'"; failed=1; continue ;;
  esac
  if out=$(k exec "$client" -- curl -sS --fail-with-body --max-time 300 \
      -H 'Content-Type: application/json' -d "$body" "http://$ip:8000$path" 2>&1) \
     && grep -q "$expect" <<<"$out"; then
    echo "ok   $pod ($ip) $model $api"
  else
    echo "FAIL $pod ($ip) $model $api: ${out:0:300}"; failed=1
  fi
done <<<"$pods"
exit "$failed"
