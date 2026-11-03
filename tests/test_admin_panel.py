"""The /admin panel: who may use it, what it changes, and what it never reveals."""
import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import admin_panel
from settings_store import SettingsStore


@pytest.fixture()
def store(tmp_path, monkeypatch):
    for name in ("ADMIN_USER_IDS", "LLM_PROVIDER", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return SettingsStore(connection, db_path=tmp_path / "bot.db")


def _update(user_id=500, *, private=True, args=None):
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, first_name="Test"),
        effective_chat=SimpleNamespace(id=user_id, type="private" if private else "supergroup"),
        message=message,
        callback_query=None,
    ), SimpleNamespace(args=args or [])


def _callback(data, user_id=500):
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id, type="private"),
        message=None,
        callback_query=query,
    )
    return update, SimpleNamespace()


def _text(update):
    return update.message.reply_text.await_args.args[0]


# --- access control --------------------------------------------------------

def test_the_founder_can_open_the_panel(store):
    update, context = _update(42)
    asyncio.run(admin_panel.admin_command(
        update, context, store=store, access=admin_panel.AdminAccess(founder_id=42),
    ))
    text = _text(update)
    assert "Aura admin" in text
    assert update.message.reply_text.await_args.kwargs["reply_markup"] is not None


def test_extra_admins_from_the_environment_are_allowed(store, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "7, 8,nonsense")
    access = admin_panel.AdminAccess.from_env(founder_id=1)
    assert access.allows(1) and access.allows(7) and access.allows(8)
    assert not access.allows(9)
    assert "nonsense" not in access.describe()


def test_a_stranger_is_denied_and_nothing_changes(store):
    update, context = _update(999, args=["set", "agent_max_rounds", "9"])
    asyncio.run(admin_panel.admin_command(
        update, context, store=store, access=admin_panel.AdminAccess(founder_id=42),
    ))
    assert _text(update) == admin_panel.DENIED
    assert store.get("agent_max_rounds") == "5"


def test_a_group_chat_is_refused_even_for_the_founder(store):
    update, context = _update(42, private=False)
    asyncio.run(admin_panel.admin_command(
        update, context, store=store, access=admin_panel.AdminAccess(founder_id=42),
    ))
    assert _text(update) == admin_panel.PRIVATE_ONLY


# --- setting values --------------------------------------------------------

def test_set_stores_a_valid_value_and_audits_the_actor(store):
    update, context = _update(42, args=["set", "agent_max_rounds", "9"])
    asyncio.run(admin_panel.admin_command(
        update, context, store=store, access=admin_panel.AdminAccess(founder_id=42),
    ))
    assert store.get("agent_max_rounds") == "9"
    assert "✅" in _text(update)
    assert store.recent_audit(1)[0]["actor"] == 42


def test_set_rejects_an_invalid_value_without_storing_it(store):
    update, context = _update(42, args=["set", "agent_max_rounds", "999"])
    asyncio.run(admin_panel.admin_command(
        update, context, store=store, access=admin_panel.AdminAccess(founder_id=42),
    ))
    assert "Maximum is 12" in _text(update)
    assert store.get("agent_max_rounds") == "5"


def test_a_secret_is_never_echoed_back(store):
    update, context = _update(42, args=["set", "openai_api_key", "sk-super-secret-value"])
    asyncio.run(admin_panel.admin_command(
        update, context, store=store, access=admin_panel.AdminAccess(founder_id=42),
    ))
    assert "sk-super-secret-value" not in _text(update)
    assert "hidden" in _text(update)

    update, context = _update(42, args=["show", "openai_api_key"])
    asyncio.run(admin_panel.admin_command(
        update, context, store=store, access=admin_panel.AdminAccess(founder_id=42),
    ))
    shown = _text(update)
    assert "sk-super-secret-value" not in shown
    assert "sk-s…alue" in shown or "sk-s" in shown


def test_a_value_can_be_reset_and_the_source_is_reported(store, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    store.set("llm_provider", "gemini", actor=1)
    update, context = _update(42, args=["unset", "llm_provider"])
    asyncio.run(admin_panel.admin_command(
        update, context, store=store, access=admin_panel.AdminAccess(founder_id=42),
    ))
    assert store.get("llm_provider") == "openai_compat"
    assert "env" in _text(update)


def test_unknown_keys_and_subcommands_are_explained(store):
    update, context = _update(42, args=["set", "nope", "1"])
    asyncio.run(admin_panel.admin_command(
        update, context, store=store, access=admin_panel.AdminAccess(founder_id=42),
    ))
    assert "Unknown setting" in _text(update)

    update, context = _update(42, args=["whatever"])
    asyncio.run(admin_panel.admin_command(
        update, context, store=store, access=admin_panel.AdminAccess(founder_id=42),
    ))
    assert "Usage" in _text(update)


# --- inline callbacks ------------------------------------------------------

def test_tapping_a_boolean_toggles_it(store):
    update, context = _callback("admin:t:web_search_enabled", user_id=42)
    asyncio.run(admin_panel.admin_callback(
        update, context, store=store, access=admin_panel.AdminAccess(founder_id=42),
    ))
    assert store.get_bool("web_search_enabled") is False
    assert update.callback_query.edit_message_text.await_count == 1
    assert "off" in update.callback_query.answer.await_args.args[0]


def test_tapping_a_choice_cycles_the_provider(store):
    update, context = _callback("admin:c:llm_provider", user_id=42)
    asyncio.run(admin_panel.admin_callback(
        update, context, store=store, access=admin_panel.AdminAccess(founder_id=42),
    ))
    assert store.get("llm_provider") == "openai_compat"

    update, context = _callback("admin:c:llm_provider", user_id=42)
    asyncio.run(admin_panel.admin_callback(
        update, context, store=store, access=admin_panel.AdminAccess(founder_id=42),
    ))
    assert store.get("llm_provider") == "gemini"


def test_a_callback_from_a_stranger_is_refused(store):
    update, context = _callback("admin:t:web_search_enabled", user_id=999)
    asyncio.run(admin_panel.admin_callback(
        update, context, store=store, access=admin_panel.AdminAccess(founder_id=42),
    ))
    assert store.get_bool("web_search_enabled") is True
    assert update.callback_query.answer.await_args.kwargs["show_alert"] is True
    assert update.callback_query.edit_message_text.await_count == 0


def test_the_provider_check_reports_failures_without_crashing(store):
    store.set("llm_provider", "openai_compat")  # no key configured
    update, context = _update(42, args=["test"])
    asyncio.run(admin_panel.admin_command(
        update, context, store=store, access=admin_panel.AdminAccess(founder_id=42),
    ))
    replies = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert any("check failed" in reply for reply in replies)


def test_audit_view_lists_recent_changes(store):
    store.set("agent_max_rounds", "6", actor=42)
    text = admin_panel.audit_text(store)
    assert "set · agent_max_rounds" in text
    assert "by 42" in text
