from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import asyncio
import json
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect as sa_inspect
from starlette.websockets import WebSocketDisconnect

from toilet2.cms_auth import CMSAuthResult, CMSAuthStatus
from toilet2.control_client import (
    ClassLayout,
    ClassInfo,
    ControlAPIUnavailable,
    StudentInfo,
)
from toilet2.database import Database
from toilet2.main import (
    _cms_login_path,
    _hold_websocket_until_deadline,
    _parse_completion,
    create_app,
)
from toilet2.models import AuditEvent, OperatorAccount, SchoolClass
from toilet2.service import Actor
from toilet2.settings import Settings
import toilet2.main as main_module


CLASS_A = str(uuid.UUID("10000000-0000-0000-0000-000000000001"))
CLASS_B = str(uuid.UUID("10000000-0000-0000-0000-000000000002"))


class FakeCMS:
    def __init__(self, result=None):
        self.result = result or CMSAuthResult(CMSAuthStatus.UNAUTHENTICATED)
        self.calls = []

    async def authenticate(self, cookies, client_ip):
        self.calls.append((dict(cookies), client_ip))
        return self.result

    async def aclose(self):
        pass


class FakeControl:
    def __init__(self):
        self.class_items = (ClassInfo(CLASS_A, "A", 1), ClassInfo(CLASS_B, "B", 2))
        self.student_items = (StudentInfo(1, "alice"),)
        self.assignment = {
            "found": True,
            "userid": "alice",
            "student": {"id": 1, "userid": "alice"},
            "computers": [
                {
                    "machine_id": "machine-a",
                    "name": "PC A",
                    "class": {"id": CLASS_A, "name": "A"},
                }
            ],
            "classes": [{"id": CLASS_A, "name": "A"}],
            "anomalies": [],
        }
        self.assignment_calls = []
        self.class_error = None
        self.student_error = None

    async def classes(self):
        if self.class_error is not None:
            raise self.class_error
        return self.class_items

    async def students(self):
        if self.student_error is not None:
            raise self.student_error
        return self.student_items

    async def class_layout(self, class_id):
        class_info = next(item for item in self.class_items if item.id == class_id)
        student = None
        if self.assignment["classes"] and self.assignment["classes"][0]["id"] == class_id:
            student = {
                "id": 1,
                "userid": self.assignment.get("userid")
                or self.assignment.get("cms_username"),
            }
        return ClassLayout(
            class_info,
            (
                {
                    "machine_id": f"machine-{class_id}",
                    "name": f"PC {class_info.name}",
                    "sequence_num": 1,
                    "grid_row": 1,
                    "grid_col": 1,
                    "class": None,
                    "student": student,
                },
            ),
        )

    async def student_assignment(self, username):
        self.assignment_calls.append(username)
        value = dict(self.assignment)
        value["userid"] = username
        value["student"] = {
            "id": 1,
            "userid": username,
        }
        return value

    async def aclose(self):
        pass


@contextmanager
def app_client(tmp_path, *, settings=None, cms=None, control=None, student_ui=True):
    path = Path(tmp_path) / "app.db"
    database = Database("sqlite:///" + path.as_posix())
    base = (
        settings
        if settings is not None
        else Settings(
            public_origin="http://testserver",
            cms_contests=("contest",),
            control_auth_key="test-key",
        )
    )
    # The contestant surface stays implemented and covered here even though
    # deployments must opt in with TOILET_STUDENT_UI_ENABLED. Tests that assert
    # the disabled staff-only behaviour pass ``student_ui=False``.
    config = replace(
        base, database_url=database.url, student_ui_enabled=student_ui
    )
    app = create_app(
        config,
        database=database,
        cms_authenticator=cms or FakeCMS(),
        control_client=control or FakeControl(),
    )
    try:
        with TestClient(app) as client:
            yield app, client
    finally:
        database.dispose()


def settings_with_paper(**changes):
    """Opt in to the paper support request, which is disabled by default."""

    return Settings(
        public_origin="http://testserver",
        cms_contests=("contest",),
        control_auth_key="test-key",
        general_request_types=("paper",),
        **changes,
    )


def seed_route(app):
    service = app.state.mutations
    service.sync_class_catalog(
        [{"id": CLASS_A, "name": "A", "sequence_num": 1}],
        actor=Actor.system(),
    )
    toilet = service.create_toilet(name="North", capacity=1, actor=Actor.system())
    service.assign_class_toilet(
        class_public_id=CLASS_A,
        toilet_id=toilet.value["id"],
        actor=Actor.system(),
    )
    return toilet.value["id"]


def seed_two_class_routes(app):
    service = app.state.mutations
    service.sync_class_catalog(
        [
            {"id": CLASS_A, "name": "A", "sequence_num": 1},
            {"id": CLASS_B, "name": "B", "sequence_num": 2},
        ],
        actor=Actor.system(),
    )
    toilet_a = service.create_toilet(name="North", capacity=1, actor=Actor.system())
    toilet_b = service.create_toilet(name="South", capacity=1, actor=Actor.system())
    service.assign_class_toilet(
        class_public_id=CLASS_A,
        toilet_id=toilet_a.value["id"],
        actor=Actor.system(),
    )
    service.assign_class_toilet(
        class_public_id=CLASS_B,
        toilet_id=toilet_b.value["id"],
        actor=Actor.system(),
    )


def login_student(client, app, username="alice", class_ids=(CLASS_A,)):
    assert len(class_ids) <= 1, "contestants can have at most one computer assignment"
    app.state.cms_auth.result = CMSAuthResult(
        CMSAuthStatus.AUTHENTICATED,
        username=username,
        contest="contest",
    )
    names = {CLASS_A: "A", CLASS_B: "B"}
    control_id = uuid.uuid5(uuid.NAMESPACE_DNS, username).int % 2_000_000_000 + 1
    app.state.mutations.sync_students(
        [{"id": control_id, "userid": username}],
        actor=Actor.system("test-student-sync"),
    )
    classes = [{"id": class_id, "name": names[class_id]} for class_id in class_ids]
    app.state.control.assignment = {
        "found": True,
        "userid": username,
        "student": {
            "id": control_id,
            "userid": username,
        },
        "computers": [
            {
                "machine_id": f"machine-{class_id}",
                "name": f"PC {names[class_id]}",
                "class": {"id": class_id, "name": names[class_id]},
            }
            for class_id in class_ids
        ],
        "classes": classes,
        "anomalies": [] if class_ids else [{"code": "no_computers"}],
    }
    response = client.get("/")
    assert response.status_code == 200
    token = client.cookies["toilet_session"]
    session = app.state.mutations.get_session(token)
    assert session is not None
    return session


def login_operator(client, app, role="admin"):
    app.state.mutations.upsert_operator(
        username=f"test-{role}",
        display_name=f"Test {role}",
        password="correct-password",
        roles={role},
        all_classes=role == "proctor",
        actor=Actor.system("test"),
    )
    response = client.post(
        "/operator/login",
        data={"username": f"test-{role}", "password": "correct-password"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session = app.state.mutations.get_session(client.cookies["toilet_session"])
    assert session is not None
    return session


def login_control_operator(
    client,
    app,
    username,
    *,
    roles=("admin",),
    class_scope=(),
    all_classes=False,
    display_name=None,
):
    app.state.mutations.upsert_operator(
        username=username,
        display_name=display_name or username,
        password="correct-password",
        roles=roles,
        class_scope=class_scope,
        all_classes=all_classes,
        actor=Actor.system("test"),
    )
    response = client.post(
        "/operator/login",
        data={"username": username, "password": "correct-password"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session = app.state.mutations.get_session(client.cookies["toilet_session"])
    assert session is not None
    return response, session


def audit_count(app):
    with app.state.database.SessionLocal() as session:
        return session.query(AuditEvent).count()


def test_student_csrf_proctor_queue_return_and_admin_audit(tmp_path):
    with app_client(tmp_path) as (app, client):
        seed_route(app)
        student = login_student(client, app)
        session_api = client.get("/api/session")
        assert session_api.status_code == 200
        assert session_api.json() == {
            "subject_type": "cms",
            "subject": "alice",
            "user_id": student["user_id"],
            "roles": [],
            "class_scope": [],
            "all_classes": False,
            "contest": "contest",
            "locale": "en",
            "csrf_token": student["csrf_token"],
        }

        denied = client.post("/api/requests", data={"type": "toilet"})
        assert denied.status_code == 403
        created = client.post(
            "/api/requests",
            data={"type": "toilet"},
            headers={"X-CSRF-Token": student["csrf_token"]},
        )
        assert created.status_code == 200
        assert app.state.control.assignment_calls == ["alice"]
        assert created.json()["status"] == "active"
        request_id = created.json()["id"]
        state = client.get("/api/state").json()
        assert state["requests"][0]["toilets"][0]["name"] == "North"

        client.cookies.clear()
        admin = login_operator(client, app)
        operator_session = client.get("/api/session").json()
        assert operator_session["subject_type"] == "operator"
        assert operator_session["roles"] == ["admin"]
        assert operator_session["csrf_token"] == admin["csrf_token"]
        admin_state = client.get("/api/admin/state")
        assert admin_state.status_code == 200
        assert set(admin_state.json()) == {"config"}
        assert "queue" not in admin_state.json()
        assert client.get("/proctor").status_code == 403
        assert client.get("/api/proctor/state").status_code == 403
        assert client.post(
            f"/api/proctor/return/{request_id}",
            headers={"X-CSRF-Token": admin["csrf_token"]},
        ).status_code == 403

        client.cookies.clear()
        proctor = login_operator(client, app, "proctor")
        proctor_state = client.get("/api/proctor/state")
        assert proctor_state.status_code == 200
        state = proctor_state.json()
        assert state["all_classes"] is True
        assert state["classes"] == [
            {
                "id": CLASS_A,
                "name": "A",
                "sequence_num": 1,
                "grid_cols": None,
            },
            {
                "id": CLASS_B,
                "name": "B",
                "sequence_num": 2,
                "grid_cols": None,
            },
        ]
        assert state["queue"][0]["id"] == request_id
        assert client.post(f"/api/proctor/return/{request_id}").status_code == 403
        returned = client.post(
            f"/api/proctor/return/{request_id}",
            headers={"X-CSRF-Token": proctor["csrf_token"]},
        )
        assert returned.status_code == 200

        client.cookies.clear()
        login_operator(client, app)
        actions = {
            event["action"]
            for event in client.get("/api/admin/audit?view=all").json()["events"]
        }
        assert {"request.created", "request.activated", "request.toilet_returned"} <= actions


def test_proctor_scope_and_support_resolution(tmp_path):
    with app_client(tmp_path, settings=settings_with_paper()) as (app, client):
        seed_route(app)
        student = login_student(client, app)
        created = client.post(
            "/api/requests",
            data={"type": "paper"},
            headers={"X-CSRF-Token": student["csrf_token"]},
        ).json()
        client.cookies.clear()
        proctor = login_operator(client, app, "proctor")
        state = client.get("/api/proctor/state").json()
        assert [item["id"] for item in state["support"]] == [created["id"]]
        response = client.post(
            f"/api/proctor/resolve/{created['id']}",
            headers={"X-CSRF-Token": proctor["csrf_token"]},
        )
        assert response.status_code == 200
        assert client.get("/api/proctor/state").json()["support"] == []


def test_operator_login_uses_toilet_local_roles(tmp_path):
    control = FakeControl()
    with app_client(tmp_path, control=control) as (app, client):
        response, admin = login_control_operator(
            client,
            app,
            "operator",
            roles=("admin",),
            display_name="Configuration administrator",
        )
        assert response.headers["location"] == "/admin"
        admin_state = client.get("/api/admin/state")
        assert admin_state.status_code == 200
        assert set(admin_state.json()) == {"config"}
        assert client.get("/proctor").status_code == 403
        assert client.get("/proctor-all").status_code == 403
        assert client.get("/api/proctor/state").status_code == 403
        assert client.post(
            "/api/proctor/return/1",
            headers={"X-CSRF-Token": admin["csrf_token"]},
        ).status_code == 403
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(
                "/ws/staff", headers={"origin": "http://testserver"}
            ) as websocket:
                websocket.receive_json()
        assert closed.value.code == 4403


def test_legacy_unsafe_operator_username_remains_deletable(tmp_path):
    with app_client(tmp_path) as (app, client):
        admin = login_operator(client, app)
        app.state.mutations.upsert_operator(
            username="delete",
            display_name="Ordinary delete user",
            password="another-secure-password",
            roles={"proctor"},
            all_classes=True,
            actor=Actor.system("test"),
        )
        updated = client.post(
            "/api/admin/operators/delete",
            data={
                "display_name": "Still an ordinary user",
                "password": "",
                "roles": "proctor",
                "all_classes": "true",
                "enabled": "true",
            },
            headers={"X-CSRF-Token": admin["csrf_token"]},
        )
        assert updated.status_code == 200
        assert updated.json()["username"] == "delete"
        assert updated.json()["display_name"] == "Still an ordinary user"

        with app.state.database.immediate_session() as session:
            session.add(
                OperatorAccount(
                    username="legacy/operator",
                    display_name="Legacy operator",
                    password_hash="legacy-disabled-hash",
                    roles_json='["proctor"]',
                    class_scope_json="[]",
                    all_classes=True,
                    enabled=False,
                )
            )
            session.commit()

        operators = client.get("/api/admin/state").json()["config"]["operators"]
        legacy = next(
            item for item in operators if item["username"] == "legacy/operator"
        )
        assert legacy["username_safe"] is False

        deleted = client.post(
            "/api/admin/operator-deletion",
            data={"username": "legacy/operator"},
            headers={"X-CSRF-Token": admin["csrf_token"]},
        )
        assert deleted.status_code == 200
        with app.state.database.SessionLocal() as session:
            assert session.get(OperatorAccount, "legacy/operator") is None


def test_scoped_and_all_class_proctor_views_use_local_scope(tmp_path):
    control = FakeControl()
    with app_client(tmp_path, control=control) as (app, client):
        seed_two_class_routes(app)

        student_a = login_student(client, app, username="student-a", class_ids=(CLASS_A,))
        request_a = client.post(
            "/api/requests",
            data={"type": "toilet"},
            headers={"X-CSRF-Token": student_a["csrf_token"]},
        ).json()
        client.cookies.clear()
        student_b = login_student(client, app, username="student-b", class_ids=(CLASS_B,))
        request_b = client.post(
            "/api/requests",
            data={"type": "toilet"},
            headers={"X-CSRF-Token": student_b["csrf_token"]},
        ).json()

        client.cookies.clear()
        scoped_login, scoped = login_control_operator(
            client,
            app,
            "class-a-proctor",
            roles=("proctor",),
            class_scope=(CLASS_A,),
            display_name="Class A proctor",
        )
        assert scoped_login.headers["location"] == "/proctor"
        assert client.get("/proctor").status_code == 200
        assert client.get("/proctor-all").status_code == 403
        scoped_state = client.get("/api/proctor/state").json()
        assert scoped_state["all_classes"] is False
        assert scoped_state["classes"] == [
            {
                "id": CLASS_A,
                "name": "A",
                "sequence_num": 1,
                "grid_cols": None,
            }
        ]
        assert [item["id"] for item in scoped_state["queue"]] == [request_a["id"]]
        denied = client.post(
            f"/api/proctor/return/{request_b['id']}",
            headers={"X-CSRF-Token": scoped["csrf_token"]},
        )
        assert denied.status_code == 403

        client.cookies.clear()
        all_login, _all_proctor = login_control_operator(
            client,
            app,
            "hall-proctor",
            roles=("proctor",),
            all_classes=True,
            display_name="Hall proctor",
        )
        assert all_login.headers["location"] == "/proctor-all"
        assert client.get("/proctor-all").status_code == 200
        all_state = client.get("/api/proctor/state").json()
        assert all_state["all_classes"] is True
        assert [item["id"] for item in all_state["classes"]] == [CLASS_A, CLASS_B]
        assert {item["id"] for item in all_state["queue"]} == {
            request_a["id"],
            request_b["id"],
        }


def test_admin_audit_summary_hides_routine_events_but_all_is_forensic(tmp_path):
    with app_client(tmp_path) as (app, client):
        seed_route(app)
        student = login_student(client, app)
        created = client.post(
            "/api/requests",
            data={"type": "toilet"},
            headers={"X-CSRF-Token": student["csrf_token"]},
        )
        assert created.status_code == 200

        with app.state.database.SessionLocal() as session:
            historical = AuditEvent(
                actor_kind="system",
                actor_identifier="old-catalog-loop",
                action="class.catalog_synced",
                target_type="class_catalog",
                target_identifier="unversioned",
                correlation_id=str(uuid.uuid4()),
                details_json=json.dumps(
                    {
                        "created": [],
                        "renamed": [],
                        "reordered": [],
                        "deleted": [],
                        "revision_changed": False,
                    },
                    sort_keys=True,
                ),
            )
            session.add(historical)
            session.commit()
            historical_id = historical.id

        client.cookies.clear()
        login_operator(client, app, "admin")
        summary = client.get("/api/admin/audit").json()
        assert summary["view"] == "summary"
        assert historical_id not in {event["id"] for event in summary["events"]}
        assert "toilet.created" in {event["action"] for event in summary["events"]}
        assert all(
            not event["action"].startswith(("request.", "session."))
            and event["action"] != "user.created"
            for event in summary["events"]
        )

        all_events = client.get("/api/admin/audit?view=all").json()
        assert all_events["view"] == "all"
        actions = {event["action"] for event in all_events["events"]}
        assert {"request.created", "session.issued"} <= actions
        assert historical_id in {event["id"] for event in all_events["events"]}


def test_student_proctor_and_admin_reads_do_not_append_audit_events(tmp_path):
    with app_client(tmp_path) as (app, client):
        seed_route(app)

        login_student(client, app)
        before_student_reads = audit_count(app)
        for _ in range(3):
            assert client.get("/").status_code == 200
            assert client.get("/api/state").status_code == 200
        for _ in range(2):
            with client.websocket_connect(
                "/ws/student", headers={"origin": "http://testserver"}
            ) as websocket:
                assert websocket.receive_json() == {
                    "locale": "en",
                    "request_types": {},
                    "requests": [],
                }
        assert audit_count(app) == before_student_reads

        client.cookies.clear()
        login_operator(client, app, "proctor")
        before_proctor_reads = audit_count(app)
        for _ in range(3):
            assert client.get("/proctor-all").status_code == 200
            assert client.get("/api/proctor/state").status_code == 200
        with client.websocket_connect(
            "/ws/staff", headers={"origin": "http://testserver"}
        ) as websocket:
            assert websocket.receive_json()["queue"] == []
        assert audit_count(app) == before_proctor_reads

        client.cookies.clear()
        login_operator(client, app, "admin")
        before_admin_reads = audit_count(app)
        for _ in range(3):
            assert client.get("/admin").status_code == 200
            assert client.get("/api/admin/state").status_code == 200
            assert client.get("/api/admin/audit").status_code == 200
            assert client.get("/api/admin/audit?view=all").status_code == 200
        assert audit_count(app) == before_admin_reads


def test_one_hundred_cms_student_polls_reuse_session_without_audit_growth(tmp_path):
    cms = FakeCMS(
        CMSAuthResult(
            CMSAuthStatus.AUTHENTICATED,
            username="alice",
            contest="contest",
        )
    )
    settings = Settings(
        public_origin="http://testserver",
        cms_contests=("contest",),
        control_auth_key="test-key",
    )
    with app_client(tmp_path, settings=settings, cms=cms) as (app, client):
        assert client.get("/").status_code == 200
        original_token = client.cookies["toilet_session"]
        before_reads = audit_count(app)

        for _ in range(100):
            assert client.get("/api/state").status_code == 200
        for _ in range(3):
            with client.websocket_connect(
                "/ws/student", headers={"origin": "http://testserver"}
            ) as websocket:
                assert websocket.receive_json() == {
                    "locale": "en",
                    "request_types": {},
                    "requests": [],
                }

        assert client.cookies["toilet_session"] == original_token
        assert audit_count(app) == before_reads


def test_cms_http_identity_cookie_relay_assignment_and_no_dev_fallback(tmp_path):
    cms = FakeCMS(
        CMSAuthResult(
            CMSAuthStatus.AUTHENTICATED,
            username="alice",
            contest="contest",
            set_cookie_headers=("contest_login=refreshed; Path=/",),
        )
    )
    control = FakeControl()
    settings = Settings(
        public_origin="http://testserver",
        cms_contests=("contest",),
        control_auth_key="test-key",
    )
    with app_client(tmp_path, settings=settings, cms=cms, control=control) as (app, client):
        seed_route(app)
        page = client.get("/", follow_redirects=False)
        assert page.status_code == 200
        set_cookies = page.headers.get_list("set-cookie")
        assert any(value.startswith("contest_login=refreshed") for value in set_cookies)
        assert any(value.startswith("toilet_session=") for value in set_cookies)
        local = app.state.mutations.get_session(client.cookies["toilet_session"])
        created = client.post(
            "/api/requests",
            data={"type": "toilet"},
            headers={"X-CSRF-Token": local["csrf_token"]},
        )
        assert created.status_code == 200
        assert created.json()["classes"] == [{"public_id": CLASS_A, "name": "A"}]

        cms.result = CMSAuthResult(CMSAuthStatus.UNAUTHENTICATED)
        assert client.get("/api/state").status_code == 401


def test_cms_outage_is_503(tmp_path):
    cms = FakeCMS(CMSAuthResult(CMSAuthStatus.UNAVAILABLE))
    settings = Settings(
        public_origin="http://testserver",
        cms_contests=("contest",),
        control_auth_key="test-key",
    )
    with app_client(tmp_path, settings=settings, cms=cms) as (_app, client):
        assert client.get("/api/state").status_code == 503


def test_cms_logout_revokes_companion_and_clears_all_cms_login_cookies(tmp_path):
    cms = FakeCMS(
        CMSAuthResult(
            CMSAuthStatus.AUTHENTICATED, username="alice", contest="first"
        )
    )
    settings = Settings(
        public_origin="http://testserver",
        cms_contests=("first", "second"),
        cms_multi_contest=True,
        control_auth_key="test-key",
    )
    with app_client(tmp_path, settings=settings, cms=cms) as (app, client):
        assert client.get("/").status_code == 200
        local = app.state.mutations.get_session(client.cookies["toilet_session"])
        response = client.post(
            "/logout",
            data={"_csrf": local["csrf_token"]},
            follow_redirects=False,
        )
        assert response.status_code == 303
        cookies = response.headers.get_list("set-cookie")
        assert any(value.startswith("first_login=") and "Max-Age=0" in value for value in cookies)
        assert any(value.startswith("second_login=") and "Max-Age=0" in value for value in cookies)
        assert app.state.mutations.get_session(local["token"]) is None


def test_student_and_staff_websocket_initial_state(tmp_path):
    with app_client(tmp_path) as (app, client):
        seed_route(app)
        login_student(client, app)
        with pytest.raises(WebSocketDisconnect) as missing_origin:
            with client.websocket_connect("/ws/student") as websocket:
                websocket.receive_json()
        assert missing_origin.value.code == 4403
        with client.websocket_connect(
            "/ws/student", headers={"origin": "http://testserver"}
        ) as websocket:
            assert websocket.receive_json() == {
                "locale": "en",
                "request_types": {},
                "requests": [],
            }
        client.cookies.clear()
        login_operator(client, app, "proctor")
        with client.websocket_connect(
            "/ws/staff", headers={"origin": "http://testserver"}
        ) as websocket:
            initial = websocket.receive_json()
            assert initial["support"] == []
            assert initial["alerts"] == []


def test_cms_login_redirect_targets_form_root_and_preserves_toilet_destination():
    single = Settings(
        public_origin="http://testserver",
        cms_contests=("contest",),
        control_auth_key="test-key",
        app_root_path="/toilet",
    )
    assert _cms_login_path(single) == "/?next=/toilet/"
    multi = Settings(
        public_origin="http://testserver",
        cms_contests=("contest name",),
        cms_multi_contest=True,
        control_auth_key="test-key",
        app_root_path="/toilet",
    )
    assert _cms_login_path(multi) == "/contest%20name/?next=/toilet/"


def test_root_path_redirect_and_sensitive_responses_are_not_cached(tmp_path):
    settings = Settings(
        public_origin="http://testserver",
        cms_contests=("contest",),
        control_auth_key="test-key",
        app_root_path="/toilet",
    )
    with app_client(tmp_path, settings=settings) as (_app, client):
        response = client.get("/", follow_redirects=False)
        assert response.headers["location"] == "/?next=/toilet/"
        assert response.headers["cache-control"] == "no-store"


def test_retired_student_and_operator_shortcuts_are_unavailable(tmp_path):
    control = FakeControl()
    with app_client(tmp_path, control=control) as (_app, client):
        assert client.post("/login", data={"username": "invented"}).status_code == 405
        response = client.post(
            "/operator/login",
            data={"dev_role": "admin"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert response.status_code == 401


def test_proctor_alert_identifies_student_classes_and_locked_toilets(tmp_path):
    with app_client(tmp_path) as (app, client):
        seed_route(app)
        student = login_student(client, app, username="unmapped", class_ids=())
        created = client.post(
            "/api/requests",
            data={"type": "toilet"},
            headers={"X-CSRF-Token": student["csrf_token"]},
        )
        assert created.status_code == 200
        client.cookies.clear()
        login_operator(client, app, "proctor")
        alert = client.get("/api/proctor/state").json()["alerts"][0]
        assert alert["username"] == "unmapped"
        assert alert["classes"] == []
        assert [item["name"] for item in alert["toilets"]] == ["North"]
        with client.websocket_connect(
            "/ws/staff", headers={"origin": "http://testserver"}
        ) as websocket:
            socket_alert = websocket.receive_json()["alerts"][0]
            assert socket_alert["username"] == "unmapped"
            assert isinstance(socket_alert["created_at"], str)


def test_staff_websocket_broadcast_serializes_new_mapping_alert(tmp_path):
    with app_client(tmp_path) as (app, client):
        seed_route(app)
        student = login_student(client, app, username="broadcast", class_ids=())
        student_token = client.cookies["toilet_session"]
        client.cookies.clear()
        login_operator(client, app, "proctor")
        with client.websocket_connect(
            "/ws/staff", headers={"origin": "http://testserver"}
        ) as websocket:
            assert websocket.receive_json()["alerts"] == []
            client.cookies.set("toilet_session", student_token)
            response = client.post(
                "/api/requests",
                data={"type": "toilet"},
                headers={"X-CSRF-Token": student["csrf_token"]},
            )
            assert response.status_code == 200
            update = websocket.receive_json()
            assert update["alerts"][0]["username"] == "broadcast"


def test_class_catalog_is_live_and_never_mirrored(tmp_path):
    control = FakeControl()
    with app_client(tmp_path, control=control) as (app, client):
        with app.state.database.SessionLocal() as session:
            assert session.query(SchoolClass).count() == 0
        response, admin = login_control_operator(client, app, "admin")
        assert response.status_code == 303
        control.class_items = (ClassInfo(CLASS_A, "A", 1),)

        # A stale local mapping is reported through reconciliation warnings,
        # never represented as though it were part of the live class catalog.
        stale_id = str(uuid.uuid4())
        toilet_id = app.state.mutations.create_toilet(
            name="North", actor=Actor.system("test")
        ).value["id"]
        app.state.mutations.assign_class_toilet(
            class_public_id=stale_id,
            toilet_id=toilet_id,
            actor=Actor.system("test"),
        )
        state = client.get("/api/admin/state").json()["config"]
        assert [item["id"] for item in state["classes"]] == [CLASS_A]
        assert stale_id not in {item["id"] for item in state["classes"]}

        page = client.get("/admin")
        assert page.status_code == 200
        assert 'id="class-status"' in page.text
        assert "/api/admin/classes/reload" in page.text
        assert "Legacy class mapping needs attention" in page.text

        reloaded = client.post(
            "/api/admin/classes/reload",
            headers={"X-CSRF-Token": admin["csrf_token"]},
        )
        assert reloaded.status_code == 200
        assert client.post(
            "/api/admin/classes/sync",
            headers={"X-CSRF-Token": admin["csrf_token"]},
        ).status_code == 404

        malformed_toilet = client.post(
            f"/api/admin/classes/{CLASS_A}/toilet",
            data={"toilet_id": "not-an-integer"},
            headers={"X-CSRF-Token": admin["csrf_token"]},
        )
        assert malformed_toilet.status_code == 400
        assert malformed_toilet.json()["detail"] == "invalid toilet id"
        for invalid_id in ("0", "-1"):
            invalid_toilet = client.post(
                f"/api/admin/classes/{CLASS_A}/toilet",
                data={"toilet_id": invalid_id},
                headers={"X-CSRF-Token": admin["csrf_token"]},
            )
            assert invalid_toilet.status_code == 400
            assert invalid_toilet.json()["detail"] == "invalid toilet id"
        with app.state.database.SessionLocal() as session:
            assert session.query(SchoolClass).filter_by(public_id=CLASS_A).count() == 0


def test_repeated_student_sync_failures_coalesce_and_recovery_is_audited(tmp_path):
    control = FakeControl()
    settings = Settings(
        public_origin="http://testserver",
        cms_contests=("contest",),
        control_auth_key="test-key",
    )
    with app_client(tmp_path, settings=settings, control=control) as (app, client):
        admin = app.state.mutations.issue_session(
            subject_type="operator",
            subject="admin",
            display_name="Administrator",
            roles={"admin"},
            all_classes=True,
            actor=Actor.system("test"),
        ).value
        client.cookies.set("toilet_session", admin["token"])

        control.student_error = ControlAPIUnavailable("temporarily down")
        for _ in range(2):
            response = client.post(
                "/api/admin/students/sync",
                headers={"X-CSRF-Token": admin["csrf_token"]},
            )
            assert response.status_code == 503

        with app.state.database.SessionLocal() as session:
            assert session.query(AuditEvent).filter_by(
                action="control.student_sync_failed"
            ).count() == 1
            assert session.query(AuditEvent).filter_by(
                action="control.student_sync_recovered"
            ).count() == 0

        control.student_error = None
        recovered = client.post(
            "/api/admin/students/sync",
            headers={"X-CSRF-Token": admin["csrf_token"]},
        )
        assert recovered.status_code == 200
        with app.state.database.SessionLocal() as session:
            assert session.query(AuditEvent).filter_by(
                action="control.student_sync_failed"
            ).count() == 1
            assert session.query(AuditEvent).filter_by(
                action="control.student_sync_recovered"
            ).count() == 1

        summary_actions = {
            event["action"]
            for event in client.get("/api/admin/audit").json()["events"]
        }
        assert {
            "control.student_sync_failed",
            "control.student_sync_recovered",
        } <= summary_actions


@pytest.mark.asyncio
async def test_websocket_deadline_is_absolute_even_with_client_messages():
    class BusySocket:
        calls = 0

        async def receive_text(self):
            self.calls += 1
            await asyncio.sleep(0)
            return "ping"

    socket = BusySocket()
    with pytest.raises(asyncio.TimeoutError):
        await _hold_websocket_until_deadline(socket, 0.01)
    assert socket.calls > 1


def test_manual_browser_time_with_offset_is_converted_to_utc():
    parsed = _parse_completion("2026-07-19T15:00:00+03:00")
    assert parsed == datetime(2026, 7, 19, 12, 0, 0)


def test_cms_operator_login_rejects_cross_origin_post(tmp_path):
    settings = Settings(
        public_origin="https://contest.example.org",
        cookie_secure=True,
        cms_contests=("contest",),
        control_auth_key="test-control-key-that-is-long-enough",
    )
    with app_client(tmp_path, settings=settings) as (app, client):
        app.state.mutations.upsert_operator(
            username="operator",
            display_name="Operator",
            password="correct-password",
            roles={"admin"},
            actor=Actor.system("test"),
        )
        missing = client.post(
            "/operator/login",
            data={"username": "operator", "password": "pw"},
            follow_redirects=False,
        )
        assert missing.status_code == 403
        denied = client.post(
            "/operator/login",
            data={"username": "operator", "password": "pw"},
            headers={"origin": "https://attacker.example.org"},
            follow_redirects=False,
        )
        assert denied.status_code == 403
        trailing_slash = client.post(
            "/operator/login",
            data={"username": "operator", "password": "pw"},
            headers={"origin": "https://contest.example.org/"},
            follow_redirects=False,
        )
        assert trailing_slash.status_code == 403
        allowed = client.post(
            "/operator/login",
            data={"username": "operator", "password": "correct-password"},
            headers={"origin": "https://contest.example.org"},
            follow_redirects=False,
        )
        assert allowed.status_code == 303


def test_module_exports_factory_without_constructing_an_app():
    assert callable(main_module.create_app)
    assert not hasattr(main_module, "app")


def test_locale_prefers_query_then_cms_cookie_then_persisted_override(tmp_path):
    with app_client(tmp_path) as (app, client):
        app.state.mutations.sync_students(
            [{"id": 1, "userid": "alice"}],
            actor=Actor.system("test"),
        )
        app.state.cms_auth.result = CMSAuthResult(
            CMSAuthStatus.AUTHENTICATED,
            username="alice",
            contest="contest",
        )
        client.cookies.set("language", "lt_LT")
        assert client.get("/api/session").json()["locale"] == "lt"
        explicit = client.get("/api/session?lang=en")
        assert explicit.json()["locale"] == "en"
        assert client.cookies["toilet_locale"] == "en"
        # A CMS language cookie has precedence over the persisted query choice.
        assert client.get("/api/session").json()["locale"] == "lt"
        client.cookies.delete("language")
        assert client.get("/api/session").json()["locale"] == "en"
        client.cookies.set(
            "toilet_locale", "lt", domain="testserver.local", path="/"
        )
        client.cookies.set("language", "fr_FR")
        assert client.get("/api/session").json()["locale"] == "en"
        unsupported = client.get("/api/session?lang=fr")
        assert unsupported.json()["locale"] == "en"
        assert client.cookies["toilet_locale"] == "en"


def test_unknown_cms_identity_is_not_auto_created(tmp_path):
    with app_client(tmp_path) as (app, client):
        app.state.cms_auth.result = CMSAuthResult(
            CMSAuthStatus.AUTHENTICATED,
            username="not-in-control",
            contest="contest",
        )
        response = client.get("/")
        assert response.status_code == 403
        assert app.state.mutations.get_student("not-in-control") is None


def test_default_configuration_hides_and_rejects_every_support_request(tmp_path):
    with app_client(tmp_path) as (app, client):
        assert app.state.settings.general_request_types == ()
        seed_route(app)
        student = login_student(client, app)
        page = client.get("/")
        assert "const INITIAL_REQUEST_TYPES = {};" in page.text
        assert client.get("/api/state").json()["request_types"] == {}
        for disabled_type in ("paper", "water", "snack", "tech"):
            rejected = client.post(
                "/api/requests",
                data={"type": disabled_type},
                headers={"X-CSRF-Token": student["csrf_token"]},
            )
            assert rejected.status_code == 400
            assert rejected.json()["detail"] == "request type is not enabled"
        accepted = client.post(
            "/api/requests",
            data={"type": "toilet"},
            headers={"X-CSRF-Token": student["csrf_token"]},
        )
        assert accepted.status_code == 200

        client.cookies.clear()
        login_operator(client, app, "proctor")
        proctor_page = client.get("/proctor-all")
        assert "const GENERAL_REQUESTS_ENABLED = false;" in proctor_page.text
        assert (
            '<section id="support-section" aria-labelledby="support-heading" hidden>'
            in proctor_page.text
        )
        assert client.get("/api/proctor/state").json()["support"] == []


def test_explicitly_enabled_support_request_is_offered_and_accepted(tmp_path):
    with app_client(tmp_path, settings=settings_with_paper()) as (app, client):
        seed_route(app)
        student = login_student(client, app)
        page = client.get("/")
        assert 'const INITIAL_REQUEST_TYPES = {"paper": "Additional paper"};' in page.text
        assert client.get("/api/state").json()["request_types"] == {
            "paper": "Additional paper"
        }
        accepted = client.post(
            "/api/requests",
            data={"type": "paper"},
            headers={"X-CSRF-Token": student["csrf_token"]},
        )
        assert accepted.status_code == 200

        client.cookies.clear()
        login_operator(client, app, "proctor")
        proctor_page = client.get("/proctor-all")
        assert "const GENERAL_REQUESTS_ENABLED = true;" in proctor_page.text
        assert (
            '<section id="support-section" aria-labelledby="support-heading">'
            in proctor_page.text
        )


def test_contestant_surface_is_disabled_until_a_deployment_opts_in(tmp_path):
    assert Settings().student_ui_enabled is False
    with app_client(tmp_path, student_ui=False) as (app, client):
        page = client.get("/", follow_redirects=False)
        assert page.status_code == 200
        assert "ask a proctor for a toilet break" in page.text
        assert "paprašykite prižiūrėtojo" in page.text
        assert "const CSRF" not in page.text
        # The notice never depends on CMS, so it also renders during an outage.
        assert app.state.cms_auth.calls == []


def test_proctor_queues_a_toilet_break_for_the_selected_contestant(tmp_path):
    with app_client(tmp_path, student_ui=False) as (app, client):
        seed_route(app)
        app.state.mutations.sync_students(
            [{"id": 1, "userid": "alice"}], actor=Actor.system("test-student-sync")
        )
        proctor = login_operator(client, app, "proctor")

        unprotected = client.post("/api/proctor/requests", data={"userid": "alice"})
        assert unprotected.status_code == 403
        unknown = client.post(
            "/api/proctor/requests",
            data={"userid": "nobody"},
            headers={"X-CSRF-Token": proctor["csrf_token"]},
        )
        assert unknown.status_code == 404

        created = client.post(
            "/api/proctor/requests",
            data={"userid": "alice"},
            headers={"X-CSRF-Token": proctor["csrf_token"]},
        )
        assert created.status_code == 200
        assert app.state.control.assignment_calls == ["alice"]
        assert created.json()["status"] == "active"
        assert created.json()["toilets"][0]["name"] == "North"
        request_id = created.json()["id"]

        duplicate = client.post(
            "/api/proctor/requests",
            data={"userid": "alice"},
            headers={"X-CSRF-Token": proctor["csrf_token"]},
        )
        assert duplicate.status_code == 409

        queue = client.get("/api/proctor/state").json()["queue"]
        assert [item["id"] for item in queue] == [request_id]
        assert queue[0]["username"] == "alice"
        layout = client.get(f"/api/proctor/layouts?class_id={CLASS_A}").json()
        computer = layout["classes"][0]["computers"][0]
        assert [item["id"] for item in computer["requests"]] == [request_id]

        with app.state.database.SessionLocal() as session:
            event = (
                session.query(AuditEvent)
                .filter_by(action="request.created", target_identifier=str(request_id))
                .one()
            )
            assert event.actor_kind == "operator"
            assert event.actor_identifier == "test-proctor"

        # A returned break lets the proctor queue the same contestant again.
        assert client.post(
            f"/api/proctor/return/{request_id}",
            headers={"X-CSRF-Token": proctor["csrf_token"]},
        ).status_code == 200
        assert client.post(
            "/api/proctor/requests",
            data={"userid": "alice"},
            headers={"X-CSRF-Token": proctor["csrf_token"]},
        ).status_code == 200


def test_scoped_proctor_cannot_queue_a_contestant_from_another_class(tmp_path):
    with app_client(tmp_path, student_ui=False) as (app, client):
        seed_two_class_routes(app)
        app.state.mutations.sync_students(
            [{"id": 1, "userid": "alice"}], actor=Actor.system("test-student-sync")
        )
        _response, session = login_control_operator(
            client,
            app,
            "class-b-proctor",
            roles=("proctor",),
            class_scope=(CLASS_B,),
        )
        # FakeControl assigns alice to Class A only.
        rejected = client.post(
            "/api/proctor/requests",
            data={"userid": "alice"},
            headers={"X-CSRF-Token": session["csrf_token"]},
        )
        assert rejected.status_code == 403
        assert rejected.json()["detail"] == "request is outside proctor class scope"
        assert client.get("/api/proctor/state").json()["queue"] == []


def test_admin_role_alone_cannot_queue_a_toilet_break(tmp_path):
    with app_client(tmp_path, student_ui=False) as (app, client):
        seed_route(app)
        app.state.mutations.sync_students(
            [{"id": 1, "userid": "alice"}], actor=Actor.system("test-student-sync")
        )
        admin = login_operator(client, app)
        rejected = client.post(
            "/api/proctor/requests",
            data={"userid": "alice"},
            headers={"X-CSRF-Token": admin["csrf_token"]},
        )
        assert rejected.status_code == 403


def test_live_layout_overlays_requests_without_location_mirror(tmp_path):
    with app_client(tmp_path) as (app, client):
        seed_route(app)
        student = login_student(client, app)
        created = client.post(
            "/api/requests",
            data={"type": "toilet"},
            headers={"X-CSRF-Token": student["csrf_token"]},
        ).json()
        client.cookies.clear()
        login_operator(client, app, "proctor")
        response = client.get(f"/api/proctor/layouts?class_id={CLASS_A}")
        assert response.status_code == 200
        computer = response.json()["classes"][0]["computers"][0]
        assert computer["student"]["userid"] == "alice"
        assert [item["id"] for item in computer["requests"]] == [created["id"]]
        tables = sa_inspect(app.state.database.engine).get_table_names()
        assert "user_current_classes" not in tables
        assert "catalog_sync_state" not in tables


def test_repeated_cms_ambiguity_is_coalesced_across_http_and_websocket(tmp_path):
    cms = FakeCMS(
        CMSAuthResult(
            CMSAuthStatus.AMBIGUOUS,
            detail="configured contests authenticated different users",
        )
    )
    settings = Settings(
        public_origin="http://testserver",
        cms_contests=("a", "b"),
        cms_multi_contest=True,
        control_auth_key="test-key",
    )
    with app_client(tmp_path, settings=settings, cms=cms) as (app, client):
        for _ in range(5):
            assert client.get("/api/state").status_code == 401
        for _ in range(2):
            with client.websocket_connect(
                "/ws/student", headers={"origin": "http://testserver"}
            ) as websocket:
                with pytest.raises(WebSocketDisconnect) as closed:
                    websocket.receive_json()
                assert closed.value.code == 4401
        with app.state.database.SessionLocal() as session:
            assert session.query(AuditEvent).filter_by(
                action="cms.authentication_ambiguous"
            ).count() == 1


def test_cms_companion_session_limit_closes_websocket_with_retry_code(tmp_path):
    cms = FakeCMS(
        CMSAuthResult(
            CMSAuthStatus.AUTHENTICATED, username="alice", contest="contest"
        )
    )
    settings = Settings(
        public_origin="http://testserver",
        cms_contests=("contest",),
        control_auth_key="test-key",
        student_rate_limit_count=1,
    )
    with app_client(tmp_path, settings=settings, cms=cms) as (_app, client):
        assert client.get("/").status_code == 200
        client.cookies.clear()
        with client.websocket_connect(
            "/ws/student", headers={"origin": "http://testserver"}
        ) as websocket:
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 1013
