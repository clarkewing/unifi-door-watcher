from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from unifi_door_watcher.config import DoorConfig, ProtectConfig
from unifi_door_watcher.models import Alert
from unifi_door_watcher.notify.protect import ProtectAlertSink

# ---- fixtures / builders -------------------------------------------------


def make_alert(alert_type: str = "unauthorized", door_id: str = "a") -> Alert:
    return Alert(
        alert_type=alert_type,  # type: ignore[arg-type]
        door_id=door_id,
        door_name=f"Door {door_id}",
        occurred_at=datetime.now(UTC),
        details={"grace_seconds": 0},
    )


def make_cfg(**over) -> ProtectConfig:
    defaults = dict(
        request_timeout_seconds=1.0,
        retry_attempts=3,
        dedupe_window_seconds=30.0,
    )
    defaults.update(over)
    return ProtectConfig(**defaults)  # type: ignore[arg-type]


def make_door(door_id: str, *, unauth: str, held: str) -> DoorConfig:
    return DoorConfig(
        id=door_id,
        name=f"Door {door_id}",
        unauthorized_webhook_url=unauth,
        held_open_webhook_url=held,
    )


# Two doors with per-door URLs, used by the shared fixture.
DEFAULT_DOORS = {
    "a": make_door(
        "a",
        unauth="https://protect.test/a-unauth",
        held="https://protect.test/a-held",
    ),
    "b": make_door(
        "b",
        unauth="https://protect.test/b-unauth",
        held="https://protect.test/b-held",
    ),
}


@pytest.fixture
async def sink_with_transport():
    """Build a sink whose internal httpx client uses a MockTransport."""
    calls: list[httpx.Request] = []
    response_plan: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if response_plan:
            return response_plan.pop(0)
        return httpx.Response(200, json={"ok": True})

    sink = ProtectAlertSink(make_cfg(), doors_by_id=DEFAULT_DOORS)
    await sink._client.aclose()
    sink._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await sink.start()
    try:
        yield sink, calls, response_plan
    finally:
        await sink.stop()


async def _drain(sink: ProtectAlertSink) -> None:
    for _ in range(50):
        if sink._queue.empty():
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)


# ---- tests ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_routes_to_per_door_url(sink_with_transport):
    """Each (door, alert_type) has its own webhook URL from config."""
    sink, calls, _ = sink_with_transport
    await sink.send(make_alert("unauthorized", "a"))
    await sink.send(make_alert("held_open", "b"))
    await _drain(sink)
    urls = [str(c.url) for c in calls]
    assert "https://protect.test/a-unauth" in urls
    assert "https://protect.test/b-held" in urls


@pytest.mark.asyncio
async def test_dedupe_within_window(sink_with_transport):
    sink, calls, _ = sink_with_transport
    await sink.send(make_alert("unauthorized", "a"))
    await sink.send(make_alert("unauthorized", "a"))  # dup
    await _drain(sink)
    assert len(calls) == 1
    assert sink.deduped == 1


@pytest.mark.asyncio
async def test_different_doors_not_deduped(sink_with_transport):
    sink, calls, _ = sink_with_transport
    await sink.send(make_alert("unauthorized", "a"))
    await sink.send(make_alert("unauthorized", "b"))
    await _drain(sink)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_retries_on_5xx_then_succeeds(sink_with_transport):
    sink, calls, plan = sink_with_transport
    plan.extend(
        [
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    await sink.send(make_alert("unauthorized", "a"))
    for _ in range(200):
        if sink.delivered:
            break
        await asyncio.sleep(0.05)
    assert len(calls) == 3
    assert sink.delivered == 1


@pytest.mark.asyncio
async def test_gives_up_after_max_attempts(sink_with_transport):
    sink, calls, plan = sink_with_transport
    plan.extend([httpx.Response(500)] * 10)
    await sink.send(make_alert("unauthorized", "a"))
    for _ in range(200):
        if sink.failed_deliveries:
            break
        await asyncio.sleep(0.05)
    assert len(calls) == 3
    assert sink.failed_deliveries == 1
    assert sink.delivered == 0


@pytest.mark.asyncio
async def test_payload_includes_door_name_and_details(sink_with_transport):
    sink, calls, _ = sink_with_transport
    await sink.send(make_alert("unauthorized", "a"))
    await _drain(sink)
    body = calls[0].read().decode()
    assert "Door a" in body
    assert "grace_seconds" in body
    assert "alert_type" in body


@pytest.mark.asyncio
async def test_no_auth_header_when_token_unset():
    """If [protect].token isn't set, no auth header is attached — supports
    the legacy incoming-webhook URLs that embed their own secret."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    sink = ProtectAlertSink(make_cfg(), doors_by_id=DEFAULT_DOORS)
    await sink._client.aclose()
    sink._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await sink.start()
    try:
        await sink.send(make_alert("unauthorized", "a"))
        await _drain(sink)
        assert "X-API-Key" not in calls[0].headers
        assert "Authorization" not in calls[0].headers
    finally:
        await sink.stop()


@pytest.mark.asyncio
async def test_api_key_header_when_token_set():
    """Protect Integration API URLs authenticate via `X-API-Key`."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    sink = ProtectAlertSink(
        make_cfg(token="my-protect-integration-token"),
        doors_by_id=DEFAULT_DOORS,
    )
    await sink._client.aclose()
    sink._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await sink.start()
    try:
        await sink.send(make_alert("unauthorized", "a"))
        await _drain(sink)
        assert calls[0].headers.get("X-API-Key") == "my-protect-integration-token"
        # Belt-and-braces: do NOT also send Bearer.
        assert "Authorization" not in calls[0].headers
    finally:
        await sink.stop()


@pytest.mark.asyncio
async def test_alert_for_unregistered_door_is_dropped():
    """If an event fires for a door not in doors_by_id (e.g. door added
    in Access but not yet bootstrapped into config), the alert is
    dropped with an ERROR log — AppConfig validation guarantees this is
    unreachable for CONFIGURED doors, so hitting this path means a real
    misconfiguration."""
    sink = ProtectAlertSink(make_cfg(), doors_by_id=DEFAULT_DOORS)
    await sink.start()
    try:
        await sink.send(make_alert("unauthorized", "not-in-config"))
        for _ in range(50):
            if sink.failed_deliveries:
                break
            await asyncio.sleep(0.02)
        assert sink.failed_deliveries == 1
        assert sink.delivered == 0
    finally:
        await sink.stop()
