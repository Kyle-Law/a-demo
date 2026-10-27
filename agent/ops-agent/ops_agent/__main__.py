"""ops-agent CLI.

  python -m ops_agent run              poll Jira forever (the Deployment's command)
  python -m ops_agent once             one pass over the triage queue
  python -m ops_agent diagnose KEY     investigate one ticket and print the findings; writes nothing
"""

from __future__ import annotations

import logging
import sys

from .config import from_env
from .diagnose import diagnose, format_comment
from .jira import Jira
from .tools import Kubectl, Loki, Prometheus, Toolbox
from .workflow import run_forever, run_once


def build_providers(cfg):
    providers = []
    for entry in cfg.llm_chain:
        if entry.kind == "openai":
            from .llm.openai_compat import OpenAICompatProvider
            providers.append(OpenAICompatProvider(name=entry.model, model=entry.model, base_url=entry.base_url))
        else:
            from .llm.anthropic_fallback import AnthropicProvider
            providers.append(AnthropicProvider(model=entry.model))
    return providers


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not argv or argv[0] not in {"run", "once", "diagnose"}:
        print(__doc__)
        return 2
    cfg = from_env()
    jira = Jira(cfg.jira_api_url, cfg.jira_email, cfg.jira_api_token, cfg.jira_project)
    toolbox = Toolbox(Kubectl(cfg.kubeconfig_dir, cfg.clusters), Prometheus(cfg.prometheus_url), Loki(cfg.loki_url))
    providers = build_providers(cfg)
    if argv[0] == "run":
        run_forever(jira, providers, toolbox, cfg)
    elif argv[0] == "once":
        print(run_once(jira, providers, toolbox, cfg))
    else:
        key = argv[1]
        issue = jira.get_issue(key)
        d = diagnose(key, issue["fields"].get("summary", ""), jira.extract_alerts(issue), providers, toolbox, cfg.max_turns)
        print(format_comment(d))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
