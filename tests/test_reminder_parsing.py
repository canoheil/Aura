"""The reminder grammar: what Persian and English time phrases actually mean.

These tests are a table on purpose. Every case names a message, a "now" and the
instant that should come out, so a regression shows up as a wrong clock rather
than as a vague failure. No bot, no database, no network.
"""
from datetime import datetime, timedelta, timezone

import pytest

from reminder_time import (
    ReminderError,
    _gregorian_to_jalali,
    _jalali_to_gregorian,
    format_due_human,
    parse_reminder,
    reminder_time_reply,
    time_candidates,
)

ZONE = "Asia/Tehran"
# Tuesday 3 November 2026, 12:00 in Tehran. Iran has not observed DST since
# 2022, so the offset is a constant +03:30 and every expectation is exact.
NOW = datetime(2026, 11, 3, 8, 30, tzinfo=timezone.utc)


def teheran(*parts) -> datetime:
    return datetime(*parts, tzinfo=timezone(timedelta(hours=3, minutes=30)))


@pytest.mark.parametrize("text,due,content", [
    # A clock word changes the hour, not just the label.
    ("یادم بنداز فردا ساعت ۹ صبح قرص بخورم", teheran(2026, 11, 4, 9, 0), "قرص بخورم"),
    ("یادم بنداز ساعت ۹ شب به بابا زنگ بزنم", teheran(2026, 11, 3, 21, 0), "به بابا زنگ بزنم"),
    ("یادم بنداز ساعت ۵ عصر جلسه دارم", teheran(2026, 11, 3, 17, 0), "جلسه دارم"),
    ("یادم بنداز ساعت ۱ ظهر ناهار", teheran(2026, 11, 3, 13, 0), "ناهار"),
    ("یادم بنداز ساعت ۷ بامداد بیدار شوم", teheran(2026, 11, 4, 7, 0), "بیدار شوم"),
    # «۱۲ شب» is midnight, so the day rolls over.
    ("یادم بنداز ساعت ۱۲ شب بخوابم", teheran(2026, 11, 4, 0, 0), "بخوابم"),
    # The day word may come after the clock.
    ("یادم بنداز ساعت ۹ صبح فردا آب بخورم", teheran(2026, 11, 4, 9, 0), "آب بخورم"),
    # A bare clock means the next time it is true; 9am has already passed.
    ("یادم بنداز ساعت ۹ صبح آب بخورم", teheran(2026, 11, 4, 9, 0), "آب بخورم"),
    ("یادم بنداز ساعت ۱۵:۳۰ قرار دارم", teheran(2026, 11, 3, 15, 30), "قرار دارم"),
    ("یادم بنداز ساعت ۱۴ قرص بخورم", teheran(2026, 11, 3, 14, 0), "قرص بخورم"),
    # Weekdays mean the next one, and today's passed time means next week.
    ("یادم بنداز دوشنبه ساعت ۵ عصر جلسه دارم", teheran(2026, 11, 9, 17, 0), "جلسه دارم"),
    ("یادم بنداز سه‌شنبه ۱۷:۰۰ کلاس دارم", teheran(2026, 11, 3, 17, 0), "کلاس دارم"),
    ("یادم بنداز سه‌شنبه ۹:۰۰ کلاس دارم", teheran(2026, 11, 10, 9, 0), "کلاس دارم"),
    # Solar Hijri dates, with and without a year.
    ("یادم بنداز ۲۸ آبان ساعت ۹ صبح تولد سارا", teheran(2026, 11, 19, 9, 0), "تولد سارا"),
    ("یادم بنداز ۲۸ آبان ۱۴۰۶ ساعت ۹ صبح تولد سارا", teheran(2027, 11, 19, 9, 0), "تولد سارا"),
    # Durations, including the compound ones Persian writes with «و».
    ("یک ساعت و نیم دیگه یادم بنداز ورزش کنم", teheran(2026, 11, 3, 13, 30), "ورزش کنم"),
    ("دو ساعت و سی دقیقه دیگه یادم بنداز ورزش کنم", teheran(2026, 11, 3, 14, 30), "ورزش کنم"),
    ("ساعت و نیم دیگه یادم بنداز چای دم کنم", teheran(2026, 11, 3, 13, 30), "چای دم کنم"),
    # English, both clocks.
    ("remind me tomorrow at 9am to call mom", teheran(2026, 11, 4, 9, 0), "call mom"),
    ("remind me at 9 pm to call Sam", teheran(2026, 11, 3, 21, 0), "call Sam"),
])
def test_times_persian_speakers_actually_write(text, due, content):
    spec = parse_reminder(text, ZONE, now=NOW, natural=True)
    assert spec.due_utc == due.astimezone(timezone.utc)
    assert spec.text == content


@pytest.mark.parametrize("text", [
    "فردا صبح یادم بنداز زنگ بزنم به مامان",
    "دوشنبه یادم بنداز جلسه دارم",
    "۲۸ آبان یادم بنداز تولد سارا",
])
def test_a_day_without_a_clock_asks_for_the_clock(text):
    """The user does not have to start over; they are asked for one thing."""
    with pytest.raises(ReminderError) as caught:
        parse_reminder(text, ZONE, now=NOW, natural=True)
    assert caught.value.code == "missing_clock"


@pytest.mark.parametrize("text", [
    "یادم بنداز فردا ساعت ۹:۰۰ و ۱۴:۰۰ قرار دارم",
    "یادم بنداز ساعت ۹ صبح و ساعت ۹ شب قرص بخورم",
])
def test_two_competing_times_are_never_resolved_by_guessing(text):
    with pytest.raises(ReminderError) as caught:
        parse_reminder(text, ZONE, now=NOW, natural=True)
    assert caught.value.code == "ambiguous_time"


def test_an_impossible_hour_is_rejected_not_wrapped():
    with pytest.raises(ReminderError):
        parse_reminder("یادم بنداز ساعت ۲۵:۰۰ قرص بخورم", ZONE, now=NOW, natural=True)


def test_ambiguity_helpers_agree_with_the_parser():
    """Grounding checks must count exactly what the parser counts."""
    assert time_candidates("فردا ساعت ۹ صبح") != []
    assert len(time_candidates("فردا ساعت ۹ صبح")) == 1
    assert len(time_candidates("فردا ۹ صبح و دوشنبه ۱۰:۰۰")) == 2


# --- calendar ---------------------------------------------------------------

@pytest.mark.parametrize("gregorian,jalali", [
    ((2024, 3, 20), (1403, 1, 1)),
    ((2025, 3, 21), (1404, 1, 1)),
    ((2026, 3, 21), (1405, 1, 1)),
    ((2026, 11, 3), (1405, 8, 12)),
    ((2025, 12, 31), (1404, 10, 10)),
])
def test_solar_hijri_anchors(gregorian, jalali):
    assert _gregorian_to_jalali(*gregorian) == jalali
    assert _jalali_to_gregorian(*jalali) == gregorian


def test_every_day_of_four_years_round_trips():
    day = datetime(2024, 1, 1)
    for _ in range(4 * 366):
        assert _jalali_to_gregorian(*_gregorian_to_jalali(day.year, day.month, day.day)) == (
            day.year, day.month, day.day
        )
        day += timedelta(days=1)


def test_a_date_that_already_passed_this_year_is_refused_not_moved():
    with pytest.raises(ReminderError) as caught:
        parse_reminder("یادم بنداز ۱ فروردین ساعت ۹ صبح نوروز", ZONE, now=NOW, natural=True)
    assert caught.value.code == "time_too_soon"


# --- presentation -----------------------------------------------------------

def test_the_confirmation_date_is_persian_first():
    due = teheran(2026, 11, 4, 9, 0)
    assert format_due_human(due.timestamp(), ZONE, "fa") == "چهارشنبه ۱۳ آبان ۱۴۰۵، ساعت ۹:۰۰ صبح"


def test_the_confirmation_date_in_english():
    due = teheran(2026, 11, 4, 9, 0)
    assert format_due_human(due.timestamp(), ZONE, "en") == "Wednesday 4 November 2026 at 09:00"


def test_evening_hours_read_naturally():
    assert "۹:۰۰ شب" in format_due_human(teheran(2026, 11, 4, 21, 0).timestamp(), ZONE, "fa")
    assert "۱۲:۰۰ ظهر" in format_due_human(teheran(2026, 11, 4, 12, 0).timestamp(), ZONE, "fa")
    assert "۵:۳۰ عصر" in format_due_human(teheran(2026, 11, 4, 17, 30).timestamp(), ZONE, "fa")


# --- follow-up replies ------------------------------------------------------

@pytest.mark.parametrize("reply,accepted", [
    ("۹ صبح", True),
    ("ساعت ۹ صبح", True),
    ("۱۷:۳۰", True),
    ("فردا ۹ صبح", True),
    ("۵ دقیقه دیگه", True),
    ("", False),
    ("باشه", False),
    ("ساعت ۹", False),       # bare hour needs the "ساعت" form to opt in
])
def test_a_followup_reply_must_be_only_a_time(reply, accepted):
    assert (reminder_time_reply(reply) is not None) is accepted


def test_a_bare_hour_is_accepted_only_when_the_question_was_about_the_clock():
    assert reminder_time_reply("ساعت ۹", allow_bare_hour=True) == "ساعت ۹"
    assert reminder_time_reply("ساعت ۹") is None
    # With a day attached it stops being a guess.
    assert reminder_time_reply("فردا ساعت ۹") == "فردا ساعت ۹"
