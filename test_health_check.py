import os
import sqlite3
from pathlib import Path

from health_check import (
    check_database,
    check_process_identity,
    overall_health,
    run_local_health_checks,
)


def test_database_check_accepts_valid_sqlite(tmp_path):
    db = tmp_path / "test.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table t (id integer)")
        conn.commit()
    result = check_database(str(db))
    assert result.ok is True
    assert result.component == "sqlite"


def test_database_check_rejects_missing_db(tmp_path):
    result = check_database(str(tmp_path / "missing.db"))
    assert result.ok is False


def test_process_identity_uses_expected_script(tmp_path):
    proc = tmp_path / "cmdline"
    proc.write_bytes(b"/opt/python/bin/python\\0/home/auracom/aura/bot.py\\0")
    result = check_process_identity("bot.py", str(proc))
    assert result.ok is True


def test_process_identity_rejects_wrong_script():
    result = check_process_identity("/definitely/not/aura/bot.py")
    assert result.ok is False


def test_overall_health_requires_all_checks():
    from health_check import HealthResult
    assert overall_health([HealthResult(True, "a", "ok")]) is True
    assert overall_health([HealthResult(True, "a", "ok"), HealthResult(False, "b", "bad")]) is False
    assert overall_health([]) is False


def test_run_local_health_checks_returns_expected_components(tmp_path):
    db = tmp_path / "db.sqlite"
    sqlite3.connect(db).close()
    results = run_local_health_checks(str(db), "/proc/self/cmdline")
    assert [r.component for r in results] == ["process", "sqlite"]


def test_database_check_reports_the_schema_version(tmp_path):
    import migrations

    db = tmp_path / "test.db"
    with sqlite3.connect(db) as conn:
        migrations.migrate(conn)
    result = check_database(str(db))
    assert result.ok is True
    assert f"schema v{migrations.LATEST_VERSION}/{migrations.LATEST_VERSION} (current)" in result.detail


def test_database_check_flags_a_pending_migration(tmp_path):
    import migrations

    db = tmp_path / "test.db"
    sqlite3.connect(db).close()  # a database that never ran a migration
    result = check_database(str(db))
    assert "pending" in result.detail
    # Pending is not fatal: the next start applies them.
    assert result.ok is True


def test_database_check_fails_a_database_from_a_newer_release(tmp_path):
    import migrations

    db = tmp_path / "test.db"
    with sqlite3.connect(db) as conn:
        conn.execute(f"PRAGMA user_version = {migrations.LATEST_VERSION + 1}")
        conn.commit()
    result = check_database(str(db))
    assert result.ok is False
    assert "newer than this release" in result.detail
