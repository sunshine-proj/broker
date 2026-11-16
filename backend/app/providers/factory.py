"""Builds provider adapters from their database configuration."""

import httpx

from app.config.settings import Settings
from app.models import Provider as ProviderRow
from app.providers.base import Provider
from app.providers.errors import ProviderConfigurationError
from app.providers.http import ProviderHttpClient
from app.providers.rate_limit import RateLimiter
from app.schemas.provider_config import AuthType, ProviderSettings

# Adapter classes by provider name. Each adapter module registers itself.
ADAPTERS: dict[str, type[Provider]] = {}


def register_adapter[T: type[Provider]](adapter: T) -> T:
    """Class decorator that makes an adapter available to the factory."""
    ADAPTERS[adapter.name] = adapter
    return adapter


class ProviderFactory:
    """Creates adapters that share one HTTP connection pool.

    Keep a single factory per process: it owns each provider's rate limiter,
    and the limits hold only if every request goes through the same one.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        adapters: dict[str, type[Provider]] | None = None,
    ) -> None:
        self._settings = settings
        self._http = http or httpx.AsyncClient(follow_redirects=False)
        self._adapters = ADAPTERS if adapters is None else adapters
        self._limiters: dict[str, RateLimiter] = {}

    def build(self, row: ProviderRow) -> Provider:
        adapter = self._adapters.get(row.name)
        if adapter is None:
            raise ProviderConfigurationError(row.name, "no adapter is implemented")
        if not row.enabled:
            raise ProviderConfigurationError(row.name, "provider is disabled")

        config = ProviderSettings.model_validate(row.configuration)
        credential = None
        if config.auth.type is not AuthType.NONE and config.auth.env is not None:
            credential = self._settings.credential(config.auth.env)

        limiter = self._limiters.get(row.name)
        if limiter is None:
            limiter = self._limiters[row.name] = RateLimiter(
                row.name, config.rate_limits, max_wait_seconds=config.rate_limit_max_wait_seconds
            )

        client = ProviderHttpClient(
            provider=row.name,
            base_url=row.base_url,
            settings=config,
            credential=credential,
            limiter=limiter,
            http=self._http,
        )
        return adapter(client)

    async def aclose(self) -> None:
        await self._http.aclose()
