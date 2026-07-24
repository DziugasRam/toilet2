"""Authenticated client for Olimp-control contestant and live-layout data."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import time
from typing import Any, Callable
import uuid

import httpx

from .settings import Settings


TIMESTAMP_HEADER = "X-LMIO-Toilet-Timestamp"
AUTH_HEADER = "X-LMIO-Toilet-Auth"


class ControlAPIError(RuntimeError):
    pass


class ControlAPIUnavailable(ControlAPIError):
    pass


class ControlAPIResponseError(ControlAPIError):
    def __init__(self, status_code: int, payload: Any):
        super().__init__(f"Olimp-control returned HTTP {status_code}")
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True, slots=True)
class StudentInfo:
    id: int
    userid: str


@dataclass(frozen=True, slots=True)
class ClassInfo:
    id: str
    name: str
    sequence_num: int = 0
    grid_cols: int | None = None


@dataclass(frozen=True, slots=True)
class ClassLayout:
    class_info: ClassInfo
    computers: tuple[dict[str, Any], ...]


def _positive_int_or_none(value: Any, context: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ControlAPIUnavailable(f"invalid {context}")
    return value


def _integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControlAPIUnavailable(f"invalid {context}")
    return value


def _student_info(value: Any, context: str) -> StudentInfo:
    if not isinstance(value, dict):
        raise ControlAPIUnavailable(f"invalid {context}: student must be an object")
    source_id = value.get("id")
    userid = value.get("userid")
    if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id <= 0:
        raise ControlAPIUnavailable(f"invalid {context}: student id")
    if not isinstance(userid, str) or not userid.strip():
        raise ControlAPIUnavailable(f"invalid {context}: student userid")
    return StudentInfo(source_id, userid)


def _student_dict(value: Any, context: str) -> dict[str, Any]:
    student = _student_info(value, context)
    return {
        "id": student.id,
        "userid": student.userid,
    }


def _class_info(value: Any, context: str) -> ClassInfo:
    if not isinstance(value, dict):
        raise ControlAPIUnavailable(f"invalid {context}: class must be an object")
    raw_id = value.get("id")
    name = value.get("name")
    sequence_num = value.get("sequence_num", 0)
    if not isinstance(raw_id, str) or not raw_id:
        raise ControlAPIUnavailable(f"invalid {context}: class id")
    try:
        public_id = str(uuid.UUID(raw_id))
    except ValueError as exc:
        raise ControlAPIUnavailable(f"invalid {context}: class id") from exc
    if not isinstance(name, str) or not name.strip():
        raise ControlAPIUnavailable(f"invalid {context}: class name")
    sequence_num = _integer(sequence_num, f"{context}: class sequence")
    grid_cols = _positive_int_or_none(
        value.get("grid_cols"), f"{context}: class grid columns"
    )
    return ClassInfo(public_id, name, sequence_num, grid_cols)


def _class_dict(value: Any, context: str) -> dict[str, Any]:
    info = _class_info(value, context)
    return {
        "id": info.id,
        "name": info.name,
        "sequence_num": info.sequence_num,
        "grid_cols": info.grid_cols,
    }


def _computer(
    value: Any,
    context: str,
    *,
    require_student_field: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ControlAPIUnavailable(f"invalid {context}: computer must be an object")
    machine_id = value.get("machine_id")
    name = value.get("name")
    if not isinstance(machine_id, str) or not machine_id:
        raise ControlAPIUnavailable(f"invalid {context}: computer machine id")
    if not isinstance(name, str):
        raise ControlAPIUnavailable(f"invalid {context}: computer name")
    result: dict[str, Any] = {
        "machine_id": machine_id,
        "name": name,
        "sequence_num": _integer(
            value.get("sequence_num", 0), f"{context}: computer sequence"
        ),
        "grid_row": _positive_int_or_none(
            value.get("grid_row"), f"{context}: computer grid row"
        ),
        "grid_col": _positive_int_or_none(
            value.get("grid_col"), f"{context}: computer grid column"
        ),
    }
    raw_class = value.get("class")
    result["class"] = (
        _class_dict(raw_class, f"{context}: computer class")
        if raw_class is not None
        else None
    )
    if require_student_field and "student" not in value:
        raise ControlAPIUnavailable(f"invalid {context}: missing computer student")
    raw_student = value.get("student")
    result["student"] = (
        _student_dict(raw_student, f"{context}: computer student")
        if raw_student is not None
        else None
    )
    return result


class ControlClient:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self._key = settings.control_auth_key.encode("utf-8")
        self._clock = clock
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.control_base_url,
            timeout=httpx.Timeout(settings.control_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _request_signature(
        self, timestamp: str, method: str, path: str, body: bytes
    ) -> str:
        message = b"\n".join(
            [timestamp.encode("ascii"), method.upper().encode("ascii"), path.encode("utf-8"), body]
        )
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def _response_signature(self, timestamp: str, status: int, body: bytes) -> str:
        message = b"\n".join(
            [timestamp.encode("ascii"), str(status).encode("ascii"), body]
        )
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = (
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if payload is not None
            else b""
        )
        timestamp = str(int(self._clock()))
        headers = {
            TIMESTAMP_HEADER: timestamp,
            AUTH_HEADER: self._request_signature(timestamp, method, path, body),
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = await self._client.request(
                method, path, content=body if payload is not None else None, headers=headers
            )
        except httpx.HTTPError as exc:
            raise ControlAPIUnavailable(type(exc).__name__) from exc
        expected = self._response_signature(timestamp, response.status_code, response.content)
        received = response.headers.get(AUTH_HEADER, "")
        if not hmac.compare_digest(received, expected):
            raise ControlAPIUnavailable("invalid Olimp-control response signature")
        try:
            decoded = response.json()
        except ValueError as exc:
            raise ControlAPIUnavailable("invalid Olimp-control JSON response") from exc
        if response.status_code >= 500:
            raise ControlAPIUnavailable(f"Olimp-control returned {response.status_code}")
        if response.status_code >= 400:
            raise ControlAPIResponseError(response.status_code, decoded)
        if not isinstance(decoded, dict):
            raise ControlAPIUnavailable("Olimp-control response must be an object")
        return decoded

    async def students(self) -> tuple[StudentInfo, ...]:
        data = await self._request("GET", "/api/toilet/v1/students")
        raw_students = data.get("students")
        if not isinstance(raw_students, list):
            raise ControlAPIUnavailable("invalid or missing student catalog response")
        students = tuple(
            _student_info(item, "student catalog response") for item in raw_students
        )
        if len({item.id for item in students}) != len(students) or len(
            {item.userid for item in students}
        ) != len(students):
            raise ControlAPIUnavailable("duplicate student catalog response")
        return students

    async def classes(self) -> tuple[ClassInfo, ...]:
        data = await self._request("GET", "/api/toilet/v1/classes")
        raw_classes = data.get("classes")
        if not isinstance(raw_classes, list):
            raise ControlAPIUnavailable("invalid or missing class catalog response")
        classes = tuple(
            _class_info(item, "class catalog response") for item in raw_classes
        )
        if len({item.id for item in classes}) != len(classes):
            raise ControlAPIUnavailable("duplicate class id in catalog response")
        return classes

    async def class_layout(self, class_id: str) -> ClassLayout:
        try:
            public_id = str(uuid.UUID(class_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ControlAPIUnavailable("invalid requested class id") from exc
        data = await self._request(
            "GET", f"/api/toilet/v1/classes/{public_id}/layout"
        )
        class_info = _class_info(data.get("class"), "class layout response")
        if class_info.id != public_id:
            raise ControlAPIUnavailable("Olimp-control returned a different class layout")
        raw_computers = data.get("computers")
        if not isinstance(raw_computers, list):
            raise ControlAPIUnavailable("invalid or missing class layout computers")
        computers = tuple(
            _computer(item, "class layout response", require_student_field=True)
            for item in raw_computers
        )
        if len({item["machine_id"] for item in computers}) != len(computers):
            raise ControlAPIUnavailable("duplicate computer in class layout response")
        for computer in computers:
            computer_class = computer["class"]
            if computer_class is not None and computer_class["id"] != public_id:
                raise ControlAPIUnavailable("computer belongs to a different class layout")
        return ClassLayout(class_info, computers)

    async def student_assignment(self, userid: str) -> dict[str, Any]:
        data = await self._request(
            "POST",
            "/api/toilet/v1/student-assignment",
            {"userid": userid},
        )
        if type(data.get("found")) is not bool:
            raise ControlAPIUnavailable("invalid student found response")
        if data.get("userid") != userid:
            raise ControlAPIUnavailable("Olimp-control returned a different student")
        raw_student = data.get("student")
        if data["found"]:
            student = _student_dict(raw_student, "student assignment response")
            if student["userid"] != userid:
                raise ControlAPIUnavailable("Olimp-control returned a different student")
        elif raw_student is not None:
            raise ControlAPIUnavailable("missing student assignment cannot contain student")
        else:
            student = None
        if not isinstance(data.get("computers"), list):
            raise ControlAPIUnavailable("invalid student computer assignment response")
        if not isinstance(data.get("classes"), list):
            raise ControlAPIUnavailable("invalid student class assignment response")
        if not isinstance(data.get("anomalies"), list):
            raise ControlAPIUnavailable("invalid student anomaly response")
        classes = [
            _class_dict(item, "student class assignment response")
            for item in data["classes"]
        ]
        class_ids = {item["id"] for item in classes}
        if len(class_ids) != len(classes):
            raise ControlAPIUnavailable("duplicate student class assignment response")
        computers = [
            _computer(
                item,
                "student computer assignment response",
                require_student_field=True,
            )
            for item in data["computers"]
        ]
        if len({item["machine_id"] for item in computers}) != len(computers):
            raise ControlAPIUnavailable("duplicate student computer assignment response")
        for computer in computers:
            computer_class = computer["class"]
            if computer_class is not None and computer_class["id"] not in class_ids:
                raise ControlAPIUnavailable(
                    "computer class missing from student class assignment response"
                )
            computer_student = computer["student"]
            if computer_student is not None and computer_student["userid"] != userid:
                raise ControlAPIUnavailable(
                    "computer is assigned to a different student"
                )
        allowed_anomalies = {
            "student_not_found",
            "no_computers",
            "computer_without_class",
            "multiple_classes",
        }
        anomalies = data["anomalies"]
        for anomaly in anomalies:
            if (
                not isinstance(anomaly, dict)
                or anomaly.get("code") not in allowed_anomalies
            ):
                raise ControlAPIUnavailable("invalid student anomaly response")
        return {
            "found": data["found"],
            "student": student,
            "userid": userid,
            "computers": computers,
            "classes": classes,
            "anomalies": anomalies,
        }
