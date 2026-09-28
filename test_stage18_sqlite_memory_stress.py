import gc
import sqlite3
import threading
import tracemalloc
from pathlib import Path

from reaction_manager import ReactionManager


def test_sqlite_5000_row_batch_insert_and_reopen(tmp_path: Path):
    db = tmp_path / "stress.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT NOT NULL)")
        rows = [(i, f"message-{i}") for i in range(1, 5001)]
        with conn:
            conn.executemany("INSERT INTO messages(id, content) VALUES (?, ?)", rows)
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 5000
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT content FROM messages WHERE id=2500").fetchone()[0] == "message-2500"
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 5000


def test_sqlite_32_concurrent_read_only_connections(tmp_path: Path):
    db = tmp_path / "concurrent.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT NOT NULL)")
        conn.executemany("INSERT INTO messages(id, content) VALUES (?, ?)",
                         ((i, f"message-{i}") for i in range(1, 5001)))
        conn.commit()

    errors = []
    barrier = threading.Barrier(32)

    def reader():
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0) as conn:
                barrier.wait(timeout=5)
                count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                if count != 5000:
                    errors.append(count)
        except Exception as exc:  # pragma: no cover - assertion reports exact failure
            errors.append(repr(exc))

    threads = [threading.Thread(target=reader) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert all(not t.is_alive() for t in threads)
    assert errors == []


def test_sqlite_repeated_writes_preserve_integrity(tmp_path: Path):
    db = tmp_path / "writes.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE kv (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        for batch in range(20):
            start = batch * 250 + 1
            conn.executemany(
                "INSERT INTO kv(id, value) VALUES (?, ?)",
                ((i, f"v-{i}") for i in range(start, start + 250)),
            )
            conn.commit()
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM kv").fetchone()[0] == 5000


def test_reaction_manager_state_is_bounded_by_configured_limit():
    manager = ReactionManager(max_state_entries=128)
    for i in range(2000):
        manager.record_reaction(-100000 - i, 100000 + i)
    assert len(manager._last_reaction) == 128


def test_reaction_candidate_gate_does_not_create_state():
    manager = ReactionManager(max_state_entries=128)
    before = len(manager._last_reaction)
    for i in range(5000):
        assert manager.should_consider_ai(f"technical message {i}") is False
    assert len(manager._last_reaction) == before == 0


def test_reaction_state_memory_remains_bounded():
    gc.collect()
    manager = ReactionManager(max_state_entries=128)
    tracemalloc.start()
    for i in range(10000):
        manager.record_reaction(-200000 - i, 200000 + i)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(manager._last_reaction) == 128
    # Regression guard: bounded state should remain comfortably below a few MB.
    assert current < 2 * 1024 * 1024
    assert peak < 4 * 1024 * 1024
