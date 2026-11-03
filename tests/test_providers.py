"""Provider gateway: config resolution, wire format and the bounded agent loop."""
import json
import sqlite3

import pytest
from google.genai import types

import providers
from settings_store import SettingsStore
from tool_system import AgentRunner, ToolRegistry


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)
        self.headers = {}

    def json(self):
        return self._payload


class _Session:
    """Stands in for requests.Session so no test ever leaves the process."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None, **kwargs):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _completion(text="", tool_calls=None, usage=None):
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": call.get("id", f"call_{index}"),
                "type": "function",
                "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments") or {})},
            }
            for index, call in enumerate(tool_calls)
        ]
    return _Response(200, {
        "choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


@pytest.fixture()
def store(tmp_path, monkeypatch):
    for name in ("LLM_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return SettingsStore(connection, db_path=tmp_path / "bot.db")


def _openai(store, base_url="https://gateway.test/v1", model="test-model"):
    store.set("llm_provider", "openai_compat")
    store.set("openai_api_key", "sk-test-123")
    store.set("openai_base_url", base_url)
    store.set("openai_model", model)
    return providers.resolve_config(store)


# --- configuration ---------------------------------------------------------

def test_openai_config_is_resolved_from_settings(store):
    config = _openai(store)
    assert config.name == providers.OPENAI_COMPAT
    assert config.model == "test-model"
    assert config.base_url == "https://gateway.test/v1"
    assert "sk-test" not in config.fingerprint()


def test_gemini_config_requires_a_key(store):
    store.set("llm_provider", "gemini")
    with pytest.raises(providers.ProviderError):
        providers.resolve_config(store)


def test_openai_config_requires_a_key(store):
    store.set("llm_provider", "openai_compat")
    with pytest.raises(providers.ProviderError) as excinfo:
        providers.resolve_config(store)
    assert "API key" in str(excinfo.value)


def test_an_invalid_base_url_cannot_be_stored_at_all(store):
    # Validation happens on write, so a broken URL can never reach the client.
    from settings_store import SettingError

    with pytest.raises(SettingError):
        store.set("openai_base_url", "not-a-url")


def test_provider_status_never_raises_for_a_broken_setup(store):
    status = providers.provider_status(store)
    assert status["configured"] is False
    assert "key" in status["error"]


# --- wire conversion -------------------------------------------------------

def test_images_are_converted_to_data_urls_and_video_is_dropped():
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            {"type": "media_bytes", "data": b"\x89PNG", "mime_type": "image/png"},
            {"type": "video_url", "video_url": {"url": "https://example.test/v.mp4"}},
        ],
    }]
    system, converted = providers.to_openai_messages(messages)
    parts = converted[0]["content"]
    assert system == ""
    assert {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}} in parts
    assert sum(1 for part in parts if part["type"] == "image_url") == 2
    assert any("cannot read" in part.get("text", "") for part in parts if part["type"] == "text")


def test_system_messages_become_one_system_prompt():
    system, converted = providers.to_openai_messages([
        {"role": "system", "content": "A"},
        {"role": "system", "content": "B"},
        {"role": "user", "content": "hi"},
    ])
    assert system == "A\n\nB"
    assert converted == [{"role": "user", "content": "hi"}]


def test_gemini_declarations_become_openai_tools():
    declarations = [
        types.FunctionDeclaration(
            name="web_search",
            description="search",
            parameters_json_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
    ]
    tools = providers.declarations_to_tools(declarations)
    assert tools == [{
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "search",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }]


# --- agent loop ------------------------------------------------------------

def _registry(handlers=None):
    handlers = handlers or {}
    registry = ToolRegistry()
    registry.register(
        "web_search",
        types.FunctionDeclaration(name="web_search", description="search"),
        handlers.get("web_search", lambda args: {"ok": True, "results": []}),
    )
    registry.register(
        "get_weather",
        types.FunctionDeclaration(name="get_weather", description="weather"),
        handlers.get("get_weather", lambda args: {"ok": True, "temp": 20}),
    )
    return registry


def test_tool_calls_are_executed_and_fed_back(store, monkeypatch):
    config = _openai(store)
    session = _Session([
        _completion(tool_calls=[{"name": "web_search", "arguments": {"query": "python"}}]),
        _completion("پایتون ۳.۱۳ منتشر شد."),
    ])
    monkeypatch.setattr(providers, "_http", lambda: session)
    registry = _registry({
        "web_search": lambda args: {
            "ok": True,
            "results": [{"url": "https://python.org", "evidence": "Python 3.13 is out"}],
        }
    })
    runner = AgentRunner(registry)
    answer, usage, events = providers.ask_agent(
        config,
        [{"role": "user", "content": "آخرین نسخهٔ پایتون؟"}],
        registry=registry,
        runner=runner,
    )

    assert answer == "پایتون ۳.۱۳ منتشر شد."
    assert usage["total_tokens"] == 30
    assert [event["tool"] for event in events] == ["web_search"]
    # The second request must carry the tool result for the model to use.
    second = session.calls[1]["json"]["messages"]
    assert any(message["role"] == "tool" for message in second)
    assert session.calls[0]["json"]["tools"][0]["type"] == "function"
    assert session.calls[0]["headers"]["Authorization"] == "Bearer sk-test-123"


def test_duplicate_tool_calls_are_not_executed_twice(store, monkeypatch):
    config = _openai(store)
    session = _Session([
        _completion(tool_calls=[{"name": "get_weather", "arguments": {"city": "Berlin"}}]),
        _completion(tool_calls=[{"name": "get_weather", "arguments": {"city": "Berlin"}}]),
        _completion("هوا ۲۰ درجه است."),
    ])
    monkeypatch.setattr(providers, "_http", lambda: session)
    calls = []
    registry = _registry({"get_weather": lambda args: calls.append(args) or {"ok": True}})
    answer, _, events = providers.ask_agent(
        config, [{"role": "user", "content": "هوا؟"}], registry=registry, runner=AgentRunner(registry),
    )
    assert answer == "هوا ۲۰ درجه است."
    assert len(calls) == 1 and calls[0]["city"] == "Berlin"
    assert len(events) == 2 and events[1].get("cached") is True


def test_forced_search_is_downgraded_when_the_gateway_refuses(store, monkeypatch):
    config = _openai(store)
    session = _Session([
        _Response(400, {"error": {"message": "tool_choice is not supported"}}),
        _completion("جواب بدون جستجو"),
    ])
    monkeypatch.setattr(providers, "_http", lambda: session)
    answer, _, _ = providers.ask_agent(
        config,
        [{"role": "user", "content": "قیمت دلار چنده؟"}],
        registry=_registry(),
        runner=AgentRunner(_registry()),
        require_web_search=True,
    )
    # A refused forced search must not turn into an unverified current answer.
    assert "نمی‌تونم" in answer
    assert "جواب بدون جستجو" not in answer


def test_a_current_answer_with_evidence_is_allowed(store, monkeypatch):
    config = _openai(store)
    session = _Session([
        _completion("قیمت امروز ۱۰۰ است."),
    ])
    monkeypatch.setattr(providers, "_http", lambda: session)
    registry = _registry()
    # Evidence is present because the first round is forced to search; emulate a
    # provider that honours the forced tool choice.
    session.responses = [
        _completion(tool_calls=[{"name": "web_search", "arguments": {"query": "دلار"}}]),
        _completion("قیمت امروز ۱۰۰ است."),
    ]
    registry = _registry({
        "web_search": lambda args: {
            "ok": True, "results": [{"url": "https://example.test/price", "evidence": "۱۰۰ تومان"}],
        }
    })
    answer, _, _ = providers.ask_agent(
        config, [{"role": "user", "content": "قیمت دلار چنده؟"}],
        registry=registry, runner=AgentRunner(registry), require_web_search=True,
    )
    assert answer == "قیمت امروز ۱۰۰ است."


def test_empty_answer_raises_instead_of_looking_like_success(store, monkeypatch):
    config = _openai(store)
    monkeypatch.setattr(providers, "_http", lambda: _Session([_completion("")]))
    with pytest.raises(providers.ProviderError):
        providers.ask_agent(
            config, [{"role": "user", "content": "hi"}],
            registry=_registry(), runner=AgentRunner(_registry()),
        )


def test_authentication_failures_are_reported_with_a_hint(store, monkeypatch):
    config = _openai(store)
    monkeypatch.setattr(providers, "_http", lambda: _Session([_Response(401, {"error": "bad key"})]))
    with pytest.raises(providers.ProviderError) as excinfo:
        providers.chat_completion(config, [{"role": "user", "content": "hi"}])
    assert "check the API key" in str(excinfo.value)
    assert excinfo.value.status_code == 401


def test_model_listing_tolerates_both_response_shapes(store, monkeypatch):
    config = _openai(store)
    session = _Session([_Response(200, {"data": [{"id": "a"}, {"id": "b"}]})])
    monkeypatch.setattr(providers, "_http", lambda: session)
    assert [item["id"] for item in providers.list_models(config)] == ["a", "b"]

    session = _Session([_Response(200, ["c", "d"])])
    monkeypatch.setattr(providers, "_http", lambda: session)
    assert [item["id"] for item in providers.list_models(config)] == ["c", "d"]


def test_generate_text_uses_the_injected_client_for_gemini(store):
    store.set("llm_provider", "gemini")
    store.set("gemini_api_key", "key-123")
    seen = {}

    class _Models:
        def generate_content(self, *, model, contents, config):
            seen["model"] = model
            seen["contents"] = contents
            return types.GenerateContentResponse(
                candidates=[types.Candidate(content=types.Content(
                    role="model",
                    parts=[types.Part(function_call=types.FunctionCall(
                        name="schedule_reminder", args={"task": "x"},
                    ))],
                ))]
            )

    class _Client:
        models = _Models()

    result = providers.generate_text(
        store,
        messages=[{"role": "user", "content": "hello"}],
        system_instruction="be brief",
        tools=[types.FunctionDeclaration(name="schedule_reminder", description="d")],
        force_tool="schedule_reminder",
        gemini_client_factory=lambda: _Client(),
    )
    assert seen["model"] == "gemini-3.1-flash-lite"
    assert result.function_calls == [{"id": "", "name": "schedule_reminder", "arguments": {"task": "x"}}]
    assert result.text == ""
