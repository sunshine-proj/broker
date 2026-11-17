"""Signing in, sessions, API tokens and the proxy's check, against the test
database (PRD FR-017), with the real authentication: nothing is overridden."""

from collections.abc import AsyncIterator, Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config.settings import Settings
from app.database import create_session_factory
from app.main import create_app
from app.models.enums import UserRole
from app.repositories.auth import SqlAuthRepository
from app.services.auth import AuthService

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

PASSWORD = "a password for tests"


@pytest.fixture
async def accounts(empty_database: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """An admin and a viewer, and an API token of the viewer's."""
    async with create_session_factory(empty_database)() as session:
        service = AuthService(SqlAuthRepository(session))
        await service.create_user("ada", PASSWORD, UserRole.ADMIN)
        await service.create_user("vic", PASSWORD, UserRole.VIEWER)
        token = await service.issue_token("vic", "script", days=30)
        await session.commit()
    yield {"token": token}


@pytest.fixture
def client(migrated_database: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(migrated_database), base_url="http://testserver") as test_client:
        yield test_client


def login(client: TestClient, username: str, password: str, next_path: str = "/docs"):
    return client.post(
        "/auth/login",
        data={"username": username, "password": password, "next": next_path},
        follow_redirects=False,
    )


async def test_signing_in_opens_the_api_and_its_documentation(
    client: TestClient, accounts: dict
) -> None:
    assert client.get("/api/v1/instruments").status_code == 401
    page = client.get("/docs", follow_redirects=False)
    assert (page.status_code, page.headers["location"]) == (303, "/auth/login?next=/docs")
    assert "Sign in" in client.get("/auth/login").text

    response = login(client, "ADA", PASSWORD, "/api/v1/auth/me")
    assert (response.status_code, response.headers["location"]) == (303, "/api/v1/auth/me")
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie

    assert client.get("/api/v1/auth/me").json() == {"username": "ada", "role": "admin"}
    assert client.get("/api/v1/instruments").status_code == 200
    assert client.get("/openapi.json").json()["info"]["title"]
    assert client.get("/docs").status_code == 200
    # Already signed in: the login page sends the user on.
    assert client.get("/auth/login?next=/docs", follow_redirects=False).status_code == 303


async def test_signing_out_ends_the_session(client: TestClient, accounts: dict) -> None:
    login(client, "vic", PASSWORD)
    session_cookie = client.cookies.get("marketdata_session")

    assert client.post("/auth/logout", follow_redirects=False).status_code == 303
    assert client.get("/api/v1/auth/me").status_code == 401
    # The old cookie is no good either.
    client.cookies.set("marketdata_session", session_cookie)
    assert client.get("/api/v1/auth/me").status_code == 401


async def test_wrong_passwords_lock_the_account(
    client: TestClient, accounts: dict, empty_database: AsyncEngine
) -> None:
    for _ in range(5):
        response = login(client, "vic", "wrong password!")
        assert response.status_code == 401
        assert "Wrong user name or password" in response.text
    # Locked now: the right password is refused too.
    assert login(client, "vic", PASSWORD).status_code == 401
    async with empty_database.connect() as connection:
        locked = await connection.scalar(
            sa.text("SELECT locked_until > now() FROM app_user WHERE username = 'vic'")
        )
    assert locked


async def test_api_tokens_authenticate_scripts(client: TestClient, accounts: dict) -> None:
    headers = {"Authorization": f"Bearer {accounts['token']}"}

    assert client.get("/api/v1/auth/me", headers=headers).json()["username"] == "vic"
    assert client.get("/api/v1/providers", headers=headers).status_code == 200
    wrong = client.get("/api/v1/providers", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401


async def test_disabling_a_user_revokes_everything(
    client: TestClient, accounts: dict, empty_database: AsyncEngine
) -> None:
    login(client, "vic", PASSWORD)
    async with create_session_factory(empty_database)() as session:
        await AuthService(SqlAuthRepository(session)).set_enabled("vic", False)
        await session.commit()

    assert client.get("/api/v1/auth/me").status_code == 401
    headers = {"Authorization": f"Bearer {accounts['token']}"}
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


async def test_a_revoked_token_stops_working(
    client: TestClient, accounts: dict, empty_database: AsyncEngine
) -> None:
    async with create_session_factory(empty_database)() as session:
        await AuthService(SqlAuthRepository(session)).revoke_token("vic", "script")
        await session.commit()

    headers = {"Authorization": f"Bearer {accounts['token']}"}
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


async def test_the_proxy_check_passes_the_user_to_grafana(
    client: TestClient, accounts: dict
) -> None:
    browser = {
        "Accept": "text/html",
        "X-Forwarded-Method": "GET",
        "X-Forwarded-Uri": "/grafana/d/portfolio",
    }
    anonymous = client.get("/auth/verify", headers=browser, follow_redirects=False)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/auth/login?next=/grafana/d/portfolio"
    assert client.get("/auth/verify", headers={"Accept": "application/json"}).status_code == 401

    login(client, "ada", PASSWORD)
    verified = client.get("/auth/verify", headers=browser)
    assert verified.status_code == 204
    assert (verified.headers["x-webauth-user"], verified.headers["x-webauth-role"]) == (
        "ada",
        "Admin",
    )


async def test_logins_from_other_sites_are_refused(client: TestClient, accounts: dict) -> None:
    response = client.post(
        "/auth/login",
        data={"username": "ada", "password": PASSWORD},
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "marketdata_session" not in client.cookies


async def test_old_sessions_are_pruned(
    client: TestClient, accounts: dict, empty_database: AsyncEngine
) -> None:
    login(client, "vic", PASSWORD)
    client.post("/auth/logout")
    async with create_session_factory(empty_database)() as session:
        from datetime import UTC, datetime

        assert await SqlAuthRepository(session).prune_sessions(datetime.now(UTC)) == 1
        await session.commit()
