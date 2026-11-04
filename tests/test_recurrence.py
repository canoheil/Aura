"""Repeating reminders: the rule, the phrase, and what happens when one fires.

The rule is small on purpose, so most of these are about the edges: the first
occurrence after a request, what a missed day costs, a snooze that must not
move the series, and the requests this build refuses instead of guessing at.
"""
import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_reminder_buttons import _callback
from test_reminder_conversation import _send, _update_with_text

import migrations
from recurrence import (
    WORKING_DAYS,
    Recurrence,
    RecurrencePhrase,
    finalize,
    find_recurrence_phrase,
    is_unsupported_recurrence,
    parse_month_day,
)
from reminder_time import ReminderError, parse_reminder
from reminders import ReminderScheduler, ReminderSpec, ReminderStore, reminder_buttons

ZONE = "Asia/Tehran"
NOW = datetime(2026, 11, 3, 8, 30, tzinfo=timezone.utc)  # Tue 12:00 in Tehran


def _local(spec_or_due):
    due = spec_or_due if isinstance(spec_or_due, datetime) else spec_or_due.due_utc
    from reminder_time import timezone_for

    return due.astimezone(timezone_for(ZONE))


def _parse(text):
    return parse_reminder(text, ZONE, now=NOW, natural=True)


# --- the rule ---------------------------------------------------------------

@pytest.mark.parametrize("rule", [
    "daily@08:00",
    "daily/2@08:00",
    "weekly@08:00:5",
    "weekly@08:00:0,2",
    "weekly/2@08:00:1",
    "weekly@08:00:0,1,2,3,4",
    "monthly@09:00:15",
    "monthly/3@09:00:5",
])
def test_a_stored_rule_survives_a_round_trip(rule):
    assert str(Recurrence.parse(rule)) == rule


@pytest.mark.parametrize("rule", ["", "daily", "daily@8", "weekly@08:00", "weekly/2@08:00:1,2",
                                  "monthly@24:00:1", "yearly@08:00", "daily/0@08:00", None])
def test_an_unreadable_rule_is_refused_rather_than_guessed(rule):
    assert Recurrence.parse(rule) is None


def test_a_daily_rule_fires_tomorrow_at_the_same_wall_clock():
    rule = Recurrence.parse("daily@08:00")
    assert _local(rule.first_after(NOW, ZONE_TZ())).strftime("%a %Y-%m-%d %H:%M") == "Wed 2026-11-04 08:00"


def ZONE_TZ():
    from reminder_time import timezone_for

    return timezone_for(ZONE)


def test_a_weekly_rule_picks_the_next_named_day():
    rule = Recurrence.parse("weekly@10:00:5")  # Saturday
    assert _local(rule.first_after(NOW, ZONE_TZ())).strftime("%a %Y-%m-%d") == "Sat 2026-11-07"


def test_an_interval_of_two_weeks_is_measured_from_the_last_occurrence():
    rule = Recurrence.parse("weekly/2@09:00:1")  # Tuesday, every other week
    first = rule.first_after(NOW, ZONE_TZ())
    assert _local(first).strftime("%a %Y-%m-%d") == "Tue 2026-11-17"
    second = rule.next_after(first, first, ZONE_TZ())
    assert _local(second).strftime("%a %Y-%m-%d") == "Tue 2026-12-01"


def test_a_month_without_the_31st_uses_its_last_day():
    rule = Recurrence.parse("monthly@09:00:31")
    january = datetime(2027, 1, 31, 6, 0, tzinfo=timezone.utc)
    assert _local(rule.next_after(january, january, ZONE_TZ())).strftime("%Y-%m-%d") == "2027-02-28"


def test_a_wall_clock_inside_a_dst_gap_moves_forward_instead_of_vanishing():
    """Berlin skips 02:30 on 2027-03-28; a daily 02:30 reminder must still exist."""
    rule = Recurrence.parse("daily@02:30")
    previous = datetime(2027, 3, 27, 1, 30, tzinfo=timezone.utc)
    fire = rule.next_after(previous, previous, ZONE_TZ_BERLIN())
    assert fire > previous
    assert fire.astimezone(ZONE_TZ_BERLIN()).strftime("%Y-%m-%d %H:%M") == "2027-03-28 03:00"


def ZONE_TZ_BERLIN():
    from reminder_time import timezone_for

    return timezone_for("Europe/Berlin")


def test_an_outage_costs_one_late_reminder_not_one_per_missed_day():
    rule = Recurrence.parse("daily@08:00")
    previous = datetime(2026, 11, 4, 4, 30, tzinfo=timezone.utc)
    later = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)  # six days of downtime
    fire = rule.next_after(previous, later, ZONE_TZ())
    # The very next one after "now", not a backlog of six.
    assert _local(fire).strftime("%Y-%m-%d %H:%M") == "2026-11-11 08:00"


@pytest.mark.parametrize("rule,fa,en", [
    ("daily@08:00", "هر روز، ساعت ۸:۰۰ صبح", "every day at 08:00"),
    ("weekly@08:00:0,2", "هر دوشنبه و چهارشنبه، ساعت ۸:۰۰ صبح",
     "every Monday and Wednesday at 08:00"),
    ("weekly@08:00:0,1,2,3,4", "روزهای کاری، ساعت ۸:۰۰ صبح", "weekdays at 08:00"),
    ("monthly@09:00:15", "هر ماه پانزدهم، ساعت ۹:۰۰ صبح", "every month on the 15th at 09:00"),
    ("monthly/3@09:00:5", "هر سه ماه، پنجم، ساعت ۹:۰۰ صبح",
     "every three months on the 5th at 09:00"),
    ("weekly/2@08:00:1", "هر دو هفته، سه‌شنبه، ساعت ۸:۰۰ صبح",
     "every two weeks on Tuesday at 08:00"),
])
def test_a_rule_says_itself_in_both_languages(rule, fa, en):
    assert Recurrence.parse(rule).describe("fa") == fa
    assert Recurrence.parse(rule).describe("en") == en


# --- reading the request ----------------------------------------------------

@pytest.mark.parametrize("text,rule,first", [
    ("هر روز ساعت ۸ صبح یادم بنداز قرص بخورم", "daily@08:00", "2026-11-04 08:00"),
    ("روزانه ساعت ۷ بامداد یادم بنداز ورزش کنم", "daily@07:00", "2026-11-04 07:00"),
    ("هر دو روز ساعت ۸ صبح یادم بنداز گلدون‌ها رو آب بدم", "daily/2@08:00", "2026-11-05 08:00"),
    ("هر ۳ روز ساعت ۸ صبح یادم بنداز گلدون‌ها رو آب بدم", "daily/3@08:00", "2026-11-06 08:00"),
    ("هر شنبه ساعت ۱۰ یادم بنداز بازار برم", "weekly@10:00:5", "2026-11-07 10:00"),
    ("هر دوشنبه و چهارشنبه ساعت ۱۹:۳۰ یادم بنداز کلاس دارم", "weekly@19:30:0,2", "2026-11-04 19:30"),
    ("هر هفته ساعت ۸:۳۰ صبح یادم بنداز جلسه دارم", "weekly@08:30:1", "2026-11-10 08:30"),
    ("هر دو هفته ساعت ۹ صبح یادم بنداز حساب‌ها رو چک کنم", "weekly/2@09:00:1", "2026-11-17 09:00"),
    ("هر ماه ساعت ۹ صبح یادم بنداز قبض رو بدم", "monthly@09:00:3", "2026-12-03 09:00"),
    ("هر ماه پانزدهم ساعت ۹ صبح یادم بنداز قبض رو بدم", "monthly@09:00:15", "2026-11-15 09:00"),
    ("هر ماه ۱۵ ساعت ۹ صبح یادم بنداز قبض رو بدم", "monthly@09:00:15", "2026-11-15 09:00"),
    ("هر روز کاری ساعت ۸:۳۰ یادم بنداز سر کار", "weekly@08:30:0,1,2,3,4", "2026-11-04 08:30"),
    ("every day at 8am remind me to take pills", "daily@08:00", "2026-11-04 08:00"),
    ("every two weeks at 9am remind me to check the books", "weekly/2@09:00:1", "2026-11-17 09:00"),
])
def test_a_repeating_request_becomes_a_rule_and_a_first_time(text, rule, first):
    spec = _parse(text)
    assert spec.recurrence == rule
    assert _local(spec).strftime("%Y-%m-%d %H:%M") == first


def test_the_repeating_words_are_not_part_of_the_task():
    spec = _parse("هر روز ساعت ۸ صبح یادم بنداز قرص بخورم")
    assert spec.text == "قرص بخورم"
    assert "هر روز" not in spec.text and "۸" not in spec.text


def test_the_task_may_contain_its_own_numbers_and_weekdays():
    spec = _parse("هر دوشنبه ساعت ۹ صبح یادم بنداز سه تا قرص بخورم")
    assert spec.text == "سه تا قرص بخورم"
    assert spec.recurrence == "weekly@09:00:0"


def test_weekdays_are_read_with_or_without_the_zero_width_joiner():
    assert _parse("هر یک شنبه ساعت ۹ صبح یادم بنداز مامانم رو ببینم").recurrence == "weekly@09:00:6"
    assert _parse("هر یک‌شنبه ساعت ۹ صبح یادم بنداز مامانم رو ببینم").recurrence == "weekly@09:00:6"


def test_the_reader_never_invents_a_repeat_from_a_bare_word():
    assert find_recurrence_phrase("هر چیزی رو یادم بنداز") is None
    assert find_recurrence_phrase("یادم بنداز ساعت ۹ قرص بخورم") is None


def test_a_phrase_carries_where_it_was_so_the_rest_can_be_read_around_it():
    phrase = find_recurrence_phrase("هر شنبه ساعت ۱۰ بازار")
    assert isinstance(phrase, RecurrencePhrase)
    assert "هر شنبه ساعت ۱۰ بازار"[phrase.span[0]:phrase.span[1]] == "هر شنبه"


@pytest.mark.parametrize("text,kind,interval,weekdays,day", [
    ("هر روز", "daily", 1, (), None),
    ("هر ۳ روز", "daily", 3, (), None),
    ("هر هفته", "weekly", 1, (), None),
    ("هر روز کاری", "weekly", 1, WORKING_DAYS, None),
    ("هر ماه", "monthly", 1, (), None),
    ("هر ۱۵ ماه", "monthly", 15, (), None),
])
def test_what_the_words_say_before_a_clock_is_known(text, kind, interval, weekdays, day):
    phrase = find_recurrence_phrase(text)
    assert (phrase.kind, phrase.interval, phrase.weekdays, phrase.day) == (kind, interval, weekdays, day)


def test_an_implicit_weekday_means_today_and_is_then_kept():
    phrase = find_recurrence_phrase("هر هفته ساعت ۹ صبح")
    rule = finalize(phrase, zone=ZONE_TZ(), now_utc=NOW, hour=9, minute=0)
    assert rule.weekdays == (1,)  # today is Tuesday in Tehran
    assert str(rule) == "weekly@09:00:1"


@pytest.mark.parametrize("text,day", [
    ("پانزدهم", 15), ("۱۵", 15), ("اول", 1), ("سی‌ام", 30), ("بیست‌ویکم", 21), ("۳۲", None), ("سلام", None),
])
def test_a_day_of_the_month_is_read_only_when_it_is_one(text, day):
    assert parse_month_day(text) == day


# --- requests that are refused ----------------------------------------------

@pytest.mark.parametrize("text", [
    "هر سال ساعت ۹ صبح یادم بنداز سالگرد",
    "سالانه ساعت ۹ صبح یادم بنداز سالگرد",
    "every year at 9am remind me about the anniversary",
])
def test_a_yearly_repeat_is_refused_not_turned_into_one_reminder(text):
    assert is_unsupported_recurrence(text) is True
    with pytest.raises(ReminderError) as excinfo:
        _parse(text)
    assert excinfo.value.code == "recurring_yearly"
    assert "سالانه" in str(excinfo.value)


def test_two_weekdays_every_other_week_is_refused_with_the_reason():
    with pytest.raises(ReminderError) as excinfo:
        _parse("هر دو هفته شنبه و دوشنبه ساعت ۹ صبح یادم بنداز باشگاه")
    assert excinfo.value.code == "recurring_weekdays"
    assert "یک روز هفته" in str(excinfo.value)


def test_a_repeat_without_a_clock_asks_for_one():
    with pytest.raises(ReminderError) as excinfo:
        _parse("هر شنبه یادم بنداز بازار برم")
    assert excinfo.value.code == "missing_clock"
    assert "چه ساعتی" in str(excinfo.value)


def test_a_repeat_cannot_be_relative_or_dated():
    with pytest.raises(ReminderError) as relative:
        _parse("هر روز ۱۰ دقیقه دیگه یادم بنداز آب بخورم")
    assert relative.value.code == "recurring_time"
    with pytest.raises(ReminderError) as dated:
        _parse("هر روز ۲۰۲۶-۱۲-۰۱ یادم بنداز آب بخورم")
    assert dated.value.code == "recurring_date"


def test_a_repeat_without_a_task_asks_for_the_task():
    with pytest.raises(ReminderError) as excinfo:
        _parse("هر روز ساعت ۹ صبح یادم بنداز")
    assert excinfo.value.code == "missing_task"


# --- the store --------------------------------------------------------------

def _clocked_store(*, at=None):
    clock = [datetime.fromtimestamp((at or NOW).timestamp(), tz=timezone.utc).timestamp()]
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = ReminderStore(db, threading.Lock(), default_timezone=ZONE, clock=lambda: clock[0])
    return store, clock


def _rows(db):
    return [dict(row) for row in db.execute(
        "SELECT id, status, due_utc, anchor_utc, recurrence, series_id, occurrence FROM reminders ORDER BY id"
    )]


def _minutes(rows):
    return (datetime.fromtimestamp(rows[0]["due_utc"], tz=timezone.utc).astimezone(ZONE_TZ())
            .strftime("%Y-%m-%d %H:%M"))


def test_a_new_series_is_its_own_series_and_has_no_occurrence_history():
    store, _ = _clocked_store()
    spec = _parse("هر روز ساعت ۸ صبح یادم بنداز قرص بخورم")
    reminder_id = store.create(1, 1, spec)
    rows = _rows(store.db)
    assert rows[0]["series_id"] == reminder_id and rows[0]["occurrence"] == 0
    assert rows[0]["anchor_utc"] == rows[0]["due_utc"]
    assert rows[0]["recurrence"] == "daily@08:00"


def test_a_one_shot_reminder_has_no_rule_and_no_anchor():
    store, _ = _clocked_store()
    store.create(1, 1, ReminderSpec(NOW + timedelta(hours=1), "آب", ZONE))
    row = _rows(store.db)[0]
    assert row["recurrence"] == "" and row["series_id"] == 0 and row["anchor_utc"] == 0


def _deliver(store, *, slow_bot_none=False):
    telegram = SimpleNamespace(send_message=AsyncMock())
    scheduler = ReminderScheduler(store, telegram)
    asyncio.run(scheduler.run_once())
    return telegram


def test_delivery_writes_the_next_occurrence_in_the_same_transaction():
    store, clock = _clocked_store()
    spec = _parse("هر روز ساعت ۸ صبح یادم بنداز قرص بخورم")
    store.create(1, 1, spec)
    clock[0] = datetime(2026, 11, 4, 4, 30, tzinfo=timezone.utc).timestamp()
    _deliver(store)
    rows = _rows(store.db)
    assert [row["status"] for row in rows] == ["sent", "pending"]
    assert rows[1]["occurrence"] == 1 and rows[1]["series_id"] == rows[0]["id"]
    assert _minutes(rows[1:]) == "2026-11-05 08:00"


def test_the_series_does_not_drift_when_an_occurrence_is_snoozed():
    store, clock = _clocked_store()
    reminder_id = store.create(1, 1, _parse("هر روز ساعت ۸ صبح یادم بنداز قرص بخورم"))
    clock[0] = datetime(2026, 11, 4, 4, 30, tzinfo=timezone.utc).timestamp()
    _deliver(store)
    store.snooze(1, 1, reminder_id, seconds=600)
    clock[0] += 600
    _deliver(store)
    pending = store.list_active(1, 1)
    assert len(pending) == 1
    # Not 08:10 tomorrow: the rule's own instant is the anchor.
    assert _minutes(pending) == "2026-11-05 08:00"


def test_a_retried_delivery_cannot_write_the_same_occurrence_twice():
    store, clock = _clocked_store()
    store.create(1, 1, _parse("هر روز ساعت ۸ صبح یادم بنداز قرص بخورم"))
    clock[0] = datetime(2026, 11, 4, 4, 30, tzinfo=timezone.utc).timestamp()
    _deliver(store)
    before = len(_rows(store.db))
    sent = dict(store.db.execute("SELECT * FROM reminders WHERE status='sent'").fetchone())
    store.mark_sent(sent)
    assert len(_rows(store.db)) == before


def test_one_occurrence_exists_at_a_time_across_a_long_outage():
    store, clock = _clocked_store()
    store.create(1, 1, _parse("هر روز ساعت ۸ صبح یادم بنداز قرص بخورم"))
    clock[0] = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc).timestamp()  # six days down
    _deliver(store)
    _deliver(store)  # nothing else is due: the missed days are not a backlog
    assert [row["status"] for row in _rows(store.db)] == ["sent", "pending"]
    assert _minutes(store.list_active(1, 1)) == "2026-11-11 08:00"


def test_skipping_moves_the_next_occurrence_past_one_round():
    store, clock = _clocked_store()
    store.create(1, 1, _parse("هر روز ساعت ۸ صبح یادم بنداز قرص بخورم"))
    clock[0] = datetime(2026, 11, 4, 4, 30, tzinfo=timezone.utc).timestamp()
    _deliver(store)
    pending = store.list_active(1, 1)[0]
    due = store.skip_next(1, 1, pending["id"])
    assert _minutes([{"due_utc": due}]) == "2026-11-06 08:00"
    assert store.skip_next(1, 1, 999_999) is None


def test_stopping_a_series_keeps_its_history_but_nothing_pending():
    store, clock = _clocked_store()
    reminder_id = store.create(1, 1, _parse("هر روز ساعت ۸ صبح یادم بنداز قرص بخورم"))
    clock[0] = datetime(2026, 11, 4, 4, 30, tzinfo=timezone.utc).timestamp()
    _deliver(store)
    assert store.cancel_series(1, 1, reminder_id) is True
    assert store.list_active(1, 1) == []
    assert [row["status"] for row in _rows(store.db)] == ["sent", "cancelled"]
    assert store.cancel_series(1, 1, reminder_id) is False


def test_a_series_belongs_to_the_chat_and_person_that_made_it():
    store, _ = _clocked_store()
    reminder_id = store.create(1, 1, _parse("هر روز ساعت ۸ صبح یادم بنداز قرص بخورم"))
    assert store.cancel_series(2, 1, reminder_id) is False
    assert store.cancel_series(1, 2, reminder_id) is False
    assert len(store.list_active(1, 1)) == 1


def test_an_old_database_gains_the_new_columns_and_keeps_its_rows():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        text TEXT NOT NULL, due_utc INTEGER NOT NULL, timezone TEXT NOT NULL,
        created_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        next_attempt INTEGER NOT NULL, lease_until INTEGER NOT NULL DEFAULT 0, claim_token TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, delivered_at INTEGER,
        last_error TEXT NOT NULL DEFAULT '', recipient_name TEXT NOT NULL DEFAULT '')""")
    db.execute("INSERT INTO reminders (chat_id, user_id, text, due_utc, timezone, created_at,"
               " next_attempt, status) VALUES (1, 1, 'آب', 0, 'Asia/Tehran', 0, 0, 'sent')")
    migrations.migrate(db, migrations=(migrations.MIGRATIONS[1],))
    columns = {row["name"] for row in db.execute("PRAGMA table_info(reminders)")}
    assert {"recurrence", "series_id", "occurrence", "anchor_utc"} <= columns
    row = db.execute("SELECT * FROM reminders").fetchone()
    assert row["text"] == "آب" and row["recurrence"] == ""


# --- the bot ----------------------------------------------------------------

def _repeating(bot, *, text="آب بخورم", due_in=1):
    return bot.reminder_store.create(
        123, 123,
        ReminderSpec(datetime.now(timezone.utc) + timedelta(seconds=due_in), text, ZONE,
                     recurrence="daily@08:00"),
    )


def test_a_confirmation_says_that_it_repeats(isolated_bot):
    bot, client = isolated_bot
    client.models.responses = []
    reply = _send(bot, "هر روز ساعت ۸ صبح یادم بنداز قرص بخورم")
    text = reply.message.reply_text.await_args.args[0]
    assert "🔁" in text and "هر روز" in text and "۸:۰۰" in text
    row = bot.reminder_store.list_active(123, 123)[0]
    assert row["recurrence"] == "daily@08:00"


def test_the_list_shows_how_each_reminder_repeats(isolated_bot):
    bot, _ = isolated_bot
    _repeating(bot)
    update, context = _update_with_text("/reminders")
    context.args = []
    asyncio.run(bot.reminders_command(update, context))
    text = update.message.reply_text.await_args.args[0]
    assert "🔁 تکرار: هر روز، ساعت ۸:۰۰ صبح" in text


def test_the_delivered_message_offers_skipping_and_stopping(isolated_bot):
    bot, _ = isolated_bot
    reminder_id = _repeating(bot)
    row = bot.reminder_store.get_owned(123, 123, reminder_id)
    bot.reminder_store.clock = lambda: row["due_utc"] + 1
    telegram = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(ReminderScheduler(bot.reminder_store, telegram).run_once())
    markup = telegram.send_message.await_args.kwargs["reply_markup"]
    labels = [button.text for line in markup.inline_keyboard for button in line]
    assert any("رد کن" in label for label in labels)
    assert any("تکرار رو متوقف" in label for label in labels)
    assert [button.callback_data for button in markup.inline_keyboard[1]] == [
        f"rm:skip:{reminder_id}", f"rm:unrepeat:{reminder_id}",
    ]


def test_skip_button_moves_the_series_and_says_when_it_is_next(isolated_bot):
    bot, _ = isolated_bot
    reminder_id = _repeating(bot)
    row = bot.reminder_store.get_owned(123, 123, reminder_id)
    bot.reminder_store.clock = lambda: row["due_utc"] + 1
    asyncio.run(ReminderScheduler(bot.reminder_store, SimpleNamespace(send_message=AsyncMock())).run_once())

    query = _callback(bot, f"rm:skip:{reminder_id}")
    toast = query.answer.await_args.args[0]
    assert "تکرار بعدی" in toast
    pending = bot.reminder_store.list_active(123, 123)
    assert len(pending) == 1 and pending[0]["due_utc"] > row["due_utc"]


def test_stop_button_ends_the_series_and_leaves_history(isolated_bot):
    bot, _ = isolated_bot
    reminder_id = _repeating(bot)
    row = bot.reminder_store.get_owned(123, 123, reminder_id)
    bot.reminder_store.clock = lambda: row["due_utc"] + 1
    asyncio.run(ReminderScheduler(bot.reminder_store, SimpleNamespace(send_message=AsyncMock())).run_once())

    query = _callback(bot, f"rm:unrepeat:{reminder_id}")
    assert "متوقف" in query.answer.await_args.args[0]
    assert bot.reminder_store.list_active(123, 123) == []
    assert bot.reminder_store.get_owned(123, 123, reminder_id)["status"] == "sent"


def test_a_one_shot_reminder_has_no_repeat_buttons(isolated_bot):
    bot, _ = isolated_bot
    reminder_id = bot.reminder_store.create(123, 123, ReminderSpec(
        datetime.now(timezone.utc) + timedelta(seconds=1), "آب", ZONE))
    row = bot.reminder_store.get_owned(123, 123, reminder_id)
    markup = reminder_buttons(row, "fa")
    assert len(markup.inline_keyboard) == 1


def test_the_model_is_never_asked_to_create_a_repeat(isolated_bot):
    """The tool is one-shot by construction; a repeat is the parser's job."""
    bot, _ = isolated_bot
    result = bot._tool_schedule_reminder({
        "when": "هر روز", "task": "قرص بخورم",
        "_reminder": {"chat_id": 123, "user_id": 123, "explicit": True, "lang": "fa",
                      "sources": ["هر روز ساعت ۸ صبح یادم بنداز قرص بخورم"]},
    })
    assert result["ok"] is False and result["code"] == "recurring"
    assert "ساعتش" in result["safe_user_message"]
    assert bot.reminder_store.list_active(123, 123) == []


def test_a_clockless_repeat_is_completed_by_the_next_message(isolated_bot):
    """«هر شنبه …» keeps its weekday; the follow-up only supplies the hour."""
    bot, _ = isolated_bot
    first = _send(bot, "هر شنبه یادم بنداز بازار برم")
    assert "چه ساعتی" in first.message.reply_text.await_args.args[0]
    assert bot.reminder_store.list_active(123, 123) == []

    _send(bot, "۱۰ صبح")
    rows = bot.reminder_store.list_active(123, 123)
    assert len(rows) == 1
    assert rows[0]["recurrence"] == "weekly@10:00:5"
    assert rows[0]["text"] == "بازار برم"


def test_an_english_reader_is_told_the_repeat_in_english(isolated_bot):
    bot, _ = isolated_bot
    bot.LANGUAGE_STORE.set(123, "en")
    _repeating(bot)
    update, context = _update_with_text("/reminders")
    context.args = []
    asyncio.run(bot.reminders_command(update, context))
    assert "🔁 Repeats: every day at 08:00" in update.message.reply_text.await_args.args[0]


def test_a_stale_skip_button_answers_instead_of_inventing_a_date(isolated_bot):
    bot, _ = isolated_bot
    reminder_id = bot.reminder_store.create(123, 123, ReminderSpec(
        datetime.now(timezone.utc) + timedelta(hours=1), "آب", ZONE))
    query = _callback(bot, f"rm:skip:{reminder_id}")
    assert "یک‌باره" in query.answer.await_args.args[0]
    assert bot.reminder_store.list_active(123, 123)[0]["due_utc"] > 0


def test_a_refused_repeat_explains_itself_in_the_readers_language(isolated_bot):
    bot, _ = isolated_bot
    bot.LANGUAGE_STORE.set(123, "en")
    reply = _send(bot, "every year at 9am remind me about the anniversary")
    text = reply.message.reply_text.await_args.args[0]
    assert "yearly" in text and "روزانه" not in text
    assert bot.reminder_store.list_active(123, 123) == []


def test_an_english_confirmation_says_it_repeats(isolated_bot):
    bot, _ = isolated_bot
    bot.LANGUAGE_STORE.set(123, "en")
    reply = _send(bot, "every day at 8am remind me to take the pills")
    text = reply.message.reply_text.await_args.args[0]
    assert "🔁 Repeats: every day at 08:00" in text
    assert bot.reminder_store.list_active(123, 123)[0]["text"] == "take the pills"


def test_cancel_command_ends_a_series_from_any_of_its_numbers(isolated_bot):
    bot, _ = isolated_bot
    reminder_id = _repeating(bot)
    row = bot.reminder_store.get_owned(123, 123, reminder_id)
    bot.reminder_store.clock = lambda: row["due_utc"] + 1
    asyncio.run(ReminderScheduler(bot.reminder_store, SimpleNamespace(send_message=AsyncMock())).run_once())
    assert len(bot.reminder_store.list_active(123, 123)) == 1

    update, context = _update_with_text(f"/cancelreminder {reminder_id}")
    context.args = [str(reminder_id)]
    asyncio.run(bot.cancel_reminder_command(update, context))
    assert "لغو شد" in update.message.reply_text.await_args.args[0]
    assert bot.reminder_store.list_active(123, 123) == []


def test_the_cancel_button_on_a_fired_repeat_still_stops_the_series(isolated_bot):
    bot, _ = isolated_bot
    reminder_id = _repeating(bot)
    row = bot.reminder_store.get_owned(123, 123, reminder_id)
    bot.reminder_store.clock = lambda: row["due_utc"] + 1
    asyncio.run(ReminderScheduler(bot.reminder_store, SimpleNamespace(send_message=AsyncMock())).run_once())

    # The keyboard on the confirmation, pressed after the first delivery.
    query = _callback(bot, f"rm:cancel:{reminder_id}")
    assert "لغو" in query.answer.await_args.args[0]
    assert bot.reminder_store.list_active(123, 123) == []
