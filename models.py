"""Persistence model for the toilet queue.

Names are display data.  Routing always uses immutable primary/public ids and
the class/computer snapshot captured on a request.  ``RequestToiletLock`` rows
are requirements while a request is pending and capacity locks while it is
active; this lets the scheduler acquire several toilets atomically.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
)
from sqlalchemy.orm import relationship

try:  # Package imports (tests/production) and legacy ``uvicorn main:app``.
    from .database import Base
except ImportError:  # pragma: no cover - exercised by legacy launch style
    from database import Base


def utc_now() -> datetime:
    """Return naive UTC for portable SQLite comparison/storage."""

    return datetime.now(UTC).replace(tzinfo=None)


REQUEST_TYPES = {
    "toilet": "Toilet",
    "paper": "Additional paper",
    # Retained for rendering legacy history only. New support requests are
    # limited by Settings.general_request_types.
    "water": "Water",
    "snack": "Snacks",
    "tech": "Technical assistance",
}
GENERAL_REQUEST_TYPES = {
    "paper": "Additional paper",
}
REQUEST_STATUSES = ("pending", "active", "done", "cancelled")
OPEN_STATUSES = ("pending", "active")

ROUTING_NORMAL = "normal"
ROUTING_MULTIPLE_CLASSES = "multiple_classes"
ROUTING_FALLBACK_ALL = "fallback_all"
ROUTING_SUPPORT = "support"
ROUTING_MODES = (
    ROUTING_NORMAL,
    ROUTING_MULTIPLE_CLASSES,
    ROUTING_FALLBACK_ALL,
    ROUTING_SUPPORT,
)

ROLE_ADMIN = "admin"
ROLE_PROCTOR = "proctor"


class Toilet(Base):
    __tablename__ = "toilets"
    __table_args__ = (
        CheckConstraint("capacity >= 1", name="ck_toilets_capacity_positive"),
        {"sqlite_autoincrement": True},
    )

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    capacity = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=utc_now)
    updated_at = Column(DateTime, nullable=False, default=utc_now, onupdate=utc_now)

    classes = relationship("SchoolClass", back_populates="toilet")
    request_locks = relationship(
        "RequestToiletLock", back_populates="toilet", passive_deletes=True
    )


class SchoolClass(Base):
    """A local class-to-toilet mapping keyed by an Olimp-control UUID.

    ``name`` and ``sequence_num`` remain for compatibility with databases from
    the earlier catalog-mirroring design. New runtime code does not refresh or
    rely on them; display metadata and physical layouts are read live from
    Olimp-control.
    """

    __tablename__ = "classes"
    __table_args__ = {"sqlite_autoincrement": True}

    id = Column(Integer, primary_key=True)
    public_id = Column(String(36), unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    sequence_num = Column(Integer, nullable=False, default=0)
    toilet_id = Column(
        Integer, ForeignKey("toilets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at = Column(DateTime, nullable=False, default=utc_now)
    updated_at = Column(DateTime, nullable=False, default=utc_now, onupdate=utc_now)

    toilet = relationship("Toilet", back_populates="classes")
class User(Base):
    """A locally synchronized Olimp-control contestant.

    The durable ``username`` value is Olimp-control's ``userid`` and the CMS
    login identifier. Computer and current-class assignments deliberately do
    not have relationships here; they are fetched live when needed.
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False, index=True)
    control_id = Column(Integer, unique=True, nullable=True, index=True)
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, nullable=False, default=utc_now)
    updated_at = Column(DateTime, nullable=False, default=utc_now, onupdate=utc_now)

    requests = relationship("Request", back_populates="user")
    sessions = relationship("BrowserSession", back_populates="user")


class OperatorAccount(Base):
    """Toilet-local staff credentials and authorization.

    These accounts intentionally have no relationship with Olimp-control users.
    """

    __tablename__ = "operator_accounts"

    username = Column(String, primary_key=True)
    display_name = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    roles_json = Column(Text, nullable=False, default="[]")
    class_scope_json = Column(Text, nullable=False, default="[]")
    all_classes = Column(Boolean, nullable=False, default=False)
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, nullable=False, default=utc_now)
    updated_at = Column(DateTime, nullable=False, default=utc_now, onupdate=utc_now)

    @property
    def roles(self) -> frozenset[str]:
        try:
            value = json.loads(self.roles_json)
        except (TypeError, ValueError):
            value = []
        return frozenset(str(role) for role in value)

    @property
    def class_scope(self) -> frozenset[str]:
        try:
            value = json.loads(self.class_scope_json)
        except (TypeError, ValueError):
            value = []
        return frozenset(str(public_id) for public_id in value)


class BrowserSession(Base):
    """Expiring local session containing a role/scope snapshot.

    Operator passwords remain bcrypt-hashed in the separate local operator
    account table. Only the successful identity, roles, and class UUID scopes
    are copied into a browser session.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('cms','operator')",
            name="ck_sessions_subject_type",
        ),
    )

    token = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    subject_type = Column(String, nullable=False)
    subject = Column(String, nullable=False, default="")
    display_name = Column(String, nullable=False, default="")
    csrf_token = Column(String, nullable=False, default=lambda: secrets.token_urlsafe(32))
    roles_json = Column(Text, nullable=False, default="[]")
    class_scope_json = Column(Text, nullable=False, default="[]")
    all_classes = Column(Boolean, nullable=False, default=False)
    contest = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utc_now)
    expires_at = Column(
        DateTime, nullable=False, default=lambda: utc_now() + timedelta(hours=8), index=True
    )

    user = relationship("User", back_populates="sessions")

    @property
    def roles(self) -> frozenset[str]:
        try:
            value = json.loads(self.roles_json)
        except (TypeError, ValueError):
            value = []
        return frozenset(str(role) for role in value)

    @property
    def class_scope(self) -> frozenset[str]:
        try:
            value = json.loads(self.class_scope_json)
        except (TypeError, ValueError):
            value = []
        return frozenset(str(public_id) for public_id in value)

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or utc_now())


class Request(Base):
    __tablename__ = "requests"
    __table_args__ = (
        CheckConstraint(
            "type IN ('toilet','paper','water','snack','tech')",
            name="ck_requests_type",
        ),
        CheckConstraint(
            "status IN ('pending','active','done','cancelled')",
            name="ck_requests_status",
        ),
        CheckConstraint(
            "routing_mode IS NULL OR routing_mode IN "
            "('normal','multiple_classes','fallback_all','support')",
            name="ck_requests_routing_mode",
        ),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    type = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending", index=True)
    created_at = Column(DateTime, nullable=False, default=utc_now)
    activated_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    manual_completion = Column(Boolean, nullable=False, default=False)
    routing_mode = Column(String, nullable=True)
    blocked_reason = Column(String, nullable=True)
    identity_snapshot_json = Column(Text, nullable=False, default="{}")
    completed_by = Column(String, nullable=True)

    user = relationship("User", back_populates="requests")
    class_snapshots = relationship(
        "RequestClassSnapshot", back_populates="request", cascade="all, delete-orphan"
    )
    toilet_locks = relationship(
        "RequestToiletLock", back_populates="request", cascade="all, delete-orphan"
    )
    alerts = relationship(
        "OperationalAlert", back_populates="request", cascade="all, delete-orphan"
    )


Index(
    "uq_requests_open_user_type",
    Request.user_id,
    Request.type,
    unique=True,
    sqlite_where=and_(Request.status.in_(OPEN_STATUSES)),
)
Index("ix_requests_fifo", Request.type, Request.status, Request.created_at, Request.id)


class RequestClassSnapshot(Base):
    __tablename__ = "request_class_snapshots"
    __table_args__ = (
        UniqueConstraint("request_id", "class_public_id", name="uq_request_class_snapshot"),
    )

    id = Column(Integer, primary_key=True)
    request_id = Column(
        Integer, ForeignKey("requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    class_public_id = Column(String(36), nullable=False, index=True)
    class_name = Column(String, nullable=False)
    source_computers_json = Column(Text, nullable=False, default="[]")

    request = relationship("Request", back_populates="class_snapshots")


class RequestToiletLock(Base):
    __tablename__ = "request_toilet_locks"

    request_id = Column(
        Integer, ForeignKey("requests.id", ondelete="CASCADE"), primary_key=True
    )
    toilet_id = Column(
        Integer, ForeignKey("toilets.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    reason_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=utc_now)

    request = relationship("Request", back_populates="toilet_locks")
    toilet = relationship("Toilet", back_populates="request_locks")


class OperationalAlert(Base):
    __tablename__ = "operational_alerts"
    __table_args__ = (
        UniqueConstraint("request_id", "code", name="uq_request_alert_code"),
    )

    id = Column(Integer, primary_key=True)
    request_id = Column(
        Integer, ForeignKey("requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code = Column(String, nullable=False, index=True)
    severity = Column(String, nullable=False, default="warning")
    global_scope = Column(Boolean, nullable=False, default=False)
    details_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, nullable=False, default=utc_now)
    resolved_at = Column(DateTime, nullable=True, index=True)
    resolved_by = Column(String, nullable=True)

    request = relationship("Request", back_populates="alerts")


class AuditEvent(Base):
    """Append-only event.  It deliberately has no cascading foreign keys."""

    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True)
    occurred_at = Column(DateTime, nullable=False, default=utc_now, index=True)
    actor_kind = Column(String, nullable=False)
    actor_identifier = Column(String, nullable=False)
    action = Column(String, nullable=False, index=True)
    target_type = Column(String, nullable=False)
    target_identifier = Column(String, nullable=False)
    correlation_id = Column(String(36), nullable=False, index=True)
    details_json = Column(Text, nullable=False, default="{}")


class RateLimitBucket(Base):
    __tablename__ = "rate_limit_buckets"

    actor_key = Column(String, primary_key=True)
    action_group = Column(String, primary_key=True)
    window_started_at = Column(DateTime, primary_key=True)
    count = Column(Integer, nullable=False, default=0)
