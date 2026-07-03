"""Tests for startup state seeding via the Access REST API."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tests.conftest import dps_evt, drain_tasks
from unifi_door_watcher.access.seed import seed_initial_state
from unifi_door_watcher.config import DoorConfig
from unifi_door_watcher.detect.pipeline import DetectionPipeline
from unifi_door_watcher.state import DoorStateRegistry


def _make_client(doors: list[dict], lock_rules: dict[str, dict[str, Any]] | None = None):
    """Build a fake AccessClient that returns canned responses."""
    lock_rules = lock_rules or {}
    client = AsyncMock()
    client.list_doors.return_value = doors
    client.fetch_lock_rule.side_effect = lambda door_id: lock_rules.get(door_id, {})
    return client


def _make_pipeline(
    sink, fake_clock, doors: list[DoorConfig]
) -> tuple[DetectionPipeline, DoorStateRegistry]:
    registry = DoorStateRegistry(doors_by_id={d.id: d for d in doors})
    pipeline = DetectionPipeline(registry, sink, clock=fake_clock)  # type: ignore[arg-type]
    return pipeline, registry


@pytest.mark.asyncio
async def test_seeds_dps_open_for_open_doors(sink, fake_clock):
    doors = [DoorConfig(id="d1", name="Front", grace_seconds=0, held_open_seconds=2)]
    pipeline, registry = _make_pipeline(sink, fake_clock, doors)
    client = _make_client(
        [
            {"id": "d1", "door_position_status": "open", "door_lock_relay_status": "lock"},
        ]
    )

    await seed_initial_state(pipeline, client, registry)

    state = registry.states["d1"]
    assert state.dps == "open"
    # Open + no free-pass → watchdog armed with source=startup_open.
    assert state.held_open_task is not None
    assert state.held_open_source == "startup_open"


@pytest.mark.asyncio
async def test_seeds_dps_close_for_closed_doors(sink, fake_clock):
    doors = [DoorConfig(id="d1", name="Front", grace_seconds=8, held_open_seconds=2)]
    pipeline, registry = _make_pipeline(sink, fake_clock, doors)
    client = _make_client(
        [
            {"id": "d1", "door_position_status": "close", "door_lock_relay_status": "lock"},
        ]
    )

    await seed_initial_state(pipeline, client, registry)

    state = registry.states["d1"]
    assert state.dps == "close"
    assert state.unauthorized_fired is False  # door's closed, normal tracking applies


@pytest.mark.asyncio
async def test_unknown_dps_left_as_unknown(sink, fake_clock):
    """A door with no DPS sensor (`door_position_status: null`) should not
    have its `dps` field clobbered."""
    doors = [DoorConfig(id="d1", name="Front", grace_seconds=8, held_open_seconds=2)]
    pipeline, registry = _make_pipeline(sink, fake_clock, doors)
    client = _make_client(
        [
            {"id": "d1", "door_position_status": None, "door_lock_relay_status": "lock"},
        ]
    )

    await seed_initial_state(pipeline, client, registry)

    assert registry.states["d1"].dps == "unknown"


@pytest.mark.asyncio
async def test_schedule_unlock_active_at_startup(sink, fake_clock):
    doors = [DoorConfig(id="d1", name="Front", grace_seconds=8, held_open_seconds=2)]
    pipeline, registry = _make_pipeline(sink, fake_clock, doors)
    client = _make_client(
        [{"id": "d1", "door_position_status": "close", "door_lock_relay_status": "unlock"}],
        lock_rules={"d1": {"type": "schedule", "ended_time": 1800000000}},
    )

    await seed_initial_state(pipeline, client, registry)

    state = registry.states["d1"]
    assert state.schedule_unlock_active is True
    assert state.temporary_unlock_active is False
    client.fetch_lock_rule.assert_awaited_once_with("d1")


@pytest.mark.asyncio
async def test_temporary_unlock_active_at_startup(sink, fake_clock):
    doors = [DoorConfig(id="d1", name="Front", grace_seconds=8, held_open_seconds=2)]
    pipeline, registry = _make_pipeline(sink, fake_clock, doors)
    client = _make_client(
        [{"id": "d1", "door_position_status": "close", "door_lock_relay_status": "unlock"}],
        lock_rules={"d1": {"type": "keep_unlock", "ended_time": 1800000000}},
    )

    await seed_initial_state(pipeline, client, registry)

    state = registry.states["d1"]
    assert state.schedule_unlock_active is False
    assert state.temporary_unlock_active is True


@pytest.mark.asyncio
async def test_custom_unlock_treated_as_temporary(sink, fake_clock):
    doors = [DoorConfig(id="d1", name="Front", grace_seconds=8, held_open_seconds=2)]
    pipeline, registry = _make_pipeline(sink, fake_clock, doors)
    client = _make_client(
        [{"id": "d1", "door_position_status": "close", "door_lock_relay_status": "unlock"}],
        lock_rules={"d1": {"type": "custom", "ended_time": 1800000000}},
    )

    await seed_initial_state(pipeline, client, registry)

    assert registry.states["d1"].temporary_unlock_active is True


@pytest.mark.asyncio
async def test_keep_lock_does_not_set_free_pass_flags(sink, fake_clock):
    """If the relay is locked, we don't query lock_rule at all."""
    doors = [DoorConfig(id="d1", name="Front", grace_seconds=8, held_open_seconds=2)]
    pipeline, registry = _make_pipeline(sink, fake_clock, doors)
    client = _make_client(
        [{"id": "d1", "door_position_status": "close", "door_lock_relay_status": "lock"}],
    )

    await seed_initial_state(pipeline, client, registry)

    state = registry.states["d1"]
    assert state.schedule_unlock_active is False
    assert state.temporary_unlock_active is False
    client.fetch_lock_rule.assert_not_called()


@pytest.mark.asyncio
async def test_skips_doors_not_in_config(sink, fake_clock):
    """UniFi might know about more doors than our config does. Skip them."""
    doors = [DoorConfig(id="d1", name="Front", grace_seconds=8, held_open_seconds=2)]
    pipeline, registry = _make_pipeline(sink, fake_clock, doors)
    client = _make_client(
        [
            {"id": "d1", "door_position_status": "close", "door_lock_relay_status": "lock"},
            {
                "id": "unknown-door",
                "door_position_status": "close",
                "door_lock_relay_status": "lock",
            },
        ]
    )

    await seed_initial_state(pipeline, client, registry)

    assert "unknown-door" not in registry.states
    assert registry.states["d1"].dps == "close"


@pytest.mark.asyncio
async def test_list_doors_failure_is_tolerated(sink, fake_clock):
    """Controller briefly unreachable at startup → log warning, continue
    with empty seed. Service still comes up."""
    doors = [DoorConfig(id="d1", name="Front", grace_seconds=8, held_open_seconds=2)]
    pipeline, registry = _make_pipeline(sink, fake_clock, doors)
    client = AsyncMock()
    client.list_doors.side_effect = RuntimeError("network down")

    await seed_initial_state(pipeline, client, registry)  # must not raise

    assert registry.states["d1"].dps == "unknown"


@pytest.mark.asyncio
async def test_lock_rule_failure_is_tolerated(sink, fake_clock):
    """If list_doors works but lock_rule fails for one door, we still seed
    the dps and just leave the free-pass flags False for that one."""
    doors = [
        DoorConfig(id="d1", name="A", grace_seconds=8, held_open_seconds=2),
        DoorConfig(id="d2", name="B", grace_seconds=8, held_open_seconds=2),
    ]
    pipeline, registry = _make_pipeline(sink, fake_clock, doors)
    client = AsyncMock()
    client.list_doors.return_value = [
        {"id": "d1", "door_position_status": "close", "door_lock_relay_status": "unlock"},
        {"id": "d2", "door_position_status": "open", "door_lock_relay_status": "lock"},
    ]

    async def lock_rule(door_id: str) -> dict:
        if door_id == "d1":
            raise RuntimeError("oops")
        return {}

    client.fetch_lock_rule.side_effect = lock_rule

    await seed_initial_state(pipeline, client, registry)

    assert registry.states["d1"].dps == "close"
    assert registry.states["d1"].schedule_unlock_active is False  # failed, defaulted
    assert registry.states["d2"].dps == "open"


@pytest.mark.asyncio
async def test_schedule_deactivate_after_seed_fires_held_open(sink, fake_clock):
    """End-to-end: seed shows schedule active + door open → schedule ends
    while door still open → held_open watchdog runs (via the synthetic
    on_open path in _free_pass_end), fires after held_open_seconds."""
    doors = [DoorConfig(id="d1", name="Front", grace_seconds=8, held_open_seconds=2)]
    pipeline, registry = _make_pipeline(sink, fake_clock, doors)
    client = _make_client(
        [{"id": "d1", "door_position_status": "open", "door_lock_relay_status": "unlock"}],
        lock_rules={"d1": {"type": "schedule"}},
    )

    await seed_initial_state(pipeline, client, registry)

    # Now the schedule ends while the door is still open.
    await pipeline.handle(
        {
            "event": "access.unlock_schedule.deactivate",
            "data": {"location": {"id": "d1", "name": "Front"}},
        }
    )
    await asyncio.sleep(2.2)
    await drain_tasks()

    held = [a for a in sink.alerts if a.alert_type == "held_open"]
    assert len(held) == 1
    assert held[0].details["source"] == "schedule_ended"


@pytest.mark.asyncio
async def test_seed_fires_held_open_for_open_door_with_no_free_pass(sink, fake_clock):
    """Door open at startup with no schedule/temp unlock — give it
    held_open_seconds from now, then alert. The alert's `source` is
    `startup_open` so the operator can tell it came from a service
    restart detection rather than live tracking."""
    doors = [DoorConfig(id="d1", name="Front", grace_seconds=0, held_open_seconds=2)]
    pipeline, registry = _make_pipeline(sink, fake_clock, doors)
    client = _make_client(
        [{"id": "d1", "door_position_status": "open", "door_lock_relay_status": "lock"}],
    )

    await seed_initial_state(pipeline, client, registry)
    await asyncio.sleep(2.2)
    await drain_tasks()

    held = [a for a in sink.alerts if a.alert_type == "held_open"]
    assert len(held) == 1
    assert held[0].details["source"] == "startup_open"


@pytest.mark.asyncio
async def test_seed_does_not_fire_when_free_pass_active(sink, fake_clock):
    """Door open at startup under an active schedule — no watchdog. The
    schedule end (or a deactivate event) is what will fire held_open."""
    doors = [DoorConfig(id="d1", name="Front", grace_seconds=0, held_open_seconds=2)]
    pipeline, registry = _make_pipeline(sink, fake_clock, doors)
    client = _make_client(
        [{"id": "d1", "door_position_status": "open", "door_lock_relay_status": "unlock"}],
        lock_rules={"d1": {"type": "schedule"}},
    )

    await seed_initial_state(pipeline, client, registry)
    await asyncio.sleep(2.2)
    await drain_tasks()

    assert sink.alerts == []
    state = registry.states["d1"]
    assert state.schedule_unlock_active is True
    assert state.held_open_task is None  # no watchdog while free-pass active


@pytest.mark.asyncio
async def test_seed_open_watchdog_cancelled_by_close(sink, fake_clock):
    """If the seeded-open door closes before the watchdog fires, no alert."""
    doors = [DoorConfig(id="d1", name="Front", grace_seconds=0, held_open_seconds=2)]
    pipeline, registry = _make_pipeline(sink, fake_clock, doors)
    client = _make_client(
        [{"id": "d1", "door_position_status": "open", "door_lock_relay_status": "lock"}],
    )

    await seed_initial_state(pipeline, client, registry)
    await asyncio.sleep(0.5)
    await pipeline.handle(dps_evt("d1", "close"))
    await asyncio.sleep(2.0)
    await drain_tasks()

    assert sink.alerts == []


@pytest.mark.asyncio
async def test_seeded_open_door_starts_tracking_after_close_reopen(sink, fake_clock):
    """A door seeded as open should resume normal unauthorized tracking
    once we see a close→open transition on the wire."""
    doors = [DoorConfig(id="d1", name="Front", grace_seconds=0, held_open_seconds=2)]
    pipeline, registry = _make_pipeline(sink, fake_clock, doors)
    client = _make_client(
        [{"id": "d1", "door_position_status": "open", "door_lock_relay_status": "lock"}],
    )

    await seed_initial_state(pipeline, client, registry)
    # Door closes (someone shut it), then reopens without a badge.
    await pipeline.handle(dps_evt("d1", "close"))
    await pipeline.handle(dps_evt("d1", "open"))
    await drain_tasks()

    assert [a.alert_type for a in sink.alerts] == ["unauthorized"]
