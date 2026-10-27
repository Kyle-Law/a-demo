"""The executor loop: for each diagnosed ticket, select a runbook, ask a human, run it, verify, close.

A ticket's state lives in Jira labels, so a restart picks up where it left off:

  (none)            select a runbook and check its preconditions, then post the plan (and wait
                    for approval) or run it straight away when requires_approval is false
  runbook-proposed  waiting for /approve or /reject in a comment after the plan
  runbook-running   steps in progress; finding this label at the start of a pass means the
                    executor died mid-run, so it stops and asks a human instead of running twice
  runbook-succeeded verify passed; the ticket is closed
  runbook-failed    a step or verify failed and the rollback ran; a human takes over
  runbook-rejected  a human said no
  runbook-none      no runbook applies (or more than one does); a human takes over
  runbook-not-needed  preconditions no longer hold: the fault cleared on its own
"""

from __future__ import annotations

import json
import logging
import re
import time

from . import actions, checks
from .jira import Jira
from .runbooks import Runbook, RunbookSource, render, select, ticket_context
from .vault import RunCredentials, Vault

log = logging.getLogger("runbook-executor")

PROPOSED, RUNNING = "runbook-proposed", "runbook-running"
SUCCEEDED, FAILED, REJECTED, NONE, NOT_NEEDED = (
    "runbook-succeeded", "runbook-failed", "runbook-rejected", "runbook-none", "runbook-not-needed")
FINISHED = [SUCCEEDED, FAILED, REJECTED, NONE, NOT_NEEDED]

PLAN_RE = re.compile(r"RUNBOOK_PLAN_BEGIN\s*\{code(?::json)?\}(.*?)\{code\}\s*RUNBOOK_PLAN_END", re.S)
COMMAND_RE = re.compile(r"^\s*/(approve|reject)\b", re.I)


class Executor:
    def __init__(self, jira: Jira, source: RunbookSource, prom: checks.Prometheus, vault: Vault,
                 clusters: actions.Clusters, cfg, sleep=time.sleep, clock=time.monotonic):
        self.jira, self.source, self.prom, self.vault, self.clusters, self.cfg = jira, source, prom, vault, clusters, cfg
        self.sleep, self.clock = sleep, clock

    # ---- one pass over the queue ----

    def run_once(self) -> dict[str, str]:
        outcomes = {}
        for issue in self.jira.queue(self.cfg.ready_label, FINISHED):
            key = issue["key"]
            try:
                outcomes[key] = self.process(key)
            except Exception as exc:  # one bad ticket must not stop the queue
                log.exception("processing %s failed: %s", key, exc)
                outcomes[key] = "error"
        return outcomes

    def process(self, key: str) -> str:
        issue = self.jira.get_issue(key)
        labels = Jira.labels(issue)
        if labels & set(FINISHED):
            return "finished"
        if RUNNING in labels:
            self.jira.comment(key, "(x) *The runbook executor restarted while a runbook was running on this ticket.* "
                                   "It won't run it a second time; check the cluster and the steps above by hand.")
            self.jira.update_labels(key, add=[FAILED], remove=[RUNNING])
            return "interrupted"
        alerts = Jira.extract_alerts(issue)
        if not alerts:
            self._finish(key, NONE, "(i) No alert-bridge payload on this ticket, so no runbook applies.")
            return "none"
        try:
            ctx = ticket_context(alerts[0])
        except ValueError as exc:
            self._finish(key, NONE, f"(i) No runbook can run for this ticket: {exc}")
            return "none"
        if PROPOSED in labels:
            return self._await_approval(key, issue, ctx)
        return self._propose(key, ctx)

    # ---- states ----

    def _propose(self, key: str, ctx: dict) -> str:
        snap = self.source.head or self.source.refresh()
        rb, why = select(snap, ctx)
        if rb is None:
            self._finish(key, NONE, f"(i) *No runbook to run:* {why}. A human takes it from here.")
            return "none"
        ok, lines = checks.evaluate_all(self.prom, render(rb.doc["preconditions"], ctx))
        if not ok:
            self._finish(key, NOT_NEEDED, f"(/) Runbook *{rb.id}* matches, but its preconditions no longer hold, "
                                          "so nothing was run:\n" + "\n".join(lines))
            return "not-needed"
        if not rb.doc["requires_approval"]:
            return self._execute(key, rb, ctx, approved_by="no approval required")
        plan = {"runbook": rb.id, "sha": rb.sha}
        self.jira.comment(key, self._plan_text(rb, ctx, why, lines) + "\n\nRUNBOOK_PLAN_BEGIN\n{code:json}\n"
                          + json.dumps(plan) + "\n{code}\nRUNBOOK_PLAN_END")
        self.jira.update_labels(key, add=[PROPOSED])
        return "proposed"

    def _await_approval(self, key: str, issue: dict, ctx: dict) -> str:
        comments = Jira.comments(issue)
        plan_comment = next((c for c in reversed(comments) if PLAN_RE.search(c["body"])), None)
        if plan_comment is None:
            self.jira.update_labels(key, remove=[PROPOSED])  # plan lost: propose again next pass
            return "replan"
        plan = json.loads(PLAN_RE.search(plan_comment["body"]).group(1))
        me = self.jira.my_account_id()
        for c in comments:
            if c["id"] <= plan_comment["id"] or c["author"] == me:
                continue
            command = COMMAND_RE.match(c["body"])
            if not command:
                continue
            if self.cfg.approvers and c["author"] not in self.cfg.approvers:
                log.info("%s: ignoring /%s from %s (not an approver)", key, command.group(1), c["author_name"])
                continue
            who = c["author_name"] or c["author"]
            if command.group(1).lower() == "reject":
                self._finish(key, REJECTED, f"(x) Runbook *{plan['runbook']}* rejected by {who}; nothing was run.",
                             remove=[PROPOSED])
                return "rejected"
            rb = self.source.at(plan["sha"]).runbooks.get(plan["runbook"])
            if rb is None:
                self._finish(key, FAILED, f"(x) Runbook *{plan['runbook']}* is not valid at the approved commit "
                                          f"{plan['sha'][:7]}; nothing was run.", remove=[PROPOSED])
                return "failed"
            # Time has passed since the plan: check again that there is still something to fix.
            ok, lines = checks.evaluate_all(self.prom, render(rb.doc["preconditions"], ctx))
            if not ok:
                self._finish(key, NOT_NEEDED, f"(/) Approved by {who}, but the preconditions no longer hold, "
                                              "so nothing was run:\n" + "\n".join(lines), remove=[PROPOSED])
                return "not-needed"
            return self._execute(key, rb, ctx, approved_by=who, remove=[PROPOSED])
        return "waiting"

    def _execute(self, key: str, rb: Runbook, ctx: dict, approved_by: str, remove: list[str] = ()) -> str:
        self.jira.update_labels(key, add=[RUNNING], remove=list(remove))
        log.info("%s: running %s@%s (%s)", key, rb.id, rb.sha[:7], approved_by)
        report = [f"h4. Runbook {rb.id} @ {rb.sha[:7]} ({approved_by})"]
        ok, step_lines, issued = self._run_steps(key, rb, render(rb.doc["steps"], ctx), ctx)
        report += ["*Steps*"] + step_lines
        if ok:
            verified, verify_lines = checks.wait_until_verified(
                self.prom, render(rb.doc["verify"], ctx), self.cfg.verify_interval_seconds, sleep=self.sleep, clock=self.clock)
            report += ["*Verify*"] + verify_lines
            ok = verified
        report += [f"*Short-lived credentials (role {rb.doc['required_role']}, revoked after the run)*"] + \
                  [f"* {i}" for i in issued or ["none needed"]]
        if ok:
            self.jira.comment(key, "(/) *Fixed by runbook.*\n" + "\n".join(report))
            self.jira.update_labels(key, add=[SUCCEEDED], remove=[RUNNING])
            status = self.jira.close(key)
            log.info("%s: %s succeeded; ticket moved to %s", key, rb.id, status)
            return "succeeded"
        _, rollback_lines, _ = self._run_steps(key, rb, render(rb.doc["rollback"], ctx), ctx)
        report += ["*Rollback*"] + rollback_lines
        self.jira.comment(key, "(x) *Runbook did not fix it; rollback ran.* A human takes it from here.\n"
                          + "\n".join(report))
        self.jira.update_labels(key, add=[FAILED], remove=[RUNNING])
        return "failed"

    def _run_steps(self, key: str, rb: Runbook, steps: list[dict], ctx: dict) -> tuple[bool, list[str], list[str]]:
        """Runs steps in order and stops at the first failure. Returns (ok, report lines, tokens issued)."""
        lines = []
        with RunCredentials(self.vault, rb.doc["required_role"]) as creds:
            for n, step in enumerate(steps, 1):
                try:
                    where = actions.target(step, ctx)
                    kube = self.clusters.client(where[0], creds.token(*where)) if where else None
                    lines.append(f"# (/) {step['action']}: {actions.run(step, kube, lambda m: self.jira.comment(key, m))}")
                except Exception as exc:
                    lines.append(f"# (x) {step['action']}: {exc}")
                    lines += [f"# (-) {s['action']}: not run" for s in steps[n:]]
                    return False, lines, creds.issued
            return True, lines, creds.issued

    # ---- helpers ----

    def _plan_text(self, rb: Runbook, ctx: dict, why: str, precondition_lines: list[str]) -> str:
        steps = render(rb.doc["steps"], ctx)
        return "\n".join(
            [f"(?) *Runbook proposed: {rb.id}* — {rb.doc['title']}",
             f"Selected: {why}. Commit: {rb.sha[:7]} of the runbooks repo (merged, reviewed).",
             f"Runs with a short-lived token for role *{rb.doc['required_role']}*, minted by Vault for this run only.",
             "*Preconditions (checked now)*"] + precondition_lines +
            ["*Steps*"] + [f"# {s['action']} {json.dumps({k: v for k, v in s.items() if k != 'action'})}" for s in steps] +
            ["*Verify*"] + [f"* {c['name']} (within {c.get('within', '5m')})" for c in rb.doc["verify"]] +
            ["", "Reply */approve* to run it or */reject* to close this proposal."])

    def _finish(self, key: str, label: str, text: str, remove: list[str] = ()) -> None:
        self.jira.comment(key, text)
        self.jira.update_labels(key, add=[label], remove=list(remove))


def run_forever(executor: Executor, cfg) -> None:
    last_refresh = 0.0
    while True:
        try:
            if time.monotonic() - last_refresh >= cfg.runbooks_refresh_seconds:
                snap = executor.source.refresh()
                last_refresh = time.monotonic()
                log.info("runbooks at %s: %s; invalid: %s", snap.sha[:7], sorted(snap.runbooks), snap.invalid or "none")
            outcomes = executor.run_once()
            if outcomes:
                log.info("pass done: %s", outcomes)
        except Exception as exc:  # Jira, GitHub or Prometheus down: keep polling
            log.exception("poll failed: %s", exc)
        executor.sleep(cfg.poll_seconds)
