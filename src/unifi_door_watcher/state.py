from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from .config import DoorConfig

DpsState = Literal["open", "close", "unknown"]


@dataclass
class DoorState:
    door_id: str
    dps: DpsState = "unknown"
    last_unlock_at: float | None = None
    last_unlock_method: str | None = None
    last_unlock_actor_id: str | None = None
    last_unlock_actor_name: str | None = None
    # `event_object_id` from the unlock event (the UAH device ID). Stashed so
    # that a subsequent `access.logs.add` carrying the same event_object_id
    # can attribute the unlock to a user, even when the access.logs.add
    # payload identifies the door only by display_name.
    last_unlock_event_object_id: str | None = None
    opened_at: float | None = None
    held_open_task: asyncio.Task[None] | None = None
    held_open_source: str = "watchdog"
    unauthorized_fired: bool = False
    held_open_fired: bool = False
    schedule_unlock_active: bool = False
    temporary_unlock_active: bool = False


@dataclass
class DoorStateRegistry:
    doors_by_id: dict[str, DoorConfig]
    states: dict[str, DoorState] = field(default_factory=dict)
    emergency_evacuation_active: bool = False

    def __post_init__(self) -> None:
        for door_id in self.doors_by_id:
            self.states.setdefault(door_id, DoorState(door_id=door_id))

    def get(self, door_id: str) -> DoorState | None:
        return self.states.get(door_id)

    def find_recent_unlock_by_event_object_id(
        self, event_object_id: str, max_age: float, now: float
    ) -> DoorState | None:
        """Find the door state whose most recent unlock has the given
        `event_object_id` (UAH device ID), within `max_age` seconds. Used by
        `_on_access_log` to attribute actors to remote unlocks.

        Linear scan is fine at 24 doors — we'd need to be much larger before
        an event_object_id index pays off."""
        for state in self.states.values():
            if state.last_unlock_event_object_id != event_object_id:
                continue

            if state.last_unlock_at is None or (now - state.last_unlock_at) > max_age:
                continue

            return state

        return None

    def free_pass(self, state: DoorState) -> bool:
        return (
            state.schedule_unlock_active
            or state.temporary_unlock_active
            or self.emergency_evacuation_active
        )
