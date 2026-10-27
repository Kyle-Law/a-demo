#!/usr/bin/env bash
# Fault: a workload asks for a GPU that isn't free (no 2g.20gb MIG slice left on C1).
#
#   chaos/gpu-claim-pending.sh [--context c1] [--revert]
#
# Creates a standalone pod (not managed by Argo CD) whose ResourceClaim wants a 2g.20gb slice;
# model A (x2) and model B already hold all three. Expected: PodUnschedulable fires after
# 3 minutes -> one ticket for <context>/inference/pod/chaos-gpu-claim. --revert deletes the pod.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
parse_args "$@"

if $REVERT; then
  say "Deleting the pod and its claim"
  k -n inference delete pod chaos-gpu-claim --ignore-not-found
  exit 0
fi

say "Creating a pod that claims a 2g.20gb slice (template mig-2g-20gb from inference/models/c1)"
k -n inference apply -f - <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  name: chaos-gpu-claim
  labels: {chaos: gpu-claim-pending, tenant: shared}
spec:
  restartPolicy: Never
  resourceClaims:
    - name: gpu
      resourceClaimTemplateName: mig-2g-20gb
  containers:
    - name: wait
      image: curlimages/curl:8.22.0
      command: ["sleep", "infinity"]
      resources:
        claims:
          - name: gpu
YAML
k -n inference get pod chaos-gpu-claim
say "Expect PodUnschedulable for $CONTEXT/inference/pod/chaos-gpu-claim in ~3-4 minutes. Revert: $0 --context $CONTEXT --revert"
