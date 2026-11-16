import pytest

from app.providers.errors import RateBudgetExceededError
from app.providers.rate_limit import RateLimiter
from app.schemas.provider_config import RateLimit, RatePeriod
from tests.providers_support import FakeClock

pytestmark = pytest.mark.anyio


def limiter(clock: FakeClock, *limits: tuple[int, str], max_wait: float = 60.0) -> RateLimiter:
    return RateLimiter(
        "example",
        [RateLimit(limit=limit, period=RatePeriod(period)) for limit, period in limits],
        max_wait_seconds=max_wait,
        clock=clock,
        sleep=clock.sleep,
    )


async def test_requests_within_the_limit_do_not_wait() -> None:
    clock = FakeClock()
    rate_limiter = limiter(clock, (3, "minute"))

    for _ in range(3):
        await rate_limiter.acquire()

    assert clock.sleeps == []


async def test_request_over_the_limit_waits_until_the_window_frees() -> None:
    clock = FakeClock()
    rate_limiter = limiter(clock, (2, "minute"))
    await rate_limiter.acquire()
    clock.now += 20
    await rate_limiter.acquire()

    await rate_limiter.acquire()

    # The first request leaves the 60-second window 40 seconds from now.
    assert clock.sleeps == [40]


async def test_no_more_than_the_limit_in_any_window() -> None:
    clock = FakeClock()
    rate_limiter = limiter(clock, (5, "second"), max_wait=1000)
    granted_at = []

    for _ in range(23):
        await rate_limiter.acquire()
        granted_at.append(clock.now)

    for start in granted_at:
        assert sum(1 for t in granted_at if start <= t < start + 1) <= 5


async def test_cost_counts_several_credits() -> None:
    clock = FakeClock()
    rate_limiter = limiter(clock, (8, "minute"))
    await rate_limiter.acquire(cost=5)

    await rate_limiter.acquire(cost=5)

    assert clock.sleeps == [60]


async def test_every_limit_is_enforced() -> None:
    clock = FakeClock()
    rate_limiter = limiter(clock, (2, "second"), (3, "day"), max_wait=10)
    await rate_limiter.acquire()
    await rate_limiter.acquire()
    await rate_limiter.acquire()  # waits one second for the per-second limit
    assert clock.sleeps == [1]

    # The per-second limit has room again, but the daily one is used up.
    clock.now += 5
    with pytest.raises(RateBudgetExceededError) as raised:
        await rate_limiter.acquire()

    assert raised.value.retry_after == pytest.approx(86400 - 6)
    assert clock.sleeps == [1]


async def test_exhausted_long_window_is_refused_instead_of_waiting() -> None:
    clock = FakeClock()
    rate_limiter = limiter(clock, (1, "month"), max_wait=60)
    await rate_limiter.acquire()

    with pytest.raises(RateBudgetExceededError, match="rate budget exhausted"):
        await rate_limiter.acquire()

    assert clock.sleeps == []


async def test_refused_request_does_not_use_credits() -> None:
    clock = FakeClock()
    rate_limiter = limiter(clock, (1, "hour"), max_wait=0)
    await rate_limiter.acquire()
    for _ in range(3):
        with pytest.raises(RateBudgetExceededError):
            await rate_limiter.acquire()

    clock.now += 3600
    await rate_limiter.acquire()


async def test_cost_larger_than_a_limit_can_never_be_granted() -> None:
    clock = FakeClock()

    with pytest.raises(RateBudgetExceededError, match="can never fit"):
        await limiter(clock, (8, "minute")).acquire(cost=9)


async def test_no_limits_means_no_waiting() -> None:
    clock = FakeClock()
    rate_limiter = limiter(clock)

    for _ in range(100):
        await rate_limiter.acquire()

    assert clock.sleeps == []
