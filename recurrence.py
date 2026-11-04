"""Repeating reminders: the rule, the next time, and how it is said.

A recurrence here is deliberately small. People ask for «هر روز», «هر شنبه»,
«هر دو هفته», «هر ماه پانزدهم» — not for RFC 5545. The rule therefore knows
three shapes (day, week, month), an interval, and a wall-clock time. Anything
outside that shape is refused and asked about, which is the same contract the
one-shot grammar follows.

Two decisions worth stating up front:

* **The rule carries the wall-clock time, not the UTC instant.** A daily
  reminder at 08:00 stays at 08:00 across a daylight-saving change, and a
  snooze — which moves one occurrence by ten minutes — cannot drift the series.
* **One occurrence exists at a time.** Each is a row; the next is written in the
  same transaction that marks the previous one delivered. That leaves the claim
  and delivery path in `reminders.py` untouched, makes `/reminders` show the
  next time, and means a crash cannot leave a hundred future rows behind.
"""
from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterator, Optional
from zoneinfo import ZoneInfo

from reminder_time import (
    _WEEKDAY_INDEX,
    _WEEKDAY_NAMES,
    _WORD_VALUES,
    ReminderError,
    _normalized,
    fa_digits,
    local_to_utc,
)

WEEKDAY_NAMES_FA = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یک‌شنبه")
WEEKDAY_NAMES_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WORKING_DAYS = (0, 1, 2, 3, 4)

_MAX_CANDIDATES = 800


@dataclass(frozen=True)
class Recurrence:
    kind: str                     # "daily" | "weekly" | "monthly"
    hour: int
    minute: int
    interval: int = 1
    weekdays: tuple[int, ...] = ()
    day: int = 1

    # --- the stored form ---------------------------------------------------
    def __str__(self) -> str:
        extra = ""
        if self.kind == "weekly":
            extra = ":" + ",".join(str(day) for day in self.weekdays)
        elif self.kind == "monthly":
            extra = f":{self.day}"
        interval = f"/{self.interval}" if self.interval > 1 else ""
        return f"{self.kind}{interval}@{self.hour:02d}:{self.minute:02d}{extra}"

    @classmethod
    def parse(cls, rule: str) -> Optional["Recurrence"]:
        """The inverse of ``str``; an unreadable rule is a corrupt row, not a crash."""
        match = re.fullmatch(
            r"(?P<kind>daily|weekly|monthly)(?:/(?P<interval>\d{1,3}))?"
            r"@(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<extra>[0-9,]+))?", str(rule or "").strip(),
        )
        if not match:
            return None
        try:
            hour, minute = int(match.group("hour")), int(match.group("minute"))
            interval = int(match.group("interval") or 1)
        except ValueError:
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 1 <= interval <= 365):
            return None
        kind, extra = match.group("kind"), match.group("extra") or ""
        weekdays: tuple[int, ...] = ()
        day = 1
        if kind == "weekly":
            try:
                weekdays = tuple(sorted({int(part) for part in extra.split(",") if part.strip()}))
            except ValueError:
                return None
            if not weekdays or any(not 0 <= value <= 6 for value in weekdays):
                return None
            if interval > 1 and len(weekdays) != 1:
                return None
        if kind == "monthly":
            try:
                day = int(extra or 1)
            except ValueError:
                return None
            if not 1 <= day <= 31:
                return None
        return cls(kind=kind, hour=hour, minute=minute, interval=interval, weekdays=weekdays, day=day)

    # --- the next time -----------------------------------------------------
    def _local_candidates(self, start: date) -> Iterator[datetime]:
        """Ascending wall-clock candidates, anchored on the week/month of ``start``."""
        time = (self.hour, self.minute)
        for step in range(_MAX_CANDIDATES):
            if self.kind == "daily":
                yield datetime.combine(start + timedelta(days=step * self.interval), datetime.min.time()).replace(
                    hour=time[0], minute=time[1],
                )
            elif self.kind == "weekly":
                week_start = start + timedelta(days=7 * self.interval * step)
                for weekday in self.weekdays:
                    day = week_start + timedelta(days=(weekday - week_start.weekday()) % 7)
                    yield datetime.combine(day, datetime.min.time()).replace(hour=time[0], minute=time[1])
            else:
                year, month = start.year, start.month + self.interval * step
                while month > 12:
                    year, month = year + 1, month - 12
                last = monthrange(year, month)[1]
                yield datetime(year, month, min(self.day, last), time[0], time[1])

    def next_after(self, previous_utc: datetime, now_utc: datetime, zone: ZoneInfo) -> datetime:
        """The first occurrence strictly after ``now``, anchored on ``previous``.

        Anchoring on the previous occurrence — not on "now" — is what keeps
        «هر دو هفته» two weeks apart. Then it walks past anything missed while
        the bot was down, so a long outage costs one late reminder rather than
        one per missed day.
        """
        base = previous_utc.astimezone(zone)
        now_local = now_utc.astimezone(zone).replace(tzinfo=None)
        floor = max(base.replace(tzinfo=None), now_local)
        return self._first_strictly_after(floor, now_utc, zone, anchor=self._anchor_date(base.date()))

    def first_after(self, now_utc: datetime, zone: ZoneInfo) -> datetime:
        """The first occurrence strictly after ``now``, for a brand-new series."""
        now_local = now_utc.astimezone(zone).replace(tzinfo=None)
        return self._first_strictly_after(now_local, now_utc, zone, anchor=self._anchor_date(now_local.date()))

    def _anchor_date(self, reference: date) -> date:
        """Where the candidate walk starts, so the first step is not always missed."""
        if self.kind == "weekly":
            # The week containing today; its weekdays are emitted in the same pass.
            return reference - timedelta(days=reference.weekday())
        return reference

    def _first_strictly_after(self, floor: datetime, now_utc: datetime, zone: ZoneInfo,
                              *, anchor: date) -> datetime:
        for candidate in self._local_candidates(anchor):
            if candidate <= floor or candidate.minute != self.minute or candidate.hour != self.hour:
                continue
            if self.kind == "weekly" and candidate.weekday() not in self.weekdays:
                continue
            due = _safe_local_to_utc(candidate, zone)
            if due > now_utc:
                return due
        raise ReminderError("زمان بعدی این یادآور تکراری پیدا نشد.", code="invalid")

    # --- how it is said ----------------------------------------------------
    def describe(self, lang: Optional[str] = None) -> str:
        english = is_english(lang)
        clock = _clock_words(self.hour, self.minute, english)
        head = self._describe_head(english)
        return f"{head} at {clock}" if english else f"{head}، ساعت {clock}"

    def _describe_head(self, english: bool) -> str:
        step = self.interval
        if self.kind == "daily":
            if step == 1:
                return "every day" if english else "هر روز"
            if english:
                return f"every {step} days"
            return "هر دو روز" if step == 2 else f"هر {fa_digits(str(step))} روز"
        if self.kind == "weekly":
            if self.weekdays == WORKING_DAYS:
                return "weekdays" if english else "روزهای کاری"
            names = _join_names([WEEKDAY_NAMES_EN[d] if english else WEEKDAY_NAMES_FA[d]
                                 for d in self.weekdays], english)
            if step == 1:
                return f"every {names}" if english else f"هر {names}"
            if english:
                weeks = "two weeks" if step == 2 else f"{step} weeks"
                return f"every {weeks} on {names}"
            weeks = "دو هفته" if step == 2 else f"{fa_digits(str(step))} هفته"
            return f"هر {weeks}، {names}"
        ordinal = _ordinal(self.day, english)
        if step == 1:
            return f"every month on the {ordinal}" if english else f"هر ماه {ordinal}"
        if english:
            months = "three months" if step == 3 else f"{step} months"
            return f"every {months} on the {ordinal}"
        months = "سه ماه" if step == 3 else f"{fa_digits(str(step))} ماه"
        return f"هر {months}، {ordinal}"


def _join_names(names: list[str], english: bool) -> str:
    """«دوشنبه، چهارشنبه و جمعه» — a list that reads like a sentence, not a CSV."""
    if len(names) == 1:
        return names[0]
    conjunction = "and" if english else "و"
    separator = ", " if english else "، "
    return separator.join(names[:-1]) + f" {conjunction} " + names[-1]


def is_english(lang: Optional[str]) -> bool:
    return str(lang or "fa").lower().startswith("en")


def _ordinal(value: int, english: bool) -> str:
    if english:
        suffix = "th" if 10 <= value % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
        return f"{value}{suffix}"
    names = {1: "اول", 2: "دوم", 3: "سوم", 4: "چهارم", 5: "پنجم", 6: "ششم", 7: "هفتم",
             8: "هشتم", 9: "نهم", 10: "دهم", 11: "یازدهم", 12: "دوازدهم", 13: "سیزدهم",
             14: "چهاردهم", 15: "پانزدهم", 16: "شانزدهم", 17: "هفدهم", 18: "هجدهم",
             19: "نوزدهم", 20: "بیستم", 21: "بیست‌ویکم", 22: "بیست‌ودوم", 23: "بیست‌وسوم",
             24: "بیست‌وچهارم", 25: "بیست‌وپنجم", 26: "بیست‌وششم", 27: "بیست‌وهفتم",
             28: "بیست‌وهشتم", 29: "بیست‌ونهم", 30: "سی‌ام", 31: "سی‌ویکم"}
    return names.get(value, fa_digits(str(value)))


def _clock_words(hour: int, minute: int, english: bool) -> str:
    if english:
        return f"{hour:02d}:{minute:02d}"
    if hour < 5:
        part = "بامداد"
    elif hour < 12:
        part = "صبح"
    elif hour == 12:
        part = "ظهر"
    elif hour < 17:
        part = "بعدازظهر"
    elif hour < 20:
        part = "عصر"
    else:
        part = "شب"
    return f"{fa_digits(f'{hour % 12 or 12}:{minute:02d}')} {part}"


def _safe_local_to_utc(local: datetime, zone: ZoneInfo):
    """A wall clock that DST skipped moves forward rather than killing the series."""
    for _ in range(180):
        try:
            return local_to_utc(local, zone)
        except ReminderError as exc:
            if exc.code != "nonexistent_time":
                raise
            local += timedelta(minutes=1)
    return local_to_utc(local, zone)


# --- reading the phrase -----------------------------------------------------

_WEEKDAY_ALT = _WEEKDAY_NAMES
_UNIT = rf"(?:روز|هفته|ماه|{_WEEKDAY_ALT}|weekdays?|days?|weeks?|months?)"
_NUMBER = rf"(?:\d{{1,3}}|{'|'.join(sorted(_WORD_VALUES, key=len, reverse=True))})"

# «هر دوشنبه» and «هر دو هفته» start with the same word, and «دو» is both a
# number and the first half of «دوشنبه». The weekday branch therefore comes
# first, so the alternation cannot read «دوشنبه» as two of «شنبه».
_RECURRING = re.compile(
    rf"(?<![\w])(?P<prefix>هر)\s*"
    rf"(?:(?P<weekday>{_WEEKDAY_ALT})"
    rf"(?P<wd_tail>(?:\s*(?:و|,|،)\s*(?:{_WEEKDAY_ALT}))*)"
    rf"|(?:(?P<count>{_NUMBER})\s*)?(?P<unit>{_UNIT})"
    rf"(?P<tail>(?:\s*(?:و|,|،)?\s*(?:{_WEEKDAY_ALT}))*))"
    rf"|(?<![\w])(?P<every>every)\s+(?:(?P<en_count>\d{{1,3}}|two|three|four|six|other)\s+)?"
    rf"(?P<en_unit>days?|weeks?|months?)"
    rf"|(?<![\w])(?P<bare>روزانه|هفتگی|ماهانه|daily|weekly|monthly)(?![\w])",
    re.IGNORECASE,
)
# «هر روز کاری» means Monday to Friday, and the general pattern would read it
# as "every day". It has to win first.
_WORKING = re.compile(r"هر\s*روز(?:ها|های)?\s*کاری", re.IGNORECASE)
_WEEKDAY_TAIL = re.compile(_WEEKDAY_ALT, re.IGNORECASE)
_EN_COUNT = {"two": 2, "three": 3, "four": 4, "six": 6, "other": 2}


def _weekday_index(word: str) -> Optional[int]:
    token = re.sub(r"\s+", " ", _normalized(word).strip().lower())
    token = token.replace("یک شنبه", "یک‌شنبه").replace("سه شنبه", "سه‌شنبه")
    token = token.replace("چهار شنبه", "چهارشنبه").replace("پنج شنبه", "پنج‌شنبه").replace("دو شنبه", "دوشنبه")
    if token == "یکشنبه":
        return _WEEKDAY_INDEX["یک‌شنبه"]
    return _WEEKDAY_INDEX.get(token)


@dataclass(frozen=True)
class RecurrencePhrase:
    """What the words said. Defaults are filled in later, against a clock."""

    kind: str
    interval: int
    weekdays: tuple[int, ...] = ()
    day: Optional[int] = None
    span: tuple[int, int] = (0, 0)


#: Things people do ask for that this build will not schedule. Refusing them is
#: the honest answer; silently treating «هر سال» as a single reminder is not.
_UNSUPPORTED = re.compile(
    r"(?<![\w])(?:هر\s*(?:\d{1,3}\s*|یک\s*|دو\s*|سه\s*|چهار\s*|پنج\s*)?سال(?:ی|انه)?"
    r"|سالانه|سال\s*به\s*سال"
    r"|\bevery\s+(?:\d{1,3}\s+)?years?\b|\byearly\b|\bannually\b|\bannual(?:ly)?\s+reminder\b)",
    re.IGNORECASE,
)


def is_unsupported_recurrence(text: str) -> bool:
    """A repeat asked for in words this build cannot honour — «هر سال»."""
    return bool(_UNSUPPORTED.search(_normalized(text)))


def find_recurrence_phrase(text: str) -> Optional[RecurrencePhrase]:
    """The recurring part of a message, or None. Never guesses at a bare «هر»."""
    value = _normalized(text)
    working = _WORKING.search(value)
    if working:
        # A day phrase glued to it («هر روز کاری ساعت ۹») is part of the span.
        return RecurrencePhrase("weekly", 1, WORKING_DAYS, None, working.span())
    for match in _RECURRING.finditer(value):
        kind, interval, weekdays = _interpret(match)
        if kind is None:
            continue
        day = None
        if kind == "monthly":
            # Only the words immediately after «هر ماه» can name its day; a
            # number in the task («هر ماه ۳ تا قرص») is not a date of the month,
            # and «هر ماه ساعت ۹» must keep its 9 as the clock.
            tail = value[match.end():match.end() + 30]
            leading = _MONTH_DAY_LEADING.match(tail)
            if leading:
                day = _day_value(leading.group("day"))
            if day:
                return RecurrencePhrase(kind, max(1, interval), weekdays, day,
                                        (match.start(), match.end() + leading.end()))
        return RecurrencePhrase(kind, max(1, interval), weekdays, day, match.span())
    return None


def _interpret(match: re.Match) -> tuple[Optional[str], int, tuple[int, ...]]:
    if match.group("bare"):
        word = match.group("bare").lower()
        if word in {"روزانه", "daily"}:
            return "daily", 1, ()
        if word in {"هفتگی", "weekly"}:
            return "weekly", 1, ()
        return "monthly", 1, ()

    if match.group("every"):
        unit = match.group("en_unit").lower()
        count = match.group("en_count") or ""
        interval = int(count) if count.isdigit() else _EN_COUNT.get(count.lower(), 1)
        kind = "daily" if unit.startswith("day") else "weekly" if unit.startswith("week") else "monthly"
        return kind, interval, ()

    # «هر شنبه» / «هر شنبه و دوشنبه»: the weekday list is the whole phrase.
    if match.group("weekday"):
        found = []
        for name in [match.group("weekday")] + _WEEKDAY_TAIL.findall(match.group("wd_tail") or ""):
            index = _weekday_index(name)
            if index is not None and index not in found:
                found.append(index)
        return "weekly", 1, tuple(sorted(found))

    unit = (match.group("unit") or "").lower()
    if unit in {"weekdays", "weekday"}:
        return "weekly", 1, WORKING_DAYS

    count = match.group("count") or ""
    try:
        interval = int(count) if count.isdigit() else _WORD_VALUES.get(count, 1)
    except ValueError:
        interval = 1
    if unit.startswith("روز") or unit.startswith("day"):
        kind = "daily"
    elif unit.startswith("هفته") or unit.startswith("week"):
        kind = "weekly"
    elif unit.startswith("ماه") or unit.startswith("month"):
        kind = "monthly"
    else:  # «هر سال» is out of scope; silently meaning monthly would be a lie
        return None, 1, ()
    weekdays: tuple[int, ...] = ()
    tail = match.group("tail") or ""
    if tail and kind == "weekly":
        found = []
        for name in _WEEKDAY_TAIL.findall(tail):
            index = _weekday_index(name)
            if index is not None and index not in found:
                found.append(index)
        weekdays = tuple(sorted(found))
    return kind, interval, weekdays


#: A day of the month is a numeral or a number-word; an ordinal ending («ام»/«م»)
#: is required for the word form, so «سی دقیقه» cannot become the 30th, and a
#: compound («بیست‌ویکم» = «بیست و یکم») is read as one number.
_DAY_WORDS = "|".join(sorted(_WORD_VALUES, key=len, reverse=True))
#: Ordinal spellings whose stem is not a plain number word («سوم», «دهم»).
_ORDINALS = {
    "اول": 1, "دوم": 2, "سوم": 3, "چهارم": 4, "پنجم": 5, "ششم": 6, "هفتم": 7,
    "هشتم": 8, "نهم": 9, "دهم": 10, "یازدهم": 11, "دوازدهم": 12, "سیزدهم": 13,
    "چهاردهم": 14, "پانزدهم": 15, "شانزدهم": 16, "هفدهم": 17, "هجدهم": 18,
    "نوزدهم": 19, "بیستم": 20, "سی ام": 30,
}
_ORDINAL_ALT = "|".join(sorted(_ORDINALS, key=len, reverse=True))
_DAY_TOKEN = (rf"(?:\d{{1,2}}|{_ORDINAL_ALT}|(?:{_DAY_WORDS})"
              rf"(?:\s*و?\s*(?:{_DAY_WORDS}))?(?:\s*(?:ام|م)))")
_MONTH_DAY_RE = re.compile(rf"(?<![\w])(?P<day>{_DAY_TOKEN})(?![\w])", re.IGNORECASE)
# «هر ماه پانزدهم» — the day must be the first thing after the phrase. A number
# buried in the task is not a day of the month, and «هر ماه ساعت ۹» is a clock.
_MONTH_DAY_LEADING = re.compile(rf"^[\s،,؛;]*(?:و\s*)?(?P<day>{_DAY_TOKEN})(?![\w])", re.IGNORECASE)


def _day_value(token: str) -> Optional[int]:
    token = re.sub(r"\s+", " ", token.replace("\u200c", " ")).strip()
    if token in _ORDINALS:
        return _ORDINALS[token]
    if token.isdigit():
        day = int(token)
        return day if 1 <= day <= 31 else None
    stem = re.sub(r"\s*(?:ام|م)$", "", token.replace("\u200c", " ")).strip()
    total = 0
    for part in re.split(r"\s+و?\s*|و", stem):
        part = part.strip()
        if not part:
            continue
        value = _WORD_VALUES.get(part)
        if value is None:
            return None
        total += value
    return total if 1 <= total <= 31 else None


def parse_month_day(text: str) -> Optional[int]:
    """«پانزدهم» / «۱۵» / «اول» — a day of the month, or None."""
    match = _MONTH_DAY_RE.search(_normalized(text))
    return _day_value(match.group("day")) if match else None


def finalize(phrase: RecurrencePhrase, *, zone: ZoneInfo, now_utc: datetime,
             hour: int, minute: int) -> Recurrence:
    """Fill in what the sentence left implicit, against today's date."""
    today = now_utc.astimezone(zone).date()
    weekdays = phrase.weekdays
    day = phrase.day or 0
    if phrase.kind == "weekly" and not weekdays:
        # «هر هفته» with no day means today's weekday, said once and then kept.
        weekdays = (today.weekday(),)
    if phrase.kind == "monthly":
        day = phrase.day or today.day
    return build(phrase.kind, phrase.interval, weekdays, day, hour, minute)


def build(kind: str, interval: int, weekdays: tuple[int, ...], day: int,
          hour: int, minute: int) -> Recurrence:
    if kind == "weekly" and not weekdays:
        raise ReminderError("کدام روز هفته؟", code="recurring")
    if interval > 1 and kind == "weekly" and len(weekdays) > 1:
        raise ReminderError(
            "«هر چند هفته» فقط با یک روز هفته ثبت می‌شود؛ مثلاً «هر دو هفته شنبه».",
            code="recurring_weekdays",
        )
    return Recurrence(kind=kind, hour=hour, minute=minute, interval=max(1, interval),
                      weekdays=weekdays, day=max(1, min(31, day)))
