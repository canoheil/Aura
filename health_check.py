"""Aura health checks.

Stage 16 provides lightweight, local health probes.  These checks deliberately
avoid changing Telegram/Gemini state and are safe to run from a cron job or
manually.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    component: str
    detail: str
    latency_ms: Optional[float] = None


def check_process_identity(expected_script: str, cmdline_path: str = "/proc/self/cmdline") -> HealthResult:
    """Confirm this process is the expected Aura bot process."""
    try:
        cmdline = Path(cmdline_path).read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        )
    except (OSError, FileNotFoundError):
        return HealthResult(False, "process", "Unable to inspect /proc/self/cmdline")

    ok = expected_script in cmdline
    return HealthResult(
        ok,
        "process",
        "Aura process identity confirmed" if ok else "Unexpected process identity",
    )


def check_database(db_path: str) -> HealthResult:
    """Run a read-only SQLite integrity probe."""
    start = time.monotonic()
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0) as conn:
            value = conn.execute("PRAGMA quick_check").fetchone()
        latency = (time.monotonic() - start) * 1000
        ok = bool(value and value[0] == "ok")
        return HealthResult(
            ok,
            "sqlite",
            "SQLite quick_check=ok" if ok else f"SQLite quick_check={value!r}",
            latency,
        )
    except Exception as exc:
        latency = (time.monotonic() - start) * 1000
        return HealthResult(False, "sqlite", f"{type(exc).__name__}: {exc}", latency)


def run_local_health_checks(db_path: str, expected_script: str) -> list[HealthResult]:
    return [
        check_process_identity(expected_script),
        check_database(db_path),
    ]


def overall_health(results: list[HealthResult]) -> bool:
    return bool(results) and all(result.ok for result in results)
