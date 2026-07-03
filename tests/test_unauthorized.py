from __future__ import annotations

import pytest

from tests.conftest import dps_evt, drain_tasks, remote_unlock_evt, unlock_evt


@pytest.mark.asyncio
async def test_open_within_grace_after_unlock_no_alert(pipeline, sink, fake_clock):
    await pipeline.handle(unlock_evt("int-1"))
    fake_clock.tick(5)  # < grace_seconds (8)
    await pipeline.handle(dps_evt("int-1", "open"))
    await drain_tasks()
    assert [a.alert_type for a in sink.alerts] == []


@pytest.mark.asyncio
async def test_open_after_grace_fires_unauthorized(pipeline, sink, fake_clock):
    await pipeline.handle(unlock_evt("int-1"))
    fake_clock.tick(20)  # > grace_seconds (8)
    await pipeline.handle(dps_evt("int-1", "open"))
    await drain_tasks()
    types = [a.alert_type for a in sink.alerts]
    assert types == ["unauthorized"]
    assert sink.alerts[0].door_id == "int-1"


@pytest.mark.asyncio
async def test_exterior_strict_any_open_without_simultaneous_unlock_fires(
    pipeline, sink, fake_clock
):
    # ext-1 has grace_seconds=0 — even a stale unlock is too late.
    await pipeline.handle(unlock_evt("ext-1"))
    fake_clock.tick(1)
    await pipeline.handle(dps_evt("ext-1", "open"))
    await drain_tasks()
    assert [a.alert_type for a in sink.alerts] == ["unauthorized"]


@pytest.mark.asyncio
async def test_ren_doorbell_does_not_authorize_open(pipeline, sink, fake_clock):
    """REN is Request-to-Enter (the outside doorbell button), not Request-
    to-Exit. A doorbell ring is just a notification — it must NOT mark the
    door as legitimately unlocked. A subsequent open without a real unlock
    should still fire `unauthorized`."""
    # Synthesize a raw doorbell event (no helper because the pipeline drops it).
    await pipeline.handle(
        {
            "event": "access.doorbell.incoming.REN",
            "data": {"location": {"id": "int-1", "name": "int-1"}},
        }
    )
    fake_clock.tick(2)
    await pipeline.handle(dps_evt("int-1", "open"))
    await drain_tasks()
    assert [a.alert_type for a in sink.alerts] == ["unauthorized"]


@pytest.mark.asyncio
async def test_remote_unlock_authorizes_open_within_grace(pipeline, sink, fake_clock):
    """Admin/portal-initiated unlocks arrive as `access.data.device.remote_unlock`
    with a different payload shape (door fields directly on `data`). They must
    be treated as legitimate unlocks."""
    await pipeline.handle(remote_unlock_evt("int-1", actor_name="Hugo"))
    fake_clock.tick(3)
    await pipeline.handle(dps_evt("int-1", "open"))
    await drain_tasks()
    assert sink.alerts == []


@pytest.mark.asyncio
async def test_remote_unlock_without_actor_still_authorizes(pipeline, sink, fake_clock):
    """The doc's example payload omits the actor field entirely. Authorization
    must still work — the alert payload just won't have actor info."""
    await pipeline.handle(remote_unlock_evt("int-1", actor_name=None))
    fake_clock.tick(3)
    await pipeline.handle(dps_evt("int-1", "open"))
    await drain_tasks()
    assert sink.alerts == []


@pytest.mark.asyncio
async def test_duplicate_open_events_dedupe_within_one_open(pipeline, sink, fake_clock):
    await pipeline.handle(dps_evt("ext-1", "open"))
    await pipeline.handle(dps_evt("ext-1", "open"))
    await drain_tasks()
    assert len([a for a in sink.alerts if a.alert_type == "unauthorized"]) == 1


@pytest.mark.asyncio
async def test_close_then_reopen_fires_again(pipeline, sink, fake_clock):
    await pipeline.handle(dps_evt("ext-1", "open"))
    await pipeline.handle(dps_evt("ext-1", "close"))
    await pipeline.handle(dps_evt("ext-1", "open"))
    await drain_tasks()
    assert len([a for a in sink.alerts if a.alert_type == "unauthorized"]) == 2


@pytest.mark.asyncio
async def test_denied_unlock_does_not_authorize(pipeline, sink, fake_clock):
    # Same shape as a granted unlock but result=Access Denied
    evt = unlock_evt("int-1")
    evt["data"]["object"]["result"] = "Access Denied"
    await pipeline.handle(evt)
    fake_clock.tick(1)
    await pipeline.handle(dps_evt("int-1", "open"))
    await drain_tasks()
    assert [a.alert_type for a in sink.alerts] == ["unauthorized"]
