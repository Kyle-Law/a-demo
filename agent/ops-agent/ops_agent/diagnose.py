"""One ticket -> one root-cause analysis, using the LLM chain and read-only tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .llm import LoopResult, Provider, run_chain
from .prompts import SYSTEM_PROMPT
from .tools import FINISH_TOOL, TOOL_SPECS, Toolbox


@dataclass
class Diagnosis:
    ticket: str
    alerts: list[dict]
    result: LoopResult
    provider_failures: list[str] = field(default_factory=list)

    @property
    def findings(self) -> dict:
        return self.result.final_args


def ticket_prompt(key: str, summary: str, alerts: list[dict]) -> str:
    """The user turn: the ticket contract from every alert on the ticket, as data."""
    contracts = [a.get("ticket") or {k: a.get("labels", {}).get(k) for k in
                 ("cluster", "namespace", "resource", "signal", "severity")} for a in alerts]
    return (f"Ticket {key}: {summary}\n\n"
            "Alerts on this ticket (alert -> ticket contract; the first one opened it, later ones were "
            "correlated onto the same resource):\n"
            f"{json.dumps(contracts, indent=2)}\n\n"
            "Investigate and call submit_diagnosis.")


def diagnose(key: str, summary: str, alerts: list[dict], providers: list[Provider], toolbox: Toolbox,
             max_turns: int = 20) -> Diagnosis:
    result, failures = run_chain(providers, SYSTEM_PROMPT, ticket_prompt(key, summary, alerts),
                                 TOOL_SPECS, toolbox.dispatch, FINISH_TOOL, max_turns)
    return Diagnosis(ticket=key, alerts=alerts, result=result, provider_failures=failures)


def format_comment(d: Diagnosis) -> str:
    """Jira wiki markup for the findings comment."""
    f = d.findings
    lines = [
        f"h3. Root-cause analysis ({f.get('confidence', '?')} confidence)",
        "",
        f"*Summary:* {f.get('summary', '-')}",
        "",
        f"*Root cause:* {f.get('root_cause', '-')}",
        "",
        "h4. Evidence",
    ]
    for e in f.get("evidence", []):
        lines.append(f"* *{e.get('source')}* {{{{{e.get('query', '')}}}}}: {e.get('finding', '')}")
    if f.get("next_steps"):
        lines += ["", "h4. Next steps"] + [f"# {s}" for s in f["next_steps"]]
    lines += ["", f"*Suggested runbook:* {f.get('suggested_runbook') or '-'}", ""]
    lines.append(f"_Read-only analysis by the ops agent using {d.result.provider}, "
                 f"{len(d.result.tool_log)} tool calls in {d.result.turns} turns._")
    if d.provider_failures:
        lines.append("_Tried first: " + "; ".join(d.provider_failures) + "_")
    return "\n".join(lines)
