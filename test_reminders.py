"""Durable reminder and scheduler tests; no real Telegram connection."""
import asyncio
from datetime import datetime, timedelta, timezone
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from reminders import (
    DEFAULT_TIMEZONE, ReminderError, ReminderScheduler, ReminderSpec, ReminderStore,
    format_due, is_reminder_request, parse_reminder,
)

NOW = datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("text,due,content", [
    ("۱۰ دقیقه دیگه یادم بنداز آب بخورم", NOW + timedelta(minutes=10), "آب بخورم"),
    ("یادم بنداز فردا ساعت ۹:۳۰ قرص بخورم", datetime(2026, 11, 1, 6, tzinfo=timezone.utc), "قرص بخورم"),
    ("remind me in 5m call Sam", NOW + timedelta(minutes=5), "call Sam"),
])
def test_natural_requests_parse_exact_time_and_original_text(text, due, content):
    assert is_reminder_request(text)
    spec = parse_reminder(text, DEFAULT_TIMEZONE, now=NOW, natural=True)
    assert spec.due_utc == due
    assert spec.text == content
    assert spec.timezone == DEFAULT_TIMEZONE


def test_commands_support_gregorian_relative_and_explicit_offsets():
    spec = parse_reminder("2026-11-01 09:30 ۲ قرص", "Asia/Tehran", now=NOW)
    assert spec.due_utc == datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
    assert spec.text == "۲ قرص"
    assert parse_reminder("in 10m water", "Europe/Berlin", now=NOW).due_utc == NOW + timedelta(minutes=10)
    explicit = parse_reminder("2026-11-01 09:30Z water", "Asia/Tehran", now=NOW)
    assert explicit.due_utc == datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("text,natural", [
    ("یادم بنداز", True),
    ("یادم بنداز ۲ ساعت ورزش کنم", True),
    ("هر روز ساعت ۹ یادم بنداز آب بخورم", True),
    ("10m", False),
    ("2026-02-31 09:00 invalid", False),
    ("2026-10-01 09:00 too late", False),
    ("10m 20m uncertain", False),
])
def test_invalid_or_ambiguous_requests_are_not_scheduled(text, natural):
    with pytest.raises(ReminderError):
        parse_reminder(text, "Asia/Tehran", now=NOW, natural=natural)


def test_dst_gap_and_fold_rejected_and_explicit_offset_works():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ReminderError, match="وجود ندارد"):
        parse_reminder("2026-03-29 02:30 task", "Europe/Berlin", now=now)
    with pytest.raises(ReminderError, match="دو بار"):
        parse_reminder("2026-10-25 02:30 task", "Europe/Berlin", now=now)
    summer = parse_reminder("2026-10-25 02:30+02:00 task", "Europe/Berlin", now=now)
    winter = parse_reminder("2026-10-25 02:30+01:00 task", "Europe/Berlin", now=now)
    assert winter.due_utc - summer.due_utc == timedelta(hours=1)


def test_zone_validation_and_utc_display():
    with pytest.raises(ReminderError):
        parse_reminder("10m water", "Not/AZone", now=NOW)
    assert "Asia/Tehran" in format_due(int(NOW.timestamp()), "Asia/Tehran")
    assert "03:30:00" in format_due(int(NOW.timestamp()), "Asia/Tehran")


@pytest.fixture
def store(tmp_path):
    clock = [NOW.timestamp()]
    path = tmp_path / "reminders.db"
    db = sqlite3.connect(path, check_same_thread=False)
    result = ReminderStore(db, threading.Lock(), clock=lambda: clock[0])
    yield result, clock, path
    db.close()


def _create(store, *, chat=1, user=2, seconds=10, text="آب بنوش"):
    spec = ReminderSpec(datetime.fromtimestamp(store.clock() + seconds, timezone.utc), text, "Asia/Tehran")
    return store.create(chat, user, spec)


def test_persistence_timezone_list_and_cancel_are_owner_and_chat_scoped(store):
    s, clock, path = store
    assert s.get_timezone(2) == "Asia/Tehran"
    s.set_timezone(2, "Europe/Berlin")
    rid = _create(s)
    assert not s.cancel(1, 999, rid)
    assert not s.cancel(999, 2, rid)
    assert s.list_active(1, 999) == []
    db2 = sqlite3.connect(path)
    restored = ReminderStore(db2, threading.Lock(), clock=lambda: clock[0])
    assert restored.get_timezone(2) == "Europe/Berlin"
    assert restored.list_active(1, 2)[0]["id"] == rid
    assert restored.list_active(1, 2)[0]["timezone"] == "Asia/Tehran"  # changing zone does not shift it
    assert restored.cancel(1, 2, rid)
    assert restored.list_active(1, 2) == []
    db2.close()


def test_store_rejects_naive_datetimes(store):
    s, _, _ = store
    with pytest.raises(ReminderError, match="منطقهٔ زمانی"):
        s.create(1, 2, ReminderSpec(datetime(2030, 1, 1, 9, 0), "task", "Asia/Tehran"))


def test_worker_retries_failed_startup_recovery(store, monkeypatch):
    async def run():
        s, _, _ = store
        recovered = asyncio.Event()
        calls = []
        original = s.recover_inflight

        def flaky_recovery():
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database temporarily unavailable")
            result = original()
            recovered.set()
            return result

        monkeypatch.setattr(s, "recover_inflight", flaky_recovery)
        worker = ReminderScheduler(s, SimpleNamespace(send_message=AsyncMock()))
        worker.poll_seconds = 0.01
        task = asyncio.create_task(worker.run())
        await asyncio.wait_for(recovered.wait(), 1)
        assert len(calls) == 2 and not task.done()
        worker.stop()
        await asyncio.wait_for(task, 1)

    asyncio.run(run())


def test_scheduler_survives_restart_and_sends_due_once(store):
    s, clock, path = store
    rid = _create(s)
    clock[0] += 11
    interrupted = s.claim_due()
    assert interrupted["id"] == rid
    assert not s.cancel(1, 2, rid)  # do not claim a send already in progress was cancelled
    db2 = sqlite3.connect(path)
    restored = ReminderStore(db2, threading.Lock(), clock=lambda: clock[0])
    assert restored.recover_inflight() == 1
    fake_bot = SimpleNamespace(send_message=AsyncMock())
    worker = ReminderScheduler(restored, fake_bot)
    assert asyncio.run(worker.run_once()) is True
    assert asyncio.run(worker.run_once()) is False
    fake_bot.send_message.assert_awaited_once()
    assert fake_bot.send_message.await_args.kwargs["chat_id"] == 1
    assert "آب بنوش" in fake_bot.send_message.await_args.kwargs["text"]
    assert restored.list_active(1, 2) == []
    db2.close()


@pytest.mark.parametrize("error,delay", [(RetryAfter(12), 13), (NetworkError("down"), 30)])
def test_temporary_delivery_failures_retry_after_delay(store, error, delay):
    s, clock, _ = store
    _create(s)
    clock[0] += 11
    fake_bot = SimpleNamespace(send_message=AsyncMock(side_effect=[error, None]))
    worker = ReminderScheduler(s, fake_bot)
    assert asyncio.run(worker.run_once())
    assert s.list_active(1, 2)[0]["status"] == "pending"
    assert not asyncio.run(worker.run_once())
    clock[0] += delay
    assert asyncio.run(worker.run_once())
    assert s.list_active(1, 2) == []
    assert fake_bot.send_message.await_count == 2


@pytest.mark.parametrize("error", [Forbidden("blocked"), BadRequest("invalid chat")])
def test_permanent_delivery_error_is_visible_and_cancellable(store, error):
    s, clock, _ = store
    rid = _create(s)
    clock[0] += 11
    worker = ReminderScheduler(s, SimpleNamespace(send_message=AsyncMock(side_effect=error)))
    assert asyncio.run(worker.run_once())
    assert s.list_active(1, 2)[0]["status"] == "failed"
    assert s.cancel(1, 2, rid)


def test_disallowed_group_is_not_sent_and_is_retried(store):
    s, clock, _ = store
    _create(s, chat=-100)
    clock[0] += 11
    fake_bot = SimpleNamespace(send_message=AsyncMock())
    worker = ReminderScheduler(s, fake_bot, can_send=lambda _chat: False)
    assert asyncio.run(worker.run_once())
    fake_bot.send_message.assert_not_awaited()
    assert s.list_active(-100, 2)[0]["last_error"] == "chat_not_allowed"


def test_cancelled_send_is_requeued(store):
    async def run():
        s, clock, _ = store
        _create(s)
        clock[0] += 11
        entered = asyncio.Event()
        async def send_message(**_):
            entered.set()
            await asyncio.Event().wait()
        worker = ReminderScheduler(s, SimpleNamespace(send_message=send_message))
        task = asyncio.create_task(worker.run_once())
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert s.list_active(1, 2)[0]["status"] == "pending"
    asyncio.run(run())
