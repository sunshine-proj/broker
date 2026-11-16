"""Shared HTTP client for provider adapters.

Applies, for every request: authentication, the provider's rate limiter, a
timeout, and retries with exponential backoff for transient failures. Each
attempt, including a retry, takes credits from the rate limiter.

When the provider answers HTTP 429, the whole limiter is paused until the
provider's allowance is expected back, so no other request is wasted on it.
"""

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import SecretStr

from app.providers.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from app.providers.rate_limit import RateLimiter
from app.schemas.provider_config import AuthType, ProviderSettings

logger = logging.getLogger(__name__)


# Extra wait past a provider's reset boundary, for clock differences.
_BOUNDARY_MARGIN_SECONDS = 1.0


def _default_jitter() -> float:
    return random.uniform(0.5, 1.0)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ProviderHttpClient:
    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        settings: ProviderSettings,
        credential: SecretStr | None,
        limiter: RateLimiter,
        http: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = _default_jitter,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ProviderConfigurationError(provider, "base_url must use https")
        if (
            settings.auth.type is not AuthType.NONE
            and credential is None
            and not settings.auth.optional
        ):
            raise ProviderConfigurationError(
                provider, f"environment variable {settings.auth.env} is not set"
            )
        self.provider = provider
        self.settings = settings
        self._base_url = base_url.rstrip("/")
        self._settings = settings
        self._credential = credential
        self._limiter = limiter
        self._http = http
        self._sleep = sleep
        self._jitter = jitter
        self._wall_clock = wall_clock

    async def get_json(
        self, path: str, params: Mapping[str, Any] | None = None, *, cost: int = 1
    ) -> Any:
        """GET `path` and return the decoded JSON body.

        JSON numbers with a fraction are decoded as Decimal, never float.
        `cost` is the number of rate-limit credits the request uses.
        """
        url = f"{self._base_url}/{path.lstrip('/')}"
        query, headers = self._authenticate(dict(params or {}))
        attempts = self._settings.max_retries + 1

        for attempt in range(attempts):
            await self._limiter.acquire(cost)
            error: ProviderError
            try:
                response = await self._http.get(
                    url, params=query, headers=headers, timeout=self._settings.timeout_seconds
                )
            except httpx.TimeoutException:
                error = ProviderUnavailableError(self.provider, "request timed out")
            except httpx.TransportError as exc:
                # The exception text is not used: it can contain the request URL.
                error = ProviderUnavailableError(
                    self.provider, f"connection failed ({type(exc).__name__})"
                )
            else:
                failure = self._classify(response)
                if failure is None:
                    return self._decode(response)
                error = failure

            if isinstance(error, ProviderRateLimitedError):
                error.retry_after = self._reset_in(error)
                if error.retry_after is not None:
                    self._limiter.pause(error.retry_after)

            delay = self._retry_delay(error, attempt)
            if delay is None or attempt == attempts - 1:
                raise error
            logger.warning(
                "Provider request failed; retrying",
                extra={
                    "provider": self.provider,
                    "path": path,
                    "attempt": attempt + 1,
                    "error": error.message,
                    "retry_in_seconds": round(delay, 2),
                },
            )
            await self._sleep(delay)

        raise AssertionError("unreachable")  # pragma: no cover

    def _authenticate(self, params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        auth = self._settings.auth
        headers: dict[str, str] = {}
        if auth.type is AuthType.NONE or self._credential is None or auth.name is None:
            return params, headers
        value = auth.prefix + self._credential.get_secret_value()
        if auth.type is AuthType.QUERY:
            params[auth.name] = value
        else:
            headers[auth.name] = value
        return params, headers

    def _classify(self, response: httpx.Response) -> ProviderError | None:
        status = response.status_code
        if 200 <= status < 300:
            return None
        if status == 429:
            return ProviderRateLimitedError(
                self.provider,
                "provider rate limit reached (HTTP 429)",
                retry_after=_parse_retry_after(response.headers.get("Retry-After")),
            )
        if status >= 500:
            return ProviderUnavailableError(self.provider, f"server error (HTTP {status})")
        try:
            body = json.loads(response.text, parse_float=Decimal)
        except ValueError:
            body = None
        if status in (401, 403):
            return ProviderAuthError(
                self.provider, f"credential rejected (HTTP {status})", body=body
            )
        return ProviderResponseError(
            self.provider, f"unexpected response (HTTP {status})", status_code=status, body=body
        )

    def _decode(self, response: httpx.Response) -> Any:
        try:
            return json.loads(response.text, parse_float=Decimal)
        except ValueError as exc:
            raise ProviderResponseError(self.provider, "response body is not valid JSON") from exc

    def _reset_in(self, error: ProviderRateLimitedError) -> float | None:
        """Seconds until the provider's allowance is expected back, if that can be known."""
        if error.retry_after is not None:
            return error.retry_after
        now = self._wall_clock()
        boundaries = [
            limit.period.next_boundary(now)
            for limit in self._settings.rate_limits
            if limit.resets_on_boundary
        ]
        if not boundaries:
            return None
        return (min(boundaries) - now).total_seconds() + _BOUNDARY_MARGIN_SECONDS

    def _retry_delay(self, error: ProviderError, attempt: int) -> float | None:
        """Seconds to wait before retrying, or None if the error must not be retried."""
        if not error.transient:
            return None
        settings = self._settings
        backoff = min(settings.backoff_max_seconds, settings.backoff_base_seconds * 2**attempt)
        delay = backoff * self._jitter()
        if isinstance(error, ProviderRateLimitedError) and error.retry_after is not None:
            # The provider said how long to stay away. Longer than we are
            # willing to wait means giving up, not retrying early.
            if error.retry_after > settings.backoff_max_seconds:
                return None
            delay = max(delay, error.retry_after)
        return delay


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either a number of seconds or an HTTP date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())
