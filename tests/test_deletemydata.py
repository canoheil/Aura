"""`/deletemydata`: what is deleted, what never is, and the consent step."""
import asyncio
import re
import sqlite3
import threading
from pathlib import Path

import pytest
from test_agent_tool_response import _telegram_update

import data_deletion
import i18n
from data_deletion import CATEGORIES, delete, plan, total

PERSIAN = re.compile("[\u0600-\u06FF]")

_PERSONAL_SCHEMA = """
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    role TEXT NOT NULL, content TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE conversations (user_id INTEGER PRIMARY KEY, model TEXT NOT NULL,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE conversation_contexts (conversation_id INTEGER PRIMARY KEY,
    summary TEXT NOT NULL DEFAULT '', summarized_until_message_id INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE video_contexts (conversation_id INTEGER PRIMARY KEY, youtube_url TEXT NOT NULL,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_user_id INTEGER NOT NULL,
    memory_type TEXT NOT NULL, content TEXT NOT NULL, memory_key TEXT NOT NULL,
    importance INTEGER NOT NULL DEFAULT 3, confidence REAL NOT NULL DEFAULT 0.9,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, last_used_at INTEGER,
    expires_at INTEGER, active INTEGER NOT NULL DEFAULT 1, UNIQUE(owner_user_id, memory_key));
CREATE TABLE user_language (user_id INTEGER PRIMARY KEY, language TEXT NOT NULL,
    updated_at INTEGER NOT NULL);
CREATE TABLE user_usage (user_id INTEGER NOT NULL, usage_date TEXT NOT NULL,
    turns INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL, PRIMARY KEY (user_id, usage_date));
"""


# --- the module -------------------------------------------------------------

def _populated_db(user_id=7, other_id=8):
    """A database with personal rows for two users, built through the real stores."""
    from aura_identity import AuraIdentity  # noqa: F401  (import order sanity)
    from reminders import ReminderSpec, ReminderStore

    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    lock = threading.RLock()
    # The same tables the application creates at startup, minus everything the
    # deletion never touches (that absence is part of what these tests prove).
    db.executescript(_PERSONAL_SCHEMA)
    store = ReminderStore(db, lock)
    from datetime import datetime, timedelta, timezone

    due = datetime.now(timezone.utc) + timedelta(hours=1)
    for uid in (user_id, other_id):
        db.execute("INSERT INTO messages (user_id, role, content, created_at) VALUES (?, 'user', 'hi', 0)", (uid,))
        db.execute("INSERT INTO messages (user_id, role, content, created_at) VALUES (?, 'model', 'hello', 0)", (uid,))
        db.execute("INSERT INTO conversations (user_id, model, created_at, updated_at) VALUES (?, 'm', 0, 0)", (uid,))
        db.execute("INSERT INTO conversation_contexts (conversation_id, summary, updated_at) VALUES (?, 's', 0)", (uid,))
        db.execute("INSERT INTO video_contexts (conversation_id, youtube_url, created_at, updated_at)"
                   " VALUES (?, 'https://youtu.be/x', 0, 0)", (uid,))
        db.execute("INSERT INTO memories (owner_user_id, memory_type, content, memory_key, created_at, updated_at)"
                   " VALUES (?, 'explicit', 'likes tea', 'tea', 0, 0)", (uid,))
        db.execute("INSERT INTO user_language (user_id, language, updated_at) VALUES (?, 'en', 0)", (uid,))
        db.execute("INSERT INTO user_usage (user_id, usage_date, turns, tokens, updated_at)"
                   " VALUES (?, '2026-01-01', 3, 0, 0)", (uid,))
        store.set_timezone(uid, "Europe/Berlin")
        store.create(uid, uid, ReminderSpec(due_utc=due, text="drink water", timezone="Europe/Berlin"))
    db.commit()
    return db, lock


def test_every_personal_table_is_covered():
    keys = {category.key for category in CATEGORIES}
    assert keys == {
        "messages", "model", "summary", "video", "memories", "reminders", "timezone", "language",
        "usage",
    }


def test_every_category_has_a_label_in_both_languages():
    for category in CATEGORIES:
        assert i18n.has_key(category.label_key), category.label_key
        for code in i18n.LANGUAGES:
            assert i18n.CATALOG[category.label_key][code].strip()


def test_the_plan_counts_only_this_user():
    db, lock = _populated_db()
    counts = plan(db, 7, lock=lock)
    assert counts["messages"] == 2
    assert counts["memories"] == 1
    assert counts["reminders"] == 1
    assert counts["language"] == 1
    assert counts["usage"] == 1
    assert total(counts) == 10


def test_deletion_removes_only_this_user_and_reports_counts():
    db, lock = _populated_db()
    deleted = delete(db, 7, lock=lock)
    assert deleted["messages"] == 2 and deleted["reminders"] == 1
    assert total(plan(db, 7, lock=lock)) == 0
    # The other user is untouched.
    assert plan(db, 8, lock=lock)["messages"] == 2


def test_deleting_twice_is_harmless():
    db, lock = _populated_db()
    delete(db, 7, lock=lock)
    assert total(delete(db, 7, lock=lock)) == 0


def test_a_missing_table_counts_as_zero():
    db = sqlite3.connect(":memory:")
    counts = plan(db, 7)
    assert set(counts) == {category.key for category in CATEGORIES}
    assert total(counts) == 0
    assert total(delete(db, 7)) == 0


def test_a_failure_rolls_every_table_back():
    db, lock = _populated_db()
    # Drop one table mid-way by making it unwritable is awkward in SQLite; a
    # BEFORE DELETE trigger that raises is a faithful stand-in for a real error.
    db.execute(
        "CREATE TRIGGER block_delete BEFORE DELETE ON memories "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        delete(db, 7, lock=lock)
    # Nothing was removed: the user is never told "deleted" while rows remain.
    assert plan(db, 7, lock=lock)["messages"] == 2


def test_a_callers_transaction_is_not_folded_into_the_delete():
    db, lock = _populated_db()
    db.execute("INSERT INTO messages (user_id, role, content, created_at) VALUES (99, 'user', 'pending', 0)")
    assert db.in_transaction is True
    delete(db, 7, lock=lock)
    # The unrelated pending insert is still the caller's to commit.
    assert db.in_transaction is True
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM messages WHERE user_id = 99").fetchone()[0] == 0


# --- the command ------------------------------------------------------------

def _write_some_data(bot, user_id=123):
    bot.db.execute("INSERT INTO messages (user_id, role, content, created_at) VALUES (?, 'user', 'hi', 0)", (user_id,))
    bot.db.execute("INSERT INTO messages (user_id, role, content, created_at) VALUES (?, 'model', 'hello', 0)", (user_id,))
    bot.save_memory(user_id, "explicit", "likes tea", 5, 1.0)
    bot.LANGUAGE_STORE.set(user_id, "en")
    bot.db.commit()


def _run(bot, args=(), lang="en"):
    if lang:
        bot.LANGUAGE_STORE.set(123, lang)
    update, context, _ = _telegram_update()
    context.args = list(args)
    asyncio.run(bot.deletemydata_command(update, context))
    return update.message.reply_text.await_args.args[0]


def test_the_preview_lists_counts_and_asks_for_confirmation(isolated_bot):
    bot, _ = isolated_bot
    _write_some_data(bot)
    reply = _run(bot)
    assert "everything I have stored about you" in reply
    assert "Conversation messages: 2" in reply
    assert "Long-term memories: 1" in reply
    assert "/deletemydata confirm" in reply
    # The preview alone changes nothing.
    assert data_deletion.plan(bot.db, 123)["messages"] == 2


def test_confirm_without_a_preview_deletes_nothing(isolated_bot):
    bot, _ = isolated_bot
    _write_some_data(bot)
    reply = _run(bot, ["confirm"])
    assert "send /deletemydata first" in reply
    assert data_deletion.plan(bot.db, 123)["messages"] == 2


def test_confirm_after_preview_really_deletes(isolated_bot):
    bot, _ = isolated_bot
    _write_some_data(bot)
    _run(bot)
    reply = _run(bot, ["confirm"])
    assert "personal item(s) removed" in reply
    assert data_deletion.plan(bot.db, 123)["messages"] == 0
    assert bot.LANGUAGE_STORE.get(123) is None
    # The reply still arrives, in the language the user had before the wipe.
    assert not PERSIAN.search(reply)


def test_cancel_changes_nothing(isolated_bot):
    bot, _ = isolated_bot
    _write_some_data(bot)
    _run(bot)
    assert "nothing was deleted" in _run(bot, ["cancel"])
    assert data_deletion.plan(bot.db, 123)["messages"] == 2
    # The pending confirmation is gone, so a later "confirm" needs a new preview.
    assert "send /deletemydata first" in _run(bot, ["confirm"], lang=None)


def test_a_bad_argument_shows_usage_and_keeps_the_pending_preview(isolated_bot):
    bot, _ = isolated_bot
    _write_some_data(bot)
    _run(bot)
    # A typo explains the syntax instead of quietly cancelling the preview.
    assert "/deletemydata confirm" in _run(bot, ["delete"])
    assert "personal item(s) removed" in _run(bot, ["confirm"])


def test_a_group_chat_is_refused(isolated_bot):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    bot, _ = isolated_bot
    _write_some_data(bot)
    update, context, _ = _telegram_update()
    update.effective_chat = SimpleNamespace(id=-100123, type="supergroup")
    update.message.reply_text = AsyncMock()
    context.args = []
    asyncio.run(bot.deletemydata_command(update, context))
    reply = update.message.reply_text.await_args.args[0]
    assert "only works in a private chat" in reply
    assert data_deletion.plan(bot.db, 123)["messages"] == 2


def test_nothing_stored_says_so_without_asking(isolated_bot):
    bot, _ = isolated_bot
    # The deployment speaks English, so nothing has to be stored per user.
    bot.SETTINGS.set("bot_language", "en", actor=1)
    reply = _run(bot, lang=None)
    assert "nothing personal stored" in reply
    # A later confirm must not create a "deleted" message out of thin air.
    assert "send /deletemydata first" in _run(bot, ["confirm"])


def test_the_reply_is_persian_for_persian_users(isolated_bot):
    bot, _ = isolated_bot
    _write_some_data(bot)
    assert PERSIAN.search(_run(bot, lang="fa"))


def test_the_final_reply_keeps_the_language_that_was_just_deleted(isolated_bot):
    """The language row is gone after the wipe; the confirmation still answers in it."""
    bot, _ = isolated_bot
    _write_some_data(bot)  # this sets the language to English
    _run(bot)
    reply = _run(bot, ["confirm"], lang=None)
    assert "personal item(s) removed" in reply
    assert not PERSIAN.search(reply)
    assert bot.LANGUAGE_STORE.get(123) is None


def test_the_command_is_registered():
    source = (Path(__file__).resolve().parent.parent / "bot.py").read_text(encoding="utf-8")
    assert 'CommandHandler("deletemydata", deletemydata_command)' in source


def test_the_preview_lists_only_non_zero_categories(isolated_bot):
    bot, _ = isolated_bot
    bot.LANGUAGE_STORE.set(123, "en")
    bot.save_memory(123, "explicit", "likes tea", 5, 1.0)
    reply = _run(bot)
    assert "Long-term memories: 1" in reply
    assert "Conversation messages: 0" not in reply
