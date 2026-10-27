"""LLM providers behind one small interface, tried in order (CLAUDE.md: the agent must not depend
solely on infrastructure it heals).

Each provider owns its own tool loop, because OpenAI-style and Anthropic messages differ; the
diagnosis code only sees `Provider.run()`. The default chain is intellect-3 on the MI300X, then
model A on the A100/A6000, then Claude through the Anthropic API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Protocol

log = logging.getLogger("ops-agent.llm")

# A tool the model may call: name, description, JSON-schema parameters (strict: required +
# additionalProperties false, so the same schema works for both providers).
ToolSpec = dict
# Runs a tool: (name, arguments) -> (output text, is_error).
Dispatch = Callable[[str, dict], "tuple[str, bool]"]


class ProviderUnavailable(Exception):
    """The endpoint can't be reached or failed (connection, timeout, 5xx, auth): try the next one."""


class BadAnswer(Exception):
    """The model answered but never produced a usable final tool call: try the next one."""


@dataclass
class LoopResult:
    final_args: dict  # arguments of the finishing tool call (submit_diagnosis)
    provider: str  # which provider produced it, e.g. "intellect-3"
    tool_log: list[dict] = field(default_factory=list)  # {"tool", "args", "is_error", "chars"}
    turns: int = 0


class Provider(Protocol):
    name: str

    def run(self, system: str, user: str, tools: list[ToolSpec], dispatch: Dispatch,
            finish_tool: str, max_turns: int) -> LoopResult: ...


def run_chain(providers: list[Provider], system: str, user: str, tools: list[ToolSpec],
              dispatch: Dispatch, finish_tool: str, max_turns: int) -> tuple[LoopResult, list[str]]:
    """Try each provider in order. Returns the first result and the failures before it."""
    failures: list[str] = []
    for provider in providers:
        try:
            return provider.run(system, user, tools, dispatch, finish_tool, max_turns), failures
        except (ProviderUnavailable, BadAnswer) as exc:
            failures.append(f"{provider.name}: {type(exc).__name__}: {exc}")
            log.warning("provider %s failed, trying the next one: %s", provider.name, exc)
    raise ProviderUnavailable("every provider failed: " + " | ".join(failures))
