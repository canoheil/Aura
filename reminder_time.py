"""What a reminder request means: the words, the clock, and the calendar.

Kept apart from storage and delivery on purpose. Everything in this module is
pure: given a message, a timezone and "now", it either returns a `ReminderSpec`
or a `ReminderError` carrying a stable code. Nothing here writes, sends or
sleeps, which is what makes the rules testable as a table.

The grammar is deliberately narrow, but it now covers the ways Persian speakers
actually write a time: «فردا ساعت ۹ صبح», «دوشنبه ۱۷:۰۰», «۲۸ آبان»، «یک ساعت و
نیم دیگه». Anything outside it is *asked about*, never guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Tehran"
MAX_TEXT_LENGTH = 500
MAX_FUTURE_SECONDS = 5 * 366 * 24 * 3600

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_FA_OUT = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


class ReminderError(ValueError):
    """Validation failure with a safe user-facing explanation and category."""

    def __init__(self, message: str, *, code: str = "invalid", fragment: str = ""):
        super().__init__(message)
        self.code = code
        # The part of the request that was understood but incomplete, e.g.
        # «فردا صبح». The conversation layer re-uses it when asking again.
        self.fragment = fragment


@dataclass(frozen=True)
class ReminderSpec:
    due_utc: datetime
    text: str
    timezone: str


# --- normalization ----------------------------------------------------------
# Every normalization below is one character for one character. The match spans
# measured on the normalized text must keep pointing at the original message,
# including Persian digits and zero-width joiners.

def _normalized(text: str) -> str:
    return str(text or "").translate(_DIGITS).replace("\u200c", " ").replace("ي", "ی").replace("ك", "ک")


def _fa_digits(value: str) -> str:
    return str(value).translate(_FA_OUT)


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


# --- numbers ----------------------------------------------------------------

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


# --- calendar ---------------------------------------------------------------
# Solar Hijri conversion, arithmetic form. About forty lines instead of a
# dependency, which keeps the cPanel deployment free of new requirements.

_JALALI_MONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                  "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")
_JALALI_MONTH_INDEX = {name: index + 1 for index, name in enumerate(_JALALI_MONTHS)}
_JALALI_MONTH_PATTERN = "|".join(sorted(_JALALI_MONTHS, key=len, reverse=True))

_FA_WEEKDAYS = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")
_WEEKDAY_INDEX = {
    "شنبه": 5, "یک‌شنبه": 6, "یکشنبه": 6, "دوشنبه": 0, "سه‌شنبه": 1, "سهشنبه": 1,
    "چهارشنبه": 2, "پنج‌شنبه": 3, "پنجشنبه": 3, "جمعه": 4,
    "saturday": 5, "sunday": 6, "monday": 0, "tuesday": 1, "wednesday": 2,
    "thursday": 3, "friday": 4,
}
_WEEKDAY_NAMES = (r"یک\s*شنبه|سه\s*شنبه|چهار\s*شنبه|پنج\s*شنبه|دو\s*شنبه|شنبه|جمعه|"
                  r"saturday|sunday|monday|tuesday|wednesday|thursday|friday")


def _gregorian_to_jalali(year: int, month: int, day: int) -> tuple[int, int, int]:
    month_starts = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = year + 1 if month > 2 else year
    days = (355666 + (365 * year) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100)
            + ((gy2 + 399) // 400) + day + month_starts[month - 1])
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + (days // 31), 1 + (days % 31)
    return jy, 7 + ((days - 186) // 30), 1 + ((days - 186) % 30)


def _jalali_to_gregorian(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    if jy > 979:
        gy, jy = 1600, jy - 979
    else:
        gy = 621
    days = ((365 * jy) + ((jy // 33) * 8) + (((jy % 33) + 3) // 4) + 78 + jd
            + ((jm - 1) * 31 if jm < 7 else ((jm - 7) * 30) + 186))
    gy += 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)
    lengths = (0, 31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    gm = 1
    while gm < 13 and gd > lengths[gm]:
        gd -= lengths[gm]
        gm += 1
    return gy, gm, gd


# --- the grammar ------------------------------------------------------------

_TRIGGER = re.compile(
    r"(?:یادم\s*(?:بندازی|بیندازی|بیاری|بیاوری|بنداز|بینداز|بیار|بیاور)(?!\w)|"
    r"(?:(?:به\s+من|بهم)\s*)?یادآوری\s*(?:تنظیم\s*کنی|تنظیم\s*کن|بکنی|بکن|کنی|کن|بذاری|بذار|بگذاری|بگذار)(?!\w)|"
    r"(?:یه\s+|یک\s+)?یادآور(?:ی)?\s*(?:تنظیم|ثبت|درست|ست)\s*(?:بکنی|بکن|کنی|کن)(?!\w)|"
    r"remind\s+me\b)", re.IGNORECASE,
)
_RECURRING = re.compile(
    r"(?:هر\s*(?:روز|هفته|ماه|ساعت)|روزانه|هفتگی|ماهانه|\bevery\b|\bdaily\b|\bweekly\b)",
    re.IGNORECASE,
)

# Persian says the clock out loud: ۹ صبح, ۵ عصر, ۹ شب. Some of those hours mean
# the same wall-clock time as their 24-hour twin, and some roll into the next
# day — «۱۲ شب» is midnight, not noon.
_MERIDIEM = r"(?:نیمه\s*شب|بعد\s*از\s*ظهر|بعدازظهر|بامداد|صبح|ظهر|عصر|شب|a\.m\.|p\.m\.|am|pm)"
_CLOCK_STRICT = (
    r"(?:(?P<h1>\d{1,2}):(?P<m1>\d{2})(?::(?P<s1>\d{2}))?(?:\s*(?P<mer1>" + _MERIDIEM + r"))?"
    r"|(?P<h2>\d{1,2})\s*(?P<mer2>" + _MERIDIEM + r"))"
)
_CLOCK_HOUR = r"(?P<h3>\d{1,2})(?!\d)"

_DAY_WORD = r"(?:امروز|فردا|today|tomorrow)"

_ABSOLUTE = re.compile(
    r"(?<!\d)(?P<year>\d{4})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})"
    r"[T\s]+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})?(?!\d)", re.IGNORECASE,
)
_DAY_CLOCK = re.compile(
    rf"(?<![\w])(?P<day>{_DAY_WORD})\s*(?P<part>صبح|بامداد|ظهر|عصر|شب)?\s*"
    rf"(?:(?:ساعت|at|،)\s*)?{_CLOCK_STRICT}(?!\d)", re.IGNORECASE,
)
_WEEKDAY_CLOCK = re.compile(
    rf"(?<![\w])(?P<weekday>{_WEEKDAY_NAMES})\s*(?P<part>صبح|بامداد|ظهر|عصر|شب)?\s*"
    rf"(?:(?:ساعت|at|،)\s*)?{_CLOCK_STRICT}(?!\d)", re.IGNORECASE,
)
_JALALI_DATE = re.compile(
    rf"(?<!\d)(?P<jday>\d{{1,2}})\s*(?P<jmonth>{_JALALI_MONTH_PATTERN})(?:\s*(?P<jyear>1[34]\d{{2}}))?"
    rf"(?:\s*(?:ساعت|at)\s*{_CLOCK_STRICT})?", re.IGNORECASE,
)
_CLOCK_STRICT_ONLY = re.compile(
    rf"(?:(?:سر|راس|رأس)\s*)?(?:(?<!\w)(?:ساعت|at)\s*)?{_CLOCK_STRICT}(?!\d)", re.IGNORECASE,
)
_HOUR_AFTER_SAAT = re.compile(
    rf"(?:(?:سر|راس|رأس)\s*)?(?:ساعت|at)\s*{_CLOCK_HOUR}(?!\d)", re.IGNORECASE,
)
_RELATIVE = re.compile(
    rf"(?<![\w+\-−.,،٫٬/])(?P<prefix>in\s+|after\s+|بعد\s+از\s+|پس\s+از\s+|در\s+|تا\s+)?"
    rf"(?P<count>\d{{1,5}}|نیم|{_WORD_NUMBER}(?:\s+و\s+{_WORD_NUMBER}){{0,3}})\s*"
    r"(?P<unit>ثانیه|دقیقه|دقیقع|ساعت|روز|ربع|seconds?|minutes?|hours?|days?|s|m|h|d)"
    r"(?P<suffix>\s*(?:دیگه|دیگر|بعد|از\s+الان|later|from\s+now))?(?!\w)", re.IGNORECASE,
)

_UNIT_PATTERN = r"(?:ثانیه|دقیقه|دقیقع|ساعت|روز|ربع|seconds?|minutes?|hours?|days?)"
_SINGLE_DURATION = re.compile(
    rf"(?P<count>\d{{1,5}}|نیم|{_WORD_NUMBER})\s*(?P<unit>{_UNIT_PATTERN})", re.IGNORECASE,
)
_ONE_DURATION = rf"(?:\d{{1,5}}|نیم|{_WORD_NUMBER})\s*{_UNIT_PATTERN}"
_COMPOUND_RELATIVE = re.compile(
    rf"(?<![\w+\-−.,،٫٬/])(?P<prefix>in\s+|after\s+|بعد\s+از\s+|پس\s+از\s+|در\s+|تا\s+)?"
    rf"(?P<body>{_ONE_DURATION}(?:\s*و\s*(?:{_ONE_DURATION}|نیم|ربع))+)"
    rf"(?P<suffix>\s*(?:دیگه|دیگر|بعد|از\s+الان|later|from\s+now))?(?!\w)", re.IGNORECASE,
)
# «ساعت و نیم» — the unit is spoken once, with no leading number.
_UNIT_FIRST_COMPOUND = re.compile(
    rf"(?<![\w+\-−.,،٫٬/])(?P<prefix>in\s+|after\s+|بعد\s+از\s+|پس\s+از\s+|در\s+|تا\s+)?"
    rf"(?P<body>{_UNIT_PATTERN}\s*و\s*(?:نیم|ربع))"
    rf"(?P<suffix>\s*(?:دیگه|دیگر|بعد|از\s+الان|later|from\s+now))?(?!\w)", re.IGNORECASE,
)
_DAY_BEFORE = re.compile(rf"(?:^|[\s،,])(?P<day>{_DAY_WORD}|{_WEEKDAY_NAMES})\s*$", re.IGNORECASE)
_DAY_AFTER = re.compile(rf"^\s*(?P<day>{_DAY_WORD}|{_WEEKDAY_NAMES})(?![\w])", re.IGNORECASE)

_UNSAFE_NUMBER = re.compile(
    r"(?<!\w)(?:[-−+]\s*\d|\d+[.,،٫]\d+)\s*"
    r"(?:ثانیه|دقیقه|دقیقع|ساعت|روز|seconds?|minutes?|hours?|days?)", re.I,
)


@dataclass(frozen=True)
class TimeCandidate:
    """One time expression, where it sits in the message, and what it implies."""

    kind: str                 # absolute | day | weekday | jalali | clock | relative | compound
    start: int
    end: int
    match: re.Match
    day_word: Optional[str] = None


def _clock_fields(match: re.Match) -> tuple[int, int, int, Optional[str]]:
    groups = match.groupdict()
    if groups.get("h1") is not None:
        return (int(groups["h1"]), int(groups["m1"]), int(groups.get("s1") or 0), groups.get("mer1"))
    if groups.get("h2") is not None:
        return (int(groups["h2"]), 0, 0, groups.get("mer2"))
    if groups.get("h3") is not None:
        return (int(groups["h3"]), 0, 0, None)
    raise ReminderError("چه ساعتی؟ مثلاً «۹ صبح» یا «۱۷:۳۰».", code="missing_clock")


def _apply_meridiem(hour: int, word: Optional[str]) -> tuple[int, int]:
    """Map a spoken hour onto 24 hours, and say whether the day rolls over."""
    if not word:
        if 0 <= hour <= 23:
            return hour, 0
        raise ReminderError("ساعت معتبر نیست؛ بین ۰ تا ۲۳ بگو.", code="invalid_datetime")
    token = _normalized(word).lower().replace(" ", "").replace(".", "")
    if not 1 <= hour <= 12:
        raise ReminderError("ساعتی که با صبح/عصر می‌گویی باید بین ۱ تا ۱۲ باشد.", code="invalid_datetime")
    if token in {"صبح", "بامداد", "am"}:
        return (0 if hour == 12 else hour), (1 if hour == 12 else 0)
    if token in {"ظهر", "بعدازظهر", "عصر", "pm"}:
        return (12 if hour == 12 else hour + 12), 0
    if token == "شب":
        return (0 if hour == 12 else hour + 12), (1 if hour == 12 else 0)
    if token == "نیمهشب":
        return (0 if hour == 12 else hour), (1 if hour == 12 else 0)
    raise ReminderError("ساعت را واضح‌تر بگو.", code="ambiguous_time")


def _unit_seconds(unit: str) -> int:
    value = unit.lower()
    if value in {"s", "second", "seconds", "ثانیه"}:
        return 1
    if value in {"m", "minute", "minutes", "دقیقه", "دقیقع"}:
        return 60
    if value in {"h", "hour", "hours", "ساعت"}:
        return 3600
    if value == "ربع":
        return 900
    if value in {"d", "day", "days", "روز"}:
        return 86400
    raise ReminderError("واحد زمان را نفهمیدم.", code="ambiguous_time")


def _relative_seconds(match: re.Match) -> float:
    return _number_value(match.group("count")) * _unit_seconds(match.group("unit"))


def _compound_seconds(body: str) -> float:
    """«یک ساعت و نیم» is one duration, not two competing ones."""
    total = 0.0
    last_unit = None
    # «دو» ends with «و»: require whitespace before the conjunction or the
    # number itself is split in half.
    for part in (piece.strip() for piece in re.split(r"\s+و\s*", body.strip()) if piece.strip()):
        single = _SINGLE_DURATION.fullmatch(part)
        if single:
            last_unit = _unit_seconds(single.group("unit"))
            total += _number_value(single.group("count")) * last_unit
            continue
        if re.fullmatch(_UNIT_PATTERN, part, re.IGNORECASE):  # «ساعت و نیم»
            last_unit = _unit_seconds(part)
            total += last_unit
            continue
        if part in {"نیم", "ربع"}:
            if last_unit is None:
                raise ReminderError("زمان را کامل بگو؛ مثلاً «یک ساعت و نیم».", code="ambiguous_time")
            total += last_unit * (0.5 if part == "نیم" else 0.25)
            continue
        raise ReminderError("زمان را کامل بگو؛ مثلاً «یک ساعت و نیم».", code="ambiguous_time")
    return total


def _absorbed(candidate: TimeCandidate, value: str) -> TimeCandidate:
    """Pull an adjacent day word into a bare clock: «فردا ساعت ۹»."""
    if candidate.kind != "clock":
        return candidate
    start, end, day_word = candidate.start, candidate.end, None
    before = _DAY_BEFORE.search(value[:start])
    if before:
        start, day_word = before.start("day"), before.group("day")
    after = _DAY_AFTER.match(value[end:])
    if after and day_word is None:
        end, day_word = end + after.end("day"), after.group("day")
    if day_word is None:
        return candidate
    return TimeCandidate("clock", start, end, candidate.match, day_word)


def time_candidates(text: str) -> list[TimeCandidate]:
    """Every independent time expression, longest-first, never overlapping."""
    value = _normalized(text)
    found: list[TimeCandidate] = []
    for kind, pattern in (
        ("absolute", _ABSOLUTE),
        ("day", _DAY_CLOCK),
        ("weekday", _WEEKDAY_CLOCK),
        ("jalali", _JALALI_DATE),
        ("compound", _COMPOUND_RELATIVE),
        ("compound", _UNIT_FIRST_COMPOUND),
        ("relative", _RELATIVE),
        ("clock", _CLOCK_STRICT_ONLY),
        ("clock", _HOUR_AFTER_SAAT),
    ):
        for match in pattern.finditer(value):
            found.append(TimeCandidate(kind, match.start(), match.end(), match))
    found.sort(key=lambda item: (item.start, -(item.end - item.start)))
    chosen: list[TimeCandidate] = []
    for candidate in found:
        expanded = _absorbed(candidate, value)
        if any(not (expanded.end <= other.start or expanded.start >= other.end) for other in chosen):
            continue
        chosen.append(expanded)
    chosen.sort(key=lambda item: item.start)
    return chosen


def _has_explicit_affix(candidate: TimeCandidate) -> bool:
    groups = candidate.match.groupdict()
    return bool(groups.get("prefix") or groups.get("suffix"))


# --- resolution -------------------------------------------------------------

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


def _weekday_target(word: str) -> int:
    token = re.sub(r"\s+", " ", _normalized(word).strip().lower())
    token = token.replace("یک شنبه", "یک‌شنبه").replace("سه شنبه", "سه‌شنبه")
    token = token.replace("چهار شنبه", "چهارشنبه").replace("پنج شنبه", "پنج‌شنبه").replace("دو شنبه", "دوشنبه")
    return _WEEKDAY_INDEX[token]


def _resolve(candidate: TimeCandidate, current: datetime, zone: ZoneInfo) -> datetime:
    if candidate.kind == "relative":
        return current + timedelta(seconds=_relative_seconds(candidate.match))
    if candidate.kind == "compound":
        return current + timedelta(seconds=_compound_seconds(candidate.match.group("body")))
    if candidate.kind == "absolute":
        match = candidate.match
        local = datetime(int(match.group("year")), int(match.group("month")), int(match.group("day")),
                         int(match.group("hour")), int(match.group("minute")),
                         int(match.group("second") or 0))
        offset = match.group("offset")
        if offset:
            if offset.upper() == "Z":
                fixed = timezone.utc
            else:
                sign = -1 if offset[0] == "-" else 1
                hours, minutes = map(int, offset[1:].split(":"))
                if hours > 23 or minutes > 59:
                    raise ReminderError("اختلاف UTC معتبر نیست.", code="invalid_datetime")
                fixed = timezone(sign * timedelta(hours=hours, minutes=minutes))
            return local.replace(tzinfo=fixed).astimezone(timezone.utc)
        return _local_to_utc(local, zone)

    today = current.astimezone(zone).date()
    if candidate.kind == "jalali":
        match = candidate.match
        year = int(match.group("jyear")) if match.group("jyear") else _gregorian_to_jalali(today.year, today.month, today.day)[0]
        base = date(*_jalali_to_gregorian(year, _JALALI_MONTH_INDEX[match.group("jmonth")], int(match.group("jday"))))
    elif candidate.day_word:
        word = _normalized(candidate.day_word).strip().lower()
        if word in {"فردا", "tomorrow"}:
            base = today + timedelta(days=1)
        elif word in {"امروز", "today"}:
            base = today
        else:
            base = today + timedelta(days=(_weekday_target(candidate.day_word) - today.weekday()) % 7)
    elif candidate.kind == "weekday":
        base = today + timedelta(days=(_weekday_target(candidate.match.group("weekday")) - today.weekday()) % 7)
    elif candidate.kind == "day":
        word = _normalized(candidate.match.group("day")).strip().lower()
        base = today + timedelta(days=1 if word in {"فردا", "tomorrow"} else 0)
    else:
        base = today

    hour, minute, second, meridiem = _clock_fields(candidate.match)
    hour, rollover = _apply_meridiem(hour, meridiem)
    local = datetime(base.year, base.month, base.day, hour, minute, second) + timedelta(days=rollover)
    due = _local_to_utc(local, zone)

    # A bare clock means the next time that clock is true.
    if candidate.kind == "clock" and due <= current + timedelta(seconds=5):
        local += timedelta(days=1)
        due = _local_to_utc(local, zone)
    # A weekday that already passed this week means the next one.
    if candidate.kind == "weekday" and due <= current + timedelta(seconds=5):
        local += timedelta(days=7)
        due = _local_to_utc(local, zone)
    return due


# --- public parsing ---------------------------------------------------------

def _message_without_spans(original: str, spans: list[tuple[int, int]]) -> str:
    chars = list(original)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    text = "".join(chars)
    text = re.sub(r"\s+", " ", text).strip(" \t\n،,:؛-–")
    # These are conversational glue only at the boundary, never inside the task.
    text = re.sub(
        r"^(?:(?:اگه\s*)?(?:میشه|می\s*شه)|می\s*تونی|لطفا|لطفاً|که|تا|برای\s+من|please|to)\s+",
        "", text, flags=re.IGNORECASE,
    ).strip()
    return text


def parse_reminder(text: str, zone_name: str, *, now: Optional[datetime] = None,
                   natural: bool = False) -> ReminderSpec:
    """Parse a conservative one-shot request without inventing dates or tasks."""
    original = str(text or "").strip()
    value = _normalized(original)
    zone = timezone_for(zone_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)

    trigger = _TRIGGER.search(value)
    if natural and not trigger:
        raise ReminderError("درخواست یادآوری مشخص نیست.", code="missing_intent")
    if _RECURRING.search(value):
        raise ReminderError("یادآور تکراری فعلاً پشتیبانی نمی‌شود؛ می‌توانی یک یادآور یک‌باره ثبت کنی.", code="recurring")
    if _UNSAFE_NUMBER.search(value):
        raise ReminderError("زمان را با عدد مثبت و واضح بگو؛ هنوز چیزی ثبت نکردم.", code="ambiguous_time")

    candidates = time_candidates(value)
    if not candidates:
        # A day without a clock («فردا صبح», «دوشنبه», «۲۸ آبان») is a time the
        # user thinks they gave. Ask for the clock instead of for the time.
        incomplete = (_DAY_CLOCK_ANYWHERE.search(value) or _WEEKDAY_ANYWHERE.search(value)
                      or _JALALI_ANYWHERE.search(value))
        if incomplete:
            raise ReminderError("چه ساعتی؟ مثلاً «۹ صبح» یا «۱۷:۳۰».", code="missing_clock",
                                fragment=original[incomplete.start():incomplete.end()].strip())
        raise ReminderError(
            "زمان یادآوری را دقیق بگو؛ مثلاً «۱۰ دقیقه دیگه»، «فردا ساعت ۹ صبح» یا «دوشنبه ۱۷:۰۰».",
            code="missing_time",
        )
    if len(candidates) > 1:
        raise ReminderError(
            "زمان یادآوری را دقیق بگو؛ مثلاً «۱۰ دقیقه دیگه یادم بنداز آب بخورم» "
            "یا /remind 2026-11-01 09:30 آب بخورم (تاریخ میلادی).",
            code="ambiguous_time",
        )
    candidate = candidates[0]
    if candidate.kind in {"relative", "compound"}:
        # Do not silently choose the final number of an approximate or
        # malformed expression like «یکی دو دقیقه» or «پنج یا ده دقیقه».
        before = value[:candidate.start].rstrip()
        if re.search(rf"(?<!\w)(?:{_WORD_NUMBER}|یا|حدود|تقریبا|تقریباً|شاید)\s*$", before):
            raise ReminderError("زمان تقریبی یا چندگزینه‌ای است؛ زمان دقیق را بگو.", code="ambiguous_time")
        if natural and not _has_explicit_affix(candidate):
            raise ReminderError("بگو چند دقیقه یا ساعت دیگر؛ مثلاً «۱۰ دقیقه دیگه یادم بنداز آب بخورم».",
                                code="missing_time")

    try:
        due = _resolve(candidate, current, zone)
    except ReminderError:
        raise
    except (ValueError, OverflowError):
        raise ReminderError(
            "تاریخ یا ساعت معتبر نیست؛ تاریخ را میلادی و به شکل YYYY-MM-DD HH:MM بفرست.",
            code="invalid_datetime",
        ) from None

    delta = (due - current).total_seconds()
    if delta < 5:
        raise ReminderError("این زمان گذشته است؛ زمان دیگری بفرست.", code="time_too_soon")
    if delta > MAX_FUTURE_SECONDS:
        raise ReminderError("زمان یادآوری بیش از پنج سال در آینده است.", code="time_too_far")

    spans = [(candidate.start, candidate.end)]
    if trigger:
        spans.append(trigger.span())
    content = _message_without_spans(original, spans)
    if not content:
        raise ReminderError("چه چیزی را یادآوری کنم؟ متن یادآوری را هم بفرست.", code="missing_task")
    if len(content) > MAX_TEXT_LENGTH:
        raise ReminderError(f"متن یادآوری خیلی طولانی است؛ حداکثر {MAX_TEXT_LENGTH} کاراکتر.", code="text_too_long")
    return ReminderSpec(due_utc=due, text=content, timezone=zone.key)


# Patterns that answer "did they name a day but forget the clock?".
_DAY_CLOCK_ANYWHERE = re.compile(
    rf"(?<!\w)(?:{_DAY_WORD})(?:\s+(?:صبح|بامداد|ظهر|عصر|شب))?(?![\w])", re.IGNORECASE,
)
_WEEKDAY_ANYWHERE = re.compile(rf"(?<!\w)(?:{_WEEKDAY_NAMES})(?!\w)", re.IGNORECASE)
_JALALI_ANYWHERE = re.compile(rf"(?<!\d)\d{{1,2}}\s*(?:{_JALALI_MONTH_PATTERN})", re.IGNORECASE)


def reminder_time_reply(text: str, *, allow_bare_hour: bool = False) -> Optional[str]:
    """Return a bare clarification time, adding an implied 'later' if needed.

    Only an entire time expression qualifies, so a pending question does not
    accidentally turn an unrelated conversation into a reminder.
    """
    value = str(text or "").strip().strip(" \t\n،,؛.!؟?")
    # Conversational wrappers are safe only when the remainder is exactly one
    # time; do not consume an unrelated sentence mentioning a duration.
    value = re.sub(r"^(?:(?:باشه|اوکی|همون|لطفا|لطفاً|به\s+نظرم)\s*[,،]?\s*)+", "", value, flags=re.I)
    value = re.sub(r"\s+(?:خوبه|مناسبه|باشه)$", "", value, flags=re.I).strip()
    normalized = _normalized(value)
    candidates = time_candidates(normalized)
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    # «ساعت ۹» alone is a guess; «فردا ساعت ۹» is not.
    if not allow_bare_hour and candidate.match.re is _HOUR_AFTER_SAAT and not candidate.day_word:
        return None
    normalized_trimmed = normalized.strip()
    offset = normalized.index(normalized_trimmed) if normalized_trimmed else 0
    if candidate.start != offset or candidate.end != offset + len(normalized_trimmed):
        return None
    if candidate.kind in {"relative", "compound"} and not _has_explicit_affix(candidate):
        return value + " دیگه"
    return value


def parse_reminder_time_expression(text: str, zone_name: str, *, now: Optional[datetime] = None,
                                   allow_implicit_relative: bool = False) -> datetime:
    """Validate a quoted time on its own, without misreading durations in a task."""
    value = str(text or "").strip()
    if allow_implicit_relative:
        value = reminder_time_reply(value, allow_bare_hour=True) or value
    marker = "__aura_reminder_task__"
    spec = parse_reminder(f"یادم بنداز {value} {marker}", zone_name, now=now, natural=True)
    if spec.text != marker:
        raise ReminderError("زمان را واضح‌تر بگو؛ چیزی ثبت نکردم.", code="ambiguous_time")
    return spec.due_utc


def explicit_reminder_time_phrases(text: str, *, include_implicit: bool = False) -> list[str]:
    """Extract time phrases for generated wording checks, never action parsing."""
    original, value = str(text or ""), _normalized(text)
    phrases = []
    for candidate in time_candidates(value):
        if candidate.kind in {"relative", "compound"} and not include_implicit and not _has_explicit_affix(candidate):
            continue
        phrases.append(original[candidate.start:candidate.end])
    return phrases


def explicit_reminder_time_count(text: str) -> int:
    return len(explicit_reminder_time_phrases(text, include_implicit=False))


def ambiguous_reminder_source(text: str) -> bool:
    """Reject competing explicit times and approximate/alternative durations."""
    value = _normalized(text)
    if _UNSAFE_NUMBER.search(value) or explicit_reminder_time_count(value) > 1:
        return True
    for match in _RELATIVE.finditer(value):
        try:
            _number_value(match.group("count"))
        except ReminderError:
            return True
        before = value[:match.start()].rstrip()
        if re.search(rf"(?<!\w)(?:{_WORD_NUMBER}|یا|حدود|تقریبا|تقریباً|شاید)\s*$", before):
            return True
    return False


def has_recurring_reminder(text: str) -> bool:
    return bool(_RECURRING.search(_normalized(text)))


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
    has_time = bool(time_candidates(value))
    if not has_time and re.search(
        r"(?:چیه|چیست|چی بود|چه بود|\b(?:چی|کی|کیم|کیه|کیستی|کجا|کجاست)\b|"
        r"چه کسی|\b(?:what|who|where)\b)",
        after, re.I,
    ):
        return False
    return True


# --- wording safety ---------------------------------------------------------

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


# --- presentation -----------------------------------------------------------

def format_due(timestamp: int, zone_name: str) -> str:
    local = datetime.fromtimestamp(int(timestamp), timezone.utc).astimezone(timezone_for(zone_name))
    return f"{local:%Y-%m-%d %H:%M:%S} ({zone_name})"


def format_due_human(timestamp: int, zone_name: str, lang: str = "fa") -> str:
    """A date a person can read, in their calendar and their numerals."""
    local = datetime.fromtimestamp(int(timestamp), timezone.utc).astimezone(timezone_for(zone_name))
    if str(lang or "fa").lower().startswith("en"):
        return f"{local:%A} {local.day} {local:%B %Y} at {local:%H:%M}"
    jalali_year, jalali_month, jalali_day = _gregorian_to_jalali(local.year, local.month, local.day)
    hour = local.hour
    if hour < 5:
        meridiem = "بامداد"
    elif hour < 12:
        meridiem = "صبح"
    elif hour == 12:
        meridiem = "ظهر"
    elif hour < 17:
        meridiem = "بعدازظهر"
    elif hour < 20:
        meridiem = "عصر"
    else:
        meridiem = "شب"
    clock = _fa_digits(f"{hour % 12 or 12}:{local.minute:02d}")
    return (
        f"{_FA_WEEKDAYS[local.weekday()]} {_fa_digits(str(jalali_day))} "
        f"{_JALALI_MONTHS[jalali_month - 1]} {_fa_digits(str(jalali_year))}، "
        f"ساعت {clock} {meridiem}"
    )
