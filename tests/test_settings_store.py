"""Runtime settings: precedence, validation, secret handling and the audit trail."""
import sqlite3

import pytest

import settings_store
from settings_store import SettingError, SettingsStore, mask_secret


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return SettingsStore(connection, db_path=tmp_path / "bot.db")


def test_defaults_apply_when_nothing_is_configured(store):
    assert store.get("llm_provider") == "gemini"
    assert store.source("llm_provider") == "default"
    assert store.get_int("agent_max_rounds") == 5


def test_environment_supplies_the_value_until_it_is_overridden(store, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    assert store.get("llm_provider") == "openai_compat"
    assert store.source("llm_provider") == "env"

    store.set("llm_provider", "gemini", actor=7)
    assert store.get("llm_provider") == "gemini"
    assert store.source("llm_provider") == "db"

    assert store.unset("llm_provider", actor=7) is True
    assert store.get("llm_provider") == "openai_compat"  # env again
    assert store.source("llm_provider") == "env"


@pytest.mark.parametrize(
    "key,value",
    [
        ("llm_provider", "something-else"),
        ("agent_max_rounds", "0"),
        ("agent_max_rounds", "999"),
        ("agent_max_rounds", "many"),
        ("web_search_enabled", "maybe"),
        ("openai_base_url", "ftp://example.com"),
        ("openai_base_url", ""),
        ("gemini_api_key", ""),
        ("gemini_api_key", "has spaces"),
    ],
)
def test_invalid_values_are_rejected_before_they_are_stored(store, key, value):
    with pytest.raises(SettingError):
        store.set(key, value)


def test_unknown_keys_are_rejected(store):
    with pytest.raises(SettingError):
        store.get("not_a_setting")
    with pytest.raises(SettingError):
        store.set("not_a_setting", "1")


def test_booleans_are_normalized(store):
    store.set("web_search_enabled", "YES")
    assert store.get("web_search_enabled") == "true"
    assert store.get_bool("web_search_enabled") is True
    store.set("web_search_enabled", "off")
    assert store.get_bool("web_search_enabled") is False


def test_urls_lose_a_trailing_slash(store):
    store.set("openai_base_url", "https://openrouter.ai/api/v1/")
    assert store.get("openai_base_url") == "https://openrouter.ai/api/v1"


def test_secrets_are_encrypted_at_rest_and_masked_in_display(store):
    secret = "sk-live-abcdefghijklmnop"
    store.set("openai_api_key", secret, actor=7)

    assert store.get("openai_api_key") == secret  # round-trips for the client
    assert mask_secret(secret) not in (secret,)
    assert secret not in store.describe("openai_api_key")
    assert store.describe("openai_api_key").startswith("sk-l")

    stored = store._db.execute(
        "SELECT value FROM app_settings WHERE key = 'openai_api_key'"
    ).fetchone()["value"]
    if store.encryption_mode() == "encrypted":
        assert secret not in stored
        assert stored.startswith("enc:")
    else:  # pragma: no cover - only without cryptography installed
        assert stored.startswith("plain:")


def test_audit_records_who_changed_what(store):
    store.set("agent_max_rounds", "7", actor=11)
    store.unset("agent_max_rounds", actor=11)
    rows = store.recent_audit(5)
    assert [(row["key"], row["action"], row["actor"]) for row in rows] == [
        ("agent_max_rounds", "unset", 11),
        ("agent_max_rounds", "set", 11),
    ]


def test_every_spec_is_self_consistent():
    keys = [spec.key for spec in settings_store.SPECS]
    assert len(keys) == len(set(keys)), "duplicate setting key"
    for spec in settings_store.SPECS:
        assert spec.label, f"{spec.key} has no label for the panel"
        assert spec.group in settings_store.GROUPS
        if spec.kind == "secret":
            assert spec.secret is True


def test_no_setting_leaks_a_default_secret():
    # A default that looks like a real key would ship a credential in the source.
    for spec in settings_store.SPECS:
        if spec.kind == "secret":
            assert spec.default == ""
