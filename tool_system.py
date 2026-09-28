"""Aura tool registry and bounded agent runtime.

The registry keeps tool declarations/execution separate from the Telegram bot.
AgentRunner owns the manual Gemini function-calling loop so Aura can safely
perform multiple tool steps without adding a second planner-model call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from typing import Any, Callable, Optional

from google.genai import types

logger = logging.getLogger("aura-tools")


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    declaration: types.FunctionDeclaration
    handler: Callable[[dict], Any]
    max_calls_per_run: int = 3


@dataclass
class AgentRunState:
    rounds: int = 0
    tool_calls: int = 0
    events: list[dict] = field(default_factory=list)
    call_counts: dict[str, int] = field(default_factory=dict)
    result_cache: dict[str, Any] = field(default_factory=dict)
    runtime_context: dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        name: str,
        declaration: types.FunctionDeclaration,
        handler: Callable[[dict], Any],
        *,
        max_calls_per_run: int = 3,
    ) -> None:
        if not name or name in self._tools:
            raise ValueError(f"Invalid or duplicate tool name: {name!r}")
        if max_calls_per_run < 1:
            raise ValueError("max_calls_per_run must be at least 1")
        self._tools[name] = ToolDefinition(name, declaration, handler, max_calls_per_run)

    def declarations(self) -> list[types.FunctionDeclaration]:
        return [tool.declaration for tool in self._tools.values()]

    def execute(self, name: str, args: dict) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"Unknown tool: {name}"}
        return tool.handler(args)

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)


class AgentRunner:
    """Run one bounded, model-driven tool task.

    This is deliberately not a second LLM planner. Gemini remains the planner
    and final answerer through function calling; Aura owns execution, budgets,
    duplicate prevention, logging, and failure isolation.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_rounds: int = 4,
        max_tool_calls: int = 8,
    ) -> None:
        if max_rounds < 1 or max_tool_calls < 1:
            raise ValueError("Agent limits must be positive")
        self.registry = registry
        self.max_rounds = max_rounds
        self.max_tool_calls = max_tool_calls

    @staticmethod
    def _signature(name: str, args: dict) -> str:
        try:
            return name + ":" + json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except TypeError:
            return name + ":" + repr(sorted(args.items()))

    def execute_tool(self, state: AgentRunState, name: str, args: dict) -> Any:
        tool = self.registry.get(name)
        if tool is None:
            result = {"ok": False, "error": f"Unknown tool: {name}"}
            state.events.append({"tool": name, "result": result})
            return result

        count = state.call_counts.get(name, 0)
        if count >= tool.max_calls_per_run:
            result = {
                "ok": False,
                "error": f"Tool call limit reached for {name} in this task.",
            }
            logger.warning("Agent tool budget reached | name=%s", name)
            state.events.append({"tool": name, "result": result})
            return result

        signature = self._signature(name, args)
        if signature in state.result_cache:
            result = state.result_cache[signature]
            logger.info("Agent duplicate tool call avoided | name=%s", name)
            state.events.append({"tool": name, "result": result, "cached": True})
            return result

        if state.tool_calls >= self.max_tool_calls:
            result = {"ok": False, "error": "Agent tool-call budget reached for this task."}
            logger.warning("Agent total tool budget reached | calls=%s", state.tool_calls)
            state.events.append({"tool": name, "result": result})
            return result

        state.tool_calls += 1
        state.call_counts[name] = count + 1
        logger.info(
            "Agent tool call | name=%s | call=%s | round=%s | args=%s",
            name, state.tool_calls, state.rounds + 1, args,
        )
        handler_args = dict(args)
        if state.runtime_context:
            # Runtime context is injected by Aura, never exposed in Gemini's tool schema.
            handler_args.update({f"_{key}": value for key, value in state.runtime_context.items()})
        try:
            result = self.registry.execute(name, handler_args)
        except Exception as exc:
            logger.exception("Agent tool execution failed | name=%s", name)
            result = {"ok": False, "error": str(exc)[:500]}

        state.result_cache[signature] = result
        state.events.append({"tool": name, "result": result})
        return result
