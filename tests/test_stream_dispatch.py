"""Tests for AccessEventStream._dispatch — focused on the parsing layer's
robustness to weird ws frames the controller has been observed to send."""

from __future__ import annotations

import pytest

from unifi_door_watcher.access.stream import AccessEventStream
from unifi_door_watcher.config import AccessConfig


def _make_stream(events_seen: list[dict]) -> AccessEventStream:
    cfg = AccessConfig(host="x", token="t")

    async def on_event(payload: dict) -> None:
        events_seen.append(payload)

    return AccessEventStream(cfg, on_event=on_event)


@pytest.mark.asyncio
async def test_dispatch_ignores_string_frame():
    """UniFi Access sometimes sends bare JSON strings as keepalives. They
    must not crash the consumer (regression: `'str' object has no attribute
    'get'`)."""
    seen: list[dict] = []
    stream = _make_stream(seen)
    await stream._dispatch("keepalive")
    await stream._dispatch("")
    assert seen == []


@pytest.mark.asyncio
async def test_dispatch_ignores_list_and_number_frames():
    seen: list[dict] = []
    stream = _make_stream(seen)
    await stream._dispatch([{"event": "noise"}])
    await stream._dispatch(42)
    await stream._dispatch(None)
    assert seen == []


@pytest.mark.asyncio
async def test_dispatch_passes_through_event_object():
    seen: list[dict] = []
    stream = _make_stream(seen)
    payload = {
        "event": "access.door.unlock",
        "data": {"location": {"id": "d1", "name": "Front"}},
    }
    await stream._dispatch(payload)
    assert seen == [payload]


@pytest.mark.asyncio
async def test_dispatch_unwraps_wrapped_event():
    seen: list[dict] = []
    stream = _make_stream(seen)
    inner = {"event": "access.door.unlock", "data": {"location": {"id": "d1"}}}
    await stream._dispatch({"type": "event", "data": inner})
    assert seen == [inner]


@pytest.mark.asyncio
async def test_dispatch_dedupes_by_event_id():
    seen: list[dict] = []
    stream = _make_stream(seen)
    payload = {"event": "access.door.unlock", "id": "evt-1", "data": {"location": {"id": "d1"}}}
    await stream._dispatch(payload)
    await stream._dispatch(payload)
    assert len(seen) == 1
