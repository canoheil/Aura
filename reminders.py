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
import logging
import re
import sqlite3
import threading
import time
from typing import Callable, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram.error import BadRequest, Forbidden, RetryAfter

logger = logging.getLogger("aura-reminders")
DEFAULT_TIMEZONE = "Asia/Tehran"
MAX_TEXT_LENGTH = 500
MAX_PENDING_PER_USER = 100
MAX_FUTURE_SECONDS = 5 * 366 * 24 * 3600
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_TRIGGER = re.compile(
    r"(?:یادم\s*(?:بنداز|بینداز)|(?:به\s+من\s+)?یادآوری\s*(?:کن|بکن|بذار|بگذار|تنظیم\s*کن)|"
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
_RELATIVE = re.compile(
    r"(?<!\w)(?P<prefix>in\s+|after\s+|در\s+|تا\s+)?(?P<count>\d{1,5})\s*"
    r"(?P<unit>ثانیه|دقیقه|ساعت|روز|seconds?|minutes?|hours?|days?|s|m|h|d)"
    r"(?P<suffix>\s*(?:دیگه|دیگر|بعد|از\s+الان|later|from\s+now))?(?!\w)", re.IGNORECASE,
)


class ReminderError(ValueError):
    """Validation failure with a safe user-facing explanation."""


@dataclass(frozen=True)
class ReminderSpec:
    due_utc: datetime
    text: str
    timezone: str


def timezone_for(name: str) -> ZoneInfo:
    value = str(name or "").strip()
    if not value or len(value) > 100:
        raise ReminderError("منطقهٔ زمانی معتبر نیست؛ مثلاً Asia/Tehran یا Europe/Berlin بفرست.")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ReminderError("منطقهٔ زمانی پیدا نشد؛ از نام IANA مثل Asia/Tehran یا Europe/Berlin استفاده کن.") from None


def is_reminder_request(text: str) -> bool:
    normalized = str(text or "").translate(_DIGITS).replace("\u200c", " ")
    return bool(_TRIGGER.search(normalized))


def _local_to_utc(local: datetime, zone: ZoneInfo) -> datetime:
    """Reject missing/repeated local times rather than choosing a DST fold."""
    options = []
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == local and utc not in options:
            options.append(utc)
    if not options:
        raise ReminderError("این ساعت به‌خاطر تغییر ساعت در منطقهٔ زمانی انتخاب‌شده وجود ندارد؛ ساعت دیگری بفرست.")
    if len(options) > 1:
        raise ReminderError("این ساعت به‌خاطر تغییر ساعت دو بار تکرار می‌شود؛ ساعت دیگری یا تاریخ با اختلاف UTC صریح بفرست.")
    return options[0]


def _message_without_spans(original: str, spans: list[tuple[int, int]]) -> str:
    chars = list(original)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    text = "".join(chars)
    text = re.sub(r"\s+", " ", text).strip(" \t\n،,:؛-–")
    # These are conversational glue only at the boundary, never inside the task.
    text = re.sub(r"^(?:لطفا|لطفاً|که|تا|برای\s+من|please)\s+", "", text, flags=re.IGNORECASE).strip()
    return text


def parse_reminder(text: str, zone_name: str, *, now: Optional[datetime] = None, natural: bool = False) -> ReminderSpec:
    """Parse a conservative one-shot request without inventing dates or tasks.

    Supports Gregorian YYYY-MM-DD HH:MM, today/tomorrow + HH:MM, and a relative
    number of seconds/minutes/hours/days. Natural requests must explicitly say
    when (e.g. «۱۰ دقیقه دیگه»); unsupported or ambiguous input is rejected.
    """
    original = str(text or "").strip()
    normalized = original.translate(_DIGITS).replace("\u200c", " ")
    zone = timezone_for(zone_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    trigger = _TRIGGER.search(normalized)
    if natural and not trigger:
        raise ReminderError("درخواست یادآوری مشخص نیست.")
    if _RECURRING.search(normalized):
        raise ReminderError("یادآور تکراری فعلاً پشتیبانی نمی‌شود؛ می‌توانی یک یادآور یک‌باره ثبت کنی.")

    matches = [("absolute", m) for m in _ABSOLUTE.finditer(normalized)]
    matches += [("day", m) for m in _NAMED_DAY.finditer(normalized)]
    matches += [("relative", m) for m in _RELATIVE.finditer(normalized)]
    if len(matches) != 1:
        raise ReminderError(
            "زمان یادآوری را دقیق بگو؛ مثلاً «۱۰ دقیقه دیگه یادم بنداز آب بخورم» "
            "یا /remind 2026-11-01 09:30 آب بخورم (تاریخ میلادی)."
        )
    kind, match = matches[0]
    try:
        if kind == "relative":
            if natural and not (match.group("prefix") or match.group("suffix")):
                raise ReminderError("بگو چند دقیقه یا ساعت دیگر؛ مثلاً «۱۰ دقیقه دیگه یادم بنداز آب بخورم».")
            count = int(match.group("count"))
            unit = match.group("unit").lower()
            if unit in {"s", "second", "seconds", "ثانیه"}:
                seconds = count
            elif unit in {"m", "minute", "minutes", "دقیقه"}:
                seconds = count * 60
            elif unit in {"h", "hour", "hours", "ساعت"}:
                seconds = count * 3600
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
        raise ReminderError("تاریخ یا ساعت معتبر نیست؛ تاریخ را میلادی و به شکل YYYY-MM-DD HH:MM بفرست.") from None
    delta = (due - current).total_seconds()
    if delta < 5:
        raise ReminderError("زمان یادآوری باید دست‌کم چند ثانیه در آینده باشد.")
    if delta > MAX_FUTURE_SECONDS:
        raise ReminderError("زمان یادآوری بیش از پنج سال در آینده است.")
    spans = [match.span()]
    if trigger:
        spans.append(trigger.span())
    content = _message_without_spans(original, spans)
    if not content:
        raise ReminderError("چه چیزی را یادآوری کنم؟ متن یادآوری را هم بفرست.")
    if len(content) > MAX_TEXT_LENGTH:
        raise ReminderError(f"متن یادآوری خیلی طولانی است؛ حداکثر {MAX_TEXT_LENGTH} کاراکتر.")
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
                attempts INTEGER NOT NULL DEFAULT 0, delivered_at INTEGER, last_error TEXT NOT NULL DEFAULT ''
            )""")
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

    def create(self, chat_id: int, user_id: int, spec: ReminderSpec) -> int:
        now = int(self.clock())
        if spec.due_utc.tzinfo is None or spec.due_utc.utcoffset() is None:
            raise ReminderError("زمان یادآوری باید منطقهٔ زمانی مشخص داشته باشد.")
        due = int(spec.due_utc.timestamp())
        if due < now + 1 or due > now + MAX_FUTURE_SECONDS:
            raise ReminderError("زمان یادآوری باید در آینده و حداکثر پنج سال دیگر باشد.")
        timezone_for(spec.timezone)
        text = str(spec.text or "").strip()
        if not text or len(text) > MAX_TEXT_LENGTH:
            raise ReminderError("متن یادآوری خالی یا بیش از حد طولانی است.")
        with self.lock:
            count = self.db.execute(
                "SELECT COUNT(*) AS n FROM reminders WHERE user_id=? AND status IN ('pending', 'sending')", (user_id,),
            ).fetchone()["n"]
            if count >= MAX_PENDING_PER_USER:
                raise ReminderError("تعداد یادآورهای فعال به سقف مجاز رسیده است؛ یکی را لغو کن.")
            cur = self.db.execute("""INSERT INTO reminders
                (chat_id, user_id, text, due_utc, timezone, created_at, status, next_attempt)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""", (chat_id, user_id, text, due, spec.timezone, now, due))
            self.db.commit()
            return int(cur.lastrowid)

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
                 can_send: Optional[Callable[[int], bool]] = None):
        self.store, self.bot = store, bot
        self.poll_seconds = max(1.0, min(60.0, float(poll_seconds)))
        self.can_send = can_send
        self.stop_event = asyncio.Event()

    async def run_once(self) -> bool:
        row = self.store.claim_due()
        if not row:
            return False
        try:
            if self.can_send and not self.can_send(row["chat_id"]):
                self.store.retry(row, delay=300, error="chat_not_allowed")
                return True
            await self.bot.send_message(chat_id=row["chat_id"], text=f"⏰ یادآوری #{row['id']}:\n{row['text']}")
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
