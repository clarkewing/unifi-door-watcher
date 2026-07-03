from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

AlertType = Literal["unauthorized", "held_open"]
HeldOpenSource = Literal[
    "watchdog",
    "schedule_ended",
    "temporary_ended",
    "emergency_ended",
    "startup_open",
]


class Actor(BaseModel):
    id: str | None = None
    name: str | None = None


class AccessEvent(BaseModel):
    """Loose wrapper around the Access notification payload — only the fields
    we use are declared; the rest is preserved in `raw` for debugging."""

    event: str
    door_id: str | None = None
    door_name: str | None = None
    timestamp: float
    raw: dict[str, Any] = Field(default_factory=dict)


class Alert(BaseModel):
    alert_type: AlertType
    door_id: str
    door_name: str
    occurred_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)
