"""Aura tool registry and bounded agent runtime.

The registry keeps tool declarations/execution separate from the Telegram bot.
AgentRunner owns the manual Gemini function-calling loop so Aura can safely
perform multiple tool steps without adding a second planner-model call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
from typing import Any, Callable, Optional

from google.genai import types

logger = logging.getLogger("aura-tools")


# A model can emit an internal tool declaration as plain text rather than a
# Gemini FunctionCall part. It is not a tool result and must never be executed
# by parsing the string. Keep the signature narrow so ordinary mentions of
# tool names, JSON and code are not mistaken for a malformed tool call.
_TEXTUAL_TOOL_DECLARATION = re.compile(
    r"\bdeclaration\s*:\s*default_api\s*:\s*[a-z_][a-z0-9_]*\s*[{(]",
    re.IGNORECASE,
)


def _declaration_end(text: str, opening: int) -> Optional[int]:
    """Find the end of a balanced declaration solely for literal comparison."""
    pairs = {"{": "}", "(": ")", "[": "]"}
    stack: list[str] = []
    quote = ""
    escaped = False
    for i in range(opening, len(text)):
        char = text[i]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'":
            # Apostrophes in unquoted natural-language arguments ("can't") are
            # not the start of a quoted string.
            if char == "'" and i and i + 1 < len(text) and text[i - 1].isalnum() and text[i + 1].isalnum():
                continue
            quote = char
        elif char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values():
            if not stack or char != stack.pop():
                return None
            if not stack:
                return i + 1
    return None


def has_textual_tool_declaration(response: Any, *, user_text: str = "") -> bool:
    """Detect leaked tool-call syntax in the first candidate's visible text.

    Gemini may return text and actual function calls in separate parts. Inspect
    the visible text before executing any calls, excluding thought parts just
    like the SDK's ``response.text`` property. Reading the parts directly also
    avoids the SDK warning about non-text parts in a normal tool response.
    A complete declaration copied verbatim from the current user message is
    allowed so the bot can still discuss a snippet the user explicitly gave it.
    """
    candidates = getattr(response, "candidates", None)
    parts = None
    if isinstance(candidates, (list, tuple)) and candidates:
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None)
    if isinstance(parts, (list, tuple)):
        text = "".join(
            part.text for part in parts
            if isinstance(getattr(part, "text", None), str)
            and not getattr(part, "thought", False)
        )
    else:
        text = getattr(response, "text", None)
    return text_has_tool_declaration(text, user_text=user_text)


def text_has_tool_declaration(text: Any, *, user_text: str = "") -> bool:
    """Provider-independent leak check for plain text answers.

    Gemini gets this through function-call parts; OpenAI-compatible providers can
    only leak the same syntax as text, so the guard is shared.
    """
    if not isinstance(text, str):
        return False

    for match in _TEXTUAL_TOOL_DECLARATION.finditer(text):
        end = _declaration_end(text, match.end() - 1) if user_text else None
        if end is not None and text[match.start():end] in user_text:
            continue
        return True
    return False


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    declaration: types.FunctionDeclaration
    handler: Callable[[dict], Any]
    max_calls_per_run: int = 3
    # Non-deterministic tools (a random meme, a live price) must run again even
    # when the model repeats the exact same call; caching would replay stale data.
    cache_results: bool = True


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
        cache_results: bool = True,
    ) -> None:
        if not name or name in self._tools:
            raise ValueError(f"Invalid or duplicate tool name: {name!r}")
        if max_calls_per_run < 1:
            raise ValueError("max_calls_per_run must be at least 1")
        self._tools[name] = ToolDefinition(
            name, declaration, handler, max_calls_per_run, bool(cache_results)
        )

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
        if tool.cache_results and signature in state.result_cache:
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

        if tool.cache_results:
            state.result_cache[signature] = result
        state.events.append({"tool": name, "result": result})
        return result
