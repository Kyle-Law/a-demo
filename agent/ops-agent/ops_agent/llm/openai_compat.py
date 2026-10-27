"""OpenAI-compatible provider: the vLLM model servers behind the inference gateway (intellect-3 on
the MI300X, model A on the A100 MIG slices and the A6000). Uses the openai SDK; vLLM serves the
same /v1/chat/completions API with tool calling (--enable-auto-tool-choice)."""

from __future__ import annotations

import json
import logging

import openai

from . import BadAnswer, Dispatch, LoopResult, ProviderUnavailable, ToolSpec

log = logging.getLogger("ops-agent.llm.openai")


class OpenAICompatProvider:
    def __init__(self, name: str, model: str, base_url: str, api_key: str = "unused",
                 max_tokens: int = 12_000, timeout: float = 600.0, client=None):
        self.name = name
        self.model = model
        # intellect-3 reasons before every answer (a short question took 2.4k tokens on the
        # MI300X), so leave room: too small and the answer arrives empty.
        self.max_tokens = max_tokens
        self.client = client or openai.OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=1)

    def run(self, system: str, user: str, tools: list[ToolSpec], dispatch: Dispatch,
            finish_tool: str, max_turns: int) -> LoopResult:
        oa_tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                      "parameters": t["input_schema"]}} for t in tools]
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        result = LoopResult(final_args={}, provider=self.name)
        nudged = False
        for turn in range(1, max_turns + 1):
            result.turns = turn
            try:
                resp = self.client.chat.completions.create(
                    model=self.model, messages=messages, tools=oa_tools, tool_choice="auto",
                    max_tokens=self.max_tokens)
            except (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError) as exc:
                raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc
            except openai.APIStatusError as exc:
                raise ProviderUnavailable(f"HTTP {exc.status_code}: {exc.message}") from exc

            msg = resp.choices[0].message
            calls = msg.tool_calls or []
            if not calls:
                # Answered in prose instead of calling a tool: ask once for the finishing call.
                if nudged:
                    raise BadAnswer(f"no {finish_tool} call after a reminder")
                nudged = True
                messages.append({"role": "assistant", "content": msg.content or ""})
                messages.append({"role": "user", "content": f"Finish by calling {finish_tool} with your findings."})
                continue

            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [c.model_dump() for c in calls]})
            for call in calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    output, is_error = "Error: arguments were not valid JSON; call the tool again.", True
                    args = {}
                else:
                    if call.function.name == finish_tool:
                        result.final_args = args
                        return result
                    output, is_error = dispatch(call.function.name, args)
                result.tool_log.append({"tool": call.function.name, "args": args,
                                        "is_error": is_error, "chars": len(output)})
                messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
        raise BadAnswer(f"no {finish_tool} call within {max_turns} turns")
