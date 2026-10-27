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


def schema_problems(schema: dict, value, path: str = "") -> list[str]:
    """Check value against the small JSON-schema subset our tools use (object/array/string/integer,
    required, enum, additionalProperties: false). vLLM doesn't enforce tool schemas the way Claude's
    strict mode does: intellect-3 wrote evidence items with "arguments" instead of "query"."""
    where = path or "arguments"
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return [f"{where} must be an object"]
        problems = [f"{where}.{k} is required" for k in schema.get("required", []) if k not in value]
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            problems += [f"{where}.{k} is not allowed (allowed: {', '.join(props)})" for k in value if k not in props]
        for k, sub_schema in props.items():
            if k in value:
                problems += schema_problems(sub_schema, value[k], f"{where}.{k}")
        return problems
    if kind == "array":
        if not isinstance(value, list):
            return [f"{where} must be an array"]
        return [p for i, item in enumerate(value) for p in schema_problems(schema.get("items", {}), item, f"{where}[{i}]")]
    if kind == "string" and not isinstance(value, str):
        return [f"{where} must be a string"]
    if kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        return [f"{where} must be an integer"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{where} must be one of {schema['enum']}"]
    return []


def finish_problems(tools: list[ToolSpec], finish_tool: str, args: dict) -> str:
    """"" if the finishing call is valid, else a message telling the model what to fix."""
    schema = next(t["input_schema"] for t in tools if t["name"] == finish_tool)
    problems = schema_problems(schema, args)
    if not problems:
        return ""
    return f"Error: {finish_tool} was not accepted. Fix these and call it again: " + "; ".join(problems[:10])


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
