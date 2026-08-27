"""Shared HTTP retry policy for safe, transient failures."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 4
    initial_backoff_seconds: float = 0.5
    maximum_backoff_seconds: float = 8.0

    def delay(self, retry_number: int) -> float:
        return float(
            min(
                self.maximum_backoff_seconds,
                self.initial_backoff_seconds * (2 ** max(0, retry_number - 1)),
            )
        )


TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
