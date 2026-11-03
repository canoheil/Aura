"""Provider gateway: native Gemini plus any OpenAI-compatible endpoint.

Aura must not force a self-hoster into one vendor. This module resolves the
configured provider from `settings_store` and exposes three things:

* ``resolve_config()`` — what to call right now (provider, base URL, key, model)
* ``ask_agent()``      — one bounded tool-using turn, using the same
  ``ToolRegistry``/``AgentRunner`` the Gemini path already uses, so tools,
  budgets, duplicate protection and evidence checks are identical either way
* ``generate_text()``  — small helper calls (summaries, wording, prompt
  rewriting) for both providers

The OpenAI-compatible path speaks ``POST {base_url}/chat/completions`` with the
standard ``tools``/``tool_calls`` protocol. That covers OpenAI, OpenRouter,
Groq, DeepSeek, Together, Fireworks, vLLM, LM Studio and Ollama's
OpenAI-compatible endpoint.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import requests

import i18n
from answer_quality import has_any_web_evidence, has_web_evidence
from tool_system import AgentRunner, AgentRunState, ToolRegistry, text_has_tool_declaration

logger = logging.getLogger("aura-providers")

DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_RETRIES = 2

# Provider names accepted in settings. "gemini" is the native SDK path.
GEMINI = "gemini"
OPENAI_COMPAT = "openai_compat"
PROVIDERS = (GEMINI, OPENAI_COMPAT)


class ProviderError(RuntimeError):
    """A model call failed. Carries an HTTP status when one exists."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    model: str
    api_key: str
    base_url: str = ""
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES

    @property
    def is_gemini(self) -> bool:
        return self.name == GEMINI

    def fingerprint(self) -> str:
        """Identity of the connection settings, for cache invalidation."""
        return f"{self.name}|{self.model}|{self.base_url}|{self.timeout}|{len(self.api_key)}|{hash(self.api_key) & 0xffff:x}"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _no_spaces(value: str) -> bool:
    return bool(value) and not any(ch.isspace() for ch in value)


def _valid_base_url(value: str) -> bool:
    if not value.startswith(("http://", "https://")):
        return False
    try:
        parts = urlsplit(value)
        return bool(parts.hostname)
    except ValueError:
        return False


def resolve_config(settings, model_override: Optional[str] = None) -> ProviderConfig:
    """Build the active provider config from runtime settings.

    Raises ``ProviderError`` with an actionable message instead of letting a
    misconfiguration surface as an opaque 401 much later.
    """
    name = _clean(settings.get("llm_provider")).lower() or GEMINI
    if name not in PROVIDERS:
        name = GEMINI
    timeout = settings.get_int("llm_timeout_seconds")
    retries = settings.get_int("llm_max_retries")

    if name == GEMINI:
        key = _clean(settings.get("gemini_api_key"))
        model = _clean(model_override) or _clean(settings.get("gemini_model"))
        if not key:
            raise ProviderError("No Gemini API key is configured. Add one with /admin or in .env.")
        if not model:
            raise ProviderError("No Gemini model is configured.")
        return ProviderConfig(GEMINI, model, key, "", timeout, retries)

    key = _clean(settings.get("openai_api_key"))
    base_url = _clean(settings.get("openai_base_url")).rstrip("/")
    model = _clean(model_override) or _clean(settings.get("openai_model"))
    if not key:
        raise ProviderError("No OpenAI-compatible API key is configured. Add one with /admin or in .env.")
    if not base_url or not _valid_base_url(base_url):
        raise ProviderError("The OpenAI-compatible base URL is missing or invalid.")
    if not model:
        raise ProviderError("No OpenAI-compatible model is configured.")
    return ProviderConfig(OPENAI_COMPAT, model, key, base_url, timeout, retries)


def provider_status(settings) -> dict:
    """Cheap, non-network description used by the admin panel."""
    try:
        config = resolve_config(settings)
    except ProviderError as exc:
        config = None
        error = str(exc)
    else:
        error = ""
    return {
        "configured": config is not None,
        "provider": _clean(settings.get("llm_provider")) or GEMINI,
        "model": config.model if config else "",
        "base_url": config.base_url if config else "",
        "error": error,
    }


# ---------------------------------------------------------------------------
# HTTP transport for the OpenAI-compatible protocol
# ---------------------------------------------------------------------------

_session_lock = threading.Lock()
_session: Optional[requests.Session] = None


def _http() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            _session = requests.Session()
        return _session


def chat_completion(
    config: ProviderConfig,
    messages: list[dict],
    *,
    tools: Optional[list[dict]] = None,
    tool_choice: Any = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    operation: str = "chat",
) -> dict:
    """One ``/chat/completions`` call with bounded retries.

    Returns ``{"text": str, "tool_calls": [{"id", "name", "arguments"}], "usage": {...}}``.
    """
    if config.name != OPENAI_COMPAT:
        raise ProviderError("chat_completion() only supports the openai_compat provider.")

    url = f"{config.base_url}/chat/completions"
    payload: dict[str, Any] = {"model": config.model, "messages": messages}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    # OpenRouter asks for an identifying referer; harmless for other gateways.
    headers.setdefault("HTTP-Referer", "https://github.com/canoheil/Aura")
    headers.setdefault("X-Title", "Aura")

    attempts = max(0, config.max_retries) + 1
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        started = time.monotonic()
        try:
            response = _http().post(url, headers=headers, json=payload, timeout=config.timeout)
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 4))
                continue
            raise ProviderError(f"{operation}: network error: {exc}") from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderError(f"{operation}: provider returned invalid JSON") from exc
            logger.info(
                "Provider call | provider=%s model=%s operation=%s latency_ms=%s",
                config.name, config.model, operation, elapsed_ms,
            )
            return _normalize_completion(data)

        body = (response.text or "")[:400]
        if response.status_code in {408, 409, 425, 429} or response.status_code >= 500:
            last_error = ProviderError(f"HTTP {response.status_code}: {body}", response.status_code)
            if attempt + 1 < attempts:
                retry_after = response.headers.get("Retry-After")
                delay = min(float(retry_after), 10.0) if (retry_after or "").replace(".", "", 1).isdigit() else min(2 ** attempt, 8)
                logger.warning(
                    "Provider transient error | status=%s attempt=%s/%s retry_in=%ss",
                    response.status_code, attempt + 1, attempts, delay,
                )
                time.sleep(delay)
                continue
            raise last_error if isinstance(last_error, ProviderError) else ProviderError(str(last_error))

        # 4xx that is not worth retrying: surface a message the admin can act on.
        detail = f"HTTP {response.status_code}: {body}"
        if response.status_code in {401, 403}:
            detail += " — check the API key"
        elif response.status_code == 404:
            detail += " — check the base URL and model name"
        raise ProviderError(f"{operation}: {detail}", response.status_code)

    raise ProviderError(f"{operation}: {last_error}")


def _normalize_completion(data: dict) -> dict:
    try:
        choice = (data.get("choices") or [{}])[0]
    except (AttributeError, IndexError):
        choice = {}
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # Some gateways return content parts instead of a plain string.
        text = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        ).strip()
    else:
        text = (content or "").strip() if isinstance(content, str) else ""

    calls = []
    for raw in message.get("tool_calls") or []:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        name = _clean(function.get("name"))
        if not name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments) if arguments.strip() else {}
            except ValueError:
                parsed = {}
        elif isinstance(arguments, dict):
            parsed = arguments
        else:
            parsed = {}
        calls.append({"id": raw.get("id") or "", "name": name, "arguments": parsed if isinstance(parsed, dict) else {}})

    usage = data.get("usage") or {}
    return {
        "text": text,
        "tool_calls": calls,
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        },
        "finish_reason": choice.get("finish_reason"),
    }


def list_models(config: ProviderConfig) -> list[dict]:
    """List model ids for the admin panel; works for both providers."""
    if config.name == GEMINI:
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=config.api_key,
            http_options=types.HttpOptions(timeout=config.timeout * 1000),
        )
        models = []
        for item in client.models.list():
            name = getattr(item, "name", "")
            if name:
                models.append({"id": str(name).removeprefix("models/")})
        return models

    url = f"{config.base_url}/models"
    response = _http().get(
        url,
        headers={"Authorization": f"Bearer {config.api_key}", "Accept": "application/json"},
        timeout=config.timeout,
    )
    if response.status_code != 200:
        raise ProviderError(f"Could not list models (HTTP {response.status_code})", response.status_code)
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderError("Provider returned invalid JSON for /models") from exc
    entries = data.get("data") if isinstance(data, dict) else data
    models = []
    for item in entries or []:
        if isinstance(item, dict):
            identifier = _clean(item.get("id") or item.get("name"))
            if identifier:
                models.append({"id": identifier})
        elif isinstance(item, str) and item.strip():
            models.append({"id": item.strip()})
    return models


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------

def _image_data_url(item: dict) -> Optional[str]:
    image_url = item.get("image_url")
    url = image_url.get("url") if isinstance(image_url, dict) else None
    return url if isinstance(url, str) and url.startswith("data:") else None


def _content_to_openai(content: Any) -> Any:
    """Convert Aura's message content into OpenAI chat content.

    Plain strings pass through. The list form already matches the vision schema
    for images; ``media_bytes`` is turned into a data URL and video is dropped
    with a note, because the OpenAI protocol has no video part.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)

    parts: list[dict] = []
    dropped_video = False
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            text = _clean(item.get("text"))
            if text:
                parts.append({"type": "text", "text": text})
        elif kind == "image_url":
            url = _image_data_url(item)
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
        elif kind == "media_bytes":
            data = item.get("data")
            mime = _clean(item.get("mime_type")) or "application/octet-stream"
            if isinstance(data, (bytes, bytearray)) and data:
                if mime.startswith("image/"):
                    import base64

                    encoded = base64.b64encode(bytes(data)).decode("ascii")
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
                else:
                    dropped_video = True
        elif kind == "video_url":
            dropped_video = True

    if dropped_video:
        parts.append({"type": "text", "text": "[A video or non-image attachment was supplied; this provider cannot read it directly.]"})
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0]["text"]
    return parts or ""


def to_openai_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Split system text out and convert the rest into chat messages."""
    system_parts: list[str] = []
    converted: list[dict] = []
    for message in messages or []:
        role = _clean(message.get("role")) or "user"
        content = message.get("content", "")
        if role == "system":
            text = _content_to_openai(content)
            if isinstance(text, str) and text.strip():
                system_parts.append(text.strip())
            continue
        converted.append({
            "role": "assistant" if role == "assistant" else "user",
            "content": _content_to_openai(content),
        })
    return "\n\n".join(system_parts).strip(), converted


def declarations_to_tools(declarations) -> list[dict]:
    """Convert Gemini-style function declarations into OpenAI tool schemas."""
    tools = []
    for declaration in declarations or []:
        schema = getattr(declaration, "parameters_json_schema", None)
        if not schema:
            parameters = getattr(declaration, "parameters", None)
            schema = _gemini_schema_to_json(parameters) if parameters is not None else {"type": "object", "properties": {}}
        tools.append({
            "type": "function",
            "function": {
                "name": getattr(declaration, "name", ""),
                "description": getattr(declaration, "description", "") or "",
                "parameters": schema,
            },
        })
    return tools


def _gemini_schema_to_json(parameters) -> dict:
    """Best-effort conversion for declarations built with the legacy schema."""
    if isinstance(parameters, dict):
        return parameters
    result: dict = {"type": "object", "properties": {}}
    properties = getattr(parameters, "properties", None) or {}
    required = list(getattr(parameters, "required", None) or [])
    for name, prop in properties.items():
        entry: dict = {}
        prop_type = getattr(prop, "type", None)
        if prop_type is not None:
            entry["type"] = str(getattr(prop_type, "value", prop_type)).lower()
        description = getattr(prop, "description", None)
        if description:
            entry["description"] = description
        enum = getattr(prop, "enum", None)
        if enum:
            entry["enum"] = list(enum)
        result["properties"][name] = entry
    if required:
        result["required"] = required
    return result


def _tool_call_to_message(call: dict) -> dict:
    return {
        "id": call.get("id") or "",
        "type": "function",
        "function": {
            "name": call["name"],
            "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
        },
    }


# ---------------------------------------------------------------------------
# Agent turn for the OpenAI-compatible provider
# ---------------------------------------------------------------------------

def ask_agent(
    config: ProviderConfig,
    messages: list[dict],
    *,
    registry: ToolRegistry,
    runner: AgentRunner,
    owner_user_id: Optional[int] = None,
    file_context: Optional[dict] = None,
    extra_context: Optional[dict] = None,
    require_web_search: bool = False,
    allow_meme_tool: bool = True,
    current_user_text: Optional[str] = None,
    require_search_evidence: bool = False,
    max_rounds: int = 5,
    lang: Optional[str] = None,
) -> tuple[str, Optional[dict], list[dict]]:
    """Run one bounded agent task against an OpenAI-compatible endpoint.

    Mirrors ``bot.ask_gemini``'s contract and guarantees: tool budgets, duplicate
    suppression, evidence requirements and a final answer that cannot claim a
    current fact without a successful search.
    """
    system_instruction, conversation = to_openai_messages(messages)

    declarations = registry.declarations()
    if not allow_meme_tool:
        declarations = [item for item in declarations if getattr(item, "name", "") != "fetch_meme"]
    if not (file_context and file_context.get("file_path")):
        declarations = [item for item in declarations if getattr(item, "name", "") != "file_analyze"]
    tools = declarations_to_tools(declarations)

    runtime_context: dict[str, Any] = {}
    if owner_user_id:
        runtime_context["owner_user_id"] = owner_user_id
    runtime_context["meme_allowed"] = bool(allow_meme_tool)
    if file_context:
        runtime_context.update(file_context)
    if extra_context:
        runtime_context.update(extra_context)
    state = AgentRunState(runtime_context=runtime_context)

    if system_instruction:
        conversation = [{"role": "system", "content": system_instruction}] + conversation

    user_text = current_user_text or ""
    forced = bool(require_web_search and "web_search" in registry.names())
    evidence_required = bool(require_search_evidence)
    declaration_retry_used = False
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage_seen = False

    def accumulate(usage: dict) -> None:
        nonlocal usage_seen
        if not usage:
            return
        for key in totals:
            value = usage.get(key)
            if isinstance(value, int):
                totals[key] += value
                usage_seen = True

    def evidence_checked(answer: str) -> str:
        searched = any(
            isinstance(event, dict) and event.get("tool") in {"web_search", "web_open"}
            for event in state.events
        )
        missing_required = (require_web_search or evidence_required) and not has_web_evidence(state.events)
        missing_evidence = searched and not has_any_web_evidence(state.events)
        if missing_required or missing_evidence:
            logger.warning("Provider web evidence unavailable; refusing an unverified answer")
            return i18n.t("web.no_evidence", lang)
        return answer

    for round_number in range(max_rounds + 1):
        state.rounds = round_number
        final_round = round_number >= max_rounds
        tool_choice = None
        if forced and not final_round:
            tool_choice = {"type": "function", "function": {"name": "web_search"}}
        elif forced:
            forced = False

        try:
            result = chat_completion(
                config,
                conversation,
                tools=None if final_round else tools,
                tool_choice=tool_choice,
                operation=f"agent-round-{round_number + 1}",
            )
        except ProviderError as exc:
            # A gateway may refuse a forced tool choice. Losing the forcing must
            # not lose the requirement: switch to evidence checking instead.
            if forced and exc.status_code in {400, 404, 422, 500}:
                logger.warning("Provider rejected a forced tool choice (%s); requiring evidence instead", exc)
                forced = False
                evidence_required = True
                continue
            raise
        accumulate(result["usage"])

        text = result["text"]
        if text and text_has_tool_declaration(text, user_text=user_text) and not declaration_retry_used:
            declaration_retry_used = True
            logger.warning("Provider returned textual tool syntax; retrying once")
            conversation.append({"role": "assistant", "content": text})
            conversation.append({
                "role": "user",
                "content": (
                    "Your previous reply printed internal tool syntax. Do not print tool "
                    "declarations, JSON arguments or function names. Answer in natural "
                    "language, and use tools only through real tool calls."
                ),
            })
            continue

        calls = result["tool_calls"]
        if not calls:
            answer = text.strip()
            if not answer:
                raise ProviderError("Provider returned an empty response.")
            return evidence_checked(answer), (totals if usage_seen else None), state.events

        conversation.append({
            "role": "assistant",
            "content": text or None,
            "tool_calls": [_tool_call_to_message(call) for call in calls],
        })
        for call in calls:
            payload = runner.execute_tool(state, call["name"], dict(call.get("arguments") or {}))
            conversation.append({
                "role": "tool",
                "tool_call_id": call.get("id") or call["name"],
                "content": json.dumps(payload, ensure_ascii=False, default=str)[:20000],
            })
        if round_number + 1 > max_rounds:
            break

    # Rounds exhausted: one last call without tools so the user always gets text.
    result = chat_completion(config, conversation, operation="agent-final")
    accumulate(result["usage"])
    answer = (result["text"] or "").strip()
    if not answer:
        raise ProviderError("Provider did not produce a final answer within the round limit.")
    return evidence_checked(answer), (totals if usage_seen else None), state.events


# ---------------------------------------------------------------------------
# Small helper calls (summaries, wording, prompt rewriting)
# ---------------------------------------------------------------------------

@dataclass
class TextResult:
    text: str = ""
    function_calls: list[dict] = field(default_factory=list)


_gemini_clients: dict[str, Any] = {}
_gemini_lock = threading.Lock()


def gemini_client(api_key: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
    """Reuse one SDK client per key so rotating the key does not leak clients."""
    from google import genai
    from google.genai import types

    key = api_key.strip()
    with _gemini_lock:
        client = _gemini_clients.get(key)
        if client is None:
            client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=timeout_seconds * 1000))
            _gemini_clients[key] = client
        return client


def generate_text(
    settings,
    *,
    system_instruction: str = "",
    prompt: Optional[str] = None,
    messages: Optional[list[dict]] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_output_tokens: Optional[int] = None,
    tools: Optional[list] = None,
    force_tool: Optional[str] = None,
    operation: str = "text",
    gemini_client_factory: Optional[Callable[[], Any]] = None,
) -> TextResult:
    """One text (or tool-declaring) completion, independent of the provider.

    ``tools`` accepts Gemini ``FunctionDeclaration`` objects, which is what the
    rest of the codebase already builds.
    """
    config = resolve_config(settings, model)

    if config.name == GEMINI:
        from google.genai import types

        if messages is not None:
            prefix, contents = _gemini_contents(messages)
            system = "\n\n".join(part for part in (prefix, system_instruction) if part)
        else:
            contents = [prompt or ""]
            system = system_instruction
        kwargs: dict[str, Any] = {}
        if system:
            kwargs["system_instruction"] = system
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_output_tokens is not None:
            kwargs["max_output_tokens"] = max_output_tokens
        if tools:
            kwargs["tools"] = [types.Tool(function_declarations=list(tools))]
            kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
            if force_tool:
                kwargs["tool_config"] = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode=types.FunctionCallingConfigMode.ANY,
                        allowed_function_names=[force_tool],
                    )
                )
        # The host application owns its Gemini client (one instance, rotation
        # aware, and replaceable in tests); only fall back to our own cache.
        client = gemini_client_factory() if gemini_client_factory else gemini_client(config.api_key, config.timeout)
        response = client.models.generate_content(
            model=config.model,
            contents=contents,
            config=types.GenerateContentConfig(**kwargs),
        )
        calls = []
        for call in getattr(response, "function_calls", None) or []:
            name = getattr(call, "name", "")
            if name:
                arguments = getattr(call, "args", {}) or {}
                calls.append({"id": getattr(call, "id", "") or "", "name": name, "arguments": dict(arguments)})
        text = getattr(response, "text", None)
        return TextResult(text=text.strip() if isinstance(text, str) else "", function_calls=calls)

    system, conversation = to_openai_messages(messages or [])
    if messages is None:
        conversation = [{"role": "user", "content": prompt or ""}]
    if system_instruction:
        system = "\n\n".join(part for part in (system, system_instruction) if part)
    if system:
        conversation = [{"role": "system", "content": system}] + conversation

    tool_schemas = declarations_to_tools(tools or [])
    choice = {"type": "function", "function": {"name": force_tool}} if (force_tool and tool_schemas) else None
    result = chat_completion(
        config,
        conversation,
        tools=tool_schemas or None,
        tool_choice=choice,
        temperature=temperature,
        max_tokens=max_output_tokens,
        operation=operation,
    )
    return TextResult(text=result["text"], function_calls=result["tool_calls"])


def _gemini_contents(messages: list[dict]) -> tuple[str, list]:
    """Minimal messages→Gemini conversion for helper calls (text only)."""
    from google.genai import types

    system_parts: list[str] = []
    contents: list[Any] = []
    for message in messages or []:
        role = _clean(message.get("role")) or "user"
        content = message.get("content", "")
        if role == "system":
            text = _content_to_openai(content)
            if isinstance(text, str) and text.strip():
                system_parts.append(text.strip())
            continue
        text = _content_to_openai(content)
        if isinstance(text, list):
            # Helper calls are text-only; keep the textual parts.
            text = " ".join(part.get("text", "") for part in text if part.get("type") == "text")
        contents.append(types.Content(
            role="model" if role == "assistant" else "user",
            parts=[types.Part.from_text(text=str(text or ""))],
        ))
    return "\n\n".join(system_parts).strip(), contents
