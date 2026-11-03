"""bot.py wiring: provider selection, startup without Gemini, /model rules."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest


def _load_bot(monkeypatch, env):
    """Import bot.py into a private module with an in-memory database."""
    real_connect = sqlite3.connect

    class _Client:
        class models:
            @staticmethod
            def generate_content(*_args, **_kwargs):
                raise AssertionError("no network call expected in this test")

            @staticmethod
            def list():
                return []

    def _fake_client(**_kwargs):
        return _Client()

    from google import genai

    # Environment must stay in place for the whole test: the module reads the
    # settings store at call time, not only at import.
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with monkeypatch.context() as setup:
        setup.setattr(sqlite3, "connect", lambda _path, **kw: real_connect(":memory:", **kw))
        setup.setattr(genai, "Client", _fake_client)
        spec = importlib.util.spec_from_file_location(
            "_wiring_aura_bot", Path(__file__).resolve().parent.parent / "bot.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


BASE_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:test-token",
    "FOUNDER_USER_ID": "42",
    "STICKERS_ENABLED": "false",
    "SVG_IMAGE_ENABLED": "false",
    "MEMES_ENABLED": "false",
    "WEATHER_ENABLED": "false",
    "POLLINATIONS_ENABLED": "false",
    "REACTION_AI_ENABLED": "false",
    "CONTEXT_MANAGER_ENABLED": "false",
    "YOU_API_KEY": "",
    "ADMIN_USER_IDS": "",
}


def test_an_openai_only_deployment_starts_without_a_gemini_key(monkeypatch):
    env = dict(BASE_ENV)
    env.pop("GEMINI_API_KEY", None)
    env.update({
        "LLM_PROVIDER": "openai_compat",
        "OPENAI_API_KEY": "sk-test-123",
        "OPENAI_BASE_URL": "https://gateway.test/v1",
        "OPENAI_MODEL": "gpt-test",
    })
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    bot = _load_bot(monkeypatch, env)
    try:
        assert bot.GEMINI_CLIENT is None
        assert bot.configured_provider() == "openai_compat"
        assert bot.default_model() == "gpt-test"
        assert bot.ADMIN_ACCESS.allows(42)
        # The admin panel is registered for the owner.
        assert bot.ADMIN_ACCESS.allows(7) is False
    finally:
        bot.db.close()


def test_a_gemini_only_deployment_still_works(monkeypatch):
    env = dict(BASE_ENV)
    env.update({"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "gem-key", "GEMINI_MODEL": "gemini-test"})
    bot = _load_bot(monkeypatch, env)
    try:
        assert bot.GEMINI_CLIENT is not None
        assert bot.configured_provider() == "gemini"
        assert bot.default_model() == "gemini-test"
    finally:
        bot.db.close()


def test_missing_every_key_fails_with_an_actionable_message(monkeypatch):
    env = dict(BASE_ENV)
    env.update({"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": ""})
    with pytest.raises(RuntimeError) as excinfo:
        _load_bot(monkeypatch, env)
    assert "No model provider is configured" in str(excinfo.value)


def test_switching_provider_at_runtime_changes_the_dispatch(monkeypatch):
    env = dict(BASE_ENV)
    env.update({
        "LLM_PROVIDER": "gemini",
        "GEMINI_API_KEY": "gem-key",
        "GEMINI_MODEL": "gemini-test",
        "OPENAI_API_KEY": "sk-test-123",
        "OPENAI_BASE_URL": "https://gateway.test/v1",
        "OPENAI_MODEL": "gpt-test",
    })
    bot = _load_bot(monkeypatch, env)
    try:
        called = {}

        def fake_ask_gemini(model, messages, **kwargs):
            called["gemini"] = model
            return "ok", None, []

        def fake_ask_agent(config, messages, **kwargs):
            called["openai"] = config.model
            return "ok", None, []

        monkeypatch.setattr(bot, "ask_gemini", fake_ask_gemini)
        monkeypatch.setattr(bot.providers, "ask_agent", fake_ask_agent)

        bot.ask_model("gemini-test", [{"role": "user", "content": "hi"}])
        assert called["gemini"] == "gemini-test"

        bot.SETTINGS.set("llm_provider", "openai_compat", actor=42)
        bot.ask_model("gemini-test", [{"role": "user", "content": "hi"}])
        # The per-chat Gemini model name must not leak into the OpenAI request.
        assert called["openai"] == "gpt-test"
    finally:
        bot.db.close()


def test_provider_errors_are_reported_as_gemini_api_errors(monkeypatch):
    env = dict(BASE_ENV)
    env.update({"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "gem-key"})
    bot = _load_bot(monkeypatch, env)
    try:
        bot.SETTINGS.set("llm_provider", "openai_compat")  # no OpenAI key stored
        with pytest.raises(bot.GeminiAPIError) as excinfo:
            bot.ask_model("x", [{"role": "user", "content": "hi"}])
        assert "API key" in str(excinfo.value)
    finally:
        bot.db.close()
