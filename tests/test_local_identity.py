from pathlib import Path
import json
import uuid

import pytest

from toilet2.database import Database
from toilet2.migrations import LEGACY_CLASS_NAMESPACE
from toilet2.models import (
    AuditEvent,
    OperatorAccount,
    Request,
    RequestClassSnapshot,
    RequestToiletLock,
    SchoolClass,
)
from toilet2.service import Actor, ConflictError, MutationService, ValidationError


def add_migration_generated_class(session, *, name, toilet_id=None):
    row = SchoolClass(
        public_id=str(uuid.uuid4()),
        name=name,
        sequence_num=0,
        toilet_id=toilet_id,
    )
    session.add(row)
    session.flush()
    row.public_id = str(
        uuid.uuid5(
            LEGACY_CLASS_NAMESPACE,
            f"legacy-class:{row.id}:{row.name}",
        )
    )
    session.flush()
    return row


@pytest.fixture
def service(tmp_path):
    database = Database("sqlite:///" + (Path(tmp_path) / "identity.db").as_posix())
    database.initialize()
    try:
        yield MutationService(database, general_request_types=("paper",))
    finally:
        database.dispose()


def test_student_catalog_is_the_only_local_control_identity_sync(service):
    result = service.sync_students(
        [
            {"id": 10, "userid": "alice"},
            {"id": 11, "userid": "bob"},
        ]
    )
    assert result.value["created"] == ["alice", "bob"]
    assert set(service.get_student("alice")) == {
        "id",
        "control_id",
        "userid",
        "username",
        "enabled",
    }

    result = service.sync_students([{"id": 10, "userid": "alice"}])
    assert result.value["disabled"] == ["bob"]
    assert service.get_student("alice")["userid"] == "alice"
    assert service.get_student("bob") is None


def test_local_operator_credentials_scopes_and_last_admin_guard(service):
    with pytest.raises(ValidationError, match="operator username must start"):
        service.upsert_operator(
            username="unsafe/operator",
            display_name="Unsafe operator",
            password="a-secure-password",
            roles={"proctor"},
            all_classes=True,
            actor=Actor.system("test"),
        )

    created = service.upsert_operator(
        username="admin",
        display_name="Local administrator",
        password="a-secure-password",
        roles={"admin"},
        actor=Actor.system("test"),
    )
    assert created.value["created"]
    assert service.authenticate_operator("admin", "wrong") is None
    authenticated = service.authenticate_operator("admin", "a-secure-password")
    assert authenticated["roles"] == {"admin"}

    scope = "10000000-0000-0000-0000-000000000001"
    service.upsert_operator(
        username="proctor",
        display_name="Room proctor",
        password="another-secure-password",
        roles={"proctor"},
        class_scope={scope},
        actor=Actor.system("test"),
    )
    proctor = service.authenticate_operator("proctor", "another-secure-password")
    assert proctor["class_scope"] == {scope}
    assert not proctor["all_classes"]

    with pytest.raises(ConflictError, match="last enabled"):
        service.delete_operator("admin", actor=Actor.system("test"))

    service.upsert_operator(
        username="second-admin",
        display_name="Second administrator",
        password="third-secure-password",
        roles={"admin"},
        actor=Actor.system("test"),
    )
    assert service.delete_operator(
        "admin", actor=Actor.system("test")
    ).value["deleted"]


def test_legacy_class_mapping_key_reconciliation_is_exact_and_conservative(service):
    toilet = service.create_toilet(
        name="North", actor=Actor.system("test")
    ).value["id"]
    with service.database.immediate_session() as session:
        legacy_id = add_migration_generated_class(
            session, name="Room 101", toilet_id=toilet
        ).public_id
        ambiguous_id = add_migration_generated_class(
            session, name="Repeated", toilet_id=toilet
        ).public_id
        session.commit()

    real_id = str(uuid.uuid4())
    repeated_targets = [str(uuid.uuid4()), str(uuid.uuid4())]
    result = service.reconcile_legacy_class_mapping_keys(
        [
            {"id": real_id, "name": "Room 101"},
            {"id": repeated_targets[0], "name": "Repeated"},
            {"id": repeated_targets[1], "name": "Repeated"},
        ]
    ).value
    assert result["remapped"] == [
        {
            "legacy_public_id": legacy_id,
            "legacy_name": "Room 101",
            "toilet_id": toilet,
            "target_public_id": real_id,
        }
    ]
    assert result["unresolved"][0]["legacy_public_id"] == ambiguous_id
    assert result["unresolved"][0]["reason"] == "ambiguous_live_name"

    with service.database.SessionLocal() as session:
        assert session.query(SchoolClass).filter_by(public_id=real_id).one().toilet_id == toilet
        assert session.query(SchoolClass).filter_by(public_id=ambiguous_id).one()
        assert session.query(AuditEvent).filter_by(
            action="class.mapping_keys_reconciled"
        ).count() == 1

    # An unchanged unresolved set must not append another startup audit row.
    repeated = service.reconcile_legacy_class_mapping_keys(
        [
            {"id": real_id, "name": "Room 101"},
            {"id": repeated_targets[0], "name": "Repeated"},
            {"id": repeated_targets[1], "name": "Repeated"},
        ]
    ).value
    assert repeated["remapped"] == []
    assert repeated["unresolved"][0]["reason"] == "ambiguous_live_name"
    with service.database.SessionLocal() as session:
        assert session.query(AuditEvent).filter_by(
            action="class.mapping_keys_reconciled"
        ).count() == 1


def test_non_migration_uuid_is_never_name_remapped(service):
    toilet = service.create_toilet(
        name="North", actor=Actor.system("test")
    ).value["id"]
    old_real_id = str(uuid.uuid4())
    replacement_id = str(uuid.uuid4())
    with service.database.immediate_session() as session:
        session.add(
            SchoolClass(
                public_id=old_real_id,
                name="Room 101",
                sequence_num=0,
                toilet_id=toilet,
            )
        )
        session.commit()

    result = service.reconcile_legacy_class_mapping_keys(
        [{"id": replacement_id, "name": "Room 101"}]
    ).value
    assert result["remapped"] == []
    assert result["unresolved"] == [
        {
            "legacy_public_id": old_real_id,
            "legacy_name": "Room 101",
            "toilet_id": toilet,
            "reason": "not_legacy_generated",
        }
    ]
    with service.database.SessionLocal() as session:
        assert session.query(SchoolClass).filter_by(public_id=old_real_id).one()
        assert session.query(SchoolClass).filter_by(
            public_id=replacement_id
        ).first() is None


def test_reconciliation_rejects_different_existing_toilet_mapping(service):
    north = service.create_toilet(
        name="North", actor=Actor.system("test")
    ).value["id"]
    south = service.create_toilet(
        name="South", actor=Actor.system("test")
    ).value["id"]
    real_id = str(uuid.uuid4())
    with service.database.immediate_session() as session:
        legacy_id = add_migration_generated_class(
            session, name="Room 101", toilet_id=north
        ).public_id
        session.add(
            SchoolClass(
                public_id=real_id,
                name="Room 101",
                sequence_num=0,
                toilet_id=south,
            )
        )
        session.commit()

    result = service.reconcile_legacy_class_mapping_keys(
        [{"id": real_id, "name": "Room 101"}]
    ).value
    assert result["remapped"] == []
    assert result["unresolved"][0]["reason"] == "target_already_mapped"
    assert result["unresolved"][0]["target_toilet_id"] == south
    with service.database.SessionLocal() as session:
        assert (
            session.query(SchoolClass)
            .filter_by(public_id=legacy_id)
            .one()
            .toilet_id
            == north
        )
        assert (
            session.query(SchoolClass)
            .filter_by(public_id=real_id)
            .one()
            .toilet_id
            == south
        )


def test_reconciliation_merges_target_and_updates_only_open_request_snapshots(service):
    toilet = service.create_toilet(
        name="North", actor=Actor.system("test")
    ).value["id"]
    real_id = str(uuid.uuid4())
    neutral_real_id = str(uuid.uuid4())
    same_mapped_real_id = str(uuid.uuid4())
    with service.database.immediate_session() as session:
        legacy_id = add_migration_generated_class(
            session, name="Room 101", toilet_id=toilet
        ).public_id
        neutral_legacy_id = add_migration_generated_class(
            session, name="Room 202", toilet_id=None
        ).public_id
        same_mapped_legacy_id = add_migration_generated_class(
            session, name="Room 303", toilet_id=toilet
        ).public_id
        session.add_all(
            [
                SchoolClass(
                    public_id=real_id,
                    name="Room 101",
                    sequence_num=0,
                    toilet_id=None,
                ),
                SchoolClass(
                    public_id=same_mapped_real_id,
                    name="Room 303",
                    sequence_num=0,
                    toilet_id=toilet,
                ),
            ]
        )
        session.commit()

    service.upsert_operator(
        username="room-proctor",
        display_name="Room proctor",
        password="another-secure-password",
        roles={"proctor"},
        class_scope={legacy_id, neutral_legacy_id},
        actor=Actor.system("test"),
    )
    operator_session = service.issue_session(
        subject_type="operator",
        subject="room-proctor",
        display_name="Room proctor",
        roles={"proctor"},
        class_scope={legacy_id, neutral_legacy_id},
    ).value["token"]

    service.sync_students(
        [
            {"id": 10, "userid": "alice"},
            {"id": 11, "userid": "bob"},
            {"id": 12, "userid": "charlie"},
        ]
    )

    def assignment(userid, *, include_real_target=False):
        computers = [
            {
                "machine_id": f"pc-{userid}",
                "name": f"PC {userid}",
                "class": {"id": legacy_id, "name": "Room 101"},
            }
        ]
        if include_real_target:
            # Exercise reconciliation of a historical ambiguous snapshot. The
            # live Control contract now supplies at most one computer.
            computers.append(
                {
                    "machine_id": f"pc-{userid}-duplicate",
                    "name": f"PC {userid} duplicate",
                    "class": {"id": real_id, "name": "Room 101"},
                }
            )
        return {
            "lookup_ok": True,
            "userid": userid,
            "computers": computers,
            "anomalies": [],
        }

    active = service.create_request(
        user_id=service.get_student("alice")["id"],
        request_type="toilet",
        assignment=assignment("alice"),
    ).value
    pending = service.create_request(
        user_id=service.get_student("bob")["id"],
        request_type="toilet",
        assignment=assignment("bob", include_real_target=True),
    ).value
    closed = service.create_request(
        user_id=service.get_student("charlie")["id"],
        request_type="paper",
        assignment=assignment("charlie"),
    ).value
    service.resolve_support_request(
        request_id=closed["id"], actor=Actor.system("test")
    )
    assert active["status"] == "active"
    assert pending["status"] == "pending"

    result = service.reconcile_legacy_class_mapping_keys(
        [
            {"id": real_id, "name": "Room 101"},
            {"id": neutral_real_id, "name": "Room 202"},
            {"id": same_mapped_real_id, "name": "Room 303"},
        ]
    )
    remaps = {
        item["legacy_public_id"]: item["target_public_id"]
        for item in result.value["remapped"]
    }
    assert remaps == {
        legacy_id: real_id,
        neutral_legacy_id: neutral_real_id,
        same_mapped_legacy_id: same_mapped_real_id,
    }
    assert result.value["operators_rescoped"] == ["room-proctor"]
    assert result.value["sessions_revoked"] == 1
    assert result.value["open_requests_remapped"] == [
        active["id"],
        pending["id"],
    ]
    assert result.value["pending_requests_rebuilt"] == [pending["id"]]
    assert result.request_ids == frozenset({active["id"], pending["id"]})
    assert service.get_session(operator_session) is None

    with service.database.SessionLocal() as session:
        assert session.query(SchoolClass).filter_by(public_id=legacy_id).first() is None
        assert (
            session.query(SchoolClass)
            .filter_by(public_id=neutral_legacy_id)
            .first()
            is None
        )
        assert (
            session.query(SchoolClass)
            .filter_by(public_id=same_mapped_legacy_id)
            .first()
            is None
        )
        assert (
            session.query(SchoolClass).filter_by(public_id=real_id).one().toilet_id
            == toilet
        )
        assert (
            session.query(SchoolClass)
            .filter_by(public_id=same_mapped_real_id)
            .one()
            .toilet_id
            == toilet
        )
        assert session.get(OperatorAccount, "room-proctor").class_scope == frozenset(
            {real_id, neutral_real_id}
        )

        active_snapshots = {
            item.class_public_id
            for item in session.query(RequestClassSnapshot)
            .filter_by(request_id=active["id"])
            .all()
        }
        pending_snapshots = {
            item.class_public_id
            for item in session.query(RequestClassSnapshot)
            .filter_by(request_id=pending["id"])
            .all()
        }
        closed_snapshots = {
            item.class_public_id
            for item in session.query(RequestClassSnapshot)
            .filter_by(request_id=closed["id"])
            .all()
        }
        assert active_snapshots == {real_id}
        assert pending_snapshots == {real_id}
        assert closed_snapshots == {legacy_id}

        pending_request = session.get(Request, pending["id"])
        pending_payload = json.loads(pending_request.identity_snapshot_json)
        assert len(pending_payload["classes"]) == 1
        assert pending_payload["classes"][0]["public_id"] == real_id
        assert pending_payload["computers"][0]["class"]["public_id"] == real_id
        assert pending_request.status == "pending"
        pending_locks = (
            session.query(RequestToiletLock)
            .filter_by(request_id=pending["id"])
            .all()
        )
        assert [item.toilet_id for item in pending_locks] == [toilet]
