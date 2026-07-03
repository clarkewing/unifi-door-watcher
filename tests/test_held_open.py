from __future__ import annotations

import asyncio

import pytest

from tests.conftest import (
    dps_evt,
    drain_tasks,
    emergency_evt,
    schedule_evt,
    temporary_evt,
    unlock_evt,
)

# Doors in the fixture have held_open_seconds=2 so tests stay fast.


@pytest.mark.asyncio
async def test_held_open_fires_after_threshold(pipeline, sink, fake_clock):
    await pipeline.handle(unlock_evt("int-1"))
    await pipeline.handle(dps_evt("int-1", "open"))
    await asyncio.sleep(2.2)
    await drain_tasks()
    held = [a for a in sink.alerts if a.alert_type == "held_open"]
    assert len(held) == 1
    assert held[0].details["source"] == "watchdog"
    # Held-open alert should carry actor info from the prior unlock.
    assert held[0].details["last_unlock_actor"] == {"id": "u1", "name": "Jane Doe"}
    assert held[0].details["last_unlock_method"] == "NFC"


@pytest.mark.asyncio
async def test_close_before_threshold_cancels(pipeline, sink, fake_clock):
    await pipeline.handle(unlock_evt("int-1"))
    await pipeline.handle(dps_evt("int-1", "open"))
    await asyncio.sleep(0.5)
    await pipeline.handle(dps_evt("int-1", "close"))
    await asyncio.sleep(2.0)
    await drain_tasks()
    assert [a for a in sink.alerts if a.alert_type == "held_open"] == []


@pytest.mark.asyncio
async def test_schedule_unlock_suppresses_then_end_starts_watchdog(pipeline, sink, fake_clock):
    await pipeline.handle(schedule_evt("int-1", activate=True))
    await pipeline.handle(dps_evt("int-1", "open"))
    await asyncio.sleep(2.2)  # would normally fire
    await drain_tasks()
    assert sink.alerts == []  # suppressed

    await pipeline.handle(schedule_evt("int-1", activate=False))
    await asyncio.sleep(0.5)
    await drain_tasks()
    assert sink.alerts == []  # watchdog still running, no fire yet

    await asyncio.sleep(2.0)
    await drain_tasks()
    held = [a for a in sink.alerts if a.alert_type == "held_open"]
    assert len(held) == 1
    assert held[0].details["source"] == "schedule_ended"


@pytest.mark.asyncio
async def test_schedule_end_with_door_closed_does_not_fire(pipeline, sink, fake_clock):
    await pipeline.handle(schedule_evt("int-1", activate=True))
    await pipeline.handle(dps_evt("int-1", "open"))
    await pipeline.handle(dps_evt("int-1", "close"))
    await pipeline.handle(schedule_evt("int-1", activate=False))
    await asyncio.sleep(2.2)
    await drain_tasks()
    assert sink.alerts == []


@pytest.mark.asyncio
async def test_temporary_unlock_suppresses_then_end_starts_watchdog(pipeline, sink, fake_clock):
    await pipeline.handle(temporary_evt("int-1", start=True))
    await pipeline.handle(dps_evt("int-1", "open"))
    await asyncio.sleep(2.2)
    await drain_tasks()
    assert sink.alerts == []

    await pipeline.handle(temporary_evt("int-1", start=False))
    await asyncio.sleep(2.2)
    await drain_tasks()
    held = [a for a in sink.alerts if a.alert_type == "held_open"]
    assert len(held) == 1
    assert held[0].details["source"] == "temporary_ended"
    # Temporary unlock carries actor; held_open should reflect it.
    assert held[0].details["last_unlock_actor"]["name"] == "Admin"


@pytest.mark.asyncio
async def test_evacuation_suppresses_then_clear_resumes(pipeline, sink, fake_clock):
    await pipeline.handle(emergency_evt("evacuation"))
    await pipeline.handle(dps_evt("int-1", "open"))
    await pipeline.handle(dps_evt("ext-1", "open"))
    await asyncio.sleep(2.2)
    await drain_tasks()
    assert sink.alerts == []

    # Clear emergency by sending any non-evacuation status.
    await pipeline.handle(emergency_evt("normal"))
    await asyncio.sleep(2.2)
    await drain_tasks()
    held = [a for a in sink.alerts if a.alert_type == "held_open"]
    assert sorted(a.door_id for a in held) == ["ext-1", "int-1"]
    assert all(a.details["source"] == "emergency_ended" for a in held)


@pytest.mark.asyncio
async def test_multiple_free_pass_sources(pipeline, sink, fake_clock):
    """If both schedule and temporary unlocks are active, ending one shouldn't fire."""
    await pipeline.handle(schedule_evt("int-1", activate=True))
    await pipeline.handle(temporary_evt("int-1", start=True))
    await pipeline.handle(dps_evt("int-1", "open"))
    await pipeline.handle(schedule_evt("int-1", activate=False))  # temp still active
    await asyncio.sleep(2.2)
    await drain_tasks()
    assert sink.alerts == []
    await pipeline.handle(temporary_evt("int-1", start=False))
    await asyncio.sleep(2.2)
    await drain_tasks()
    held = [a for a in sink.alerts if a.alert_type == "held_open"]
    assert len(held) == 1
    assert held[0].details["source"] == "temporary_ended"
