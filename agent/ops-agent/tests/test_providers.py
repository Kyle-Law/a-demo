"""Both providers drive their real SDK against a fake server: tool call -> tool result -> finish."""

import json

import pytest

from conftest import echo_dispatch
from ops_agent.llm import BadAnswer, ProviderUnavailable, run_chain
from ops_agent.llm.anthropic_fallback import AnthropicProvider
from ops_agent.llm.openai_compat import OpenAICompatProvider
from ops_agent.tools import FINISH_TOOL, TOOL_SPECS

FINAL = {"summary": "s", "root_cause": "r", "evidence": [], "confidence": "high",
         "suggested_runbook": "", "next_steps": []}


def openai_reply(tool_name=None, args=None, content=None):
    msg = {"role": "assistant", "content": content}
    if tool_name:
        msg["tool_calls"] = [{"id": f"call_{tool_name}", "type": "function",
                              "function": {"name": tool_name, "arguments": json.dumps(args)}}]
    return {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_name else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def test_openai_compat_tool_loop(fake_server):
    script = [openai_reply("kubectl", {"cluster": "c1", "args": ["get", "pods"]}),
              openai_reply(FINISH_TOOL, FINAL)]
    srv = fake_server(lambda m, p, b: (200, script.pop(0)))
    calls = []
    p = OpenAICompatProvider("intellect-3", "intellect-3", base_url=f"{srv.url}/v1")
    res = p.run("sys", "user", TOOL_SPECS, echo_dispatch(calls), FINISH_TOOL, 5)
    assert res.final_args == FINAL and res.provider == "intellect-3"
    assert calls == [("kubectl", {"cluster": "c1", "args": ["get", "pods"]})]
    second = srv.requests[1]["body"]
    assert second["model"] == "intellect-3" and second["tool_choice"] == "auto"
    assert {t["function"]["name"] for t in second["tools"]} >= {"kubectl", FINISH_TOOL}
    assert second["messages"][-1] == {"role": "tool", "tool_call_id": "call_kubectl", "content": "ran kubectl"}


def test_openai_compat_prose_answer_is_nudged_then_bad(fake_server):
    srv = fake_server(lambda m, p, b: (200, openai_reply(content="I think the node is down.")))
    with pytest.raises(BadAnswer):
        OpenAICompatProvider("m", "m", base_url=f"{srv.url}/v1").run("s", "u", TOOL_SPECS, echo_dispatch([]), FINISH_TOOL, 5)
    assert "Finish by calling submit_diagnosis" in srv.requests[1]["body"]["messages"][-1]["content"]


def test_openai_compat_unreachable_is_unavailable():
    p = OpenAICompatProvider("m", "m", base_url="http://127.0.0.1:9/v1", timeout=2)
    with pytest.raises(ProviderUnavailable):
        p.run("s", "u", TOOL_SPECS, echo_dispatch([]), FINISH_TOOL, 3)


def anthropic_reply(tool_name=None, args=None, stop="tool_use"):
    content = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    if tool_name:
        content.append({"type": "tool_use", "id": f"tu_{tool_name}", "name": tool_name, "input": args})
    else:
        content.append({"type": "text", "text": "done"})
    return {"id": "msg", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}}


def test_anthropic_haiku_default_has_no_server_fallback(fake_server):
    """Claude Haiku 5.5 (the demo default) has no server-side refusal fallback: plain messages.create."""
    srv = fake_server(lambda m, p, b: (200, anthropic_reply(FINISH_TOOL, FINAL)))
    import anthropic
    client = anthropic.Anthropic(api_key="test", base_url=srv.url, max_retries=0)
    res = AnthropicProvider(client=client).run("s", "u", TOOL_SPECS, echo_dispatch([]), FINISH_TOOL, 3)
    body, headers = srv.requests[0]["body"], srv.requests[0]["headers"]
    assert res.provider == "anthropic:claude-haiku-5-5" and body["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in body and "anthropic-beta" not in headers
    assert body["output_config"] == {"effort": "high"} and "tool_choice" not in body


def test_anthropic_tool_loop_request_shape(fake_server, monkeypatch):
    script = [anthropic_reply("prometheus_query", {"query": "up"}), anthropic_reply(FINISH_TOOL, FINAL)]
    srv = fake_server(lambda m, p, b: (200, script.pop(0)))
    import anthropic
    client = anthropic.Anthropic(api_key="test", base_url=srv.url, max_retries=0)
    calls = []
    res = AnthropicProvider(model="claude-opus-5-5", client=client).run("sys", "user", TOOL_SPECS, echo_dispatch(calls), FINISH_TOOL, 5)
    assert res.final_args == FINAL and res.provider == "anthropic:claude-opus-5-5"
    assert calls == [("prometheus_query", {"query": "up"})]
    first, second = srv.requests[0], srv.requests[1]
    body = first["body"]
    assert body["model"] == "claude-opus-5-5" and body["output_config"] == {"effort": "high"}
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in first["headers"]["anthropic-beta"]
    assert "tool_choice" not in body and all(t["strict"] is True for t in body["tools"])
    # thinking block passed back unchanged; the tool result goes back in one user turn
    assert second["body"]["messages"][1]["content"][0]["type"] == "thinking"
    assert second["body"]["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu_prometheus_query",
                                                          "content": "ran prometheus_query", "is_error": False}


def test_anthropic_server_error_is_unavailable(fake_server):
    srv = fake_server(lambda m, p, b: (529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}))
    import anthropic
    client = anthropic.Anthropic(api_key="test", base_url=srv.url, max_retries=0)
    with pytest.raises(ProviderUnavailable):
        AnthropicProvider(client=client).run("s", "u", TOOL_SPECS, echo_dispatch([]), FINISH_TOOL, 3)


class Scripted:
    def __init__(self, name, outcome):
        self.name, self.outcome = name, outcome

    def run(self, *a):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_chain_falls_through_in_order():
    from ops_agent.llm import LoopResult
    ok = LoopResult(final_args=FINAL, provider="anthropic")
    res, failures = run_chain([Scripted("intellect-3", ProviderUnavailable("down")),
                               Scripted("model-a", BadAnswer("no finish")),
                               Scripted("anthropic", ok)], "s", "u", TOOL_SPECS, None, FINISH_TOOL, 5)
    assert res is ok and [f.split(":")[0] for f in failures] == ["intellect-3", "model-a"]
    with pytest.raises(ProviderUnavailable, match="every provider failed"):
        run_chain([Scripted("a", ProviderUnavailable("x"))], "s", "u", TOOL_SPECS, None, FINISH_TOOL, 5)


def test_invalid_finish_is_sent_back_for_correction(fake_server):
    """intellect-3 on the MI300X wrote evidence with "arguments" instead of "query" (vLLM doesn't
    enforce tool schemas). The provider rejects it with the problems and accepts the corrected call."""
    bad = {**FINAL, "evidence": [{"source": "kubectl", "arguments": ["logs", "x"], "finding": "f"}]}
    good = {**FINAL, "evidence": [{"source": "kubectl", "query": "kubectl logs x", "finding": "f"}]}
    script = [openai_reply(FINISH_TOOL, bad), openai_reply(FINISH_TOOL, good)]
    srv = fake_server(lambda m, p, b: (200, script.pop(0)))
    res = OpenAICompatProvider("m", "m", base_url=f"{srv.url}/v1").run("s", "u", TOOL_SPECS, echo_dispatch([]), FINISH_TOOL, 5)
    assert res.final_args == good
    feedback = srv.requests[1]["body"]["messages"][-1]["content"]
    assert "evidence[0].query is required" in feedback and "evidence[0].arguments is not allowed" in feedback
