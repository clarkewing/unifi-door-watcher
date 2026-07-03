from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import ProtectConfig
from ..models import Alert
from .dedupe import TTLDedupe

log = logging.getLogger(__name__)


class ProtectAlertSink:
    """Posts alerts to UniFi Protect Alarm Manager webhook URLs.

    One bounded queue, one worker — detection never blocks on Protect being
    slow. Per-alert retries with exponential backoff; dedupe on (door, type)
    within a configurable TTL.
    """

    def __init__(self, cfg: ProtectConfig) -> None:
        self._cfg = cfg
        # Protect's Alarm Manager URLs are usually on the UDM/UNVR with a
        # self-signed cert — disable TLS verification by default. We could
        # add a verify flag later if a real cert is in front.
        self._client = httpx.AsyncClient(timeout=cfg.request_timeout_seconds, verify=False)
        self._queue: asyncio.Queue[Alert] = asyncio.Queue(maxsize=256)
        self._worker: asyncio.Task[None] | None = None
        self._dedupe = TTLDedupe(cfg.dedupe_window_seconds)
        self.failed_deliveries: int = 0
        self.delivered: int = 0
        self.deduped: int = 0

    async def start(self) -> None:
        self._worker = asyncio.create_task(self._run(), name="protect-sink")

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

            try:
                await self._worker
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass

        await self._client.aclose()

    async def send(self, alert: Alert) -> None:
        key = (alert.door_id, alert.alert_type)

        if self._dedupe.seen(key):
            self.deduped += 1

            log.info(
                "deduped %s alert for %s within %.0fs window",
                alert.alert_type,
                alert.door_name,
                self._cfg.dedupe_window_seconds,
            )

            return

        try:
            self._queue.put_nowait(alert)
        except asyncio.QueueFull:
            self.failed_deliveries += 1

            log.error("protect queue full — dropping %s for %s", alert.alert_type, alert.door_name)

    async def _run(self) -> None:
        while True:
            alert = await self._queue.get()

            try:
                ok = await self._deliver_with_retries(alert)

                if ok:
                    self.delivered += 1
                else:
                    self.failed_deliveries += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("unexpected delivery error for %s", alert)
                self.failed_deliveries += 1

    def _url_for(self, alert: Alert) -> str:
        if alert.alert_type == "unauthorized":
            return str(self._cfg.unauthorized_webhook_url)
        return str(self._cfg.held_open_webhook_url)

    async def _deliver_with_retries(self, alert: Alert) -> bool:
        url = self._url_for(alert)
        body = alert.model_dump(mode="json")

        # Protect's Integration API authenticates via `X-API-Key`.
        # Legacy incoming-webhook URLs don't need any header at all —
        # leave token=None for those.
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._cfg.token:
            headers["X-API-Key"] = self._cfg.token

        backoff = 1.0

        for attempt in range(1, self._cfg.retry_attempts + 1):
            try:
                r = await self._client.post(url, json=body, headers=headers)

                if 200 <= r.status_code < 300:
                    log.info(
                        "delivered %s alert for %s (attempt %d, %d)",
                        alert.alert_type,
                        alert.door_name,
                        attempt,
                        r.status_code,
                    )

                    return True

                log.warning(
                    "protect returned %d for %s alert on %s (attempt %d)",
                    r.status_code,
                    alert.alert_type,
                    alert.door_name,
                    attempt,
                )
            except httpx.HTTPError as e:
                log.warning(
                    "protect POST failed for %s on %s (attempt %d): %s",
                    alert.alert_type,
                    alert.door_name,
                    attempt,
                    e,
                )

            if attempt < self._cfg.retry_attempts:
                await asyncio.sleep(backoff)
                backoff *= 2

        log.error(
            "giving up on %s alert for %s after %d attempts",
            alert.alert_type,
            alert.door_name,
            self._cfg.retry_attempts,
        )

        return False
