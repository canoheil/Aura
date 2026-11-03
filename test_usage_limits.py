"""Per-user daily quotas: accounting, the gate, and who is exempt."""
import asyncio
import re
import sqlite3
import threading

import pytest

from test_agent_tool_response import _telegram_update, _text_response
from usage_limits import Limits, Usage, UsageStore, evaluate, next_reset

PERSIAN = re.compile("[\u0600-\u06FF]")


@pytest.fixture()
def store():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    return UsageStore(db, threading.RLock())


# --- accounting -------------------------------------------------------------

def test_a_new_user_starts_at_zero(store):
    assert store.today(7) == Usage(0, 0)


def test_turns_and_tokens_accumulate(store):
    store.record_turn(7)
    store.record_turn(7, tokens=100)
    store.add_tokens(7, 50)
    assert store.today(7) == Usage(turns=2, tokens=150)


def test_users_and_days_are_counted_separately(tmp_path):
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    clock = {"now": 1_800_000_000.0}
    store = UsageStore(db, threading.RLock(), clock=lambda: clock["now"])

    store.record_turn(7)
    store.record_turn(8)
    assert store.today(7).turns == 1 and store.today(8).turns == 1

    clock["now"] += 86400  # next UTC day
    assert store.today(7) == Usage(0, 0)


def test_pruning_drops_yesterday_and_keeps_today():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    clock = {"now": 1_800_000_000.0}
    store = UsageStore(db, threading.RLock(), clock=lambda: clock["now"])
    store.record_turn(7)
    clock["now"] += 86400
    store.record_turn(7)
    assert store.purge_history() == 1
    assert store.today(7).turns == 1


def test_reset_only_touches_today(store):
    store.record_turn(7)
    assert store.reset(7) == 1
    assert store.today(7) == Usage(0, 0)
    assert store.reset(7) == 0


def test_negative_tokens_are_ignored(store):
    store.add_tokens(7, -5)
    assert store.today(7).tokens == 0


def test_next_reset_is_the_next_utc_midnight():
    import datetime

    moment = datetime.datetime(2026, 11, 3, 15, 30, tzinfo=datetime.timezone.utc).timestamp()
    reset = datetime.datetime.fromtimestamp(next_reset(moment), datetime.timezone.utc)
    assert (reset.year, reset.month, reset.day, reset.hour, reset.minute) == (2026, 11, 4, 0, 0)


# --- the decision -----------------------------------------------------------

def test_zero_means_unlimited():
    assert evaluate(Usage(999, 999_999), Limits(0, 0)).allowed is True


def test_turns_are_checked_before_tokens():
    decision = evaluate(Usage(turns=5, tokens=10), Limits(daily_turns=5, daily_tokens=1000))
    assert decision.allowed is False and decision.reason == "turns"
    assert decision.used == 5 and decision.limit == 5 and decision.remaining == 0


def test_the_token_budget_also_blocks():
    decision = evaluate(Usage(turns=1, tokens=1000), Limits(daily_turns=50, daily_tokens=1000))
    assert (decision.allowed, decision.reason) == (False, "tokens")


def test_under_the_limit_is_allowed_and_reports_the_remainder():
    decision = evaluate(Usage(turns=2, tokens=10), Limits(daily_turns=5))
    assert decision.allowed is True
    assert decision.remaining == 3


def test_limits_are_read_defensively():
    values = {"user_daily_turns": "not-a-number", "user_daily_tokens": "12"}
    limits = Limits.from_setting(lambda key: values[key])
    assert limits.daily_turns == 0  # unusable value = unlimited, never "block everyone"
    assert limits.daily_tokens == 12


# --- the bot ----------------------------------------------------------------

def _send(bot, client, text="hi", user_id=None, responses=None):
    if responses is not None:
        client.models.responses = list(responses)
    update, context, _ = _telegram_update()
    if user_id is not None:
        update.effective_user.id = user_id
    update.message.text = text
    asyncio.run(bot.message_handler(update, context))
    return update.message.reply_text.await_args.args[0] if update.message.reply_text.await_args else ""


def test_a_message_is_counted_against_the_user(isolated_bot):
    bot, client = isolated_bot
    _send(bot, client, responses=[_text_response("hello")])
    assert bot.USAGE_STORE.today(123).turns == 1


def test_the_limit_refuses_before_calling_the_model(isolated_bot):
    bot, client = isolated_bot
    bot.SETTINGS.set("user_daily_turns", "1", actor=1)
    _send(bot, client, responses=[_text_response("first")])
    calls = len(client.models.requests)

    reply = _send(bot, client, responses=[_text_response("second")])
    assert len(client.models.requests) == calls, "a blocked message must not reach the model"
    assert "1 of 1 messages" in reply or "1 از 1 پیام" in reply
    assert "/usage" in reply or "سهمیه" in reply


def test_an_unlimited_default_never_blocks(isolated_bot):
    bot, client = isolated_bot
    for _ in range(3):
        _send(bot, client, responses=[_text_response("ok")])
    assert bot.USAGE_STORE.today(123).turns == 3
    assert bot.user_limits().unlimited is True


def test_the_founder_is_never_limited(isolated_bot, monkeypatch):
    bot, client = isolated_bot
    monkeypatch.setattr(bot, "FOUNDER_USER_ID", 123)
    bot.SETTINGS.set("user_daily_turns", "1", actor=1)
    _send(bot, client, responses=[_text_response("a")])
    _send(bot, client, responses=[_text_response("b")])
    assert len(client.models.requests) == 2
    # The founder's own usage is not even counted.
    assert bot.USAGE_STORE.today(123).turns == 0


def test_admins_are_never_limited(isolated_bot, monkeypatch):
    bot, client = isolated_bot
    monkeypatch.setenv("ADMIN_USER_IDS", "123")
    monkeypatch.setattr(bot, "ADMIN_ACCESS", bot.AdminAccess.from_env(0))
    bot.SETTINGS.set("user_daily_turns", "1", actor=1)
    _send(bot, client, responses=[_text_response("a")])
    _send(bot, client, responses=[_text_response("b")])
    assert len(client.models.requests) == 2


def test_a_token_budget_blocks_too(isolated_bot):
    bot, client = isolated_bot
    bot.LANGUAGE_STORE.set(123, "en")
    bot.SETTINGS.set("user_daily_tokens", "10", actor=1)
    bot.USAGE_STORE.add_tokens(123, 25)
    reply = _send(bot, client, responses=[_text_response("a")])
    assert client.models.requests == []
    assert "25 of 10 tokens" in reply


def test_the_refusal_follows_the_user_language(isolated_bot):
    bot, client = isolated_bot
    bot.LANGUAGE_STORE.set(123, "en")
    bot.SETTINGS.set("user_daily_turns", "1", actor=1)
    _send(bot, client, responses=[_text_response("first")])
    reply = _send(bot, client, responses=[_text_response("second")])
    assert PERSIAN.search(reply) is None


def test_commands_still_work_while_capped(isolated_bot):
    """A capped user must be able to see their usage and delete their data."""
    bot, client = isolated_bot
    bot.LANGUAGE_STORE.set(123, "en")
    bot.SETTINGS.set("user_daily_turns", "1", actor=1)
    bot.USAGE_STORE.record_turn(123)
    update, context, _ = _telegram_update()
    asyncio.run(bot.usage_command(update, context))
    assert "1 of 1" in update.message.reply_text.await_args.args[0]


# --- /usage ----------------------------------------------------------------

def test_usage_shows_the_counter_and_the_reset(isolated_bot):
    bot, _ = isolated_bot
    bot.LANGUAGE_STORE.set(123, "en")
    bot.USAGE_STORE.record_turn(123, tokens=42)
    update, context, _ = _telegram_update()
    asyncio.run(bot.usage_command(update, context))
    reply = update.message.reply_text.await_args.args[0]
    assert "Model messages: 1 (no limit)" in reply
    assert "Tokens: 42 (no limit)" in reply
    assert "00:00 UTC" in reply


def test_usage_shows_the_limits_when_they_exist(isolated_bot):
    bot, _ = isolated_bot
    bot.LANGUAGE_STORE.set(123, "en")
    bot.SETTINGS.set("user_daily_turns", "20", actor=1)
    bot.USAGE_STORE.record_turn(123, tokens=7)
    update, context, _ = _telegram_update()
    asyncio.run(bot.usage_command(update, context))
    reply = update.message.reply_text.await_args.args[0]
    assert "Model messages: 1 of 20" in reply
    assert "Tokens: 7 (no limit)" in reply


def test_usage_speaks_persian_to_persian_users(isolated_bot):
    bot, _ = isolated_bot
    update, context, _ = _telegram_update()
    asyncio.run(bot.usage_command(update, context))
    assert PERSIAN.search(update.message.reply_text.await_args.args[0])


# --- configuration and deletion --------------------------------------------

def test_the_settings_are_registered_and_default_to_unlimited():
    from settings_store import INDEX

    assert INDEX["user_daily_turns"].default == "0"
    assert INDEX["user_daily_tokens"].default == "0"
    assert INDEX["user_daily_turns"].env == "USER_DAILY_TURNS"
    assert INDEX["user_daily_turns"].group == "limits"


def test_deleting_a_users_data_clears_the_counter():
    import data_deletion

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    UsageStore(db, threading.RLock()).record_turn(7, tokens=10)
    assert "usage" in {category.key for category in data_deletion.CATEGORIES}
    data_deletion.delete(db, 7)
    row = db.execute("SELECT COUNT(*) FROM user_usage WHERE user_id = 7").fetchone()[0]
    assert row == 0


def test_the_bot_prunes_old_rows_at_startup(isolated_bot):
    bot, _ = isolated_bot
    bot.db.execute(
        "INSERT INTO user_usage (user_id, usage_date, turns, tokens, updated_at) VALUES (5, '2000-01-01', 3, 0, 0)"
    )
    bot.db.commit()
    assert bot.USAGE_STORE.purge_history() == 1


def test_image_commands_are_counted_and_gated(isolated_bot):
    """Images cost the most; a cap that skipped them would be theatre."""
    from unittest.mock import AsyncMock, PropertyMock, patch

    bot, _ = isolated_bot
    bot.LANGUAGE_STORE.set(123, "en")
    bot.SETTINGS.set("user_daily_turns", "1", actor=1)
    update, context, _ = _telegram_update()
    context.args = ["a cat"]
    with patch.object(type(bot.pollinations_client), "is_configured",
                      new_callable=PropertyMock, return_value=True), \
            patch.object(bot, "_run_image_generation", new=AsyncMock()) as generate:
        asyncio.run(bot.image_command(update, context))
        assert generate.await_count == 1
        assert bot.USAGE_STORE.today(123).turns == 1

        update2, context2, _ = _telegram_update()
        context2.args = ["a dog"]
        asyncio.run(bot.image_command(update2, context2))
        assert generate.await_count == 1, "the second image must be refused"
        assert "1 of 1 messages" in update2.message.reply_text.await_args.args[0]
