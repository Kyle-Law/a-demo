"""Executor configuration from the environment (Secrets and the Deployment set these on C3)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    jira_api_url: str
    jira_email: str
    jira_api_token: str
    jira_project: str
    # The executor acts on a ticket once the ops agent has posted its diagnosis (B14).
    ready_label: str = "ai-triaged"
    runbooks_repo: str = "Kyle-Law/a-demo-runbooks"
    runbooks_ref: str = "main"  # only merged runbooks run (CLAUDE.md decision 7)
    runbooks_refresh_seconds: int = 300  # GitHub allows 60 unauthenticated API calls an hour
    prometheus_url: str = "http://kps-kube-prometheus-stack-prometheus.monitoring.svc:9090"
    vault_addr: str = "http://vault-active.vault.svc:8200"
    vault_role: str = "runbook-executor"
    vault_jwt_path: str = "/var/run/secrets/vault/token"  # projected ServiceAccount token, audience "vault"
    clusters_dir: str = "/etc/runbook-executor/clusters"  # endpoints.json + <cluster>.crt
    # Jira accountIds allowed to /approve. Empty: anyone except the executor's own account.
    approvers: list[str] = field(default_factory=list)
    poll_seconds: int = 60
    verify_interval_seconds: int = 30


def from_env(env=os.environ) -> Config:
    missing = [k for k in ("JIRA_API_TOKEN", "JIRA_PROJECT_KEY") if not env.get(k)]
    api_url = env.get("JIRA_API_URL") or env.get("JIRA_BASE_URL", "")
    if not api_url:
        missing.append("JIRA_API_URL or JIRA_BASE_URL")
    if missing:
        raise SystemExit(f"missing required env vars: {', '.join(missing)}")
    cfg = Config(jira_api_url=api_url, jira_email=env.get("JIRA_EMAIL", ""),
                 jira_api_token=env["JIRA_API_TOKEN"], jira_project=env["JIRA_PROJECT_KEY"])
    for attr, key in (("ready_label", "EXECUTOR_READY_LABEL"), ("runbooks_repo", "RUNBOOKS_REPO"),
                      ("runbooks_ref", "RUNBOOKS_REF"), ("prometheus_url", "PROMETHEUS_URL"),
                      ("vault_addr", "VAULT_ADDR"), ("vault_role", "VAULT_ROLE"),
                      ("vault_jwt_path", "VAULT_JWT_PATH"), ("clusters_dir", "EXECUTOR_CLUSTERS_DIR")):
        if env.get(key):
            setattr(cfg, attr, env[key])
    if env.get("EXECUTOR_APPROVERS"):
        cfg.approvers = [a.strip() for a in env["EXECUTOR_APPROVERS"].split(",") if a.strip()]
    for attr, key in (("poll_seconds", "EXECUTOR_POLL_SECONDS"), ("verify_interval_seconds", "EXECUTOR_VERIFY_INTERVAL_SECONDS"),
                      ("runbooks_refresh_seconds", "RUNBOOKS_REFRESH_SECONDS")):
        if env.get(key):
            setattr(cfg, attr, int(env[key]))
    return cfg
