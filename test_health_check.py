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
