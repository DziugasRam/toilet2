"""Serialized mutation boundary and deterministic multi-resource scheduler.

Every public write method acquires the process-wide lock, opens ``BEGIN
IMMEDIATE``, performs validation/state/audit/reconciliation, and commits once.
Callers perform CMS/Olimp-control network work first and pass immutable response
snapshots into this service.  Returned values are detached dictionaries rather
than live ORM entities.
"""

from __future__ import annotations

import json
import math
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

try:
    from .database import Database, GLOBAL_WRITE_LOCK
    from .models import (
        OPEN_STATUSES,
        REQUEST_TYPES,
        ROLE_ADMIN,
        ROLE_PROCTOR,
        ROUTING_FALLBACK_ALL,
        ROUTING_MULTIPLE_CLASSES,
        ROUTING_NORMAL,
        ROUTING_SUPPORT,
        AuditEvent,
        BrowserSession,
        OperationalAlert,
        OperatorAccount,
        RateLimitBucket,
        Request,
        RequestClassSnapshot,
        RequestToiletLock,
        SchoolClass,
        Toilet,
        User,
        utc_now,
    )
    from .migrations import LEGACY_CLASS_NAMESPACE
    from .passwords import hash_password, verify_password
except ImportError:  # pragma: no cover - legacy ``uvicorn main:app`` style
    from database import Database, GLOBAL_WRITE_LOCK
    from models import (
        OPEN_STATUSES,
        REQUEST_TYPES,
        ROLE_ADMIN,
        ROLE_PROCTOR,
        ROUTING_FALLBACK_ALL,
        ROUTING_MULTIPLE_CLASSES,
        ROUTING_NORMAL,
        ROUTING_SUPPORT,
        AuditEvent,
        BrowserSession,
        OperationalAlert,
        OperatorAccount,
        RateLimitBucket,
        Request,
        RequestClassSnapshot,
        RequestToiletLock,
        SchoolClass,
        Toilet,
        User,
        utc_now,
    )
    from migrations import LEGACY_CLASS_NAMESPACE
    from passwords import hash_password, verify_password


ROUTING_ALERT_CODES = frozenset(
    {
        "multiple_classes",
        "computer_without_class",
        "student_not_found",
        "no_computers",
        "no_class",
        "class_without_toilet",
        "assignment_lookup_failed",
        "assignment_malformed",
        "no_toilets",
    }
)
GLOBAL_ALERT_CODES = frozenset(
    {
        "student_not_found",
        "no_computers",
        "no_class",
        "class_without_toilet",
        "assignment_lookup_failed",
        "assignment_malformed",
        "no_toilets",
        "active_toilet_deleted",
    }
)
SECRET_KEYS = frozenset(
    {"password", "cookie", "token", "secret", "authorization", "hmac", "signature"}
)
OPERATOR_USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@+-]{0,149}$")


class ServiceError(Exception):
    status_code = 400
    code = "service_error"

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NotFoundError(ServiceError):
    status_code = 404
    code = "not_found"


class ConflictError(ServiceError):
    status_code = 409
    code = "conflict"


class ForbiddenError(ServiceError):
    status_code = 403
    code = "forbidden"


class ValidationError(ServiceError):
    status_code = 400
    code = "validation_error"


class RateLimitExceeded(ServiceError):
    status_code = 429
    code = "rate_limited"


class _CommitRejection(Exception):
    """Commit audit/rate-limit state, then surface the contained error."""

    def __init__(self, error: ServiceError) -> None:
        self.error = error


@dataclass(frozen=True)
class Actor:
    kind: str
    identifier: str
    roles: frozenset[str] = field(default_factory=frozenset)
    class_scope: frozenset[str] = field(default_factory=frozenset)
    all_classes: bool = False

    @classmethod
    def system(cls, identifier: str = "toilet2") -> "Actor":
        return cls("system", identifier, frozenset({ROLE_ADMIN}), frozenset(), True)

    @classmethod
    def student(cls, username: str) -> "Actor":
        return cls("student", username)


@dataclass(frozen=True)
class MutationResult:
    value: dict[str, Any]
    student_user_ids: frozenset[int] = field(default_factory=frozenset)
    staff_changed: bool = True
    request_ids: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True)
class _Assignment:
    snapshot: dict[str, Any]
    classes: tuple[dict[str, Any], ...]
    anomaly_codes: frozenset[str]
    failed: bool


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_value(value: Any) -> Any:
    """Recursively remove accidental credentials from audit/snapshot input."""

    if isinstance(value, Mapping):
        return {
            str(key): _safe_value(item)
            for key, item in value.items()
            if not _is_secret_key(str(key))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _is_secret_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return normalized in SECRET_KEYS or any(
        marker in normalized
        for marker in ("password", "cookie", "secret", "authorization", "hmac", "signature")
    ) or normalized == "csrf_token" or normalized.endswith("_token")


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _canonical_uuid(value: Any, *, field_name: str = "public_id") -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError(f"invalid {field_name}") from exc


class MutationService:
    """The only supported write API for toilet2 state.

    Integration contract:

    * call :meth:`Database.initialize` once before constructing the service;
    * complete CMS/Olimp HTTP calls before invoking a mutation;
    * pass the full Olimp assignment mapping (or ``None`` on outage) to
      :meth:`create_request`;
    * translate :class:`ServiceError.status_code` at the HTTP boundary;
    * broadcast only after a method returns ``MutationResult`` successfully,
      using its user/request/staff notification hints.

    Configuration creation and rename are deliberately non-reconciling.
    Class-to-toilet changes rebuild pending routes but preserve active ones.
    Toilet deletion is the sole configuration operation that may demote an
    active request because the referenced resource no longer exists.
    """

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] = utc_now,
        student_rate_limit: int = 10,
        student_rate_window_seconds: int = 60,
        operator_login_rate_limit: int = 10,
        operator_login_rate_window_seconds: int = 60,
        general_request_types: Iterable[str] = (),
        write_lock=GLOBAL_WRITE_LOCK,
    ) -> None:
        self.database = database
        self.clock = clock
        self.student_rate_limit = int(student_rate_limit)
        self.student_rate_window_seconds = int(student_rate_window_seconds)
        self.operator_login_rate_limit = int(operator_login_rate_limit)
        self.operator_login_rate_window_seconds = int(operator_login_rate_window_seconds)
        # Fail closed: without an explicit configuration only toilet requests
        # exist, so ``paper`` and any other support type is rejected.
        self.general_request_types = frozenset(str(item) for item in general_request_types)
        unknown_request_types = self.general_request_types - (
            set(REQUEST_TYPES) - {"toilet"}
        )
        if unknown_request_types:
            raise ValueError(
                "unknown configured general request types: "
                + ", ".join(sorted(unknown_request_types))
            )
        self._legacy_catalog_revision: int | None = None
        self._write_lock = write_lock

    # -- transaction/audit primitives -------------------------------------

    def _run(self, operation: Callable[[Session], MutationResult]) -> MutationResult:
        rejection: ServiceError | None = None
        result: MutationResult | None = None
        with self._write_lock:
            with self.database.immediate_session() as session:
                try:
                    result = operation(session)
                except _CommitRejection as committed:
                    session.commit()
                    rejection = committed.error
                else:
                    session.commit()
        if rejection is not None:
            raise rejection
        assert result is not None
        return result

    def _correlation(self, correlation_id: str | None) -> str:
        if correlation_id is None:
            return str(uuid.uuid4())
        return _canonical_uuid(correlation_id, field_name="correlation_id")

    def _actor(self, actor: Actor | None) -> Actor:
        return actor or Actor.system()

    def _audit(
        self,
        session: Session,
        *,
        actor: Actor,
        action: str,
        target_type: str,
        target_identifier: str | int,
        correlation_id: str,
        details: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        session.add(
            AuditEvent(
                occurred_at=now or self.clock(),
                actor_kind=actor.kind,
                actor_identifier=actor.identifier,
                action=action,
                target_type=target_type,
                target_identifier=str(target_identifier),
                correlation_id=correlation_id,
                details_json=_canonical_json(_safe_value(details or {})),
            )
        )

    def _require_admin(self, actor: Actor) -> None:
        if actor.kind != "system" and ROLE_ADMIN not in actor.roles:
            raise ForbiddenError("administrator role required")

    def _require_staff(self, actor: Actor) -> None:
        if actor.kind != "system" and ROLE_PROCTOR not in actor.roles:
            raise ForbiddenError("proctor role required")

    @staticmethod
    def _require_proctor_scope(
        actor: Actor,
        class_ids: set[str],
        *,
        global_scope: bool = False,
    ) -> None:
        if (
            actor.kind != "system"
            and not actor.all_classes
            and not global_scope
            and class_ids
            and not (class_ids & actor.class_scope)
        ):
            raise ForbiddenError("request is outside proctor class scope")

    @staticmethod
    def _user_ids_for_requests(session: Session, request_ids: Iterable[int]) -> set[int]:
        ids = set(request_ids)
        if not ids:
            return set()
        return {
            row[0]
            for row in session.query(Request.user_id).filter(Request.id.in_(ids)).all()
        }

    # -- rate limiting ------------------------------------------------------

    @staticmethod
    def _window_start(now: datetime, seconds: int) -> datetime:
        aware = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        epoch = int(aware.timestamp())
        return datetime.fromtimestamp(epoch - (epoch % seconds), UTC).replace(tzinfo=None)

    def _consume_rate_limit(
        self,
        session: Session,
        *,
        actor_key: str,
        action_group: str,
        limit: int,
        window_seconds: int,
        actor: Actor,
        correlation_id: str,
    ) -> None:
        if limit <= 0:
            return
        if window_seconds <= 0:
            raise ValueError("rate limit window must be positive")
        now = self.clock()
        window = self._window_start(now, window_seconds)
        # Keep the current and previous window for diagnostics, but do not let
        # attacker-controlled actor keys grow this table forever.
        session.query(RateLimitBucket).filter(
            RateLimitBucket.action_group == action_group,
            RateLimitBucket.window_started_at
            < window - timedelta(seconds=window_seconds),
        ).delete(synchronize_session=False)
        key = (actor_key, action_group, window)
        bucket = session.get(RateLimitBucket, key)
        if bucket is None:
            bucket = RateLimitBucket(
                actor_key=actor_key,
                action_group=action_group,
                window_started_at=window,
                count=0,
            )
            session.add(bucket)
        if bucket.count >= limit:
            first_rejection = bucket.count == limit
            bucket.count = min(bucket.count + 1, limit + 1)
            retry_after = max(
                1,
                math.ceil((window + timedelta(seconds=window_seconds) - now).total_seconds()),
            )
            if first_rejection:
                self._audit(
                    session,
                    actor=actor,
                    action="rate_limit.rejected",
                    target_type="rate_limit",
                    target_identifier=action_group,
                    correlation_id=correlation_id,
                    details={"retry_after": retry_after, "window_seconds": window_seconds},
                    now=now,
                )
            raise _CommitRejection(
                RateLimitExceeded("too many requests", retry_after=retry_after)
            )
        bucket.count += 1

    def consume_operator_login_attempt(
        self,
        *,
        username: str,
        client_key: str,
        correlation_id: str | None = None,
    ) -> MutationResult:
        correlation = self._correlation(correlation_id)
        normalized = username.strip().casefold()
        actor = Actor("operator_login", normalized or "<empty>")

        def operation(session: Session) -> MutationResult:
            self._consume_rate_limit(
                session,
                actor_key=f"operator-ip:{client_key}",
                action_group="operator_login_ip",
                limit=self.operator_login_rate_limit,
                window_seconds=self.operator_login_rate_window_seconds,
                actor=actor,
                correlation_id=correlation,
            )
            self._consume_rate_limit(
                session,
                actor_key=f"operator:{normalized}:{client_key}",
                action_group="operator_login",
                limit=self.operator_login_rate_limit,
                window_seconds=self.operator_login_rate_window_seconds,
                actor=actor,
                correlation_id=correlation,
            )
            return MutationResult({"ok": True}, staff_changed=False)

        return self._run(operation)

    def consume_student_assignment_lookup_attempt(
        self,
        *,
        user_id: int,
        actor: Actor | None = None,
        correlation_id: str | None = None,
    ) -> MutationResult:
        """Bound calls to the external assignment API before making the call."""

        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            user = session.get(User, user_id)
            if user is None:
                raise NotFoundError("user not found")
            request_actor = actor or Actor.student(user.username)
            self._consume_rate_limit(
                session,
                actor_key=f"student:{user.username}",
                action_group="student_assignment_lookup",
                limit=self.student_rate_limit,
                window_seconds=self.student_rate_window_seconds,
                actor=request_actor,
                correlation_id=correlation,
            )
            return MutationResult({"ok": True}, staff_changed=False)

        return self._run(operation)

    # -- users and sessions -------------------------------------------------

    def ensure_student(
        self,
        username: str,
        *,
        control_id: int | None = None,
        actor: Actor | None = None,
        correlation_id: str | None = None,
    ) -> MutationResult:
        username = username.strip()
        if not username:
            raise ValidationError("username required")
        if control_id is not None and (
            isinstance(control_id, bool) or not isinstance(control_id, int) or control_id <= 0
        ):
            raise ValidationError("invalid Olimp-control student id")
        audit_actor = self._actor(actor)
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            user = session.query(User).filter_by(username=username).first()
            created = user is None
            if user is None:
                user = User(
                    username=username,
                    control_id=control_id,
                    enabled=True,
                )
                session.add(user)
                session.flush()
            else:
                if control_id is not None:
                    user.control_id = control_id
                user.enabled = True
            if created:
                self._audit(
                    session,
                    actor=audit_actor,
                    action="user.created",
                    target_type="user",
                    target_identifier=user.id,
                    correlation_id=correlation,
                    details={"username": username},
                )
            return MutationResult(
                {
                    "id": user.id,
                    "username": user.username,
                    "userid": user.username,
                    "control_id": user.control_id,
                    "enabled": user.enabled,
                    "created": created,
                },
                frozenset({user.id}),
                staff_changed=False,
            )

        return self._run(operation)

    def get_student(self, userid: str) -> dict[str, Any] | None:
        """Return one enabled synchronized student by durable userid."""

        userid = userid.strip()
        if not userid:
            return None
        session = self.database.SessionLocal()
        try:
            user = (
                session.query(User)
                .filter(User.username == userid, User.enabled.is_(True))
                .first()
            )
            if user is None:
                return None
            return {
                "id": user.id,
                "control_id": user.control_id,
                "userid": user.username,
                "username": user.username,
                "enabled": user.enabled,
            }
        finally:
            session.close()

    def sync_students(
        self,
        students: Sequence[Mapping[str, Any]],
        *,
        actor: Actor | None = None,
        correlation_id: str | None = None,
    ) -> MutationResult:
        """Replace the enabled Olimp-control student roster.

        Omitted students are disabled rather than deleted so local request and
        audit history remains referentially intact.
        """

        prepared: list[dict[str, Any]] = []
        seen_userids: set[str] = set()
        seen_control_ids: set[int] = set()
        for item in students:
            if not isinstance(item, Mapping):
                raise ValidationError("student catalog entry must be an object")
            control_id = item.get("id")
            userid = item.get("userid")
            if (
                isinstance(control_id, bool)
                or not isinstance(control_id, int)
                or control_id <= 0
            ):
                raise ValidationError("invalid Olimp-control student id")
            if not isinstance(userid, str) or not userid.strip():
                raise ValidationError("invalid student userid")
            userid = userid.strip()
            if userid in seen_userids or control_id in seen_control_ids:
                raise ValidationError("duplicate student catalog entry")
            seen_userids.add(userid)
            seen_control_ids.add(control_id)
            prepared.append(
                {
                    "control_id": control_id,
                    "userid": userid,
                }
            )

        audit_actor = self._actor(actor)
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            existing = {item.username: item for item in session.query(User).all()}
            existing_by_control_id = {
                item.control_id: item
                for item in existing.values()
                if item.control_id is not None
            }
            created: list[str] = []
            updated: list[str] = []
            changed_ids: set[int] = set()
            for item in prepared:
                user = existing.get(item["userid"])
                conflicting = existing_by_control_id.get(item["control_id"])
                if conflicting is not None and conflicting is not user:
                    raise ValidationError(
                        "Olimp-control student id is assigned to another userid"
                    )
                if user is None:
                    user = User(
                        username=item["userid"],
                        control_id=item["control_id"],
                        enabled=True,
                    )
                    session.add(user)
                    session.flush()
                    existing[user.username] = user
                    existing_by_control_id[user.control_id] = user
                    created.append(user.username)
                    changed_ids.add(user.id)
                    continue
                changed = (
                    user.control_id != item["control_id"]
                    or not user.enabled
                )
                user.control_id = item["control_id"]
                user.enabled = True
                if changed:
                    updated.append(user.username)
                    changed_ids.add(user.id)

            disabled: list[str] = []
            for userid, user in existing.items():
                if userid not in seen_userids and user.enabled:
                    user.enabled = False
                    disabled.append(userid)
                    changed_ids.add(user.id)

            if created or updated or disabled:
                self._audit(
                    session,
                    actor=audit_actor,
                    action="student.catalog_synced",
                    target_type="student_catalog",
                    target_identifier="olimp-control",
                    correlation_id=correlation,
                    details={
                        "created": sorted(created),
                        "updated": sorted(updated),
                        "disabled": sorted(disabled),
                        "count": len(prepared),
                    },
                )
            return MutationResult(
                {
                    "created": sorted(created),
                    "updated": sorted(updated),
                    "disabled": sorted(disabled),
                    "count": len(prepared),
                },
                frozenset(changed_ids),
                staff_changed=bool(created or updated or disabled),
            )

        return self._run(operation)

    # -- toilet-local operator accounts -----------------------------------

    @staticmethod
    def _normalize_operator_authority(
        roles: Iterable[str],
        class_scope: Iterable[str],
        all_classes: bool,
    ) -> tuple[frozenset[str], frozenset[str], bool]:
        normalized_roles = frozenset(str(role) for role in roles)
        if not normalized_roles or normalized_roles - {ROLE_ADMIN, ROLE_PROCTOR}:
            raise ValidationError("operator requires an admin and/or proctor role")
        normalized_scope = frozenset(
            _canonical_uuid(value, field_name="class scope") for value in class_scope
        )
        normalized_all = bool(all_classes)
        if ROLE_PROCTOR not in normalized_roles:
            if normalized_scope or normalized_all:
                raise ValidationError("only a proctor can have class scope")
            return normalized_roles, frozenset(), False
        if normalized_all and normalized_scope:
            raise ValidationError("all-class proctor must not have explicit class scope")
        if not normalized_all and not normalized_scope:
            raise ValidationError("proctor requires class scope or all-class access")
        return normalized_roles, normalized_scope, normalized_all

    def upsert_operator(
        self,
        *,
        username: str,
        display_name: str,
        roles: Iterable[str],
        class_scope: Iterable[str] = (),
        all_classes: bool = False,
        password: str | None = None,
        enabled: bool = True,
        actor: Actor | None = None,
        correlation_id: str | None = None,
    ) -> MutationResult:
        username = username.strip()
        display_name = display_name.strip()
        if OPERATOR_USERNAME_RE.fullmatch(username) is None:
            raise ValidationError(
                "operator username must start with a letter, digit, or underscore "
                "and contain only letters, digits, '_', '-', '.', '+', or '@'"
            )
        if not display_name:
            raise ValidationError("operator display name required")
        normalized_roles, normalized_scope, normalized_all = (
            self._normalize_operator_authority(roles, class_scope, all_classes)
        )
        if password is not None:
            try:
                password_hash = hash_password(password)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        else:
            password_hash = None
        audit_actor = self._actor(actor)
        self._require_admin(audit_actor)
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            account = session.get(OperatorAccount, username)
            created = account is None
            if account is None:
                if password_hash is None:
                    raise ValidationError("password required for a new operator")
                account = OperatorAccount(
                    username=username,
                    password_hash=password_hash,
                    created_at=self.clock(),
                )
                session.add(account)
            elif (
                ROLE_ADMIN in account.roles
                and account.enabled
                and (ROLE_ADMIN not in normalized_roles or not enabled)
            ):
                other_admin = any(
                    item.username != username
                    and item.enabled
                    and ROLE_ADMIN in item.roles
                    for item in session.query(OperatorAccount).all()
                )
                if not other_admin:
                    raise ConflictError("cannot remove the last enabled administrator")

            account.display_name = display_name
            account.roles_json = _canonical_json(sorted(normalized_roles))
            account.class_scope_json = _canonical_json(sorted(normalized_scope))
            account.all_classes = normalized_all
            account.enabled = bool(enabled)
            account.updated_at = self.clock()
            if password_hash is not None:
                account.password_hash = password_hash
            session.query(BrowserSession).filter(
                BrowserSession.subject_type == "operator",
                BrowserSession.subject == username,
            ).delete(synchronize_session=False)
            self._audit(
                session,
                actor=audit_actor,
                action="operator.created" if created else "operator.updated",
                target_type="operator",
                target_identifier=username,
                correlation_id=correlation,
                details={
                    "display_name": display_name,
                    "roles": sorted(normalized_roles),
                    "class_scope": sorted(normalized_scope),
                    "all_classes": normalized_all,
                    "enabled": bool(enabled),
                    "password_changed": password_hash is not None,
                },
            )
            return MutationResult(
                {
                    "username": username,
                    "display_name": display_name,
                    "roles": sorted(normalized_roles),
                    "class_scope": sorted(normalized_scope),
                    "all_classes": normalized_all,
                    "enabled": bool(enabled),
                    "created": created,
                }
            )

        return self._run(operation)

    def delete_operator(
        self,
        username: str,
        *,
        actor: Actor | None = None,
        correlation_id: str | None = None,
    ) -> MutationResult:
        username = username.strip()
        audit_actor = self._actor(actor)
        self._require_admin(audit_actor)
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            account = session.get(OperatorAccount, username)
            if account is None:
                raise NotFoundError("operator not found")
            if account.enabled and ROLE_ADMIN in account.roles:
                other_admin = any(
                    item.username != username
                    and item.enabled
                    and ROLE_ADMIN in item.roles
                    for item in session.query(OperatorAccount).all()
                )
                if not other_admin:
                    raise ConflictError("cannot delete the last enabled administrator")
            session.query(BrowserSession).filter(
                BrowserSession.subject_type == "operator",
                BrowserSession.subject == username,
            ).delete(synchronize_session=False)
            session.delete(account)
            self._audit(
                session,
                actor=audit_actor,
                action="operator.deleted",
                target_type="operator",
                target_identifier=username,
                correlation_id=correlation,
            )
            return MutationResult({"username": username, "deleted": True})

        return self._run(operation)

    def authenticate_operator(
        self, username: str, password: str
    ) -> dict[str, Any] | None:
        username = username.strip()
        session = self.database.SessionLocal()
        try:
            account = session.get(OperatorAccount, username)
            # Always perform a bcrypt verification to reduce account-enumeration
            # timing differences. The fallback hash is not a credential.
            password_hash = (
                account.password_hash
                if account is not None
                else "$2b$12$zi3vnUbJuuNZM/sJBwXGiOvZDAVVMN02e/Dwt/dPKgvxfYpuzXyQ2"
            )
            valid_password = verify_password(password, password_hash)
            if account is None or not account.enabled or not valid_password:
                return None
            roles = account.roles
            scopes = account.class_scope
            if (
                not roles
                or roles - {ROLE_ADMIN, ROLE_PROCTOR}
                or (ROLE_PROCTOR not in roles and (scopes or account.all_classes))
                or (ROLE_PROCTOR in roles and not account.all_classes and not scopes)
                or (account.all_classes and scopes)
            ):
                return None
            return {
                "username": account.username,
                "display_name": account.display_name,
                "roles": roles,
                "class_scope": scopes,
                "all_classes": account.all_classes,
            }
        finally:
            session.close()

    def list_operators(self) -> list[dict[str, Any]]:
        session = self.database.SessionLocal()
        try:
            return [
                {
                    "username": item.username,
                    "display_name": item.display_name,
                    "roles": sorted(item.roles),
                    "class_scope": sorted(item.class_scope),
                    "all_classes": item.all_classes,
                    "enabled": item.enabled,
                    "username_safe": OPERATOR_USERNAME_RE.fullmatch(
                        item.username
                    )
                    is not None,
                }
                for item in session.query(OperatorAccount)
                .order_by(OperatorAccount.username)
                .all()
            ]
        finally:
            session.close()

    def issue_session(
        self,
        *,
        subject_type: str,
        subject: str,
        user_id: int | None = None,
        display_name: str = "",
        roles: Iterable[str] = (),
        class_scope: Iterable[str] = (),
        all_classes: bool = False,
        contest: str | None = None,
        ttl_seconds: int = 8 * 60 * 60,
        actor: Actor | None = None,
        correlation_id: str | None = None,
        issuance_rate_key: str | None = None,
        issuance_rate_limit: int | None = None,
        issuance_rate_window_seconds: int | None = None,
    ) -> MutationResult:
        if ttl_seconds <= 0:
            raise ValidationError("session lifetime must be positive")
        if subject_type not in {"cms", "operator"}:
            raise ValidationError("unknown session subject type")
        subject = subject.strip()
        if not subject:
            raise ValidationError("session subject required")
        roles_set = frozenset(str(role) for role in roles)
        unknown_roles = roles_set - {ROLE_ADMIN, ROLE_PROCTOR}
        if unknown_roles:
            raise ValidationError("unknown operator role")
        scopes = frozenset(_canonical_uuid(value, field_name="class scope") for value in class_scope)
        if subject_type == "cms":
            if user_id is None:
                raise ValidationError("CMS session requires a student user")
            if not isinstance(contest, str) or not contest.strip():
                raise ValidationError("CMS session requires a contest")
            if roles_set or scopes or all_classes:
                raise ValidationError("CMS session cannot contain operator authority")
            contest = contest.strip()
        else:
            if not roles_set:
                raise ValidationError("operator session requires a role")
            if user_id is not None:
                raise ValidationError("operator session cannot reference a student user")
            if contest is not None:
                raise ValidationError("operator session cannot reference a CMS contest")
        audit_actor = self._actor(actor)
        correlation = self._correlation(correlation_id)
        now = self.clock()
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)

        def operation(session: Session) -> MutationResult:
            if user_id is not None and session.get(User, user_id) is None:
                raise NotFoundError("user not found")
            if issuance_rate_key is not None:
                self._consume_rate_limit(
                    session,
                    actor_key=issuance_rate_key,
                    action_group="cms_session_issuance",
                    limit=(
                        self.student_rate_limit
                        if issuance_rate_limit is None
                        else issuance_rate_limit
                    ),
                    window_seconds=(
                        self.student_rate_window_seconds
                        if issuance_rate_window_seconds is None
                        else issuance_rate_window_seconds
                    ),
                    actor=audit_actor,
                    correlation_id=correlation,
                )
            expired_count = (
                session.query(BrowserSession)
                .filter(BrowserSession.expires_at <= now)
                .delete(synchronize_session=False)
            )
            if expired_count:
                self._audit(
                    session,
                    actor=Actor.system("session-cleanup"),
                    action="session.expired_purged",
                    target_type="session",
                    target_identifier="expired",
                    correlation_id=correlation,
                    details={"count": expired_count},
                    now=now,
                )
            browser_session = BrowserSession(
                token=token,
                user_id=user_id,
                subject_type=subject_type,
                subject=subject,
                display_name=display_name or subject,
                csrf_token=csrf_token,
                roles_json=_canonical_json(sorted(roles_set)),
                class_scope_json=_canonical_json(sorted(scopes)),
                all_classes=bool(all_classes),
                contest=contest,
                created_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            session.add(browser_session)
            self._audit(
                session,
                actor=audit_actor,
                action="session.issued",
                target_type="session",
                target_identifier=subject,
                correlation_id=correlation,
                details={
                    "subject_type": subject_type,
                    "roles": sorted(roles_set),
                    "class_scope": sorted(scopes),
                    "all_classes": bool(all_classes),
                    "expires_at": browser_session.expires_at,
                },
                now=now,
            )
            # Tokens are returned only to the direct caller and never audited.
            return MutationResult(
                {
                    "token": token,
                    "csrf_token": csrf_token,
                    "expires_at": browser_session.expires_at,
                },
                staff_changed=False,
            )

        return self._run(operation)

    def get_session(self, token: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        session = self.database.SessionLocal()
        try:
            browser_session = session.get(BrowserSession, token)
            if browser_session is None or browser_session.is_expired(now or self.clock()):
                return None
            roles = browser_session.roles
            scopes = browser_session.class_scope
            if browser_session.subject_type == "cms":
                valid = (
                    browser_session.user_id is not None
                    and bool(browser_session.contest)
                    and not roles
                    and not scopes
                    and not browser_session.all_classes
                )
            elif browser_session.subject_type == "operator":
                valid = (
                    browser_session.user_id is None
                    and bool(roles)
                    and roles <= {ROLE_ADMIN, ROLE_PROCTOR}
                    and browser_session.contest is None
                )
            else:
                valid = False
            if not valid:
                return None
            return {
                "token": browser_session.token,
                "user_id": browser_session.user_id,
                "subject_type": browser_session.subject_type,
                "subject": browser_session.subject,
                "display_name": browser_session.display_name,
                "csrf_token": browser_session.csrf_token,
                "roles": sorted(roles),
                "class_scope": sorted(scopes),
                "all_classes": browser_session.all_classes,
                "contest": browser_session.contest,
                "expires_at": browser_session.expires_at,
            }
        finally:
            session.close()

    def revoke_session(
        self,
        token: str,
        *,
        actor: Actor | None = None,
        correlation_id: str | None = None,
    ) -> MutationResult:
        audit_actor = self._actor(actor)
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            browser_session = session.get(BrowserSession, token)
            if browser_session is None:
                return MutationResult({"revoked": False}, staff_changed=False)
            subject = browser_session.subject
            session.delete(browser_session)
            self._audit(
                session,
                actor=audit_actor,
                action="session.revoked",
                target_type="session",
                target_identifier=subject,
                correlation_id=correlation,
            )
            return MutationResult({"revoked": True}, staff_changed=False)

        return self._run(operation)

    def purge_expired_sessions(
        self,
        *,
        actor: Actor | None = None,
        correlation_id: str | None = None,
    ) -> MutationResult:
        audit_actor = self._actor(actor)
        correlation = self._correlation(correlation_id)
        now = self.clock()

        def operation(session: Session) -> MutationResult:
            count = (
                session.query(BrowserSession)
                .filter(BrowserSession.expires_at <= now)
                .delete(synchronize_session=False)
            )
            if count:
                self._audit(
                    session,
                    actor=audit_actor,
                    action="session.expired_purged",
                    target_type="session",
                    target_identifier="expired",
                    correlation_id=correlation,
                    details={"count": count},
                    now=now,
                )
            return MutationResult({"count": count}, staff_changed=False)

        return self._run(operation)

    def record_security_event(
        self,
        action: str,
        *,
        target_identifier: str,
        details: Mapping[str, Any] | None = None,
        actor: Actor | None = None,
        correlation_id: str | None = None,
    ) -> MutationResult:
        audit_actor = self._actor(actor)
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            self._audit(
                session,
                actor=audit_actor,
                action=action,
                target_type="security",
                target_identifier=target_identifier,
                correlation_id=correlation,
                details=details,
            )
            return MutationResult({"ok": True}, staff_changed=False)

        return self._run(operation)

    # -- assignment normalization/routing ----------------------------------

    def _normalize_assignment(self, assignment: Mapping[str, Any] | None) -> _Assignment:
        if assignment is None:
            snapshot = {
                "lookup_ok": False,
                "lookup_error": "assignment_service_failure",
                "computers": [],
                "classes": [],
                "anomalies": [{"code": "assignment_lookup_failed"}],
            }
            return _Assignment(snapshot, (), frozenset({"assignment_lookup_failed"}), True)

        safe = _safe_value(assignment)
        assert isinstance(safe, dict)
        failed = bool(safe.get("lookup_ok") is False or safe.get("error"))
        anomaly_codes: set[str] = set()
        anomaly_details: dict[str, dict[str, Any]] = {}
        for anomaly in safe.get("anomalies", []) or []:
            if isinstance(anomaly, Mapping) and anomaly.get("code"):
                code = str(anomaly["code"])
                anomaly_codes.add(code)
                anomaly_details[code] = dict(anomaly)
            elif isinstance(anomaly, str):
                anomaly_codes.add(anomaly)
                anomaly_details[anomaly] = {"code": anomaly}
        if failed:
            anomaly_codes.add("assignment_lookup_failed")

        computers_out: list[dict[str, Any]] = []
        class_sources: dict[str, dict[str, Any]] = {}
        malformed = False

        def validated_int(
            value: Any,
            *,
            default: int | None,
            minimum: int | None = None,
            maximum: int | None = None,
        ) -> int | None:
            nonlocal malformed
            if value is None:
                return default
            if isinstance(value, bool) or not isinstance(value, int):
                malformed = True
                return default
            if minimum is not None and value < minimum:
                malformed = True
                return default
            if maximum is not None and value > maximum:
                malformed = True
                return default
            return value

        def add_class(raw_class: Mapping[str, Any], computer_id: str | None = None) -> None:
            nonlocal malformed
            raw_public_id = raw_class.get("public_id", raw_class.get("id"))
            try:
                public_id = _canonical_uuid(raw_public_id, field_name="class public_id")
            except ValidationError:
                malformed = True
                return
            name = str(raw_class.get("name") or public_id)
            sequence_num = validated_int(
                raw_class.get("sequence_num", 0),
                default=0,
            )
            grid_cols = validated_int(
                raw_class.get("grid_cols"),
                default=None,
                minimum=1,
                maximum=30,
            )
            entry = class_sources.setdefault(
                public_id,
                {
                    "public_id": public_id,
                    "name": name,
                    "sequence_num": sequence_num,
                    "grid_cols": grid_cols,
                    "source_computers": [],
                },
            )
            if (
                entry["name"] != name
                or entry["sequence_num"] != sequence_num
                or entry["grid_cols"] != grid_cols
            ):
                malformed = True
            if computer_id and computer_id not in entry["source_computers"]:
                entry["source_computers"].append(computer_id)

        for raw_computer in safe.get("computers", []) or []:
            if not isinstance(raw_computer, Mapping):
                malformed = True
                continue
            computer_id = str(
                raw_computer.get("public_id")
                or raw_computer.get("machine_id")
                or raw_computer.get("id")
                or raw_computer.get("name")
                or "unknown"
            )
            raw_class = raw_computer.get("class") or raw_computer.get("location")
            class_out = None
            if isinstance(raw_class, Mapping):
                before = set(class_sources)
                add_class(raw_class, computer_id)
                new_or_existing = set(class_sources) - before
                raw_id = raw_class.get("public_id", raw_class.get("id"))
                try:
                    canonical = _canonical_uuid(raw_id, field_name="class public_id")
                except ValidationError:
                    canonical = None
                if canonical and canonical in class_sources:
                    class_out = {
                        "public_id": canonical,
                        "name": class_sources[canonical]["name"],
                        "sequence_num": class_sources[canonical]["sequence_num"],
                        "grid_cols": class_sources[canonical]["grid_cols"],
                    }
            else:
                anomaly_codes.add("computer_without_class")

            sequence_num = validated_int(
                raw_computer.get("sequence_num", 0),
                default=0,
            )
            raw_row = raw_computer.get("grid_row")
            raw_col = raw_computer.get("grid_col")
            if (raw_row is None) != (raw_col is None):
                malformed = True
                grid_row = None
                grid_col = None
            elif raw_row is None:
                grid_row = None
                grid_col = None
            else:
                grid_row = validated_int(
                    raw_row,
                    default=None,
                    minimum=1,
                    maximum=30,
                )
                grid_col = validated_int(
                    raw_col,
                    default=None,
                    minimum=1,
                    maximum=30,
                )
                if grid_row is None or grid_col is None:
                    grid_row = None
                    grid_col = None

            raw_student = raw_computer.get("student")
            student_out = None
            if raw_student is not None:
                if not isinstance(raw_student, Mapping):
                    malformed = True
                else:
                    student_id = raw_student.get("id")
                    userid = raw_student.get("userid")
                    if (
                        isinstance(student_id, bool)
                        or not isinstance(student_id, int)
                        or student_id <= 0
                        or not isinstance(userid, str)
                        or not userid.strip()
                    ):
                        malformed = True
                    else:
                        student_out = {
                            "id": student_id,
                            "userid": userid.strip(),
                        }
            computers_out.append(
                {
                    "id": computer_id,
                    "machine_id": computer_id,
                    "name": str(raw_computer.get("name") or computer_id),
                    "sequence_num": sequence_num,
                    "grid_row": grid_row,
                    "grid_col": grid_col,
                    "student": student_out,
                    "class": class_out,
                }
            )

        for raw_class in safe.get("classes", []) or []:
            if isinstance(raw_class, Mapping):
                add_class(raw_class)
            else:
                malformed = True

        if malformed:
            failed = True
            anomaly_codes.add("assignment_malformed")
        classes = tuple(sorted(class_sources.values(), key=lambda item: item["public_id"]))
        if len(classes) > 1:
            anomaly_codes.add("multiple_classes")
        if not classes and not failed:
            anomaly_codes.add("no_class")
        # Retain the complete sanitized service response (identifier and
        # structured anomaly context included), while replacing
        # its routing fields with canonical UUID/deduplicated representations.
        snapshot = dict(safe)
        snapshot.update({
            "lookup_ok": not failed,
            "computers": computers_out,
            "classes": list(classes),
            "anomalies": [
                anomaly_details.get(code, {"code": code})
                for code in sorted(anomaly_codes)
            ],
        })
        if failed and safe.get("error"):
            snapshot["lookup_error"] = str(safe["error"])
        return _Assignment(snapshot, classes, frozenset(anomaly_codes), failed)

    @staticmethod
    def _snapshot_data(request: Request) -> dict[str, Any]:
        try:
            value = json.loads(request.identity_snapshot_json)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _ensure_alert(
        self,
        session: Session,
        request: Request,
        *,
        code: str,
        details: Mapping[str, Any],
        actor: Actor,
        correlation_id: str,
        severity: str = "warning",
        global_scope: bool | None = None,
    ) -> OperationalAlert:
        alert = next((item for item in request.alerts if item.code == code), None)
        now = self.clock()
        opened = alert is None or alert.resolved_at is not None
        if alert is None:
            alert = OperationalAlert(request=request, code=code)
            session.add(alert)
        alert.severity = severity
        alert.global_scope = code in GLOBAL_ALERT_CODES if global_scope is None else global_scope
        alert.details_json = _canonical_json(_safe_value(details))
        if opened:
            alert.created_at = now
            alert.resolved_at = None
            alert.resolved_by = None
            self._audit(
                session,
                actor=actor,
                action="alert.opened",
                target_type="request",
                target_identifier=request.id,
                correlation_id=correlation_id,
                details={"code": code, "severity": severity},
                now=now,
            )
        return alert

    def _resolve_routing_alerts(
        self,
        request: Request,
        *,
        actor: Actor,
        correlation_id: str,
        session: Session,
        keep_codes: frozenset[str] = frozenset(),
    ) -> None:
        now = self.clock()
        for alert in request.alerts:
            if (
                alert.code in ROUTING_ALERT_CODES
                and alert.code not in keep_codes
                and alert.resolved_at is None
            ):
                alert.resolved_at = now
                alert.resolved_by = actor.identifier
                self._audit(
                    session,
                    actor=actor,
                    action="alert.resolved",
                    target_type="alert",
                    target_identifier=alert.id or f"{request.id}:{alert.code}",
                    correlation_id=correlation_id,
                    details={"code": alert.code},
                    now=now,
                )

    def _resolve_all_request_alerts(
        self,
        session: Session,
        request: Request,
        *,
        actor: Actor,
        correlation_id: str,
        now: datetime,
    ) -> None:
        for alert in request.alerts:
            if alert.resolved_at is not None:
                continue
            alert.resolved_at = now
            alert.resolved_by = actor.identifier
            self._audit(
                session,
                actor=actor,
                action="alert.resolved",
                target_type="alert",
                target_identifier=alert.id or f"{request.id}:{alert.code}",
                correlation_id=correlation_id,
                details={"code": alert.code, "request_id": request.id},
                now=now,
            )

    def _rebuild_route(
        self,
        session: Session,
        request: Request,
        *,
        actor: Actor,
        correlation_id: str,
    ) -> None:
        if request.status != "pending" or request.type != "toilet":
            return
        request.toilet_locks.clear()
        snapshot = self._snapshot_data(request)
        anomaly_codes = {
            str(item.get("code"))
            for item in snapshot.get("anomalies", [])
            if isinstance(item, Mapping) and item.get("code")
        }
        class_ids = [item.class_public_id for item in request.class_snapshots]
        local_classes = {
            school_class.public_id: school_class
            for school_class in session.query(SchoolClass)
            .filter(SchoolClass.public_id.in_(class_ids or ["<none>"]))
            .all()
        }
        missing = [
            public_id
            for public_id in class_ids
            if public_id not in local_classes or local_classes[public_id].toilet_id is None
        ]
        failed = snapshot.get("lookup_ok") is False or "assignment_lookup_failed" in anomaly_codes
        fallback = failed or not class_ids or bool(missing)
        if fallback:
            required_toilets = session.query(Toilet).order_by(Toilet.id).all()
            request.routing_mode = ROUTING_FALLBACK_ALL
        else:
            toilet_ids = sorted({local_classes[public_id].toilet_id for public_id in class_ids})
            required_toilets = (
                session.query(Toilet).filter(Toilet.id.in_(toilet_ids)).order_by(Toilet.id).all()
            )
            request.routing_mode = (
                ROUTING_MULTIPLE_CLASSES if len(class_ids) > 1 else ROUTING_NORMAL
            )

        request.blocked_reason = None if required_toilets else "no_toilets"
        desired_alerts = set()
        if len(class_ids) > 1:
            desired_alerts.add("multiple_classes")
        if "computer_without_class" in anomaly_codes:
            desired_alerts.add("computer_without_class")
        desired_alerts.update(
            code
            for code in ("student_not_found", "no_computers", "assignment_malformed")
            if code in anomaly_codes
        )
        if failed:
            desired_alerts.add("assignment_lookup_failed")
        elif not class_ids:
            desired_alerts.add("no_class")
        elif missing:
            desired_alerts.add("class_without_toilet")
        if not required_toilets:
            desired_alerts.add("no_toilets")
        self._resolve_routing_alerts(
            request,
            actor=actor,
            correlation_id=correlation_id,
            session=session,
            keep_codes=frozenset(desired_alerts),
        )
        reason = {
            "routing_mode": request.routing_mode,
            "class_public_ids": sorted(class_ids),
        }
        for toilet in required_toilets:
            request.toilet_locks.append(
                RequestToiletLock(toilet=toilet, reason_json=_canonical_json(reason))
            )

        details = {
            "student": {
                "username": request.user.username,
            },
            "classes": [
                {"public_id": item.class_public_id, "name": item.class_name}
                for item in request.class_snapshots
            ],
            "computers": snapshot.get("computers", []),
            "toilets": [
                {"id": toilet.id, "name": toilet.name} for toilet in required_toilets
            ],
        }
        if len(class_ids) > 1:
            self._ensure_alert(
                session,
                request,
                code="multiple_classes",
                details=details,
                actor=actor,
                correlation_id=correlation_id,
                global_scope=False,
            )
        if "computer_without_class" in anomaly_codes:
            self._ensure_alert(
                session,
                request,
                code="computer_without_class",
                details=details,
                actor=actor,
                correlation_id=correlation_id,
                global_scope=not bool(class_ids),
            )
        for code in ("student_not_found", "no_computers", "assignment_malformed"):
            if code in anomaly_codes:
                self._ensure_alert(
                    session,
                    request,
                    code=code,
                    details=details,
                    actor=actor,
                    correlation_id=correlation_id,
                )
        if failed:
            self._ensure_alert(
                session,
                request,
                code="assignment_lookup_failed",
                details=details,
                actor=actor,
                correlation_id=correlation_id,
            )
        elif not class_ids:
            self._ensure_alert(
                session,
                request,
                code="no_class",
                details=details,
                actor=actor,
                correlation_id=correlation_id,
            )
        elif missing:
            self._ensure_alert(
                session,
                request,
                code="class_without_toilet",
                details={**details, "missing_class_public_ids": missing},
                actor=actor,
                correlation_id=correlation_id,
            )
        if not required_toilets:
            self._ensure_alert(
                session,
                request,
                code="no_toilets",
                details=details,
                actor=actor,
                correlation_id=correlation_id,
            )

    def _schedule(
        self,
        session: Session,
        *,
        actor: Actor,
        correlation_id: str,
    ) -> set[int]:
        # SessionLocal deliberately disables autoflush so endpoint reads cannot
        # surprise callers.  Scheduling is the explicit point where newly built
        # requirement rows and status/config changes must become query-visible.
        session.flush()
        toilets = session.query(Toilet).order_by(Toilet.id).all()
        free = {toilet.id: toilet.capacity for toilet in toilets}
        active = (
            session.query(Request)
            .filter(Request.type == "toilet", Request.status == "active")
            .order_by(Request.created_at, Request.id)
            .all()
        )
        for request in active:
            for lock in request.toilet_locks:
                if lock.toilet_id in free:
                    free[lock.toilet_id] -= 1

        blocked_resources: set[int] = set()
        activated: set[int] = set()
        pending = (
            session.query(Request)
            .filter(Request.type == "toilet", Request.status == "pending")
            .order_by(Request.created_at, Request.id)
            .all()
        )
        now = self.clock()
        for request in pending:
            required = {lock.toilet_id for lock in request.toilet_locks}
            if not required:
                request.blocked_reason = request.blocked_reason or "no_toilets"
                continue
            if required & blocked_resources:
                blocked_resources.update(required)
                continue
            if all(free.get(toilet_id, 0) > 0 for toilet_id in required):
                request.status = "active"
                request.activated_at = now
                request.blocked_reason = None
                for toilet_id in required:
                    free[toilet_id] -= 1
                activated.add(request.id)
                self._audit(
                    session,
                    actor=actor,
                    action="request.activated",
                    target_type="request",
                    target_identifier=request.id,
                    correlation_id=correlation_id,
                    details={"toilet_ids": sorted(required)},
                    now=now,
                )
            else:
                blocked_resources.update(required)
        return activated

    # -- request lifecycle --------------------------------------------------

    def create_request(
        self,
        *,
        user_id: int,
        request_type: str,
        assignment: Mapping[str, Any] | None,
        actor: Actor | None = None,
        correlation_id: str | None = None,
    ) -> MutationResult:
        allowed_request_types = {"toilet"} | set(self.general_request_types)
        if request_type not in allowed_request_types:
            raise ValidationError("request type is not enabled")
        normalized = self._normalize_assignment(assignment)
        if actor is not None and actor.kind == "operator":
            # A proctor may open a request on a contestant's behalf, but only
            # inside their own class scope. Snapshots without a class stay
            # global, matching return/resolve authorization.
            self._require_staff(actor)
            self._require_proctor_scope(
                actor, {item["public_id"] for item in normalized.classes}
            )
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            user = session.get(User, user_id)
            if user is None:
                raise NotFoundError("user not found")
            request_actor = actor or Actor.student(user.username)
            self._consume_rate_limit(
                session,
                actor_key=f"student:{user.username}",
                action_group="student_mutation",
                limit=self.student_rate_limit,
                window_seconds=self.student_rate_window_seconds,
                actor=request_actor,
                correlation_id=correlation,
            )
            existing = (
                session.query(Request)
                .filter(
                    Request.user_id == user.id,
                    Request.type == request_type,
                    Request.status.in_(OPEN_STATUSES),
                )
                .first()
            )
            if existing is not None:
                raise _CommitRejection(
                    ConflictError("user already has an open request of this type")
                )

            request = Request(
                user=user,
                type=request_type,
                status="pending",
                created_at=self.clock(),
                routing_mode=ROUTING_SUPPORT if request_type != "toilet" else ROUTING_NORMAL,
                identity_snapshot_json=_canonical_json(normalized.snapshot),
            )
            session.add(request)
            session.flush()
            for item in normalized.classes:
                snapshot = RequestClassSnapshot(
                    request=request,
                    class_public_id=item["public_id"],
                    class_name=item["name"],
                    source_computers_json=_canonical_json(item["source_computers"]),
                )
                session.add(snapshot)
            session.flush()
            if request_type == "toilet":
                self._rebuild_route(
                    session,
                    request,
                    actor=request_actor,
                    correlation_id=correlation,
                )
            self._audit(
                session,
                actor=request_actor,
                action="request.created",
                target_type="request",
                target_identifier=request.id,
                correlation_id=correlation,
                details={
                    "type": request_type,
                    "class_public_ids": [item["public_id"] for item in normalized.classes],
                    "routing_mode": request.routing_mode,
                },
            )
            activated = self._schedule(
                session, actor=request_actor, correlation_id=correlation
            ) if request_type == "toilet" else set()
            session.flush()
            changed_users = {user.id} | self._user_ids_for_requests(session, activated)
            return MutationResult(
                self._request_dict(request),
                frozenset(changed_users),
                True,
                frozenset({request.id} | activated),
            )

        try:
            return self._run(operation)
        except IntegrityError as exc:
            raise ConflictError("user already has an open request of this type") from exc

    def cancel_request(
        self,
        *,
        request_id: int,
        user_id: int,
        actor: Actor | None = None,
        correlation_id: str | None = None,
    ) -> MutationResult:
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            user = session.get(User, user_id)
            if user is None:
                raise NotFoundError("user not found")
            request_actor = actor or Actor.student(user.username)
            self._consume_rate_limit(
                session,
                actor_key=f"student:{user.username}",
                action_group="student_mutation",
                limit=self.student_rate_limit,
                window_seconds=self.student_rate_window_seconds,
                actor=request_actor,
                correlation_id=correlation,
            )
            request = session.get(Request, request_id)
            if request is None or request.user_id != user_id:
                raise _CommitRejection(NotFoundError("request not found"))
            if request.status not in OPEN_STATUSES:
                raise _CommitRejection(ConflictError("request is not open"))
            if request.type == "toilet" and request.status == "active":
                raise _CommitRejection(
                    ConflictError(
                        "an active toilet visit must be completed by a proctor"
                    )
                )
            now = self.clock()
            request.status = "cancelled"
            request.completed_at = now
            self._resolve_all_request_alerts(
                session,
                request,
                actor=request_actor,
                correlation_id=correlation,
                now=now,
            )
            self._audit(
                session,
                actor=request_actor,
                action="request.cancelled",
                target_type="request",
                target_identifier=request.id,
                correlation_id=correlation,
                details={"was_active": request.activated_at is not None},
                now=now,
            )
            activated = self._schedule(
                session, actor=request_actor, correlation_id=correlation
            ) if request.type == "toilet" else set()
            changed_users = {user_id} | self._user_ids_for_requests(session, activated)
            return MutationResult(
                {"id": request.id, "status": request.status},
                frozenset(changed_users),
                True,
                frozenset({request.id} | activated),
            )

        return self._run(operation)

    def complete_toilet_request(
        self,
        *,
        request_id: int,
        actor: Actor,
        completed_at: datetime | None = None,
        correlation_id: str | None = None,
    ) -> MutationResult:
        self._require_staff(actor)
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            request = session.get(Request, request_id)
            if request is None or request.type != "toilet":
                raise NotFoundError("toilet request not found")
            class_ids = {item.class_public_id for item in request.class_snapshots}
            self._require_proctor_scope(actor, class_ids)
            if request.status != "active":
                raise ConflictError("only an active toilet request can be returned")
            now = self.clock()
            effective = _naive_utc(completed_at) if completed_at is not None else now
            if effective > now:
                raise ValidationError("completion time cannot be in the future")
            if request.activated_at is not None and effective < request.activated_at:
                raise ValidationError("completion time cannot precede activation")
            request.status = "done"
            request.completed_at = effective
            request.manual_completion = completed_at is not None
            request.completed_by = actor.identifier
            self._resolve_all_request_alerts(
                session,
                request,
                actor=actor,
                correlation_id=correlation,
                now=now,
            )
            self._audit(
                session,
                actor=actor,
                action="request.toilet_returned",
                target_type="request",
                target_identifier=request.id,
                correlation_id=correlation,
                details={
                    "completed_at": effective,
                    "manual_completion": completed_at is not None,
                    "class_public_ids": sorted(class_ids),
                },
                now=now,
            )
            activated = self._schedule(session, actor=actor, correlation_id=correlation)
            changed_users = {request.user_id} | self._user_ids_for_requests(
                session, activated
            )
            return MutationResult(
                {"id": request.id, "status": request.status, "completed_at": effective},
                frozenset(changed_users),
                True,
                frozenset({request.id} | activated),
            )

        return self._run(operation)

    def resolve_support_request(
        self,
        *,
        request_id: int,
        actor: Actor,
        correlation_id: str | None = None,
    ) -> MutationResult:
        self._require_staff(actor)
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            request = session.get(Request, request_id)
            if request is None or request.type == "toilet":
                raise NotFoundError("support request not found")
            if request.status != "pending":
                raise ConflictError("support request is not pending")
            class_ids = {item.class_public_id for item in request.class_snapshots}
            self._require_proctor_scope(actor, class_ids)
            now = self.clock()
            request.status = "done"
            request.completed_at = now
            request.completed_by = actor.identifier
            self._audit(
                session,
                actor=actor,
                action="request.support_resolved",
                target_type="request",
                target_identifier=request.id,
                correlation_id=correlation,
                details={"type": request.type, "class_public_ids": sorted(class_ids)},
                now=now,
            )
            return MutationResult(
                {"id": request.id, "status": request.status},
                frozenset({request.user_id}),
                True,
                frozenset({request.id}),
            )

        return self._run(operation)

    # -- configuration ------------------------------------------------------

    @staticmethod
    def _clean_name(name: str) -> str:
        value = name.strip()
        if not value:
            raise ValidationError("name required")
        return value

    def create_toilet(
        self,
        *,
        name: str,
        capacity: int = 1,
        actor: Actor,
        correlation_id: str | None = None,
    ) -> MutationResult:
        self._require_admin(actor)
        clean_name = self._clean_name(name)
        if capacity < 1:
            raise ValidationError("capacity must be at least 1")
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            if session.query(Toilet).filter_by(name=clean_name).first() is not None:
                raise ConflictError("toilet name already exists")
            toilet = Toilet(name=clean_name, capacity=capacity)
            session.add(toilet)
            session.flush()
            self._audit(
                session,
                actor=actor,
                action="toilet.created",
                target_type="toilet",
                target_identifier=toilet.id,
                correlation_id=correlation,
                details={"name": clean_name, "capacity": capacity},
            )
            # Per contract, creation does not rewrite or schedule existing rows.
            return MutationResult(
                {"id": toilet.id, "name": toilet.name, "capacity": toilet.capacity}
            )

        return self._run(operation)

    def rename_toilet(
        self,
        *,
        toilet_id: int,
        name: str,
        actor: Actor,
        correlation_id: str | None = None,
    ) -> MutationResult:
        self._require_admin(actor)
        clean_name = self._clean_name(name)
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            toilet = session.get(Toilet, toilet_id)
            if toilet is None:
                raise NotFoundError("toilet not found")
            duplicate = (
                session.query(Toilet)
                .filter(Toilet.name == clean_name, Toilet.id != toilet_id)
                .first()
            )
            if duplicate is not None:
                raise ConflictError("toilet name already exists")
            old_name = toilet.name
            toilet.name = clean_name
            self._audit(
                session,
                actor=actor,
                action="toilet.renamed",
                target_type="toilet",
                target_identifier=toilet.id,
                correlation_id=correlation,
                details={"old_name": old_name, "new_name": clean_name},
            )
            return MutationResult({"id": toilet.id, "name": toilet.name})

        return self._run(operation)

    def set_toilet_capacity(
        self,
        *,
        toilet_id: int,
        capacity: int,
        actor: Actor,
        correlation_id: str | None = None,
    ) -> MutationResult:
        self._require_admin(actor)
        if capacity < 1:
            raise ValidationError("capacity must be at least 1")
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            toilet = session.get(Toilet, toilet_id)
            if toilet is None:
                raise NotFoundError("toilet not found")
            active_use = (
                session.query(func.count(RequestToiletLock.request_id))
                .join(Request, Request.id == RequestToiletLock.request_id)
                .filter(
                    RequestToiletLock.toilet_id == toilet_id,
                    Request.status == "active",
                )
                .scalar()
                or 0
            )
            if capacity < active_use:
                raise ConflictError("capacity is below current active use")
            old_capacity = toilet.capacity
            toilet.capacity = capacity
            self._audit(
                session,
                actor=actor,
                action="toilet.capacity_changed",
                target_type="toilet",
                target_identifier=toilet.id,
                correlation_id=correlation,
                details={"old_capacity": old_capacity, "new_capacity": capacity},
            )
            activated = (
                self._schedule(session, actor=actor, correlation_id=correlation)
                if capacity > old_capacity
                else set()
            )
            user_ids = {
                row[0]
                for row in session.query(Request.user_id)
                .filter(Request.id.in_(activated or [-1]))
                .all()
            }
            return MutationResult(
                {"id": toilet.id, "capacity": capacity},
                frozenset(user_ids),
                True,
                frozenset(activated),
            )

        return self._run(operation)

    def assign_class_toilet(
        self,
        *,
        class_public_id: str,
        toilet_id: int | None,
        actor: Actor,
        correlation_id: str | None = None,
    ) -> MutationResult:
        self._require_admin(actor)
        public_id = _canonical_uuid(class_public_id, field_name="class public_id")
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            school_class = session.query(SchoolClass).filter_by(public_id=public_id).first()
            if toilet_id is not None and session.get(Toilet, toilet_id) is None:
                raise NotFoundError("toilet not found")
            old_toilet_id = school_class.toilet_id if school_class is not None else None
            if school_class is None and toilet_id is not None:
                # Legacy non-null display columns are populated with neutral
                # compatibility values. Runtime display data remains live.
                school_class = SchoolClass(
                    public_id=public_id,
                    name=public_id,
                    sequence_num=0,
                    toilet_id=toilet_id,
                )
                session.add(school_class)
            elif school_class is not None:
                school_class.toilet_id = toilet_id
            pending = (
                session.query(Request)
                .join(RequestClassSnapshot)
                .filter(
                    Request.type == "toilet",
                    Request.status == "pending",
                    RequestClassSnapshot.class_public_id == public_id,
                )
                .distinct()
                .all()
            )
            for request in pending:
                self._rebuild_route(
                    session, request, actor=actor, correlation_id=correlation
                )
            mapping_retained = False
            if school_class is not None and toilet_id is None:
                has_open_snapshot = (
                    session.query(RequestClassSnapshot.id)
                    .join(Request)
                    .filter(
                        Request.status.in_(OPEN_STATUSES),
                        RequestClassSnapshot.class_public_id == public_id,
                    )
                    .first()
                    is not None
                )
                has_operator_scope = any(
                    public_id in account.class_scope
                    for account in session.query(OperatorAccount).all()
                )
                mapping_retained = has_open_snapshot or has_operator_scope
                if not mapping_retained:
                    session.delete(school_class)
            self._audit(
                session,
                actor=actor,
                action="class.toilet_assigned",
                target_type="class",
                target_identifier=public_id,
                correlation_id=correlation,
                details={
                    "old_toilet_id": old_toilet_id,
                    "new_toilet_id": toilet_id,
                    "mapping_anchor_retained": mapping_retained,
                },
            )
            activated = self._schedule(session, actor=actor, correlation_id=correlation)
            affected = {request.id for request in pending} | activated
            user_ids = {
                row[0]
                for row in session.query(Request.user_id)
                .filter(Request.id.in_(affected or [-1]))
                .all()
            }
            return MutationResult(
                {
                    "class_public_id": public_id,
                    "toilet_id": toilet_id,
                    "mapping_anchor_retained": mapping_retained,
                },
                frozenset(user_ids),
                True,
                frozenset(affected),
            )

        return self._run(operation)

    def reconcile_legacy_class_mapping_keys(
        self,
        live_classes: Sequence[Mapping[str, Any]],
        *,
        actor: Actor | None = None,
        correlation_id: str | None = None,
    ) -> MutationResult:
        """Remap synthetic legacy class keys using exact unique names only.

        This is a one-time mapping-key migration, not a class catalog sync.
        Live names/order/layout are not persisted.
        """

        audit_actor = self._actor(actor)
        self._require_admin(audit_actor)
        normalized: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for item in live_classes:
            if not isinstance(item, Mapping):
                raise ValidationError("malformed live class catalog")
            public_id = _canonical_uuid(
                item.get("public_id", item.get("id")),
                field_name="class public_id",
            )
            name = self._clean_name(str(item.get("name") or ""))
            if public_id in seen_ids:
                raise ValidationError("duplicate live class public_id")
            seen_ids.add(public_id)
            normalized.append({"public_id": public_id, "name": name})
        live_by_name: dict[str, list[str]] = {}
        for item in normalized:
            live_by_name.setdefault(item["name"], []).append(item["public_id"])
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            all_rows = session.query(SchoolClass).order_by(SchoolClass.public_id).all()
            operator_accounts = (
                session.query(OperatorAccount)
                .order_by(OperatorAccount.username)
                .all()
            )
            operator_scope_ids = {
                public_id
                for account in operator_accounts
                for public_id in account.class_scope
            }
            open_snapshot_ids = {
                row[0]
                for row in session.query(RequestClassSnapshot.class_public_id)
                .join(Request)
                .filter(Request.status.in_(OPEN_STATUSES))
                .distinct()
                .all()
            }
            referenced_ids = operator_scope_ids | open_snapshot_ids
            unmatched = [
                item
                for item in all_rows
                if item.public_id not in seen_ids
                and (
                    item.toilet_id is not None
                    or item.public_id in referenced_ids
                )
            ]
            local_by_name: dict[str, list[SchoolClass]] = {}
            for item in unmatched:
                local_by_name.setdefault(item.name, []).append(item)
            existing_by_id = {item.public_id: item for item in all_rows}
            remapped: list[dict[str, Any]] = []
            unresolved: list[dict[str, Any]] = []
            key_remaps: dict[str, str] = {}

            for mapping in unmatched:
                base = {
                    "legacy_public_id": mapping.public_id,
                    "legacy_name": mapping.name,
                    "toilet_id": mapping.toilet_id,
                }
                expected_legacy_id = str(
                    uuid.uuid5(
                        LEGACY_CLASS_NAMESPACE,
                        f"legacy-class:{mapping.id}:{mapping.name}",
                    )
                )
                if mapping.public_id != expected_legacy_id:
                    unresolved.append(
                        {
                            **base,
                            "reason": "not_legacy_generated",
                        }
                    )
                    continue
                if len(local_by_name[mapping.name]) != 1:
                    unresolved.append(
                        {
                            **base,
                            "reason": "ambiguous_local_name",
                            "local_match_count": len(local_by_name[mapping.name]),
                        }
                    )
                    continue
                targets = live_by_name.get(mapping.name, [])
                if len(targets) != 1:
                    unresolved.append(
                        {
                            **base,
                            "reason": (
                                "live_name_not_found"
                                if not targets
                                else "ambiguous_live_name"
                            ),
                            "live_target_ids": sorted(targets),
                        }
                    )
                    continue
                target_id = targets[0]
                conflict = existing_by_id.get(target_id)
                old_id = mapping.public_id
                if mapping.toilet_id is None:
                    # This row existed only as mirrored catalog residue. Its
                    # operator/open-request references are migrated below, so
                    # no replacement SchoolClass row is needed.
                    session.delete(mapping)
                    existing_by_id.pop(old_id, None)
                    key_remaps[old_id] = target_id
                elif conflict is not None and conflict is not mapping:
                    if (
                        conflict.toilet_id is not None
                        and conflict.toilet_id != mapping.toilet_id
                    ):
                        unresolved.append(
                            {
                                **base,
                                "reason": "target_already_mapped",
                                "target_public_id": target_id,
                                "target_toilet_id": conflict.toilet_id,
                            }
                        )
                        continue
                    # A neutral destination can receive the legacy mapping. A
                    # destination already mapped to the same toilet is also an
                    # unambiguous duplicate and can be collapsed safely.
                    if conflict.toilet_id is None:
                        conflict.toilet_id = mapping.toilet_id
                    session.delete(mapping)
                    existing_by_id.pop(old_id, None)
                    key_remaps[old_id] = target_id
                else:
                    mapping.public_id = target_id
                    existing_by_id.pop(old_id, None)
                    existing_by_id[target_id] = mapping
                    key_remaps[old_id] = target_id
                remapped.append(
                    {
                        **base,
                        "target_public_id": target_id,
                    }
                )

            stale_neutral_rows_deleted: list[str] = []
            for item in all_rows:
                if (
                    item.public_id in seen_ids
                    or item.public_id in referenced_ids
                    or item.toilet_id is not None
                    or item.public_id in key_remaps
                ):
                    continue
                expected_legacy_id = str(
                    uuid.uuid5(
                        LEGACY_CLASS_NAMESPACE,
                        f"legacy-class:{item.id}:{item.name}",
                    )
                )
                if item.public_id != expected_legacy_id:
                    continue
                session.delete(item)
                stale_neutral_rows_deleted.append(item.public_id)

            # Flush key moves/deletions before updating dependent, deliberately
            # non-FK snapshots and scopes.
            session.flush()

            operators_rescoped: list[str] = []
            sessions_revoked = 0
            if key_remaps:
                for account in operator_accounts:
                    old_scope = set(account.class_scope)
                    new_scope = {
                        key_remaps.get(public_id, public_id)
                        for public_id in old_scope
                    }
                    if new_scope == old_scope:
                        continue
                    account.class_scope_json = _canonical_json(sorted(new_scope))
                    account.updated_at = self.clock()
                    operators_rescoped.append(account.username)
                rescoped_subjects = set(operators_rescoped)
                remapped_keys = set(key_remaps)
                for browser_session in (
                    session.query(BrowserSession)
                    .filter(BrowserSession.subject_type == "operator")
                    .all()
                ):
                    if (
                        browser_session.subject in rescoped_subjects
                        or set(browser_session.class_scope) & remapped_keys
                    ):
                        session.delete(browser_session)
                        sessions_revoked += 1

            open_requests: list[Request] = []
            if key_remaps:
                open_requests = (
                    session.query(Request)
                    .join(RequestClassSnapshot)
                    .filter(
                        Request.status.in_(OPEN_STATUSES),
                        RequestClassSnapshot.class_public_id.in_(
                            sorted(key_remaps)
                        ),
                    )
                    .distinct()
                    .order_by(Request.id)
                    .all()
                )

            remapped_request_ids: set[int] = set()
            rebuilt_request_ids: set[int] = set()
            rerouted_toilet_requests: list[Request] = []
            for request in open_requests:
                snapshots_by_id = {
                    item.class_public_id: item for item in request.class_snapshots
                }
                request_changed = False
                for old_id, target_id in sorted(key_remaps.items()):
                    old_snapshot = snapshots_by_id.get(old_id)
                    if old_snapshot is None:
                        continue
                    target_snapshot = snapshots_by_id.get(target_id)
                    if target_snapshot is not None and target_snapshot is not old_snapshot:
                        try:
                            target_sources = json.loads(
                                target_snapshot.source_computers_json
                            )
                        except (TypeError, ValueError):
                            target_sources = []
                        if not isinstance(target_sources, list):
                            target_sources = []
                        try:
                            old_sources = json.loads(old_snapshot.source_computers_json)
                        except (TypeError, ValueError):
                            old_sources = []
                        if not isinstance(old_sources, list):
                            old_sources = []
                        merged_sources = list(
                            dict.fromkeys(
                                str(value)
                                for value in [*target_sources, *old_sources]
                            )
                        )
                        target_snapshot.source_computers_json = _canonical_json(
                            merged_sources
                        )
                        request.class_snapshots.remove(old_snapshot)
                    else:
                        old_snapshot.class_public_id = target_id
                        snapshots_by_id[target_id] = old_snapshot
                    snapshots_by_id.pop(old_id, None)
                    request_changed = True

                if not request_changed:
                    continue

                # Keep the canonical assignment payload consistent with open
                # snapshots. Closed historical requests are never touched.
                snapshot = self._snapshot_data(request)
                snapshot_changed = False
                raw_classes = snapshot.get("classes")
                if isinstance(raw_classes, list):
                    for raw_class in raw_classes:
                        if not isinstance(raw_class, dict):
                            continue
                        raw_id = str(
                            raw_class.get("public_id", raw_class.get("id", ""))
                        )
                        target_id = key_remaps.get(raw_id)
                        if target_id is None:
                            continue
                        if "public_id" in raw_class or "id" not in raw_class:
                            raw_class["public_id"] = target_id
                        if "id" in raw_class:
                            raw_class["id"] = target_id
                        snapshot_changed = True
                    deduplicated_classes: list[Any] = []
                    classes_by_id: dict[str, dict[str, Any]] = {}
                    for raw_class in raw_classes:
                        if not isinstance(raw_class, dict):
                            deduplicated_classes.append(raw_class)
                            continue
                        raw_id = str(
                            raw_class.get("public_id", raw_class.get("id", ""))
                        )
                        existing_class = classes_by_id.get(raw_id)
                        if not raw_id or existing_class is None:
                            deduplicated_classes.append(raw_class)
                            if raw_id:
                                classes_by_id[raw_id] = raw_class
                            continue
                        old_sources = existing_class.get("source_computers")
                        new_sources = raw_class.get("source_computers")
                        if not isinstance(old_sources, list):
                            old_sources = []
                        if not isinstance(new_sources, list):
                            new_sources = []
                        existing_class["source_computers"] = list(
                            dict.fromkeys(
                                str(value)
                                for value in [*old_sources, *new_sources]
                            )
                        )
                        snapshot_changed = True
                    if len(deduplicated_classes) != len(raw_classes):
                        snapshot["classes"] = deduplicated_classes
                raw_computers = snapshot.get("computers")
                if isinstance(raw_computers, list):
                    for raw_computer in raw_computers:
                        if not isinstance(raw_computer, dict):
                            continue
                        raw_class = raw_computer.get("class")
                        if not isinstance(raw_class, dict):
                            continue
                        raw_id = str(
                            raw_class.get("public_id", raw_class.get("id", ""))
                        )
                        target_id = key_remaps.get(raw_id)
                        if target_id is None:
                            continue
                        if "public_id" in raw_class or "id" not in raw_class:
                            raw_class["public_id"] = target_id
                        if "id" in raw_class:
                            raw_class["id"] = target_id
                        snapshot_changed = True
                if snapshot_changed:
                    request.identity_snapshot_json = _canonical_json(snapshot)
                remapped_request_ids.add(request.id)
                if request.status == "pending" and request.type == "toilet":
                    rebuilt_request_ids.add(request.id)
                    rerouted_toilet_requests.append(request)

            session.flush()
            for request in rerouted_toilet_requests:
                self._rebuild_route(
                    session,
                    request,
                    actor=audit_actor,
                    correlation_id=correlation,
                )
            activated = (
                self._schedule(
                    session,
                    actor=audit_actor,
                    correlation_id=correlation,
                )
                if rerouted_toilet_requests
                else set()
            )
            changed_request_ids = remapped_request_ids | activated
            changed_user_ids = self._user_ids_for_requests(
                session, changed_request_ids
            )

            audit_details = {
                "remapped": remapped,
                "unresolved": unresolved,
                "stale_neutral_rows_deleted": sorted(stale_neutral_rows_deleted),
                "operators_rescoped": operators_rescoped,
                "sessions_revoked": sessions_revoked,
                "open_requests_remapped": sorted(remapped_request_ids),
                "pending_requests_rebuilt": sorted(rebuilt_request_ids),
            }
            latest_audit = (
                session.query(AuditEvent)
                .filter(AuditEvent.action == "class.mapping_keys_reconciled")
                .order_by(AuditEvent.id.desc())
                .first()
            )
            previous_unresolved_json: str | None = None
            if latest_audit is not None:
                try:
                    previous_details = json.loads(latest_audit.details_json)
                except (TypeError, ValueError):
                    previous_details = {}
                if isinstance(previous_details, Mapping) and isinstance(
                    previous_details.get("unresolved"), list
                ):
                    previous_unresolved_json = _canonical_json(
                        previous_details["unresolved"]
                    )
            unresolved_json = _canonical_json(unresolved)
            material_change = bool(
                remapped
                or stale_neutral_rows_deleted
                or operators_rescoped
                or remapped_request_ids
            )
            unresolved_transition = (
                unresolved_json != previous_unresolved_json
                and (
                    bool(unresolved)
                    or previous_unresolved_json not in (None, _canonical_json([]))
                )
            )
            if material_change or unresolved_transition:
                self._audit(
                    session,
                    actor=audit_actor,
                    action="class.mapping_keys_reconciled",
                    target_type="class_mappings",
                    target_identifier="olimp-control",
                    correlation_id=correlation,
                    details=audit_details,
                )
            return MutationResult(
                audit_details,
                frozenset(changed_user_ids),
                staff_changed=material_change,
                request_ids=frozenset(changed_request_ids),
            )

        return self._run(operation)

    def sync_class_catalog(
        self,
        classes: Sequence[Mapping[str, Any]],
        *,
        actor: Actor,
        revision: int | None = None,
        correlation_id: str | None = None,
    ) -> MutationResult:
        """Legacy/testing helper from the catalog-mirroring implementation.

        Production request handlers no longer call this method. Olimp-control
        class metadata and layouts are fetched live; only explicit
        class-to-toilet mappings are persisted through
        :meth:`assign_class_toilet`.
        """
        self._require_admin(actor)
        if revision is not None:
            try:
                revision = int(revision)
            except (TypeError, ValueError) as exc:
                raise ValidationError("invalid class catalog revision") from exc
        normalized: dict[str, dict[str, Any]] = {}
        for item in classes:
            if not isinstance(item, Mapping):
                raise ValidationError("malformed class catalog")
            public_id = _canonical_uuid(
                item.get("public_id", item.get("id")), field_name="class public_id"
            )
            if public_id in normalized:
                raise ValidationError("duplicate class public_id")
            normalized[public_id] = {
                "public_id": public_id,
                "name": self._clean_name(str(item.get("name") or "")),
                "sequence_num": int(item.get("sequence_num", 0)),
            }
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            previous_revision = self._legacy_catalog_revision
            if (
                revision is not None
                and previous_revision is not None
                and revision < previous_revision
            ):
                raise ConflictError("stale class catalog revision")
            existing = {item.public_id: item for item in session.query(SchoolClass).all()}
            created: list[str] = []
            renamed: list[str] = []
            reordered: list[str] = []
            for public_id, item in normalized.items():
                school_class = existing.get(public_id)
                if school_class is None:
                    school_class = SchoolClass(**item)
                    session.add(school_class)
                    created.append(public_id)
                else:
                    if school_class.name != item["name"]:
                        renamed.append(public_id)
                    if school_class.sequence_num != item["sequence_num"]:
                        reordered.append(public_id)
                    school_class.name = item["name"]
                    school_class.sequence_num = item["sequence_num"]

            omitted = sorted(set(existing) - set(normalized))
            affected: dict[int, Request] = {}
            if omitted:
                pending = (
                    session.query(Request)
                    .join(RequestClassSnapshot)
                    .filter(
                        Request.type == "toilet",
                        Request.status == "pending",
                        RequestClassSnapshot.class_public_id.in_(omitted),
                    )
                    .distinct()
                    .all()
                )
                affected = {request.id: request for request in pending}
                for public_id in omitted:
                    session.delete(existing[public_id])
                session.flush()
                for request in affected.values():
                    self._rebuild_route(
                        session, request, actor=actor, correlation_id=correlation
                    )

            catalog_changed = bool(created or renamed or reordered or omitted)
            revision_changed = revision is not None and revision != previous_revision
            audit_changed = revision_changed or catalog_changed
            if audit_changed:
                self._audit(
                    session,
                    actor=actor,
                    action="class.catalog_synced",
                    target_type="class_catalog",
                    target_identifier=revision if revision is not None else "unversioned",
                    correlation_id=correlation,
                    details={
                        "created": created,
                        "renamed": renamed,
                        "reordered": reordered,
                        "deleted": omitted,
                        "revision_changed": revision_changed,
                    },
                )
            activated = (
                self._schedule(session, actor=actor, correlation_id=correlation)
                if affected
                else set()
            )
            request_ids = set(affected) | activated
            user_ids = {
                row[0]
                for row in session.query(Request.user_id)
                .filter(Request.id.in_(request_ids or [-1]))
                .all()
            }
            return MutationResult(
                {
                    "classes": [normalized[key] for key in sorted(normalized)],
                    "created": created,
                    "renamed": renamed,
                    "reordered": reordered,
                    "deleted": omitted,
                    "revision": revision,
                },
                frozenset(user_ids),
                catalog_changed,
                frozenset(request_ids),
            )

        result = self._run(operation)
        if revision is not None:
            self._legacy_catalog_revision = revision
        return result

    def delete_toilet(
        self,
        *,
        toilet_id: int,
        actor: Actor,
        correlation_id: str | None = None,
    ) -> MutationResult:
        self._require_admin(actor)
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            toilet = session.get(Toilet, toilet_id)
            if toilet is None:
                raise NotFoundError("toilet not found")
            deleted_name = toilet.name
            affected_requests = (
                session.query(Request)
                .join(RequestToiletLock)
                .filter(
                    Request.type == "toilet",
                    Request.status.in_(OPEN_STATUSES),
                    RequestToiletLock.toilet_id == toilet_id,
                )
                .distinct()
                .all()
            )
            affected_classes = session.query(SchoolClass).filter_by(toilet_id=toilet_id).all()
            affected_class_ids = [item.public_id for item in affected_classes]
            for school_class in affected_classes:
                session.delete(school_class)
            now = self.clock()
            for request in affected_requests:
                if request.status == "active":
                    request.status = "pending"
                    request.activated_at = None
                    self._ensure_alert(
                        session,
                        request,
                        code="active_toilet_deleted",
                        details={"toilet_id": toilet_id, "toilet_name": deleted_name},
                        actor=actor,
                        correlation_id=correlation,
                        severity="urgent",
                        global_scope=True,
                    )
                    self._audit(
                        session,
                        actor=actor,
                        action="request.demoted_for_toilet_delete",
                        target_type="request",
                        target_identifier=request.id,
                        correlation_id=correlation,
                        details={"deleted_toilet_id": toilet_id},
                        now=now,
                    )
                request.toilet_locks.clear()
            session.flush()
            session.delete(toilet)
            session.flush()
            for request in affected_requests:
                self._rebuild_route(
                    session, request, actor=actor, correlation_id=correlation
                )
            self._audit(
                session,
                actor=actor,
                action="toilet.deleted",
                target_type="toilet",
                target_identifier=toilet_id,
                correlation_id=correlation,
                details={
                    "name": deleted_name,
                    "affected_request_ids": [item.id for item in affected_requests],
                    "cleared_class_public_ids": affected_class_ids,
                },
                now=now,
            )
            activated = self._schedule(session, actor=actor, correlation_id=correlation)
            request_ids = {item.id for item in affected_requests} | activated
            user_ids = {item.user_id for item in affected_requests}
            user_ids.update(
                row[0]
                for row in session.query(Request.user_id)
                .filter(Request.id.in_(activated or [-1]))
                .all()
            )
            return MutationResult(
                {"id": toilet_id, "deleted": True},
                frozenset(user_ids),
                True,
                frozenset(request_ids),
            )

        return self._run(operation)

    # -- alert operations and read DTOs ------------------------------------

    def resolve_alert(
        self,
        *,
        alert_id: int,
        actor: Actor,
        correlation_id: str | None = None,
    ) -> MutationResult:
        self._require_staff(actor)
        correlation = self._correlation(correlation_id)

        def operation(session: Session) -> MutationResult:
            alert = session.get(OperationalAlert, alert_id)
            if alert is None:
                raise NotFoundError("alert not found")
            class_ids = {item.class_public_id for item in alert.request.class_snapshots}
            self._require_proctor_scope(
                actor,
                class_ids,
                global_scope=alert.global_scope,
            )
            if alert.resolved_at is None:
                alert.resolved_at = self.clock()
                alert.resolved_by = actor.identifier
                self._audit(
                    session,
                    actor=actor,
                    action="alert.resolved",
                    target_type="alert",
                    target_identifier=alert.id,
                    correlation_id=correlation,
                    details={"code": alert.code, "request_id": alert.request_id},
                )
            return MutationResult(
                {"id": alert.id, "resolved": True},
                request_ids=frozenset({alert.request_id}),
            )

        return self._run(operation)

    @staticmethod
    def _request_dict(request: Request) -> dict[str, Any]:
        return {
            "id": request.id,
            "user_id": request.user_id,
            "type": request.type,
            "status": request.status,
            "created_at": request.created_at,
            "activated_at": request.activated_at,
            "completed_at": request.completed_at,
            "routing_mode": request.routing_mode,
            "blocked_reason": request.blocked_reason,
            "classes": [
                {"public_id": item.class_public_id, "name": item.class_name}
                for item in sorted(request.class_snapshots, key=lambda item: item.class_public_id)
            ],
            "toilets": [
                {"id": lock.toilet.id, "name": lock.toilet.name}
                for lock in sorted(request.toilet_locks, key=lambda item: item.toilet_id)
                if lock.toilet is not None
            ],
            "alerts": [
                {"id": alert.id, "code": alert.code, "severity": alert.severity}
                for alert in request.alerts
                if alert.resolved_at is None
            ],
        }

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        session = self.database.SessionLocal()
        try:
            request = session.get(Request, request_id)
            return self._request_dict(request) if request is not None else None
        finally:
            session.close()

    def list_open_alerts(self, actor: Actor) -> list[dict[str, Any]]:
        self._require_staff(actor)
        session = self.database.SessionLocal()
        try:
            alerts = (
                session.query(OperationalAlert)
                .filter(OperationalAlert.resolved_at.is_(None))
                .order_by(OperationalAlert.created_at, OperationalAlert.id)
                .all()
            )
            visible: list[dict[str, Any]] = []
            for alert in alerts:
                class_ids = {
                    item.class_public_id for item in alert.request.class_snapshots
                }
                try:
                    self._require_proctor_scope(
                        actor,
                        class_ids,
                        global_scope=alert.global_scope,
                    )
                except ForbiddenError:
                    continue
                visible.append(
                    {
                        "id": alert.id,
                        "request_id": alert.request_id,
                        "code": alert.code,
                        "severity": alert.severity,
                        "global_scope": alert.global_scope,
                        "details": json.loads(alert.details_json),
                        "username": alert.request.user.username,
                        "classes": [
                            {
                                "public_id": item.class_public_id,
                                "name": item.class_name,
                            }
                            for item in alert.request.class_snapshots
                        ],
                        "toilets": [
                            {"id": lock.toilet.id, "name": lock.toilet.name}
                            for lock in alert.request.toilet_locks
                            if lock.toilet is not None
                        ],
                        "created_at": alert.created_at.isoformat(),
                    }
                )
            return visible
        finally:
            session.close()
