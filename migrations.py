"""Versioned schema migrations for ``bot.db``.

Why this exists
---------------
Every table in Aura is created with ``CREATE TABLE IF NOT EXISTS`` by the module
that owns it, which makes a *fresh* database trivial and keeps each store usable
on its own. That is enough for the first release, but it cannot express a change
that must happen exactly once (``ALTER TABLE``, a backfill, a data fix): running
such a statement on every start is wrong, and running it never leaves old
databases behind.

This module adds the missing piece: an ordered registry of one-shot changes,
tracked by SQLite's own ``PRAGMA user_version`` counter, so a self-hosted
deployment upgrades its database in place instead of being told to delete it.

How it works
------------
* ``MIGRATIONS`` is an ordered tuple; each entry has a version, a name and an
  idempotent ``apply(connection)`` function.
* ``migrate()`` runs everything above the database's current version, in order,
  each inside the same transaction as its version bump. A failure rolls the
  whole step back and leaves the counter untouched, so a crash mid-upgrade never
  leaves a half-applied schema that pretends to be current.
* ``schema_migrations`` keeps a short history (version, name, timestamp) so an
  operator can see what changed in their database and when.
* A database written by a *newer* release is refused rather than silently
  downgraded: running old code against a new schema is how data gets corrupted.

Adding a migration
------------------
Append one entry with the next version number. Never edit or reorder an existing
entry: databases in the field have already run it. Make ``apply`` safe on both a
fresh database (the table may not exist yet) and an old one, and keep the
migration itself free of application imports so it stays runnable on its own.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

logger = logging.getLogger("aura-migrations")

#: Version of the schema this code expects. Equal to the last entry below.
LATEST_VERSION = 0  # replaced after MIGRATIONS is defined (keeps the two in sync)

HISTORY_TABLE = "schema_migrations"


class MigrationError(RuntimeError):
    """Raised when the database cannot be safely upgraded by this release."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


# --- individual steps -------------------------------------------------------

def ensure_reminder_recipient_column(db: sqlite3.Connection) -> None:
    """Add ``reminders.recipient_name`` to a database created before it existed.

    The column is part of the canonical table definition, so a fresh database
    already has it and this is a no-op. ReminderStore calls the same function so
    the store stays usable on its own, without this module.
    """
    tables = {
        row[0] if not isinstance(row, sqlite3.Row) else row["name"]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "reminders" not in tables:
        return
    columns = {
        row[1] if not isinstance(row, sqlite3.Row) else row["name"]
        for row in db.execute("PRAGMA table_info(reminders)")
    }
    if "recipient_name" not in columns:
        # Existing reminders keep their IDs and statuses and fall back to a
        # generic display name; the verified Telegram user_id was already stored.
        db.execute("ALTER TABLE reminders ADD COLUMN recipient_name TEXT NOT NULL DEFAULT ''")


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="baseline",
        apply=ensure_reminder_recipient_column,
    ),
)

LATEST_VERSION = MIGRATIONS[-1].version if MIGRATIONS else 0


# --- engine -----------------------------------------------------------------

def _pragma_version(db: sqlite3.Connection) -> int:
    row = db.execute("PRAGMA user_version").fetchone()
    value = row[0] if not isinstance(row, sqlite3.Row) else row["user_version"]
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def schema_version(db: sqlite3.Connection) -> int:
    """The schema version recorded in the database (0 means "pre-migrations")."""
    return _pragma_version(db)


def _ensure_history_table(db: sqlite3.Connection) -> None:
    db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at INTEGER NOT NULL
        )
        """
    )


def history(db: sqlite3.Connection) -> list[dict]:
    """Applied migrations, oldest first. Empty on a database that never migrated."""
    try:
        rows = db.execute(
            f"SELECT version, name, applied_at FROM {HISTORY_TABLE} ORDER BY version"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {"version": row[0], "name": row[1], "applied_at": row[2]}
        if not isinstance(row, sqlite3.Row)
        else {"version": row["version"], "name": row["name"], "applied_at": row["applied_at"]}
        for row in rows
    ]


def pending(db: sqlite3.Connection, migrations: Optional[Iterable[Migration]] = None) -> list[Migration]:
    """Migrations this release would still apply to ``db``."""
    available = tuple(migrations if migrations is not None else MIGRATIONS)
    current = _pragma_version(db)
    return [migration for migration in available if migration.version > current]


def _check_registry(migrations: tuple[Migration, ...]) -> None:
    versions = [migration.version for migration in migrations]
    if versions != sorted(versions) or len(versions) != len(set(versions)):
        raise MigrationError("migrations must be unique and ordered by version")
    if versions and versions[0] < 1:
        raise MigrationError("migration versions start at 1; 0 means 'never migrated'")


def migrate(
    db: sqlite3.Connection,
    lock: Optional[threading.Lock] = None,
    *,
    migrations: Optional[Iterable[Migration]] = None,
    clock: Callable[[], float] = time.time,
) -> list[Migration]:
    """Apply pending migrations in order and return the ones that ran.

    Each step commits together with its version bump, so the counter never
    claims a step that did not finish. Calling this twice is a no-op.
    """
    available = tuple(migrations if migrations is not None else MIGRATIONS)
    _check_registry(available)
    latest = available[-1].version if available else 0

    guard = lock or _NULL_LOCK
    applied: list[Migration] = []
    with guard:
        current = _pragma_version(db)
        if current > latest:
            raise MigrationError(
                f"database schema version {current} is newer than this release supports ({latest}); "
                "upgrade Aura instead of running older code against a newer database"
            )
        if current == latest:
            return applied
        for migration in available:
            if migration.version <= current:
                continue
            logger.info("Applying migration %s: %s", migration.version, migration.name)
            # Python's sqlite3 opens a transaction for DML but NOT for DDL, so an
            # ALTER would commit itself the moment it runs. The step is therefore
            # wrapped in an explicit transaction: the change, the version bump and
            # the history row commit together, or not at all.
            savepoint = f"migration_{int(migration.version)}"
            # A caller that already holds a transaction keeps it: the step runs in
            # a savepoint it can decide about, and migrate() never commits work it
            # did not start.
            nested = db.in_transaction
            if nested:
                db.execute(f"SAVEPOINT {savepoint}")
            else:
                db.execute("BEGIN IMMEDIATE")
            try:
                _ensure_history_table(db)
                migration.apply(db)
                # The counter lives in the database header but is written inside
                # the transaction, so a failed step cannot bump it.
                db.execute(f"PRAGMA user_version = {int(migration.version)}")
                db.execute(
                    f"INSERT OR REPLACE INTO {HISTORY_TABLE}(version, name, applied_at) VALUES(?, ?, ?)",
                    (migration.version, migration.name, int(clock())),
                )
                if nested:
                    db.execute(f"RELEASE {savepoint}")
                else:
                    db.commit()
            except MigrationError:
                _abort(db, savepoint, nested)
                raise
            except Exception as exc:  # noqa: BLE001 - re-raised with context
                _abort(db, savepoint, nested)
                raise MigrationError(
                    f"migration {migration.version} ({migration.name}) failed: {exc}"
                ) from exc
            applied.append(migration)
    return applied


def _abort(db: sqlite3.Connection, savepoint: str, nested: bool) -> None:
    """Undo a failed step without touching a transaction the caller started."""
    try:
        if nested:
            db.execute(f"ROLLBACK TO {savepoint}")
            db.execute(f"RELEASE {savepoint}")
        else:
            db.rollback()
    except sqlite3.Error:  # the failure may already have rolled everything back
        logger.warning("Rollback of %s failed", savepoint, exc_info=True)


class _NullLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc_info) -> None:
        return None


_NULL_LOCK = _NullLock()


def describe(db: sqlite3.Connection) -> str:
    """One-line status for logs and health output."""
    version = _pragma_version(db)
    state = "current" if version == LATEST_VERSION else (
        f"{LATEST_VERSION - version} pending" if version < LATEST_VERSION else "newer than this release"
    )
    return f"schema v{version}/{LATEST_VERSION} ({state})"
