"""Versioned schema migrations: idempotence, upgrades and safe failure."""
import sqlite3

import pytest

import migrations
from migrations import Migration, MigrationError, migrate, schema_version


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    return connection


def _columns(db, table):
    return [row["name"] for row in db.execute(f"PRAGMA table_info({table})")]


_LEGACY_REMINDERS = """
    CREATE TABLE reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, text TEXT NOT NULL,
        due_utc INTEGER NOT NULL, timezone TEXT NOT NULL, created_at INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', next_attempt INTEGER NOT NULL,
        lease_until INTEGER NOT NULL DEFAULT 0, claim_token TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, delivered_at INTEGER,
        last_error TEXT NOT NULL DEFAULT ''{extra}
    )
"""


def _legacy_reminders_db(*, with_column=False) -> sqlite3.Connection:
    """A database written by a release that predates the recipient_name column."""
    db = _db()
    extra = ", recipient_name TEXT NOT NULL DEFAULT ''" if with_column else ""
    db.execute(_LEGACY_REMINDERS.format(extra=extra))
    db.execute(
        "INSERT INTO reminders (chat_id, user_id, text, due_utc, timezone, created_at, next_attempt)"
        " VALUES (7, 7, 'drink water', 0, 'Asia/Tehran', 0, 0)"
    )
    db.commit()
    return db


# --- the registry -----------------------------------------------------------

def test_the_registry_is_ordered_and_starts_at_one():
    versions = [migration.version for migration in migrations.MIGRATIONS]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert versions[0] == 1
    assert migrations.LATEST_VERSION == versions[-1]


def test_every_migration_is_named_and_callable():
    for migration in migrations.MIGRATIONS:
        assert migration.name.strip(), migration.version
        assert callable(migration.apply)


# --- a fresh database -------------------------------------------------------

def test_a_fresh_database_is_stamped_to_the_latest_version():
    db = _db()
    assert schema_version(db) == 0
    applied = migrate(db)
    assert [migration.version for migration in applied] == [
        migration.version for migration in migrations.MIGRATIONS
    ]
    assert schema_version(db) == migrations.LATEST_VERSION


def test_the_history_table_records_what_ran():
    db = _db()
    migrate(db)
    rows = migrations.history(db)
    assert [row["version"] for row in rows] == [m.version for m in migrations.MIGRATIONS]
    assert {row["name"] for row in rows} == {m.name for m in migrations.MIGRATIONS}
    assert all(isinstance(row["applied_at"], int) and row["applied_at"] > 0 for row in rows)


def test_history_is_empty_before_anything_ran():
    assert migrations.history(_db()) == []


def test_running_twice_changes_nothing():
    db = _db()
    migrate(db)
    version = schema_version(db)
    history = migrations.history(db)
    assert migrate(db) == []
    assert schema_version(db) == version
    assert migrations.history(db) == history


def test_pending_lists_only_newer_migrations():
    db = _db()
    assert len(migrations.pending(db)) == len(migrations.MIGRATIONS)
    migrate(db)
    assert migrations.pending(db) == []


# --- upgrading a database that predates migrations ---------------------------

def test_an_old_database_is_upgraded_in_place():
    db = _legacy_reminders_db()
    assert "recipient_name" not in _columns(db, "reminders")
    assert schema_version(db) == 0

    migrate(db)

    assert "recipient_name" in _columns(db, "reminders")
    assert schema_version(db) == migrations.LATEST_VERSION
    # Existing rows survive and get the documented default.
    row = db.execute("SELECT chat_id, recipient_name FROM reminders").fetchone()
    assert (row["chat_id"], row["recipient_name"]) == (7, "")


def test_a_database_that_already_has_the_column_is_not_touched():
    db = _legacy_reminders_db(with_column=True)
    migrate(db)
    assert _columns(db, "reminders").count("recipient_name") == 1


def test_a_missing_table_is_not_an_error():
    """A fresh database gets its tables later, from the module that owns them."""
    db = _db()
    migrations.ensure_reminder_recipient_column(db)  # must not raise
    migrate(db)
    assert schema_version(db) == migrations.LATEST_VERSION


def test_the_column_guard_is_idempotent():
    db = _legacy_reminders_db()
    migrations.ensure_reminder_recipient_column(db)
    migrations.ensure_reminder_recipient_column(db)
    assert _columns(db, "reminders").count("recipient_name") == 1


# --- refusing unsafe states -------------------------------------------------

def test_a_newer_database_is_refused():
    db = _db()
    db.execute(f"PRAGMA user_version = {migrations.LATEST_VERSION + 5}")
    db.commit()
    with pytest.raises(MigrationError) as excinfo:
        migrate(db)
    assert "newer than this release" in str(excinfo.value)
    # Nothing was changed on the way out.
    assert schema_version(db) == migrations.LATEST_VERSION + 5


def test_an_out_of_order_registry_is_rejected(monkeypatch):
    broken = (
        Migration(version=2, name="second", apply=lambda db: None),
        Migration(version=1, name="first", apply=lambda db: None),
    )
    with pytest.raises(MigrationError) as excinfo:
        migrate(_db(), migrations=broken)
    assert "ordered" in str(excinfo.value)


def test_version_zero_is_reserved():
    broken = (Migration(version=0, name="zero", apply=lambda db: None),)
    with pytest.raises(MigrationError):
        migrate(_db(), migrations=broken)


# --- failing safely ---------------------------------------------------------

def test_a_failing_step_rolls_back_completely():
    def boom(db):
        db.execute("ALTER TABLE t ADD COLUMN partial TEXT")
        raise RuntimeError("mid-step failure")

    db = _db()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    db.commit()
    registry = (*migrations.MIGRATIONS, Migration(version=migrations.LATEST_VERSION + 50, name="boom", apply=boom))

    with pytest.raises(MigrationError) as excinfo:
        migrate(db, migrations=registry)
    assert "boom" in str(excinfo.value) and "mid-step failure" in str(excinfo.value)
    # The DDL was undone and the counter still points at the last good version.
    assert "partial" not in _columns(db, "t")
    assert schema_version(db) == migrations.LATEST_VERSION


def test_a_failed_step_can_be_retried():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    db.commit()

    def boom(_db):
        raise RuntimeError("transient")

    with pytest.raises(MigrationError):
        migrate(db, migrations=(*migrations.MIGRATIONS, Migration(version=migrations.LATEST_VERSION + 50, name="boom", apply=boom)))

    fixed = Migration(version=migrations.LATEST_VERSION + 50, name="fixed",
                      apply=lambda conn: conn.execute("ALTER TABLE t ADD COLUMN ok TEXT"))
    applied = migrate(db, migrations=(*migrations.MIGRATIONS, fixed))
    assert [(m.version, m.name) for m in applied] == [(fixed.version, "fixed")]
    assert "ok" in _columns(db, "t")


def test_a_callers_transaction_is_never_committed_behind_its_back():
    """Inside an open transaction the step runs in a savepoint, not a commit."""
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    db.commit()
    db.execute("INSERT INTO t (id) VALUES (1)")  # opens an implicit transaction
    assert db.in_transaction is True

    applied = migrate(db, migrations=migrations.MIGRATIONS)
    assert [m.version for m in applied] == [m.version for m in migrations.MIGRATIONS]
    # The caller still owns the transaction, and the migration is visible in it.
    assert db.in_transaction is True
    assert schema_version(db) == migrations.LATEST_VERSION

    # If the caller rolls back, the migration goes with it — no half a schema.
    db.rollback()
    assert schema_version(db) == 0
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_a_failed_step_inside_a_callers_transaction_keeps_the_outer_work():
    db = _db()
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    db.commit()
    db.execute("INSERT INTO t (id) VALUES (1)")

    def boom(connection):
        connection.execute("ALTER TABLE t ADD COLUMN partial TEXT")
        raise RuntimeError("nope")

    with pytest.raises(MigrationError):
        migrate(db, migrations=(*migrations.MIGRATIONS, Migration(version=migrations.LATEST_VERSION + 51, name="boom", apply=boom)))
    # Only the failed savepoint was undone; the caller's insert is still there.
    assert db.in_transaction is True
    assert db.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    assert "partial" not in _columns(db, "t")
    db.rollback()


# --- status -----------------------------------------------------------------

def test_describe_reports_pending_and_current_states():
    db = _db()
    assert "pending" in migrations.describe(db)
    migrate(db)
    text = migrations.describe(db)
    assert text == f"schema v{migrations.LATEST_VERSION}/{migrations.LATEST_VERSION} (current)"


# --- the application --------------------------------------------------------

def test_the_bot_stamps_its_database_on_startup(isolated_bot):
    bot, _ = isolated_bot
    assert schema_version(bot.db) == migrations.LATEST_VERSION
    assert migrations.pending(bot.db) == []
    assert "recipient_name" in _columns(bot.db, "reminders")


def test_the_reminder_store_works_without_this_module():
    """The store is usable on its own: it shares the column guard, not the engine."""
    import threading

    from reminders import ReminderStore

    db = _legacy_reminders_db()
    ReminderStore(db, threading.RLock())
    assert _columns(db, "reminders").count("recipient_name") == 1
    # Building it did not stamp a version: only migrate() owns that counter.
    assert schema_version(db) == 0
