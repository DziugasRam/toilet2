"""LMIO contest assistance and toilet queue web application."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote
import uuid

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from .auth import (
    LOCALE_COOKIE,
    SESSION_COOKIE,
    Principal,
    authenticate_staff_websocket,
    authenticate_student_websocket,
    get_operator_principal,
    get_student_principal,
    require_admin,
    require_all_class_proctor,
    require_staff,
    require_student,
    verify_csrf,
)
from .cms_auth import CMSAuthStatus, CMSAuthenticator
from .control_client import (
    ControlAPIError,
    ControlClient,
)
from .database import Database
from .models import (
    OPEN_STATUSES,
    GENERAL_REQUEST_TYPES,
    REQUEST_TYPES,
    AuditEvent,
    Request as QueueRequest,
    RequestToiletLock,
    SchoolClass,
    Toilet,
    User,
)
from .service import Actor, MutationResult, MutationService, ServiceError
from .settings import Settings
from .ws import Hub, StaffSubscription, StudentSubscription


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


AUDIT_VIEWS = frozenset({"summary", "requests", "system", "all"})
CONFIGURATION_AUDIT_ACTIONS = frozenset(
    {
        "class.toilet_assigned",
        "request.demoted_for_toilet_delete",
    }
)


def _error_response(exc: ServiceError) -> JSONResponse:
    headers = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(
        {"detail": str(exc), "code": exc.code},
        status_code=exc.status_code,
        headers=headers,
    )


def _base_context(request: Request, **values) -> dict[str, Any]:
    return {
        "base_path": request.app.state.settings.app_root_path,
        **values,
    }


def _cms_login_path(settings: Settings) -> str:
    contest = settings.cms_contests[0]
    # CWS renders its login form on the contest root; /login itself is POST-only.
    # Preserve the toilet destination so a successful CMS login returns here.
    destination = settings.app_root_path + "/"
    root = f"/{quote(contest, safe='')}/" if settings.cms_multi_contest else "/"
    return f"{root}?next={quote(destination, safe='/')}"


async def _hold_websocket_until_deadline(websocket: WebSocket, max_age: float) -> None:
    """Receive harmless client frames without extending the authentication TTL."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_age
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        await asyncio.wait_for(websocket.receive_text(), timeout=remaining)


def _principal_socket_max_age(principal: Principal, configured_max_age: float) -> float:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return max(
        0.0,
        min(configured_max_age, (principal.session_expires_at - now).total_seconds()),
    )


def _set_local_cookie(response, settings: Settings, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path=settings.cookie_path,
    )


def _operator_login_origin_allowed(request: Request) -> bool:
    settings = request.app.state.settings
    return request.headers.get("origin", "") == settings.public_origin


def _actor_from_subscription(subscription: StaffSubscription) -> Actor:
    return Actor(
        "operator",
        subscription.subject,
        subscription.roles,
        subscription.class_scope,
        subscription.all_classes,
    )


def _request_to_dict(request: QueueRequest, all_open: list[QueueRequest]) -> dict[str, Any]:
    required = {lock.toilet_id for lock in request.toilet_locks}
    position = 0
    if request.status == "pending" and request.type == "toilet":
        position = 1
        for other in all_open:
            if other.id == request.id or other.status != "pending":
                continue
            if (other.created_at, other.id) >= (request.created_at, request.id):
                continue
            other_required = {lock.toilet_id for lock in other.toilet_locks}
            if required & other_required or not required or not other_required:
                position += 1
    return {
        "id": request.id,
        "user_id": request.user_id,
        "userid": request.user.username,
        "username": request.user.username,
        "type": request.type,
        "type_label": REQUEST_TYPES.get(request.type, request.type),
        "status": request.status,
        "position": position,
        "created_at": request.created_at.isoformat(),
        "activated_at": request.activated_at.isoformat() if request.activated_at else None,
        "routing_mode": request.routing_mode,
        "blocked_reason": request.blocked_reason,
        "classes": [
            {"id": item.class_public_id, "name": item.class_name}
            for item in sorted(
                request.class_snapshots, key=lambda item: item.class_public_id
            )
        ],
        "toilets": [
            {"id": lock.toilet.id, "name": lock.toilet.name}
            for lock in sorted(request.toilet_locks, key=lambda lock: lock.toilet_id)
            if lock.toilet is not None
        ],
        "alerts": [
            {"id": alert.id, "code": alert.code, "severity": alert.severity}
            for alert in request.alerts
            if alert.resolved_at is None
        ],
    }


def _general_request_types(app: FastAPI) -> dict[str, str]:
    return {
        request_type: GENERAL_REQUEST_TYPES[request_type]
        for request_type in app.state.settings.general_request_types
    }


def student_state(app: FastAPI, user_id: int, locale: str = "en") -> dict[str, Any]:
    session = app.state.database.SessionLocal()
    try:
        requests = (
            session.query(QueueRequest)
            .options(
                selectinload(QueueRequest.user),
                selectinload(QueueRequest.class_snapshots),
                selectinload(QueueRequest.toilet_locks).selectinload(
                    RequestToiletLock.toilet
                ),
                selectinload(QueueRequest.alerts),
            )
            .filter(
                QueueRequest.user_id == user_id,
                QueueRequest.status.in_(OPEN_STATUSES),
            )
            .order_by(QueueRequest.created_at, QueueRequest.id)
            .all()
        )
        all_toilet = (
            session.query(QueueRequest)
            .options(selectinload(QueueRequest.toilet_locks))
            .filter(
                QueueRequest.type == "toilet",
                QueueRequest.status.in_(OPEN_STATUSES),
            )
            .order_by(QueueRequest.created_at, QueueRequest.id)
            .all()
        )
        return {
            "locale": locale,
            "request_types": _general_request_types(app),
            "requests": [_request_to_dict(item, all_toilet) for item in requests],
        }
    finally:
        session.close()


def _request_visible_to_actor(request: QueueRequest, actor: Actor) -> bool:
    if actor.all_classes:
        return True
    class_ids = {item.class_public_id for item in request.class_snapshots}
    return not class_ids or bool(class_ids & actor.class_scope)


def staff_state(app: FastAPI, actor: Actor) -> dict[str, Any]:
    session = app.state.database.SessionLocal()
    try:
        requests = (
            session.query(QueueRequest)
            .options(
                selectinload(QueueRequest.user),
                selectinload(QueueRequest.class_snapshots),
                selectinload(QueueRequest.toilet_locks).selectinload(
                    RequestToiletLock.toilet
                ),
                selectinload(QueueRequest.alerts),
            )
            .filter(QueueRequest.status.in_(OPEN_STATUSES))
            .order_by(QueueRequest.created_at, QueueRequest.id)
            .all()
        )
        visible = [item for item in requests if _request_visible_to_actor(item, actor)]
        toilet_open = [item for item in requests if item.type == "toilet"]
        return {
            "queue": [
                _request_to_dict(item, toilet_open)
                for item in visible
                if item.type == "toilet"
            ],
            "support": [
                _request_to_dict(item, toilet_open)
                for item in visible
                if item.type != "toilet"
            ],
            "alerts": app.state.mutations.list_open_alerts(actor),
            # Filled by live_staff_state from Olimp-control. This synchronous
            # portion deliberately has no class-catalog fallback.
            "classes": [],
            "all_classes": actor.all_classes,
        }
    finally:
        session.close()


async def live_staff_state(app: FastAPI, actor: Actor) -> dict[str, Any]:
    """Return queue data plus a fresh Olimp-control class catalog."""

    state = staff_state(app, actor)
    try:
        classes = await app.state.control.classes()
    except ControlAPIError as exc:
        state["classes"] = []
        state["layout_error"] = type(exc).__name__
        return state
    visible = [
        item
        for item in classes
        if actor.all_classes or item.id in actor.class_scope
    ]
    state["classes"] = [
        {
            "id": item.id,
            "name": item.name,
            "sequence_num": item.sequence_num,
            "grid_cols": item.grid_cols,
        }
        for item in visible
    ]
    state["layout_error"] = None
    return state


async def proctor_layout_state(
    app: FastAPI,
    actor: Actor,
    class_id: str | None = None,
) -> dict[str, Any]:
    """Fetch physical layouts live and overlay toilet-local open requests."""

    state = staff_state(app, actor)
    requests_by_userid: dict[str, list[dict[str, Any]]] = {}
    for item in [*state["queue"], *state["support"]]:
        requests_by_userid.setdefault(item["userid"], []).append(item)

    try:
        if class_id is None:
            classes = await app.state.control.classes()
            class_ids = [
                item.id
                for item in classes
                if actor.all_classes or item.id in actor.class_scope
            ]
        else:
            normalized = str(uuid.UUID(class_id))
            if not actor.all_classes and normalized not in actor.class_scope:
                raise HTTPException(403, "class is outside proctor scope")
            class_ids = [normalized]
        layouts = await asyncio.gather(
            *(app.state.control.class_layout(item) for item in class_ids)
        )
    except (ValueError, AttributeError) as exc:
        raise HTTPException(400, "invalid class id") from exc
    except ControlAPIError as exc:
        raise HTTPException(
            503, "Olimp-control class layout is temporarily unavailable"
        ) from exc

    output = []
    for layout in layouts:
        class_info = layout.class_info
        computers = []
        for item in layout.computers:
            student = item["student"]
            computers.append(
                {
                    "machine_id": item["machine_id"],
                    "name": item["name"],
                    "sequence_num": item["sequence_num"],
                    "grid_row": item["grid_row"],
                    "grid_col": item["grid_col"],
                    "student": student,
                    "requests": (
                        requests_by_userid.get(student["userid"], [])
                        if student is not None
                        else []
                    ),
                }
            )
        output.append(
            {
                "id": class_info.id,
                "name": class_info.name,
                "sequence_num": class_info.sequence_num,
                "grid_cols": class_info.grid_cols,
                "computers": computers,
            }
        )
    return {"classes": output, "layout_error": None}


def config_state(
    app: FastAPI,
    live_classes: tuple[Any, ...] | list[Any] = (),
) -> dict[str, Any]:
    session = app.state.database.SessionLocal()
    try:
        toilets = session.query(Toilet).order_by(Toilet.name, Toilet.id).all()
        mappings = {
            item.public_id: item
            for item in session.query(SchoolClass).order_by(SchoolClass.public_id).all()
        }
        classes = [
            {
                "id": item.id,
                "name": item.name,
                "sequence_num": item.sequence_num,
                "grid_cols": item.grid_cols,
                "toilet_id": (
                    mappings[item.id].toilet_id if item.id in mappings else None
                ),
            }
            for item in live_classes
        ]
        classes.sort(key=lambda item: (item["sequence_num"], item["name"], item["id"]))
        return {
            "toilets": [
                {"id": item.id, "name": item.name, "capacity": item.capacity}
                for item in toilets
            ],
            "classes": classes,
            "operators": app.state.mutations.list_operators(),
        }
    finally:
        session.close()


def _catalog_audit_is_meaningful(details: dict[str, Any]) -> bool:
    return bool(
        details.get("created")
        or details.get("renamed")
        or details.get("reordered")
        or details.get("deleted")
        or details.get("revision_changed")
    )


def _audit_category(action: str, details: dict[str, Any]) -> str:
    if action == "class.catalog_synced":
        return "summary" if _catalog_audit_is_meaningful(details) else "system"
    if action.startswith("toilet.") or action in CONFIGURATION_AUDIT_ACTIONS:
        return "summary"
    if action.startswith("request."):
        return "requests"
    if action.startswith("session.") or action == "user.created":
        return "system"
    # Alerts, rate-limit rejections, authentication failures, schema upgrades,
    # and control integration failures/recoveries are incidents worth showing.
    return "summary"


def _audit_event_dict(event: AuditEvent) -> dict[str, Any]:
    details = json.loads(event.details_json)
    return {
        "id": event.id,
        "occurred_at": event.occurred_at.isoformat(),
        "actor_kind": event.actor_kind,
        "actor": event.actor_identifier,
        "action": event.action,
        "target_type": event.target_type,
        "target": event.target_identifier,
        "correlation_id": event.correlation_id,
        "details": details,
        "category": _audit_category(event.action, details),
    }


def audit_state(
    app: FastAPI,
    limit: int = 100,
    view: str = "summary",
) -> dict[str, Any]:
    if view not in AUDIT_VIEWS:
        raise ValueError("unknown audit view")
    session = app.state.database.SessionLocal()
    try:
        requested = min(max(limit, 1), 500)
        query = session.query(AuditEvent).order_by(
            AuditEvent.occurred_at.desc(), AuditEvent.id.desc()
        )
        if view == "all":
            event_dicts = [_audit_event_dict(event) for event in query.limit(requested)]
        else:
            # Filter before applying the response limit. Scan in bounded pages so
            # routine session/request rows cannot crowd configuration incidents
            # out of a short admin view without loading an unbounded audit table.
            event_dicts = []
            offset = 0
            batch_size = 250
            max_scan = 5000
            while len(event_dicts) < requested and offset < max_scan:
                batch = query.offset(offset).limit(batch_size).all()
                if not batch:
                    break
                for event in batch:
                    serialized = _audit_event_dict(event)
                    if serialized["category"] == view:
                        event_dicts.append(serialized)
                        if len(event_dicts) == requested:
                            break
                offset += len(batch)
                if len(batch) < batch_size:
                    break
        return {"view": view, "events": event_dicts}
    finally:
        session.close()


def _notify(app: FastAPI, result: MutationResult) -> None:
    if not result.student_user_ids and not result.staff_changed:
        return
    app.state.hub.notify_from_thread(
        lambda subscription: student_state(
            app, subscription.user_id, subscription.locale
        ),
        lambda subscription: live_staff_state(
            app, _actor_from_subscription(subscription)
        ),
    )


async def _sync_students(app: FastAPI) -> bool:
    """Refresh the only Olimp-control catalog permitted in the local DB."""

    async with app.state.student_sync_lock:
        try:
            students = await app.state.control.students()
            result = await run_in_threadpool(
                lambda: app.state.mutations.sync_students(
                    [
                        {
                            "id": item.id,
                            "userid": item.userid,
                        }
                        for item in students
                    ],
                    actor=Actor.system("control-student-sync"),
                )
            )
        except ControlAPIError as exc:
            fingerprint = type(exc).__name__
            if app.state.student_sync_failure != fingerprint:
                await run_in_threadpool(
                    lambda: app.state.mutations.record_security_event(
                        "control.student_sync_failed",
                        target_identifier="student_catalog",
                        details={"error": fingerprint},
                    )
                )
            app.state.student_sync_failure = fingerprint
            return False
        previous_failure = app.state.student_sync_failure
        app.state.student_sync_failure = None
        if previous_failure is not None:
            await run_in_threadpool(
                lambda: app.state.mutations.record_security_event(
                    "control.student_sync_recovered",
                    target_identifier="student_catalog",
                    details={"previous_error": previous_failure},
                )
            )
        _notify(app, result)
        return True


async def _reconcile_legacy_class_mapping_keys(
    app: FastAPI,
    *,
    classes=None,
    actor: Actor | None = None,
) -> bool:
    try:
        live_classes = classes if classes is not None else await app.state.control.classes()
        result = await run_in_threadpool(
            lambda: app.state.mutations.reconcile_legacy_class_mapping_keys(
                [
                    {
                        "id": item.id,
                        "name": item.name,
                    }
                    for item in live_classes
                ],
                actor=actor or Actor.system("class-mapping-key-reconciliation"),
            )
        )
    except ControlAPIError as exc:
        app.state.class_mapping_reconciliation = {
            "remapped": [],
            "unresolved": [],
            "error": type(exc).__name__,
        }
        return False
    app.state.class_mapping_reconciliation = {
        **result.value,
        "error": None,
    }
    _notify(app, result)
    return True


async def _live_class_catalog(app: FastAPI):
    try:
        return await app.state.control.classes()
    except ControlAPIError as exc:
        raise HTTPException(
            503, "Olimp-control class catalog is temporarily unavailable"
        ) from exc


def _parse_completion(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, "invalid completion time") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    cms_authenticator: CMSAuthenticator | None = None,
    control_client: ControlClient | None = None,
    hub: Hub | None = None,
) -> FastAPI:
    config = (settings or Settings.from_env()).validate()
    db = database or Database(config.database_url)
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await run_in_threadpool(db.initialize)
        await run_in_threadpool(app.state.mutations.purge_expired_sessions)
        app.state.hub.loop = asyncio.get_running_loop()
        await _sync_students(app)
        await _reconcile_legacy_class_mapping_keys(app)
        try:
            yield
        finally:
            await app.state.cms_auth.aclose()
            await app.state.control.aclose()

    application = FastAPI(
        title="LMIO toilet", root_path=config.app_root_path, lifespan=lifespan
    )
    application.state.settings = config
    application.state.database = db
    application.state.mutations = MutationService(
        db,
        student_rate_limit=config.student_rate_limit_count,
        student_rate_window_seconds=config.student_rate_limit_window_seconds,
        operator_login_rate_limit=config.operator_login_rate_limit_count,
        operator_login_rate_window_seconds=config.operator_login_rate_limit_window_seconds,
        general_request_types=config.general_request_types,
    )
    application.state.cms_auth = cms_authenticator or CMSAuthenticator(config)
    application.state.control = control_client or ControlClient(config)
    application.state.hub = hub or Hub()
    application.state.student_sync_lock = asyncio.Lock()
    application.state.student_sync_failure = None
    application.state.class_mapping_reconciliation = {
        "remapped": [],
        "unresolved": [],
        "error": None,
    }
    application.state.security_event_lock = asyncio.Lock()
    application.state.security_event_seen = {}

    @application.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, exc: ServiceError):
        return _error_response(exc)

    @application.middleware("http")
    async def auth_cookie_relay(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        for header in getattr(request.state, "cms_set_cookie_headers", ()):
            response.headers.append("set-cookie", header)
        local_session = getattr(request.state, "local_session", None)
        if local_session is not None:
            _set_local_cookie(response, config, local_session["token"])
        locale = getattr(request.state, "contestant_locale_cookie", None)
        if locale is not None:
            response.set_cookie(
                LOCALE_COOKIE,
                locale,
                max_age=config.session_ttl_seconds,
                secure=config.cookie_secure,
                samesite="strict",
                path=config.cookie_path,
            )
        return response

    # -- login/session -----------------------------------------------------

    @application.get("/login", response_class=HTMLResponse)
    async def login_page(_request: Request):
        return RedirectResponse(_cms_login_path(config))

    @application.get("/operator/login", response_class=HTMLResponse)
    async def operator_login_page(request: Request):
        return templates.TemplateResponse(
            request,
            "operator_login.html",
            _base_context(request),
        )

    @application.post("/operator/login")
    async def operator_login(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
    ):
        if not _operator_login_origin_allowed(request):
            raise HTTPException(403, "cross-origin operator login rejected")
        username = username.strip()
        if len(username) > 150 or len(password) > 4096:
            raise HTTPException(400, "operator credentials are too long")
        client_key = request.client.host if request.client else "unknown"
        await run_in_threadpool(
            lambda: application.state.mutations.consume_operator_login_attempt(
                username=username, client_key=client_key
            )
        )
        operator = await run_in_threadpool(
            lambda: application.state.mutations.authenticate_operator(
                username, password
            )
        )
        if operator is None:
            await run_in_threadpool(
                lambda: application.state.mutations.record_security_event(
                    "operator.login_failed",
                    target_identifier=username or "<empty>",
                    details={"client": client_key},
                    actor=Actor("operator_login", username or "<empty>"),
                )
            )
            raise HTTPException(401, "invalid operator credentials")
        identity = {
            "subject_type": "operator",
            "username": operator["username"],
            "display_name": operator["display_name"],
            "roles": operator["roles"],
            "class_scope": operator["class_scope"],
            "all_classes": operator["all_classes"],
        }
        issued = await run_in_threadpool(
            lambda: application.state.mutations.issue_session(
                subject_type=identity["subject_type"],
                subject=identity["username"],
                display_name=identity["display_name"],
                roles=identity["roles"],
                class_scope=identity["class_scope"],
                all_classes=identity["all_classes"],
                ttl_seconds=config.session_ttl_seconds,
                actor=Actor.system("local-operator-auth"),
            )
        )
        if "admin" in identity["roles"]:
            target = "/admin"
        elif identity["all_classes"]:
            target = "/proctor-all"
        else:
            target = "/proctor"
        response = RedirectResponse(config.app_root_path + target, status_code=303)
        _set_local_cookie(response, config, issued.value["token"])
        return response

    @application.post("/logout")
    async def logout(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        session = application.state.mutations.get_session(token) if token else None
        if session is None:
            raise HTTPException(401)
        principal = Principal(
            subject_type=session["subject_type"],
            subject=session["subject"],
            display_name=session["display_name"],
            user_id=session["user_id"],
            roles=frozenset(session["roles"]),
            class_scope=frozenset(session["class_scope"]),
            all_classes=session["all_classes"],
            csrf_token=session["csrf_token"],
            session_token=session["token"],
            session_expires_at=session["expires_at"],
            contest=session["contest"],
        )
        await verify_csrf(request, principal)
        await run_in_threadpool(
            lambda: application.state.mutations.revoke_session(
                token, actor=principal.actor
            )
        )
        response = RedirectResponse(
            config.app_root_path + ("/operator/login" if principal.roles else "/login"),
            status_code=303,
        )
        response.delete_cookie(SESSION_COOKIE, path=config.cookie_path)
        if principal.subject_type == "cms":
            # CMS login cookies are stateless and scoped to the shared browser
            # origin. Clear every configured contest cookie so multi-contest
            # sessions cannot immediately reauthenticate through another one.
            for contest_name in config.cms_contests:
                response.delete_cookie(
                    f"{contest_name}_login", path="/", secure=config.cookie_secure
                )
        return response

    # -- student -----------------------------------------------------------

    @application.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if not config.student_ui_enabled:
            # Fail before probing CMS: the contestant surface is off, so the
            # notice must render during a CMS outage too.
            return templates.TemplateResponse(request, "student_disabled.html", {})
        principal = await get_student_principal(request)
        if principal is None:
            return RedirectResponse(_cms_login_path(config))
        return templates.TemplateResponse(
            request,
            "index.html",
            _base_context(
                request,
                principal=principal,
                csrf_token=principal.csrf_token,
                locale=principal.locale,
                request_types=_general_request_types(application),
            ),
        )

    @application.get("/api/session")
    async def api_session(request: Request):
        principal = get_operator_principal(request)
        if principal is None:
            principal = await get_student_principal(request)
        if principal is None:
            raise HTTPException(401, "authentication required")
        payload = {
            "subject_type": principal.subject_type,
            "subject": principal.subject,
            "user_id": principal.user_id,
            "roles": sorted(principal.roles),
            "class_scope": sorted(principal.class_scope),
            "all_classes": principal.all_classes,
            "contest": principal.contest,
            "locale": principal.locale,
            "csrf_token": principal.csrf_token,
        }
        if principal.subject_type == "operator":
            payload["display_name"] = principal.display_name
        return payload

    @application.get("/api/state")
    async def api_state(principal: Principal = Depends(require_student)):
        return student_state(application, principal.user_id, principal.locale)

    @application.post("/api/requests")
    async def create_request_endpoint(
        request: Request,
        type: str = Form(...),
        principal: Principal = Depends(require_student),
    ):
        await verify_csrf(request, principal)
        correlation_id = str(uuid.uuid4())
        await run_in_threadpool(
            lambda: application.state.mutations.consume_student_assignment_lookup_attempt(
                user_id=principal.user_id,
                actor=principal.actor,
                correlation_id=correlation_id,
            )
        )
        try:
            assignment = await application.state.control.student_assignment(
                principal.subject
            )
        except ControlAPIError:
            assignment = None
        result = await run_in_threadpool(
            lambda: application.state.mutations.create_request(
                user_id=principal.user_id,
                request_type=type,
                assignment=assignment,
                actor=principal.actor,
                correlation_id=correlation_id,
            )
        )
        _notify(application, result)
        return result.value

    @application.post("/api/requests/{request_id}/cancel")
    async def cancel_request_endpoint(
        request_id: int,
        request: Request,
        principal: Principal = Depends(require_student),
    ):
        await verify_csrf(request, principal)
        result = await run_in_threadpool(
            lambda: application.state.mutations.cancel_request(
                request_id=request_id,
                user_id=principal.user_id,
                actor=principal.actor,
            )
        )
        _notify(application, result)
        return result.value

    # -- administrator -----------------------------------------------------

    @application.get("/admin", response_class=HTMLResponse)
    async def admin_page(request: Request, principal: Principal = Depends(require_admin)):
        return templates.TemplateResponse(
            request,
            "admin.html",
            _base_context(request, principal=principal, csrf_token=principal.csrf_token),
        )

    @application.get("/api/admin/state")
    async def admin_state(principal: Principal = Depends(require_admin)):
        try:
            classes = await application.state.control.classes()
            class_catalog_error = None
        except ControlAPIError as exc:
            classes = ()
            class_catalog_error = type(exc).__name__
        state = config_state(application, classes)
        state["class_catalog_error"] = class_catalog_error
        state["mapping_reconciliation"] = (
            application.state.class_mapping_reconciliation
        )
        return {"config": state}

    @application.get("/api/admin/audit")
    async def admin_audit(
        limit: int = 100,
        view: str = "summary",
        principal: Principal = Depends(require_admin),
    ):
        if view not in AUDIT_VIEWS:
            raise HTTPException(400, "unknown audit view")
        return audit_state(application, limit, view)

    @application.post("/api/admin/toilets")
    async def create_toilet_endpoint(
        request: Request,
        name: str = Form(...),
        capacity: int = Form(1),
        principal: Principal = Depends(require_admin),
    ):
        await verify_csrf(request, principal)
        result = await run_in_threadpool(
            lambda: application.state.mutations.create_toilet(
                name=name, capacity=capacity, actor=principal.actor
            )
        )
        _notify(application, result)
        return result.value

    @application.post("/api/admin/toilets/{toilet_id}/rename")
    async def rename_toilet_endpoint(
        toilet_id: int,
        request: Request,
        name: str = Form(...),
        principal: Principal = Depends(require_admin),
    ):
        await verify_csrf(request, principal)
        result = await run_in_threadpool(
            lambda: application.state.mutations.rename_toilet(
                toilet_id=toilet_id, name=name, actor=principal.actor
            )
        )
        _notify(application, result)
        return result.value

    @application.post("/api/admin/toilets/{toilet_id}/capacity")
    async def capacity_endpoint(
        toilet_id: int,
        request: Request,
        capacity: int = Form(...),
        principal: Principal = Depends(require_admin),
    ):
        await verify_csrf(request, principal)
        result = await run_in_threadpool(
            lambda: application.state.mutations.set_toilet_capacity(
                toilet_id=toilet_id, capacity=capacity, actor=principal.actor
            )
        )
        _notify(application, result)
        return result.value

    @application.post("/api/admin/toilets/{toilet_id}/delete")
    async def delete_toilet_endpoint(
        toilet_id: int,
        request: Request,
        principal: Principal = Depends(require_admin),
    ):
        await verify_csrf(request, principal)
        result = await run_in_threadpool(
            lambda: application.state.mutations.delete_toilet(
                toilet_id=toilet_id, actor=principal.actor
            )
        )
        _notify(application, result)
        return result.value

    @application.post("/api/admin/classes/{class_id}/toilet")
    async def assign_class_toilet_endpoint(
        class_id: str,
        request: Request,
        toilet_id: str = Form(""),
        principal: Principal = Depends(require_admin),
    ):
        await verify_csrf(request, principal)
        raw_toilet_id = toilet_id.strip()
        try:
            parsed_toilet = int(raw_toilet_id) if raw_toilet_id else None
        except ValueError as exc:
            raise HTTPException(400, "invalid toilet id") from exc
        if parsed_toilet is not None and parsed_toilet <= 0:
            raise HTTPException(400, "invalid toilet id")
        if parsed_toilet is not None:
            classes = await _live_class_catalog(application)
            try:
                normalized_class_id = str(uuid.UUID(class_id))
            except (ValueError, AttributeError) as exc:
                raise HTTPException(400, "invalid class id") from exc
            if normalized_class_id not in {item.id for item in classes}:
                raise HTTPException(404, "class not found in Olimp-control")
        result = await run_in_threadpool(
            lambda: application.state.mutations.assign_class_toilet(
                class_public_id=class_id,
                toilet_id=parsed_toilet,
                actor=principal.actor,
            )
        )
        _notify(application, result)
        return result.value

    @application.post("/api/admin/classes/reload")
    async def reload_classes_endpoint(
        request: Request, principal: Principal = Depends(require_admin)
    ):
        await verify_csrf(request, principal)
        classes = await _live_class_catalog(application)
        await _reconcile_legacy_class_mapping_keys(
            application, classes=classes, actor=principal.actor
        )
        state = config_state(application, classes)
        state["mapping_reconciliation"] = (
            application.state.class_mapping_reconciliation
        )
        return {"ok": True, "config": state}

    @application.post("/api/admin/students/sync")
    async def sync_students_endpoint(
        request: Request, principal: Principal = Depends(require_admin)
    ):
        await verify_csrf(request, principal)
        if not await _sync_students(application):
            raise HTTPException(503, "student synchronization failed")
        return {"ok": True}

    @application.post("/api/admin/operators")
    async def create_operator_endpoint(
        request: Request,
        username: str = Form(...),
        display_name: str = Form(...),
        password: str = Form(...),
        roles: list[str] = Form(...),
        class_scope: list[str] = Form(default=[]),
        all_classes: bool = Form(False),
        enabled: bool = Form(True),
        principal: Principal = Depends(require_admin),
    ):
        await verify_csrf(request, principal)
        result = await run_in_threadpool(
            lambda: application.state.mutations.upsert_operator(
                username=username,
                display_name=display_name,
                password=password,
                roles=roles,
                class_scope=class_scope,
                all_classes=all_classes,
                enabled=enabled,
                actor=principal.actor,
            )
        )
        _notify(application, result)
        return result.value

    @application.post("/api/admin/operator-deletion")
    async def delete_operator_by_name_endpoint(
        request: Request,
        username: str = Form(...),
        principal: Principal = Depends(require_admin),
    ):
        """Body-based fallback keeps pre-validation legacy names removable."""

        await verify_csrf(request, principal)
        result = await run_in_threadpool(
            lambda: application.state.mutations.delete_operator(
                username, actor=principal.actor
            )
        )
        _notify(application, result)
        return result.value

    @application.post("/api/admin/operators/{username}")
    async def update_operator_endpoint(
        username: str,
        request: Request,
        display_name: str = Form(...),
        password: str = Form(""),
        roles: list[str] = Form(...),
        class_scope: list[str] = Form(default=[]),
        all_classes: bool = Form(False),
        enabled: bool = Form(True),
        principal: Principal = Depends(require_admin),
    ):
        await verify_csrf(request, principal)
        result = await run_in_threadpool(
            lambda: application.state.mutations.upsert_operator(
                username=username,
                display_name=display_name,
                password=password or None,
                roles=roles,
                class_scope=class_scope,
                all_classes=all_classes,
                enabled=enabled,
                actor=principal.actor,
            )
        )
        _notify(application, result)
        return result.value

    @application.post("/api/admin/operators/{username}/delete")
    async def delete_operator_endpoint(
        username: str,
        request: Request,
        principal: Principal = Depends(require_admin),
    ):
        await verify_csrf(request, principal)
        result = await run_in_threadpool(
            lambda: application.state.mutations.delete_operator(
                username, actor=principal.actor
            )
        )
        _notify(application, result)
        return result.value

    @application.post("/api/proctor/alerts/{alert_id}/resolve")
    async def resolve_alert_endpoint(
        alert_id: int,
        request: Request,
        principal: Principal = Depends(require_staff),
    ):
        await verify_csrf(request, principal)
        result = await run_in_threadpool(
            lambda: application.state.mutations.resolve_alert(
                alert_id=alert_id, actor=principal.actor
            )
        )
        _notify(application, result)
        return result.value

    # -- proctor -----------------------------------------------------------

    @application.get("/proctor", response_class=HTMLResponse)
    async def proctor_page(
        request: Request, principal: Principal = Depends(require_staff)
    ):
        return templates.TemplateResponse(
            request,
            "proctor.html",
            _base_context(
                request,
                principal=principal,
                csrf_token=principal.csrf_token,
                general_requests_enabled=bool(config.general_request_types),
            ),
        )

    @application.get("/proctor-all", response_class=HTMLResponse)
    async def all_class_proctor_page(
        request: Request,
        principal: Principal = Depends(require_all_class_proctor),
    ):
        return templates.TemplateResponse(
            request,
            "proctor.html",
            _base_context(
                request,
                principal=principal,
                csrf_token=principal.csrf_token,
                general_requests_enabled=bool(config.general_request_types),
            ),
        )

    @application.get("/api/proctor/state")
    async def proctor_state(principal: Principal = Depends(require_staff)):
        return await live_staff_state(application, principal.actor)

    @application.get("/api/proctor/layouts")
    async def proctor_layouts(
        class_id: str | None = None,
        principal: Principal = Depends(require_staff),
    ):
        return await proctor_layout_state(
            application, principal.actor, class_id=class_id
        )

    @application.post("/api/proctor/requests")
    async def create_staff_toilet_request_endpoint(
        request: Request,
        userid: str = Form(...),
        principal: Principal = Depends(require_staff),
    ):
        """Queue a toilet break for the contestant a proctor selected."""

        await verify_csrf(request, principal)
        student = await run_in_threadpool(
            lambda: application.state.mutations.get_student(userid)
        )
        if student is None:
            raise HTTPException(
                404, "contestant is not in the synchronized roster"
            )
        correlation_id = str(uuid.uuid4())
        await run_in_threadpool(
            lambda: application.state.mutations.consume_student_assignment_lookup_attempt(
                user_id=student["id"],
                actor=principal.actor,
                correlation_id=correlation_id,
            )
        )
        # Route from the live assignment exactly like a contestant request, so a
        # Control outage falls back conservatively instead of failing the proctor.
        try:
            assignment = await application.state.control.student_assignment(
                student["userid"]
            )
        except ControlAPIError:
            assignment = None
        result = await run_in_threadpool(
            lambda: application.state.mutations.create_request(
                user_id=student["id"],
                request_type="toilet",
                assignment=assignment,
                actor=principal.actor,
                correlation_id=correlation_id,
            )
        )
        _notify(application, result)
        return result.value

    @application.post("/api/proctor/return/{request_id}")
    async def return_student(
        request_id: int,
        request: Request,
        returned_at: str = Form(""),
        principal: Principal = Depends(require_staff),
    ):
        await verify_csrf(request, principal)
        result = await run_in_threadpool(
            lambda: application.state.mutations.complete_toilet_request(
                request_id=request_id,
                actor=principal.actor,
                completed_at=_parse_completion(returned_at),
            )
        )
        _notify(application, result)
        return result.value

    @application.post("/api/proctor/resolve/{request_id}")
    async def resolve_support_endpoint(
        request_id: int,
        request: Request,
        principal: Principal = Depends(require_staff),
    ):
        await verify_csrf(request, principal)
        result = await run_in_threadpool(
            lambda: application.state.mutations.resolve_support_request(
                request_id=request_id, actor=principal.actor
            )
        )
        _notify(application, result)
        return result.value

    # -- WebSockets --------------------------------------------------------

    @application.websocket("/ws/student")
    async def student_websocket(websocket: WebSocket):
        authenticated = await authenticate_student_websocket(websocket)
        if authenticated.principal is None:
            if authenticated.status is None:
                await websocket.close(code=4403)
                return
            await websocket.accept(headers=list(authenticated.response_headers))
            await websocket.close(
                code=1013 if authenticated.status is CMSAuthStatus.UNAVAILABLE else 4401
            )
            return
        principal = authenticated.principal
        await websocket.accept(headers=list(authenticated.response_headers))
        application.state.hub.add_student(
            principal.user_id, websocket, principal.locale
        )
        try:
            await websocket.send_json(
                student_state(application, principal.user_id, principal.locale)
            )
            await _hold_websocket_until_deadline(
                websocket,
                _principal_socket_max_age(principal, config.cms_socket_max_age_seconds),
            )
        except asyncio.TimeoutError:
            await websocket.close(code=4001)
        except WebSocketDisconnect:
            pass
        finally:
            application.state.hub.remove_student(principal.user_id, websocket)

    @application.websocket("/ws/staff")
    async def staff_websocket(websocket: WebSocket):
        principal = authenticate_staff_websocket(websocket)
        if principal is None:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        subscription = StaffSubscription(
            principal.subject,
            principal.roles,
            principal.all_classes,
            principal.class_scope,
        )
        application.state.hub.add_staff(websocket, subscription)
        try:
            await websocket.send_json(
                await live_staff_state(application, principal.actor)
            )
            await _hold_websocket_until_deadline(
                websocket,
                _principal_socket_max_age(principal, config.cms_socket_max_age_seconds),
            )
        except asyncio.TimeoutError:
            await websocket.close(code=4001)
        except WebSocketDisconnect:
            pass
        finally:
            application.state.hub.remove_staff(websocket)

    return application
