"""Non-secret provider settings, stored in `provider.configuration`."""

import re
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Only variables that are recognisably provider credentials may be referenced,
# so configuration cannot be used to send some other secret to a provider.
_CREDENTIAL_ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*_(API_KEY|TOKEN)$")


class RatePeriod(StrEnum):
    SECOND = "second"
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    MONTH = "month"

    @property
    def seconds(self) -> int:
        return _PERIOD_SECONDS[self]


_PERIOD_SECONDS = {
    RatePeriod.SECOND: 1,
    RatePeriod.MINUTE: 60,
    RatePeriod.HOUR: 3600,
    RatePeriod.DAY: 86400,
    # A rolling 31 days never allows more than a calendar month does.
    RatePeriod.MONTH: 31 * 86400,
}


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RateLimit(_ConfigModel):
    """At most `limit` request credits per `period`."""

    limit: int = Field(gt=0)
    period: RatePeriod


class AuthType(StrEnum):
    NONE = "none"
    # Credential sent as a query parameter called `name`.
    QUERY = "query"
    # Credential sent as a header called `name`.
    HEADER = "header"


class ProviderAuth(_ConfigModel):
    type: AuthType = AuthType.NONE
    # Query parameter or header name.
    name: str | None = None
    # Name of the environment variable holding the credential. Never the credential itself.
    env: str | None = None
    # Text put before the credential, for example "Bearer ".
    prefix: str = ""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.type is AuthType.NONE:
            return self
        if not self.name or not self.env:
            raise ValueError(f"auth type '{self.type}' needs both 'name' and 'env'")
        if not _CREDENTIAL_ENV_PATTERN.match(self.env):
            raise ValueError(
                "auth 'env' must be an upper-case variable name ending in _API_KEY or _TOKEN"
            )
        return self


class ProviderSettings(_ConfigModel):
    timeout_seconds: float = Field(default=10.0, gt=0)
    # Retries after the first attempt, for transient failures only.
    max_retries: int = Field(default=3, ge=0, le=10)
    backoff_base_seconds: float = Field(default=1.0, gt=0)
    backoff_max_seconds: float = Field(default=30.0, gt=0)
    # Longest a request may wait for rate-limit capacity before it is refused.
    rate_limit_max_wait_seconds: float = Field(default=60.0, ge=0)
    rate_limits: tuple[RateLimit, ...] = ()
    # Most symbols an adapter may put in one batched request. None means no cap.
    max_batch_size: int | None = Field(default=None, gt=0)
    auth: ProviderAuth = ProviderAuth()
