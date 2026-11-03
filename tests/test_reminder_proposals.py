"""A model may propose a time; the message has to corroborate it.

The grammar in `reminder_time` covers what Persian speakers say most often. The
long tail — «سه هفته دیگه», «آخر ماه» — is left to a model, which is only safe
because nothing it returns is trusted on its own: the phrase must be a verbatim
fragment of the message, and every clock, day, date and quantity inside that
phrase must agree with the instant it claims.

The interesting cases here are not the ones that work. They are the ones where a
plausible-looking instant contradicts the words, and the answer must be a
question rather than a wrong reminder.
"""
from datetime import datetime, timezone

import pytest

from reminder_time import ReminderError, validate_time_proposal

ZONE = "Asia/Tehran"
# Tuesday 3 November 2026, 12:00 in Tehran.
NOW = datetime(2026, 11, 3, 8, 30, tzinfo=timezone.utc)


def accepted(phrase: str, local: str) -> datetime:
    return validate_time_proposal(time_phrase=phrase, local_datetime=local, zone_name=ZONE, now=NOW)


def rejected(phrase: str, local: str, code: str = "ungrounded_time") -> None:
    with pytest.raises(ReminderError) as caught:
        accepted(phrase, local)
    assert caught.value.code == code


# --- phrases the grammar cannot read, that a model can ----------------------

def test_three_weeks_from_now_is_checkable():
    due = accepted("سه هفته دیگه", "2026-11-24T12:00")
    assert due == datetime(2026, 11, 24, 8, 30, tzinfo=timezone.utc)


def test_a_quantity_that_does_not_match_the_instant_is_refused():
    rejected("سه هفته دیگه", "2026-11-10T12:00")
    rejected("یک ماه دیگه", "2026-11-10T12:00")
    rejected("in three weeks", "2026-11-17T12:00")


def test_english_quantities_work_too():
    assert accepted("in three weeks", "2026-11-24T12:00") == datetime(2026, 11, 24, 8, 30, tzinfo=timezone.utc)


# --- phrases with nothing to check against ---------------------------------

@pytest.mark.parametrize("phrase", ["هفته دیگه", "هفتهٔ آینده", "آخر هفته", "آخر ماه", "بعداً"])
def test_a_vague_phrase_is_never_accepted_however_plausible(phrase):
    """There is no anchor, so there is no way to be wrong — or right."""
    rejected(phrase, "2026-11-10T12:00")


# --- clocks ----------------------------------------------------------------

def test_a_spoken_clock_must_agree_with_the_hour():
    accepted("۹ صبح", "2026-11-04T09:00")
    rejected("۹ صبح", "2026-11-04T21:00")


def test_an_evening_word_flips_the_hour():
    accepted("۹ شب", "2026-11-03T21:00")
    rejected("۹ شب", "2026-11-03T09:00")


def test_written_minutes_must_agree():
    accepted("۱۵:۳۰", "2026-11-03T15:30")
    rejected("۱۵:۳۰", "2026-11-03T15:00")


def test_spoken_minutes_must_agree():
    accepted("ساعت ۹ و نیم شب", "2026-11-03T21:30")
    rejected("ساعت ۹ و نیم شب", "2026-11-03T21:00")


# --- days and dates --------------------------------------------------------

def test_a_day_word_must_match_the_date():
    accepted("فردا", "2026-11-04T12:00")
    rejected("فردا", "2026-11-06T12:00")


def test_a_weekday_must_match_the_date():
    accepted("دوشنبه", "2026-11-09T12:00")
    rejected("دوشنبه", "2026-11-10T12:00")


def test_a_solar_hijri_date_must_match_the_date():
    accepted("۲۸ آبان", "2026-11-19T12:00")
    rejected("۲۸ آبان", "2026-11-20T12:00")


def test_a_gregorian_date_must_match_the_date():
    accepted("2026-12-25", "2026-12-25T12:00")
    rejected("2026-12-25", "2026-12-26T12:00")


# --- ranges ----------------------------------------------------------------

def test_a_past_instant_is_a_refusal_not_a_wrap():
    # Yesterday's date contradicts «فردا», so it never reaches the range check.
    rejected("فردا", "2026-11-02T09:00")
    # A time that agrees with every word but has already gone is the range
    # check's job, and it refuses rather than rolling to tomorrow.
    rejected("امروز ۹ صبح", "2026-11-03T09:00", code="time_too_soon")


def test_absurdly_far_is_refused():
    rejected("۲۰۵۰-01-01", "2050-01-01T09:00", code="time_too_far")


def test_a_malformed_proposal_is_not_a_crash():
    rejected("سه هفته دیگه", "next tuesday")
    rejected("سه هفته دیگه", "")


def test_the_proposal_is_read_as_a_wall_clock_not_an_offset():
    """The zone comes from the user's profile, never from the model."""
    due = accepted("۹ صبح", "2026-11-04T09:00+04:00")
    assert due == datetime(2026, 11, 4, 5, 30, tzinfo=timezone.utc)
