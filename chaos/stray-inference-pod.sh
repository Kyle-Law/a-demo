#!/usr/bin/env bash
# Fault: someone starts a pod by hand in the inference namespace and leaves it running.
#
#   chaos/stray-inference-pod.sh [--context c2] [--revert]
#
# Creates a standalone pod (no controller, not managed by Argo CD) that just sleeps. Expected:
# StrayInferencePod fires after 5 minutes -> one ticket for <context>/inference/pod/chaos-stray-pod.
# There is no runbook for it at first: the executor finds none, and the ops agent proposes one as a
# PR to the runbooks repo (B16). --revert deletes the pod.
set -euo pipefail
DEFAULT_CONTEXT=c2
. "$(dirname "$0")/lib.sh"
parse_args "$@"

if $REVERT; then
  say "Deleting the stray pod"
  k -n inference delete pod chaos-stray-pod --ignore-not-found
  exit 0
fi

say "Creating a pod with no controller in inference"
k -n inference apply -f - <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: chaos-stray-pod
  labels: {chaos: stray-inference-pod, tenant: shared}
spec:
  restartPolicy: Never
  terminationGracePeriodSeconds: 1
  containers:
    - name: debug
      image: curlimages/curl:8.22.0
      command: ["sleep", "infinity"]
      resources:
        requests: {cpu: 10m, memory: 16Mi}
        limits: {memory: 32Mi}
YAML
k -n inference get pod chaos-stray-pod
say "Expect StrayInferencePod for $CONTEXT/inference/pod/chaos-stray-pod in ~5-6 minutes. Revert: $0 --context $CONTEXT --revert"
