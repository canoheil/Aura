"""Secretary mode: answering a business account's customers, and its limits.

Most of these are about what must *not* happen: an answer through a connection
that was switched off, a second answer to the same message, a reply built from
the owner's private context, or a customer's message reaching the owner's own
Aura pipeline.
"""
import asyncio
import sqlite3
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.ext import ApplicationHandlerStop, BusinessConnectionHandler, TypeHandler, filters
from test_agent_tool_response import _text_response
from test_reminder_conversation import _update_with_text

import migrations
from business_mode import (
    BusinessStore,
    Incoming,
    Limits,
    Secretary,
    clean_reply,
    secretary_prompt,
)

BOT_ID = 900
OWNER_ID = 42
CONNECTION = "conn-1"


def _store(clock=None):
    clock = clock or [1_700_000_000.0]
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    return BusinessStore(db, threading.Lock(), clock=lambda: clock[0]), clock


def _connection(store, *, enabled=True, can_reply=True, owner_id=OWNER_ID):
    return store.save(connection_id=CONNECTION, owner_user_id=owner_id, owner_chat_id=owner_id,
                      is_enabled=enabled, can_reply=can_reply, owner_name="شرکت رضایی")


def _incoming(**overrides):
    values = dict(
        connection_id=CONNECTION, chat_id=555, chat_type="private", message_id=1,
        date=int(datetime.now(timezone.utc).timestamp()), from_user_id=555,
        from_is_bot=False, sender_business_bot_id=None, text="سلام، قیمت چنده؟",
    )
    values.update(overrides)
    return Incoming(**values)


def _limits(**overrides):
    values = dict(min_seconds_between_replies=0, max_replies_per_hour=30, max_reply_chars=900,
                  history_turns=10, max_message_age_seconds=180)
    values.update(overrides)
    return Limits(**values)


def _secretary(store, *, reply="سلام، پیام شما را به مدیر می‌رسانم.", sent=None, limits=None):
    sent = sent if sent is not None else []

    async def send(connection_id, chat_id, text):
        sent.append((connection_id, chat_id, text))
        return True

    def compose(**_kwargs):
        return reply

    return Secretary(store, compose=compose, send=send, limits=limits or _limits(),
                     bot_user_id=BOT_ID), sent


def _answer(secretary, incoming=None, **kwargs):
    return asyncio.run(secretary.handle(incoming or _incoming(), **kwargs))


# --- what may be answered ---------------------------------------------------

def test_a_customer_message_gets_one_reply_through_its_connection():
    store, _ = _store()
    _connection(store)
    secretary, sent = _secretary(store)
    assert _answer(secretary) == "sent"
    assert sent and sent[0][0] == CONNECTION and sent[0][1] == 555


@pytest.mark.parametrize("overrides,reason", [
    ({"connection_id": "other"}, "no_connection"),
    ({"chat_type": "supergroup"}, "group_not_allowed"),
    ({"from_is_bot": True}, "from_bot"),
    ({"sender_business_bot_id": BOT_ID}, "from_bot"),
    ({"from_user_id": OWNER_ID}, "from_owner"),
    ({"from_user_id": BOT_ID}, "from_owner"),
    ({"text": "   "}, "no_text"),
    ({"text": "/start"}, "no_text"),
    ({"date": 1}, "stale"),
])
def test_the_wrong_message_is_left_alone(overrides, reason):
    store, _ = _store()
    _connection(store)
    secretary, sent = _secretary(store)
    assert _answer(secretary, _incoming(**overrides)) == reason
    assert sent == []


def test_a_connection_that_is_off_answers_nothing():
    store, _ = _store()
    _connection(store, enabled=False)
    secretary, sent = _secretary(store)
    assert _answer(secretary) == "disabled"
    assert sent == []


def test_a_connection_without_the_right_to_reply_says_nothing():
    store, _ = _store()
    _connection(store, can_reply=False)
    secretary, sent = _secretary(store)
    assert _answer(secretary) == "no_rights"
    assert sent == []


def test_the_same_message_is_never_answered_twice():
    store, _ = _store()
    _connection(store)
    secretary, sent = _secretary(store)
    assert _answer(secretary) == "sent"
    assert _answer(secretary) == "duplicate"
    assert len(sent) == 1


def test_a_paused_mode_stays_quiet_even_for_a_valid_connection():
    store, _ = _store()
    _connection(store)
    store.set_paused(True)
    secretary, sent = _secretary(store)
    assert _answer(secretary) == "paused"
    assert sent == []


def test_two_replies_in_a_second_are_throttled_and_the_hour_is_capped():
    store, clock = _store()
    _connection(store)
    secretary, sent = _secretary(store, limits=_limits(min_seconds_between_replies=30))
    assert _answer(secretary, _incoming(message_id=1)) == "sent"
    clock[0] += 1
    assert _answer(secretary, _incoming(message_id=2)) == "throttled"

    store2, clock2 = _store()
    _connection(store2)
    capped, capped_sent = _secretary(store2, limits=_limits(max_replies_per_hour=2))
    for message_id in range(1, 4):
        clock2[0] += 5
        _answer(capped, _incoming(message_id=message_id))
    assert len(capped_sent) == 2


def test_a_reply_that_breaks_the_rules_is_not_sent_at_all():
    store, _ = _store()
    _connection(store)
    for bad in ("", "```python\nprint(1)\n```", "https://example.com/deal", "x" * 5000):
        secretary, sent = _secretary(store, reply=bad)
        assert _answer(secretary, _incoming(message_id=hash(bad) % 10_000)) == "unusable_reply"
        assert sent == []


def test_a_failing_model_or_telegram_keeps_the_owner_silent():
    store, _ = _store()
    _connection(store)

    def explode(**_kwargs):
        raise RuntimeError("provider down")

    failing = Secretary(store, compose=explode, send=lambda *a: True, limits=_limits())
    assert _answer(failing) == "compose_failed"

    store2, _ = _store()
    _connection(store2)

    def refuse(*_args):
        return False

    refusing = Secretary(store2, compose=lambda **k: "سلام", send=refuse, limits=_limits())
    assert _answer(refusing) == "send_failed"


def test_a_sync_send_is_accepted_like_an_async_one():
    store, _ = _store()
    _connection(store)
    sent = []
    secretary = Secretary(store, compose=lambda **k: "سلام",
                          send=lambda c, ch, t: sent.append(t) or True, limits=_limits())
    assert _answer(secretary) == "sent"
    assert sent


# --- the connection itself --------------------------------------------------

def test_enabling_remembers_the_connection_and_disabling_removes_it():
    store, _ = _store()
    secretary, _ = _secretary(store)
    saved = secretary.on_connection(connection_id=CONNECTION, owner_user_id=OWNER_ID,
                                    owner_chat_id=OWNER_ID, is_enabled=True,
                                    owner_name="شرکت رضایی")
    assert saved is not None and saved.is_enabled and saved.owner_name == "شرکت رضایی"
    assert store.get(CONNECTION).can_reply

    assert secretary.on_connection(connection_id=CONNECTION, owner_user_id=OWNER_ID,
                                   owner_chat_id=OWNER_ID, is_enabled=False) is None
    stored = store.get(CONNECTION)
    assert stored is not None and stored.is_enabled is False


def test_connections_are_listed_per_owner():
    store, _ = _store()
    _connection(store)
    store.save(connection_id="conn-2", owner_user_id=7, owner_chat_id=7, is_enabled=True)
    assert [c.id for c in store.list(OWNER_ID)] == [CONNECTION]
    assert len(store.list()) == 2
    assert store.owned_by(CONNECTION, OWNER_ID) is True
    assert store.owned_by(CONNECTION, 7) is False


# --- transcripts ------------------------------------------------------------

def test_the_transcript_keeps_only_its_tail_and_can_be_erased():
    store, clock = _store()
    _connection(store)
    for index in range(12):
        clock[0] += 1
        store.add_turn(CONNECTION, 555, "user", f"m{index}", keep=2, owner_user_id=OWNER_ID)
    # Two turns are kept, which is two exchanges: the older ones are dropped.
    assert [row["content"] for row in store.history(CONNECTION, 555)] == ["m8", "m9", "m10", "m11"]
    assert store.forget(connection_id=CONNECTION) >= 1


def test_dropping_a_connection_removes_it_completely():
    store, _ = _store()
    _connection(store)
    store.add_turn(CONNECTION, 555, "user", "a", owner_user_id=OWNER_ID)
    assert store.drop_connection(CONNECTION) is True
    assert store.get(CONNECTION) is None
    assert store.history(CONNECTION, 555) == []
    assert store.drop_connection(CONNECTION) is False


def test_forgetting_one_chat_leaves_another_alone():
    store, _ = _store()
    _connection(store)
    store.add_turn(CONNECTION, 555, "user", "a", owner_user_id=OWNER_ID)
    store.add_turn(CONNECTION, 556, "user", "b", owner_user_id=OWNER_ID)
    store.remember_reply(CONNECTION, 555, 1, owner_user_id=OWNER_ID)
    store.forget(connection_id=CONNECTION, chat_id=555)
    assert store.history(CONNECTION, 555) == []
    assert len(store.history(CONNECTION, 556)) == 1
    assert store.already_answered(CONNECTION, 555, 1) is False


def test_the_reply_is_recorded_so_a_restart_cannot_repeat_it():
    store, _ = _store()
    _connection(store)
    secretary, _ = _secretary(store)
    assert _answer(secretary) == "sent"
    assert store.already_answered(CONNECTION, 555, 1) is True
    assert [turn["role"] for turn in store.history(CONNECTION, 555)] == ["user", "assistant"]


# --- the words --------------------------------------------------------------

def test_the_prompt_states_the_boundary_it_must_not_cross():
    prompt = secretary_prompt("Sara", customer_name="Ali", first_contact=True, disclose_always=True)
    assert "Sara" in prompt and "Ali" in prompt
    for phrase in ("You are not Sara", "AI assistant", "never invent prices", "no access"):
        assert phrase.lower() in prompt.lower()


def test_the_disclosure_can_be_relaxed_without_losing_honesty():
    relaxed = secretary_prompt("Sara", disclose_always=False)
    assert "AI assistant" in relaxed
    assert "first message" not in relaxed


def test_a_reply_carrying_an_unasked_link_is_rejected():
    assert clean_reply("این هم لینک: https://t.me/x") == ""
    assert clean_reply("این هم لینک: https://t.me/x", source_text="بفرست https://t.me/x") != ""
    assert clean_reply("As an AI language model, I cannot") == ""
    assert clean_reply("سلام! در خدمتم.")


# --- the bot wiring ---------------------------------------------------------

def _connection_update(*, enabled=True, can_reply=True, owner_id=OWNER_ID):
    connection = SimpleNamespace(
        id=CONNECTION, is_enabled=enabled, user_chat_id=owner_id,
        user=SimpleNamespace(id=owner_id, first_name="Sara"),
        rights=SimpleNamespace(can_reply=can_reply, can_read=True),
    )
    return SimpleNamespace(business_connection=connection,
                           effective_user=SimpleNamespace(id=owner_id),
                           effective_chat=SimpleNamespace(id=owner_id, type="private"),
                           message=None, callback_query=None)


def _business_message(
    *, text="قیمت چنده؟", message_id=1, chat_id=555, connection_id=CONNECTION, from_id=555,
    is_bot=False, sender_bot=None, chat_type="private",
):
    message = SimpleNamespace(
        business_connection_id=connection_id, chat_id=chat_id, message_id=message_id,
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        date=datetime.now(timezone.utc),
        from_user=SimpleNamespace(id=from_id, first_name="Ali", is_bot=is_bot),
        sender_business_bot=sender_bot,
        text=text,
    )
    return SimpleNamespace(business_message=message, effective_user=SimpleNamespace(id=from_id),
                           effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
                           message=None, callback_query=None)


def _run_business(bot, update, telegram):
    context = SimpleNamespace(bot=telegram)
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(bot.business_message_handler(update, context))


def _telegram():
    return SimpleNamespace(id=BOT_ID, send_message=AsyncMock())


def test_the_connection_is_stored_and_the_owner_is_told(isolated_bot):
    bot, _ = isolated_bot
    telegram = _telegram()
    asyncio.run(bot.business_connection_handler(_connection_update(), SimpleNamespace(bot=telegram)))

    stored = bot.business_store.get(CONNECTION)
    assert stored is not None and stored.is_enabled and stored.owner_user_id == OWNER_ID
    telegram.send_message.assert_awaited_once()
    assert telegram.send_message.await_args.kwargs["chat_id"] == OWNER_ID


def test_a_disabled_connection_is_forgotten(isolated_bot):
    bot, _ = isolated_bot
    telegram = _telegram()
    asyncio.run(bot.business_connection_handler(_connection_update(), SimpleNamespace(bot=telegram)))
    asyncio.run(bot.business_connection_handler(
        _connection_update(enabled=False), SimpleNamespace(bot=telegram)))
    assert bot.business_store.get(CONNECTION).is_enabled is False


def test_a_customer_is_answered_with_the_connection_id(isolated_bot):
    bot, client = isolated_bot
    bot.BUSINESS_LIMITS = Limits(min_seconds_between_replies=0)
    client.models.responses = [_text_response("سلام! قیمت را مدیرم اعلام می‌کند.")]
    asyncio.run(bot.business_connection_handler(_connection_update(), SimpleNamespace(bot=_telegram())))

    telegram = _telegram()
    _run_business(bot, _business_message(), telegram)
    sent = telegram.send_message.await_args.kwargs
    assert sent["business_connection_id"] == CONNECTION
    assert sent["chat_id"] == 555
    assert "مدیرم" in sent["text"]


def test_the_customer_never_touches_the_owners_conversation(isolated_bot):
    bot, client = isolated_bot
    bot.BUSINESS_LIMITS = Limits(min_seconds_between_replies=0)
    client.models.responses = [_text_response("سلام، در خدمتم.")]
    asyncio.run(bot.business_connection_handler(_connection_update(), SimpleNamespace(bot=_telegram())))
    _run_business(bot, _business_message(), _telegram())
    # The owner's own history and the reminder store stay untouched: only the
    # system prompt exists, and no customer words were written into it.
    assert [row for row in bot.get_history(OWNER_ID) if row["role"] != "system"] == []
    assert bot.reminder_store.list_active(555, 555) == []
    # The customer's transcript lives in its own table instead.
    assert len(bot.business_store.history(CONNECTION, 555)) == 2


def test_the_pipeline_stops_so_the_owner_handler_never_sees_it(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("سلام")]
    asyncio.run(bot.business_connection_handler(_connection_update(), SimpleNamespace(bot=_telegram())))
    _run_business(bot, _business_message(), _telegram())
    # A business update is excluded from the ordinary text filter as well.
    update = Update.de_json({
        "update_id": 1,
        "business_message": {
            "message_id": 7, "date": 1_700_000_000,
            "chat": {"id": 555, "type": "private"},
            "from": {"id": 555, "is_bot": False, "first_name": "Ali"},
            "business_connection_id": CONNECTION, "text": "سلام",
        },
    }, bot=SimpleNamespace())
    assert isinstance(update, Update) and update.business_message is not None
    assert filters.UpdateType.BUSINESS_MESSAGES.check_update(update)
    assert not (filters.TEXT & ~filters.UpdateType.BUSINESS_MESSAGES).check_update(update)


def test_a_media_message_is_answered_without_guessing(isolated_bot):
    bot, _ = isolated_bot
    asyncio.run(bot.business_connection_handler(_connection_update(), SimpleNamespace(bot=_telegram())))
    telegram = _telegram()
    _run_business(bot, _business_message(text=None), telegram)
    assert "فقط پیام متنی" in telegram.send_message.await_args.kwargs["text"]


def test_a_message_for_an_unknown_connection_is_ignored(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = []
    telegram = _telegram()
    _run_business(bot, _business_message(connection_id="unknown"), telegram)
    telegram.send_message.assert_not_awaited()


def test_the_group_whitelist_does_not_gate_a_customer_chat(isolated_bot):
    """A customer's chat is the owner's business, not this deployment's groups."""
    bot, client = isolated_bot
    bot.BUSINESS_LIMITS = Limits(min_seconds_between_replies=0)
    bot.BUSINESS_ALLOW_GROUPS = True
    client.models.responses = [_text_response("سلام، پیام رسید.")]
    asyncio.run(bot.business_connection_handler(_connection_update(), SimpleNamespace(bot=_telegram())))
    update = _business_message(chat_id=-100999, chat_type="supergroup")
    asyncio.run(bot.group_access_guard(update, SimpleNamespace()))  # must not raise
    _run_business(bot, update, _telegram())


def test_business_messages_never_reach_the_ordinary_handler(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = []
    update, context = _update_with_text("دو دقیقه دیگه یادم بنداز آب بخورم")
    asyncio.run(bot.message_handler(update, context))
    assert bot.reminder_store.list_active(123, 123)


def test_the_owner_switch_pauses_and_resumes(isolated_bot):
    bot, client = isolated_bot
    bot.BUSINESS_LIMITS = Limits(min_seconds_between_replies=0)
    asyncio.run(bot.business_connection_handler(_connection_update(), SimpleNamespace(bot=_telegram())))
    bot.ADMIN_ACCESS = SimpleNamespace(allows=lambda user_id: user_id == OWNER_ID)

    update, context = _update_with_text("/secretary off", user_id=OWNER_ID)
    context.args = ["off"]
    asyncio.run(bot.secretary_command(update, context))
    assert bot.business_store.paused() is True

    telegram = _telegram()
    _run_business(bot, _business_message(message_id=2), telegram)
    telegram.send_message.assert_not_awaited()

    update, context = _update_with_text("/secretary on", user_id=OWNER_ID)
    context.args = ["on"]
    asyncio.run(bot.secretary_command(update, context))
    assert bot.business_store.paused() is False


def test_anyone_but_an_admin_is_refused(isolated_bot):
    bot, _ = isolated_bot
    bot.ADMIN_ACCESS = SimpleNamespace(allows=lambda user_id: user_id == OWNER_ID)
    update, context = _update_with_text("/secretary off", user_id=123)
    context.args = ["off"]
    asyncio.run(bot.secretary_command(update, context))
    assert bot.business_store.paused() is False
    assert "مدیر" in update.message.reply_text.await_args.args[0]


def test_the_status_lists_the_connection_and_how_to_stop_it(isolated_bot):
    bot, _ = isolated_bot
    asyncio.run(bot.business_connection_handler(_connection_update(), SimpleNamespace(bot=_telegram())))
    bot.ADMIN_ACCESS = SimpleNamespace(allows=lambda user_id: user_id == OWNER_ID)
    update, context = _update_with_text("/secretary", user_id=OWNER_ID)
    context.args = []
    asyncio.run(bot.secretary_command(update, context))
    text = update.message.reply_text.await_args.args[0]
    assert "Sara" in text and "secretary off" in text


def test_forget_erases_customer_transcripts_only(isolated_bot):
    bot, client = isolated_bot
    bot.BUSINESS_LIMITS = Limits(min_seconds_between_replies=0)
    bot.ADMIN_ACCESS = SimpleNamespace(allows=lambda user_id: user_id == OWNER_ID)
    client.models.responses = [_text_response("سلام")]
    asyncio.run(bot.business_connection_handler(_connection_update(), SimpleNamespace(bot=_telegram())))
    _run_business(bot, _business_message(), _telegram())
    assert bot.business_store.history(CONNECTION, 555)

    update, context = _update_with_text("/secretary forget", user_id=OWNER_ID)
    context.args = ["forget"]
    asyncio.run(bot.secretary_command(update, context))
    assert bot.business_store.history(CONNECTION, 555) == []
    # The permission itself survives: forgetting what was said is not the same
    # as disconnecting the account.
    assert bot.business_store.get(CONNECTION) is not None


def test_the_update_types_are_asked_for_explicitly(isolated_bot):
    bot, _ = isolated_bot
    bot.BUSINESS_MODE_ENABLED = True
    asked = bot._allowed_updates()
    assert bot.UpdateType.BUSINESS_CONNECTION in asked
    assert bot.UpdateType.BUSINESS_MESSAGE in asked
    bot.BUSINESS_MODE_ENABLED = False
    assert bot.UpdateType.BUSINESS_MESSAGE not in bot._allowed_updates()


def test_the_handlers_are_registered(isolated_bot):
    bot, _ = isolated_bot
    application = bot.build_application()
    handlers = [handler for group in application.handlers.values() for handler in group]
    assert any(isinstance(handler, BusinessConnectionHandler) for handler in handlers)
    assert any(getattr(handler, "callback", None) is bot.business_message_handler for handler in handlers)
    assert any(getattr(handler, "callback", None) is bot.secretary_command for handler in handlers)


def test_the_owner_can_delete_the_business_rows_with_their_data(isolated_bot):
    bot, _ = isolated_bot
    asyncio.run(bot.business_connection_handler(_connection_update(), SimpleNamespace(bot=_telegram())))
    plan = bot.data_deletion.plan(bot.db, OWNER_ID, lock=bot.db_lock)
    assert plan["business"] == 1
    deleted = bot.data_deletion.delete(bot.db, OWNER_ID, lock=bot.db_lock)
    assert deleted["business"] == 1
    row = bot.db.execute("SELECT 1 FROM business_connections WHERE id=?", (CONNECTION,)).fetchone()
    assert row is None


def test_an_edited_customer_message_is_not_answered_again(isolated_bot):
    """An edit is the customer changing their words; a second reply is noise."""
    bot, client = isolated_bot
    client.models.responses = []
    telegram = _telegram()
    edited = SimpleNamespace(
        edited_business_message=SimpleNamespace(
            business_connection_id=CONNECTION, chat_id=555, message_id=1,
            chat=SimpleNamespace(id=555, type="private"),
            date=datetime.now(timezone.utc), from_user=SimpleNamespace(id=555, is_bot=False),
            text="سلام، قیمت چنده؟ (ویرایش)", sender_business_bot=None,
        ),
        business_message=None, effective_user=SimpleNamespace(id=555),
        effective_chat=SimpleNamespace(id=555, type="private"), message=None, callback_query=None,
    )
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(bot.business_message_handler(edited, SimpleNamespace(bot=telegram)))
    telegram.send_message.assert_not_awaited()


def test_a_customer_can_return_with_a_new_message(isolated_bot):
    bot, client = isolated_bot
    bot.BUSINESS_LIMITS = Limits(min_seconds_between_replies=0)
    asyncio.run(bot.business_connection_handler(_connection_update(), SimpleNamespace(bot=_telegram())))
    client.models.responses = [_text_response("سلام، در خدمتم."), _text_response("بله، موجود است.")]
    _run_business(bot, _business_message(message_id=1), _telegram())
    _run_business(bot, _business_message(text="پس بفرستید", message_id=2), _telegram())
    transcript = bot.business_store.history(CONNECTION, 555)
    assert len(transcript) == 4 and transcript[2]["content"] == "پس بفرستید"


# --- the bug this section exists for ---------------------------------------
#
# python-telegram-bot runs **at most one handler per group** (the dispatch loop
# breaks after the first match). The group access guard is a `TypeHandler(Update)`
# and therefore matches everything, so anything registered beside it in group -1
# is dead code: Telegram delivers the update, PTB hands it to the guard, and the
# handler never runs. The connection update used to be registered exactly there.

def _dispatched_update(application, update):
    """Run one update through PTB's real dispatch loop, offline.

    `Application.process_update` is the production path; only the bot object is
    replaced, so a handler that is unreachable because of a group collision is
    unreachable here too.
    """
    application.bot = _telegram()
    application._initialized = True
    asyncio.run(application.process_update(update))
    return application.bot


def _connection_update_object(bot, *, connection_id=CONNECTION, enabled=True):
    return Update.de_json({
        "update_id": 1,
        "business_connection": {
            "id": connection_id,
            "user": {"id": OWNER_ID, "is_bot": False, "first_name": "Sara"},
            "user_chat_id": OWNER_ID, "date": int(datetime.now(timezone.utc).timestamp()),
            "is_enabled": enabled, "rights": {"can_reply": True, "can_read": True},
        },
    }, bot=SimpleNamespace())


def test_a_business_connection_update_actually_reaches_its_handler(isolated_bot):
    """The regression: a real dispatch, not a handler that merely exists."""
    bot, _ = isolated_bot
    application = bot.build_application()
    telegram = _dispatched_update(application, _connection_update_object(bot))

    stored = bot.business_store.get(CONNECTION)
    assert stored is not None and stored.is_enabled is True
    # And the owner was told, which only happens inside the handler.
    telegram.send_message.assert_awaited()


def test_no_catch_all_handler_shares_a_group_with_the_connection_handler(isolated_bot):
    """The rule behind the regression, asserted directly so it cannot return."""
    bot, _ = isolated_bot
    application = bot.build_application()
    for group, handlers in application.handlers.items():
        catches_everything = any(
            isinstance(handler, TypeHandler) and handler.callback is bot.group_access_guard
            for handler in handlers
        )
        if catches_everything:
            assert len(handlers) == 1, (
                f"group {group} holds the catch-all access guard and "
                f"{[type(other).__name__ for other in handlers if other.callback is not bot.group_access_guard]}; "
                "they can never run"
            )


def test_the_connection_handler_has_a_group_of_its_own(isolated_bot):
    bot, _ = isolated_bot
    application = bot.build_application()
    holding = [group for group, handlers in application.handlers.items()
               if any(isinstance(handler, BusinessConnectionHandler) for handler in handlers)]
    assert len(holding) == 1
    assert holding[0] == bot.GROUP_BUSINESS_CONNECTION
    assert holding[0] < bot.GROUP_ACCESS_GUARD < 0


# --- persistence and recovery (the restart-safe path) ----------------------

def test_a_stored_connection_survives_a_new_process():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    lock = threading.Lock()
    first = BusinessStore(db, lock)
    first.save(connection_id=CONNECTION, owner_user_id=OWNER_ID, owner_chat_id=OWNER_ID,
               is_enabled=True, can_reply=True, owner_name="شرکت رضایی")

    # A fresh store on the same file is what a restart looks like.
    second = BusinessStore(db, lock)
    restored = second.get(CONNECTION)
    assert restored is not None
    assert (restored.id, restored.owner_user_id, restored.is_enabled, restored.can_reply,
            restored.owner_name) == (CONNECTION, OWNER_ID, True, True, "شرکت رضایی")


def test_the_cache_never_serves_a_connection_that_changed():
    store, _ = _store()
    _connection(store)
    assert store.get(CONNECTION).is_enabled is True
    store.disable(CONNECTION)
    assert store.get(CONNECTION).is_enabled is False
    store.save(connection_id=CONNECTION, owner_user_id=OWNER_ID, owner_chat_id=OWNER_ID,
               is_enabled=True, can_reply=False)
    assert store.get(CONNECTION).can_reply is False


def test_restart_recovery_asks_telegram_instead_of_trusting_the_database():
    store, _ = _store()
    _connection(store, enabled=True)
    secretary, _ = _secretary(store)

    live = SimpleNamespace(is_enabled=True, rights=SimpleNamespace(can_reply=True, can_read=True),
                           user=SimpleNamespace(first_name="Sara"))

    async def fetch(connection_id):
        assert connection_id == CONNECTION
        return live

    outcome = asyncio.run(secretary.restore(fetch))
    assert outcome == {CONNECTION: "enabled"}
    assert store.get(CONNECTION).is_enabled is True


def test_recovery_switches_off_a_connection_the_owner_removed():
    store, _ = _store()
    _connection(store, enabled=True)
    secretary, _ = _secretary(store)

    async def gone(_connection_id):
        raise RuntimeError("Bad Request: BUSINESS_CONNECTION_INVALID")

    outcome = asyncio.run(secretary.restore(gone))
    assert outcome[CONNECTION].startswith("unavailable")
    # Kept as history, but nothing can be answered through it any more.
    assert store.get(CONNECTION).is_enabled is False


def test_recovery_picks_up_a_connection_that_arrived_while_the_bot_was_off(isolated_bot):
    bot, _ = isolated_bot
    bot.business_store.save(connection_id=CONNECTION, owner_user_id=OWNER_ID,
                            owner_chat_id=OWNER_ID, is_enabled=True, owner_name="Sara")
    telegram = _telegram()
    live = SimpleNamespace(is_enabled=True, rights=SimpleNamespace(can_reply=True, can_read=True),
                           user=SimpleNamespace(first_name="Sara"))
    telegram.get_business_connection = AsyncMock(return_value=live)
    application = SimpleNamespace(bot=telegram)

    outcome = asyncio.run(bot.restore_business_connections(application))
    telegram.get_business_connection.assert_awaited_once_with(CONNECTION)
    assert outcome == {CONNECTION: "enabled"}


def test_recovery_is_quiet_when_nothing_was_ever_granted(isolated_bot):
    bot, _ = isolated_bot
    telegram = _telegram()
    telegram.get_business_connection = AsyncMock()
    assert asyncio.run(bot.restore_business_connections(SimpleNamespace(bot=telegram))) == {}
    telegram.get_business_connection.assert_not_awaited()


def test_a_broken_recovery_never_blocks_startup(isolated_bot):
    bot, _ = isolated_bot
    bot.business_store.save(connection_id=CONNECTION, owner_user_id=OWNER_ID,
                            owner_chat_id=OWNER_ID, is_enabled=True)
    telegram = _telegram()
    telegram.get_business_connection = AsyncMock(side_effect=RuntimeError("network"))

    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    bot._business_secretary = broken
    assert asyncio.run(bot.restore_business_connections(SimpleNamespace(bot=telegram))) == {}


# --- polling settings -------------------------------------------------------

def test_polling_keeps_pending_updates_so_a_missed_connection_can_arrive(isolated_bot):
    """Telegram holds updates for 24h; dropping them loses the only notice."""
    bot, _ = isolated_bot
    kwargs = bot._polling_kwargs()
    assert kwargs["drop_pending_updates"] is False
    assert bot.UpdateType.BUSINESS_CONNECTION in kwargs["allowed_updates"]
    assert bot.UpdateType.BUSINESS_MESSAGE in kwargs["allowed_updates"]


def test_a_legacy_database_gains_the_connection_table():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    migrations.migrate(db, migrations=(migrations.MIGRATIONS[0],))
    tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    migrations.migrate(db, migrations=migrations.MIGRATIONS)
    tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"business_connections", "business_state", "business_messages", "business_replies"} <= tables
    columns = {row["name"] for row in db.execute("PRAGMA table_info(business_connections)")}
    assert {"id", "owner_user_id", "owner_chat_id", "is_enabled", "can_reply", "can_read"} <= columns
