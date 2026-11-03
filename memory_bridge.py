"""Safety boundary between conversational memory and authoritative identity.

Memory may store user facts/preferences, but it must never establish or mutate
Telegram identity. Identity belongs to the identity layer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_IDENTITY_PATTERNS = (
    r"(?:^|\s)(?:اسمم|اسم من|منو)\s+[^،.!؟?]{1,120}(?:است|هست|صدا(?:م|م کن)|کن)",
    r"(?:^|\s)من\s+[^\s،.!؟?]{2,80}م(?:$|[\s،.!؟?])",
    r"(?:^|\s)من\s+[^،.!؟?]{1,80}(?:هستم|ام)(?:$|[\s،.!؟?])",
    r"\b(?:my name is|i am|i'm|call me)\s+[^,.!?]{1,120}(?:$|[,.!?])",
    r"\b(?:telegram id|user id)\b|(?:^|\s)(?:ایدی|آیدی)\s*(?:من|این اکانت)",
    r"(?:^|\s)(?:این اکانت|this account)\b[^.!?]{0,100}(?:من|mine|belongs to)",
)


def looks_like_identity_claim(text: str) -> bool:
    value = re.sub(r"\s+", " ", text or "").strip().lower()
    return bool(value) and any(re.search(pattern, value, flags=re.I) for pattern in _IDENTITY_PATTERNS)


@dataclass(frozen=True)
class MemoryOwner:
    user_id: int
    source: str = "message_author"


class MemoryBridge:
    """Enforces the boundary: memory is scoped to a verified message owner."""

    def resolve_owner(self, *, speaker_user_id: int | None, explicit_owner_user_id: int | None = None) -> MemoryOwner | None:
        speaker = int(speaker_user_id or 0)
        explicit = int(explicit_owner_user_id or 0)
        if not speaker:
            return None
        # A tool cannot choose another user's memory owner. The Telegram
        # message author is the only safe owner unless an upstream trusted
        # identity layer explicitly supplies the same ID.
        if explicit and explicit != speaker:
            return None
        return MemoryOwner(speaker)

    def validate_memory_content(self, content: str) -> tuple[bool, str]:
        clean = re.sub(r"\s+", " ", content or "").strip()
        if not clean:
            return False, "empty"
        if looks_like_identity_claim(clean):
            return False, "identity_claim"
        return True, clean
