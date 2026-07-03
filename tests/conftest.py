from __future__ import annotations

import asyncio
from typing import Any

import pytest

from unifi_door_watcher.config import DoorConfig
from unifi_door_watcher.detect.pipeline import DetectionPipeline
from unifi_door_watcher.models import Alert
from unifi_door_watcher.state import DoorStateRegistry


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def tick(self, secs: float) -> None:
        self.t += secs


class RecordingSink:
    """Stand-in for ProtectAlertSink — records alerts without HTTP."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    async def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def doors() -> list[DoorConfig]:
    return [
        DoorConfig(id="ext-1", name="Front", grace_seconds=0, held_open_seconds=2),
        DoorConfig(id="int-1", name="IT Closet", grace_seconds=8, held_open_seconds=2),
    ]


@pytest.fixture
def registry(doors: list[DoorConfig]) -> DoorStateRegistry:
    return DoorStateRegistry(doors_by_id={d.id: d for d in doors})


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def pipeline(
    registry: DoorStateRegistry, sink: RecordingSink, fake_clock: FakeClock
) -> DetectionPipeline:
    return DetectionPipeline(registry, sink, clock=fake_clock)  # type: ignore[arg-type]


# --- event factories ----------------------------------------------------


def evt(kind: str, door_id: str, **data: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event": kind,
        "data": {"location": {"id": door_id, "name": door_id}},
    }
    payload["data"].update(data)
    return payload


def unlock_evt(door_id: str, method: str = "NFC", actor_name: str = "Jane Doe") -> dict[str, Any]:
    return {
        "event": "access.door.unlock",
        "data": {
            "location": {"id": door_id, "name": door_id},
            "actor": {"id": "u1", "name": actor_name},
            "object": {"authentication_type": method, "result": "Access Granted"},
        },
    }


def access_log_evt(
    event_object_id: str = "uah-default",
    actor_id: str = "user-uuid",
    actor_name: str = "Hugo Clarke-Wing",
    result: str = "ACCESS",
) -> dict[str, Any]:
    """Mirrors `access.logs.add` for an unlock. `event_object_id` is the
    UAH device ID; the pipeline correlates against the same id stashed by
    `_on_remote_unlock`."""
    return {
        "event": "access.logs.add",
        "event_object_id": event_object_id,
        "data": {
            "_source": {
                "actor": {
                    "id": actor_id,
                    "display_name": actor_name,
                    "first_name": actor_name.split()[0] if actor_name else "",
                    "last_name": " ".join(actor_name.split()[1:]) if actor_name else "",
                },
                "event": {
                    "type": "access.door.unlock.success.protect_shortcut",
                    "result": result,
                    "display_message": "Access Granted (Remote)",
                },
            },
            "tag": "access",
        },
    }


def remote_unlock_evt(
    door_id: str,
    actor_name: str | None = None,
    event_object_id: str = "uah-default",
) -> dict[str, Any]:
    """Mirrors `access.data.device.remote_unlock`: door fields directly on
    `data`, UAH device ID in the top-level `event_object_id`."""
    data: dict[str, Any] = {
        "unique_id": door_id,
        "name": door_id,
        "location_type": "door",
        "full_name": f"Building - {door_id}",
    }
    if actor_name is not None:
        data["actor"] = {"id": "admin1", "name": actor_name}
    return {
        "event": "access.data.device.remote_unlock",
        "event_object_id": event_object_id,
        "data": data,
    }


def dps_evt(door_id: str, status: str) -> dict[str, Any]:
    return {
        "event": "access.device.dps_status",
        "data": {
            "location": {"id": door_id, "name": door_id},
            "object": {"event_type": "dps_change", "status": status},
        },
    }


def schedule_evt(door_id: str, activate: bool) -> dict[str, Any]:
    return {
        "event": "access.unlock_schedule.activate"
        if activate
        else "access.unlock_schedule.deactivate",
        "data": {"location": {"id": door_id, "name": door_id}},
    }


def temporary_evt(door_id: str, start: bool, actor_name: str = "Admin") -> dict[str, Any]:
    return {
        "event": "access.temporary_unlock.start" if start else "access.temporary_unlock.end",
        "data": {
            "location": {"id": door_id, "name": door_id},
            "actor": {"id": "admin1", "name": actor_name},
        },
    }


def emergency_evt(mode: str) -> dict[str, Any]:
    return {
        "event": "access.device.emergency_status",
        "data": {"object": {"mode": mode}},
    }


async def drain_tasks() -> None:
    """Yield to the event loop so any fire-and-forget _emit() tasks run."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)
