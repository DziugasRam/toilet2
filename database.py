"""Configurable SQLite database setup.

SQLite is deliberately used with one serialized writer.  Foreign keys, WAL
and a busy timeout are installed on every connection so tests and production
cannot accidentally exercise different integrity rules.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, declarative_base, sessionmaker


Base = declarative_base()
GLOBAL_WRITE_LOCK = threading.RLock()


class Database:
    """Own an engine and session factory for one database."""

    def __init__(
        self,
        url: str,
        *,
        busy_timeout_ms: int = 5_000,
        wal: bool = True,
        echo: bool = False,
    ) -> None:
        if not url:
            raise ValueError("database URL is required")
        self.url = url
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.wal = bool(wal)
        connect_args = {}
        if self.url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            connect_args["timeout"] = max(self.busy_timeout_ms / 1000, 0.001)
        self.engine = create_engine(
            self.url,
            connect_args=connect_args,
            echo=echo,
            future=True,
        )
        if self.url.startswith("sqlite"):
            event.listen(self.engine, "connect", self._configure_sqlite)
        self.SessionLocal = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )

    def _configure_sqlite(self, dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            if self.wal:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    def initialize(self, *, upgrade: bool = True) -> None:
        """Create a fresh schema or safely upgrade the original prototype schema."""

        if upgrade:
            try:
                from .migrations import upgrade_schema
            except ImportError:  # pragma: no cover - legacy launch style
                from migrations import upgrade_schema

            upgrade_schema(self.engine)
        else:
            # Import registers every mapped table on Base.metadata.
            try:
                from . import models as _models  # noqa: F401
            except ImportError:  # pragma: no cover
                import models as _models  # noqa: F401

            Base.metadata.create_all(self.engine)
            try:
                from .migrations import install_audit_triggers
            except ImportError:  # pragma: no cover
                from migrations import install_audit_triggers
            install_audit_triggers(self.engine)

    @contextmanager
    def immediate_session(self) -> Iterator[Session]:
        """Yield a session whose first statement obtains SQLite's writer lock.

        The process lock is intentionally outside this helper; MutationService
        owns it so a complete domain operation, not an individual SQL statement,
        is the serialization boundary.
        """

        session = self.SessionLocal()
        try:
            if self.url.startswith("sqlite"):
                session.execute(text("BEGIN IMMEDIATE"))
            else:  # Useful for future database backends and unit tests.
                session.begin()
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
