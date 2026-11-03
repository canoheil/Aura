"""End-to-end reminder conversations against an in-memory DB and fake APIs."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_agent_tool_response import _telegram_update, _text_response

from reminders import DEFAULT_TIMEZONE, ReminderSpec, format_due, parse_reminder


@pytest.mark.parametrize("text,seconds,task", [
    ("دو دقیقه دیگه یادم بنداز اب بخورم", 120, "اب بخورم"),
    ("پنج دقیقع دیگه یادم بیار فلان کارو بکنم", 300, "فلان کارو بکنم"),
    ("یادم بیار بیست و پنج دقیقه دیگه دارو بخورم", 1500, "دارو بخورم"),
])
def test_written_numbers_and_colloquial_requests(text, seconds, task):
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    spec = parse_reminder(text, DEFAULT_TIMEZONE, now=now, natural=True)
    assert spec.due_utc == now + timedelta(seconds=seconds)
    assert spec.text == task


def test_missing_time_followup_actually_creates_the_reminder(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("حتماً، دو دقیقه دیگه آب خوردن رو یادت میارم 💧")]
    first, context, _ = _telegram_update()
    first.message.text = "یادم بنداز آب بخورم"
    asyncio.run(bot.message_handler(first, context))
    assert bot.reminder_store.list_active(123, 123) == []
    assert "ثبت شد" not in first.message.reply_text.await_args.args[0]

    second, context, _ = _telegram_update()
    second.message.text = "2 دقیقه دیگه"
    asyncio.run(bot.message_handler(second, context))
    rows = bot.reminder_store.list_active(123, 123)
    assert len(rows) == 1 and rows[0]["text"] == "آب بخورم"
    reply_text = second.message.reply_text.await_args.args[0]
    assert reply_text.startswith("حتماً، دو دقیقه دیگه آب خوردن رو یادت میارم 💧")
    assert "⏰" in reply_text


def test_a_day_without_a_clock_is_completed_by_the_clock_alone(isolated_bot):
    """«فردا صبح» is a real request; only the hour is missing."""
    bot, client = isolated_bot
    client.models.responses = [_text_response("باشه، فردا صبح یادت می‌ندازم.")]
    first = _send(bot, "یادم بنداز فردا صبح زنگ بزنم به مامان")
    assert "چه ساعتی" in first.message.reply_text.await_args.args[0]
    assert not bot.reminder_store.list_active(123, 123)

    _send(bot, "۹ صبح")
    rows = bot.reminder_store.list_active(123, 123)
    assert len(rows) == 1
    assert rows[0]["text"] == "زنگ بزنم به مامان"
    # Tomorrow at 09:00, and the task did not keep the half-stated time.
    assert format_due(rows[0]["due_utc"], DEFAULT_TIMEZONE).endswith("09:00:00 (Asia/Tehran)")


def test_a_phrase_the_grammar_cannot_read_is_verified_by_the_model(isolated_bot):
    """«سه هفته دیگه» is outside the parser; the model resolves it, the app checks it."""
    from test_agent_tool_response import _tool_response

    bot, client = isolated_bot
    clock = [datetime(2026, 11, 3, 8, 30, tzinfo=timezone.utc).timestamp()]
    bot.reminder_store.clock = lambda: clock[0]
    client.models.responses = [
        _tool_response("schedule_reminder", {
            "when": "سه هفته دیگه", "task": "قبض رو بدم", "local_datetime": "2026-11-24T12:00",
        }),
        _text_response("باشه، یادت می‌ندازم."),
    ]
    reply = _send(bot, "یادم بنداز سه هفته دیگه قبض رو بدم")
    rows = bot.reminder_store.list_active(123, 123)
    assert len(rows) == 1
    assert rows[0]["text"] == "قبض رو بدم"
    assert rows[0]["due_utc"] == int(datetime(2026, 11, 24, 8, 30, tzinfo=timezone.utc).timestamp())
    # Whatever the model says, the instant the app committed is always shown.
    assert "۳ آذر ۱۴۰۵" in reply.message.reply_text.await_args.args[0]


def test_a_model_instant_that_contradicts_the_words_is_never_committed(isolated_bot):
    """The proposal says three weeks; the clock says one. Nothing is scheduled."""
    from test_agent_tool_response import _tool_response

    bot, client = isolated_bot
    clock = [datetime(2026, 11, 3, 8, 30, tzinfo=timezone.utc).timestamp()]
    bot.reminder_store.clock = lambda: clock[0]
    client.models.responses = [
        _tool_response("schedule_reminder", {
            "when": "سه هفته دیگه", "task": "قبض رو بدم", "local_datetime": "2026-11-10T12:00",
        }),
    ]
    reply = _send(bot, "یادم بنداز سه هفته دیگه قبض رو بدم")
    assert not bot.reminder_store.list_active(123, 123)
    assert "یادت می" not in reply.message.reply_text.await_args.args[0]


def test_a_vague_time_gets_a_question_not_a_guess(isolated_bot):
    from test_agent_tool_response import _tool_response

    bot, client = isolated_bot
    clock = [datetime(2026, 11, 3, 8, 30, tzinfo=timezone.utc).timestamp()]
    bot.reminder_store.clock = lambda: clock[0]
    client.models.responses = [
        _tool_response("schedule_reminder", {
            "when": "هفته دیگه", "task": "قبض رو بدم", "local_datetime": "2026-11-10T12:00",
        }),
    ]
    reply = _send(bot, "یادم بنداز هفته دیگه قبض رو بدم")
    assert not bot.reminder_store.list_active(123, 123)
    # It explains why the phrase was too vague instead of silently picking a day.
    assert "کلی است" in reply.message.reply_text.await_args.args[0]


def test_the_parser_wins_over_the_model_when_both_can_read_the_time(isolated_bot):
    """A proposal cannot move a reminder the grammar already understands."""
    from test_agent_tool_response import _tool_response

    bot, client = isolated_bot
    clock = [datetime(2026, 11, 3, 8, 30, tzinfo=timezone.utc).timestamp()]
    bot.reminder_store.clock = lambda: clock[0]
    client.models.responses = [
        _tool_response("schedule_reminder", {
            "when": "۵ دقیقه دیگه", "task": "آب بخورم", "local_datetime": "2030-01-01T00:00",
        }),
        _text_response("باشه، یادت می‌ندازم."),
    ]
    _send(bot, "یادم بنداز ۵ دقیقه دیگه آب بخورم")
    rows = bot.reminder_store.list_active(123, 123)
    assert len(rows) == 1 and rows[0]["due_utc"] == clock[0] + 300


def test_a_message_with_no_time_words_does_not_call_the_model(isolated_bot):
    """Nothing to ground on, so asking is both cheaper and more honest."""
    bot, client = isolated_bot
    _send(bot, "یادم بنداز آب بخورم")
    assert not client.models.requests
    assert not bot.reminder_store.list_active(123, 123)


def test_a_weekday_request_can_be_completed_with_an_afternoon_clock(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("باشه، دوشنبه یادت می‌ندازم.")]
    _send(bot, "یادم بنداز دوشنبه جلسه دارم")
    _send(bot, "۵ عصر")
    rows = bot.reminder_store.list_active(123, 123)
    assert len(rows) == 1 and rows[0]["text"] == "جلسه دارم"
    assert format_due(rows[0]["due_utc"], DEFAULT_TIMEZONE).endswith("17:00:00 (Asia/Tehran)")


def test_due_message_uses_ai_and_tags_original_group_member(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [
        _text_response("حتماً، دو دقیقه دیگه نوشیدن آب رو یادت میارم."),
        _text_response("وقتشه یه لیوان آب بخوری؛ به سلامت! 💧"),
    ]
    update, context, _ = _telegram_update()
    update.effective_chat = SimpleNamespace(id=-100123, type="supergroup", title="group")
    update.message.text = "آورا دو دقیقه دیگه یادم بنداز آب بخورم"
    update.effective_user.first_name = "A<&"
    bot.add_allowed_group(-100123)
    asyncio.run(bot.message_handler(update, context))
    rows = bot.reminder_store.list_active(-100123, 123)
    assert len(rows) == 1
    clock = [rows[0]["due_utc"] + 1]
    bot.reminder_store.clock = lambda: clock[0]
    fake_tg = SimpleNamespace(send_message=AsyncMock())

    async def run():
        app = SimpleNamespace(bot=fake_tg, bot_data={})
        await bot.application_post_init(app)
        await asyncio.sleep(0.05)
        await bot.stop_reminder_scheduler(app)

    asyncio.run(run())
    sent = fake_tg.send_message.await_args.kwargs
    assert sent["chat_id"] == -100123
    assert "وقتشه" in sent["text"]
    assert "tg://user?id=123" in sent["text"]
    assert "A&lt;&amp;" in sent["text"]
    assert bot.reminder_store.list_active(-100123, 123) == []


def _update_with_text(text, *, user_id=123, chat_id=123, group=False):
    update, context, _ = _telegram_update()
    update.effective_user.id = user_id
    update.message.from_user = update.effective_user
    update.effective_chat = SimpleNamespace(id=chat_id, type="supergroup" if group else "private", title="group")
    update.message.text = text
    return update, context


def _send(bot, text, **kwargs):
    update, context = _update_with_text(text, **kwargs)
    asyncio.run(bot.message_handler(update, context))
    return update


def test_task_followup_and_greetings_do_not_create_unintended_reminders(isolated_bot):
    bot, client = isolated_bot
    first = _send(bot, "یادم بنداز پنج دقیقه دیگه")
    assert "چی رو" in first.message.reply_text.await_args.args[0]
    assert not bot.reminder_store.list_active(123, 123)

    client.models.responses = [_text_response("سلام! چه خبر؟")]
    _send(bot, "سلام")
    assert not bot.reminder_store.list_active(123, 123)

    # Resume explicitly after the conversation changed topic.
    _send(bot, "یادم بنداز پنج دقیقه دیگه")
    client.models.responses = [_text_response("باشه، یادت میارم داروت رو بخوری.")]
    reply = _send(bot, "دارو بخورم")
    rows = bot.reminder_store.list_active(123, 123)
    assert len(rows) == 1 and rows[0]["text"] == "دارو بخورم"
    reply_text = reply.message.reply_text.await_args.args[0]
    assert reply_text.startswith("باشه، یادت میارم داروت رو بخوری.")
    assert "⏰" in reply_text


def test_followup_is_scoped_to_same_user_and_chat(isolated_bot):
    bot, client = isolated_bot
    _send(bot, "یادم بنداز آب بخورم")
    client.models.responses = [_text_response("چه منظوری داری؟"), _text_response("چی رو میگی؟")]
    _send(bot, "دو دقیقه دیگه", user_id=999, chat_id=123)
    _send(bot, "دو دقیقه دیگه", user_id=123, chat_id=555)
    assert not bot.reminder_store.list_active(123, 999)
    assert not bot.reminder_store.list_active(555, 123)
    assert not bot.reminder_store.list_active(123, 123)

    client.models.responses = [_text_response("چشم، آب خوردن یادت میارم.")]
    _send(bot, "به نظرم دو دقیقه خوبه")
    assert bot.reminder_store.list_active(123, 123)[0]["text"] == "آب بخورم"


def test_group_pending_time_belongs_only_to_original_person_and_whitelist(isolated_bot):
    bot, client = isolated_bot
    group = -100123
    bot.add_allowed_group(group)
    _send(bot, "آورا یادم بنداز آب بخورم", chat_id=group, group=True)
    stranger = _send(bot, "دو دقیقه دیگه", user_id=999, chat_id=group, group=True)
    stranger.message.reply_text.assert_not_awaited()
    assert not bot.reminder_store.list_active(group, 999)

    client.models.responses = [_text_response("دو دقیقه دیگه یادآوریت می‌کنم.")]
    _send(bot, "دو دقیقه دیگه", chat_id=group, group=True)
    assert bot.reminder_store.list_active(group, 123)[0]["text"] == "آب بخورم"

    _send(bot, "آورا یادم بنداز دارو بخورم", chat_id=group, group=True)
    bot.remove_allowed_group(group)
    ignored = _send(bot, "دو دقیقه دیگه", chat_id=group, group=True)
    ignored.message.reply_text.assert_not_awaited()
    assert len(bot.reminder_store.list_active(group, 123)) == 1


def test_status_is_verified_from_db_and_never_invented(isolated_bot):
    bot, client = isolated_bot
    _send(bot, "یادم بنداز آب بخورم")
    unanswered = _send(bot, "مطمعنی؟")
    assert "هنوز ثبت نشده" in unanswered.message.reply_text.await_args.args[0]
    assert not bot.reminder_store.list_active(123, 123)

    listing, ctx = _update_with_text("/reminders")
    ctx.args = []
    asyncio.run(bot.reminders_command(listing, ctx))
    assert "یادآور فعالی نداری" in listing.message.reply_text.await_args.args[0]
    still_unanswered = _send(bot, "واقعاً؟")
    assert "هنوز ثبت نشده" in still_unanswered.message.reply_text.await_args.args[0]

    client.models.responses = [_text_response("حتماً یادم می‌مونه.")]
    _send(bot, "دو دقیقه دیگه")
    row = bot.reminder_store.list_active(123, 123)[0]
    confirmed = _send(bot, "یادآور فعاله؟")
    assert f"#{row['id']}" in confirmed.message.reply_text.await_args.args[0]
    assert "ثبت شده" in confirmed.message.reply_text.await_args.args[0]

    assert bot.reminder_store.cancel(123, 123, row["id"])
    cancelled = _send(bot, "مطمئنی؟")
    assert "لغو شده" in cancelled.message.reply_text.await_args.args[0]


def test_discard_incomplete_request_and_new_conversation_keeps_saved_reminders(isolated_bot):
    bot, client = isolated_bot
    _send(bot, "یادم بنداز دارو بخورم")
    reply = _send(bot, "بیخیال")
    assert "ثبت نکردم" in reply.message.reply_text.await_args.args[0]
    assert not bot._pending_reminder_context.get((123, 123))

    client.models.responses = [_text_response("حتماً، آب خوردن رو یادت میارم.")]
    _send(bot, "دو دقیقه دیگه یادم بنداز آب بخورم")
    _send(bot, "یادم بنداز دارو بخورم")
    update, ctx = _update_with_text("/new")
    asyncio.run(bot.new_command(update, ctx))
    assert not bot._pending_reminder_context.get((123, 123))
    assert len(bot.reminder_store.list_active(123, 123)) == 1


def test_no_false_confirmation_when_db_commit_or_model_fails(isolated_bot, monkeypatch):
    bot, client = isolated_bot
    def fail_create(*_args, **_kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(bot.reminder_store, "create", fail_create)
    client.models.responses = [_text_response("تنظیم شد")]
    update = _send(bot, "دو دقیقه دیگه یادم بنداز آب بخورم")
    assert "نتونستم" in update.message.reply_text.await_args.args[0]
    assert "ثبت شد" not in update.message.reply_text.await_args.args[0]
    assert client.models.requests == []  # never ask the model to confirm an uncommitted row
    assert not bot.reminder_store.list_active(123, 123)


def test_model_outage_after_commit_uses_honest_confirmation_and_due_fallback(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [RuntimeError("Gemini down"), RuntimeError("Gemini still down")]
    update = _send(bot, "دو دقیقه دیگه یادم بنداز آب بخورم")
    row = bot.reminder_store.list_active(123, 123)[0]
    fallback = update.message.reply_text.await_args.args[0]
    assert fallback.startswith("باشه — ") and fallback.endswith("یادت می‌ندازم.")
    bot.reminder_store.clock = lambda: row["due_utc"] + 1
    fake_tg = SimpleNamespace(send_message=AsyncMock())
    worker = bot.ReminderScheduler(bot.reminder_store, fake_tg, compose_message=lambda r: bot._natural_reminder_phrase(r, "delivery"))
    asyncio.run(worker.run_once())
    assert "آب بخورم" in fake_tg.send_message.await_args.kwargs["text"]
    assert not bot.reminder_store.list_active(123, 123)


def test_false_model_promises_without_tool_are_replaced(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("آره، یادآور رو تنظیم کردم و دو دقیقه دیگه یادآوری می‌کنم.")]
    update = _send(bot, "دو دقیقه دیگه")
    reply = update.message.reply_text.await_args.args[0]
    assert "ثبت یا لغو نشده" in reply
    assert not bot.reminder_store.list_active(123, 123)


def test_general_agent_has_a_real_grounded_reminder_tool(isolated_bot):
    from test_agent_tool_response import _tool_response
    bot, client = isolated_bot
    client.models.responses = [
        _tool_response("schedule_reminder", {"when": "۵ دقیقه دیگه", "task": "قرص بخورم", "_reminder": {"chat_id": 999}}),
        _text_response("حتماً، خوردن قرص رو یادت میارم."),
    ]
    text = "می‌خوام یه یادآور داشته باشم؛ ۵ دقیقه دیگه قرص بخورم"
    update = _send(bot, text)
    rows = bot.reminder_store.list_active(123, 123)
    assert len(rows) == 1 and rows[0]["text"] == "قرص بخورم"
    assert not bot.reminder_store.list_active(999, 123)
    reply_text = update.message.reply_text.await_args.args[0]
    assert reply_text.startswith("حتماً، خوردن قرص رو یادت میارم.")
    assert "⏰" in reply_text
    tool_names = {d.name for t in client.models.requests[0][2].tools for d in t.function_declarations}
    assert "schedule_reminder" in tool_names


def test_model_can_distinguish_scheduling_time_from_duration_in_task(isolated_bot):
    from test_agent_tool_response import _tool_response
    bot, client = isolated_bot
    client.models.responses = [
        _tool_response("schedule_reminder", {"when": "پنج دقیقه دیگه", "task": "دو ساعت ورزش کنم"}),
        _text_response("باشه، ورزش رو یادت میارم!"),
    ]
    clock = [datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()]
    bot.reminder_store.clock = lambda: clock[0]
    update = _send(bot, "پنج دقیقه دیگه یادم بنداز دو ساعت ورزش کنم")
    rows = bot.reminder_store.list_active(123, 123)
    assert len(rows) == 1 and rows[0]["text"] == "دو ساعت ورزش کنم"
    assert rows[0]["due_utc"] == clock[0] + 300
    fallback = update.message.reply_text.await_args.args[0]
    assert fallback.startswith("باشه — ") and fallback.endswith("یادت می‌ندازم.")
    cfg = client.models.requests[0][2]
    assert cfg.tool_config.function_calling_config.allowed_function_names == ["schedule_reminder"]


@pytest.mark.parametrize("when,task", [
    ("۵ دقیقه دیگه", "دو ساعت ورزش کنم"),  # invented time
    ("پنج دقیقه دیگه", "ورزش کنم"),        # drops the duration from the task
    ("پنج دقیقه دیگه", "بخوابم"),           # invented task
])
def test_ambiguous_interpretation_cannot_invent_or_drop_details(isolated_bot, when, task):
    from test_agent_tool_response import _tool_response
    bot, client = isolated_bot
    client.models.responses = [_tool_response("schedule_reminder", {"when": when, "task": task})]
    reply = _send(bot, "پنج دقیقه دیگه یادم بنداز دو ساعت ورزش کنم")
    assert not bot.reminder_store.list_active(123, 123)
    assert "ثبت شد" not in reply.message.reply_text.await_args.args[0]


def test_tool_cannot_act_without_explicit_user_request(isolated_bot):
    from test_agent_tool_response import _tool_response
    bot, client = isolated_bot
    client.models.responses = [_tool_response("schedule_reminder", {"when": "۵ دقیقه دیگه", "task": "قرص بخورم"})]
    reply = _send(bot, "داستانی بنویس که شخصیتش گفت ۵ دقیقه دیگه قرص بخورم")
    assert not bot.reminder_store.list_active(123, 123)
    assert "ثبت نکردم" in reply.message.reply_text.await_args.args[0]


def test_recipient_and_task_are_escaped_and_model_uses_real_task(isolated_bot):
    from test_agent_tool_response import _text_response
    bot, client = isolated_bot
    group = -100123
    bot.add_allowed_group(group)
    client.models.responses = [
        _text_response("باشه، یادت میارم."),
        _text_response("وقتشه <b>کار رو انجام بدی</b> & حواست به خودت باشه!"),
    ]
    update, context = _update_with_text("آورا ۲ دقیقه دیگه یادم بنداز <script> & دارو", chat_id=group, group=True)
    update.effective_user.first_name = 'A<&"'
    asyncio.run(bot.message_handler(update, context))
    row = bot.reminder_store.list_active(group, 123)[0]
    assert row["text"] == "<script> & دارو"
    bot.reminder_store.clock = lambda: row["due_utc"] + 1
    tg = SimpleNamespace(send_message=AsyncMock())
    worker = bot.ReminderScheduler(bot.reminder_store, tg, compose_message=lambda r: bot._natural_reminder_phrase(r, "delivery"))
    asyncio.run(worker.run_once())
    text = tg.send_message.await_args.kwargs["text"]
    assert 'tg://user?id=123' in text
    assert "A&lt;&amp;&quot;" in text
    assert "&lt;b&gt;" not in text  # unrelated model wording falls back to the saved task
    assert "&lt;script&gt; &amp; دارو" in text
    assert '<script>' not in text
    assert '"phase": "delivery"' in client.models.requests[1][1][0].parts[0].text


def test_pending_task_does_not_consume_an_unrelated_question(isolated_bot):
    bot, client = isolated_bot
    _send(bot, "یادم بنداز پنج دقیقه دیگه")
    client.models.responses = [_text_response("امروز آفتابیه.")]
    _send(bot, "فردا هوا چطوره")
    assert not bot.reminder_store.list_active(123, 123)


def test_generic_reminder_claims_cannot_fake_cancellation(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("یادآور رو لغو کردم.")]
    response = _send(bot, "لطفاً یادآور من رو لغو کن")
    assert "ثبت یا لغو نشده" in response.message.reply_text.await_args.args[0]


def test_tool_time_and_task_must_match_whole_user_words(isolated_bot):
    bot, _ = isolated_bot
    update, _ = _update_with_text("۱۵ دقیقه دیگه یادم بنداز آب بخورم")
    context = bot._reminder_runtime_context(update, update.message.text)
    result = bot._tool_schedule_reminder({"when": "۵ دقیقه دیگه", "task": "آب بخورم", "_reminder": context})
    assert result["ok"] is False
    assert not bot.reminder_store.list_active(123, 123)


def test_tool_rejects_competing_times_across_two_turns(isolated_bot):
    bot, _ = isolated_bot
    update, _ = _update_with_text("ده دقیقه دیگه دارو بخورم")
    pending = {"request": "پنج دقیقه دیگه یادم بنداز", "waiting": "missing_task"}
    context = bot._reminder_runtime_context(update, update.message.text, pending)
    result = bot._tool_schedule_reminder({"when": "پنج دقیقه دیگه", "task": "دارو بخورم", "_reminder": context})
    assert result["ok"] is False
    assert not bot.reminder_store.list_active(123, 123)


def test_confirmation_comes_after_the_commit(isolated_bot, monkeypatch):
    bot, client = isolated_bot
    def generate_content(*, model, contents, config):
        rows = bot.reminder_store.list_active(123, 123)
        assert len(rows) == 1 and rows[0]["text"] == "آب بخورم"
        return _text_response("چشم، آب خوردن رو یادت میارم.")
    monkeypatch.setattr(client.models, "generate_content", generate_content)
    response = _send(bot, "دو دقیقه دیگه یادم بنداز آب بخورم")
    reply_text = response.message.reply_text.await_args.args[0]
    assert reply_text.startswith("چشم، آب خوردن رو یادت میارم.")
    assert "⏰" in reply_text


@pytest.mark.parametrize("text", [
    "این جمله رو ترجمه کن: «پنج دقیقه دیگه یادم بنداز آب بخورم»",
    "نمی‌خوام پنج دقیقه دیگه یادم بنداز آب بخورم",
])
def test_quoted_or_negated_reminder_is_not_registered(isolated_bot, text):
    from test_agent_tool_response import _tool_response
    bot, client = isolated_bot
    client.models.responses = [_tool_response("schedule_reminder", {"when": "پنج دقیقه دیگه", "task": "آب بخورم"})]
    reply = _send(bot, text)
    assert not bot.reminder_store.list_active(123, 123)
    assert "ثبت" not in reply.message.reply_text.await_args.args[0] or "نکردم" in reply.message.reply_text.await_args.args[0]


def test_status_question_without_ephemeral_state_checks_database(isolated_bot):
    bot, _ = isolated_bot
    update = _send(bot, "یادآور فعاله؟")
    assert "یادآور فعالی نداری" in update.message.reply_text.await_args.args[0]
    due = datetime.now(timezone.utc) + timedelta(minutes=5)
    bot.reminder_store.create(123, 123, ReminderSpec(due, "دارو", "Asia/Tehran"))
    update = _send(bot, "یادآور فعاله؟")
    assert "1 یادآور فعال" in update.message.reply_text.await_args.args[0]


def test_timed_out_model_interpretation_cannot_write_later(isolated_bot, monkeypatch):
    import threading

    from test_agent_tool_response import _tool_response
    bot, _ = isolated_bot
    release = threading.Event()
    monkeypatch.setattr(bot, "REMINDER_MODEL_TIMEOUT_SECONDS", 0.01)
    def slow_model(_ctx, _model):
        release.wait(timeout=3)
        return _tool_response("schedule_reminder", {"when": "پنج دقیقه دیگه", "task": "دو ساعت ورزش کنم"})
    monkeypatch.setattr(bot, "_model_reminder_interpretation", slow_model)
    reply, context = _update_with_text("پنج دقیقه دیگه یادم بنداز دو ساعت ورزش کنم")
    async def run():
        try:
            await bot.message_handler(reply, context)
            assert "ثبت شد" not in reply.message.reply_text.await_args.args[0]
            assert not bot.reminder_store.list_active(123, 123)
        finally:
            release.set()
    asyncio.run(run())  # waits for the late worker to finish
    assert not bot.reminder_store.list_active(123, 123)


def test_changing_topic_does_not_make_are_you_sure_about_old_reminder(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("آب خوردن یادت میارم.")]
    _send(bot, "دو دقیقه دیگه یادم بنداز آب بخورم")
    client.models.responses = [_text_response("به نظرم کتاب خوبیه."), _text_response("بله، دلیلش رو گفتم.")]
    _send(bot, "نظرت درباره این کتاب چیه؟")
    reply = _send(bot, "مطمئنی؟")
    assert "یادآور #" not in reply.message.reply_text.await_args.args[0]
    assert len(bot.reminder_store.list_active(123, 123)) == 1


def test_pending_task_does_not_consume_a_new_reminder_request(isolated_bot):
    from test_agent_tool_response import _tool_response
    bot, client = isolated_bot
    _send(bot, "یادم بنداز پنج دقیقه دیگه")
    client.models.responses = [
        _tool_response("schedule_reminder", {"when": "دو دقیقه دیگه", "task": "آب بخورم"}),
        _text_response("حتماً، آب خوردن یادت میارم."),
    ]
    _send(bot, "می‌خوام یه یادآور داشته باشم؛ دو دقیقه دیگه آب بخورم")
    rows = bot.reminder_store.list_active(123, 123)
    assert len(rows) == 1 and rows[0]["text"] == "آب بخورم"


def test_time_reply_after_topic_change_does_not_finish_old_reminder(isolated_bot):
    bot, client = isolated_bot
    _send(bot, "یادم بنداز آب بخورم")
    client.models.responses = [_text_response("برای جلسه از مسئولش بپرس."), _text_response("متوجه شدم.")]
    _send(bot, "جلسه کی شروع میشه؟")
    _send(bot, "دو دقیقه دیگه")
    assert not bot.reminder_store.list_active(123, 123)


def test_contradictory_model_wording_is_not_used_for_committed_confirmation(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("متأسفم، یادآور ثبت نشده و نتونستم تنظیمش کنم.")]
    reply = _send(bot, "دو دقیقه دیگه یادم بنداز آب بخورم")
    text = reply.message.reply_text.await_args.args[0]
    assert "ثبت نشده" not in text
    assert text.startswith("باشه — ") and text.endswith("یادت می‌ندازم.")
    assert len(bot.reminder_store.list_active(123, 123)) == 1


def test_memory_recall_question_is_not_diverted_to_reminders(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("قبلاً گفتی اسمت سامانه.")]
    reply = _send(bot, "یادم بیار اسمم چیه؟")
    assert "سامانه" in reply.message.reply_text.await_args.args[0]
    assert not bot._pending_reminder_context.get((123, 123))
    assert not bot.reminder_store.list_active(123, 123)


def test_model_tool_followup_missing_time_is_grounded_and_persisted(isolated_bot):
    from test_agent_tool_response import _tool_response
    bot, client = isolated_bot
    client.models.responses = [_tool_response("schedule_reminder", {"when": "", "task": "آب بخورم"})]
    first = _send(bot, "می‌خوام یه یادآور داشته باشم که آب بخورم")
    assert "هنوز چیزی ثبت نکردم" in first.message.reply_text.await_args.args[0]
    assert not bot.reminder_store.list_active(123, 123)
    client.models.responses = [
        _tool_response("schedule_reminder", {"when": "دو دقیقه", "task": "آب بخورم"}),
        _text_response("حتماً، آب خوردن رو یادت میارم."),
    ]
    reply = _send(bot, "دو دقیقه")
    rows = bot.reminder_store.list_active(123, 123)
    assert len(rows) == 1 and rows[0]["text"] == "آب بخورم"
    reply_text = reply.message.reply_text.await_args.args[0]
    assert reply_text.startswith("حتماً، آب خوردن رو یادت میارم.")
    assert "⏰" in reply_text


def test_gemini_failure_and_transient_telegram_failure_do_not_lose_delivery(isolated_bot):
    from telegram.error import NetworkError
    bot, client = isolated_bot
    client.models.responses = [
        _text_response("دارو رو بهت یادآوری می‌کنم."),
        RuntimeError("Gemini unavailable"),
        _text_response("وقت داروته؛ مراقب خودت باش."),
    ]
    _send(bot, "دو دقیقه دیگه یادم بنداز دارو بخورم")
    row = bot.reminder_store.list_active(123, 123)[0]
    clock = [row["due_utc"] + 1]
    bot.reminder_store.clock = lambda: clock[0]
    tg = SimpleNamespace(send_message=AsyncMock(side_effect=[NetworkError("network down"), None]))
    worker = bot.ReminderScheduler(bot.reminder_store, tg, compose_message=lambda r: bot._natural_reminder_phrase(r, "delivery"))
    asyncio.run(worker.run_once())
    retry = bot.reminder_store.list_active(123, 123)[0]
    assert retry["status"] == "pending" and retry["last_error"] == "NetworkError"
    clock[0] += 30
    asyncio.run(worker.run_once())
    assert "وقت داروته" in tg.send_message.await_args.kwargs["text"]
    assert bot.reminder_store.list_active(123, 123) == []


def test_even_vague_success_words_cannot_confirm_without_a_tool_result(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("باشه، انجام شد! 👌")]
    response = _send(bot, "می‌خوام یه یادآور داشته باشم؛ ۵ دقیقه دیگه آب بخورم")
    assert "هنوز این یادآور ثبت نشده" in response.message.reply_text.await_args.args[0]
    assert not bot.reminder_store.list_active(123, 123)


def test_explaining_reminder_feature_does_not_schedule_or_override_answer(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("می‌تونی با دستور /remind زمان و متن رو بفرستی.")]
    response = _send(bot, "می‌خوام درباره تنظیم یادآور بدونم")
    assert "/remind" in response.message.reply_text.await_args.args[0]
    assert not bot.reminder_store.list_active(123, 123)


def test_recent_reminder_natural_cancellation_really_updates_database(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("آب خوردن رو یادت میارم.")]
    _send(bot, "دو دقیقه دیگه یادم بنداز آب بخورم")
    row = bot.reminder_store.list_active(123, 123)[0]
    reply = _send(bot, "لغوش کن")
    assert "لغو شد" in reply.message.reply_text.await_args.args[0]
    assert bot.reminder_store.list_active(123, 123) == []
    assert bot.reminder_store.get_owned(123, 123, row["id"])["status"] == "cancelled"


def test_natural_cancellation_does_not_claim_success_for_inflight_reminder(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("آب خوردن رو یادت میارم.")]
    _send(bot, "دو دقیقه دیگه یادم بنداز آب بخورم")
    row = bot.reminder_store.list_active(123, 123)[0]
    bot.reminder_store.clock = lambda: row["due_utc"] + 1
    assert bot.reminder_store.claim_due()["id"] == row["id"]
    reply = _send(bot, "لغوش کن")
    assert "نمی‌تونم لغوش کنم" in reply.message.reply_text.await_args.args[0]
    assert bot.reminder_store.get_owned(123, 123, row["id"])["status"] == "sending"


def test_confirmation_rejects_a_hallucinated_different_time(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("حتماً، ده دقیقه دیگه آب خوردن رو یادت میارم.")]
    reply = _send(bot, "دو دقیقه دیگه یادم بنداز آب بخورم")
    text = reply.message.reply_text.await_args.args[0]
    assert "ده دقیقه" not in text
    assert text.startswith("باشه — ") and text.endswith("یادت می‌ندازم.")
    assert bot.reminder_store.list_active(123, 123)[0]["text"] == "آب بخورم"


def test_correct_model_wording_can_naturally_repeat_the_verified_time(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("حتماً، دو دقیقه دیگه آب خوردن رو یادت میارم 💧")]
    reply = _send(bot, "دو دقیقه دیگه یادم بنداز آب بخورم")
    assert "دو دقیقه دیگه" in reply.message.reply_text.await_args.args[0]
