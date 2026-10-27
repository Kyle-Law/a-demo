"""Read-only kubectl across the fleet.

Ported from ai-sre-demo (11-agent/kube.py). Defence in depth: the agent's ServiceAccount on every
cluster is read-only (RBAC is the backstop), but the agent also refuses to *attempt* anything
outside a small read-only allowlist, never reads Secrets, and never lets the model point kubectl
elsewhere. The model names a cluster; the agent picks that cluster's read-only kubeconfig from
AGENT_KUBECONFIG_DIR/<cluster>.yaml and ignores any KUBECONFIG in its environment.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

READ_ONLY_VERBS = {"get", "describe", "logs", "top", "events", "explain", "api-resources", "version"}
# verb -> allowed sub-commands
READ_ONLY_SUBCOMMANDS = {"auth": {"can-i"}, "rollout": {"status", "history"}}

FORBIDDEN_FLAGS = (
    "--kubeconfig", "--context", "--cluster", "--user", "--token", "--server", "-s",
    "--as", "--as-group", "--as-uid", "--insecure-skip-tls-verify", "--username", "--password",
    "--client-certificate", "--client-key", "--certificate-authority", "--raw",
    "-w", "--watch", "--watch-only", "-f", "--follow",
)
SECRET_RESOURCES = {"secret", "secrets"}
MAX_OUTPUT_CHARS = 12_000


class KubectlRefused(Exception):
    """Raised when a kubectl invocation is outside the read-only allowlist."""


def validate_args(args: list[str]) -> None:
    if not args or not all(isinstance(a, str) for a in args):
        raise KubectlRefused("kubectl needs a non-empty list of string arguments")
    verb = args[0]
    if verb in READ_ONLY_SUBCOMMANDS:
        if len(args) < 2 or args[1] not in READ_ONLY_SUBCOMMANDS[verb]:
            raise KubectlRefused(f"only 'kubectl {verb} {'|'.join(sorted(READ_ONLY_SUBCOMMANDS[verb]))}' is allowed")
    elif verb not in READ_ONLY_VERBS:
        raise KubectlRefused(f"verb '{verb}' is not read-only; allowed: {sorted(READ_ONLY_VERBS | set(READ_ONLY_SUBCOMMANDS))}")

    for arg in args[1:]:
        flag = arg.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS:
            raise KubectlRefused(f"flag '{flag}' is not allowed")
        if arg.startswith("-") or verb == "auth":  # "auth can-i get secrets" only asks a question
            continue
        # e.g. "secrets", "secret/foo", "pods,secrets", "secrets.v1"
        for part in arg.lower().replace("/", ",").split(","):
            if part.split(".", 1)[0] in SECRET_RESOURCES:
                raise KubectlRefused("reading Secrets is never allowed for the agent")


class Kubectl:
    def __init__(self, kubeconfig_dir: str, clusters: list[str], kubectl_bin: str = "kubectl", timeout: int = 30):
        self.kubeconfig_dir = Path(kubeconfig_dir)
        self.clusters = clusters
        self.kubectl_bin = kubectl_bin
        self.timeout = timeout

    def run(self, cluster: str, args: list[str]) -> tuple[int, str]:
        """Return (exit_code, output). Never raises for kubectl errors; output is truncated."""
        if cluster not in self.clusters:
            raise KubectlRefused(f"unknown cluster '{cluster}'; known: {self.clusters}")
        validate_args(args)
        kubeconfig = self.kubeconfig_dir / f"{cluster}.yaml"
        if not kubeconfig.is_file():
            return 1, f"no read-only kubeconfig for {cluster} at {kubeconfig}"
        env = {k: v for k, v in os.environ.items() if k != "KUBECONFIG"}
        try:
            proc = subprocess.run([self.kubectl_bin, "--kubeconfig", str(kubeconfig), *args],
                                  env=env, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return 124, f"kubectl timed out after {self.timeout}s"
        except FileNotFoundError:
            return 127, f"kubectl binary not found: {self.kubectl_bin}"
        output = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        return proc.returncode, truncate(output)


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 3]
    tail = text[-(2 * limit // 3):]
    return f"{head}\n... [{len(text) - limit} chars truncated] ...\n{tail}"
