"""Smart sticker policy for Aura.

Stage 21: map a fixed set of English tags onto the owner's personal Telegram
sticker pack, with conservative per-(chat, user) cooldowns. The policy layer is
deliberately offline-safe: it never imports Telegram/Gemini SDKs and never
sends anything itself. bot.py loads the pack from Telegram (file_ids stay
private and never reach the model), the send_sticker tool reserves a slot, and
message_handler delivers the sticker after the text reply.

Silence is a valid decision: a sticker is rare garnish, never the meal. Any
config/registry problem disables the feature instead of crashing the bot.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("aura-stickers")

DEFAULT_CONFIG_FILENAME = "stickers_config.json"
DEFAULT_COOLDOWN_SECONDS = 60.0
DEFAULT_GROUP_COOLDOWN_SECONDS = 120.0
# Explicit user requests skip the normal cooldown but never this flood guard.
HARD_MINIMUM_GAP_SECONDS = 3.0


@dataclass(frozen=True)
class StickerEntry:
    """One configured sticker.

    file_id/file_unique_id stay empty until the Telegram pack is joined onto
    the config by position; tags and descriptions come from the config file.
    """

    tag: str
    description: str
    position: int
    file_id: str = ""
    file_unique_id: str = ""


def _env_seconds(name: str, default: float) -> float:
    """Tolerant env parsing: a bad value must never crash the policy layer.

    bot.py validates the same variables fail-fast at startup and pushes the
    authoritative values in via configure().
    """
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s; using default %s seconds.", name, default)
        return default
    return max(0.0, value)


class StickerManager:
    """Conservative, stateful sticker policy.

    Cooldown is tracked per (chat, user), so one person cannot turn Aura into a
    sticker machine while other conversations remain unaffected. An explicit
    user request bypasses the normal cooldown but keeps a hard minimum gap so
    repeated demands cannot flood a chat.
    """

    def __init__(
        self,
        config_path: Optional[object] = None,
        *,
        cooldown_seconds: Optional[float] = None,
        group_cooldown_seconds: Optional[float] = None,
        max_state_entries: int = 2048,
        hard_minimum_gap_seconds: float = HARD_MINIMUM_GAP_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._cooldown_seconds = max(0.0, float(cooldown_seconds)) if cooldown_seconds is not None else _env_seconds(
            "STICKER_COOLDOWN_SECONDS", DEFAULT_COOLDOWN_SECONDS
        )
        self._group_cooldown_seconds = (
            max(0.0, float(group_cooldown_seconds))
            if group_cooldown_seconds is not None
            else _env_seconds("STICKER_GROUP_COOLDOWN_SECONDS", DEFAULT_GROUP_COOLDOWN_SECONDS)
        )
        self.max_state_entries = max(128, int(max_state_entries))
        self._hard_minimum_gap_seconds = max(0.0, float(hard_minimum_gap_seconds))
        self._last_reservation: "OrderedDict[tuple[int, int], float]" = OrderedDict()
        self._config_entries: tuple[StickerEntry, ...] = ()
        self._registry: dict[str, StickerEntry] = {}
        self._set_name: str = ""
        self._config_error: str = ""
        self._registry_error: str = ""
        self._config_path = self._resolve_config_path(config_path)
        self._load_config(self._config_path)

    # ---------- configuration ----------

    @staticmethod
    def _resolve_config_path(config_path: Optional[object]) -> Path:
        if config_path:
            return Path(config_path)
        from_env = (os.getenv("STICKER_CONFIG_PATH", "") or "").strip()
        if from_env:
            return Path(from_env)
        return Path(__file__).resolve().parent / DEFAULT_CONFIG_FILENAME

    def _load_config(self, path: Path) -> None:
        """Load and validate stickers_config.json; any problem disables the feature."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._config_error = f"Cannot read sticker config: {exc}"
            logger.error("Sticker config unreadable; smart sticker sending disabled | path=%s | %s", path, exc)
            return
        if not isinstance(raw, dict):
            self._config_error = "Sticker config must be a JSON object."
            logger.error("Sticker config invalid; smart sticker sending disabled | path=%s", path)
            return
        set_name = str(raw.get("set_name", "") or "").strip()
        items = raw.get("stickers")
        if not set_name or not isinstance(items, list) or not items:
            self._config_error = "Sticker config needs a non-empty set_name and stickers list."
            logger.error("Sticker config invalid; smart sticker sending disabled | path=%s", path)
            return
        entries: list[StickerEntry] = []
        seen_tags: set[str] = set()
        seen_positions: set[int] = set()
        for item in items:
            if not isinstance(item, dict):
                self._config_error = "Every sticker entry must be a JSON object."
                logger.error("Sticker config invalid; entry is not an object | path=%s", path)
                return
            tag = str(item.get("tag", "") or "").strip()
            description = str(item.get("description", "") or "").strip()
            position = item.get("position")
            if not tag or not description or not isinstance(position, int) or isinstance(position, bool) or position < 1:
                self._config_error = "Every sticker entry needs tag, description and an integer position >= 1."
                logger.error("Sticker config invalid; bad entry | entry=%r", item)
                return
            if tag in seen_tags or position in seen_positions:
                self._config_error = f"Duplicate sticker tag or position: {tag!r} / {position}."
                logger.error("Sticker config invalid; duplicate tag or position | tag=%s | position=%s", tag, position)
                return
            seen_tags.add(tag)
            seen_positions.add(position)
            entries.append(StickerEntry(tag=tag, description=description, position=position))
        if seen_positions != set(range(1, len(entries) + 1)):
            self._config_error = "Sticker positions must be contiguous starting at 1."
            logger.error("Sticker config invalid; positions not contiguous | positions=%s", sorted(seen_positions))
            return
        entries.sort(key=lambda entry: entry.position)
        self._config_entries = tuple(entries)
        self._set_name = set_name
        self._config_error = ""
        logger.info("Sticker config loaded | path=%s | set=%s | stickers=%s", path, set_name, len(entries))

    # ---------- public state ----------

    @property
    def set_name(self) -> str:
        """Sticker set name from the config file (env override lives in bot.py)."""
        return self._set_name

    @property
    def config_error(self) -> str:
        return self._config_error

    @property
    def registry_error(self) -> str:
        return self._registry_error

    def is_configured(self) -> bool:
        """True when the config file loaded and validated (tags are known offline)."""
        return bool(self._config_entries)

    def is_ready(self) -> bool:
        """True when the Telegram pack has been joined onto the config (file_ids known)."""
        return self.is_configured() and bool(self._registry)

    def tags(self) -> tuple[str, ...]:
        """Configured tags in pack order; used as the enum in the tool schema."""
        return tuple(entry.tag for entry in self._config_entries)

    def describe_for_prompt(self) -> str:
        """Short English tag list for the system prompt. Never contains file_ids."""
        return "\n".join(f"{entry.tag}: {entry.description}" for entry in self._config_entries)

    def configure(self, *, cooldown_seconds: Optional[float] = None, group_cooldown_seconds: Optional[float] = None) -> None:
        """Apply validated runtime settings (bot.py pushes fail-fast env values here)."""
        with self._lock:
            if cooldown_seconds is not None:
                self._cooldown_seconds = max(0.0, float(cooldown_seconds))
            if group_cooldown_seconds is not None:
                self._group_cooldown_seconds = max(0.0, float(group_cooldown_seconds))

    # ---------- registry ----------

    def set_registry(self, items: object) -> bool:
        """Join ordered (file_unique_id, file_id) pairs from Telegram onto the config.

        The pair order is the 1-based pack order returned by get_sticker_set().
        Any count or identifier problem disables sending and logs an error; it
        never raises, because a wrong position mapping would send the wrong
        face and a broken pack must not take the bot down.
        """
        pairs = list(items or [])
        with self._lock:
            if not self._config_entries:
                self._registry = {}
                self._registry_error = "Sticker config is not loaded."
                logger.error("Sticker registry rejected; config missing | items=%s", len(pairs))
                return False
            if len(pairs) != len(self._config_entries):
                self._registry = {}
                self._registry_error = (
                    f"Pack size mismatch: config expects {len(self._config_entries)}, Telegram returned {len(pairs)}."
                )
                logger.error(
                    "Sticker registry mismatch; smart sticker sending disabled | set=%s | expected=%s | got=%s",
                    self._set_name,
                    len(self._config_entries),
                    len(pairs),
                )
                return False
            unique_ids: set[str] = set()
            registry: dict[str, StickerEntry] = {}
            for entry in self._config_entries:
                pair = pairs[entry.position - 1]
                try:
                    file_unique_id, file_id = pair
                except (TypeError, ValueError):
                    file_unique_id, file_id = "", ""
                file_unique_id = str(file_unique_id or "").strip()
                file_id = str(file_id or "").strip()
                if not file_unique_id or not file_id or file_unique_id in unique_ids:
                    self._registry = {}
                    self._registry_error = "Telegram returned empty or duplicate sticker identifiers."
                    logger.error(
                        "Sticker registry invalid; smart sticker sending disabled | set=%s | position=%s",
                        self._set_name,
                        entry.position,
                    )
                    return False
                unique_ids.add(file_unique_id)
                registry[entry.tag] = StickerEntry(
                    tag=entry.tag,
                    description=entry.description,
                    position=entry.position,
                    file_id=file_id,
                    file_unique_id=file_unique_id,
                )
            self._registry = registry
            self._registry_error = ""
            logger.info("Sticker registry applied | set=%s | stickers=%s", self._set_name, len(registry))
            return True

    def get(self, tag: str) -> Optional[StickerEntry]:
        """Return the live entry for a tag, or None when unavailable/disabled."""
        with self._lock:
            return self._registry.get(str(tag or "").strip())

    def entry_at_position(self, position: int) -> Optional[StickerEntry]:
        """Return the registry entry mapped to a 1-based pack position."""
        with self._lock:
            for entry in self._registry.values():
                if entry.position == int(position):
                    return entry
        return None

    def invalidate(self, file_unique_id: str) -> None:
        """Drop one broken sticker after Telegram rejected its file_id."""
        target = str(file_unique_id or "").strip()
        if not target:
            return
        with self._lock:
            removed = [tag for tag, entry in self._registry.items() if entry.file_unique_id == target]
            for tag in removed:
                del self._registry[tag]
            if removed:
                self._registry_error = f"Invalidated stale stickers: {', '.join(sorted(removed))}."
                logger.warning(
                    "Sticker entry invalidated | file_unique_id=%s | tags=%s",
                    target,
                    sorted(removed),
                )

    # ---------- cooldown / reservation ----------

    def check_and_reserve(
        self,
        chat_id: int,
        user_id: int,
        is_group: bool,
        explicit: bool,
        now: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Reserve one sticker slot for (chat, user) or explain why it is denied.

        Returns (allowed, reason). reason is empty when allowed and an English
        explanation when denied (it is surfaced to the model, never to users).
        The clock is injectable so tests are deterministic.
        """
        if not self.is_ready():
            return False, "Sticker sending is currently disabled."
        timestamp = float(now) if now is not None else time.monotonic()
        key = (int(chat_id), int(user_id))
        with self._lock:
            gap = self._hard_minimum_gap_seconds if explicit else (
                self._group_cooldown_seconds if is_group else self._cooldown_seconds
            )
            previous = self._last_reservation.get(key)
            if previous is not None and timestamp - previous < gap:
                if explicit:
                    return False, "A sticker was just sent for this user; wait a few seconds before the next one."
                return False, "Sticker cooldown is active for this chat/user pair; reply with text only."
            self._last_reservation[key] = timestamp
            self._last_reservation.move_to_end(key)
            # Keep the state bounded because this is intentionally ephemeral.
            while len(self._last_reservation) > self.max_state_entries:
                self._last_reservation.popitem(last=False)
        return True, ""


sticker_manager = StickerManager()
