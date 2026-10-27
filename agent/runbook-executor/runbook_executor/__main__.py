"""runbook-executor CLI.

  python -m runbook_executor run          poll Jira forever (the Deployment's command)
  python -m runbook_executor once         one pass over the queue
  python -m runbook_executor plan KEY     which runbook would run for a ticket, and its preconditions now;
                                          writes nothing and mints no credentials
  python -m runbook_executor runbooks     the runbooks at the head of the runbooks repo, and any invalid ones
"""

from __future__ import annotations

import json
import logging
import sys

from . import checks
from .actions import Clusters
from .config import from_env
from .jira import Jira
from .runbooks import RunbookSource, render, select, ticket_context
from .vault import Vault
from .workflow import Executor, run_forever


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not argv or argv[0] not in {"run", "once", "plan", "runbooks"} or (argv[0] == "plan" and len(argv) < 2):
        print(__doc__)
        return 2
    cfg = from_env()
    source = RunbookSource(cfg.runbooks_repo, cfg.runbooks_ref)
    if argv[0] == "runbooks":
        snap = source.refresh()
        print(json.dumps({"sha": snap.sha, "runbooks": sorted(snap.runbooks), "invalid": snap.invalid}, indent=2))
        return 1 if snap.invalid else 0
    jira = Jira(cfg.jira_api_url, cfg.jira_email, cfg.jira_api_token, cfg.jira_project)
    prom = checks.Prometheus(cfg.prometheus_url)
    if argv[0] == "plan":
        alerts = Jira.extract_alerts(jira.get_issue(argv[1]))
        if not alerts:
            print("no alert-bridge payload on this ticket")
            return 1
        ctx = ticket_context(alerts[0])
        rb, why = select(source.refresh(), ctx)
        print(f"ticket: {ctx}\nselected: {rb.id + '@' + rb.sha[:7] if rb else None} ({why})")
        if rb:
            print("\n".join(checks.evaluate_all(prom, render(rb.doc["preconditions"], ctx))[1]))
        return 0
    executor = Executor(jira, source, prom, Vault(cfg.vault_addr, cfg.vault_role, cfg.vault_jwt_path),
                        Clusters(cfg.clusters_dir), cfg)
    if argv[0] == "run":
        run_forever(executor, cfg)
    else:
        source.refresh()
        print(executor.run_once())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
