"""Signing in and out, and who is signed in (PRD FR-017).

- `current_user` / `require_user`: FastAPI dependencies. A request is
  authenticated by a session cookie (browsers) or an API token in
  `Authorization: Bearer ...` (scripts).
- `/auth/login`, `/auth/logout`: the login page and its form.
- `/auth/verify`: asked by the production proxy before every Grafana request;
  answers with the user name and Grafana role in headers, or sends the
  browser to the login page.
"""

import html
import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import Annotated
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import APIRouter, Depends, Request, Response
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import AppError
from app.config.settings import Settings
from app.database import get_session
from app.models.enums import UserRole
from app.repositories.auth import SqlAuthRepository
from app.services.auth import AuthPolicy, AuthService

logger = logging.getLogger(__name__)

COOKIE = "marketdata_session"
# Grafana's organisation role for each platform role.
GRAFANA_ROLES = {UserRole.ADMIN: "Admin", UserRole.VIEWER: "Viewer"}
# Longest form body read from the login page.
MAX_FORM_BYTES = 4096

_bearer = HTTPBearer(auto_error=False, description="A personal API token (PRD FR-017)")


@dataclass(frozen=True)
class AuthenticatedUser:
    username: str
    role: UserRole


class UserOut(BaseModel):
    username: str
    role: UserRole


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def auth_service(request: Request, session: AsyncSession) -> AuthService:
    settings = _settings(request)
    return AuthService(
        SqlAuthRepository(session),
        AuthPolicy(
            session_hours=settings.session_hours,
            max_failures=settings.login_max_failures,
            lockout_minutes=settings.login_lockout_minutes,
        ),
    )


async def current_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthenticatedUser | None:
    """The signed-in user, by API token or session cookie; None if neither is valid."""
    service = auth_service(request, session)
    user = None
    if credentials is not None:
        user = await service.token_user(credentials.credentials)
    elif cookie := request.cookies.get(COOKIE):
        user = await service.session_user(cookie)
    return AuthenticatedUser(user.username, user.role) if user is not None else None


CurrentUser = Annotated[AuthenticatedUser | None, Depends(current_user)]


async def require_user(user: CurrentUser) -> AuthenticatedUser:
    if user is None:
        raise AppError(
            "authentication_required",
            "Sign in at /auth/login, or send an API token as 'Authorization: Bearer <token>'.",
            HTTPStatus.UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def safe_next(value: str | None, default: str) -> str:
    """A local path to return to after signing in; anything else becomes `default`."""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return default
    return value


def login_redirect(next_path: str) -> RedirectResponse:
    return RedirectResponse(
        f"/auth/login?next={quote(next_path, safe='/')}", status_code=HTTPStatus.SEE_OTHER
    )


def _same_origin(request: Request) -> bool:
    """A form post must come from this site's own pages (cross-site request forgery)."""
    origin = request.headers.get("origin")
    if origin is None or origin == "null":
        # Browsers send Origin on every POST; a missing one is not a browser form.
        return origin is None
    return urlsplit(origin).netloc == request.headers.get("host")


router = APIRouter(tags=["authentication"])


# --- Pages ----------------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title} · Market Data</title>
<style>
  :root {{ color-scheme: light dark; --fg: #1d2330; --bg: #f4f5f7; --card: #fff;
          --muted: #5c6370; --accent: #2f5bd3; --error: #b3261e; --line: #d5d8de; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fg: #e6e8ec; --bg: #111318; --card: #1b1e25; --muted: #9aa1ad;
            --accent: #7aa2ff; --error: #ff8a80; --line: #333844; }} }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
         font: 16px/1.5 system-ui, sans-serif; color: var(--fg); background: var(--bg); }}
  main {{ width: min(360px, calc(100vw - 32px)); background: var(--card); padding: 32px;
         border-radius: 12px; border: 1px solid var(--line); box-sizing: border-box; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 4px; }}
  p {{ color: var(--muted); margin: 0 0 20px; font-size: .9rem; }}
  label {{ display: block; font-size: .85rem; margin: 12px 0 4px; }}
  input {{ width: 100%; box-sizing: border-box; padding: 10px; font: inherit; color: inherit;
          background: transparent; border: 1px solid var(--line); border-radius: 8px; }}
  button {{ width: 100%; margin-top: 20px; padding: 10px; font: inherit; font-weight: 600;
           color: #fff; background: var(--accent); border: 0; border-radius: 8px;
           cursor: pointer; }}
  .error {{ color: var(--error); }}
</style>
</head>
<body><main>{body}</main></body>
</html>
"""


def _page(title: str, body: str, status: int = HTTPStatus.OK) -> HTMLResponse:
    response = HTMLResponse(_PAGE.format(title=html.escape(title), body=body), status_code=status)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    )
    return response


def _login_page(next_path: str, error: str | None = None, username: str = "") -> HTMLResponse:
    message = (
        f'<p class="error" role="alert">{html.escape(error)}</p>'
        if error
        else ("<p>Sign in to use the market data platform.</p>")
    )
    body = f"""
<h1>Market Data</h1>
{message}
<form method="post" action="/auth/login">
  <input type="hidden" name="next" value="{html.escape(next_path)}">
  <label for="username">User name</label>
  <input id="username" name="username" autocomplete="username" required autofocus
         value="{html.escape(username)}">
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Sign in</button>
</form>"""
    return _page("Sign in", body, HTTPStatus.UNAUTHORIZED if error else HTTPStatus.OK)


@router.get("/auth/login", include_in_schema=False)
async def login_page(request: Request, user: CurrentUser, next: str | None = None) -> Response:  # noqa: A002
    target = safe_next(next, _settings(request).home_path)
    if user is not None:
        return RedirectResponse(target, status_code=HTTPStatus.SEE_OTHER)
    return _login_page(target)


@router.post("/auth/login", include_in_schema=False)
async def login(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Response:
    settings = _settings(request)
    if not _same_origin(request):
        return _page("Sign in", "<p class='error'>Sign in from this site's own page.</p>", 403)
    body = await request.body()
    form = parse_qs(body[:MAX_FORM_BYTES].decode(errors="replace"), max_num_fields=8)
    username = form.get("username", [""])[0][:64]
    password = form.get("password", [""])[0][:1024]
    target = safe_next(form.get("next", [None])[0], settings.home_path)

    service = auth_service(request, session)
    user = await service.authenticate(username, password) if username and password else None
    if user is None:
        # Failure counts are kept, whatever happens next.
        await session.commit()
        return _login_page(
            target, "Wrong user name or password, or the account is locked.", username
        )
    token, expires = await service.start_session(user)
    await session.commit()
    response = RedirectResponse(target, status_code=HTTPStatus.SEE_OTHER)
    response.set_cookie(
        COOKIE,
        token,
        max_age=settings.session_hours * 3600,
        expires=expires,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/auth/logout", include_in_schema=False)
async def logout_page(user: CurrentUser) -> Response:
    if user is None:
        return login_redirect("/auth/login")
    return _page(
        "Sign out",
        f"""
<h1>Sign out</h1>
<p>Signed in as {html.escape(user.username)}.</p>
<form method="post" action="/auth/logout"><button type="submit">Sign out</button></form>""",
    )


@router.post("/auth/logout", include_in_schema=False)
async def logout(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> Response:
    if not _same_origin(request):
        return _page("Sign out", "<p class='error'>Sign out from this site's own page.</p>", 403)
    if cookie := request.cookies.get(COOKIE):
        await auth_service(request, session).end_session(cookie)
        await session.commit()
    response = login_redirect(_settings(request).home_path)
    response.delete_cookie(
        COOKIE,
        path="/",
        secure=_settings(request).session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/auth/verify", include_in_schema=False)
async def verify(request: Request, user: CurrentUser) -> Response:
    """For the proxy's forward_auth: 204 with the user for Grafana, else to the login page."""
    if user is not None:
        return Response(
            status_code=HTTPStatus.NO_CONTENT,
            headers={
                "X-WEBAUTH-USER": user.username,
                "X-WEBAUTH-ROLE": GRAFANA_ROLES[user.role],
                "Cache-Control": "no-store",
            },
        )
    original = request.headers.get("x-forwarded-uri", "/")
    page = request.headers.get("x-forwarded-method", "GET") == "GET" and "text/html" in (
        request.headers.get("accept", "")
    )
    if page:
        return login_redirect(safe_next(original, _settings(request).home_path))
    return JSONResponse(
        {"error": {"code": "authentication_required", "message": "Sign in at /auth/login."}},
        status_code=HTTPStatus.UNAUTHORIZED,
    )


# --- API documentation, for signed-in users only -------------------------------------------


@router.get("/openapi.json", include_in_schema=False)
async def openapi(request: Request, user: CurrentUser) -> JSONResponse:
    await require_user(user)
    return JSONResponse(request.app.openapi())


@router.get("/docs", include_in_schema=False)
async def docs(request: Request, user: CurrentUser) -> Response:
    if user is None:
        return login_redirect("/docs")
    return get_swagger_ui_html(openapi_url="/openapi.json", title=request.app.title)


# --- Who am I -------------------------------------------------------------------------------

me_router = APIRouter(tags=["authentication"])


@me_router.get("/auth/me", response_model=UserOut, summary="The signed-in user")
async def me(user: Annotated[AuthenticatedUser, Depends(require_user)]) -> UserOut:
    return UserOut(username=user.username, role=user.role)
