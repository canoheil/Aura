"""Localization: catalog integrity, language resolution and the /language command."""
import asyncio
import sqlite3
from pathlib import Path

import pytest

import i18n
from i18n import LanguageStore, normalize_language, resolve_language, t
from test_agent_tool_response import _telegram_update, _text_response

# --- catalog ---------------------------------------------------------------

def test_every_key_has_all_languages():
    assert i18n.missing_translations() == []


def test_no_translation_is_empty_or_identical_across_languages():
    for key, entry in i18n.CATALOG.items():
        for code in i18n.LANGUAGES:
            assert entry[code].strip(), f"{key} is empty in {code}"
    # A copy-paste that leaves English text in the Persian slot is a real bug.
    for key, entry in i18n.CATALOG.items():
        if entry["fa"] == entry["en"]:
            assert key in {
                "whoami.body", "settings.denied",
            }, f"{key} was not translated"


def test_placeholders_are_consistent_between_languages():
    import re

    pattern = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
    for key, entry in i18n.CATALOG.items():
        fa = set(pattern.findall(entry["fa"]))
        en = set(pattern.findall(entry["en"]))
        assert fa == en, f"{key} uses different placeholders: {fa} vs {en}"


def test_unknown_placeholders_are_left_alone():
    # Extra values are ignored, and only the named placeholders are substituted,
    # so a stray brace in a translation can never raise at runtime.
    assert t("new.done", "en", extra="x") == t("new.done", "en")
    rendered = t("model.changed", "en", model="m", unused=1)
    assert "m" in rendered and "{model}" not in rendered
    assert "{unused}" not in rendered


def test_missing_key_is_visible_and_logged_once():
    assert t("no.such.key", "en") == "no.such.key"


def test_language_normalization():
    assert normalize_language("EN") == "en"
    assert normalize_language("en-US") == "en"
    assert normalize_language("English") == "en"
    assert normalize_language("فارسی") == "fa"
    assert normalize_language("klingon") == "fa"  # deployment default
    assert normalize_language("") == "fa"


# --- store -----------------------------------------------------------------

@pytest.fixture()
def store():
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return LanguageStore(connection)


def test_a_user_choice_is_remembered(store):
    assert store.get(7) is None
    store.set(7, "en")
    assert store.get(7) == "en"
    store.set(7, "fa")
    assert store.get(7) == "fa"
    assert store.clear(7) is True
    assert store.get(7) is None


def test_resolution_precedence(store):
    store.set(7, "en")
    # explicit beats the stored choice, which beats the deployment default
    assert resolve_language(explicit="fa", store=store, user_id=7, default="en") == "fa"
    assert resolve_language(store=store, user_id=7, default="fa") == "en"
    assert resolve_language(store=store, user_id=8, default="en") == "en"
    assert resolve_language(store=store, user_id=8, default="nonsense") == "fa"


def test_the_prompt_names_the_interface_language():
    assert "English" in i18n.system_instruction("en")
    assert "Persian" in i18n.system_instruction("fa")
    # Unknown codes must not silently produce an English instruction.
    assert "Persian" in i18n.system_instruction(None)


# --- handlers --------------------------------------------------------------

def test_default_language_is_still_persian(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = [_text_response("سلام")]
    update, context, _ = _telegram_update()
    asyncio.run(bot.language_command(update, context))
    assert "زبان فعلی" in update.message.reply_text.await_args.args[0]


def test_language_command_switches_and_confirms_in_the_new_language(isolated_bot):
    bot, _ = isolated_bot
    update, context, _ = _telegram_update()
    context.args = ["en"]
    asyncio.run(bot.language_command(update, context))
    assert bot.LANGUAGE_STORE.get(123) == "en"
    assert "Language changed to English" in update.message.reply_text.await_args.args[0]

    update, context, _ = _telegram_update()
    asyncio.run(bot.language_command(update, context))
    assert "Current language: English" in update.message.reply_text.await_args.args[0]


def test_an_unsupported_language_is_rejected(isolated_bot):
    bot, _ = isolated_bot
    update, context, _ = _telegram_update()
    context.args = ["klingon"]
    asyncio.run(bot.language_command(update, context))
    assert bot.LANGUAGE_STORE.get(123) is None
    # The user is still on the default (Persian), so the refusal is in Persian.
    assert "زبان پشتیبانی نمی‌شود" in update.message.reply_text.await_args.args[0]


def test_commands_answer_in_the_selected_language(isolated_bot):
    bot, client = isolated_bot
    bot.LANGUAGE_STORE.set(123, "en")

    update, context, _ = _telegram_update()
    asyncio.run(bot.start_command(update, context))
    assert "your personal AI assistant" in update.message.reply_text.await_args.args[0]

    update, context, _ = _telegram_update()
    asyncio.run(bot.reminders_command(update, context))
    assert update.message.reply_text.await_args.args[0] == "You have no active reminders in this chat."

    update, context, _ = _telegram_update()
    context.args = ["nowhere-bogus"]
    asyncio.run(bot.timezone_command(update, context))
    assert "timezone was not found" in update.message.reply_text.await_args.args[0]


def test_the_deployment_setting_changes_the_default_for_everyone(isolated_bot):
    bot, _ = isolated_bot
    bot.SETTINGS.set("bot_language", "en", actor=1)

    update, context, _ = _telegram_update()
    update.effective_user.id = 999
    asyncio.run(bot.language_command(update, context))
    assert "Current language: English" in update.message.reply_text.await_args.args[0]

    # An explicit per-user choice still wins over the deployment default.
    bot.LANGUAGE_STORE.set(999, "fa")
    update, context, _ = _telegram_update()
    update.effective_user.id = 999
    asyncio.run(bot.language_command(update, context))
    assert "زبان فعلی" in update.message.reply_text.await_args.args[0]


def test_the_system_prompt_carries_the_language_instruction(isolated_bot):
    bot, _ = isolated_bot
    prompt_fa = bot.build_system_prompt(user_id=123)
    assert "interface language is Persian" in prompt_fa

    bot.LANGUAGE_STORE.set(123, "en")
    prompt_en = bot.build_system_prompt(user_id=123)
    assert "interface language is English" in prompt_en


def test_english_users_get_english_reminder_errors(isolated_bot):
    bot, client = isolated_bot
    bot.LANGUAGE_STORE.set(123, "en")
    client.models.responses = [_text_response("ok")]
    update, context, _ = _telegram_update()
    update.message.text = "remind me"
    asyncio.run(bot.message_handler(update, context))
    reply = update.message.reply_text.await_args.args[0]
    assert "when should I remind you?" in reply


def test_every_reminder_error_code_has_a_translation():
    """A reminder error can only reach the user if its code is in the catalog."""
    import ast

    tree = ast.parse(Path("reminders.py").read_text(encoding="utf-8"))
    codes = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or getattr(node.func, "id", None) != "ReminderError":
            continue
        for keyword in node.keywords:
            if keyword.arg == "code" and isinstance(keyword.value, ast.Constant):
                codes.add(keyword.value.value)
    assert codes, "the parser should raise coded errors"
    missing = sorted(code for code in codes if not i18n.has_key(f"reminder.err.{code}"))
    assert missing == []


def test_a_coded_reminder_error_is_translated(isolated_bot):
    bot, _ = isolated_bot
    from reminders import ReminderError

    exc = ReminderError("متن یادآوری خیلی طولانی است؛ حداکثر 500 کاراکتر.", code="text_too_long")
    assert bot.reminder_error_text(exc, "en") ==         "The reminder text is too long; at most 500 characters."
    # Persian output keeps the original parser wording.
    assert bot.reminder_error_text(exc, "fa") == exc.args[0]


def test_reminder_delivery_falls_back_to_the_recipients_language():
    from reminders import ReminderScheduler

    scheduler = ReminderScheduler(store=None, bot=None)
    row = {"id": 1, "text": "drink water", "user_id": 5, "chat_id": 5}
    assert asyncio.run(scheduler._message_text(row, "en")) == "Reminder: drink water"
    assert asyncio.run(scheduler._message_text(row, "fa")) == "یادت نره: drink water"
    # Without a language the original Persian fallback is unchanged.
    assert asyncio.run(scheduler._message_text(row)) == "یادت نره: drink water"


def test_weather_errors_are_localized(isolated_bot):
    bot, _ = isolated_bot
    from weather_api import ResolvedLocation, WeatherError

    not_found = WeatherError("Geocoding found nothing.", code="not_found")
    assert "couldn't find that city" in bot.weather_error_message(not_found, "en")
    assert "پیدا نکردم" in bot.weather_error_message(not_found, "fa")

    # An unknown code must still produce a readable message, not a bare key.
    unexpected = WeatherError("boom", code="upstream")
    message = bot.weather_error_message(unexpected, "en")
    assert message and not message.startswith("weather.") and "weather service" in message

    choices = (
        ResolvedLocation(name="Berlin", country="Germany", admin1="Berlin",
                         latitude=52.5, longitude=13.4, timezone="Europe/Berlin"),
        ResolvedLocation(name="Berlin", country="United States", admin1="New Hampshire",
                         latitude=44.5, longitude=-71.2, timezone="America/New_York"),
    )
    ambiguous = WeatherError("ambiguous", code="ambiguous", choices=choices)
    english = bot.weather_error_message(ambiguous, "en")
    assert "which one do you mean?" in english
    assert "1. Berlin, Germany" in english and "2. Berlin" in english


def test_weather_report_is_rendered_in_the_selected_language(isolated_bot):
    bot, _ = isolated_bot
    data = {
        "location": "Berlin",
        "current": {"condition": "Clear sky", "temperature_c": 12, "feels_like_c": 10},
        "daily": [{"date": "2026-11-03", "condition": "Cloudy", "temp_min_c": 5,
                   "temp_max_c": 13, "precipitation_probability_max": 40}],
    }
    english = bot._format_selected_weather(data, "en")
    assert "feels like 10" in english
    assert "5–13°C" in english
    assert "40% chance of precipitation" in english
    assert "احساس‌شده" not in english

    persian = bot._format_selected_weather(data, "fa")
    assert "احساس‌شده" in persian and "تا" in persian


def test_every_key_used_in_source_exists():
    """A typo would otherwise reach the user as a bare key like ``reminder.cancel.done``."""
    import ast

    used = set()
    for name in ("bot.py", "providers.py", "error_handling.py", "reminders.py"):
        tree = ast.parse(Path(name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = getattr(func, "id", None) or getattr(func, "attr", None)
            if called not in {"tr", "_t", "t", "has_key", "system_instruction"}:
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue  # dynamically built keys (e.g. f"reminder.err.{code}")
            key = node.args[0].value
            if called == "system_instruction" or not isinstance(key, str):
                continue
            used.add(key)
    assert used, "the application should reference catalog keys"
    missing = sorted(key for key in used if not i18n.has_key(key))
    assert missing == []


def test_no_persian_literal_is_sent_straight_to_the_user():
    """Every user-visible message must come from the catalog, not a literal.

    Model-facing prompts may stay Persian; only text handed to Telegram counts.
    """
    import ast

    persian = __import__("re").compile("[\u0600-\u06FF]")
    tree = ast.parse(Path("bot.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name not in {"reply_text", "reply_html", "_safe_reply_text", "send_long_message", "edit_text"}:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and persian.search(arg.value):
                offenders.append((node.lineno, arg.value[:60]))
    assert offenders == []


def test_canned_and_offer_wording_follows_the_language(isolated_bot):
    import re

    bot, _ = isolated_bot
    persian = re.compile("[\u0600-\u06FF]")

    english = bot._bare_call_responses("en")
    assert english and not any(persian.search(item) for item in english)
    assert any(persian.search(item) for item in bot._bare_call_responses("fa"))

    # The offer only exists when the local vector renderer is installed.
    bot.SVG_IMAGE_AVAILABLE = True
    offer = bot._svg_fallback_hint(None, "en")
    assert "vector (SVG)" in offer and not persian.search(offer)
    assert "برداری" in bot._svg_fallback_hint(None, "fa")
