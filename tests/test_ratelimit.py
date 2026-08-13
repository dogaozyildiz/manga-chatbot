"""Rate limiting, driven by a fake clock so nothing sleeps."""

from __future__ import annotations

from bot.ratelimit import RateLimiter


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestMinimumGap:
    def test_first_message_allowed(self):
        limiter = RateLimiter(min_seconds=3.0, clock=Clock())
        assert limiter.check("u") is None

    def test_immediate_second_message_refused(self):
        limiter = RateLimiter(min_seconds=3.0, clock=Clock())
        limiter.check("u")
        assert limiter.check("u") is not None

    def test_allowed_again_after_the_gap(self):
        clock = Clock()
        limiter = RateLimiter(min_seconds=3.0, clock=clock)
        limiter.check("u")
        clock.advance(3.1)
        assert limiter.check("u") is None

    def test_users_are_independent(self):
        limiter = RateLimiter(min_seconds=3.0, clock=Clock())
        limiter.check("a")
        assert limiter.check("b") is None


class TestHourlyCap:
    def test_cap_is_enforced(self):
        clock = Clock()
        limiter = RateLimiter(min_seconds=0.0, per_hour=3, clock=clock)
        for _ in range(3):
            assert limiter.check("u") is None
        assert limiter.check("u") is not None

    def test_window_rolls_forward(self):
        clock = Clock()
        limiter = RateLimiter(min_seconds=0.0, per_hour=2, clock=clock)
        limiter.check("u")
        limiter.check("u")
        assert limiter.check("u") is not None

        clock.advance(3601)
        assert limiter.check("u") is None

    def test_refused_attempts_do_not_extend_the_penalty(self):
        """Hammering while limited must not push the allowance further out."""
        clock = Clock()
        limiter = RateLimiter(min_seconds=10.0, per_hour=100, clock=clock)
        limiter.check("u")
        for _ in range(5):
            limiter.check("u")

        clock.advance(10.1)
        assert limiter.check("u") is None
