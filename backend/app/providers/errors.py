"""Errors raised by provider adapters and the shared HTTP client.

Messages never contain credentials or full request URLs.
"""


class ProviderError(Exception):
    """Base class. `transient` errors are worth retrying."""

    transient = False

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message


class ProviderUnavailableError(ProviderError):
    """Timeout, connection failure or a 5xx response."""

    transient = True


class ProviderRateLimitedError(ProviderError):
    """The provider answered 429."""

    transient = True

    def __init__(self, provider: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(provider, message)
        self.retry_after = retry_after


class RateBudgetExceededError(ProviderError):
    """Our own rate limiter refused the request; nothing was sent."""

    def __init__(self, provider: str, message: str, retry_after: float) -> None:
        super().__init__(provider, message)
        self.retry_after = retry_after


class ProviderAuthError(ProviderError):
    """The provider rejected the credential (401 or 403)."""


class ProviderResponseError(ProviderError):
    """An unexpected status, or a body that is not what the adapter expects.

    `status_code` and `body` (the decoded JSON, if there was any) let an adapter
    recognise provider-specific answers, such as "no data for these dates".
    They are deliberately not part of the message.
    """

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
        body: object = None,
    ) -> None:
        super().__init__(provider, message)
        self.status_code = status_code
        self.body = body


class ProviderConfigurationError(ProviderError):
    """The provider cannot be used as configured, for example a missing credential."""


class UnsupportedOperationError(ProviderError):
    """The provider does not offer this kind of data."""
