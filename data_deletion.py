"""Deleting everything Aura stores about one Telegram user.

Why a separate module
---------------------
Forgetting a single fact is ``/forget``; this is the opposite: a complete,
auditable wipe of the rows that belong to the person asking. Keeping the table
list in one place means the promise "here is what I delete" is literal code
rather than prose in a handler, and a test can assert that every personal table
is covered.

What is personal and what is not
--------------------------------
Rows are attributed by ``user_id`` in private chats and by chat ID in groups,
so deletion is only offered in a private chat: clearing a group would erase
other members' messages too, and a group's history cannot be split safely by
speaker. Shared, non-personal state (the web-search cache, settings, allowed
groups, the sticker cache) is deliberately left alone.

``plan()`` and ``delete()`` share the same category list, so the preview a user
confirms is exactly what runs.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("aura-deletion")


@dataclass(frozen=True)
class Category:
    """One personal table, and the column that ties a row to its owner."""

    key: str
    table: str
    column: str

    @property
    def label_key(self) -> str:
        return f"deletemydata.item.{self.key}"


#: Everything Aura stores per user. Adding a personal table means adding a line
#: here; the deletion test fails when a known table is missing.
CATEGORIES: tuple[Category, ...] = (
    Category("messages", "messages", "user_id"),
    Category("model", "conversations", "user_id"),
    Category("summary", "conversation_contexts", "conversation_id"),
    Category("video", "video_contexts", "conversation_id"),
    Category("memories", "memories", "owner_user_id"),
    Category("reminders", "reminders", "user_id"),
    Category("timezone", "reminder_timezones", "user_id"),
    Category("language", "user_language", "user_id"),
    Category("usage", "user_usage", "user_id"),
)


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def plan(db: sqlite3.Connection, user_id: int, *, lock: Optional[threading.Lock] = None) -> dict[str, int]:
    """Row counts per category for one user.

    A table that does not exist yet counts as zero rather than raising: an older
    database, or a module used on its own, must not make the command unusable.
    """
    guard = lock or _NULL_LOCK
    uid = int(user_id)
    counts: dict[str, int] = {}
    with guard:
        for category in CATEGORIES:
            if not _table_exists(db, category.table):
                counts[category.key] = 0
                continue
            row = db.execute(
                f"SELECT COUNT(*) FROM {category.table} WHERE {category.column} = ?", (uid,)
            ).fetchone()
            counts[category.key] = int(row[0]) if row else 0
    return counts


def delete(db: sqlite3.Connection, user_id: int, *, lock: Optional[threading.Lock] = None) -> dict[str, int]:
    """Delete every personal row for ``user_id`` and return what was removed.

    One transaction for all tables: a partial wipe would be worse than none,
    because the user would be told their data is gone while some of it stayed.
    """
    guard = lock or _NULL_LOCK
    uid = int(user_id)
    deleted: dict[str, int] = {}
    savepoint = "delete_user_data"
    with guard:
        # A caller that already holds a transaction keeps ownership of it: the
        # wipe runs in a savepoint it can decide about, and this function never
        # commits work it did not start.
        nested = db.in_transaction
        if nested:
            db.execute(f"SAVEPOINT {savepoint}")
        else:
            db.execute("BEGIN IMMEDIATE")
        try:
            for category in CATEGORIES:
                if not _table_exists(db, category.table):
                    deleted[category.key] = 0
                    continue
                cursor = db.execute(
                    f"DELETE FROM {category.table} WHERE {category.column} = ?", (uid,)
                )
                deleted[category.key] = int(cursor.rowcount or 0)
            if nested:
                db.execute(f"RELEASE {savepoint}")
            else:
                db.commit()
        except Exception:
            try:
                if nested:
                    db.execute(f"ROLLBACK TO {savepoint}")
                    db.execute(f"RELEASE {savepoint}")
                else:
                    db.rollback()
            except sqlite3.Error:
                logger.warning("Rollback of the deletion failed", exc_info=True)
            logger.exception("Data deletion failed | user=%s", uid)
            raise
    logger.info(
        "User data deleted | user=%s | %s",
        uid, ", ".join(f"{key}={value}" for key, value in deleted.items() if value),
    )
    return deleted


def total(counts: dict[str, int]) -> int:
    return sum(int(value) for value in counts.values())


class _NullLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc_info) -> None:
        return None


_NULL_LOCK = _NullLock()
