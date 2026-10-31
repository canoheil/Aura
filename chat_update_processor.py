"""Bounded Telegram update concurrency with per-chat ordering."""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable

from telegram.ext import BaseUpdateProcessor


@dataclass
class _ChatLock:
    lock: asyncio.Lock
    users: int = 0


class ChatUpdateProcessor(BaseUpdateProcessor):
    """Allow different chats to progress while serialising a chat's updates.

    Commands use the same lock as messages, so e.g. /new cannot race a pending
    reply. Locks are removed when the last holder/waiter exits; the global limit
    is enforced by python-telegram-bot's BaseUpdateProcessor.
    """

    def __init__(self, max_concurrent_updates: int = 4):
        super().__init__(max_concurrent_updates)
        self._guard = asyncio.Lock()
        self._locks: dict[tuple[str, int], _ChatLock] = {}

    @staticmethod
    def _key(update: object) -> tuple[str, int] | None:
        chat = getattr(update, "effective_chat", None)
        chat_id = getattr(chat, "id", None)
        if isinstance(chat_id, int):
            return ("chat", chat_id)
        user = getattr(update, "effective_user", None)
        user_id = getattr(user, "id", None)
        return ("user", user_id) if isinstance(user_id, int) else None

    async def do_process_update(self, update: object, coroutine: Awaitable[Any]) -> None:
        key = self._key(update)
        if key is None:
            await coroutine
            return
        entry = None
        started = False
        try:
            async with self._guard:
                entry = self._locks.setdefault(key, _ChatLock(asyncio.Lock()))
                entry.users += 1
            async with entry.lock:
                started = True
                await coroutine
        finally:
            # The coroutine supplied by PTB is already created even when a
            # waiting update is cancelled before entering the per-chat lock.
            if not started and inspect.iscoroutine(coroutine):
                coroutine.close()
            if entry is not None:
                async with self._guard:
                    entry.users -= 1
                    if not entry.users:
                        self._locks.pop(key, None)

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass
