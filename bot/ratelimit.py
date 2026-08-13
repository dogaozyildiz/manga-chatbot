"""Per-user rate limiting.

manhwa-rag has no auth and no limiter of its own — its README says so plainly
and warns against exposing it with a key attached. This bot *is* that exposure,
so the limiter lives here. The budget being protected is real: the free Groq
tier is 100k tokens a day, shared by every user of the bot.

Two limits, because they stop different things: a minimum gap stops a user
hammering the backend, and an hourly cap stops a patient user draining the
day's quota.

In-process and per-instance, which is the honest scope for a single-process
bot. A multi-replica deployment would need Redis; that is noted rather than
pretended.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        *,
        min_seconds: float = 3.0,
        per_hour: int = 20,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._min_seconds = min_seconds
        self._per_hour = per_hour
        # Injectable so the tests can advance time without sleeping.
        self._clock = clock
        self._history: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> str | None:
        """Return None to allow, or a user-facing reason to refuse.

        A refused call records nothing, so a rate-limited user cannot push
        their own allowance further out by continuing to type.
        """
        now = self._clock()
        history = self._history[key]

        while history and now - history[0] > 3600:
            history.popleft()

        if history and now - history[-1] < self._min_seconds:
            wait = self._min_seconds - (now - history[-1])
            return f"One at a time — give me {wait:.0f}s."

        if len(history) >= self._per_hour:
            return (
                f"That's {self._per_hour} questions this hour, which is the cap "
                "on the free backend quota. Try again a bit later."
            )

        history.append(now)
        return None
