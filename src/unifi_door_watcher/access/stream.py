from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from ..config import AccessConfig
from .client import AccessClient

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]

log = logging.getLogger(__name__)


class AccessEventStream:
    """Websocket consumer with exponential reconnect and a reconciliation poll.

    On every successful (re)connect, we fetch `system/logs` for the gap window
    and replay events through the same callback before resuming the live feed.
    """

    def __init__(
        self,
        cfg: AccessConfig,
        on_event: EventCallback,
        client: AccessClient | None = None,
    ) -> None:
        self._cfg = cfg
        self._on_event = on_event
        self._client = client
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.connected: bool = False
        self.last_event_at: float | None = None  # wall-clock seconds
        # Track the most recent event timestamp we observed (epoch seconds)
        # so reconciliation knows the gap window.
        self._last_event_published: int | None = None
        # Dedupe replayed events that may overlap with the live stream.
        self._seen_event_ids: set[str] = set()

    # ----- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._stop.clear()

        self._task = asyncio.create_task(self._run_forever(), name="access-stream")

    async def stop(self) -> None:
        self._stop.set()

        if self._task is not None:
            self._task.cancel()

            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass

            self._task = None

    # ----- main loop -------------------------------------------------------

    async def _run_forever(self) -> None:
        backoff = list(self._cfg.reconnect_backoff_seconds) or [5.0]
        attempt = 0

        while not self._stop.is_set():
            try:
                await self._reconcile_if_possible()
                await self._connect_and_consume()

                attempt = 0  # reset on clean exit (e.g. server closed normally)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("ws connection error: %s", e)
            finally:
                self.connected = False

            if self._stop.is_set():
                break

            delay = backoff[min(attempt, len(backoff) - 1)]
            attempt += 1

            log.info("reconnecting to Access ws in %.0fs (attempt %d)", delay, attempt)

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                break  # stop signalled during backoff
            except TimeoutError:
                pass

    async def _connect_and_consume(self) -> None:
        ssl_ctx: ssl.SSLContext | bool

        if self._cfg.verify_tls:
            ssl_ctx = ssl.create_default_context()
        else:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        headers = {"Authorization": f"Bearer {self._cfg.token}"}

        log.info("connecting to %s", self._cfg.ws_url)

        # NOTE: ping_interval=None disables the websockets library's
        # protocol-level keepalive. UniFi Access sends its own application-
        # level keepalive (a bare "Hello" JSON string every 5s) but never
        # replies to ws PINGs, so leaving the default ping enabled causes
        # the client to close the connection with code 1011 every ~45s. Our
        # `liveness_timeout_seconds` (in _consume_loop) is the real guard:
        # any frame — including the Hellos — refreshes it.
        async with websockets.connect(
            self._cfg.ws_url,
            additional_headers=headers,
            ssl=ssl_ctx,
            ping_interval=None,
            close_timeout=5,
            max_size=2**20,
        ) as ws:
            self.connected = True
            self.last_event_at = time.time()

            log.info("Access ws connected")

            try:
                await self._consume_loop(ws)
            except ConnectionClosed as e:
                log.info("Access ws closed: %s", e)

    async def _consume_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        timeout = self._cfg.liveness_timeout_seconds

        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except TimeoutError:
                log.warning(
                    "no ws activity in %ds — closing for reconnect",
                    timeout,
                )

                await ws.close()

                return

            self.last_event_at = time.time()

            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    log.debug("dropping non-utf8 ws frame")

                    continue

            # Full untruncated frame at debug — the `websockets` library's
            # own logger trims to ~80 chars, which makes large payloads
            # unrecoverable. Ours doesn't.
            log.debug("raw ws frame (%d bytes): %s", len(raw), raw)

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.debug("non-json ws frame: %r", raw[:120])

                continue

            await self._dispatch(msg)

    async def _dispatch(self, msg: Any) -> None:
        # The controller occasionally sends non-object frames (JSON strings or
        # numbers used as keepalives/hellos). We only care about object-shaped
        # event payloads — silently ignore everything else.
        if not isinstance(msg, dict):
            log.debug(
                "ignoring non-object ws frame: %r", msg if not isinstance(msg, str) else msg[:120]
            )
            return

        # Some Access deployments wrap events in {"type":"event","data":{...}}.
        # Others emit the event payload directly with an "event" field. Handle
        # both shapes.
        if "event" in msg:
            payload = msg
        elif isinstance(msg.get("data"), dict) and "event" in msg["data"]:
            payload = msg["data"]
        else:
            log.debug("ignoring ws message without event field: %s", list(msg)[:5])

            return

        event_id = self._event_id(payload)

        if event_id and event_id in self._seen_event_ids:
            return

        if event_id:
            self._remember(event_id)

        published = self._published_seconds(payload)

        if published is not None:
            if self._last_event_published is None or published > self._last_event_published:
                self._last_event_published = published

        await self._on_event(payload)

    # ----- reconciliation --------------------------------------------------

    async def _reconcile_if_possible(self) -> None:
        if self._client is None or self._last_event_published is None:
            return

        until = int(time.time())
        since = max(
            self._last_event_published - 5,
            until - self._cfg.reconcile_lookback_seconds,
        )

        if since >= until:
            return

        try:
            hits = await self._client.fetch_door_openings(since, until)
        except Exception as e:
            log.warning("reconciliation poll failed: %s", e)

            return

        log.info("reconciliation: %d events since %d", len(hits), since)

        for hit in hits:
            payload = self._hit_to_event(hit)

            if payload is None:
                continue

            event_id = self._event_id(payload)

            if event_id and event_id in self._seen_event_ids:
                continue

            if event_id:
                self._remember(event_id)

            await self._on_event(payload)

    # ----- helpers ---------------------------------------------------------

    def _remember(self, event_id: str) -> None:
        self._seen_event_ids.add(event_id)

        # Keep the set bounded.
        if len(self._seen_event_ids) > 4096:
            for _ in range(1024):
                self._seen_event_ids.pop()

    @staticmethod
    def _event_id(payload: dict[str, Any]) -> str | None:
        # Multiple shapes seen in the wild — try a few candidates.
        for key in ("id", "event_id"):
            v = payload.get(key)
            if v:
                return str(v)

        data = payload.get("data") or {}
        for key in ("id", "event_id", "_id"):
            v = data.get(key)
            if v:
                return str(v)

        # Fallback: synthesize from (event, door, published).
        ev = payload.get("event")
        loc_id = (data.get("location") or {}).get("id")
        pub = data.get("published") or payload.get("published")
        if ev and pub:
            return f"{ev}:{loc_id}:{pub}"
        return None

    @staticmethod
    def _published_seconds(payload: dict[str, Any]) -> int | None:
        data = payload.get("data") or {}

        for src in (data.get("published"), payload.get("published")):
            if isinstance(src, (int, float)):
                # API uses milliseconds for `published` in system/logs hits.
                return int(src // 1000 if src > 1_000_000_000_000 else src)

        return None

    @staticmethod
    def _hit_to_event(hit: dict[str, Any]) -> dict[str, Any] | None:
        """Convert a system/logs hit into the same shape the ws callback expects."""
        evt = hit.get("event") or {}
        kind = evt.get("type")
        if not kind:
            return None

        target = hit.get("target") or []
        door = next((t for t in target if (t.get("type") or "").startswith("door")), None) or (
            target[0] if target else {}
        )
        actor = hit.get("actor") or {}

        return {
            "event": kind,
            "data": {
                "location": {
                    "id": door.get("id"),
                    "name": door.get("display_name") or door.get("name"),
                },
                "actor": {
                    "id": actor.get("id"),
                    "name": actor.get("display_name") or actor.get("name"),
                },
                "object": {
                    "result": evt.get("result"),
                    "authentication_type": evt.get("authentication_type"),
                    # `system/logs` doesn't carry DPS open/close directly;
                    # we mainly rely on it to backfill unlock events.
                },
                "published": evt.get("published"),
            },
            "id": evt.get("id") or hit.get("id"),
        }
