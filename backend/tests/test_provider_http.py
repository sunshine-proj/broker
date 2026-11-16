import logging
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from app.config.logging import configure_logging
from app.providers.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
    RateBudgetExceededError,
)
from app.providers.http import ProviderHttpClient
from app.providers.rate_limit import RateLimiter
from app.schemas.provider_config import ProviderSettings
from tests.providers_support import FakeClock, make_client

pytestmark = pytest.mark.anyio

SECRET = "k3y-that-must-never-leak"
QUERY_AUTH = {"type": "query", "name": "apikey", "env": "EXAMPLE_API_KEY"}
HEADER_AUTH = {
    "type": "header",
    "name": "Authorization",
    "env": "EXAMPLE_TOKEN",
    "prefix": "Bearer ",
}


class Recorder:
    """Answers requests from a list of responses or exceptions, recording each request."""

    def __init__(self, *outcomes: httpx.Response | Exception) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def ok(body: object = None) -> httpx.Response:
    return httpx.Response(200, json=body if body is not None else {"ok": True})


async def test_successful_request_returns_decoded_json() -> None:
    recorder = Recorder(ok({"symbol": "AAPL"}))
    client = make_client(recorder)

    data = await client.get_json("/quotes", {"symbol": "AAPL"})

    assert data == {"symbol": "AAPL"}
    (request,) = recorder.requests
    assert str(request.url) == "https://api.example.test/v1/quotes?symbol=AAPL"


async def test_json_numbers_keep_their_precision() -> None:
    body = b'{"price": 0.1000000000000000055511, "volume": 12, "text": "1.5"}'
    client = make_client(Recorder(httpx.Response(200, content=body)))

    data = await client.get_json("/quote")

    assert data["price"] == Decimal("0.1000000000000000055511")
    assert data["volume"] == 12
    assert data["text"] == "1.5"


async def test_query_authentication_adds_the_key_parameter() -> None:
    recorder = Recorder(ok())
    client = make_client(recorder, credential=SECRET, auth=QUERY_AUTH)

    await client.get_json("/quotes", {"symbol": "AAPL"})

    assert dict(recorder.requests[0].url.params) == {"symbol": "AAPL", "apikey": SECRET}


async def test_header_authentication_adds_the_header_with_prefix() -> None:
    recorder = Recorder(ok())
    client = make_client(recorder, credential=SECRET, auth=HEADER_AUTH)

    await client.get_json("/quotes")

    request = recorder.requests[0]
    assert request.headers["Authorization"] == f"Bearer {SECRET}"
    assert SECRET not in str(request.url)


async def test_missing_credential_is_a_configuration_error() -> None:
    with pytest.raises(ProviderConfigurationError, match="EXAMPLE_API_KEY is not set"):
        make_client(Recorder(ok()), auth=QUERY_AUTH)


async def test_plain_http_base_url_is_refused() -> None:
    settings = ProviderSettings()
    with pytest.raises(ProviderConfigurationError, match="https"):
        ProviderHttpClient(
            provider="example",
            base_url="http://api.example.test",
            settings=settings,
            credential=None,
            limiter=RateLimiter("example", (), max_wait_seconds=0),
            http=httpx.AsyncClient(),
        )


async def test_server_error_is_retried_with_exponential_backoff() -> None:
    clock = FakeClock()
    recorder = Recorder(httpx.Response(500), httpx.Response(503), httpx.Response(502), ok())
    client = make_client(recorder, clock=clock, max_retries=3, backoff_base_seconds=1)

    assert await client.get_json("/quotes") == {"ok": True}

    assert len(recorder.requests) == 4
    assert clock.sleeps == [1, 2, 4]


async def test_backoff_is_capped() -> None:
    clock = FakeClock()
    recorder = Recorder(*[httpx.Response(500)] * 5, ok())
    client = make_client(
        recorder, clock=clock, max_retries=5, backoff_base_seconds=1, backoff_max_seconds=5
    )

    await client.get_json("/quotes")

    assert clock.sleeps == [1, 2, 4, 5, 5]


async def test_gives_up_after_max_retries() -> None:
    clock = FakeClock()
    recorder = Recorder(httpx.Response(500))
    client = make_client(recorder, clock=clock, max_retries=2)

    with pytest.raises(ProviderUnavailableError, match="HTTP 500"):
        await client.get_json("/quotes")

    assert len(recorder.requests) == 3
    assert len(clock.sleeps) == 2


async def test_timeout_is_retried_then_reported() -> None:
    recorder = Recorder(httpx.ReadTimeout("timed out"))
    client = make_client(recorder, max_retries=1)

    with pytest.raises(ProviderUnavailableError, match="timed out"):
        await client.get_json("/quotes")

    assert len(recorder.requests) == 2


async def test_connection_failure_recovers_on_retry() -> None:
    recorder = Recorder(httpx.ConnectError("refused"), ok())
    client = make_client(recorder)

    assert await client.get_json("/quotes") == {"ok": True}


async def test_configured_timeout_is_applied() -> None:
    recorder = Recorder(ok())
    client = make_client(recorder, timeout_seconds=7.5)

    await client.get_json("/quotes")

    assert recorder.requests[0].extensions["timeout"]["read"] == 7.5


async def test_rate_limited_response_honors_retry_after() -> None:
    clock = FakeClock()
    recorder = Recorder(httpx.Response(429, headers={"Retry-After": "12"}), ok())
    client = make_client(recorder, clock=clock, backoff_base_seconds=1)

    await client.get_json("/quotes")

    assert clock.sleeps == [12]


async def test_rate_limited_without_retry_after_uses_backoff() -> None:
    clock = FakeClock()
    recorder = Recorder(httpx.Response(429), ok())
    client = make_client(recorder, clock=clock, backoff_base_seconds=2)

    await client.get_json("/quotes")

    assert clock.sleeps == [2]


async def test_long_retry_after_is_not_retried() -> None:
    clock = FakeClock()
    recorder = Recorder(httpx.Response(429, headers={"Retry-After": "3600"}))
    client = make_client(recorder, clock=clock, backoff_max_seconds=30)

    with pytest.raises(ProviderRateLimitedError) as raised:
        await client.get_json("/quotes")

    assert raised.value.retry_after == 3600
    assert len(recorder.requests) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize(
    ("status", "error"),
    [(400, ProviderResponseError), (404, ProviderResponseError), (401, ProviderAuthError)],
)
async def test_client_errors_are_not_retried(status: int, error: type[Exception]) -> None:
    recorder = Recorder(httpx.Response(status))
    client = make_client(recorder)

    with pytest.raises(error):
        await client.get_json("/quotes")

    assert len(recorder.requests) == 1


async def test_invalid_json_is_a_response_error() -> None:
    recorder = Recorder(httpx.Response(200, content=b"<html>maintenance</html>"))
    client = make_client(recorder)

    with pytest.raises(ProviderResponseError, match="not valid JSON"):
        await client.get_json("/quotes")

    assert len(recorder.requests) == 1


async def test_every_attempt_uses_rate_limit_credits() -> None:
    clock = FakeClock()
    recorder = Recorder(httpx.Response(500), httpx.Response(500), ok())
    client = make_client(
        recorder,
        clock=clock,
        backoff_base_seconds=1,
        rate_limits=[{"limit": 2, "period": "minute"}],
    )

    await client.get_json("/quotes")

    # Two attempts fit the limit. The third had to wait for the window:
    # 1s backoff, then 2s backoff, then the 57s left of the first request's minute.
    assert clock.sleeps == [1, 2, 57]


async def test_retries_stop_when_the_rate_budget_is_exhausted() -> None:
    recorder = Recorder(httpx.Response(500))
    client = make_client(
        recorder,
        max_retries=5,
        rate_limits=[{"limit": 2, "period": "day"}],
        rate_limit_max_wait_seconds=60,
    )

    with pytest.raises(RateBudgetExceededError):
        await client.get_json("/quotes")

    assert len(recorder.requests) == 2


async def test_request_cost_is_charged_to_the_limiter() -> None:
    clock = FakeClock()
    client = make_client(
        Recorder(ok()), clock=clock, rate_limits=[{"limit": 8, "period": "minute"}]
    )

    await client.get_json("/quotes", cost=5)
    await client.get_json("/quotes", cost=5)

    assert clock.sleeps == [60]


async def test_api_key_never_appears_in_errors_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    recorder = Recorder(
        httpx.Response(500), httpx.ConnectError(f"failed for ?apikey={SECRET}"), httpx.Response(400)
    )
    client = make_client(recorder, credential=SECRET, auth=QUERY_AUTH)

    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderResponseError) as raised:
        await client.get_json("/quotes")

    assert len(recorder.requests) == 3
    assert SECRET not in str(raised.value)
    assert SECRET not in caplog.text
    for record in caplog.records:
        assert SECRET not in str(record.__dict__)


def test_httpx_request_logging_is_silenced() -> None:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        configure_logging("DEBUG", "json")

        # httpx logs each request URL, query string included, at INFO.
        assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    finally:
        root.handlers, root.level = handlers, level
        logging.getLogger("httpx").setLevel(logging.NOTSET)


def test_credential_is_masked_in_repr() -> None:
    assert SECRET not in repr(SecretStr(SECRET))


MINUTE_RESET = [{"limit": 8, "period": "minute", "resets_on_boundary": True}]


async def test_rate_limited_waits_for_the_providers_next_minute() -> None:
    # The fake wall clock starts at 09:30:15; the allowance returns at 09:31:00.
    clock = FakeClock()
    recorder = Recorder(httpx.Response(429), ok())
    client = make_client(
        recorder,
        clock=clock,
        backoff_base_seconds=1,
        backoff_max_seconds=65,
        rate_limits=MINUTE_RESET,
    )

    await client.get_json("/quotes")

    assert clock.sleeps == [46]
    assert len(recorder.requests) == 2


async def test_after_a_rate_limit_other_requests_wait_too() -> None:
    clock = FakeClock()
    recorder = Recorder(httpx.Response(429), ok())
    client = make_client(
        recorder,
        clock=clock,
        max_retries=0,
        rate_limit_max_wait_seconds=65,
        rate_limits=MINUTE_RESET,
    )

    with pytest.raises(ProviderRateLimitedError):
        await client.get_json("/quotes", {"symbol": "AAPL"})
    await client.get_json("/quotes", {"symbol": "MSFT"})

    # The second request was not sent until the provider's minute had turned.
    assert clock.sleeps == [46]
    assert [r.url.params["symbol"] for r in recorder.requests] == ["AAPL", "MSFT"]


async def test_exhausted_daily_allowance_is_not_retried_and_stops_further_requests() -> None:
    clock = FakeClock()
    recorder = Recorder(httpx.Response(429))
    client = make_client(
        recorder,
        clock=clock,
        backoff_max_seconds=65,
        rate_limits=[{"limit": 800, "period": "day", "resets_on_boundary": True}],
    )

    with pytest.raises(ProviderRateLimitedError) as raised:
        await client.get_json("/quotes")
    with pytest.raises(RateBudgetExceededError):
        await client.get_json("/quotes")

    # From 09:30:15 to midnight UTC, plus the one-second margin.
    assert raised.value.retry_after == pytest.approx(14 * 3600 + 29 * 60 + 45 + 1)
    assert len(recorder.requests) == 1
    assert clock.sleeps == []


async def test_retry_after_header_takes_precedence_over_the_boundary() -> None:
    clock = FakeClock()
    recorder = Recorder(httpx.Response(429, headers={"Retry-After": "5"}), ok())
    client = make_client(recorder, clock=clock, backoff_base_seconds=1, rate_limits=MINUTE_RESET)

    await client.get_json("/quotes")

    assert clock.sleeps == [5]
