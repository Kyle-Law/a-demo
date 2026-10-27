# Shared helpers for the chaos scripts (sourced, not run). Each script injects one fault that
# should yield exactly one Jira ticket (B13), and undoes it with --revert.

usage() {
  awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
  exit "${1:-0}"
}

# Parse "[--revert] [--context <c>]" into REVERT and CONTEXT (default: $DEFAULT_CONTEXT).
parse_args() {
  REVERT=false
  CONTEXT=${DEFAULT_CONTEXT:-c1}
  while [ $# -gt 0 ]; do
    case "$1" in
      --revert) REVERT=true ;;
      --context) CONTEXT=${2:?--context needs a value}; shift ;;
      -h|--help) usage 0 ;;
      *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
    shift
  done
}

k() { kubectl --context "$CONTEXT" "$@"; }
hub() { kubectl --context c3 "$@"; } # Argo CD and Alertmanager live on C3

say() { printf '\n==> %s\n' "$*"; }
