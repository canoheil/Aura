"""Authority and claim evaluation for Aura identity data.

Identity is anchored to Telegram user IDs. Claims are observations/requests and
never mutate an authoritative identity by themselves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AuthorityLevel(str, Enum):
    VERIFIED = "verified"
    FOUNDER_ADMIN_CONFIRMED = "founder_admin_confirmed"
    ESTABLISHED = "established"
    SELF_CLAIM = "self_claim"
    OTHER_USER_CLAIM = "other_user_claim"
    JOKE_OR_SPECULATION = "joke_or_speculation"


class ClaimDecision(str, Enum):
    ACCEPT = "accept"
    ESTABLISH = "establish"
    PENDING = "pending"
    REJECT = "reject"
    IGNORE = "ignore"


_AUTHORITY_RANK = {
    AuthorityLevel.VERIFIED: 60,
    AuthorityLevel.FOUNDER_ADMIN_CONFIRMED: 50,
    AuthorityLevel.ESTABLISHED: 40,
    AuthorityLevel.SELF_CLAIM: 30,
    AuthorityLevel.OTHER_USER_CLAIM: 20,
    AuthorityLevel.JOKE_OR_SPECULATION: 10,
}


@dataclass(frozen=True)
class IdentityRecord:
    """Authoritative identity state for one Telegram user ID."""

    telegram_user_id: int
    canonical_name: str
    aliases: tuple[str, ...] = ()
    authority: AuthorityLevel = AuthorityLevel.ESTABLISHED


@dataclass(frozen=True)
class IdentityClaim:
    """A claim about an identity; it is not authoritative by itself."""

    speaker_user_id: int
    target_user_id: Optional[int]
    claimed_name: str
    authority: AuthorityLevel
    source: str = "message"
    is_joke: bool = False


@dataclass(frozen=True)
class ClaimEvaluation:
    decision: ClaimDecision
    claim: IdentityClaim
    reason: str
    resulting_identity: Optional[IdentityRecord] = None


@dataclass
class IdentityAuthority:
    """Evaluate identity claims without allowing low-authority claims to overwrite truth."""

    records: dict[int, IdentityRecord] = field(default_factory=dict)

    def register_verified(self, user_id: int, canonical_name: str, aliases: tuple[str, ...] = ()) -> IdentityRecord:
        """Create/replace a verified identity record for a Telegram user ID."""
        record = IdentityRecord(
            telegram_user_id=user_id,
            canonical_name=canonical_name.strip(),
            aliases=_normalize_aliases(aliases, canonical_name),
            authority=AuthorityLevel.VERIFIED,
        )
        self.records[user_id] = record
        return record

    def register_established(self, user_id: int, canonical_name: str, aliases: tuple[str, ...] = ()) -> IdentityRecord:
        """Register an established identity only when no stronger record exists."""
        current = self.records.get(user_id)
        if current and _AUTHORITY_RANK[current.authority] > _AUTHORITY_RANK[AuthorityLevel.ESTABLISHED]:
            return current
        record = IdentityRecord(
            telegram_user_id=user_id,
            canonical_name=canonical_name.strip(),
            aliases=_normalize_aliases(aliases, canonical_name),
            authority=AuthorityLevel.ESTABLISHED,
        )
        self.records[user_id] = record
        return record

    def get(self, user_id: int) -> Optional[IdentityRecord]:
        return self.records.get(user_id)

    def evaluate_claim(self, claim: IdentityClaim) -> ClaimEvaluation:
        """Evaluate a claim; this method never mutates records."""
        if not claim.claimed_name.strip():
            return ClaimEvaluation(ClaimDecision.IGNORE, claim, "empty identity claim")

        if claim.is_joke or claim.authority == AuthorityLevel.JOKE_OR_SPECULATION:
            return ClaimEvaluation(ClaimDecision.IGNORE, claim, "joke/speculation is not identity authority")

        target_id = claim.target_user_id or claim.speaker_user_id
        current = self.records.get(target_id)

        if claim.target_user_id is not None and claim.target_user_id != claim.speaker_user_id:
            if claim.authority != AuthorityLevel.VERIFIED and claim.authority != AuthorityLevel.FOUNDER_ADMIN_CONFIRMED:
                return ClaimEvaluation(
                    ClaimDecision.PENDING,
                    claim,
                    "a user cannot establish another user's identity through an ordinary claim",
                    current,
                )

        if current:
            current_rank = _AUTHORITY_RANK[current.authority]
            claim_rank = _AUTHORITY_RANK[claim.authority]
            if claim_rank < current_rank:
                return ClaimEvaluation(
                    ClaimDecision.REJECT,
                    claim,
                    "claim authority is weaker than the existing authoritative identity",
                    current,
                )
            if claim_rank == current_rank:
                if current.canonical_name.casefold() == claim.claimed_name.strip().casefold():
                    return ClaimEvaluation(ClaimDecision.ACCEPT, claim, "claim matches existing identity", current)
                return ClaimEvaluation(
                    ClaimDecision.REJECT,
                    claim,
                    "a conflicting claim at the same authority level cannot silently overwrite identity",
                    current,
                )
            if current.authority == AuthorityLevel.VERIFIED:
                return ClaimEvaluation(ClaimDecision.REJECT, claim, "verified identity cannot be overwritten by a claim", current)

        if claim.authority == AuthorityLevel.SELF_CLAIM and target_id == claim.speaker_user_id:
            return ClaimEvaluation(
                ClaimDecision.ESTABLISH,
                claim,
                "self-claim is provisional and may be recorded as a candidate alias/name, not verified truth",
                current,
            )

        if claim.authority == AuthorityLevel.FOUNDER_ADMIN_CONFIRMED or claim.authority == AuthorityLevel.VERIFIED:
            return ClaimEvaluation(
                ClaimDecision.ACCEPT,
                claim,
                "claim carries authoritative confirmation",
                current,
            )

        if claim.authority == AuthorityLevel.ESTABLISHED:
            return ClaimEvaluation(ClaimDecision.ESTABLISH, claim, "claim may reinforce an established identity", current)

        return ClaimEvaluation(ClaimDecision.PENDING, claim, "claim requires stronger identity evidence", current)

    def apply_authoritative_claim(self, claim: IdentityClaim) -> IdentityRecord:
        """Apply only a verified/founder-admin-confirmed claim.

        Low-authority claims must go through evaluate_claim() and are never applied
        by this method.
        """
        evaluation = self.evaluate_claim(claim)
        if evaluation.decision != ClaimDecision.ACCEPT or claim.authority not in {
            AuthorityLevel.VERIFIED,
            AuthorityLevel.FOUNDER_ADMIN_CONFIRMED,
        }:
            raise ValueError("claim is not authoritative enough to mutate identity")

        target_id = claim.target_user_id or claim.speaker_user_id
        current = self.records.get(target_id)
        aliases = tuple(dict.fromkeys((*(current.aliases if current else ()), claim.claimed_name.strip())))
        record = IdentityRecord(
            telegram_user_id=target_id,
            canonical_name=claim.claimed_name.strip(),
            aliases=_normalize_aliases(aliases, claim.claimed_name),
            authority=claim.authority,
        )
        self.records[target_id] = record
        return record


def _normalize_aliases(aliases: tuple[str, ...], canonical_name: str) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in (canonical_name, *aliases):
        cleaned = value.strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return tuple(result)
