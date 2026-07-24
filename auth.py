"""FastAPI authentication, local sessions, authorization, and CSRF helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from http.cookies import SimpleCookie
import asyncio
import secrets
import time
from typing import Iterable

from fastapi import HTTPException, Request, WebSocket
from starlette.concurrency import run_in_threadpool

from .cms_auth import CMSAuthResult, CMSAuthStatus
from .models import ROLE_ADMIN, ROLE_PROCTOR
from .service import Actor, MutationService, ServiceError
from .settings import Settings


SESSION_COOKIE = "toilet_session"
LOCALE_COOKIE = "toilet_locale"
SUPPORTED_CONTESTANT_LOCALES = frozenset({"en", "lt"})


def normalize_contestant_locale(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    language = value.strip().replace("_", "-").split("-", 1)[0].casefold()
    return language if language in SUPPORTED_CONTESTANT_LOCALES else None


def resolve_contestant_locale(connection: Request | WebSocket) -> str:
    """Resolve query override, CMS language cookie, then persisted override."""

    query_value = None
    query_present = False
    for key in ("lang", "language"):
        if key in connection.query_params:
            query_present = True
            query_value = connection.query_params.get(key)
            break
    if query_present:
        query_locale = normalize_contestant_locale(query_value) or "en"
        connection.state.contestant_locale_cookie = query_locale
        return query_locale

    if "language" in connection.cookies:
        return (
            normalize_contestant_locale(connection.cookies.get("language"))
            or "en"
        )
    return normalize_contestant_locale(
        connection.cookies.get(LOCALE_COOKIE)
    ) or "en"


@dataclass(frozen=True, slots=True)
class Principal:
    subject_type: str
    subject: str
    display_name: str
    user_id: int | None
    roles: frozenset[str]
    class_scope: frozenset[str]
    all_classes: bool
    csrf_token: str
    session_token: str
    session_expires_at: datetime
    contest: str | None = None
    locale: str = "en"

    @property
    def is_admin(self) -> bool:
        return ROLE_ADMIN in self.roles

    @property
    def is_proctor(self) -> bool:
        return ROLE_PROCTOR in self.roles

    @property
    def actor(self) -> Actor:
        kind = "operator" if self.roles else "student"
        return Actor(
            kind,
            self.subject,
            self.roles,
            self.class_scope,
            self.all_classes,
        )


@dataclass(frozen=True, slots=True)
class WebSocketStudentAuth:
    principal: Principal | None
    status: CMSAuthStatus | None
    response_headers: tuple[tuple[bytes, bytes], ...] = ()


def _principal_from_session(session: dict, *, locale: str = "en") -> Principal:
    return Principal(
        subject_type=session["subject_type"],
        subject=session["subject"],
        display_name=session["display_name"],
        user_id=session["user_id"],
        roles=frozenset(session["roles"]),
        class_scope=frozenset(session["class_scope"]),
        all_classes=bool(session["all_classes"]),
        csrf_token=session["csrf_token"],
        session_token=session["token"],
        session_expires_at=session["expires_at"],
        contest=session.get("contest"),
        locale=locale,
    )


def _service_from_connection(connection: Request | WebSocket) -> MutationService:
    return connection.app.state.mutations


def _settings_from_connection(connection: Request | WebSocket) -> Settings:
    return connection.app.state.settings


def _local_session(connection: Request | WebSocket) -> dict | None:
    token = connection.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return _service_from_connection(connection).get_session(token)


def session_cookie_header(settings: Settings, token: str, max_age: int) -> str:
    """Build a Set-Cookie header usable in a WebSocket opening handshake."""

    cookie = SimpleCookie()
    cookie[SESSION_COOKIE] = token
    morsel = cookie[SESSION_COOKIE]
    morsel["path"] = settings.cookie_path
    morsel["max-age"] = str(max_age)
    morsel["httponly"] = True
    morsel["samesite"] = "Strict"
    if settings.cookie_secure:
        morsel["secure"] = True
    return morsel.OutputString()


async def _issue_cms_companion_session(
    connection: Request | WebSocket,
    *,
    username: str,
    contest: str,
    user_id: int,
    display_name: str,
) -> dict:
    service = _service_from_connection(connection)
    settings = _settings_from_connection(connection)
    result = await run_in_threadpool(
        lambda: service.issue_session(
            subject_type="cms",
            subject=username,
            user_id=user_id,
            display_name=display_name,
            contest=contest,
            ttl_seconds=settings.session_ttl_seconds,
            issuance_rate_key=(
                f"cms-session:{username}:"
                f"{connection.client.host if connection.client else 'unknown'}"
            ),
            issuance_rate_limit=settings.student_rate_limit_count,
            issuance_rate_window_seconds=settings.student_rate_limit_window_seconds,
            actor=Actor.system("cms-auth"),
        )
    )
    session = service.get_session(result.value["token"])
    assert session is not None
    return session


async def _cms_identity(connection: Request | WebSocket) -> CMSAuthResult:
    client = connection.client
    if client is None:
        return CMSAuthResult(
            CMSAuthStatus.UNAVAILABLE, detail="connection has no client address"
        )
    return await connection.app.state.cms_auth.authenticate(
        connection.cookies, client.host
    )


async def get_student_principal(request: Request) -> Principal | None:
    service = _service_from_connection(request)
    cms = await _cms_identity(request)
    request.state.cms_set_cookie_headers = cms.set_cookie_headers
    if cms.status is CMSAuthStatus.UNAVAILABLE:
        raise HTTPException(503, "CMS authentication is temporarily unavailable")
    if cms.status is CMSAuthStatus.AMBIGUOUS:
        await _record_cms_ambiguity(
            request,
            detail=cms.detail,
            transport="http",
        )
        raise HTTPException(401, "ambiguous CMS identity")
    if not cms.authenticated or cms.username is None or cms.contest is None:
        return None

    student = await run_in_threadpool(lambda: service.get_student(cms.username))
    if student is None:
        if getattr(request.app.state, "student_sync_failure", None) is not None:
            raise HTTPException(
                503, "contestant roster synchronization is unavailable"
            )
        raise HTTPException(
            403, "CMS user is not in the synchronized contestant roster"
        )
    session = _local_session(request)
    if (
        session is None
        or session["subject_type"] != "cms"
        or session["subject"] != cms.username
        or session["contest"] != cms.contest
        or session["user_id"] != student["id"]
    ):
        session = await _issue_cms_companion_session(
            request,
            username=cms.username,
            contest=cms.contest,
            user_id=student["id"],
            display_name=cms.username,
        )
        request.state.local_session = session
    return _principal_from_session(
        session, locale=resolve_contestant_locale(request)
    )


async def require_student(request: Request) -> Principal:
    principal = await get_student_principal(request)
    if principal is None or principal.user_id is None:
        raise HTTPException(401, "student authentication required")
    return principal


def get_operator_principal(request: Request) -> Principal | None:
    session = _local_session(request)
    if session is None or session["subject_type"] != "operator":
        return None
    principal = _principal_from_session(session)
    if not principal.roles:
        return None
    return principal


def require_admin(request: Request) -> Principal:
    principal = get_operator_principal(request)
    if principal is None or not principal.is_admin:
        raise HTTPException(403, "administrator role required")
    return principal


def require_staff(request: Request) -> Principal:
    principal = get_operator_principal(request)
    if principal is None or not principal.is_proctor:
        raise HTTPException(403, "proctor role required")
    return principal


def require_all_class_proctor(request: Request) -> Principal:
    principal = require_staff(request)
    if not principal.all_classes:
        raise HTTPException(403, "all-classes proctor scope required")
    return principal


async def _record_cms_ambiguity(
    connection: Request | WebSocket,
    *,
    detail: str | None,
    transport: str,
) -> None:
    """Audit one ambiguous identity per client/reason window, not every poll."""

    client = connection.client.host if connection.client else "unknown"
    key = (client, detail or "")
    now = time.monotonic()
    lock: asyncio.Lock = connection.app.state.security_event_lock
    async with lock:
        seen: dict[tuple[str, str], float] = connection.app.state.security_event_seen
        last = seen.get(key)
        if last is not None and now - last < 300:
            return
        seen[key] = now
        if len(seen) > 1024:
            cutoff = now - 300
            connection.app.state.security_event_seen = {
                item: timestamp for item, timestamp in seen.items() if timestamp >= cutoff
            }
    await run_in_threadpool(
        lambda: _service_from_connection(connection).record_security_event(
            "cms.authentication_ambiguous",
            target_identifier=client,
            details={"reason": detail, "transport": transport},
        )
    )


async def verify_csrf(request: Request, principal: Principal) -> None:
    supplied = request.headers.get("X-CSRF-Token")
    if not supplied:
        try:
            form = await request.form()
            supplied = form.get("_csrf")
        except Exception:
            supplied = None
    if not isinstance(supplied, str) or not secrets.compare_digest(
        supplied, principal.csrf_token
    ):
        raise HTTPException(403, "invalid CSRF token")


def websocket_origin_allowed(websocket: WebSocket) -> bool:
    settings = _settings_from_connection(websocket)
    origin = websocket.headers.get("origin", "")
    return bool(origin) and secrets.compare_digest(origin, settings.public_origin)


async def authenticate_student_websocket(websocket: WebSocket) -> WebSocketStudentAuth:
    if not websocket_origin_allowed(websocket):
        return WebSocketStudentAuth(None, None)
    settings = _settings_from_connection(websocket)
    service = _service_from_connection(websocket)
    headers: list[tuple[bytes, bytes]] = []
    cms = await _cms_identity(websocket)
    headers.extend(
        (b"set-cookie", value.encode("latin-1")) for value in cms.set_cookie_headers
    )
    if cms.status is CMSAuthStatus.AMBIGUOUS:
        await _record_cms_ambiguity(
            websocket,
            detail=cms.detail,
            transport="websocket",
        )
    if not cms.authenticated or cms.username is None or cms.contest is None:
        return WebSocketStudentAuth(None, cms.status, tuple(headers))
    student = await run_in_threadpool(lambda: service.get_student(cms.username))
    if student is None:
        status = (
            CMSAuthStatus.UNAVAILABLE
            if getattr(websocket.app.state, "student_sync_failure", None) is not None
            else cms.status
        )
        return WebSocketStudentAuth(None, status, tuple(headers))
    session = _local_session(websocket)
    if (
        session is None
        or session["subject_type"] != "cms"
        or session["subject"] != cms.username
        or session["contest"] != cms.contest
        or session["user_id"] != student["id"]
    ):
        try:
            session = await _issue_cms_companion_session(
                websocket,
                username=cms.username,
                contest=cms.contest,
                user_id=student["id"],
                display_name=cms.username,
            )
        except ServiceError:
            return WebSocketStudentAuth(
                None, CMSAuthStatus.UNAVAILABLE, tuple(headers)
            )
        headers.append(
            (
                b"set-cookie",
                session_cookie_header(
                    settings, session["token"], settings.session_ttl_seconds
                ).encode("latin-1"),
            )
        )
    locale = resolve_contestant_locale(websocket)
    if getattr(websocket.state, "contestant_locale_cookie", None):
        cookie = SimpleCookie()
        cookie[LOCALE_COOKIE] = locale
        morsel = cookie[LOCALE_COOKIE]
        morsel["path"] = settings.cookie_path
        morsel["max-age"] = str(settings.session_ttl_seconds)
        morsel["samesite"] = "Strict"
        if settings.cookie_secure:
            morsel["secure"] = True
        headers.append((b"set-cookie", morsel.OutputString().encode("latin-1")))
    return WebSocketStudentAuth(
        _principal_from_session(session, locale=locale), cms.status, tuple(headers)
    )


def authenticate_staff_websocket(websocket: WebSocket) -> Principal | None:
    if not websocket_origin_allowed(websocket):
        return None
    session = _local_session(websocket)
    if session is None or session["subject_type"] != "operator":
        return None
    principal = _principal_from_session(session)
    return principal if principal.is_proctor else None
