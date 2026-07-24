import hashlib
import hmac
import json

import httpx
import pytest

from toilet2.control_client import (
    AUTH_HEADER,
    TIMESTAMP_HEADER,
    ControlAPIUnavailable,
    ControlClient,
    StudentInfo,
)
from toilet2.settings import Settings


KEY = "test-service-key"
CLASS_ID = "10000000-0000-0000-0000-000000000001"


def _response(timestamp, status, payload, valid=True):
    body = json.dumps(payload, separators=(",", ":")).encode()
    message = b"\n".join([timestamp.encode(), str(status).encode(), body])
    signature = hmac.new(KEY.encode(), message, hashlib.sha256).hexdigest()
    if not valid:
        signature = "0" * 64
    return httpx.Response(status, content=body, headers={AUTH_HEADER: signature})


def _assert_request_signature(request):
    timestamp = request.headers[TIMESTAMP_HEADER]
    message = b"\n".join(
        [timestamp.encode(), request.method.encode(), request.url.raw_path, request.content]
    )
    expected = hmac.new(KEY.encode(), message, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(request.headers[AUTH_HEADER], expected)
    return timestamp


@pytest.mark.asyncio
async def test_students_classes_assignment_and_live_layout_contracts():
    async def handler(request):
        timestamp = _assert_request_signature(request)
        if request.url.path.endswith("/students"):
            return _response(
                timestamp,
                200,
                {
                    "students": [{"id": 7, "userid": "alice"}]
                },
            )
        if request.url.path.endswith("/classes"):
            return _response(
                timestamp,
                200,
                {
                    "classes": [
                        {
                            "id": CLASS_ID,
                            "name": "101",
                            "sequence_num": -2,
                            "grid_cols": 8,
                        }
                    ]
                },
            )
        if request.url.path.endswith("/layout"):
            assert request.url.path == f"/api/toilet/v1/classes/{CLASS_ID}/layout"
            return _response(
                timestamp,
                200,
                {
                    "class": {
                        "id": CLASS_ID,
                        "name": "101",
                        "sequence_num": -2,
                        "grid_cols": 8,
                    },
                    "computers": [
                        {
                            "machine_id": "m1",
                            "name": "PC 1",
                            "sequence_num": -3,
                            "grid_row": 1,
                            "grid_col": 2,
                            "student": {
                                "id": 7,
                                "userid": "alice",
                            },
                        }
                    ],
                },
            )
        assert request.url.path.endswith("/student-assignment")
        assert json.loads(request.content) == {"userid": "alice"}
        return _response(
            timestamp,
            200,
            {
                "found": True,
                "userid": "alice",
                "student": {"id": 7, "userid": "alice"},
                "computers": [
                    {
                        "machine_id": "m1",
                        "name": "PC 1",
                        "sequence_num": -3,
                        "grid_row": 1,
                        "grid_col": 2,
                        "student": {
                            "id": 7,
                            "userid": "alice",
                        },
                        "class": {
                            "id": CLASS_ID,
                            "name": "101",
                            "sequence_num": -2,
                            "grid_cols": 8,
                        },
                    }
                ],
                "classes": [
                    {
                        "id": CLASS_ID,
                        "name": "101",
                        "sequence_num": -2,
                        "grid_cols": 8,
                    }
                ],
                "anomalies": [],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://control.test"
    ) as client:
        api = ControlClient(
            Settings(control_auth_key=KEY), client=client, clock=lambda: 1234.0
        )
        students = await api.students()
        assert students == (StudentInfo(7, "alice"),)
        classes = await api.classes()
        assert classes[0].grid_cols == 8
        assert classes[0].sequence_num == -2
        layout = await api.class_layout(CLASS_ID)
        assert layout.computers[0]["sequence_num"] == -3
        assert layout.computers[0]["student"]["userid"] == "alice"
        assignment = await api.student_assignment("alice")
        assert assignment["userid"] == "alice"
        assert assignment["computers"][0]["class"]["id"] == CLASS_ID
        assert not hasattr(api, "authenticate_operator")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,method,match",
    [
        (
            {"students": [{"id": 1, "userid": " "}]},
            "students",
            "userid",
        ),
        (
            {
                "class": {
                    "id": CLASS_ID,
                    "name": "101",
                    "sequence_num": 0,
                    "grid_cols": 0,
                },
                "computers": [],
            },
            "layout",
            "grid columns",
        ),
        (
            {
                "found": True,
                "userid": "alice",
                "student": {"id": 1, "userid": "other"},
                "computers": [],
                "classes": [],
                "anomalies": [],
            },
            "assignment",
            "different student",
        ),
    ],
)
async def test_live_data_schemas_fail_closed(payload, method, match):
    async def handler(request):
        return _response(request.headers[TIMESTAMP_HEADER], 200, payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://control.test"
    ) as client:
        api = ControlClient(Settings(control_auth_key=KEY), client=client)
        with pytest.raises(ControlAPIUnavailable, match=match):
            if method == "students":
                await api.students()
            elif method == "layout":
                await api.class_layout(CLASS_ID)
            else:
                await api.student_assignment("alice")


@pytest.mark.asyncio
async def test_bad_response_signature_fails_closed():
    async def handler(request):
        timestamp = request.headers[TIMESTAMP_HEADER]
        return _response(timestamp, 200, {"classes": []}, valid=False)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://control.test"
    ) as client:
        api = ControlClient(Settings(control_auth_key=KEY), client=client)
        with pytest.raises(ControlAPIUnavailable, match="signature"):
            await api.classes()
