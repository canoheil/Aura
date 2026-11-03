"""The buttons on a reminder: what they do, and who is allowed to press them.

A button is a promise that something will happen. These tests are mostly about
the cases where it must *not*: a stranger in a group, a second press on a
message that was already decided, and a reminder that has already fired.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest
from test_agent_tool_response import _telegram_update, _text_response
from test_reminder_conversation import _send

from reminders import ReminderSpec, format_due


def _callback(bot, data, *, user_id=123, chat_id=123, text="⏰ یادت نره: آب بخورم"):
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(chat_id=chat_id, text=text),
        from_user=SimpleNamespace(id=user_id),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, first_name="Test"),
        effective_chat=SimpleNamespace(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
        message=None,
        callback_query=query,
    )
    asyncio.run(bot.reminder_callback(update, SimpleNamespace()))
    return query


def _delivered(bot, *, task="آب بخورم", chat_id=123, user_id=123):
    """A row that has already gone out, as the scheduler leaves it."""
    reminder_id = bot.reminder_store.create(
        chat_id, user_id,
        ReminderSpec(datetime.now(timezone.utc) + timedelta(seconds=1), task, "Asia/Tehran"),
    )
    row = bot.reminder_store.get_owned(chat_id, user_id, reminder_id)
    bot.reminder_store.clock = lambda: row["due_utc"] + 1
    telegram = SimpleNamespace(send_message=AsyncMock())
    scheduler = bot.ReminderScheduler(bot.reminder_store, telegram)
    asyncio.run(scheduler.run_once())
    bot.reminder_store.clock = lambda: row["due_utc"] + 2
    return reminder_id, telegram


def _status(bot, reminder_id, chat_id=123, user_id=123):
    return bot.reminder_store.get_owned(chat_id, user_id, reminder_id)["status"]


# --- what the messages carry -------------------------------------------------

def test_the_snooze_label_states_the_delay_it_will_use(isolated_bot):
    """A changed REMINDER_SNOOZE_MINUTES must not leave a stale "10 minutes"."""
    from reminders import reminder_buttons

    row = {"id": 3}
    labels = [b.text for r in reminder_buttons(row, "fa", snooze_minutes=30).inline_keyboard for b in r]
    assert "۳۰" in labels[0]
    labels = [b.text for r in reminder_buttons(row, "en", snooze_minutes=30).inline_keyboard for b in r]
    assert "30" in labels[0]


def test_a_delivered_reminder_offers_exactly_two_decisions(isolated_bot):
    bot, _ = isolated_bot
    reminder_id, telegram = _delivered(bot)
    keyboard = telegram.send_message.await_args.kwargs["reply_markup"]
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert [button.callback_data for button in buttons] == [
        f"rm:snooze:{reminder_id}", f"rm:done:{reminder_id}",
    ]
    assert all(button.text.strip() for button in buttons)


def test_the_confirmation_offers_the_undo(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("باشه، یادت می‌ندازم.")]
    update, context, _ = _telegram_update()
    update.message.text = "۱۰ دقیقه دیگه یادم بنداز آب بخورم"
    asyncio.run(bot.message_handler(update, context))
    row = bot.reminder_store.list_active(123, 123)[0]
    keyboard = update.message.reply_text.await_args.kwargs["reply_markup"]
    assert [b.callback_data for r in keyboard.inline_keyboard for b in r] == [f"rm:cancel:{row['id']}"]


# --- the actions -------------------------------------------------------------

def test_done_closes_the_reminder_and_removes_the_buttons(isolated_bot):
    bot, _ = isolated_bot
    reminder_id, _ = _delivered(bot)
    query = _callback(bot, f"rm:done:{reminder_id}")
    assert _status(bot, reminder_id) == "done"
    assert bot.reminder_store.list_active(123, 123) == []
    # The original text is kept; the outcome is appended and the keyboard dropped.
    edited = query.edit_message_text.await_args.args[0]
    assert edited.startswith("⏰ یادت نره: آب بخورم")
    assert "✅" in edited
    assert query.edit_message_text.await_args.kwargs["reply_markup"] is None


def test_snooze_requeues_the_same_reminder_later(isolated_bot):
    bot, _ = isolated_bot
    reminder_id, _ = _delivered(bot)
    before = bot.reminder_store.get_owned(123, 123, reminder_id)["due_utc"]
    query = _callback(bot, f"rm:snooze:{reminder_id}")
    row = bot.reminder_store.get_owned(123, 123, reminder_id)
    assert row["status"] == "pending"
    assert row["due_utc"] > before
    assert row["text"] == "آب بخورم"
    assert "⏰" in query.edit_message_text.await_args.args[0]
    assert query.edit_message_text.await_args.kwargs["reply_markup"] is None


def test_a_snoozed_reminder_is_delivered_again(isolated_bot):
    bot, _ = isolated_bot
    reminder_id, _ = _delivered(bot)
    _callback(bot, f"rm:snooze:{reminder_id}")
    row = bot.reminder_store.get_owned(123, 123, reminder_id)
    bot.reminder_store.clock = lambda: row["due_utc"] + 1
    telegram = SimpleNamespace(send_message=AsyncMock())
    scheduler = bot.ReminderScheduler(bot.reminder_store, telegram)
    assert asyncio.run(scheduler.run_once())
    telegram.send_message.assert_awaited_once()
    assert "آب بخورم" in telegram.send_message.await_args.kwargs["text"]


def test_cancel_stops_a_pending_reminder(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("باشه، یادت می‌ندازم.")]
    update, context, _ = _telegram_update()
    update.message.text = "۱۰ دقیقه دیگه یادم بنداز آب بخورم"
    asyncio.run(bot.message_handler(update, context))
    row = bot.reminder_store.list_active(123, 123)[0]
    query = _callback(bot, f"rm:cancel:{row['id']}", text="باشه — یادت می‌ندازم.")
    assert _status(bot, row["id"]) == "cancelled"
    assert bot.reminder_store.list_active(123, 123) == []
    assert "✖️" in query.edit_message_text.await_args.args[0]


# --- the cases where nothing may happen -------------------------------------

def test_someone_else_pressing_the_button_changes_nothing(isolated_bot):
    bot, _ = isolated_bot
    group = -100123
    bot.add_allowed_group(group)
    reminder_id, _ = _delivered(bot, chat_id=group, user_id=123)
    query = _callback(bot, f"rm:done:{reminder_id}", user_id=999, chat_id=group)
    assert _status(bot, reminder_id, group) == "sent"
    assert query.edit_message_text.await_count == 0
    assert query.answer.await_args.kwargs["show_alert"] is True


def test_a_second_press_is_honest_about_the_first(isolated_bot):
    bot, _ = isolated_bot
    reminder_id, _ = _delivered(bot)
    _callback(bot, f"rm:done:{reminder_id}")
    query = _callback(bot, f"rm:snooze:{reminder_id}")
    assert _status(bot, reminder_id) == "done"
    assert query.edit_message_text.await_count == 0
    assert query.answer.await_count == 1


def test_cancelling_something_already_delivered_is_refused_plainly(isolated_bot):
    bot, _ = isolated_bot
    reminder_id, _ = _delivered(bot)
    query = _callback(bot, f"rm:cancel:{reminder_id}")
    assert _status(bot, reminder_id) == "sent"
    assert query.answer.await_args.kwargs["show_alert"] is True
    assert query.edit_message_text.await_count == 0


def test_a_stale_button_for_a_deleted_reminder_does_not_raise(isolated_bot):
    bot, _ = isolated_bot
    query = _callback(bot, "rm:done:999999")
    assert query.answer.await_count == 1
    assert query.edit_message_text.await_count == 0


@pytest.mark.parametrize("data", ["rm:", "rm:done", "rm:done:x", "admin:menu", "", "rm:delete:1"])
def test_unknown_callback_data_is_ignored(isolated_bot, data):
    bot, _ = isolated_bot
    query = _callback(bot, data)
    assert query.edit_message_text.await_count == 0


# --- failure handling --------------------------------------------------------

def test_a_failed_edit_still_records_the_action(isolated_bot):
    """Telegram refuses edits after two days; the decision still counts."""
    bot, _ = isolated_bot
    reminder_id, _ = _delivered(bot)
    query = SimpleNamespace(
        data=f"rm:done:{reminder_id}",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(side_effect=BadRequest("Message can't be edited")),
        message=SimpleNamespace(chat_id=123, text="⏰ یادت نره: آب بخورم"),
        from_user=SimpleNamespace(id=123),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=123, type="private"),
        message=None, callback_query=query,
    )
    asyncio.run(bot.reminder_callback(update, SimpleNamespace()))
    assert _status(bot, reminder_id) == "done"
    assert query.answer.await_args.kwargs.get("show_alert") is not True


def test_an_expired_callback_query_does_not_propagate(isolated_bot):
    bot, _ = isolated_bot
    reminder_id, _ = _delivered(bot)
    query = SimpleNamespace(
        data=f"rm:done:{reminder_id}",
        answer=AsyncMock(side_effect=BadRequest("query is too old")),
        edit_message_text=AsyncMock(),
        message=SimpleNamespace(chat_id=123, text="x"),
        from_user=SimpleNamespace(id=123),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=123, type="private"),
        message=None, callback_query=query,
    )
    asyncio.run(bot.reminder_callback(update, SimpleNamespace()))
    assert _status(bot, reminder_id) == "done"


def test_asking_about_a_finished_reminder_says_done_not_cancelled(isolated_bot):
    """The typed follow-up path has to know the new state too."""
    bot, _ = isolated_bot
    reminder_id, _ = _delivered(bot)
    _callback(bot, f"rm:done:{reminder_id}")
    answer = _send(bot, "یادآور ثبت شده؟")
    text = answer.message.reply_text.await_args.args[0]
    assert "انجام‌شده" in text
    assert "لغو" not in text


# --- the list reads like a sentence, not a database row ----------------------

def test_the_list_shows_a_date_a_person_can_read(isolated_bot):
    bot, _ = isolated_bot
    due = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=2, hours=1)
    bot.reminder_store.create(123, 123, ReminderSpec(due, "جلسه", "Asia/Tehran"))
    update, context, _ = _telegram_update()
    context.args = []
    asyncio.run(bot.reminders_command(update, context))
    text = update.message.reply_text.await_args.args[0]
    assert "Asia/Tehran" in text          # the zone appears once, in the header
    assert format_due(int(due.timestamp()), "Asia/Tehran")[:10] not in text
    assert "ساعت" in text                  # a clock word, not 09:00:00
    assert "جلسه" in text
