"""Structured Telegram group context for Aura.

This module keeps message-speaker identity separate from reply targets,
forward origins, and textual mentions. It is intentionally read-only: it does
not mutate identity records or memory.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class GroupPersonRef:
    user_id: Optional[int]
    display_name: str = ""
    username: str = ""


@dataclass(frozen=True)
class GroupContext:
    is_group: bool
    speaker: GroupPersonRef
    replied_author: Optional[GroupPersonRef] = None
    forwarded_author: Optional[GroupPersonRef] = None
    mentioned_usernames: tuple[str, ...] = ()
    mentioned_user_ids: tuple[int, ...] = ()

    def prompt_block(self) -> str:
        """Return deterministic context for Gemini without asserting claims as facts."""
        lines = [
            f"[Current speaker: {self.speaker.display_name or 'Unknown'} | Telegram ID: {self.speaker.user_id}]"
        ]
        if self.replied_author:
            lines.append(
                f"[Replied message author: {self.replied_author.display_name or 'Unknown'} | Telegram ID: {self.replied_author.user_id}]"
            )
        if self.forwarded_author:
            lines.append(
                f"[Forwarded message author: {self.forwarded_author.display_name or 'Unknown'} | Telegram ID: {self.forwarded_author.user_id}]"
            )
        if self.mentioned_user_ids:
            lines.append(f"[Mentioned Telegram IDs: {', '.join(map(str, self.mentioned_user_ids))}]")
        if self.mentioned_usernames:
            lines.append(f"[Mentioned usernames: {', '.join('@' + x for x in self.mentioned_usernames)}]")
        return "\n".join(lines)


def _person(user) -> Optional[GroupPersonRef]:
    if not user:
        return None
    first = str(getattr(user, "first_name", "") or "").strip()
    last = str(getattr(user, "last_name", "") or "").strip()
    display = " ".join(x for x in (first, last) if x).strip()
    username = str(getattr(user, "username", "") or "").strip()
    user_id = getattr(user, "id", None)
    return GroupPersonRef(
        user_id=int(user_id) if user_id is not None else None,
        display_name=display or (f"@{username}" if username else ""),
        username=username,
    )


def build_group_context(message, *, is_group: bool) -> GroupContext:
    """Build context from one Telegram message without changing stored identity."""
    speaker = _person(getattr(message, "from_user", None)) or GroupPersonRef(None, "Unknown")
    reply = getattr(message, "reply_to_message", None)
    replied_author = _person(getattr(reply, "from_user", None)) if reply else None

    forwarded_author = None
    origin = getattr(message, "forward_origin", None)
    origin_sender = getattr(origin, "sender_user", None) if origin else None
    if origin_sender:
        forwarded_author = _person(origin_sender)

    mentioned_usernames: list[str] = []
    mentioned_user_ids: list[int] = []
    entities = list(getattr(message, "entities", None) or []) + list(getattr(message, "caption_entities", None) or [])
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    for entity in entities:
        kind = getattr(entity, "type", None)
        offset = int(getattr(entity, "offset", 0) or 0)
        length = int(getattr(entity, "length", 0) or 0)
        if kind == "text_mention":
            mentioned = getattr(entity, "user", None)
            if mentioned and getattr(mentioned, "id", None) is not None:
                mentioned_user_ids.append(int(mentioned.id))
        elif kind == "mention":
            value = text[offset:offset + length].lstrip("@").strip()
            if value:
                mentioned_usernames.append(value)

    return GroupContext(
        is_group=is_group,
        speaker=speaker,
        replied_author=replied_author,
        forwarded_author=forwarded_author,
        mentioned_usernames=tuple(dict.fromkeys(mentioned_usernames)),
        mentioned_user_ids=tuple(dict.fromkeys(mentioned_user_ids)),
    )
