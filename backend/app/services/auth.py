"""Authentication (PRD FR-017): passwords, logins, sessions and API tokens.

- Passwords are hashed with scrypt (standard library), with the parameters
  stored beside the hash so they can be raised later.
- Session and API tokens are random; only their SHA-256 hash is stored, so a
  copy of the database does not let anyone sign in.
- Nothing secret is logged: no password, token or hash.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.models import AppUser
from app.models.enums import UserRole
from app.repositories.interfaces import AuthRepository

logger = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 12
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
TOKEN_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# OWASP's scrypt baseline: N=2^15, r=8, p=3 (about 32 MiB per hash).
_N, _R, _P = 2**15, 8, 3
_MAXMEM = 64 * 1024 * 1024


class AuthError(Exception):
    """A request the operator made cannot be done; the message says why."""


@dataclass(frozen=True)
class AuthPolicy:
    session_hours: int = 12
    max_failures: int = 5
    lockout_minutes: int = 15


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Verified against when a user name is unknown, so the answer takes as long.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"A password needs at least {MIN_PASSWORD_LENGTH} characters.")


def normalize_username(username: str) -> str:
    name = username.strip().lower()
    if not USERNAME.match(name):
        raise AuthError(
            "A user name is 2 to 64 lower-case letters, digits, '.', '_' or '-', "
            "starting with a letter or digit."
        )
    return name


class AuthService:
    def __init__(
        self,
        repository: AuthRepository,
        policy: AuthPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._policy = policy or AuthPolicy()
        self._clock = clock

    # --- Signing in ---------------------------------------------------------------------

    async def authenticate(self, username: str, password: str) -> AppUser | None:
        """The user, if the name and password are right and the account may sign in.

        Failures count towards a lockout; a locked or disabled account fails
        even with the right password. The caller commits, also on failure.
        """
        now = self._clock()
        user = await self._repository.get_user(username.strip().lower())
        stored = user.password_hash if user is not None else _DUMMY_HASH
        valid = await asyncio.to_thread(verify_password, password, stored)
        if user is None:
            logger.info("Login failed", extra={"reason": "unknown user"})
            return None
        if user.locked_until is not None and user.locked_until > now:
            logger.warning("Login refused: account locked", extra={"user": user.username})
            return None
        if not valid or not user.enabled:
            failures = user.failed_logins + 1 if not valid else user.failed_logins
            locked = None
            if failures >= self._policy.max_failures:
                locked = now + timedelta(minutes=self._policy.lockout_minutes)
                failures = 0
                logger.warning("Account locked after failed logins", extra={"user": user.username})
            await self._repository.update_user(user.id, failed_logins=failures, locked_until=locked)
            logger.info(
                "Login failed",
                extra={"user": user.username, "reason": "disabled" if valid else "password"},
            )
            return None
        await self._repository.update_user(
            user.id, failed_logins=0, locked_until=None, last_login_at=now
        )
        logger.info("Login", extra={"user": user.username})
        return user

    async def start_session(self, user: AppUser) -> tuple[str, datetime]:
        """A new session token for `user` and when it expires."""
        token = new_token()
        expires = self._clock() + timedelta(hours=self._policy.session_hours)
        await self._repository.add_session(
            user_id=user.id, token_hash=token_hash(token), expires_at=expires
        )
        return token, expires

    async def session_user(self, token: str) -> AppUser | None:
        return await self._repository.session_user(token_hash(token), self._clock())

    async def token_user(self, token: str) -> AppUser | None:
        return await self._repository.token_user(token_hash(token), self._clock())

    async def end_session(self, token: str) -> None:
        await self._repository.revoke_session(token_hash(token), self._clock())

    # --- Administration (command line) ---------------------------------------------------

    async def _existing(self, username: str) -> AppUser:
        user = await self._repository.get_user(normalize_username(username))
        if user is None:
            raise AuthError(f"No user named {username!r}.")
        return user

    async def create_user(self, username: str, password: str, role: UserRole) -> None:
        name = normalize_username(username)
        check_password(password)
        if await self._repository.get_user(name) is not None:
            raise AuthError(f"A user named {name!r} already exists.")
        await self._repository.create_user(
            username=name,
            password_hash=await asyncio.to_thread(hash_password, password),
            role=role,
        )
        logger.info("User created", extra={"user": name, "role": role.value})

    async def set_password(self, username: str, password: str) -> None:
        """Replace the password, unlock the account and end its sessions (not its API tokens)."""
        user = await self._existing(username)
        check_password(password)
        await self._repository.update_user(
            user.id,
            password_hash=await asyncio.to_thread(hash_password, password),
            failed_logins=0,
            locked_until=None,
        )
        await self._repository.revoke_credentials(user.id, self._clock(), tokens=False)
        logger.info("Password changed", extra={"user": user.username})

    async def set_role(self, username: str, role: UserRole) -> None:
        user = await self._existing(username)
        await self._repository.update_user(user.id, role=role)
        logger.info("Role changed", extra={"user": user.username, "role": role.value})

    async def set_enabled(self, username: str, enabled: bool) -> None:
        """Enable or disable an account; disabling also revokes its sessions and tokens."""
        user = await self._existing(username)
        await self._repository.update_user(
            user.id, enabled=enabled, failed_logins=0, locked_until=None
        )
        if not enabled:
            await self._repository.revoke_credentials(user.id, self._clock())
        logger.info("User enabled" if enabled else "User disabled", extra={"user": user.username})

    async def issue_token(self, username: str, name: str, days: int | None) -> str:
        """A new API token for `username`; shown once, stored as a hash."""
        user = await self._existing(username)
        if not TOKEN_NAME.match(name):
            raise AuthError("A token name is up to 128 letters, digits, '.', '_' or '-'.")
        if days is not None and days <= 0:
            raise AuthError("A token's lifetime must be at least one day.")
        if any(
            t["username"] == user.username and t["name"] == name
            for t in await self._repository.list_tokens()
        ):
            raise AuthError(f"{user.username} already has a token named {name!r}.")
        token = new_token()
        expires = self._clock() + timedelta(days=days) if days else None
        await self._repository.add_token(
            user_id=user.id, name=name, token_hash=token_hash(token), expires_at=expires
        )
        logger.info("API token issued", extra={"user": user.username, "token": name})
        return token

    async def revoke_token(self, username: str, name: str) -> None:
        user = await self._existing(username)
        if not await self._repository.revoke_token(user.id, name, self._clock()):
            raise AuthError(f"{user.username} has no active token named {name!r}.")
        logger.info("API token revoked", extra={"user": user.username, "token": name})
