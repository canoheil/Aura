"""Refuse to commit files that hold credentials or real user data.

`.gitignore` already lists `.env`, `bot.db` and `.aura_secret_key`, but the
failure mode this guards against is `git add -f` (or a file renamed past the
pattern): a production database with every user's messages, or a bot token,
becomes a permanent part of the public history.

The check is small on purpose — it looks at the staged path names only, never at
contents, so it runs in milliseconds and cannot leak anything. It is wired into
`.pre-commit-config.yaml` and can be run by hand:

    python repo_guard.py
"""

from __future__ import annotations

import subprocess
import sys
from typing import Iterable, Optional

# Paths that must never enter the repository, as exact names or suffixes.
FORBIDDEN_NAMES = {
    ".env",
    "bot.db",
    ".aura_secret_key",
}
FORBIDDEN_SUFFIXES = (
    ".db",
    ".db-wal",
    ".db-shm",
    ".sqlite",
    ".sqlite3",
    ".pid",
)
# The committed template is the one exception a `.env*` pattern would catch.
ALLOWED_NAMES = {".env.example"}


def offenders(paths: Iterable[str]) -> list[str]:
    """Return the staged paths that must not be committed."""
    found: list[str] = []
    for raw in paths:
        path = str(raw).strip()
        if not path:
            continue
        name = path.rsplit("/", 1)[-1]
        if name in ALLOWED_NAMES:
            continue
        if name in FORBIDDEN_NAMES or name.startswith(".env.") or name.endswith(FORBIDDEN_SUFFIXES):
            found.append(path)
    return found


def staged_paths() -> list[str]:
    """Added/copied/modified/renamed paths in the index."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Outside a git checkout or with no index, there is nothing to guard.
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def main(paths: Optional[Iterable[str]] = None) -> int:
    """Exit 1 when a forbidden path is staged (or was passed explicitly)."""
    if paths is None:
        paths = sys.argv[1:] or None
    found = offenders(paths if paths is not None else staged_paths())
    if not found:
        return 0
    print("Refusing to commit files that hold credentials or real user data:", file=sys.stderr)
    for path in found:
        print(f"  - {path}", file=sys.stderr)
    print(
        "These belong on the server, not in Git. Unstage them with:\n"
        "  git restore --staged <path>\n"
        "If the file is genuinely source, rename it so it no longer matches a data pattern.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
