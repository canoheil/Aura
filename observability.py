"""Lightweight, dependency-free observability helpers for Aura."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestTrace:
    request_id: str
    started_at: float

    @classmethod
    def start(cls) -> "RequestTrace":
        return cls(uuid.uuid4().hex[:12], time.monotonic())

    @property
    def latency_ms(self) -> float:
        return (time.monotonic() - self.started_at) * 1000.0
