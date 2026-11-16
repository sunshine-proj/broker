import uuid
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ProviderHealth
from app.models.enums import ProviderHealthStatus

# Stored error text is capped; provider messages can be arbitrarily long.
_MAX_ERROR_LENGTH = 1000


class SqlProviderHealthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_success(self, provider_id: uuid.UUID, at: datetime) -> None:
        values = {
            "status": ProviderHealthStatus.HEALTHY,
            "last_success_at": at,
            "consecutive_failures": 0,
            "updated_at": at,
        }
        statement = (
            insert(ProviderHealth)
            .values(provider_id=provider_id, **values)
            .on_conflict_do_update(index_elements=[ProviderHealth.provider_id], set_=values)
        )
        await self._session.execute(statement)

    async def record_failure(
        self, provider_id: uuid.UUID, at: datetime, error: str, *, down_after: int
    ) -> None:
        """Count one more failure in a row; `down_after` of them mark the provider down."""
        error = error[:_MAX_ERROR_LENGTH]
        failures = ProviderHealth.consecutive_failures + 1
        statement = (
            insert(ProviderHealth)
            .values(
                provider_id=provider_id,
                status=_status_for(1, down_after),
                last_error_at=at,
                last_error=error,
                consecutive_failures=1,
                updated_at=at,
            )
            .on_conflict_do_update(
                index_elements=[ProviderHealth.provider_id],
                set_={
                    "status": sa.case(
                        (failures >= down_after, ProviderHealthStatus.DOWN.value),
                        else_=ProviderHealthStatus.DEGRADED.value,
                    ),
                    "last_error_at": at,
                    "last_error": error,
                    "consecutive_failures": failures,
                    "updated_at": at,
                },
            )
        )
        await self._session.execute(statement)

    async def get(self, provider_id: uuid.UUID) -> ProviderHealth | None:
        # populate_existing: the upserts above bypass the session's identity map.
        return await self._session.get(ProviderHealth, provider_id, populate_existing=True)

    async def list(self) -> Sequence[ProviderHealth]:
        statement = sa.select(ProviderHealth).execution_options(populate_existing=True)
        return (await self._session.execute(statement)).scalars().all()


def _status_for(failures: int, down_after: int) -> ProviderHealthStatus:
    return ProviderHealthStatus.DOWN if failures >= down_after else ProviderHealthStatus.DEGRADED
