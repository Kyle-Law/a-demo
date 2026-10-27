"""The agent's tools: what the model may call, and how each call runs. All of them only read."""

from __future__ import annotations

from .kube import Kubectl, KubectlRefused
from .observability import Loki, Prometheus

FINISH_TOOL = "submit_diagnosis"


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOL_SPECS = [
    {
        "name": "kubectl",
        "description": ("Run a READ-ONLY kubectl command on one cluster (c1, c2 or c3). Allowed verbs: get, describe, "
                        "logs, top, events, explain, api-resources, version, rollout status|history, auth can-i. "
                        "Secrets are never readable; --context/--kubeconfig/-w/-f are refused. "
                        "Example args: [\"get\", \"pods\", \"-n\", \"inference\", \"-o\", \"wide\"]"),
        "input_schema": _schema({
            "cluster": {"type": "string", "enum": ["c1", "c2", "c3"]},
            "args": {"type": "array", "items": {"type": "string"}},
        }, ["cluster", "args"]),
    },
    {
        "name": "prometheus_query",
        "description": ("PromQL instant query on the hub Prometheus, which holds every cluster's metrics. Every series "
                        "has a cluster label; always filter by it. Returns up to 30 series."),
        "input_schema": _schema({"query": {"type": "string"}}, ["query"]),
    },
    {
        "name": "loki_query",
        "description": ("LogQL query over the last N minutes on the hub Loki (pod logs from every cluster). Labels: "
                        "cluster, namespace, pod, container, node, tenant, model. Example: "
                        "{cluster=\"c1\", namespace=\"inference\", model=\"model-b\"} |= \"error\""),
        "input_schema": _schema({
            "query": {"type": "string"},
            "minutes": {"type": "integer"},
        }, ["query", "minutes"]),
    },
    {
        "name": FINISH_TOOL,
        "description": "Finish the investigation. Call exactly once, with evidence for every claim.",
        "input_schema": _schema({
            "summary": {"type": "string", "description": "One sentence: what is broken and why."},
            "root_cause": {"type": "string"},
            "evidence": {"type": "array", "items": _schema({
                "source": {"type": "string", "enum": ["kubectl", "prometheus", "loki"]},
                "query": {"type": "string", "description": "the exact command or query you ran"},
                "finding": {"type": "string", "description": "what it showed"},
            }, ["source", "query", "finding"])},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "suggested_runbook": {"type": "string", "description": "runbook id (usually the ticket's runbook_hint), or empty"},
            "next_steps": {"type": "array", "items": {"type": "string"}},
        }, ["summary", "root_cause", "evidence", "confidence", "suggested_runbook", "next_steps"]),
    },
]


class Toolbox:
    def __init__(self, kubectl: Kubectl, prometheus: Prometheus, loki: Loki):
        self.kubectl, self.prometheus, self.loki = kubectl, prometheus, loki

    def dispatch(self, name: str, args: dict) -> tuple[str, bool]:
        """Run one tool call. Returns (output, is_error); never raises, so the loop can continue."""
        try:
            if name == "kubectl":
                code, out = self.kubectl.run(args["cluster"], list(args["args"]))
                return out or f"(exit {code}, no output)", code != 0
            if name == "prometheus_query":
                return self.prometheus.query(args["query"]), False
            if name == "loki_query":
                return self.loki.query(args["query"], int(args.get("minutes", 30))), False
            return f"Error: unknown tool '{name}'", True
        except KubectlRefused as exc:
            return f"Refused: {exc}", True
        except (KeyError, TypeError, ValueError) as exc:
            return f"Error: bad arguments for {name}: {exc}", True
        except Exception as exc:  # network errors etc. go back to the model as data
            return f"Error: {name} failed: {exc}", True
