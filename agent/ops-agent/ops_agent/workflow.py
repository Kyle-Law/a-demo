"""The triage loop: poll Jira, diagnose each new ticket once, post the findings."""

from __future__ import annotations

import logging
import re
import time

from .diagnose import diagnose, format_comment
from .jira import Jira
from .llm import Provider, ProviderUnavailable
from .tools import Toolbox

log = logging.getLogger("ops-agent")


def triage_one(jira: Jira, key: str, providers: list[Provider], toolbox: Toolbox, cfg) -> str:
    """Diagnose one ticket and post the result. Returns "triaged", "failed" or "skipped"."""
    issue = jira.get_issue(key)
    alerts = jira.extract_alerts(issue)
    if not alerts:
        log.warning("%s has no ALERT_JSON block; not an alert-bridge ticket, skipping", key)
        jira.add_labels(key, [cfg.failed_label])
        return "skipped"
    summary = issue.get("fields", {}).get("summary", "")
    try:
        d = diagnose(key, summary, alerts, providers, toolbox, cfg.max_turns)
    except ProviderUnavailable as exc:
        # Every LLM failed. Say so once and stop retrying this ticket; a human takes over.
        jira.comment(key, f"(x) *Ops agent could not complete a diagnosis.* {exc}")
        jira.add_labels(key, [cfg.done_label, cfg.failed_label])
        return "failed"
    jira.comment(key, format_comment(d))
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", d.result.provider)
    jira.add_labels(key, [cfg.done_label, f"rca-by-{slug}"])
    log.info("triaged %s with %s (%d tool calls)", key, d.result.provider, len(d.result.tool_log))
    return "triaged"


def run_once(jira: Jira, providers: list[Provider], toolbox: Toolbox, cfg) -> dict[str, str]:
    outcomes = {}
    for issue in jira.triage_queue(cfg.triage_label, cfg.done_label):
        key = issue["key"]
        try:
            outcomes[key] = triage_one(jira, key, providers, toolbox, cfg)
        except Exception as exc:  # one bad ticket must not stop the queue
            log.exception("triage of %s failed: %s", key, exc)
            outcomes[key] = "error"
    return outcomes


def run_forever(jira: Jira, providers: list[Provider], toolbox: Toolbox, cfg) -> None:
    while True:
        try:
            outcomes = run_once(jira, providers, toolbox, cfg)
            if outcomes:
                log.info("pass done: %s", outcomes)
        except Exception as exc:  # Jira down etc.: keep polling
            log.exception("poll failed: %s", exc)
        time.sleep(cfg.poll_seconds)
