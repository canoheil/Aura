"""Runtime settings for Aura: DB-backed, env-fallback, typed and audited.

Why this exists
---------------
Self-hosting Aura should not require editing `.env` and restarting for every
change. An owner should be able to switch the model provider, rotate a key, or
turn a feature off from Telegram.

Precedence for every value is:  database override  >  environment variable  >  default.
Secrets are never returned in plain text to callers that only need to display
them (`describe()` masks them), and when `cryptography` is installed they are
encrypted at rest with a key that is kept out of the database.

The store is deliberately dumb about Telegram and about providers: it validates
values against a registry and stores strings. `providers.py` and `admin_panel.py`
build on top of it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("aura-settings")

# Marker prefix so a value written without encryption is recognizable and can be
# migrated later instead of being silently mistaken for ciphertext.
_PLAIN_PREFIX = "plain:"
_ENC_PREFIX = "enc:"


class SettingError(ValueError):
    """Raised when a setting key is unknown or a value fails validation."""


@dataclass(frozen=True)
class SettingSpec:
    """One editable setting.

    ``kind`` drives validation and the admin UI: bool, int, choice, text, url,
    secret. ``env`` is the environment variable used when no database override
    exists; it is also the migration path for existing deployments.
    """

    key: str
    default: str = ""
    kind: str = "text"
    group: str = "general"
    label: str = ""
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    env: Optional[str] = None
    secret: bool = False
    restart_hint: bool = False

    def __post_init__(self) -> None:
        if self.kind not in {"bool", "int", "choice", "text", "url", "secret"}:
            raise SettingError(f"unknown setting kind: {self.kind}")
        if self.kind == "choice" and not self.choices:
            raise SettingError(f"choice setting without choices: {self.key}")


def _spec(*args, **kwargs) -> SettingSpec:
    spec = SettingSpec(*args, **kwargs)
    if spec.kind == "secret":
        # Secrets are masked in every human-readable surface.
        object.__setattr__(spec, "secret", True)
    return spec


# The editable surface. Adding a new knob is one entry here plus (optionally)
# one use site that reads it through `get()`.
SPECS: tuple[SettingSpec, ...] = (
    # --- provider selection -------------------------------------------------
    _spec("llm_provider", "gemini", kind="choice", group="provider",
          label="Model provider", env="LLM_PROVIDER",
          choices=("gemini", "openai_compat"),
          help="gemini uses the native Google SDK. openai_compat works with OpenAI, "
               "OpenRouter, Groq, DeepSeek, Together, Ollama and any other gateway "
               "that speaks the /chat/completions protocol."),
    _spec("gemini_model", "gemini-3.1-flash-lite", kind="text", group="provider",
          label="Gemini model", env="GEMINI_MODEL"),
    _spec("gemini_api_key", "", kind="secret", group="provider",
          label="Gemini API key", env="GEMINI_API_KEY"),
    _spec("openai_base_url", "https://api.openai.com/v1", kind="url", group="provider",
          label="OpenAI-compatible base URL", env="OPENAI_BASE_URL",
          help="For OpenRouter use https://openrouter.ai/api/v1"),
    _spec("openai_api_key", "", kind="secret", group="provider",
          label="OpenAI-compatible API key", env="OPENAI_API_KEY"),
    _spec("openai_model", "gpt-4o-mini", kind="text", group="provider",
          label="OpenAI-compatible model", env="OPENAI_MODEL"),
    _spec("llm_timeout_seconds", "120", kind="int", group="provider", env="LLM_TIMEOUT_SECONDS",
          label="Model request timeout (seconds)", minimum=5, maximum=300),
    _spec("llm_max_retries", "2", kind="int", group="provider", env="LLM_MAX_RETRIES",
          label="Model request retries", minimum=0, maximum=4),

    # --- agent budget -------------------------------------------------------
    _spec("agent_max_rounds", "5", kind="int", group="agent", env="AGENT_MAX_ROUNDS",
          label="Maximum agent rounds", minimum=1, maximum=12),
    _spec("agent_max_tool_calls", "10", kind="int", group="agent", env="AGENT_MAX_TOOL_CALLS",
          label="Maximum tool calls per turn", minimum=1, maximum=30),

    # --- features -----------------------------------------------------------
    _spec("web_search_enabled", "true", kind="bool", group="features", env="WEB_SEARCH_ENABLED",
          label="Web search"),
    _spec("web_search_daily_limit", "100", kind="int", group="features", env="WEB_SEARCH_DAILY_LIMIT",
          label="Web searches per day", minimum=0, maximum=10000),
    _spec("weather_enabled", "true", kind="bool", group="features", env="WEATHER_ENABLED",
          label="Weather"),
    _spec("memes_enabled", "true", kind="bool", group="features", env="MEMES_ENABLED",
          label="Memes"),
    _spec("stickers_enabled", "true", kind="bool", group="features", env="STICKERS_ENABLED",
          label="Stickers"),
    _spec("pollinations_enabled", "true", kind="bool", group="features", env="POLLINATIONS_ENABLED",
          label="Image generation"),
    _spec("svg_image_enabled", "true", kind="bool", group="features", env="SVG_IMAGE_ENABLED",
          label="Vector (SVG) images"),
    _spec("reaction_ai_enabled", "true", kind="bool", group="features", env="REACTION_AI_ENABLED",
          label="AI-chosen reactions"),
    _spec("context_manager_enabled", "true", kind="bool", group="features", env="CONTEXT_MANAGER_ENABLED",
          label="Rolling conversation summary"),

    # --- behaviour ----------------------------------------------------------
    _spec("bot_language", "fa", kind="choice", group="behaviour", env="BOT_LANGUAGE",
          label="Bot language", choices=("fa", "en"),
          help="Language of Aura's own replies. Persian remains the default."),
    _spec("max_concurrent_chats", "4", kind="int", group="behaviour", env="MAX_CONCURRENT_CHATS",
          label="Concurrent chats", minimum=1, maximum=32, restart_hint=True),
    _spec("reminder_default_timezone", "Asia/Tehran", kind="text", group="behaviour",
          env="REMINDER_DEFAULT_TIMEZONE", label="Default timezone"),
)

INDEX: dict[str, SettingSpec] = {spec.key: spec for spec in SPECS}
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(spec.group for spec in SPECS))


# --- secret handling -------------------------------------------------------

def _load_fernet(key_path_hint: Optional[Path] = None):
    """Return a Fernet instance, or None when encryption is unavailable.

    The key is read from AURA_SECRET_KEY, or generated once into
    `.aura_secret_key` next to the database. That file is gitignored and never
    leaves the host; losing it only means stored secrets must be re-entered.
    """
    try:
        from cryptography.fernet import Fernet
    except Exception:  # pragma: no cover - depends on the install
        return None

    raw = os.getenv("AURA_SECRET_KEY", "").strip()
    if raw:
        try:
            return Fernet(raw.encode("ascii"))
        except Exception:
            logger.warning("AURA_SECRET_KEY is not a valid Fernet key; falling back to a local key file.")
    key_path = Path(
        os.getenv("AURA_SECRET_KEY_FILE", "")
        or (key_path_hint or Path.cwd() / "bot.db").parent / ".aura_secret_key"
    )
    try:
        if key_path.exists():
            key = key_path.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            key_path.write_bytes(key + b"\n")
            try:
                key_path.chmod(0o600)
            except OSError:
                pass
            logger.info("Created a local encryption key at %s", key_path)
        return Fernet(key)
    except Exception as exc:  # pragma: no cover - filesystem dependent
        logger.warning("Could not prepare an encryption key (%s); secrets will be stored unencrypted.", exc)
        return None


class SettingsStore:
    """SQLite-backed overrides with validation, masking and an audit trail."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        lock: Optional[threading.Lock] = None,
        *,
        db_path: Optional[Path] = None,
    ) -> None:
        self._db = connection
        # Re-entrant: settings are read from code paths that already hold
        # the application database lock.
        self._lock = lock or threading.RLock()
        # The encryption key lives beside the database unless overridden.
        self._fernet = _load_fernet(db_path)
        self._ensure_schema()

    # -- storage -----------------------------------------------------------
    def _ensure_schema(self) -> None:
        with self._lock:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    is_secret INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    updated_by INTEGER
                )
                """
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor INTEGER,
                    at INTEGER NOT NULL
                )
                """
            )
            self._db.commit()

    def _encrypt(self, value: str) -> str:
        if self._fernet is None:
            return _PLAIN_PREFIX + value
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        return _ENC_PREFIX + token

    def _decrypt(self, stored: str) -> str:
        if stored.startswith(_ENC_PREFIX):
            if self._fernet is None:
                logger.warning("A stored secret cannot be decrypted without cryptography.")
                return ""
            try:
                return self._fernet.decrypt(stored[len(_ENC_PREFIX):].encode("ascii")).decode("utf-8")
            except Exception:
                logger.warning("A stored secret could not be decrypted (key changed?).")
                return ""
        if stored.startswith(_PLAIN_PREFIX):
            return stored[len(_PLAIN_PREFIX):]
        return stored

    def _row(self, key: str):
        with self._lock:
            return self._db.execute(
                "SELECT key, value, is_secret, updated_at, updated_by FROM app_settings WHERE key = ?",
                (key,),
            ).fetchone()

    # -- public API --------------------------------------------------------
    def is_overridden(self, key: str) -> bool:
        return self._row(_require_spec(key).key) is not None

    def source(self, key: str) -> str:
        """Where the effective value came from: db, env or default."""
        spec = _require_spec(key)
        if self._row(spec.key) is not None:
            return "db"
        if spec.env and os.getenv(spec.env, "").strip():
            return "env"
        return "default"

    def raw(self, key: str) -> str:
        """Effective value, secrets decrypted. Use only where the value is needed."""
        spec = _require_spec(key)
        row = self._row(spec.key)
        if row is not None:
            stored = row["value"] if isinstance(row, sqlite3.Row) else row[1]
            return self._decrypt(stored)
        if spec.env:
            env_value = os.getenv(spec.env, "").strip()
            if env_value:
                return env_value
        return spec.default

    def get(self, key: str) -> str:
        return self.raw(key)

    def get_bool(self, key: str) -> bool:
        return self.raw(key).strip().lower() in {"1", "true", "yes", "on"}

    def get_int(self, key: str) -> int:
        spec = _require_spec(key)
        value = self.raw(key).strip()
        try:
            number = int(value)
        except (TypeError, ValueError):
            logger.warning("Setting %s has a non-numeric value; using default %s", key, spec.default)
            number = int(spec.default or 0)
        if spec.minimum is not None:
            number = max(spec.minimum, number)
        if spec.maximum is not None:
            number = min(spec.maximum, number)
        return number

    def describe(self, key: str) -> str:
        """Display form: secrets are masked, never returned."""
        spec = _require_spec(key)
        value = self.raw(key)
        if spec.secret:
            return mask_secret(value)
        return value

    def set(self, key: str, value: str, *, actor: Optional[int] = None) -> str:
        """Validate and store one override. Returns the normalized value."""
        spec = _require_spec(key)
        normalized = _validate(spec, value)
        stored = self._encrypt(normalized) if spec.kind == "secret" else normalized
        now = int(time.time())
        with self._lock:
            self._db.execute(
                """
                INSERT INTO app_settings (key, value, is_secret, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    is_secret = excluded.is_secret,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (spec.key, stored, 1 if spec.kind == "secret" else 0, now, actor),
            )
            self._db.execute(
                "INSERT INTO app_settings_audit (key, action, actor, at) VALUES (?, 'set', ?, ?)",
                (spec.key, actor, now),
            )
            self._db.commit()
        logger.info("Setting changed | key=%s | actor=%s | source=db", spec.key, actor)
        return normalized

    def unset(self, key: str, *, actor: Optional[int] = None) -> bool:
        """Remove an override so the environment or default applies again."""
        spec = _require_spec(key)
        with self._lock:
            cursor = self._db.execute("DELETE FROM app_settings WHERE key = ?", (spec.key,))
            if cursor.rowcount:
                self._db.execute(
                    "INSERT INTO app_settings_audit (key, action, actor, at) VALUES (?, 'unset', ?, ?)",
                    (spec.key, actor, int(time.time())),
                )
            self._db.commit()
        logger.info("Setting override removed | key=%s | actor=%s | removed=%s", spec.key, actor, bool(cursor.rowcount))
        return bool(cursor.rowcount)

    def recent_audit(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT key, action, actor, at FROM app_settings_audit ORDER BY id DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [
            {
                "key": row["key"] if isinstance(row, sqlite3.Row) else row[0],
                "action": row["action"] if isinstance(row, sqlite3.Row) else row[1],
                "actor": row["actor"] if isinstance(row, sqlite3.Row) else row[2],
                "at": row["at"] if isinstance(row, sqlite3.Row) else row[3],
            }
            for row in rows
        ]

    def encryption_mode(self) -> str:
        return "encrypted" if self._fernet is not None else "plaintext"


def _require_spec(key: str) -> SettingSpec:
    spec = INDEX.get(str(key or "").strip())
    if spec is None:
        raise SettingError(f"Unknown setting: {key!r}")
    return spec


def _validate(spec: SettingSpec, value: str) -> str:
    if not isinstance(value, str):
        value = str(value)
    text = value.strip()
    if spec.kind == "bool":
        normalized = text.lower()
        if normalized in {"1", "true", "yes", "on"}:
            return "true"
        if normalized in {"0", "false", "no", "off"}:
            return "false"
        raise SettingError("Expected one of: true, false")
    if spec.kind == "int":
        try:
            number = int(text)
        except ValueError as exc:
            raise SettingError("Expected a whole number") from exc
        if spec.minimum is not None and number < spec.minimum:
            raise SettingError(f"Minimum is {spec.minimum}")
        if spec.maximum is not None and number > spec.maximum:
            raise SettingError(f"Maximum is {spec.maximum}")
        return str(number)
    if spec.kind == "choice":
        if text not in spec.choices:
            raise SettingError("Expected one of: " + ", ".join(spec.choices))
        return text
    if spec.kind == "url":
        if not text:
            raise SettingError("Expected a URL")
        if not text.startswith(("http://", "https://")):
            raise SettingError("URL must start with http:// or https://")
        return text.rstrip("/")
    if spec.kind == "secret":
        if not text:
            raise SettingError("Expected a non-empty value")
        if any(ch.isspace() for ch in text):
            raise SettingError("A key must not contain spaces")
        return text
    return text


def mask_secret(value: str) -> str:
    """Show that a secret exists without revealing it."""
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]} ({len(value)} chars)"


def fingerprint(value: str) -> str:
    """Stable short hash, useful for logs that must not contain the value."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def specs_in_group(group: str) -> Iterable[SettingSpec]:
    return (spec for spec in SPECS if spec.group == group)
