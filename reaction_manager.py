"""Natural Telegram message-reaction policy for Aura.

The reaction layer is deliberately conservative: silence is a valid decision.
High-confidence social signals are handled locally for zero-latency reactions.
Ambiguous short social messages can optionally be classified by Gemini after the
normal Aura reply, so reactions are not limited to a tiny hard-coded phrase list.
"""

from __future__ import annotations

import json
import re
import time

from retry_policy import call_with_retry
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import Optional


REACTION_EMOJIS = frozenset({"❤️", "😂", "🔥", "👍", "🎉", "😢"})
AI_REACTION_VALUES = ("NONE", "❤️", "😂", "🔥", "👍", "🎉", "😢")

THANKS_PATTERNS = (
    r"^(?:خیلی\s*)?(?:ممنون|مرسی|متشکرم|سپاس(?:گزارم)?|دمت\s*گرم|دستت\s*درد\s*نکنه)(?:\s*[!！.،,❤❤️🙏]+)*$",
    r"^(?:thanks|thank\s+you|thx|ty|many\s+thanks)(?:\s*[!！.❤❤️🙏]+)*$",
)
LAUGH_ONLY_PATTERN = re.compile(
    r"^(?:[😂🤣😆]+|(?:خخ+|هاها+|لول+|lol+|lmao+)[!！.😂🤣😆\s]*)$",
    re.IGNORECASE,
)
COMPLIMENT_ONLY_PATTERNS = (
    r"^(?:خیلی\s+)?(?:خفنه|باحاله|عالیه|عالی|توپی|دمت\s+گرم|محشره|فوق\s*العاده)(?:\s*[!！.،,🔥❤️]+)*$",
    r"^(?:that'?s\s+awesome|so\s+cool|nice|great|awesome|amazing)(?:\s*[!！.🔥❤️]+)*$",
)
SAD_ONLY_PATTERNS = (
    r"^(?:خیلی\s+)?(?:ناراحتم|حالم\s+خوبه\s+نیست|حالم\s+بد(?:ه)?|دلم\s+گرفته|حوصله\s+ندارم)(?:\s*[.،!！😢💔]+)*$",
)

# Hints used only to decide whether an ambiguous short message is worth asking
# Gemini about. This is not the reaction decision itself.
AI_SOCIAL_HINTS = (
    "ممنون", "مرسی", "متشک", "سپاس", "لطف", "دمت", "عالی", "خفن", "باحال",
    "محشر", "فوق العاده", "دوستت", "عاشق", "تبریک", "مبارک", "دمت گرم", "قربون",
    "خخ", "هاها", "لول", "خنده", "ناراح", "غم", "حالم", "دلم", "خسته",
    "تبریک", "مبارک", "یوهو", "بردیم", "بردی", "موفق شد", "اوکی", "باشه", "قبول",
    "thanks", "thank", "awesome", "cool", "nice", "great", "amazing", "love", "lol",
    "😂", "🤣", "😆", "❤️", "❤", "🔥", "🎉", "😢", "💔", "🙏", "🥹", "😍",
)


@dataclass(frozen=True)
class ReactionDecision:
    """Decision made before invoking the expensive assistant path."""

    reaction: Optional[str] = None
    reply_only: bool = False

    @property
    def should_react(self) -> bool:
        return self.reaction is not None


class ReactionManager:
    """Conservative, stateful reaction policy.

    Cooldown is tracked per (chat, user), so one person cannot accidentally
    produce a stream of reactions while other conversations remain unaffected.
    """

    def __init__(self, cooldown_seconds: float = 45.0, max_state_entries: int = 2048) -> None:
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.max_state_entries = max(128, int(max_state_entries))
        self._last_reaction: "OrderedDict[tuple[int, int], float]" = OrderedDict()
        self._lock = Lock()

    @staticmethod
    def _normalize(text: str) -> str:
        text = (text or "").strip().casefold()
        text = re.sub(r"\s+", " ", text)
        return text

    @staticmethod
    def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
        return any(re.fullmatch(pattern, text, flags=re.IGNORECASE) for pattern in patterns)

    def decide(self, *, chat_id: int, user_id: int, text: str) -> ReactionDecision:
        """Return a high-confidence local reaction or silence."""
        normalized = self._normalize(text)
        if not normalized:
            return ReactionDecision()

        # Never steal messages that are clearly questions or requests.
        if "?" in normalized or "؟" in normalized:
            return ReactionDecision()

        reaction: Optional[str] = None
        if self._matches_any(normalized, THANKS_PATTERNS) or re.fullmatch(
            r"^(?:خیلی\s*)?مرسیی+[!！.،,❤❤️🙏\s]*$", normalized
        ):
            reaction = "❤️"
        elif LAUGH_ONLY_PATTERN.fullmatch(normalized):
            reaction = "😂"
        elif self._matches_any(normalized, COMPLIMENT_ONLY_PATTERNS) or re.fullmatch(
            r"^(?:این\s+)?(?:خیلی\s+)?(?:خفنه|باحاله|عالیه|عالی|توپه|محشره|فوق\s*العاده)[!！.،,🔥❤️\s]*$",
            normalized,
        ):
            reaction = "🔥"
        elif self._matches_any(normalized, SAD_ONLY_PATTERNS) or re.fullmatch(
            r"^(?:امروز\s+)?(?:خیلی\s+)?(?:ناراحتم|حالم\s+خوبه\s+نیست|حالم\s+بد(?:ه)?|دلم\s+گرفته|حوصله\s+ندارم)[.،!！😢💔\s]*$",
            normalized,
        ):
            reaction = "❤️"

        if reaction is None or not self.can_react(chat_id, user_id):
            return ReactionDecision()
        return ReactionDecision(reaction=reaction)

    def should_consider_ai(self, text: str) -> bool:
        """Return True for short social-looking messages worth semantic classification."""
        normalized = self._normalize(text)
        if not normalized or len(normalized) > 240:
            return False
        if normalized.startswith("/"):
            return False
        if "?" in normalized or "؟" in normalized:
            return False
        if "http://" in normalized or "https://" in normalized or "www." in normalized:
            return False
        # Avoid spending a second model call on obvious technical payloads.
        if any(marker in normalized for marker in ("```", "<script", "SELECT ", "Traceback", "def ", "import ")):
            return False
        if any(hint in normalized for hint in AI_SOCIAL_HINTS):
            return True
        # Very short emoji-heavy messages are also good social candidates.
        if len(normalized) <= 18 and sum(ch in "😂🤣😆❤️❤🔥🎉😢💔🙏🥹😍👍" for ch in normalized) >= 2:
            return True
        return False

    def decide_with_gemini(self, *, client, model: str, text: str) -> ReactionDecision:
        """Use Gemini as a semantic second opinion for ambiguous social messages."""
        normalized = self._normalize(text)
        if not self.should_consider_ai(normalized):
            return ReactionDecision()

        prompt = f"""Choose whether Aura should add one natural Telegram reaction to this user's message.

The reaction is optional. NONE is often the best answer.
React only when the message clearly expresses a social/emotional signal such as thanks,
amusement, praise, celebration, affection/appreciation, or a simple emotional moment.
Do NOT react to questions, requests, technical content, factual statements, arguments,
commands, or messages where a reaction would feel forced.
Never use more than one reaction.

Allowed values: NONE, ❤️, 😂, 🔥, 👍, 🎉, 😢
Return JSON only with exactly these fields:
{{"reaction":"NONE|❤️|😂|🔥|👍|🎉|😢","confidence":0.0}}

User message:
{normalized[:240]}
"""
        try:
            response = call_with_retry(
                lambda: client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config={
                        "response_mime_type": "application/json",
                        "response_schema": {
                            "type": "OBJECT",
                            "properties": {
                                "reaction": {"type": "STRING", "enum": list(AI_REACTION_VALUES)},
                                "confidence": {"type": "NUMBER"},
                            },
                            "required": ["reaction", "confidence"],
                        },
                    },
                ),
                operation_name="ai-reaction",
                logger=__import__("logging").getLogger("aura-reaction"),
                max_retries=2,
            )
            raw = str(getattr(response, "text", "") or "").strip()
            data = json.loads(raw)
            reaction = str(data.get("reaction", "NONE"))
            confidence = float(data.get("confidence", 0.0))
            if reaction not in REACTION_EMOJIS or confidence < 0.82:
                return ReactionDecision()
            return ReactionDecision(reaction=reaction)
        except Exception:
            # Reaction is optional. A classifier failure must never affect Aura's answer.
            return ReactionDecision()

    def can_react(self, chat_id: int, user_id: int) -> bool:
        now = time.monotonic()
        key = (int(chat_id), int(user_id))
        with self._lock:
            previous = self._last_reaction.get(key)
            return previous is None or now - previous >= self.cooldown_seconds

    def record_reaction(self, chat_id: int, user_id: int) -> None:
        now = time.monotonic()
        key = (int(chat_id), int(user_id))
        with self._lock:
            self._last_reaction[key] = now
            self._last_reaction.move_to_end(key)
            while len(self._last_reaction) > self.max_state_entries:
                self._last_reaction.popitem(last=False)


reaction_manager = ReactionManager()
