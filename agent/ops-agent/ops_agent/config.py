"""Agent configuration from the environment (Secrets and the Deployment set these on C3)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# provider|model|base_url entries, tried in order. "|" because URLs contain ":".
DEFAULT_LLM_CHAIN = ",".join([
    "openai|intellect-3|http://inference-gateway.inference.svc/v1",  # MI300X (c1)
    "openai|model-a|http://inference-gateway.inference.svc/v1",      # A100 MIG / A6000 (c1, c2)
    "anthropic|claude-opus-5-5",                                     # external, survives our GPUs failing
])


@dataclass
class LLMEntry:
    kind: str  # "openai" (OpenAI-compatible, our vLLM) or "anthropic"
    model: str
    base_url: str = ""


@dataclass
class Config:
    jira_api_url: str
    jira_email: str
    jira_api_token: str
    jira_project: str
    triage_label: str = "ai-triage"
    done_label: str = "ai-triaged"
    failed_label: str = "ai-triage-failed"
    prometheus_url: str = "http://kps-kube-prometheus-stack-prometheus.monitoring.svc:9090"
    loki_url: str = "http://loki.monitoring.svc:3100"
    kubeconfig_dir: str = "/etc/ops-agent/kubeconfigs"
    clusters: list[str] = field(default_factory=lambda: ["c1", "c2", "c3"])
    llm_chain: list[LLMEntry] = field(default_factory=list)
    poll_seconds: int = 60
    max_turns: int = 20


def parse_chain(spec: str) -> list[LLMEntry]:
    entries = []
    for item in filter(None, (s.strip() for s in spec.split(","))):
        parts = item.split("|")
        if parts[0] == "openai" and len(parts) == 3:
            entries.append(LLMEntry("openai", parts[1], parts[2]))
        elif parts[0] == "anthropic" and len(parts) == 2:
            entries.append(LLMEntry("anthropic", parts[1]))
        else:
            raise ValueError(f"bad AGENT_LLM_CHAIN entry '{item}' (want openai|model|url or anthropic|model)")
    return entries


def from_env(env=os.environ) -> Config:
    missing = [k for k in ("JIRA_API_TOKEN", "JIRA_PROJECT_KEY") if not env.get(k)]
    api_url = env.get("JIRA_API_URL") or env.get("JIRA_BASE_URL", "")
    if not api_url:
        missing.append("JIRA_API_URL or JIRA_BASE_URL")
    if missing:
        raise SystemExit(f"missing required env vars: {', '.join(missing)}")
    cfg = Config(jira_api_url=api_url, jira_email=env.get("JIRA_EMAIL", ""),
                 jira_api_token=env["JIRA_API_TOKEN"], jira_project=env["JIRA_PROJECT_KEY"],
                 llm_chain=parse_chain(env.get("AGENT_LLM_CHAIN", DEFAULT_LLM_CHAIN)))
    for attr, key in (("prometheus_url", "PROMETHEUS_URL"), ("loki_url", "LOKI_URL"),
                      ("kubeconfig_dir", "AGENT_KUBECONFIG_DIR"), ("triage_label", "AGENT_TRIAGE_LABEL")):
        if env.get(key):
            setattr(cfg, attr, env[key])
    if env.get("AGENT_CLUSTERS"):
        cfg.clusters = [c.strip() for c in env["AGENT_CLUSTERS"].split(",") if c.strip()]
    cfg.poll_seconds = int(env.get("AGENT_POLL_SECONDS", cfg.poll_seconds))
    cfg.max_turns = int(env.get("AGENT_MAX_TURNS", cfg.max_turns))
    return cfg
