"""AGENT_LLM_CHAIN parsing."""

import pytest

from ops_agent.config import DEFAULT_LLM_CHAIN, parse_chain


def test_default_chain_order():
    assert [(e.kind, e.model) for e in parse_chain(DEFAULT_LLM_CHAIN)] == [
        ("openai", "intellect-3"), ("openai", "model-a"), ("anthropic", "claude-opus-5-5")]


def test_labels_tell_the_same_model_apart():
    chain = parse_chain("openai|model-a|http://172.30.1.10:30080/v1|model-a-c1,"
                        "openai|model-a|http://172.30.2.10:30080/v1|model-a-c2")
    assert [(e.label, e.base_url) for e in chain] == [("model-a-c1", "http://172.30.1.10:30080/v1"),
                                                       ("model-a-c2", "http://172.30.2.10:30080/v1")]
    assert parse_chain("openai|m|http://x/v1")[0].label == "m"


@pytest.mark.parametrize("bad", ["openai|m", "anthropic", "ollama|m|http://x", "anthropic|m|extra"])
def test_bad_entries_rejected(bad):
    with pytest.raises(ValueError):
        parse_chain(bad)
