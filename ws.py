"""WebSocket connection registry and post-commit state broadcasts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from typing import Awaitable, Callable


@dataclass(frozen=True, slots=True)
class StaffSubscription:
    subject: str
    roles: frozenset[str]
    all_classes: bool
    class_scope: frozenset[str]


@dataclass(frozen=True, slots=True)
class StudentSubscription:
    user_id: int
    locale: str


class Hub:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.students: dict[object, StudentSubscription] = {}
        self.staff: dict[object, StaffSubscription] = {}

    def add_student(self, user_id: int, websocket, locale: str = "en") -> None:
        self.students[websocket] = StudentSubscription(user_id, locale)

    def remove_student(self, user_id: int, websocket) -> None:
        subscription = self.students.get(websocket)
        if subscription is not None and subscription.user_id == user_id:
            self.students.pop(websocket, None)

    def add_staff(self, websocket, subscription: StaffSubscription) -> None:
        self.staff[websocket] = subscription

    def remove_staff(self, websocket) -> None:
        self.staff.pop(websocket, None)

    def notify_from_thread(
        self,
        student_state: Callable[[StudentSubscription], dict],
        staff_state: Callable[[StaffSubscription], dict],
    ) -> None:
        """Schedule a broadcast after a successful synchronous mutation."""

        if self.loop is None or self.loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(
            self._broadcast(student_state, staff_state), self.loop
        )

    async def _broadcast(
        self,
        student_state: Callable[[StudentSubscription], dict],
        staff_state: Callable[[StaffSubscription], dict],
    ) -> None:
        for websocket, subscription in list(self.students.items()):
            try:
                await websocket.send_json(student_state(subscription))
            except Exception:
                self.remove_student(subscription.user_id, websocket)
        for websocket, subscription in list(self.staff.items()):
            try:
                payload = staff_state(subscription)
                if inspect.isawaitable(payload):
                    payload = await payload
                await websocket.send_json(payload)
            except Exception:
                self.remove_staff(websocket)


hub = Hub()
