"""Authentication without a database (PRD FR-017): hashing, lockout, redirects,
and that every protected route refuses anonymous requests."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.auth import safe_next
from app.config.settings import Settings
from app.database import get_session
from app.main import create_app
from app.models.enums import UserRole
from app.services.auth import (
    AuthError,
    AuthPolicy,
    AuthService,
    hash_password,
    token_hash,
    verify_password,
)
from tests.conftest import FakeSession

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def test_passwords_are_salted_scrypt_hashes() -> None:
    first, second = hash_password("correct horse battery"), hash_password("correct horse battery")

    assert first.startswith("scrypt$32768$8$3$")
    assert first != second
    assert verify_password("correct horse battery", first)
    assert not verify_password("correct horse batter", first)
    assert not verify_password("anything", "not-a-hash")


def test_tokens_are_stored_as_sha256() -> None:
    assert len(token_hash("abc")) == 64
    assert token_hash("abc") != token_hash("abd")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/grafana/d/x?var=1", "/grafana/d/x?var=1"),
        (None, "/home"),
        ("https://evil.example/", "/home"),
        ("//evil.example/", "/home"),
        ("/\\evil.example", "/home"),
        ("grafana", "/home"),
    ],
)
def test_only_local_paths_are_returned_to(value: str | None, expected: str) -> None:
    assert safe_next(value, "/home") == expected


# --- The service, with an in-memory repository ------------------------------------------------


@dataclass
class User:
    username: str
    password_hash: str
    role: UserRole = UserRole.VIEWER
    enabled: bool = True
    failed_logins: int = 0
    locked_until: datetime | None = None
    last_login_at: datetime | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


class FakeRepository:
    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.sessions: dict[str, tuple[uuid.UUID, datetime, bool]] = {}
        self.revoked_users: list[tuple[uuid.UUID, bool]] = []

    async def get_user(self, username: str) -> User | None:
        return self.users.get(username)

    async def create_user(self, *, username: str, password_hash: str, role: UserRole) -> uuid.UUID:
        self.users[username] = User(username, password_hash, role)
        return self.users[username].id

    async def update_user(self, user_id: uuid.UUID, **values: Any) -> None:
        for name, user in self.users.items():
            if user.id == user_id:
                self.users[name] = replace(user, **values)

    async def add_session(
        self, *, user_id: uuid.UUID, token_hash: str, expires_at: datetime
    ) -> None:
        self.sessions[token_hash] = (user_id, expires_at, False)

    async def session_user(self, token_hash: str, now: datetime) -> User | None:
        found = self.sessions.get(token_hash)
        if found is None or found[2] or found[1] <= now:
            return None
        return next(u for u in self.users.values() if u.id == found[0] and u.enabled)

    async def revoke_session(self, token_hash: str, now: datetime) -> None:
        user_id, expires, _ = self.sessions[token_hash]
        self.sessions[token_hash] = (user_id, expires, True)

    async def revoke_credentials(
        self, user_id: uuid.UUID, now: datetime, *, tokens: bool = True
    ) -> None:
        self.revoked_users.append((user_id, tokens))

    async def list_tokens(self) -> list[dict[str, Any]]:
        return []


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def setup() -> tuple[AuthService, FakeRepository, Clock]:
    repository, clock = FakeRepository(), Clock()
    return (
        AuthService(repository, AuthPolicy(max_failures=3, lockout_minutes=15), clock),
        repository,
        clock,
    )


pytestmark = pytest.mark.anyio


async def test_a_user_signs_in_and_gets_a_session(setup: tuple) -> None:
    service, repository, clock = setup
    await service.create_user("Alice", "a long password", UserRole.ADMIN)

    user = await service.authenticate("alice", "a long password")
    assert user is not None and user.role is UserRole.ADMIN
    assert repository.users["alice"].last_login_at == NOW
    token, expires = await service.start_session(user)
    assert expires == NOW + timedelta(hours=12)
    assert token_hash(token) in repository.sessions and token not in repository.sessions
    assert (await service.session_user(token)).username == "alice"

    clock.now = expires
    assert await service.session_user(token) is None


async def test_repeated_failures_lock_the_account(setup: tuple) -> None:
    service, repository, clock = setup
    await service.create_user("bob", "a long password", UserRole.VIEWER)

    for _ in range(3):
        assert await service.authenticate("bob", "wrong password") is None
    assert repository.users["bob"].locked_until == NOW + timedelta(minutes=15)
    # Locked: even the right password fails, until the lock expires.
    assert await service.authenticate("bob", "a long password") is None
    clock.now = NOW + timedelta(minutes=16)
    assert await service.authenticate("bob", "a long password") is not None
    assert repository.users["bob"].failed_logins == 0


async def test_disabled_and_unknown_users_cannot_sign_in(setup: tuple) -> None:
    service, repository, _ = setup
    await service.create_user("carol", "a long password", UserRole.VIEWER)
    await service.set_enabled("carol", False)

    assert await service.authenticate("carol", "a long password") is None
    assert await service.authenticate("nobody", "a long password") is None
    assert repository.revoked_users == [(repository.users["carol"].id, True)]


async def test_a_new_password_ends_sessions_but_keeps_tokens(setup: tuple) -> None:
    service, repository, _ = setup
    await service.create_user("dave", "a long password", UserRole.VIEWER)
    await service.set_password("dave", "another long password")

    assert repository.revoked_users == [(repository.users["dave"].id, False)]
    assert await service.authenticate("dave", "another long password") is not None


@pytest.mark.parametrize(
    ("username", "password", "message"),
    [
        ("eve", "short", "at least 12"),
        ("x", "a long password", "user name"),
        ("bad name", "a long password", "user name"),
    ],
)
async def test_weak_passwords_and_bad_names_are_refused(
    setup: tuple, username: str, password: str, message: str
) -> None:
    service, _, _ = setup
    with pytest.raises(AuthError, match=message):
        await service.create_user(username, password, UserRole.VIEWER)


# --- Routes -------------------------------------------------------------------------------------

PUBLIC = {"/api/v1/health"}


def test_every_api_route_but_health_needs_a_user(settings: Settings) -> None:
    app = create_app(settings)
    app.dependency_overrides[get_session] = lambda: FakeSession()
    operations = [
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        for method in item
        if path not in PUBLIC
    ]
    assert len(operations) > 25

    with TestClient(app, raise_server_exceptions=False) as client:
        for method, path in operations:
            # No cookie and no token: the check answers before any query runs.
            response = client.request(method, path.replace("{", "").replace("}", ""))
            assert response.status_code == 401, (method, path)
            assert response.json()["error"]["code"] == "authentication_required"
            assert response.headers["www-authenticate"] == "Bearer"
        # The documentation too: pages go to the login page.
        assert client.get("/openapi.json").status_code == 401
        docs = client.get("/docs", follow_redirects=False)
        assert docs.headers["location"] == "/auth/login?next=/docs"
        assert client.get("/api/v1/health").status_code in (200, 503)
