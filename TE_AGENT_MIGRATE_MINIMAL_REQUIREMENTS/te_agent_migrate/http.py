"""Shared HTTP retry policy for safe, transient failures."""


class RetryPolicy(object):
    def __init__(
        self,
        attempts=4,
        initial_backoff_seconds=0.5,
        maximum_backoff_seconds=8.0,
    ):
        self.attempts = attempts
        self.initial_backoff_seconds = initial_backoff_seconds
        self.maximum_backoff_seconds = maximum_backoff_seconds

    def delay(self, retry_number):
        return float(
            min(
                self.maximum_backoff_seconds,
                self.initial_backoff_seconds * (2 ** max(0, retry_number - 1)),
            )
        )


TRANSIENT_STATUS_CODES = frozenset((429, 500, 502, 503, 504))
