"""Durable one-shot reminders, with explicit timezone and safe scheduling.

All due times are stored in UTC; the user's IANA timezone is retained to show
exactly how a reminder was scheduled. The worker makes at-least-once delivery
attempts: Telegram has no idempotency key, so a crash after Telegram accepted a
message but before SQLite recorded success can result in a duplicate.

Everything about *understanding* the request lives in `reminder_time`; this
module only stores, claims and delivers. Keeping the two apart is what lets the
grammar be tested as a table without a database or a bot.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import sqlite3
import threading
import time
from datetime import timedelta
from typing import Awaitable, Callable, Optional
from uuid import uuid4

from telegram.error import BadRequest, Forbidden, RetryAfter

import i18n
import migrations
from reminder_time import (
    DEFAULT_TIMEZONE,
    MAX_FUTURE_SECONDS,
    MAX_TEXT_LENGTH,
    ReminderError,
    ReminderSpec,
    ambiguous_reminder_source,
    explicit_reminder_time_count,
    explicit_reminder_time_phrases,
    format_due,
    format_due_human,
    grounded_reminder_task,
    has_recurring_reminder,
    is_reminder_request,
    parse_reminder,
    parse_reminder_time_expression,
    reminder_source_contains,
    reminder_time_reply,
    reminder_wording_matches_task,
    timezone_for,
)

logger = logging.getLogger("aura-reminders")
MAX_PENDING_PER_USER = 100

# Re-exported: callers and tests import the reminder language from here.
__all__ = [
    "DEFAULT_TIMEZONE",
    "MAX_FUTURE_SECONDS",
    "MAX_PENDING_PER_USER",
    "MAX_TEXT_LENGTH",
    "ReminderError",
    "ReminderScheduler",
    "ReminderSpec",
    "ReminderStore",
    "ambiguous_reminder_source",
    "explicit_reminder_time_count",
    "explicit_reminder_time_phrases",
    "format_due",
    "format_due_human",
    "grounded_reminder_task",
    "has_recurring_reminder",
    "is_reminder_request",
    "parse_reminder",
    "parse_reminder_time_expression",
    "reminder_source_contains",
    "reminder_time_reply",
    "reminder_wording_matches_task",
    "timezone_for",
]


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
            # Shared with the versioned migration of the same name, so an old
            # database is fixed here too when the store is used on its own.
            migrations.ensure_reminder_recipient_column(self.db)
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
