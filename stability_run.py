#!/usr/bin/env python3
"""Offline final stability runner for Aura's development package.

Runs the complete pytest suite repeatedly and performs a Python compile pass.
It never imports bot.py, touches .env/bot.db, or contacts Telegram/Gemini.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

FORBIDDEN = {".env", "bot.db", "bot.db-shm", "bot.db-wal"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 10:
        raise SystemExit("--rounds must be between 1 and 10")

    found = sorted(p.name for p in Path(".").iterdir() if p.name in FORBIDDEN)
    if found:
        print(f"FAIL: forbidden files present: {', '.join(found)}")
        return 2

    python_files = sorted(Path(".").glob("*.py"))
    compile_result = subprocess.run(
        [sys.executable, "-m", "py_compile", *map(str, python_files)],
        check=False,
    )
    if compile_result.returncode:
        print("FAIL: py_compile")
        return compile_result.returncode

    for round_no in range(1, args.rounds + 1):
        print(f"STABILITY ROUND {round_no}/{args.rounds}")
        result = subprocess.run([sys.executable, "-m", "pytest", "-q"], check=False)
        if result.returncode:
            print(f"FAIL: pytest round {round_no}")
            return result.returncode

    print(f"PASS: {args.rounds} complete pytest rounds + py_compile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
