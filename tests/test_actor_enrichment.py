"""Tests for the `access.logs.add` actor-enrichment path.

A remote unlock fires `access.data.device.remote_unlock` (often without an
actor in the payload) followed ~1s later by `access.logs.add` (with full
actor info). The two events share the same `event_object_id` (UAH device
ID), and that's what the pipeline correlates on — no name matching, so the
operator can rename `name` in config freely."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import (
    access_log_evt,
    dps_evt,
    drain_tasks,
    remote_unlock_evt,
    unlock_evt,
)
from unifi_door_watcher.config import DoorConfig
from unifi_door_watcher.detect.pipeline import DetectionPipeline
from unifi_door_watcher.state import DoorStateRegistry


@pytest.mark.asyncio
async def test_remote_unlock_followed_by_access_log_attributes_actor(
    pipeline, registry, fake_clock
):
    await pipeline.handle(remote_unlock_evt("int-1", event_object_id="uah-a"))
    state = registry.states["int-1"]
    # Before access.logs.add arrives: method recorded, actor empty.
    assert state.last_unlock_method == "remote"
    assert state.last_unlock_actor_id is None
    assert state.last_unlock_event_object_id == "uah-a"

    fake_clock.tick(1)
    await pipeline.handle(access_log_evt(event_object_id="uah-a", actor_name="Alice Example"))

    assert state.last_unlock_actor_id == "user-uuid"
    assert state.last_unlock_actor_name == "Alice Example"


@pytest.mark.asyncio
async def test_access_log_does_not_overwrite_existing_actor(pipeline, registry):
    """A badge unlock (`access.door.unlock`) carries actor inline and does
    not stash an event_object_id — so the matching access.logs.add can't
    correlate and can't overwrite. This also guards against any future
    code path that might wire that correlation up."""
    await pipeline.handle(unlock_evt("int-1", method="NFC", actor_name="Jane Doe"))
    await pipeline.handle(
        access_log_evt(event_object_id="uah-a", actor_id="someone-else", actor_name="Someone Else")
    )
    state = registry.states["int-1"]
    assert state.last_unlock_actor_id == "u1"
    assert state.last_unlock_actor_name == "Jane Doe"


@pytest.mark.asyncio
async def test_access_log_with_no_matching_recent_unlock_is_ignored(pipeline, registry, fake_clock):
    """If no remote unlock preceded the logs.add (e.g. we missed it during
    reconnect), nothing to attribute — leave state alone."""
    await pipeline.handle(access_log_evt(event_object_id="uah-unknown", actor_name="Hugo"))
    state = registry.states["int-1"]
    assert state.last_unlock_at is None
    assert state.last_unlock_actor_id is None


@pytest.mark.asyncio
async def test_access_log_outside_window_does_not_attribute(pipeline, registry, fake_clock):
    """If the logs.add arrives long after the unlock (e.g. severely delayed
    delivery), the temporal correlation no longer holds — don't attribute."""
    await pipeline.handle(remote_unlock_evt("int-1", event_object_id="uah-a"))
    fake_clock.tick(30)  # > 10s window
    await pipeline.handle(access_log_evt(event_object_id="uah-a", actor_name="Hugo"))
    state = registry.states["int-1"]
    assert state.last_unlock_actor_id is None


@pytest.mark.asyncio
async def test_access_log_mismatched_event_object_id_is_ignored(pipeline, registry):
    """If the UAH IDs don't line up (e.g. logs.add for a different door),
    no attribution happens."""
    await pipeline.handle(remote_unlock_evt("int-1", event_object_id="uah-a"))
    await pipeline.handle(access_log_evt(event_object_id="uah-b", actor_name="Hugo"))
    state = registry.states["int-1"]
    assert state.last_unlock_actor_id is None


@pytest.mark.asyncio
async def test_access_log_non_access_result_ignored(pipeline, registry):
    """`access.logs.add` is also emitted for *denied* attempts. Those must
    not attribute an unlock (since no unlock happened)."""
    await pipeline.handle(remote_unlock_evt("int-1", event_object_id="uah-a"))
    await pipeline.handle(
        access_log_evt(event_object_id="uah-a", actor_name="Hugo", result="BLOCKED")
    )
    state = registry.states["int-1"]
    assert state.last_unlock_actor_id is None


@pytest.mark.asyncio
async def test_correlation_works_after_renaming_door_in_config(sink, fake_clock):
    """Operator changes `name` in config for friendlier alerts. The watcher
    correlates by `event_object_id`, not name — so enrichment still works
    and the alert payload carries the new operator-facing name."""
    doors = [
        DoorConfig(
            id="x",
            name="Server Room",
            grace_seconds=8,
            held_open_seconds=2,
        )
    ]
    registry = DoorStateRegistry(doors_by_id={d.id: d for d in doors})
    pipeline = DetectionPipeline(registry, sink, clock=fake_clock)  # type: ignore[arg-type]

    await pipeline.handle(remote_unlock_evt("x", event_object_id="uah-a"))
    await pipeline.handle(access_log_evt(event_object_id="uah-a", actor_name="Hugo"))
    state = registry.states["x"]
    assert state.last_unlock_actor_name == "Hugo"

    await pipeline.handle(dps_evt("x", "open"))
    await asyncio.sleep(2.2)
    await drain_tasks()
    held = [a for a in sink.alerts if a.alert_type == "held_open"]
    assert len(held) == 1
    assert held[0].door_name == "Server Room"  # operator-facing
    assert held[0].details["last_unlock_actor"]["name"] == "Hugo"


@pytest.mark.asyncio
async def test_held_open_alert_includes_enriched_actor(pipeline, sink, registry, fake_clock):
    """End-to-end: remote unlock → logs.add → DPS open → held_open. The
    Protect alert payload should carry the attributed actor."""
    await pipeline.handle(remote_unlock_evt("int-1", event_object_id="uah-a"))
    await pipeline.handle(access_log_evt(event_object_id="uah-a", actor_name="Alice Example"))
    await pipeline.handle(dps_evt("int-1", "open"))
    await asyncio.sleep(2.2)  # held_open_seconds = 2 in fixture
    await drain_tasks()
    held = [a for a in sink.alerts if a.alert_type == "held_open"]
    assert len(held) == 1
    assert held[0].details["last_unlock_method"] == "remote"
    assert held[0].details["last_unlock_actor"] == {
        "id": "user-uuid",
        "name": "Alice Example",
    }
