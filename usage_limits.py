"""Per-user, per-day usage accounting and quotas.

Why this exists
---------------
Aura is often deployed on a public link with one shared API key. Without a
per-user cap, one enthusiastic account can spend the whole day's budget before
anyone else gets an answer — and the operator only finds out from the bill.

The counter is deliberately small: one row per user per UTC day with the number
of model turns and the tokens reported by the provider. It is enough to answer
"how much is left?" and to refuse politely, and it costs one row rather than one
row per message.

Semantics
---------
* A limit of ``0`` means **unlimited**, for both turns and tokens. The default is
  unlimited, so upgrading does not silently start refusing messages.
* The window is the UTC calendar day. A deployment-wide cap that resets at local
  midnight would need a per-user timezone in the key; a single boundary keeps the
  counter unambiguous and is easy to explain ("resets at 00:00 UTC", shown in the
  user's own timezone).
* Only model turns are counted. Commands such as ``/usage`` or ``/deletemydata``
  never touch the counter, so a capped user can still see their usage and delete
  their data.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

USAGE_TABLE = "user_usage"


@dataclass(frozen=True)
class Usage:
    turns: int = 0
    tokens: int = 0


@dataclass(frozen=True)
class Decision:
    """The outcome of a quota check, ready to be rendered in any language."""

    allowed: bool
    limit: int = 0
    used: int = 0
    reason: str = ""  # "turns" | "tokens" when blocked

    @property
    def remaining(self) -> Optional[int]:
        if not self.limit:
            return None
        return max(0, self.limit - self.used)


@dataclass(frozen=True)
class Limits:
    daily_turns: int = 0
    daily_tokens: int = 0

    @property
    def unlimited(self) -> bool:
        return self.daily_turns <= 0 and self.daily_tokens <= 0

    @classmethod
    def from_setting(cls, getter: Callable[[str], int]) -> "Limits":
        """Read both limits defensively: a broken value must not break a turn."""
        def _read(key: str) -> int:
            try:
                return max(0, int(getter(key)))
            except Exception:
                return 0

        return cls(daily_turns=_read("user_daily_turns"), daily_tokens=_read("user_daily_tokens"))


def _utc_day(timestamp: float) -> str:
    return datetime.fromtimestamp(int(timestamp), timezone.utc).strftime("%Y-%m-%d")


def next_reset(timestamp: Optional[float] = None) -> int:
    """Epoch seconds of the next UTC midnight — when the counter starts over."""
    now = datetime.fromtimestamp(int(timestamp if timestamp is not None else time.time()), timezone.utc)
    tomorrow = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return int(tomorrow.timestamp()) + 86400


class UsageStore:
    """One row per user per UTC day; safe to call from the event loop thread."""

    def __init__(self, db: sqlite3.Connection, lock: Optional[threading.Lock] = None,
                 *, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.lock = lock or threading.RLock()
        self.clock = clock
        if self.db.row_factory is None:
            self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {USAGE_TABLE} (
                    user_id INTEGER NOT NULL,
                    usage_date TEXT NOT NULL,
                    turns INTEGER NOT NULL DEFAULT 0,
                    tokens INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (user_id, usage_date)
                )
                """
            )
            self.db.commit()

    # -- reads -------------------------------------------------------------
    def today(self, user_id: int) -> Usage:
        day = _utc_day(self.clock())
        with self.lock:
            row = self.db.execute(
                f"SELECT turns, tokens FROM {USAGE_TABLE} WHERE user_id = ? AND usage_date = ?",
                (int(user_id), day),
            ).fetchone()
        if not row:
            return Usage()
        return Usage(turns=int(row[0]), tokens=int(row[1]))

    def day(self, user_id: int) -> str:
        return _utc_day(self.clock())

    # -- writes ------------------------------------------------------------
    def record_turn(self, user_id: int, *, tokens: int = 0) -> Usage:
        """Count one model turn, and any tokens the provider reported for it."""
        now = int(self.clock())
        day = _utc_day(now)
        with self.lock:
            self.db.execute(
                f"""
                INSERT INTO {USAGE_TABLE} (user_id, usage_date, turns, tokens, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(user_id, usage_date) DO UPDATE SET
                    turns = turns + 1,
                    tokens = tokens + excluded.tokens,
                    updated_at = excluded.updated_at
                """,
                (int(user_id), day, max(0, int(tokens)), now),
            )
            self.db.commit()
        return self.today(user_id)

    def add_tokens(self, user_id: int, tokens: int) -> Usage:
        """Add tokens to today's row without counting another turn."""
        if not tokens or tokens < 0:
            return self.today(user_id)
        now = int(self.clock())
        with self.lock:
            self.db.execute(
                f"""
                INSERT INTO {USAGE_TABLE} (user_id, usage_date, turns, tokens, updated_at)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(user_id, usage_date) DO UPDATE SET
                    tokens = tokens + excluded.tokens,
                    updated_at = excluded.updated_at
                """,
                (int(user_id), _utc_day(now), int(tokens), now),
            )
            self.db.commit()
        return self.today(user_id)

    def reset(self, user_id: int) -> int:
        """Drop today's counter (used by /usage reset and account deletion)."""
        with self.lock:
            cursor = self.db.execute(
                f"DELETE FROM {USAGE_TABLE} WHERE user_id = ? AND usage_date = ?",
                (int(user_id), _utc_day(self.clock())),
            )
            self.db.commit()
        return int(cursor.rowcount or 0)

    def purge_history(self) -> int:
        """Forget counters older than the current day; called at startup."""
        with self.lock:
            cursor = self.db.execute(
                f"DELETE FROM {USAGE_TABLE} WHERE usage_date != ?", (_utc_day(self.clock()),)
            )
            self.db.commit()
        return int(cursor.rowcount or 0)


def evaluate(usage: Usage, limits: Limits) -> Decision:
    """Turns are checked first: they are the visible, cheap-to-explain limit."""
    if limits.daily_turns > 0 and usage.turns >= limits.daily_turns:
        return Decision(False, limits.daily_turns, usage.turns, "turns")
    if limits.daily_tokens > 0 and usage.tokens >= limits.daily_tokens:
        return Decision(False, limits.daily_tokens, usage.tokens, "tokens")
    # Allowed, but still describe the turn allowance so callers can show what is
    # left without re-reading the settings.
    return Decision(True, limits.daily_turns, usage.turns)
