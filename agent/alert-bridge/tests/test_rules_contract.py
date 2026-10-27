"""Every alert that opens a ticket must satisfy the alert -> ticket contract (CLAUDE.md), so the
bridge never refuses it, and must have a chaos script that triggers it (B13 "each fault yields
exactly one ticket"). Static checks; the expressions themselves are exercised on real clusters.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
RULE_FILES = sorted((ROOT / "platform/observability/rules").glob("*-rules.yaml"))


def ticket_rules():
    for f in RULE_FILES:
        for group in yaml.safe_load(f.read_text())["spec"]["groups"]:
            for rule in group["rules"]:
                if rule.get("labels", {}).get("ticket") == "true":
                    yield rule


def test_there_are_ticket_rules():
    assert RULE_FILES and len(list(ticket_rules())) >= 4


def test_contract_labels_and_hint():
    for rule in ticket_rules():
        labels, expr = rule["labels"], rule["expr"]
        assert labels["signal"] == rule["alert"], rule["alert"]
        assert labels["severity"] in {"critical", "warning", "info"}, rule["alert"]
        assert rule["annotations"].get("runbook_hint"), f"{rule['alert']}: runbook_hint"
        # resource and namespace come from the series: built with label_replace or kept by name.
        assert re.search(r'label_replace\([\s\S]*"resource"', expr), f"{rule['alert']}: resource"
        assert "namespace" in expr, f"{rule['alert']}: namespace"
        # Aggregations must keep cluster, or alerts from different clusters merge.
        for by in re.findall(r"by \(([^)]*)\)", expr):
            assert "cluster" in by, f"{rule['alert']}: aggregation drops cluster"


def test_every_ticket_rule_has_a_chaos_script():
    scripts = "\n".join(p.read_text() for p in (ROOT / "chaos").glob("*.sh"))
    for rule in ticket_rules():
        assert f"Expect {rule['alert']} for" in scripts, f"no chaos script triggers {rule['alert']}"


def test_chaos_scripts_have_revert_and_strict_mode():
    for p in (ROOT / "chaos").glob("*.sh"):
        if p.name == "lib.sh":
            continue
        text = p.read_text()
        assert "set -euo pipefail" in text and "--revert" in text, p.name
