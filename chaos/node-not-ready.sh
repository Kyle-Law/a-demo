#!/usr/bin/env bash
# Fault: a GPU node goes NotReady (kubelet stops).
#
#   chaos/node-not-ready.sh [--host c1-a100] [--revert]
#
# Stops kubelet on the host through the Ansible inventory (infra/ansible). Expected: NodeNotReady
# fires after 2 minutes -> one ticket for <cluster>/cluster-scoped/node/<host>. The node's model
# servers also go down, but Alertmanager inhibits their tickets in that cluster: the node ticket
# is the root cause. --revert starts kubelet again.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
host=c1-a100
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --host) host=${2:?--host needs a value}; shift ;;
    *) args+=("$1") ;;
  esac
  shift
done
parse_args "${args[@]+"${args[@]}"}"
state=stopped
$REVERT && state=started

say "kubelet on $host -> $state"
(cd "$(dirname "$0")/../infra/ansible" && \
  ansible "$host" -b -m ansible.builtin.systemd_service -a "name=kubelet state=$state")
$REVERT || say "Expect NodeNotReady for node/$host in ~2-3 minutes. Revert: $0 --host $host --revert"
