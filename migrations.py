"""Small dependency-free schema upgrader for the pre-migration prototype.

This project started with ``metadata.create_all`` and an already useful local
development database.  The functions here keep that database usable while all
fresh/test databases receive the exact production schema.  Future changes can
replace this module with Alembic without changing ``Database.initialize``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

try:
    from .database import Base
    from . import models as _models  # noqa: F401 - register metadata
except ImportError:  # pragma: no cover - legacy launch style
    from database import Base
    import models as _models  # noqa: F401


SCHEMA_VERSION = 5
LEGACY_CLASS_NAMESPACE = uuid.UUID("9d2730ae-a594-47ce-a975-2737b36de071")


def _table_names(connection) -> set[str]:
    return set(inspect(connection).get_table_names())


def _columns(connection, table: str) -> dict[str, dict]:
    if table not in _table_names(connection):
        return {}
    return {column["name"]: column for column in inspect(connection).get_columns(table)}


def _add_column(connection, table: str, name: str, definition: str) -> None:
    if name not in _columns(connection, table):
        connection.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')


def _upgrade_legacy_tables(engine: Engine) -> None:
    """Add/rebuild columns that ``create_all`` cannot add to existing tables."""

    with engine.begin() as connection:
        tables = _table_names(connection)
        if "classes" not in tables:
            return

        _add_column(connection, "classes", "public_id", "VARCHAR(36)")
        _add_column(connection, "classes", "sequence_num", "INTEGER NOT NULL DEFAULT 0")
        _add_column(connection, "classes", "toilet_id", "INTEGER REFERENCES toilets(id) ON DELETE SET NULL")
        _add_column(connection, "classes", "created_at", "DATETIME")
        _add_column(connection, "classes", "updated_at", "DATETIME")
        _add_column(connection, "toilets", "created_at", "DATETIME")
        _add_column(connection, "toilets", "updated_at", "DATETIME")
        _add_column(connection, "users", "created_at", "DATETIME")
        _add_column(connection, "users", "updated_at", "DATETIME")

        now = datetime.now(UTC).replace(tzinfo=None)
        now_sql = now.isoformat(" ")
        rows = connection.exec_driver_sql("SELECT id, name, public_id FROM classes").mappings()
        for row in rows:
            if not row["public_id"]:
                public_id = str(
                    uuid.uuid5(
                        LEGACY_CLASS_NAMESPACE,
                        f"legacy-class:{row['id']}:{row['name']}",
                    )
                )
                connection.exec_driver_sql(
                    "UPDATE classes SET public_id = ? WHERE id = ?",
                    (public_id, row["id"]),
                )
        connection.exec_driver_sql(
            "UPDATE classes SET created_at = COALESCE(created_at, ?), "
            "updated_at = COALESCE(updated_at, ?)",
            (now_sql, now_sql),
        )
        connection.exec_driver_sql(
            "UPDATE toilets SET created_at = COALESCE(created_at, ?), "
            "updated_at = COALESCE(updated_at, ?)",
            (now_sql, now_sql),
        )
        connection.exec_driver_sql(
            "UPDATE users SET created_at = COALESCE(created_at, ?), "
            "updated_at = COALESCE(updated_at, ?)",
            (now_sql, now_sql),
        )

        if "class_toilets" in tables:
            # The old UI allowed several choices.  Pick the stable lowest PK;
            # the upgrade audit event makes this visible before real catalog sync.
            connection.exec_driver_sql(
                "UPDATE classes SET toilet_id = ("
                "SELECT MIN(ct.toilet_id) FROM class_toilets ct "
                "WHERE ct.class_id = classes.id"
                ") WHERE toilet_id IS NULL"
            )

        if "requests" in tables:
            _add_column(connection, "requests", "routing_mode", "VARCHAR")
            _add_column(connection, "requests", "blocked_reason", "VARCHAR")
            _add_column(connection, "requests", "identity_snapshot_json", "TEXT NOT NULL DEFAULT '{}'")
            _add_column(connection, "requests", "completed_by", "VARCHAR")
            connection.exec_driver_sql(
                "UPDATE requests SET routing_mode = CASE "
                "WHEN type = 'toilet' THEN COALESCE(routing_mode, 'normal') "
                "ELSE COALESCE(routing_mode, 'support') END"
            )

            # Make the later partial unique index installable without deleting
            # history: keep the oldest open request and cancel later duplicates.
            duplicate_groups = connection.exec_driver_sql(
                "SELECT user_id, type FROM requests "
                "WHERE status IN ('pending','active') "
                "GROUP BY user_id, type HAVING COUNT(*) > 1"
            ).all()
            for user_id, request_type in duplicate_groups:
                duplicate_ids = [
                    row[0]
                    for row in connection.exec_driver_sql(
                        "SELECT id FROM requests WHERE user_id=? AND type=? "
                        "AND status IN ('pending','active') ORDER BY created_at, id",
                        (user_id, request_type),
                    ).all()
                ]
                for duplicate_id in duplicate_ids[1:]:
                    connection.exec_driver_sql(
                        "UPDATE requests SET status='cancelled', completed_at=?, "
                        "completed_by='schema-upgrade:duplicate-open' WHERE id=?",
                        (now_sql, duplicate_id),
                    )

    _rebuild_legacy_classes(engine)
    _rebuild_legacy_sessions(engine)


def _rebuild_legacy_classes(engine: Engine) -> None:
    """Remove UNIQUE(name) and make local row identifiers non-reusable."""

    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        table_row = cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='classes'"
        ).fetchone()
        has_autoincrement = bool(
            table_row and "AUTOINCREMENT" in (table_row[0] or "").upper()
        )
        unique_name = False
        for row in cursor.execute("PRAGMA index_list('classes')").fetchall():
            # seq, name, unique, origin, partial
            if not row[2]:
                continue
            columns = [
                item[2]
                for item in cursor.execute(
                    f"PRAGMA index_info('{row[1]}')"
                ).fetchall()
            ]
            if columns == ["name"]:
                unique_name = True
                break
        if not unique_name and has_autoincrement:
            cursor.close()
            return
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            """
            CREATE TABLE classes_new (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                public_id VARCHAR(36) NOT NULL UNIQUE,
                name VARCHAR NOT NULL,
                sequence_num INTEGER NOT NULL DEFAULT 0,
                toilet_id INTEGER REFERENCES toilets(id) ON DELETE SET NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
        cursor.execute(
            "INSERT INTO classes_new(id,public_id,name,sequence_num,toilet_id,created_at,updated_at) "
            "SELECT id,public_id,name,sequence_num,toilet_id,created_at,updated_at FROM classes"
        )
        cursor.execute("DROP TABLE classes")
        cursor.execute("ALTER TABLE classes_new RENAME TO classes")
        raw.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    except Exception:
        raw.rollback()
        try:
            raw.cursor().execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        raise
    finally:
        raw.close()


def _ensure_toilet_autoincrement(engine: Engine) -> None:
    """Make deleted toilet identifiers permanently non-reusable."""

    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        row = cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='toilets'"
        ).fetchone()
        if row is None or "AUTOINCREMENT" in (row[0] or "").upper():
            cursor.close()
            return
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            """
            CREATE TABLE toilets_new (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                name VARCHAR NOT NULL UNIQUE,
                capacity INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT ck_toilets_capacity_positive CHECK (capacity >= 1)
            )
            """
        )
        cursor.execute(
            "INSERT INTO toilets_new(id,name,capacity,created_at,updated_at) "
            "SELECT id,name,capacity,created_at,updated_at FROM toilets"
        )
        cursor.execute("DROP TABLE toilets")
        cursor.execute("ALTER TABLE toilets_new RENAME TO toilets")
        raw.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    except Exception:
        raw.rollback()
        try:
            raw.cursor().execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        raise
    finally:
        raw.close()


def _repair_v1_legacy_locks(engine: Engine) -> int:
    """Remove the v1 upgrader's identifiable fabricated second lock."""

    with engine.begin() as connection:
        if "request_toilet_locks" not in _table_names(connection):
            return 0
        rows = connection.exec_driver_sql(
            "SELECT request_id,toilet_id,reason_json FROM request_toilet_locks "
            "ORDER BY request_id,toilet_id"
        ).all()
        sources: dict[int, list[tuple[int, str | None]]] = {}
        for request_id, toilet_id, reason_json in rows:
            try:
                value = json.loads(reason_json)
                source = value.get("source") if isinstance(value, dict) else None
            except (TypeError, ValueError):
                source = None
            sources.setdefault(request_id, []).append((toilet_id, source))
        removed = 0
        for request_id, locks in sources.items():
            if not any(source == "legacy_name" for _toilet_id, source in locks):
                continue
            for toilet_id, source in locks:
                if source != "legacy_class_mapping":
                    continue
                connection.exec_driver_sql(
                    "DELETE FROM request_toilet_locks WHERE request_id=? AND toilet_id=?",
                    (request_id, toilet_id),
                )
                removed += 1
        return removed


def _rebuild_legacy_sessions(engine: Engine) -> None:
    """Make the legacy table structurally compatible before credentials are purged."""

    with engine.connect() as connection:
        columns = _columns(connection, "sessions")
        if not columns or "csrf_token" in columns:
            return

    # SQLite cannot relax NOT NULL with ALTER TABLE.  Disable FK checks outside
    # a transaction, copy, then atomically swap the table.
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            """
            CREATE TABLE sessions_new (
                token VARCHAR PRIMARY KEY,
                user_id INTEGER NULL REFERENCES users(id) ON DELETE CASCADE,
                subject_type VARCHAR NOT NULL,
                subject VARCHAR NOT NULL DEFAULT '',
                display_name VARCHAR NOT NULL DEFAULT '',
                csrf_token VARCHAR NOT NULL,
                roles_json TEXT NOT NULL DEFAULT '[]',
                class_scope_json TEXT NOT NULL DEFAULT '[]',
                all_classes BOOLEAN NOT NULL DEFAULT 0,
                contest VARCHAR NULL,
                created_at DATETIME NOT NULL,
                expires_at DATETIME NOT NULL
            )
            """
        )
        cursor.execute(
            """
            INSERT INTO sessions_new (
                token, user_id, subject_type, subject, display_name, csrf_token,
                roles_json, class_scope_json, all_classes, created_at, expires_at
            )
            SELECT s.token, s.user_id,
                   CASE WHEN COALESCE(u.is_admin,0) THEN 'dev_operator' ELSE 'dev_student' END,
                   COALESCE(u.username,''),
                   COALESCE(u.username,''), lower(hex(randomblob(32))),
                   CASE WHEN COALESCE(u.is_admin,0) THEN '["admin"]' ELSE '[]' END,
                   '[]', CASE WHEN COALESCE(u.is_admin,0) THEN 1 ELSE 0 END,
                   s.created_at, datetime(s.created_at, '+8 hours')
            FROM sessions s LEFT JOIN users u ON u.id = s.user_id
            """
        )
        cursor.execute("DROP TABLE sessions")
        cursor.execute("ALTER TABLE sessions_new RENAME TO sessions")
        cursor.execute("CREATE INDEX ix_sessions_expires_at ON sessions(expires_at)")
        raw.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def _invalidate_nonproduction_sessions(engine: Engine) -> int:
    """Delete credentials minted by the retired in-process development auth."""

    with engine.begin() as connection:
        if "sessions" not in _table_names(connection):
            return 0
        result = connection.exec_driver_sql(
            "DELETE FROM sessions WHERE subject_type IS NULL "
            "OR subject_type NOT IN ('cms','operator')"
        )
        return max(result.rowcount or 0, 0)


def _upgrade_local_identity_tables(engine: Engine) -> int:
    """Add synchronized-student fields and preserve legacy local staff logins."""

    with engine.begin() as connection:
        if "users" not in _table_names(connection):
            return 0
        _add_column(connection, "users", "control_id", "INTEGER")
        _add_column(
            connection,
            "users",
            "enabled",
            "BOOLEAN NOT NULL DEFAULT 1",
        )
        _add_column(connection, "users", "created_at", "DATETIME")
        _add_column(connection, "users", "updated_at", "DATETIME")
        now = datetime.now(UTC).replace(tzinfo=None).isoformat(" ")
        connection.exec_driver_sql(
            "UPDATE users SET created_at=COALESCE(created_at,?), "
            "updated_at=COALESCE(updated_at,?)",
            (now, now),
        )
        migrated = 0
        if (
            "operator_accounts" in _table_names(connection)
            and "password_hash" in _columns(connection, "users")
        ):
            for username, password_hash, is_admin in connection.exec_driver_sql(
                "SELECT username,password_hash,is_admin FROM users "
                "WHERE password_hash IS NOT NULL AND password_hash<>''"
            ).all():
                roles = ["admin", "proctor"] if is_admin else ["proctor"]
                inserted = connection.exec_driver_sql(
                    "INSERT OR IGNORE INTO operator_accounts("
                    "username,display_name,password_hash,roles_json,class_scope_json,"
                    "all_classes,enabled,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        username,
                        username,
                        password_hash,
                        json.dumps(roles, separators=(",", ":")),
                        "[]",
                        1,
                        1,
                        now,
                        now,
                    ),
                )
                migrated += max(inserted.rowcount or 0, 0)
        return migrated


def _drop_current_class_mirror(engine: Engine) -> None:
    """Remove the retired current-location mirror after legacy conversion."""

    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS user_current_classes")
        connection.exec_driver_sql("DROP TABLE IF EXISTS catalog_sync_state")


def _finalize_schema_upgrade(
    engine: Engine,
    *,
    details: dict,
    record_audit: bool,
) -> None:
    """Atomically remove retired schema and publish the new schema version.

    Legacy class/credential columns are consumed earlier in the upgrade.  Keep
    their physical removal in the same transaction as the audit/version write
    so an interrupted upgrade remains fully retryable.
    """

    with engine.connect() as connection:
        tables = _table_names(connection)
        user_columns = set(_columns(connection, "users"))
    retired_user_columns = sorted(
        user_columns
        & {"school_class_id", "password_hash", "is_admin", "display_name"}
    )
    retired_tables = ["class_toilets"] if "class_toilets" in tables else []
    details["removed_legacy_user_columns"] = retired_user_columns
    details["dropped_legacy_tables"] = retired_tables

    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN IMMEDIATE")

        if retired_user_columns:
            current_columns = {
                row[1] for row in cursor.execute("PRAGMA table_info('users')")
            }
            if not {"id", "username"} <= current_columns:
                raise RuntimeError(
                    "Cannot rebuild legacy users table without id and username"
                )

            def source(column: str, fallback: str) -> str:
                return f'"{column}"' if column in current_columns else fallback

            control_id = source("control_id", "NULL")
            enabled = source("enabled", "1")
            created_at = source("created_at", "CURRENT_TIMESTAMP")
            updated_at = source("updated_at", "CURRENT_TIMESTAMP")

            cursor.execute("DROP TABLE IF EXISTS users_schema_v5")
            cursor.execute(
                """
                CREATE TABLE users_schema_v5 (
                    id INTEGER NOT NULL PRIMARY KEY,
                    username VARCHAR NOT NULL,
                    control_id INTEGER,
                    enabled BOOLEAN NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
            cursor.execute(
                f"""
                INSERT INTO users_schema_v5 (
                    id, username, control_id, enabled,
                    created_at, updated_at
                )
                SELECT
                    "id", "username", {control_id},
                    COALESCE({enabled}, 1),
                    COALESCE({created_at}, CURRENT_TIMESTAMP),
                    COALESCE({updated_at}, CURRENT_TIMESTAMP)
                FROM users
                """
            )
            cursor.execute("DROP TABLE users")
            cursor.execute("ALTER TABLE users_schema_v5 RENAME TO users")
            cursor.execute(
                "CREATE UNIQUE INDEX ix_users_username ON users(username)"
            )
            cursor.execute(
                "CREATE UNIQUE INDEX ix_users_control_id "
                "ON users(control_id) WHERE control_id IS NOT NULL"
            )
            cursor.execute(
                "CREATE INDEX ix_users_enabled ON users(enabled)"
            )

        cursor.execute("DROP TABLE IF EXISTS class_toilets")
        foreign_key_errors = cursor.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                "Schema cleanup would leave invalid foreign keys: "
                + repr(foreign_key_errors[:10])
            )

        if record_audit:
            cursor.execute(
                "INSERT INTO audit_events(occurred_at,actor_kind,actor_identifier,"
                "action,target_type,target_identifier,correlation_id,details_json) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    datetime.now(UTC).replace(tzinfo=None).isoformat(" "),
                    "system",
                    "schema-upgrade",
                    "schema.upgraded",
                    "database",
                    "toilet2",
                    str(uuid.uuid4()),
                    json.dumps(details, sort_keys=True),
                ),
            )
        # Deliberately the final statement in the atomic cleanup transaction.
        cursor.execute(
            "INSERT INTO schema_version(id,version) VALUES(1,?) "
            "ON CONFLICT(id) DO UPDATE SET version=excluded.version",
            (SCHEMA_VERSION,),
        )
        raw.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    except Exception:
        raw.rollback()
        try:
            recovery_cursor = raw.cursor()
            recovery_cursor.execute("PRAGMA foreign_keys=ON")
            recovery_cursor.close()
        except Exception:
            pass
        raise
    finally:
        raw.close()


def _repair_legacy_fallback_routes(connection) -> int:
    connection.exec_driver_sql(
        "UPDATE requests SET routing_mode='fallback_all' "
        "WHERE type='toilet' AND status IN ('pending','active') AND ("
        "NOT EXISTS (SELECT 1 FROM request_class_snapshots s "
        "WHERE s.request_id=requests.id) OR "
        "NOT EXISTS (SELECT 1 FROM request_toilet_locks l "
        "WHERE l.request_id=requests.id) OR "
        "EXISTS (SELECT 1 FROM request_toilet_locks l "
        "WHERE l.request_id=requests.id "
        "AND l.reason_json LIKE '%legacy_fallback_all%'))"
    )
    connection.exec_driver_sql(
        "UPDATE requests SET blocked_reason='no_toilets' "
        "WHERE type='toilet' AND status='pending' "
        "AND NOT EXISTS (SELECT 1 FROM request_toilet_locks l "
        "WHERE l.request_id=requests.id)"
    )
    opened = 0
    fallback_rows = connection.exec_driver_sql(
        "SELECT r.id,r.created_at,"
        "EXISTS(SELECT 1 FROM request_class_snapshots s WHERE s.request_id=r.id),"
        "EXISTS(SELECT 1 FROM request_toilet_locks l WHERE l.request_id=r.id) "
        "FROM requests r WHERE r.type='toilet' "
        "AND r.status IN ('pending','active') AND r.routing_mode='fallback_all' "
        "ORDER BY r.id"
    ).all()
    for request_id, created_at, has_class, has_lock in fallback_rows:
        codes = ["class_without_toilet" if has_class else "no_class"]
        if not has_lock:
            codes.append("no_toilets")
        for code in codes:
            details = json.dumps(
                {"source": "legacy_upgrade", "reason": "unresolved_legacy_route"},
                sort_keys=True,
            )
            inserted = connection.exec_driver_sql(
                "INSERT OR IGNORE INTO operational_alerts("
                "request_id,code,severity,global_scope,details_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (request_id, code, "warning", 1, details, created_at),
            )
            if inserted.rowcount:
                opened += 1
                connection.exec_driver_sql(
                    "INSERT INTO audit_events(occurred_at,actor_kind,actor_identifier,"
                    "action,target_type,target_identifier,correlation_id,details_json) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        created_at,
                        "system",
                        "schema-upgrade",
                        "alert.opened",
                        "request",
                        str(request_id),
                        str(uuid.uuid4()),
                        json.dumps(
                            {"code": code, "source": "legacy_upgrade"},
                            sort_keys=True,
                        ),
                    ),
                )
    return opened


def _populate_legacy_relations(engine: Engine) -> None:
    with engine.begin() as connection:
        tables = _table_names(connection)
        user_columns = _columns(connection, "users")
        request_columns = _columns(connection, "requests")
        if "school_class_id" in user_columns:
            connection.exec_driver_sql(
                "INSERT OR IGNORE INTO request_class_snapshots("
                "request_id,class_public_id,class_name,source_computers_json) "
                "SELECT r.id,c.public_id,c.name,'[]' FROM requests r "
                "JOIN users u ON u.id=r.user_id "
                "JOIN classes c ON c.id=u.school_class_id"
            )
        if "toilet" in request_columns:
            connection.exec_driver_sql(
                "INSERT OR IGNORE INTO request_toilet_locks("
                "request_id,toilet_id,reason_json,created_at) "
                "SELECT r.id,t.id,?,r.created_at FROM requests r "
                "JOIN toilets t ON t.name=r.toilet WHERE r.toilet IS NOT NULL",
                (json.dumps({"source": "legacy_name"}, sort_keys=True),),
            )
        # Pending legacy requests had no toilet string.  Reconstruct their
        # requirement from the old user's class mapping, then conservatively
        # use every toilet for any remaining open toilet request.
        if "school_class_id" in user_columns:
            connection.exec_driver_sql(
                "INSERT OR IGNORE INTO request_toilet_locks("
                "request_id,toilet_id,reason_json,created_at) "
                "SELECT r.id,c.toilet_id,?,r.created_at FROM requests r "
                "JOIN users u ON u.id=r.user_id JOIN classes c ON c.id=u.school_class_id "
                "WHERE r.type='toilet' AND r.status IN ('pending','active') "
                "AND c.toilet_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM request_toilet_locks l "
                "WHERE l.request_id=r.id)",
                (json.dumps({"source": "legacy_class_mapping"}, sort_keys=True),),
            )
        connection.exec_driver_sql(
            "INSERT OR IGNORE INTO request_toilet_locks("
            "request_id,toilet_id,reason_json,created_at) "
            "SELECT r.id,t.id,?,r.created_at FROM requests r CROSS JOIN toilets t "
            "WHERE r.type='toilet' AND r.status IN ('pending','active') "
            "AND NOT EXISTS (SELECT 1 FROM request_toilet_locks l WHERE l.request_id=r.id)",
            (json.dumps({"source": "legacy_fallback_all"}, sort_keys=True),),
        )
        _repair_legacy_fallback_routes(connection)


def install_audit_triggers(engine: Engine) -> None:
    """Enforce append-only audit history even for direct SQL callers."""

    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS audit_events_reject_update
            BEFORE UPDATE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events are append-only');
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS audit_events_reject_delete
            BEFORE DELETE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events are append-only');
            END
            """
        )


def _legacy_upgrade_details(engine: Engine) -> dict:
    with engine.connect() as connection:
        tables = _table_names(connection)
        collapsed = []
        if "class_toilets" in tables:
            for class_id, chosen_toilet_id, mapping_count in connection.exec_driver_sql(
                "SELECT class_id,MIN(toilet_id),COUNT(*) FROM class_toilets "
                "GROUP BY class_id HAVING COUNT(*) > 1 ORDER BY class_id"
            ).all():
                collapsed.append(
                    {
                        "class_id": class_id,
                        "chosen_toilet_id": chosen_toilet_id,
                        "mapping_count": mapping_count,
                    }
                )
        cancelled = []
        if "requests" in tables and "completed_by" in _columns(connection, "requests"):
            cancelled = [
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT id FROM requests "
                    "WHERE completed_by='schema-upgrade:duplicate-open' ORDER BY id"
                ).all()
            ]
        return {
            "version": SCHEMA_VERSION,
            "collapsed_class_mappings": collapsed,
            "cancelled_duplicate_request_ids": cancelled,
        }


def upgrade_schema(engine: Engine) -> None:
    """Upgrade the legacy schema or create a fresh one; safe to call repeatedly."""

    with engine.connect() as connection:
        tables = _table_names(connection)
        retired_user_columns = set(_columns(connection, "users")) & {
            "school_class_id",
            "password_hash",
            "is_admin",
            "display_name",
        }
        schema_finalization_needed = bool(retired_user_columns)
        version = None
        if "schema_version" in tables:
            version = connection.exec_driver_sql(
                "SELECT version FROM schema_version WHERE id=1"
            ).scalar_one_or_none()
        if version is not None and version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {version} is newer than supported {SCHEMA_VERSION}"
            )
        legacy = version is None and "classes" in tables and (
            "public_id" not in _columns(connection, "classes")
            or "class_toilets" in tables
            or "school_class_id" in _columns(connection, "users")
            or "toilet" in _columns(connection, "requests")
            or "csrf_token" not in _columns(connection, "sessions")
        )
    if legacy:
        _upgrade_legacy_tables(engine)

    Base.metadata.create_all(engine)

    repaired_legacy_locks = 0
    repaired_fallback_alerts = 0
    invalidated_nonproduction_sessions = 0
    migrated_local_operators = 0
    if version is not None and version < 2:
        repaired_legacy_locks = _repair_v1_legacy_locks(engine)
        with engine.begin() as connection:
            repaired_fallback_alerts = _repair_legacy_fallback_routes(connection)

    if version is None or version < SCHEMA_VERSION:
        _rebuild_legacy_classes(engine)
        _ensure_toilet_autoincrement(engine)
    if version is None or version < 3:
        invalidated_nonproduction_sessions = _invalidate_nonproduction_sessions(engine)
    if version is None or version < 4:
        migrated_local_operators = _upgrade_local_identity_tables(engine)

    # Existing tables are skipped by create_all, including their new indexes.
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_classes_public_id ON classes(public_id)"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_control_id "
            "ON users(control_id) WHERE control_id IS NOT NULL"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_users_enabled ON users(enabled)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_classes_toilet_id ON classes(toilet_id)"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_requests_open_user_type "
            "ON requests(user_id,type) WHERE status IN ('pending','active')"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_requests_fifo "
            "ON requests(type,status,created_at,id)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)"
        )
    if legacy:
        _populate_legacy_relations(engine)
    if version is None or version < SCHEMA_VERSION:
        _drop_current_class_mirror(engine)

    install_audit_triggers(engine)

    if (
        version is None
        or version < SCHEMA_VERSION
        or schema_finalization_needed
    ):
        details = (
            _legacy_upgrade_details(engine)
            if legacy
            else {
                "version": SCHEMA_VERSION,
                "from_version": version,
                "repaired_legacy_lock_rows": repaired_legacy_locks,
                "opened_legacy_fallback_alerts": repaired_fallback_alerts,
            }
        )
        if version == SCHEMA_VERSION and schema_finalization_needed:
            details["structural_repair"] = True
        details["invalidated_nonproduction_sessions"] = (
            invalidated_nonproduction_sessions
        )
        details["migrated_local_operators"] = migrated_local_operators
        _finalize_schema_upgrade(
            engine,
            details=details,
            record_audit=legacy or version is not None,
        )
