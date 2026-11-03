import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from test_agent_tool_response import _telegram_update, _text_response

from reminders import ReminderSpec


def test_natural_request_is_persisted_without_model_guessing(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("حتماً، آب خوردن رو یادت میارم 💧")]
    update, context, _ = _telegram_update()
    update.message.text = "۱۰ دقیقه دیگه یادم بنداز آب بخورم"
    asyncio.run(bot.message_handler(update, context))
    rows = bot.reminder_store.list_active(123, 123)
    assert len(rows) == 1
    assert rows[0]["text"] == "آب بخورم"
    assert rows[0]["timezone"] == "Asia/Tehran"
    reply_text = update.message.reply_text.await_args.args[0]
    assert reply_text.startswith("حتماً، آب خوردن رو یادت میارم 💧")
    assert "⏰" in reply_text
    # The model only phrases the confirmation, after the actual row exists.
    assert len(client.models.requests) == 1
    assert '"phase": "confirmation"' in client.models.requests[0][1][0].parts[0].text


def test_incomplete_natural_request_asks_instead_of_claiming_success(isolated_bot):
    bot, client = isolated_bot
    update, context, _ = _telegram_update()
    update.message.text = "یادم بنداز آب بخورم"
    asyncio.run(bot.message_handler(update, context))
    assert not bot.reminder_store.list_active(123, 123)
    assert "زمان" in update.message.reply_text.await_args.args[0]
    assert client.models.requests == []


def test_timezone_command_schedule_list_and_cancel(isolated_bot):
    bot, _ = isolated_bot
    update, context, _ = _telegram_update()
    context.args = ["Europe/Berlin"]
    asyncio.run(bot.timezone_command(update, context))
    assert bot.reminder_store.get_timezone(123) == "Europe/Berlin"
    assert "قبلی تغییر نمی‌کند" in update.message.reply_text.await_args.args[0]
    target = (datetime.now(ZoneInfo("Europe/Berlin")) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    update, context, _ = _telegram_update()
    context.args = [*target.split(), "جلسه"]
    asyncio.run(bot.remind_command(update, context))
    rows = bot.reminder_store.list_active(123, 123)
    assert len(rows) == 1 and rows[0]["timezone"] == "Europe/Berlin"
    rid = rows[0]["id"]
    fallback = update.message.reply_text.await_args.args[0]
    assert fallback.startswith("باشه — ") and fallback.endswith("یادت می‌ندازم.")

    update, context, _ = _telegram_update()
    context.args = []
    asyncio.run(bot.reminders_command(update, context))
    assert f"#{rid}" in update.message.reply_text.await_args.args[0]
    assert "جلسه" in update.message.reply_text.await_args.args[0]

    update, context, _ = _telegram_update()
    context.args = [str(rid)]
    asyncio.run(bot.cancel_reminder_command(update, context))
    assert "لغو شد" in update.message.reply_text.await_args.args[0]
    assert bot.reminder_store.list_active(123, 123) == []


def test_list_reminders_preserves_literal_text_and_splits_between_items(isolated_bot):
    bot, _ = isolated_bot
    due = datetime.now(timezone.utc) + timedelta(hours=1)
    texts = [f"{i}: __init__ [1] & <tag> " + ("🙂" * 400) for i in range(7)]
    for text in texts:
        bot.reminder_store.create(123, 123, ReminderSpec(due, text, "Asia/Tehran"))
    update, context, _ = _telegram_update()
    context.args = []
    asyncio.run(bot.reminders_command(update, context))
    replies = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert len(replies) > 1
    assert all(len(reply.encode("utf-16-le")) // 2 <= 3500 for reply in replies)
    assert all(sum(text in reply for reply in replies) == 1 for text in texts)
    assert all("parse_mode" not in call.kwargs for call in update.message.reply_text.await_args_list)


def test_bad_timezone_does_not_replace_previous_value(isolated_bot):
    bot, _ = isolated_bot
    update, context, _ = _telegram_update()
    context.args = ["Nowhere/Bogus"]
    asyncio.run(bot.timezone_command(update, context))
    assert bot.reminder_store.get_timezone(123) == "Asia/Tehran"
    assert "پیدا نشد" in update.message.reply_text.await_args.args[0]


def test_application_lifecycle_starts_worker_and_cleans_up(isolated_bot, monkeypatch):
    bot, _ = isolated_bot
    clock = [datetime.now(timezone.utc).timestamp()]
    monkeypatch.setattr(bot.reminder_store, "clock", lambda: clock[0])
    bot.reminder_store.create(
        123, 123, ReminderSpec(datetime.fromtimestamp(clock[0] + 10, timezone.utc), "دارو", "Asia/Tehran")
    )
    clock[0] += 11

    async def run():
        sent = asyncio.Event()
        async def send_message(**kwargs):
            assert kwargs["chat_id"] == 123
            assert "دارو" in kwargs["text"]
            sent.set()
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(side_effect=send_message)), bot_data={})
        await bot.application_post_init(app)
        task = app.bot_data["reminder_task"]
        await bot.application_post_init(app)
        assert app.bot_data["reminder_task"] is task
        await asyncio.wait_for(sent.wait(), timeout=1)
        await bot.stop_reminder_scheduler(app)
        assert app.bot_data == {}
        assert bot.reminder_store.list_active(123, 123) == []
        # shutdown can call the cleanup a second time safely
        await bot.stop_reminder_scheduler(app)
    asyncio.run(run())
