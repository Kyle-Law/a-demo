"""Claude through the Anthropic API: the last link of the chain, so the agent still works when the
GPU clusters it is diagnosing are the thing that's broken. Anthropic SDK only."""

from __future__ import annotations

import logging

import anthropic

from . import BadAnswer, Dispatch, LoopResult, ProviderUnavailable, ToolSpec

log = logging.getLogger("ops-agent.llm.anthropic")

DEFAULT_MODEL = "claude-opus-5-5"


class AnthropicProvider:
    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "high", max_tokens: int = 16_000,
                 client: anthropic.Anthropic | None = None):
        self.name = f"anthropic:{model}"
        self.model = model
        # Root-cause analysis is intelligence-sensitive; Claude Opus 5.5 defaults to "medium".
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = client or anthropic.Anthropic()  # ANTHROPIC_API_KEY from the environment

    def run(self, system: str, user: str, tools: list[ToolSpec], dispatch: Dispatch,
            finish_tool: str, max_turns: int) -> LoopResult:
        # strict: true keeps tool inputs schema-valid; forcing a tool (tool_choice any/tool) is a
        # 400 on this model, so the system prompt asks for the finishing call instead.
        claude_tools = [{**t, "strict": True} for t in tools]
        messages: list[dict] = [{"role": "user", "content": user}]
        result = LoopResult(final_args={}, provider=self.name)
        nudged = False
        for turn in range(1, max_turns + 1):
            result.turns = turn
            try:
                resp = self.client.beta.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    tools=claude_tools,
                    messages=messages,
                    output_config={"effort": self.effort},
                    # On a safety decline, re-run on a fallback model chosen by category.
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except (anthropic.APIConnectionError, anthropic.RateLimitError) as exc:
                raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc
            except anthropic.AuthenticationError as exc:
                raise ProviderUnavailable(f"authentication failed: {exc.message}") from exc
            except anthropic.APIStatusError as exc:
                raise ProviderUnavailable(f"HTTP {exc.status_code}: {exc.message}") from exc

            if resp.stop_reason == "refusal":
                raise BadAnswer(f"refused ({resp.stop_details.category if resp.stop_details else 'no category'})")
            if resp.stop_reason == "max_tokens":
                raise BadAnswer(f"hit max_tokens={self.max_tokens}")

            # Append the full content (thinking blocks included, unchanged) before tool results.
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "pause_turn":
                continue

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                if nudged:
                    raise BadAnswer(f"no {finish_tool} call after a reminder")
                nudged = True
                messages.append({"role": "user", "content": f"Finish by calling {finish_tool} with your findings."})
                continue

            results = []
            for block in tool_uses:
                if block.name == finish_tool:
                    result.final_args = dict(block.input)
                    return result
                output, is_error = dispatch(block.name, dict(block.input))
                result.tool_log.append({"tool": block.name, "args": dict(block.input),
                                        "is_error": is_error, "chars": len(output)})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output, "is_error": is_error})
            # All results of one turn go back in a single user message.
            messages.append({"role": "user", "content": results})
        raise BadAnswer(f"no {finish_tool} call within {max_turns} turns")
