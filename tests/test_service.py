from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from toilet2.database import Database
from toilet2.migrations import SCHEMA_VERSION
from toilet2.models import (
    AuditEvent,
    OperationalAlert,
    RateLimitBucket,
    Request,
    RequestToiletLock,
    SchoolClass,
    Toilet,
)
from toilet2.service import (
    Actor,
    ConflictError,
    ForbiddenError,
    MutationService,
    RateLimitExceeded,
    ValidationError,
)


ADMIN = Actor("operator", "admin", frozenset({"admin"}), frozenset(), True)
PROCTOR_ALL = Actor(
    "operator", "hall-proctor", frozenset({"proctor"}), frozenset(), True
)


class FakeClock:
    def __init__(self):
        self.value = datetime(2026, 7, 19, 12, 0, 0)

    def __call__(self):
        return self.value

    def advance(self, seconds: int):
        self.value += timedelta(seconds=seconds)


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="toilet2-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.temp_dir, ignore_errors=True))
        self.database = Database(
            f"sqlite:///{(self.temp_dir / 'test.db').as_posix()}", busy_timeout_ms=2_000
        )
        self.addCleanup(self.database.dispose)
        self.database.initialize()
        self.clock = FakeClock()
        self.service = MutationService(
            self.database,
            clock=self.clock,
            student_rate_limit=100,
            operator_login_rate_limit=100,
            # Support requests are disabled by default; these cases exercise the
            # opted-in behaviour.
            general_request_types=("paper",),
        )

    def class_item(self, name: str, sequence: int = 0):
        return {
            "public_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"class:{name}")),
            "name": name,
            "sequence_num": sequence,
        }

    def sync(self, *names: str, revision: int = 1):
        items = [self.class_item(name, index) for index, name in enumerate(names)]
        self.service.sync_class_catalog(items, actor=ADMIN, revision=revision)
        return {item["name"]: item["public_id"] for item in items}

    def toilet(self, name: str, capacity: int = 1) -> int:
        return self.service.create_toilet(
            name=name, capacity=capacity, actor=ADMIN
        ).value["id"]

    def user(self, username: str) -> int:
        return self.service.ensure_student(username).value["id"]

    @staticmethod
    def _assignment_payload(
        *classes: tuple[str, str], unmapped: bool = False
    ) -> dict:
        computers = []
        for index, (public_id, name) in enumerate(classes):
            computers.append(
                {
                    "id": f"pc-{index}",
                    "name": f"PC {index}",
                    "class": {"public_id": public_id, "name": name},
                }
            )
        if unmapped:
            computers.append({"id": "spare", "name": "Spare", "class": None})
        return {"lookup_ok": True, "computers": computers, "anomalies": []}

    def assignment(self, *classes: tuple[str, str], unmapped: bool = False):
        if len(classes) + int(unmapped) > 1:
            raise AssertionError(
                "a valid contestant assignment contains at most one computer"
            )
        return self._assignment_payload(*classes, unmapped=unmapped)

    def malformed_legacy_assignment(
        self, *classes: tuple[str, str], unmapped: bool = False
    ):
        if len(classes) + int(unmapped) <= 1:
            raise AssertionError("legacy ambiguity fixture must contain several computers")
        return self._assignment_payload(*classes, unmapped=unmapped)

    def assign(self, class_id: str, toilet_id: int | None):
        self.service.assign_class_toilet(
            class_public_id=class_id, toilet_id=toilet_id, actor=ADMIN
        )

    def request(self, username: str, assignment, request_type="toilet"):
        user_id = self.user(username)
        result = self.service.create_request(
            user_id=user_id,
            request_type=request_type,
            assignment=assignment,
        )
        return user_id, result.value

    def orm(self):
        return self.database.SessionLocal()


class RoutingTests(ServiceTestCase):
    def test_legacy_multi_computer_payload_locks_all_routes_and_alerts(self):
        classes = self.sync("A", "B")
        first = self.toilet("T1")
        second = self.toilet("T2")
        self.assign(classes["A"], first)
        self.assign(classes["B"], second)

        user_id, multi = self.request(
            "multi",
            self.malformed_legacy_assignment(
                (classes["A"], "A"), (classes["B"], "B")
            ),
        )
        self.assertEqual("active", multi["status"])
        self.assertEqual({first, second}, {item["id"] for item in multi["toilets"]})

        waiting_id, waiting = self.request(
            "waiting", self.assignment((classes["A"], "A"))
        )
        self.assertEqual("pending", waiting["status"])
        self.service.complete_toilet_request(request_id=multi["id"], actor=PROCTOR_ALL)
        self.assertEqual("active", self.service.get_request(waiting["id"])["status"])

        with self.orm() as session:
            self.assertEqual(
                1,
                session.query(RequestToiletLock)
                .join(Request)
                .filter(Request.status == "active", RequestToiletLock.toilet_id == first)
                .count(),
            )

    def test_legacy_multi_computer_payload_deduplicates_a_shared_toilet(self):
        classes = self.sync("A", "B")
        toilet_id = self.toilet("Shared")
        self.assign(classes["A"], toilet_id)
        self.assign(classes["B"], toilet_id)
        _, request = self.request(
            "student",
            self.malformed_legacy_assignment(
                (classes["A"], "A"), (classes["B"], "B")
            ),
        )
        self.assertEqual("active", request["status"])
        self.assertEqual([toilet_id], [item["id"] for item in request["toilets"]])
        self.assertIn("multiple_classes", {item["code"] for item in request["alerts"]})

    def test_student_cannot_cancel_active_visit_and_free_capacity(self):
        classes = self.sync("A")
        toilet_id = self.toilet("Only")
        self.assign(classes["A"], toilet_id)
        active_user, active = self.request(
            "active", self.assignment((classes["A"], "A"))
        )
        _, waiting = self.request(
            "waiting", self.assignment((classes["A"], "A"))
        )
        with self.assertRaises(ConflictError):
            self.service.cancel_request(
                request_id=active["id"], user_id=active_user
            )
        self.assertEqual("active", self.service.get_request(active["id"])["status"])
        self.assertEqual("pending", self.service.get_request(waiting["id"])["status"])
        self.service.complete_toilet_request(request_id=active["id"], actor=PROCTOR_ALL)
        self.assertEqual("active", self.service.get_request(waiting["id"])["status"])

    def test_blocked_older_request_prevents_overlap_but_not_disjoint(self):
        classes = self.sync("A", "B", "C")
        toilets = {name: self.toilet(name) for name in classes}
        for name, public_id in classes.items():
            self.assign(public_id, toilets[name])

        _, occupying = self.request("occupying", self.assignment((classes["A"], "A")))
        self.clock.advance(1)
        _, older = self.request(
            "older",
            self.malformed_legacy_assignment(
                (classes["A"], "A"), (classes["B"], "B")
            ),
        )
        self.clock.advance(1)
        _, overlap = self.request("overlap", self.assignment((classes["B"], "B")))
        self.clock.advance(1)
        _, disjoint = self.request("disjoint", self.assignment((classes["C"], "C")))
        self.assertEqual("active", occupying["status"])
        self.assertEqual("pending", older["status"])
        self.assertEqual("pending", overlap["status"])
        self.assertEqual("active", disjoint["status"])

    def test_unmapped_fallback_locks_all_and_empty_config_blocks(self):
        user_id = self.user("unknown")
        empty = self.service.create_request(
            user_id=user_id, request_type="toilet", assignment={"computers": []}
        ).value
        self.assertEqual("pending", empty["status"])
        self.assertEqual("no_toilets", empty["blocked_reason"])
        self.assertIn("no_class", {item["code"] for item in empty["alerts"]})

        # Creation is intentionally non-reconciling: the old request stays empty.
        first = self.toilet("T1")
        second = self.toilet("T2")
        _, fallback = self.request("unknown2", {"computers": []})
        self.assertEqual("active", fallback["status"])
        self.assertEqual({first, second}, {item["id"] for item in fallback["toilets"]})

    def test_legacy_assigned_plus_unmapped_payload_uses_known_route_and_alerts(self):
        classes = self.sync("A")
        toilet_id = self.toilet("T")
        self.assign(classes["A"], toilet_id)
        _, request = self.request(
            "student",
            self.malformed_legacy_assignment(
                (classes["A"], "A"), unmapped=True
            ),
        )
        self.assertEqual("normal", request["routing_mode"])
        self.assertEqual([toilet_id], [item["id"] for item in request["toilets"]])
        self.assertIn("computer_without_class", {item["code"] for item in request["alerts"]})

    def test_control_api_shape_is_normalized_without_losing_assignment_context(self):
        classes = self.sync("A")
        toilet_id = self.toilet("T")
        self.assign(classes["A"], toilet_id)
        payload = {
            "found": True,
            "userid": "cms-user",
            "computers": [
                {
                    "machine_id": "machine-1",
                    "name": "PC 1",
                    "sequence_num": -2,
                    "grid_row": 3,
                    "grid_col": 5,
                    "student": {
                        "id": 17,
                        "userid": "cms-user",
                    },
                    "class": {
                        "id": classes["A"],
                        "name": "A",
                        "sequence_num": 4,
                        "grid_cols": 8,
                    },
                },
            ],
            "classes": [
                {
                    "id": classes["A"],
                    "name": "A",
                    "sequence_num": 4,
                    "grid_cols": 8,
                }
            ],
            "anomalies": [],
        }
        _, request = self.request("cms-user", payload)
        self.assertEqual("active", request["status"])
        with self.orm() as session:
            stored = json.loads(session.get(Request, request["id"]).identity_snapshot_json)
        self.assertNotIn("display_name", stored)
        self.assertEqual([], stored["anomalies"])
        self.assertEqual(
            {
                "id": "machine-1",
                "machine_id": "machine-1",
                "name": "PC 1",
                "sequence_num": -2,
                "grid_row": 3,
                "grid_col": 5,
                "student": {
                    "id": 17,
                    "userid": "cms-user",
                },
                "class": {
                    "public_id": classes["A"],
                    "name": "A",
                    "sequence_num": 4,
                    "grid_cols": 8,
                },
            },
            stored["computers"][0],
        )
        self.assertEqual(8, stored["classes"][0]["grid_cols"])

    def test_unassign_retains_only_mapping_anchors_still_in_use(self):
        classes = self.sync("Referenced", "Unused")
        toilet_id = self.toilet("T")
        self.assign(classes["Referenced"], toilet_id)
        self.assign(classes["Unused"], toilet_id)
        self.service.upsert_operator(
            username="room-proctor",
            display_name="Room proctor",
            password="strong-enough-password",
            roles=["proctor"],
            class_scope=[classes["Referenced"]],
            actor=ADMIN,
        )
        self.request(
            "student",
            self.assignment((classes["Referenced"], "Referenced")),
        )

        retained = self.service.assign_class_toilet(
            class_public_id=classes["Referenced"],
            toilet_id=None,
            actor=ADMIN,
        ).value
        removed = self.service.assign_class_toilet(
            class_public_id=classes["Unused"],
            toilet_id=None,
            actor=ADMIN,
        ).value

        self.assertTrue(retained["mapping_anchor_retained"])
        self.assertFalse(removed["mapping_anchor_retained"])
        with self.orm() as session:
            referenced = session.query(SchoolClass).filter_by(
                public_id=classes["Referenced"]
            ).one()
            self.assertIsNone(referenced.toilet_id)
            self.assertIsNone(
                session.query(SchoolClass)
                .filter_by(public_id=classes["Unused"])
                .first()
            )


class ReconciliationTests(ServiceTestCase):
    def test_rename_is_name_only_and_create_does_not_rewrite_blocked_route(self):
        classes = self.sync("A")
        toilet_id = self.toilet("Original")
        self.assign(classes["A"], toilet_id)
        _, active = self.request("active", self.assignment((classes["A"], "A")))
        original_ids = [item["id"] for item in active["toilets"]]
        self.service.rename_toilet(toilet_id=toilet_id, name="Renamed", actor=ADMIN)
        renamed = self.service.get_request(active["id"])
        self.assertEqual(original_ids, [item["id"] for item in renamed["toilets"]])
        self.assertEqual("Renamed", renamed["toilets"][0]["name"])

        unknown_id = self.user("blocked")
        blocked = self.service.create_request(
            user_id=unknown_id, request_type="toilet", assignment={"computers": []}
        ).value
        # It locks the one existing toilet and waits because it is occupied.
        self.assertEqual([toilet_id], [item["id"] for item in blocked["toilets"]])
        added = self.toilet("Added")
        after_create = self.service.get_request(blocked["id"])
        self.assertEqual([toilet_id], [item["id"] for item in after_create["toilets"]])
        self.assertNotIn(added, [item["id"] for item in after_create["toilets"]])

    def test_mapping_change_moves_pending_and_preserves_active(self):
        classes = self.sync("A")
        old = self.toilet("Old")
        new = self.toilet("New")
        self.assign(classes["A"], old)
        _, active = self.request("active", self.assignment((classes["A"], "A")))
        _, pending = self.request("pending", self.assignment((classes["A"], "A")))

        self.assign(classes["A"], new)
        active_after = self.service.get_request(active["id"])
        pending_after = self.service.get_request(pending["id"])
        self.assertEqual("active", active_after["status"])
        self.assertEqual([old], [item["id"] for item in active_after["toilets"]])
        self.assertEqual("active", pending_after["status"])
        self.assertEqual([new], [item["id"] for item in pending_after["toilets"]])

    def test_toilet_delete_demotes_active_removes_fk_and_raises_urgent_alert(self):
        classes = self.sync("A")
        toilet_id = self.toilet("Only")
        self.assign(classes["A"], toilet_id)
        _, request = self.request("student", self.assignment((classes["A"], "A")))
        self.assertEqual("active", request["status"])

        self.service.delete_toilet(toilet_id=toilet_id, actor=ADMIN)
        after = self.service.get_request(request["id"])
        self.assertEqual("pending", after["status"])
        self.assertEqual([], after["toilets"])
        self.assertEqual("no_toilets", after["blocked_reason"])
        self.assertIn("active_toilet_deleted", {item["code"] for item in after["alerts"]})
        with self.orm() as session:
            self.assertEqual(0, session.query(RequestToiletLock).filter_by(toilet_id=toilet_id).count())

    def test_capacity_decrease_rejected_and_increase_promotes(self):
        classes = self.sync("A")
        toilet_id = self.toilet("T", capacity=2)
        self.assign(classes["A"], toilet_id)
        _, active = self.request("one", self.assignment((classes["A"], "A")))
        _, active_two = self.request("two", self.assignment((classes["A"], "A")))
        with self.assertRaises(ConflictError):
            self.service.set_toilet_capacity(toilet_id=toilet_id, capacity=1, actor=ADMIN)
        _, pending = self.request("three", self.assignment((classes["A"], "A")))
        self.service.set_toilet_capacity(toilet_id=toilet_id, capacity=3, actor=ADMIN)
        self.assertEqual("active", self.service.get_request(active["id"])["status"])
        self.assertEqual("active", self.service.get_request(active_two["id"])["status"])
        self.assertEqual("active", self.service.get_request(pending["id"])["status"])

    def test_catalog_rename_preserves_active_and_omission_rebuilds_pending(self):
        classes = self.sync("A")
        toilet_id = self.toilet("T")
        other_id = self.toilet("Other")
        self.assign(classes["A"], toilet_id)
        _, active = self.request("active", self.assignment((classes["A"], "A")))
        _, pending = self.request("pending", self.assignment((classes["A"], "A")))

        renamed = {"public_id": classes["A"], "name": "Renamed", "sequence_num": 0}
        self.service.sync_class_catalog([renamed], actor=ADMIN, revision=2)
        self.assertEqual([toilet_id], [x["id"] for x in self.service.get_request(active["id"])["toilets"]])
        self.service.sync_class_catalog([], actor=ADMIN, revision=3)
        self.assertEqual([toilet_id], [x["id"] for x in self.service.get_request(active["id"])["toilets"]])
        pending_after = self.service.get_request(pending["id"])
        self.assertEqual({toilet_id, other_id}, {x["id"] for x in pending_after["toilets"]})
        self.assertEqual("pending", pending_after["status"])

    def test_unchanged_catalog_sync_is_not_audited_or_broadcast(self):
        items = [self.class_item("A", 1), self.class_item("B", 2)]
        self.service.sync_class_catalog(items, actor=ADMIN, revision=1)
        unchanged = self.service.sync_class_catalog(items, actor=ADMIN, revision=1)

        self.assertFalse(unchanged.staff_changed)
        self.assertEqual([], unchanged.value["created"])
        self.assertEqual([], unchanged.value["renamed"])
        self.assertEqual([], unchanged.value["reordered"])
        self.assertEqual([], unchanged.value["deleted"])
        with self.orm() as session:
            self.assertEqual(
                1,
                session.query(AuditEvent)
                .filter_by(action="class.catalog_synced")
                .count(),
            )

        reordered = self.service.sync_class_catalog(
            [self.class_item("A", 2), self.class_item("B", 1)],
            actor=ADMIN,
            revision=2,
        )
        self.assertTrue(reordered.staff_changed)
        self.assertEqual(
            {item["public_id"] for item in items}, set(reordered.value["reordered"])
        )
        with self.orm() as session:
            event = (
                session.query(AuditEvent)
                .filter_by(action="class.catalog_synced")
                .order_by(AuditEvent.id.desc())
                .first()
            )
            self.assertEqual(
                set(reordered.value["reordered"]),
                set(json.loads(event.details_json)["reordered"]),
            )

    def test_stale_catalog_revision_is_rejected_without_changes(self):
        classes = self.sync("A", revision=5)
        with self.assertRaises(ConflictError):
            self.service.sync_class_catalog([], actor=ADMIN, revision=4)
        with self.orm() as session:
            self.assertEqual([classes["A"]], [item.public_id for item in session.query(SchoolClass)])


class SecurityAndIntegrityTests(ServiceTestCase):
    def test_transaction_rolls_back_request_locks_alerts_rate_and_audit(self):
        user_id = self.user("student")
        self.toilet("T")
        before = {}
        with self.orm() as session:
            before = {
                "requests": session.query(Request).count(),
                "alerts": session.query(OperationalAlert).count(),
                "audit": session.query(AuditEvent).count(),
            }
        original = self.service._schedule

        def explode(*args, **kwargs):
            raise RuntimeError("injected failure")

        self.service._schedule = explode
        try:
            with self.assertRaises(RuntimeError):
                self.service.create_request(
                    user_id=user_id, request_type="toilet", assignment={"computers": []}
                )
        finally:
            self.service._schedule = original
        with self.orm() as session:
            self.assertEqual(before["requests"], session.query(Request).count())
            self.assertEqual(before["alerts"], session.query(OperationalAlert).count())
            self.assertEqual(before["audit"], session.query(AuditEvent).count())

    def test_partial_unique_index_and_serialized_concurrent_create(self):
        classes = self.sync("A")
        toilet_id = self.toilet("T")
        self.assign(classes["A"], toilet_id)
        user_id = self.user("student")
        assignment = self.assignment((classes["A"], "A"))
        barrier = threading.Barrier(3)
        outcomes = []

        def worker():
            barrier.wait()
            try:
                result = self.service.create_request(
                    user_id=user_id, request_type="toilet", assignment=assignment
                )
                outcomes.append(("ok", result.value["id"]))
            except ConflictError:
                outcomes.append(("conflict", None))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
        self.assertEqual(["conflict", "ok"], sorted(item[0] for item in outcomes))

    def test_rate_limit_persists_rejection_audit_and_rolls_window(self):
        service = MutationService(
            self.database,
            clock=self.clock,
            student_rate_limit=1,
            student_rate_window_seconds=60,
            general_request_types=("paper",),
        )
        user_id = self.user("limited")
        first = service.create_request(
            user_id=user_id, request_type="paper", assignment={"computers": []}
        ).value
        self.clock.advance(1)
        with self.assertRaises(RateLimitExceeded) as caught:
            service.cancel_request(request_id=first["id"], user_id=user_id)
        self.assertEqual(caught.exception.retry_after, 59)
        with self.orm() as session:
            bucket = session.query(RateLimitBucket).filter_by(
                actor_key="student:limited", action_group="student_mutation"
            ).one()
            self.assertEqual(datetime(2026, 7, 19, 12, 0), bucket.window_started_at)
            self.assertEqual(
                1, session.query(AuditEvent).filter_by(action="rate_limit.rejected").count()
            )
        self.clock.advance(61)
        service.cancel_request(request_id=first["id"], user_id=user_id)

    def test_duplicate_mutation_attempts_consume_and_bound_rate_audit(self):
        service = MutationService(
            self.database,
            clock=self.clock,
            student_rate_limit=2,
            student_rate_window_seconds=60,
            general_request_types=("paper",),
        )
        user_id = self.user("churn")
        service.create_request(
            user_id=user_id, request_type="paper", assignment={"computers": []}
        )
        with self.assertRaises(ConflictError):
            service.create_request(
                user_id=user_id, request_type="paper", assignment={"computers": []}
            )
        for _ in range(3):
            with self.assertRaises(RateLimitExceeded):
                service.create_request(
                    user_id=user_id,
                    request_type="paper",
                    assignment={"computers": []},
                )
        with self.orm() as session:
            bucket = session.query(RateLimitBucket).filter_by(
                actor_key="student:churn", action_group="student_mutation"
            ).one()
            self.assertEqual(3, bucket.count)
            self.assertEqual(
                1, session.query(AuditEvent).filter_by(action="rate_limit.rejected").count()
            )

    def test_routing_alert_remains_open_across_noop_reconciliation(self):
        classes = self.sync("A", "B")
        toilet_id = self.toilet("Shared")
        self.assign(classes["A"], toilet_id)
        self.assign(classes["B"], toilet_id)
        self.request("occupant", self.assignment((classes["A"], "A")))
        _, waiting = self.request(
            "ambiguous",
            self.malformed_legacy_assignment(
                (classes["A"], "A"), (classes["B"], "B")
            ),
        )
        self.assertEqual("pending", waiting["status"])
        self.assign(classes["A"], toilet_id)
        with self.orm() as session:
            alert = session.query(OperationalAlert).filter_by(
                request_id=waiting["id"], code="multiple_classes"
            ).one()
            self.assertIsNone(alert.resolved_at)
            events = session.query(AuditEvent).filter(
                AuditEvent.target_identifier == str(waiting["id"]),
                AuditEvent.action == "alert.opened",
            ).count()
            self.assertEqual(1, events)

    def test_deleted_toilet_identifier_is_not_reused(self):
        first = self.toilet("First")
        self.service.delete_toilet(toilet_id=first, actor=ADMIN)
        second = self.toilet("Second")
        self.assertGreater(second, first)

    def test_deleted_local_class_identifier_is_not_reused(self):
        first_sync = self.sync("A", "B")
        with self.orm() as session:
            deleted_id = session.query(SchoolClass).filter_by(
                public_id=first_sync["B"]
            ).one().id
        self.sync("A", revision=2)
        second_sync = self.sync("A", "C", revision=3)
        with self.orm() as session:
            new_id = session.query(SchoolClass).filter_by(
                public_id=second_sync["C"]
            ).one().id
        self.assertGreater(new_id, deleted_id)

    def test_audit_is_database_enforced_append_only(self):
        self.toilet("T")
        with self.database.engine.connect() as connection:
            event_id = connection.execute(text("SELECT MIN(id) FROM audit_events")).scalar_one()
            with self.assertRaises(DatabaseError):
                connection.execute(
                    text("UPDATE audit_events SET action='tampered' WHERE id=:id"),
                    {"id": event_id},
                )
            connection.rollback()
            with self.assertRaises(DatabaseError):
                connection.execute(
                    text("DELETE FROM audit_events WHERE id=:id"), {"id": event_id}
                )

    def test_session_expiry_csrf_and_role_scope_snapshot(self):
        class_id = self.class_item("A")["public_id"]
        issued = self.service.issue_session(
            subject_type="operator",
            subject="proctor",
            roles=["proctor"],
            class_scope=[class_id],
            ttl_seconds=30,
            actor=ADMIN,
        ).value
        current = self.service.get_session(issued["token"])
        self.assertEqual(issued["csrf_token"], current["csrf_token"])
        self.assertEqual(["proctor"], current["roles"])
        self.assertEqual([class_id], current["class_scope"])
        self.clock.advance(31)
        self.assertIsNone(self.service.get_session(issued["token"]))
        with self.assertRaises(ValidationError):
            self.service.issue_session(
                subject_type="operator", subject="no-role", roles=[], actor=ADMIN
            )
        with self.assertRaisesRegex(ValidationError, "subject type"):
            self.service.issue_session(
                subject_type="dev_operator",
                subject="retired",
                roles=["admin"],
                actor=ADMIN,
            )
        with self.assertRaisesRegex(ValidationError, "student user"):
            self.service.issue_session(
                subject_type="cms", subject="student", contest="contest", actor=ADMIN
            )
        with self.assertRaisesRegex(ValidationError, "operator authority"):
            self.service.issue_session(
                subject_type="cms",
                subject="student",
                user_id=self.user("cms-student"),
                contest="contest",
                roles=["admin"],
                actor=ADMIN,
            )

    def test_unconfigured_service_only_accepts_toilet_requests(self):
        service = MutationService(self.database, clock=self.clock)
        self.assertEqual(frozenset(), service.general_request_types)
        user_id = self.user("default-config")
        for request_type in ("paper", "water", "snack", "tech"):
            with self.assertRaisesRegex(ValidationError, "not enabled"):
                service.create_request(
                    user_id=user_id,
                    request_type=request_type,
                    assignment={"computers": []},
                )
        created = service.create_request(
            user_id=user_id, request_type="toilet", assignment={"computers": []}
        ).value
        self.assertEqual("toilet", created["type"])

    def test_support_resolution_enforces_proctor_scope(self):
        classes = self.sync("A", "B")
        user_id, support = self.request(
            "student", self.assignment((classes["A"], "A")), request_type="paper"
        )
        wrong = Actor(
            "operator", "wrong", frozenset({"proctor"}), frozenset({classes["B"]}), False
        )
        with self.assertRaises(ForbiddenError):
            self.service.resolve_support_request(request_id=support["id"], actor=wrong)
        right = Actor(
            "operator", "right", frozenset({"proctor"}), frozenset({classes["A"]}), False
        )
        result = self.service.resolve_support_request(request_id=support["id"], actor=right)
        self.assertEqual("done", result.value["status"])

    def test_alert_listing_and_resolution_enforce_class_scope(self):
        classes = self.sync("A", "B")
        toilet_id = self.toilet("T")
        self.assign(classes["A"], toilet_id)
        self.assign(classes["B"], toilet_id)
        _, request = self.request(
            "multi",
            self.malformed_legacy_assignment(
                (classes["A"], "A"), (classes["B"], "B")
            ),
        )
        alert_id = next(
            item["id"] for item in request["alerts"] if item["code"] == "multiple_classes"
        )
        class_a = Actor(
            "operator", "a", frozenset({"proctor"}), frozenset({classes["A"]}), False
        )
        outside = Actor(
            "operator", "outside", frozenset({"proctor"}), frozenset({str(uuid.uuid4())}), False
        )
        self.assertIn(alert_id, {item["id"] for item in self.service.list_open_alerts(class_a)})
        self.assertNotIn(alert_id, {item["id"] for item in self.service.list_open_alerts(outside)})
        with self.assertRaises(ForbiddenError):
            self.service.resolve_alert(alert_id=alert_id, actor=outside)
        self.service.resolve_alert(alert_id=alert_id, actor=class_a)

    def test_manual_return_validation_and_atomic_promotion(self):
        classes = self.sync("A")
        toilet_id = self.toilet("T")
        self.assign(classes["A"], toilet_id)
        _, first = self.request("first", self.assignment((classes["A"], "A")))
        _, second = self.request("second", self.assignment((classes["A"], "A")))
        with self.assertRaises(ValidationError):
            self.service.complete_toilet_request(
                request_id=first["id"], actor=PROCTOR_ALL, completed_at=self.clock() + timedelta(1)
            )
        completed = self.service.complete_toilet_request(
            request_id=first["id"], actor=PROCTOR_ALL, completed_at=self.clock()
        )
        self.assertEqual("done", completed.value["status"])
        self.assertEqual("active", self.service.get_request(second["id"])["status"])

    def test_return_requires_explicit_proctor_role_and_class_scope(self):
        classes = self.sync("A", "B")
        self.assign(classes["A"], self.toilet("A toilet"))
        self.assign(classes["B"], self.toilet("B toilet"))
        _, request_a = self.request("a", self.assignment((classes["A"], "A")))
        _, request_b = self.request("b", self.assignment((classes["B"], "B")))
        class_a_proctor = Actor(
            "operator",
            "class-a",
            frozenset({"proctor"}),
            frozenset({classes["A"]}),
            False,
        )
        dual_class_a = Actor(
            "operator",
            "dual-class-a",
            frozenset({"admin", "proctor"}),
            frozenset({classes["A"]}),
            False,
        )

        with self.assertRaises(ForbiddenError):
            self.service.complete_toilet_request(request_id=request_a["id"], actor=ADMIN)
        with self.assertRaises(ForbiddenError):
            self.service.complete_toilet_request(
                request_id=request_b["id"], actor=class_a_proctor
            )
        with self.assertRaises(ForbiddenError):
            self.service.complete_toilet_request(
                request_id=request_b["id"], actor=dual_class_a
            )
        self.assertEqual("active", self.service.get_request(request_b["id"])["status"])

        self.service.complete_toilet_request(
            request_id=request_a["id"], actor=class_a_proctor
        )
        self.service.complete_toilet_request(
            request_id=request_b["id"], actor=PROCTOR_ALL
        )

    def test_legacy_multi_computer_return_allows_an_affected_class_proctor(self):
        classes = self.sync("A", "B")
        self.assign(classes["A"], self.toilet("A toilet"))
        self.assign(classes["B"], self.toilet("B toilet"))
        _, request = self.request(
            "multi",
            self.malformed_legacy_assignment(
                (classes["A"], "A"), (classes["B"], "B")
            ),
        )
        class_b_proctor = Actor(
            "operator",
            "class-b",
            frozenset({"proctor"}),
            frozenset({classes["B"]}),
            False,
        )
        completed = self.service.complete_toilet_request(
            request_id=request["id"], actor=class_b_proctor
        )
        self.assertEqual("done", completed.value["status"])

    def test_security_audit_redacts_secret_shaped_keys(self):
        self.service.record_security_event(
            "security.test",
            target_identifier="test",
            details={
                "password": "bad",
                "csrf_token": "bad",
                "nested": {"access_token": "bad", "safe": "kept"},
            },
        )
        with self.orm() as session:
            event = session.query(AuditEvent).filter_by(action="security.test").one()
            details = json.loads(event.details_json)
            self.assertNotIn("password", details)
            self.assertNotIn("csrf_token", details)
            self.assertEqual({"safe": "kept"}, details["nested"])

    def test_sqlite_pragmas_are_applied_to_every_connection(self):
        with self.database.engine.connect() as connection:
            self.assertEqual(1, connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
            self.assertEqual("wal", connection.exec_driver_sql("PRAGMA journal_mode").scalar_one())
            self.assertEqual(2000, connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one())


class LegacyUpgradeTests(unittest.TestCase):
    SCHEMA = """
        CREATE TABLE classes (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
        CREATE TABLE toilets (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, capacity INTEGER NOT NULL);
        CREATE TABLE class_toilets (class_id INTEGER, toilet_id INTEGER, PRIMARY KEY(class_id,toilet_id));
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, school_class_id INTEGER, is_admin BOOLEAN NOT NULL, password_hash TEXT);
        CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at DATETIME NOT NULL);
        CREATE TABLE requests (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, type TEXT NOT NULL, status TEXT NOT NULL, created_at DATETIME NOT NULL, activated_at DATETIME, toilet TEXT, completed_at DATETIME, manual_completion BOOLEAN NOT NULL);
    """

    def legacy_database(self, data_sql: str) -> Database:
        temp_dir = Path(tempfile.mkdtemp(prefix="toilet2-legacy-"))
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        db_path = temp_dir / "legacy.db"
        raw = sqlite3.connect(db_path)
        raw.executescript(self.SCHEMA + data_sql)
        raw.commit()
        raw.close()
        database = Database(f"sqlite:///{db_path.as_posix()}")
        self.addCleanup(database.dispose)
        return database

    def blank_database(self) -> Database:
        temp_dir = Path(tempfile.mkdtemp(prefix="toilet2-v1-"))
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))
        database = Database(f"sqlite:///{(temp_dir / 'v1.db').as_posix()}")
        self.addCleanup(database.dispose)
        return database

    def test_original_dev_schema_is_upgraded_without_losing_open_request(self):
        database = self.legacy_database(
            """
            INSERT INTO classes VALUES(1,'A');
            INSERT INTO toilets VALUES(1,'Old toilet',1);
            INSERT INTO class_toilets VALUES(1,1);
            INSERT INTO users VALUES(1,'student',1,0,NULL);
            INSERT INTO sessions VALUES('session',1,'2099-07-19 10:00:00');
            INSERT INTO requests VALUES(1,1,'toilet','active','2026-07-19 10:00:00','2026-07-19 10:00:01','Old toilet',NULL,0);
            """
        )
        database.initialize()
        database.initialize()  # The production startup path is idempotent.
        with database.SessionLocal() as session:
            school_class = session.query(SchoolClass).one()
            self.assertTrue(uuid.UUID(school_class.public_id))
            request = session.get(Request, 1)
            self.assertEqual([1], [lock.toilet_id for lock in request.toilet_locks])
            self.assertEqual(
                1, session.query(AuditEvent).filter_by(action="schema.upgraded").count()
            )
        service = MutationService(database)
        self.assertIsNone(service.get_session("session"))
        with database.SessionLocal() as session:
            upgrade = session.query(AuditEvent).filter_by(action="schema.upgraded").one()
            details = json.loads(upgrade.details_json)
            self.assertEqual(1, details["invalidated_nonproduction_sessions"])
            self.assertEqual(
                ["is_admin", "password_hash", "school_class_id"],
                details["removed_legacy_user_columns"],
            )
            self.assertEqual(
                ["class_toilets"], details["dropped_legacy_tables"]
            )
        with database.engine.connect() as connection:
            self.assertNotIn("class_toilets", {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            })
            user_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info('users')"
                )
            }
            self.assertNotIn("school_class_id", user_columns)
            self.assertNotIn("password_hash", user_columns)
            self.assertEqual(
                (1, "student", 1),
                connection.exec_driver_sql(
                    "SELECT id,username,enabled "
                    "FROM users WHERE id=1"
                ).one(),
            )
            self.assertEqual(
                [],
                connection.exec_driver_sql("PRAGMA foreign_key_check").all(),
            )

    def test_schema_v4_cleanup_preserves_current_student_data_and_relations(self):
        database = self.blank_database()
        database.initialize()
        service = MutationService(database, general_request_types=("paper",))
        student = service.sync_students(
            [{"id": 71, "userid": "preserved"}]
        )
        self.assertEqual(["preserved"], student.value["created"])
        user_id = service.get_student("preserved")["id"]
        request = service.create_request(
            user_id=user_id,
            request_type="paper",
            assignment={
                "lookup_ok": True,
                "computers": [],
                "classes": [],
                "anomalies": [],
            },
        ).value
        token = service.issue_session(
            subject_type="cms",
            subject="preserved",
            user_id=user_id,
            display_name="preserved",
            contest="contest",
        ).value["token"]

        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN school_class_id INTEGER"
            )
            connection.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN password_hash TEXT"
            )
            connection.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"
            )
            connection.exec_driver_sql(
                "UPDATE users SET school_class_id=99,password_hash='retired-hash' "
                "WHERE id=?",
                (user_id,),
            )
            connection.exec_driver_sql(
                "CREATE TABLE class_toilets("
                "class_id INTEGER,toilet_id INTEGER,"
                "PRIMARY KEY(class_id,toilet_id))"
            )
            connection.exec_driver_sql(
                "UPDATE schema_version SET version=4 WHERE id=1"
            )

        database.initialize()
        preserved = service.get_student("preserved")
        self.assertEqual(71, preserved["control_id"])
        self.assertTrue(preserved["enabled"])
        self.assertIsNotNone(service.get_session(token))
        with database.SessionLocal() as session:
            self.assertEqual(user_id, session.get(Request, request["id"]).user_id)
            upgrade = (
                session.query(AuditEvent)
                .filter_by(action="schema.upgraded")
                .one()
            )
            details = json.loads(upgrade.details_json)
            self.assertEqual(4, details["from_version"])
            self.assertEqual(
                ["is_admin", "password_hash", "school_class_id"],
                details["removed_legacy_user_columns"],
            )
            self.assertEqual(
                ["class_toilets"], details["dropped_legacy_tables"]
            )
        with database.engine.connect() as connection:
            self.assertEqual(
                {
                    "id",
                    "username",
                    "control_id",
                    "enabled",
                    "created_at",
                    "updated_at",
                },
                {
                    row[1]
                    for row in connection.exec_driver_sql(
                        "PRAGMA table_info('users')"
                    )
                },
            )
            self.assertFalse(
                connection.exec_driver_sql(
                    "SELECT EXISTS("
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='class_toilets')"
                ).scalar_one()
            )
            self.assertEqual(
                [],
                connection.exec_driver_sql("PRAGMA foreign_key_check").all(),
            )

    def test_current_schema_repairs_retired_display_name_column(self):
        database = self.blank_database()
        database.initialize()
        service = MutationService(database)
        service.sync_students([{"id": 71, "userid": "preserved"}])

        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN "
                "display_name VARCHAR NOT NULL DEFAULT ''"
            )
            connection.exec_driver_sql(
                "UPDATE users SET display_name='Retired name'"
            )
            self.assertEqual(
                SCHEMA_VERSION,
                connection.exec_driver_sql(
                    "SELECT version FROM schema_version WHERE id=1"
                ).scalar_one(),
            )

        database.initialize()
        database.initialize()

        self.assertEqual(71, service.get_student("preserved")["control_id"])
        self.assertEqual(
            ["new-student"],
            service.sync_students(
                [
                    {"id": 71, "userid": "preserved"},
                    {"id": 72, "userid": "new-student"},
                ]
            ).value["created"],
        )
        with database.engine.connect() as connection:
            self.assertNotIn(
                "display_name",
                {
                    row[1]
                    for row in connection.exec_driver_sql(
                        "PRAGMA table_info('users')"
                    )
                },
            )
            self.assertEqual(
                [],
                connection.exec_driver_sql(
                    "PRAGMA foreign_key_check"
                ).all(),
            )
        with database.SessionLocal() as session:
            repairs = (
                session.query(AuditEvent)
                .filter_by(action="schema.upgraded")
                .all()
            )
            self.assertEqual(1, len(repairs))
            details = json.loads(repairs[0].details_json)
            self.assertTrue(details["structural_repair"])
            self.assertEqual(
                ["display_name"],
                details["removed_legacy_user_columns"],
            )

    def test_legacy_active_actual_toilet_wins_over_changed_class_mapping(self):
        database = self.legacy_database(
            """
            INSERT INTO classes VALUES(1,'A');
            INSERT INTO toilets VALUES(1,'Actual toilet',1);
            INSERT INTO toilets VALUES(2,'New mapping',1);
            INSERT INTO class_toilets VALUES(1,2);
            INSERT INTO users VALUES(1,'student',1,0,NULL);
            INSERT INTO requests VALUES(1,1,'toilet','active','2026-07-19 10:00:00','2026-07-19 10:00:01','Actual toilet',NULL,0);
            """
        )
        database.initialize()
        with database.SessionLocal() as session:
            request = session.get(Request, 1)
            self.assertEqual([1], [lock.toilet_id for lock in request.toilet_locks])

    def test_legacy_missing_toilet_mapping_is_fallback_and_alerted(self):
        database = self.legacy_database(
            """
            INSERT INTO classes VALUES(1,'A');
            INSERT INTO toilets VALUES(1,'Only configured toilet',1);
            INSERT INTO users VALUES(1,'student',1,0,NULL);
            INSERT INTO requests VALUES(1,1,'toilet','pending','2026-07-19 10:00:00',NULL,'Missing toilet',NULL,0);
            """
        )
        database.initialize()
        with database.SessionLocal() as session:
            request = session.get(Request, 1)
            self.assertEqual("fallback_all", request.routing_mode)
            self.assertEqual([1], [lock.toilet_id for lock in request.toilet_locks])
            self.assertEqual(
                ["class_without_toilet"],
                [alert.code for alert in request.alerts if alert.resolved_at is None],
            )

    def test_interrupted_upgrade_is_retryable_and_populates_relations_once(self):
        database = self.legacy_database(
            """
            INSERT INTO classes VALUES(1,'A');
            INSERT INTO toilets VALUES(1,'Toilet',1);
            INSERT INTO class_toilets VALUES(1,1);
            INSERT INTO users VALUES(1,'student',1,0,NULL);
            INSERT INTO requests VALUES(1,1,'toilet','pending','2026-07-19 10:00:00',NULL,NULL,NULL,0);
            """
        )
        with patch("toilet2.migrations._populate_legacy_relations", side_effect=RuntimeError("injected")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                database.initialize()
        with database.engine.connect() as connection:
            self.assertIsNone(
                connection.exec_driver_sql(
                    "SELECT version FROM schema_version WHERE id=1"
                ).scalar_one_or_none()
            )
        database.initialize()
        with database.SessionLocal() as session:
            self.assertEqual(1, session.query(RequestToiletLock).count())
            self.assertEqual(
                1, session.query(AuditEvent).filter_by(action="schema.upgraded").count()
            )

    def test_upgraded_class_names_are_nonunique_and_transformations_are_audited(self):
        database = self.legacy_database(
            """
            INSERT INTO classes VALUES(1,'Old');
            INSERT INTO toilets VALUES(1,'First',1);
            INSERT INTO toilets VALUES(2,'Second',1);
            INSERT INTO class_toilets VALUES(1,1);
            INSERT INTO class_toilets VALUES(1,2);
            INSERT INTO users VALUES(1,'student',1,0,NULL);
            INSERT INTO requests VALUES(1,1,'paper','pending','2026-07-19 10:00:00',NULL,NULL,NULL,0);
            INSERT INTO requests VALUES(2,1,'paper','pending','2026-07-19 10:00:01',NULL,NULL,NULL,0);
            """
        )
        database.initialize()
        service = MutationService(database)
        service.sync_class_catalog(
            [
                {"id": str(uuid.uuid4()), "name": "Same", "sequence_num": 1},
                {"id": str(uuid.uuid4()), "name": "Same", "sequence_num": 2},
            ],
            actor=ADMIN,
        )
        with database.SessionLocal() as session:
            self.assertEqual(2, session.query(SchoolClass).filter_by(name="Same").count())
            event = session.query(AuditEvent).filter_by(action="schema.upgraded").one()
            details = json.loads(event.details_json)
            self.assertEqual(2, details["collapsed_class_mappings"][0]["mapping_count"])
            self.assertEqual([2], details["cancelled_duplicate_request_ids"])

    def test_schema_v1_repairs_class_constraint_and_fabricated_legacy_lock(self):
        database = self.blank_database()
        database.initialize(upgrade=False)
        service = MutationService(database)
        class_id = str(uuid.uuid4())
        service.sync_class_catalog(
            [{"id": class_id, "name": "Old", "sequence_num": 1}], actor=ADMIN
        )
        actual = service.create_toilet(name="Actual", actor=ADMIN).value["id"]
        mapped = service.create_toilet(name="Mapped", actor=ADMIN).value["id"]
        service.assign_class_toilet(
            class_public_id=class_id, toilet_id=actual, actor=ADMIN
        )
        user_id = service.ensure_student("student").value["id"]
        request = service.create_request(
            user_id=user_id,
            request_type="toilet",
            assignment={
                "lookup_ok": True,
                "computers": [
                    {
                        "machine_id": "pc",
                        "name": "PC",
                        "class": {"id": class_id, "name": "Old"},
                    }
                ],
                "classes": [{"id": class_id, "name": "Old"}],
                "anomalies": [],
            },
        ).value
        fallback_user = service.ensure_student("fallback-student").value["id"]
        fallback_request = service.create_request(
            user_id=fallback_user,
            request_type="toilet",
            assignment={"lookup_ok": True, "computers": [], "classes": [], "anomalies": []},
        ).value

        raw = database.engine.raw_connection()
        try:
            cursor = raw.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "UPDATE request_toilet_locks SET reason_json=? "
                "WHERE request_id=? AND toilet_id=?",
                (json.dumps({"source": "legacy_name"}, sort_keys=True), request["id"], actual),
            )
            cursor.execute(
                "INSERT INTO request_toilet_locks(request_id,toilet_id,reason_json,created_at) "
                "VALUES(?,?,?,?)",
                (
                    request["id"],
                    mapped,
                    json.dumps({"source": "legacy_class_mapping"}, sort_keys=True),
                    "2026-07-19 10:00:00",
                ),
            )
            cursor.execute(
                "UPDATE requests SET routing_mode='normal' WHERE id=?",
                (fallback_request["id"],),
            )
            cursor.execute(
                "UPDATE request_toilet_locks SET reason_json=? WHERE request_id=?",
                (
                    json.dumps({"source": "legacy_fallback_all"}, sort_keys=True),
                    fallback_request["id"],
                ),
            )
            cursor.execute(
                "DELETE FROM operational_alerts WHERE request_id=?",
                (fallback_request["id"],),
            )
            cursor.execute(
                "CREATE TABLE classes_v1 ("
                "id INTEGER PRIMARY KEY, public_id VARCHAR(36) NOT NULL UNIQUE, "
                "name VARCHAR NOT NULL UNIQUE, sequence_num INTEGER NOT NULL, "
                "toilet_id INTEGER REFERENCES toilets(id) ON DELETE SET NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
            cursor.execute(
                "INSERT INTO classes_v1 SELECT id,public_id,name,sequence_num,toilet_id,created_at,updated_at FROM classes"
            )
            cursor.execute("DROP TABLE classes")
            cursor.execute("ALTER TABLE classes_v1 RENAME TO classes")
            cursor.execute(
                "CREATE TABLE schema_version (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)"
            )
            cursor.execute("INSERT INTO schema_version VALUES(1,1)")
            raw.commit()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
        finally:
            raw.close()

        database.initialize()
        service.sync_class_catalog(
            [
                {"id": str(uuid.uuid4()), "name": "Same", "sequence_num": 1},
                {"id": str(uuid.uuid4()), "name": "Same", "sequence_num": 2},
            ],
            actor=ADMIN,
        )
        with database.SessionLocal() as session:
            repaired = session.get(Request, request["id"])
            self.assertEqual([actual], [lock.toilet_id for lock in repaired.toilet_locks])
            repaired_fallback = session.get(Request, fallback_request["id"])
            self.assertEqual("fallback_all", repaired_fallback.routing_mode)
            self.assertEqual(
                ["no_class"],
                [alert.code for alert in repaired_fallback.alerts if alert.resolved_at is None],
            )
            self.assertEqual(2, session.query(SchoolClass).filter_by(name="Same").count())
            upgrade = session.query(AuditEvent).filter_by(action="schema.upgraded").one()
            upgrade_details = json.loads(upgrade.details_json)
            self.assertEqual(1, upgrade_details["repaired_legacy_lock_rows"])
            self.assertEqual(1, upgrade_details["opened_legacy_fallback_alerts"])


if __name__ == "__main__":
    unittest.main()
