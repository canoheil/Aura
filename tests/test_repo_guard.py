"""The pre-commit guard that keeps credentials and user data out of Git."""
import subprocess
import sys

import pytest

import repo_guard


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "bot.db",
        "bot.db-wal",
        "backup/bot.db-shm",
        "data/app.sqlite",
        "data/app.sqlite3",
        ".aura_secret_key",
        "aura.pid",
        ".env.local",
    ],
)
def test_runtime_files_are_refused(path):
    assert repo_guard.offenders([path]) == [path]


@pytest.mark.parametrize(
    "path",
    [".env.example", "bot.py", "i18n.py", "assets/logo.png", "README.md", "app.db.py", "settings_store.py"],
)
def test_source_files_are_allowed(path):
    assert repo_guard.offenders([path]) == []


def test_only_the_offending_paths_are_reported():
    assert repo_guard.offenders(["bot.py", ".env", "i18n.py", "bot.db"]) == [".env", "bot.db"]


def test_blank_lines_are_ignored():
    assert repo_guard.offenders(["", "  ", "\n"]) == []


def test_the_command_exits_non_zero_and_names_the_file(capsys):
    assert repo_guard.main([".env"]) == 1
    captured = capsys.readouterr()
    assert ".env" in captured.err
    assert "Refusing to commit" in captured.err


def test_the_command_exits_zero_for_ordinary_files(capsys):
    assert repo_guard.main(["bot.py"]) == 0
    assert capsys.readouterr().err == ""


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _run_guard(repo):
    return subprocess.run(
        [sys.executable, str(repo_guard.__file__)], cwd=repo, capture_output=True, text=True,
    )


def test_the_guard_reads_the_real_index(tmp_path):
    """End to end: the hook is what runs on `git commit`, so test it that way."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git is not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    (repo / "bot.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "bot.db.txt").write_text("not a database\n", encoding="utf-8")
    _git(repo, "add", "-A")
    assert _run_guard(repo).returncode == 0

    # `git add -f` defeats .gitignore; the hook must still refuse.
    (repo / "bot.db").write_text("SQLite format 3\x00", encoding="utf-8")
    _git(repo, "add", "-f", "bot.db")
    result = _run_guard(repo)
    assert result.returncode == 1
    assert "bot.db" in result.stderr
