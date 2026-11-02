"""Durable one-shot reminders, with explicit timezone and safe scheduling.

All due times are stored in UTC; the user's IANA timezone is retained to show
exactly how a reminder was scheduled. The worker makes at-least-once delivery
attempts: Telegram has no idempotency key, so a crash after Telegram accepted a
message but before SQLite recorded success can result in a duplicate.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import html
import logging
import re
import sqlite3
import threading
import time
from typing import Awaitable, Callable, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram.error import BadRequest, Forbidden, RetryAfter

import i18n

logger = logging.getLogger("aura-reminders")
DEFAULT_TIMEZONE = "Asia/Tehran"
MAX_TEXT_LENGTH = 500
MAX_PENDING_PER_USER = 100
MAX_FUTURE_SECONDS = 5 * 366 * 24 * 3600
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
# Normalization is deliberately length-preserving: match spans must still point
# at the original text, including Persian digits and zero-width joiners.
_TRIGGER = re.compile(
    r"(?:یادم\s*(?:بندازی|بیندازی|بیاری|بیاوری|بنداز|بینداز|بیار|بیاور)(?!\w)|"
    r"(?:(?:به\s+من|بهم)\s*)?یادآوری\s*(?:تنظیم\s*کنی|تنظیم\s*کن|بکنی|بکن|کنی|کن|بذاری|بذار|بگذاری|بگذار)(?!\w)|"
    r"(?:یه\s+|یک\s+)?یادآور(?:ی)?\s*(?:تنظیم|ثبت|درست|ست)\s*(?:بکنی|بکن|کنی|کن)(?!\w)|"
    r"remind\s+me\b)", re.IGNORECASE,
)
_RECURRING = re.compile(r"(?:هر\s*(?:روز|هفته|ماه|ساعت)|روزانه|هفتگی|ماهانه|\bevery\b|\bdaily\b|\bweekly\b)", re.IGNORECASE)
_ABSOLUTE = re.compile(
    r"(?<!\d)(?P<year>\d{4})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})"
    r"[T\s]+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})?(?!\d)", re.IGNORECASE,
)
_NAMED_DAY = re.compile(
    r"(?:فردا|امروز|tomorrow|today)\s*(?:(?:ساعت|at)\s*)?"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?(?!\d)", re.IGNORECASE,
)
_WORD_VALUES = {
    "یک": 1, "یه": 1, "يک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5,
    "شش": 6, "هفت": 7, "هشت": 8, "نه": 9, "ده": 10, "یازده": 11,
    "دوازده": 12, "سیزده": 13, "چهارده": 14, "پانزده": 15, "پونزده": 15,
    "شانزده": 16, "شونزده": 16, "هفده": 17, "هجده": 18, "نوزده": 19,
    "بیست": 20, "سی": 30, "چهل": 40, "پنجاه": 50, "شصت": 60,
    "هفتاد": 70, "هشتاد": 80, "نود": 90, "صد": 100, "یکصد": 100,
    "دویست": 200, "سیصد": 300, "چهارصد": 400, "پانصد": 500,
    "ششصد": 600, "هفتصد": 700, "هشتصد": 800, "نهصد": 900, "هزار": 1000,
}
_WORD_NUMBER = "(?:" + "|".join(sorted(_WORD_VALUES, key=len, reverse=True)) + ")"
_RELATIVE = re.compile(
    rf"(?<![\w+\-−.,،٫٬/])(?P<prefix>in\s+|after\s+|بعد\s+از\s+|پس\s+از\s+|در\s+|تا\s+)?"
    rf"(?P<count>\d{{1,5}}|نیم|{_WORD_NUMBER}(?:\s+و\s+{_WORD_NUMBER}){{0,3}})\s*"
    r"(?P<unit>ثانیه|دقیقه|دقیقع|ساعت|روز|ربع|seconds?|minutes?|hours?|days?|s|m|h|d)"
    r"(?P<suffix>\s*(?:دیگه|دیگر|بعد|از\s+الان|later|from\s+now))?(?!\w)", re.IGNORECASE,
)


class ReminderError(ValueError):
    """Validation failure with a safe user-facing explanation and category."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


def _normalized(text: str) -> str:
    return str(text or "").translate(_DIGITS).replace("\u200c", " ").replace("ي", "ی").replace("ك", "ک")


def _number_value(text: str) -> float:
    if text.isdigit():
        return int(text)
    if text == "نیم":
        return 0.5
    words = re.split(r"\s+و\s+", text)
    total, previous_rank = 0, 10_000
    for word in words:
        value = _WORD_VALUES.get(word)
        if value is None:
            raise ReminderError("عدد زمان یادآوری واضح نیست؛ لطفاً دوباره بگو.", code="ambiguous_time")
        rank = 1000 if value >= 1000 else 100 if value >= 100 else 10 if value >= 10 else 1
        if rank >= previous_rank:
            raise ReminderError("عدد زمان یادآوری واضح نیست؛ لطفاً دوباره بگو.", code="ambiguous_time")
        total += value
        previous_rank = rank
    return total


def has_recurring_reminder(text: str) -> bool:
    return bool(_RECURRING.search(_normalized(text)))


def reminder_time_reply(text: str) -> Optional[str]:
    """Return a bare clarification time, adding an implied 'later' if needed.

    Only an entire time expression qualifies, so a pending question does not
    accidentally turn an unrelated conversation into a reminder.
    """
    value = str(text or "").strip().strip(" \t\n،,؛.!؟?")
    # Conversational wrappers are safe only when the remainder is exactly one
    # time; do not consume an unrelated sentence mentioning a duration.
    value = re.sub(r"^(?:(?:باشه|اوکی|همون|لطفا|لطفاً|به\s+نظرم)\s*[,،]?\s+)+", "", value, flags=re.I)
    value = re.sub(r"\s+(?:خوبه|مناسبه|باشه)$", "", value, flags=re.I).strip()
    normalized = _normalized(value)
    match = _RELATIVE.fullmatch(normalized)
    if match:
        return value if match.group("prefix") or match.group("suffix") else value + " دیگه"
    if _ABSOLUTE.fullmatch(normalized) or _NAMED_DAY.fullmatch(normalized):
        return value
    return None


def reminder_source_contains(source: str, quote: str) -> bool:
    """Match a whole user-supplied fragment, tolerating only orthography.

    A substring is not enough: «5 دقیقه» is inside «15 دقیقه», but is not
    what the person asked for. Do not accept a fragment inside a word/number.
    """
    def normalized(value: str) -> str:
        return re.sub(r"\s+", " ", _normalized(value)).strip().casefold()
    haystack, needle = normalized(source), normalized(quote)
    if not needle:
        return False
    for match in re.finditer(re.escape(needle), haystack):
        before = haystack[match.start() - 1] if match.start() else ""
        after = haystack[match.end()] if match.end() < len(haystack) else ""
        if before and (before.isalnum() or before in "_+-−.,،٫٬/") and needle[0].isalnum():
            continue
        if after and (after.isalnum() or after == "_") and needle[-1].isalnum():
            continue
        return True
    return False


_UNSAFE_NUMBER = re.compile(
    r"(?<!\w)(?:[-−+]\s*\d|\d+[.,،٫]\d+)\s*"
    r"(?:ثانیه|دقیقه|دقیقع|ساعت|روز|seconds?|minutes?|hours?|days?)", re.I,
)


def explicit_reminder_time_phrases(text: str, *, include_implicit: bool = False) -> list[str]:
    """Extract time phrases for generated wording checks, never action parsing."""
    original, value = str(text or ""), _normalized(text)
    matches = list(_ABSOLUTE.finditer(value)) + list(_NAMED_DAY.finditer(value))
    matches += [m for m in _RELATIVE.finditer(value) if include_implicit or m.group("prefix") or m.group("suffix")]
    return [original[m.start():m.end()] for m in matches]


# A conservative lexical backstop for generated wording. It is not a semantic
# verifier: when it cannot match the substantive words (including quantities),
# we prefer the saved task in a single fallback sentence over a wrong paraphrase.
_WORDING_GLUE = {
    "و", "یا", "از", "به", "با", "در", "برای", "که", "را", "رو", "من", "تو", "شما",
    "تا", "هم", "این", "اون", "می", "باید", "لطفا", "لطفاً", "کردن",
    "کن", "کنم", "کنی", "کنه", "بکن", "بکنم", "بکنی", "کردم", "کردی", "کنید",
    "برم", "بری", "بره", "برو", "رفتن", "باشم", "باشی", "بشم", "بشی", "بشه",
}
_CONFIRMATION_GLUE = _WORDING_GLUE | {
    "باشه", "باش", "حتما", "حتماً", "اوکی", "چشم", "بله", "آره", "خیالت", "خیال",
    "راحت", "نگران", "نباش", "یادت", "یادم", "یاد", "یادآوری", "یاداوری", "یادآورت",
    "میارم", "میآرم", "میندازم", "میاندازم", "بیارم", "بندازم", "میگم", "میگویم",
    "میکنم", "میدم", "میمونه", "می‌مونه", "میشم", "برات", "بهت", "بعد", "دیگه",
    "وقتش", "موقعش", "زمانش", "زمان", "رسید", "رسیدنش", "شد", "انجام", "حله", "خبرت",
    "می کنم", "می گم", "یادآور", "یادآوریه", "مطمئن", "میاد", "میشه", "حواسم", "هست",
}


def _wording_terms(text: str, *, confirmation: bool = False) -> list[str]:
    value = _normalized(text).casefold()
    value = re.sub(r"[\u064b-\u065f\u0670]", "", value)
    glue = _CONFIRMATION_GLUE if confirmation else _WORDING_GLUE
    terms: list[str] = []
    for word in re.findall(r"[آ-ی]+|[a-z]+|\d+", value):
        if word in glue:
            continue
        if word in _WORD_VALUES:
            word = str(_WORD_VALUES[word])
        elif word.startswith(("بخور", "خور")):
            word = "خور"
        elif word.startswith(("بنوش", "نوش")):
            word = "نوش"
        else:
            for suffix in (
                "هایی", "های", "تونو", "مونو", "شونو", "تون", "مون", "شون",
                "مو", "تو", "شو", "ها", "یم", "ید", "م", "ی",
            ):
                if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                    word = word[:-len(suffix)]
                    break
        if len(word) >= 2 or word.isdigit():
            terms.append(word)
    return terms


def _has_negated_task_action(text: str) -> bool:
    value = _normalized(text).casefold()
    # These phrases mean "remember", not "do not perform the saved action".
    value = re.sub(r"(?:یادت|یادم)\s*(?:نره|نرفته|نرود|نمی\s*ره)", " ", value)
    return bool(re.search(
        r"(?<!\w)(?:ن(?:خور|نوش|رو|ری|رم|ره|رید|ریم|کن|بر|خواب|گیر|بین|خر|فرست|زن|ذار|گو|ده)\w*"
        r"|نمی\s*(?:خور|نوش|رو|ری|کن|بر|خواب|گیر|بین|خر|فرست|زن|ذار|گو|ده)\w*)"
        r"|\b(?:don't|do not|never)\b", value, re.I,
    ))


def reminder_wording_matches_task(task: str, phrase: str, *, confirmation: bool = False) -> bool:
    """Check the saved task is not lost in a generated, non-neutral phrase.

    At delivery require its meaningful words/quantities. A confirmation may be
    neutral ("I'll remind you") without repeating the task. Conservative false
    negatives use a factual fallback; this check cannot prove semantic accuracy.
    """
    required = _wording_terms(task)
    if not required:
        return False
    candidate = str(phrase or "")
    if _has_negated_task_action(candidate) != _has_negated_task_action(task):
        return False
    if confirmation:
        for time_phrase in explicit_reminder_time_phrases(candidate):
            candidate = candidate.replace(time_phrase, " ")
    offered = _wording_terms(candidate, confirmation=confirmation)
    if confirmation and not offered:
        return True
    required_numbers = {word for word in required if word.isdigit()}
    offered_numbers = {word for word in offered if word.isdigit()}
    if required_numbers and required_numbers != offered_numbers:
        return False
    # «وقت داروته» naturally implies taking the medicine, even though it omits
    # the verb. Do not apply this shorthand to a negated or different action.
    medicine_due = bool(
        "خور" in required and any(x in required for x in ("دارو", "قرص"))
        and re.search(r"(?:وقت|نوبت)\s*(?:دارو|قرص)", _normalized(candidate), re.I)
        and not re.search(r"ن(?:خور|کن)", _normalized(candidate), re.I)
    )
    return all(
        (medicine_due and word == "خور")
        or any(word == item or (len(word) >= 3 and item.startswith(word)) for item in offered)
        for word in required
    )


def explicit_reminder_time_count(text: str) -> int:
    value = _normalized(text)
    return (len(list(_ABSOLUTE.finditer(value))) + len(list(_NAMED_DAY.finditer(value)))
            + sum(bool(m.group("prefix") or m.group("suffix")) for m in _RELATIVE.finditer(value)))


def ambiguous_reminder_source(text: str) -> bool:
    """Reject competing explicit times and approximate/alternative durations."""
    value = _normalized(text)
    relative = list(_RELATIVE.finditer(value))
    if _UNSAFE_NUMBER.search(value) or explicit_reminder_time_count(value) > 1:
        return True
    # «دو ساعت و سی دقیقه دیگه» is one compound duration, not 30
    # minutes or two independent reminders. Ask rather than choosing half.
    for first, second in zip(relative, relative[1:]):
        between = value[first.end():second.start()]
        if re.fullmatch(r"\s*(?:و|and)\s*", between, re.I):
            return True
    for match in relative:
        try:
            _number_value(match.group("count"))
        except ReminderError:
            return True
        before = value[:match.start()].rstrip()
        if re.search(rf"(?<!\w)(?:{_WORD_NUMBER}|یا|حدود|تقریبا|تقریباً|شاید)\s*$", before):
            return True
    return False


def grounded_reminder_task(sources: list[str], when: str) -> Optional[str]:
    """For a recognized trigger, recover the *entire* task, not an LLM fragment.

    Use only length-preserving normalization to retain the user's literal
    wording. If the quoted time cannot be located exactly, do not guess.
    """
    combined = " ".join(sources).strip()
    normalized, needle = _normalized(combined), _normalized(when).strip()
    trigger = _TRIGGER.search(normalized)
    if not trigger:
        return None
    start = normalized.find(needle)
    if not needle or start < 0:
        raise ReminderError("زمان و متن یادآوری را دقیق‌تر بگو؛ چیزی ثبت نکردم.", code="ambiguous_time")
    return _message_without_spans(combined, [trigger.span(), (start, start + len(needle))])


def parse_reminder_time_expression(text: str, zone_name: str, *, now: Optional[datetime] = None,
                                   allow_implicit_relative: bool = False) -> datetime:
    """Validate a quoted time on its own, without misreading durations in a task."""
    value = str(text or "").strip()
    if allow_implicit_relative:
        value = reminder_time_reply(value) or value
    marker = "__aura_reminder_task__"
    spec = parse_reminder(f"یادم بنداز {value} {marker}", zone_name, now=now, natural=True)
    if spec.text != marker:
        raise ReminderError("زمان را واضح‌تر بگو؛ چیزی ثبت نکردم.", code="ambiguous_time")
    return spec.due_utc



@dataclass(frozen=True)
class ReminderSpec:
    due_utc: datetime
    text: str
    timezone: str


def timezone_for(name: str) -> ZoneInfo:
    value = str(name or "").strip()
    if not value or len(value) > 100:
        raise ReminderError("منطقهٔ زمانی معتبر نیست؛ مثلاً Asia/Tehran یا Europe/Berlin بفرست.", code="invalid_timezone")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ReminderError(
            "منطقهٔ زمانی پیدا نشد؛ از نام IANA مثل Asia/Tehran یا Europe/Berlin استفاده کن.",
            code="unknown_timezone",
        ) from None


def is_reminder_request(text: str) -> bool:
    value = _normalized(text)
    match = _TRIGGER.search(value)
    if not match:
        return False
    before = value[:match.start()]
    # A quoted example, translation, negation or hypothetical is not an
    # instruction to create a real reminder. Polite «اگه میشه» is different.
    if (before.count("«") > before.count("»") or before.count('"') % 2
            or re.search(r"(?:ترجمه|معنی|معنای|مثال|داستان|نقل قول|نمی\s*خوام|"
                         r"نمی\s*خواهم|(?:اگه|اگر)(?!\s*(?:میشه|می\s*شه|امکانش|می\s*تونی))|"
                         r"\b(?:translate|example|fiction|do not|don't)\b)", before, re.I)):
        return False
    # «یادم بیار اسمم چیه؟» is a request to recall information, not a
    # request for a future notification. Explicit time changes that meaning.
    after = value[match.end():]
    has_time = bool(_ABSOLUTE.search(value) or _NAMED_DAY.search(value)
                    or any(m.group("prefix") or m.group("suffix") for m in _RELATIVE.finditer(value)))
    if not has_time and re.search(
        r"(?:چیه|چیست|چی بود|چه بود|\b(?:چی|کی|کیم|کیه|کیستی|کجا|کجاست)\b|"
        r"چه کسی|\b(?:what|who|where)\b)",
        after, re.I,
    ):
        return False
    return True


def _local_to_utc(local: datetime, zone: ZoneInfo) -> datetime:
    """Reject missing/repeated local times rather than choosing a DST fold."""
    options = []
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == local and utc not in options:
            options.append(utc)
    if not options:
        raise ReminderError(
            "این ساعت به‌خاطر تغییر ساعت در منطقهٔ زمانی انتخاب‌شده وجود ندارد؛ ساعت دیگری بفرست.",
            code="nonexistent_time",
        )
    if len(options) > 1:
        raise ReminderError(
            "این ساعت به‌خاطر تغییر ساعت دو بار تکرار می‌شود؛ ساعت دیگری یا تاریخ با اختلاف UTC صریح بفرست.",
            code="repeated_local_time",
        )
    return options[0]


def _message_without_spans(original: str, spans: list[tuple[int, int]]) -> str:
    chars = list(original)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    text = "".join(chars)
    text = re.sub(r"\s+", " ", text).strip(" \t\n،,:؛-–")
    # These are conversational glue only at the boundary, never inside the task.
    text = re.sub(r"^(?:(?:اگه\s*)?(?:میشه|می\s*شه)|می\s*تونی|لطفا|لطفاً|که|تا|برای\s+من|please)\s+", "", text, flags=re.IGNORECASE).strip()
    return text


def parse_reminder(text: str, zone_name: str, *, now: Optional[datetime] = None, natural: bool = False) -> ReminderSpec:
    """Parse a conservative one-shot request without inventing dates or tasks.

    Supports Gregorian YYYY-MM-DD HH:MM, today/tomorrow + HH:MM, and a relative
    number of seconds/minutes/hours/days. Natural requests must explicitly say
    when (e.g. «۱۰ دقیقه دیگه»); unsupported or ambiguous input is rejected.
    """
    original = str(text or "").strip()
    normalized = _normalized(original)
    zone = timezone_for(zone_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    trigger = _TRIGGER.search(normalized)
    if natural and not trigger:
        raise ReminderError("درخواست یادآوری مشخص نیست.", code="missing_intent")
    if _RECURRING.search(normalized):
        raise ReminderError("یادآور تکراری فعلاً پشتیبانی نمی‌شود؛ می‌توانی یک یادآور یک‌باره ثبت کنی.", code="recurring")
    if _UNSAFE_NUMBER.search(normalized):
        raise ReminderError("زمان را با عدد مثبت و واضح بگو؛ هنوز چیزی ثبت نکردم.", code="ambiguous_time")

    matches = [("absolute", m) for m in _ABSOLUTE.finditer(normalized)]
    matches += [("day", m) for m in _NAMED_DAY.finditer(normalized)]
    matches += [("relative", m) for m in _RELATIVE.finditer(normalized)]
    if len(matches) != 1:
        raise ReminderError(
            "زمان یادآوری را دقیق بگو؛ مثلاً «۱۰ دقیقه دیگه یادم بنداز آب بخورم» "
            "یا /remind 2026-11-01 09:30 آب بخورم (تاریخ میلادی).",
            code="missing_time" if not matches else "ambiguous_time",
        )
    kind, match = matches[0]
    try:
        if kind == "relative":
            if natural and not (match.group("prefix") or match.group("suffix")):
                raise ReminderError("بگو چند دقیقه یا ساعت دیگر؛ مثلاً «۱۰ دقیقه دیگه یادم بنداز آب بخورم».", code="missing_time")
            count = _number_value(match.group("count"))
            # Do not silently choose the final number of an approximate or
            # malformed expression like «یکی دو دقیقه» or «پنج یا ده دقیقه».
            before = normalized[:match.start()].rstrip()
            if re.search(rf"(?<!\w)(?:{_WORD_NUMBER}|یا|حدود|تقریبا|تقریباً)\s*$", before):
                raise ReminderError("زمان تقریبی یا چندگزینه‌ای است؛ زمان دقیق را بگو.", code="ambiguous_time")
            unit = match.group("unit").lower()
            if unit in {"s", "second", "seconds", "ثانیه"}:
                seconds = count
            elif unit in {"m", "minute", "minutes", "دقیقه", "دقیقع"}:
                seconds = count * 60
            elif unit in {"h", "hour", "hours", "ساعت"}:
                seconds = count * 3600
            elif unit == "ربع":
                seconds = count * 900
            else:
                seconds = count * 86400
            due = current + timedelta(seconds=seconds)
        else:
            if kind == "absolute":
                y, month, day = (int(match.group(key)) for key in ("year", "month", "day"))
            else:
                local_today = current.astimezone(zone).date()
                is_tomorrow = match.group(0).lower().startswith(("فردا", "tomorrow"))
                selected = local_today + timedelta(days=1 if is_tomorrow else 0)
                y, month, day = selected.year, selected.month, selected.day
            local = datetime(y, month, day, int(match.group("hour")), int(match.group("minute")), int(match.group("second") or 0))
            offset = match.group("offset") if kind == "absolute" else None
            if offset:
                if offset.upper() == "Z":
                    fixed_zone = timezone.utc
                else:
                    sign = -1 if offset[0] == "-" else 1
                    hours, minutes = map(int, offset[1:].split(":"))
                    if hours > 23 or minutes > 59:
                        raise ValueError("invalid UTC offset")
                    fixed_zone = timezone(sign * timedelta(hours=hours, minutes=minutes))
                due = local.replace(tzinfo=fixed_zone).astimezone(timezone.utc)
            else:
                due = _local_to_utc(local, zone)
    except ReminderError:
        raise
    except (ValueError, OverflowError):
        raise ReminderError(
            "تاریخ یا ساعت معتبر نیست؛ تاریخ را میلادی و به شکل YYYY-MM-DD HH:MM بفرست.",
            code="invalid_datetime",
        ) from None
    delta = (due - current).total_seconds()
    if delta < 5:
        raise ReminderError("زمان یادآوری باید دست‌کم چند ثانیه در آینده باشد.", code="time_too_soon")
    if delta > MAX_FUTURE_SECONDS:
        raise ReminderError("زمان یادآوری بیش از پنج سال در آینده است.", code="time_too_far")
    spans = [match.span()]
    if trigger:
        spans.append(trigger.span())
    content = _message_without_spans(original, spans)
    if not content:
        raise ReminderError("چه چیزی را یادآوری کنم؟ متن یادآوری را هم بفرست.", code="missing_task")
    if len(content) > MAX_TEXT_LENGTH:
        raise ReminderError(f"متن یادآوری خیلی طولانی است؛ حداکثر {MAX_TEXT_LENGTH} کاراکتر.", code="text_too_long")
    return ReminderSpec(due_utc=due, text=content, timezone=zone.key)


def format_due(timestamp: int, zone_name: str) -> str:
    local = datetime.fromtimestamp(int(timestamp), timezone.utc).astimezone(timezone_for(zone_name))
    return f"{local:%Y-%m-%d %H:%M:%S} ({zone_name})"


class ReminderStore:
    def __init__(self, db: sqlite3.Connection, lock: threading.Lock, *, default_timezone: str = DEFAULT_TIMEZONE,
                 clock: Callable[[], float] = time.time):
        self.db = db
        self.lock = lock
        self.default_timezone = timezone_for(default_timezone).key
        self.clock = clock
        if self.db.row_factory is None:
            self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("""CREATE TABLE IF NOT EXISTS reminder_timezones (
                user_id INTEGER PRIMARY KEY, timezone TEXT NOT NULL, updated_at INTEGER NOT NULL
            )""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, text TEXT NOT NULL,
                due_utc INTEGER NOT NULL, timezone TEXT NOT NULL, created_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', next_attempt INTEGER NOT NULL,
                lease_until INTEGER NOT NULL DEFAULT 0, claim_token TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, delivered_at INTEGER, last_error TEXT NOT NULL DEFAULT '',
                recipient_name TEXT NOT NULL DEFAULT ''
            )""")
            # Existing reminders keep their IDs/statuses and get a safe generic
            # display name; the verified Telegram user_id was already stored.
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(reminders)").fetchall()}
            if "recipient_name" not in columns:
                self.db.execute("ALTER TABLE reminders ADD COLUMN recipient_name TEXT NOT NULL DEFAULT ''")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, next_attempt, due_utc)")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_reminders_owner ON reminders(user_id, chat_id, status)")
            self.db.commit()

    def get_timezone(self, user_id: int) -> str:
        with self.lock:
            row = self.db.execute("SELECT timezone FROM reminder_timezones WHERE user_id = ?", (user_id,)).fetchone()
        return str(row["timezone"]) if row else self.default_timezone

    def set_timezone(self, user_id: int, zone_name: str) -> str:
        zone = timezone_for(zone_name).key
        with self.lock:
            self.db.execute("""INSERT INTO reminder_timezones(user_id, timezone, updated_at) VALUES(?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET timezone=excluded.timezone, updated_at=excluded.updated_at""",
                (user_id, zone, int(self.clock())))
            self.db.commit()
        return zone

    def create(self, chat_id: int, user_id: int, spec: ReminderSpec, *, recipient_name: str = "") -> int:
        now = int(self.clock())
        if spec.due_utc.tzinfo is None or spec.due_utc.utcoffset() is None:
            raise ReminderError("زمان یادآوری باید منطقهٔ زمانی مشخص داشته باشد.", code="missing_timezone")
        due = int(spec.due_utc.timestamp())
        if due < now + 1 or due > now + MAX_FUTURE_SECONDS:
            raise ReminderError("زمان یادآوری باید در آینده و حداکثر پنج سال دیگر باشد.", code="out_of_range")
        timezone_for(spec.timezone)
        text = str(spec.text or "").strip()
        if not text or len(text) > MAX_TEXT_LENGTH:
            raise ReminderError("متن یادآوری خالی یا بیش از حد طولانی است.", code="bad_text")
        # A display name is only a label, never an authority for the mention.
        # Strip controls and bound it; Telegram IDs always come from the update.
        name = re.sub(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]", " ", str(recipient_name or ""))
        name = re.sub(r"\s+", " ", name).strip()[:80]
        with self.lock:
            try:
                count = self.db.execute(
                    "SELECT COUNT(*) AS n FROM reminders WHERE user_id=? AND status IN ('pending', 'sending')", (user_id,),
                ).fetchone()["n"]
                if count >= MAX_PENDING_PER_USER:
                    raise ReminderError("تعداد یادآورهای فعال به سقف مجاز رسیده است؛ یکی را لغو کن.", code="limit_reached")
                cur = self.db.execute("""INSERT INTO reminders
                    (chat_id, user_id, text, due_utc, timezone, created_at, status, next_attempt, recipient_name)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    (chat_id, user_id, text, due, spec.timezone, now, due, name))
                self.db.commit()
                return int(cur.lastrowid)
            except Exception:
                # A failed commit must not leave an uncommitted row that some
                # unrelated later operation could accidentally commit.
                self.db.rollback()
                raise

    def get_owned(self, chat_id: int, user_id: int, reminder_id: int) -> Optional[dict]:
        with self.lock:
            row = self.db.execute("SELECT * FROM reminders WHERE id=? AND chat_id=? AND user_id=?",
                                  (reminder_id, chat_id, user_id)).fetchone()
        return dict(row) if row else None

    def list_active(self, chat_id: int, user_id: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute("""SELECT * FROM reminders WHERE chat_id=? AND user_id=?
                AND status IN ('pending', 'sending', 'failed')
                ORDER BY CASE WHEN status='failed' THEN 1 ELSE 0 END, due_utc, id LIMIT 200""", (chat_id, user_id)).fetchall()
        return [dict(row) for row in rows]

    def cancel(self, chat_id: int, user_id: int, reminder_id: int) -> bool:
        with self.lock:
            cur = self.db.execute("""UPDATE reminders SET status='cancelled', claim_token=NULL, lease_until=0
                WHERE id=? AND chat_id=? AND user_id=? AND status IN ('pending', 'failed')""",
                (reminder_id, chat_id, user_id))
            self.db.commit()
            return cur.rowcount == 1

    def recover_inflight(self) -> int:
        """Requeue interrupted sends on startup (one polling instance only)."""
        now = int(self.clock())
        with self.lock:
            cur = self.db.execute("""UPDATE reminders SET status='pending', claim_token=NULL, lease_until=0,
                next_attempt=? WHERE status='sending'""", (now,))
            self.db.commit()
            return int(cur.rowcount)

    def claim_due(self, *, lease_seconds: int = 180) -> Optional[dict]:
        now = int(self.clock())
        token = uuid4().hex
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                row = self.db.execute("""SELECT * FROM reminders WHERE
                    (status='pending' AND due_utc<=? AND next_attempt<=?)
                    OR (status='sending' AND lease_until<=?) ORDER BY due_utc, id LIMIT 1""",
                    (now, now, now)).fetchone()
                if not row:
                    self.db.commit()
                    return None
                self.db.execute("""UPDATE reminders SET status='sending', claim_token=?, lease_until=?,
                    attempts=attempts+1 WHERE id=?""", (token, now + lease_seconds, row["id"]))
                claimed = self.db.execute("SELECT * FROM reminders WHERE id=?", (row["id"],)).fetchone()
                self.db.commit()
                return dict(claimed)
            except Exception:
                self.db.rollback()
                raise

    def mark_sent(self, row: dict) -> bool:
        with self.lock:
            cur = self.db.execute("""UPDATE reminders SET status='sent', delivered_at=?, lease_until=0,
                claim_token=NULL, last_error='' WHERE id=? AND status='sending' AND claim_token=?""",
                (int(self.clock()), row["id"], row["claim_token"]))
            self.db.commit()
            return cur.rowcount == 1

    def retry(self, row: dict, *, delay: int, error: str) -> bool:
        with self.lock:
            cur = self.db.execute("""UPDATE reminders SET status='pending', next_attempt=?, claim_token=NULL,
                lease_until=0, last_error=? WHERE id=? AND status='sending' AND claim_token=?""",
                (int(self.clock()) + max(0, int(delay)), error[:80], row["id"], row["claim_token"]))
            self.db.commit()
            return cur.rowcount == 1

    def mark_failed(self, row: dict, *, error: str) -> bool:
        with self.lock:
            cur = self.db.execute("""UPDATE reminders SET status='failed', claim_token=NULL, lease_until=0,
                last_error=? WHERE id=? AND status='sending' AND claim_token=?""",
                (error[:80], row["id"], row["claim_token"]))
            self.db.commit()
            return cur.rowcount == 1


class ReminderScheduler:
    def __init__(self, store: ReminderStore, bot, *, poll_seconds: float = 3.0,
                 can_send: Optional[Callable[[int], bool]] = None,
                 compose_message: Optional[Callable[[dict], Awaitable[str]]] = None,
                 language: Optional[Callable[[dict], str]] = None):
        self.store, self.bot = store, bot
        self.poll_seconds = max(1.0, min(60.0, float(poll_seconds)))
        self.can_send = can_send
        self.compose_message = compose_message
        # Delivery runs without an Update; the deployment supplies the recipient's
        # language so a fallback wording never assumes Persian.
        self.language = language
        self.stop_event = asyncio.Event()

    def _language(self, row: dict) -> Optional[str]:
        try:
            return self.language(row) if self.language else None
        except Exception:
            logger.warning("Reminder language lookup failed | id=%s", row.get("id"), exc_info=True)
            return None

    async def _message_text(self, row: dict, lang: Optional[str] = None) -> str:
        task = str(row["text"])
        if self.compose_message:
            try:
                result = await self.compose_message(row)
                if isinstance(result, str) and 0 < len(result.strip()) <= 1000:
                    result = result.strip()
                    if reminder_wording_matches_task(task, result):
                        return result
                    logger.warning("Reminder wording did not preserve the task | id=%s", row["id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Reminder composition failed | id=%s", row["id"], exc_info=True)
        return i18n.t("reminder.delivery.fallback", lang, task=task)

    async def run_once(self) -> bool:
        row = self.store.claim_due()
        if not row:
            return False
        lang = self._language(row)
        try:
            if self.can_send and not self.can_send(row["chat_id"]):
                self.store.retry(row, delay=300, error="chat_not_allowed")
                return True
            message = await self._message_text(row, lang)
            # Access may have been revoked while the model was composing.
            if self.can_send and not self.can_send(row["chat_id"]):
                self.store.retry(row, delay=300, error="chat_not_allowed")
                return True
            if row["chat_id"] < 0 and row["user_id"] > 0:
                # Telegram's text-mention URL uses the authoritative stored ID.
                # Escape *all* model and user text; neither can inject HTML or a
                # mention for a different person. Pre-migration rows use a label.
                raw_name = row.get("recipient_name") or i18n.t("reminder.delivery.recipient", lang)
                name = html.escape(raw_name)
                mention = f'<a href="tg://user?id={int(row["user_id"])}">{name}</a>'
                try:
                    await self.bot.send_message(
                        chat_id=row["chat_id"], text=f"{mention}، {html.escape(message)}", parse_mode="HTML",
                    )
                except BadRequest:
                    # If Telegram rejects the text-mention entity, still try to
                    # deliver the reminder literally. An invalid/inaccessible
                    # chat will fail again and be recorded as permanently failed.
                    logger.warning("Reminder mention rejected; trying plain text | id=%s", row["id"])
                    await self.bot.send_message(chat_id=row["chat_id"], text=f"{raw_name}، {message}")
            else:
                await self.bot.send_message(chat_id=row["chat_id"], text=message)
        except asyncio.CancelledError:
            self.store.retry(row, delay=0, error="interrupted")
            raise
        except RetryAfter as exc:
            delay_value = exc.retry_after
            if isinstance(delay_value, timedelta):
                delay_value = delay_value.total_seconds()
            self.store.retry(row, delay=max(1, int(delay_value) + 1), error="rate_limited")
        except (Forbidden, BadRequest) as exc:
            logger.warning("Reminder permanently failed | id=%s | type=%s", row["id"], type(exc).__name__)
            self.store.mark_failed(row, error=type(exc).__name__)
        except Exception as exc:
            delay = min(3600, 30 * (2 ** min(max(0, row["attempts"] - 1), 7)))
            logger.warning("Reminder delivery will be retried | id=%s | type=%s", row["id"], type(exc).__name__)
            self.store.retry(row, delay=delay, error=type(exc).__name__)
        else:
            self.store.mark_sent(row)
        return True

    async def run(self) -> None:
        # A single long-polling bot instance owns this SQLite file. On restart,
        # recover sends interrupted between the claim and Telegram confirmation.
        needs_recovery = True
        while not self.stop_event.is_set():
            try:
                if needs_recovery:
                    recovered = self.store.recover_inflight()
                    needs_recovery = False
                    if recovered:
                        logger.warning("Recovered %s interrupted reminder(s)", recovered)
                if await self.run_once():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient database error during startup must not kill the
                # worker permanently; recovery remains pending and is retried.
                logger.exception("Reminder scheduler iteration failed")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self.stop_event.set()
