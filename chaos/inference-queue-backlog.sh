#!/usr/bin/env bash
# Fault: more inference demand than model A's replicas can take.
#
#   chaos/inference-queue-backlog.sh [--context c1] [--revert]
#
# Starts a Job that keeps 256 long chat requests for model-a in flight through the inference
# gateway. Expected: InferenceQueueBacklog fires after 2 minutes -> one ticket for
# <context>/inference/deployment/model-a-vllm. --revert deletes the Job.
# The threshold (more than 5 waiting) is a first guess; tune it on real hardware.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
parse_args "$@"

if $REVERT; then
  say "Deleting the load Job"
  k -n inference delete job chaos-queue-backlog --ignore-not-found
  exit 0
fi

say "Starting 256 concurrent model-a requests through the gateway"
k -n inference apply -f - <<'YAML'
apiVersion: batch/v1
kind: Job
metadata:
  name: chaos-queue-backlog
  labels: {chaos: queue-backlog}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 1800 # stops by itself after 30 minutes
  template:
    metadata:
      labels: {chaos: queue-backlog}
    spec:
      restartPolicy: Never
      containers:
        - name: load
          image: curlimages/curl:8.22.0
          command: ["sh", "-c"]
          args:
            - |
              body='{"model": "model-a", "max_tokens": 2048, "messages": [{"role": "user", "content": "Write a very long, detailed essay on GPU scheduling in Kubernetes."}]}'
              worker() { while true; do curl -s -o /dev/null --max-time 600 -H 'Content-Type: application/json' -d "$body" http://inference-gateway/v1/chat/completions; done; }
              for i in $(seq 256); do worker & done
              wait
YAML
say "Expect InferenceQueueBacklog for $CONTEXT/inference/deployment/model-a-vllm in ~2-3 minutes. Revert: $0 --context $CONTEXT --revert"
