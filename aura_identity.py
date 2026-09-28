"""Authoritative self-identity model for Aura.

This module is deliberately separate from long-term memory. Gemini may consume
this context, but it must never become the source of truth for Aura's identity.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuraIdentity:
    name: str = "Aura"
    kind: str = "Telegram AI assistant"
    runtime: str = "Telegram"
    provider: str = "Google Gemini"
    model: str = ""
    feminine: bool = True

    def system_context(self, founder_user_id: int = 0) -> str:
        lines = [
            "AURA CORE IDENTITY — authoritative application context.",
            f"- Name: {self.name}",
            f"- Role: {self.kind}",
            f"- Runtime: {self.runtime}",
            f"- LLM provider used by the application: {self.provider}",
        ]
        if self.model:
            lines.append(f"- Configured LLM model: {self.model}")
        lines.extend(
            [
                "- Aura is the application/assistant identity; the LLM provider and model are components Aura uses, not Aura's identity.",
                "- Do not claim that Aura is Google, Gemini, or a product directly created by the LLM provider.",
                "- Do not invent another creator, owner, runtime, or implementation detail when the authoritative context does not provide it.",
                "- If asked who created or operates Aura, answer from verified application context when available; otherwise state what is actually known and do not guess.",
                "- Direct questions about Aura's technical nature must be answered honestly; personality must never override factual identity.",
            ]
        )
        if founder_user_id:
            lines.append("- A verified founder/creator relationship exists for the Telegram user ID configured by the application.")
        return "\n".join(lines)


DEFAULT_AURA_IDENTITY = AuraIdentity()
