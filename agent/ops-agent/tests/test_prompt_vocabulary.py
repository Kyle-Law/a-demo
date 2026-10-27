"""The system prompt must name every metric the ticket rules alert on: intellect-3 invented a
`gpu` label when it wasn't told the real ones (MI300X test, 2026-10-27)."""

import re
from pathlib import Path

import yaml

from ops_agent.prompts import SYSTEM_PROMPT

RULES = Path(__file__).resolve().parents[3] / "platform/observability/rules/ticket-rules.yaml"


def test_prompt_names_every_alerted_metric():
    exprs = " ".join(r["expr"] for g in yaml.safe_load(RULES.read_text())["spec"]["groups"] for r in g["rules"])
    metrics = set(re.findall(r"\b((?:kube|vllm|DCGM)[A-Za-z0-9_:]+)\{", exprs))
    assert metrics, "found no metrics in the ticket rules"
    missing = sorted(m for m in metrics if m not in SYSTEM_PROMPT)
    assert not missing, f"prompt doesn't mention {missing}"


def test_prompt_says_there_is_no_gpu_label():
    assert "There is no `gpu` label" in SYSTEM_PROMPT
