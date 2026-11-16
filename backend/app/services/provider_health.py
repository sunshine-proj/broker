"""Provider health monitoring and failure isolation."""

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.providers.errors import ProviderError
from app.repositories.interfaces import ProviderHealthRepository

logger = logging.getLogger(__name__)

# Consecutive failures after which a provider is reported as down, not degraded.
DOWN_AFTER_FAILURES = 3


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ProviderHealthService:
    def __init__(
        self,
        repository: ProviderHealthRepository,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repository = repository
        self._clock = clock

    async def record_success(self, provider_id: uuid.UUID) -> None:
        await self._repository.record_success(provider_id, self._clock())

    async def record_failure(self, provider_id: uuid.UUID, error: str) -> None:
        await self._repository.record_failure(
            provider_id, self._clock(), error, down_after=DOWN_AFTER_FAILURES
        )

    async def guard[T](
        self, provider_id: uuid.UUID, operation: Callable[[], Awaitable[T]]
    ) -> T | None:
        """Run a provider call, record the outcome, and contain a provider failure.

        Returns the result, or None if the provider failed. A provider error is
        logged and recorded but never propagates, so one provider cannot stop
        the others. Any other exception is a bug and is not swallowed.
        """
        try:
            result = await operation()
        except ProviderError as exc:
            logger.warning(
                "Provider call failed",
                extra={"provider": exc.provider, "error": exc.message, "kind": type(exc).__name__},
            )
            await self.record_failure(provider_id, f"{type(exc).__name__}: {exc.message}")
            return None
        await self.record_success(provider_id)
        return result
