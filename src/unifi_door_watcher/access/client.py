from __future__ import annotations

from typing import Any

import httpx

from ..config import AccessConfig


class AccessClient:
    """Thin async REST client for the UniFi Access developer API.

    Only the endpoints used by the watcher are implemented here. The websocket
    consumer lives in `stream.py` so this client can be reused by the bootstrap
    script and the reconciliation poll without pulling in ws machinery.
    """

    def __init__(self, cfg: AccessConfig) -> None:
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.token}"},
            verify=cfg.verify_tls,
            timeout=10.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_doors(self) -> list[dict[str, Any]]:
        r = await self._client.get("/api/v1/developer/doors")

        r.raise_for_status()

        return r.json().get("data", [])

    async def fetch_lock_rule(self, door_id: str) -> dict[str, Any]:
        r = await self._client.get(f"/api/v1/developer/doors/{door_id}/lock_rule")

        r.raise_for_status()

        return r.json().get("data", {})

    async def fetch_door_openings(
        self, since: int, until: int, page_size: int = 200
    ) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        page = 1

        while True:
            r = await self._client.post(
                "/api/v1/developer/system/logs",
                params={"page_num": page, "page_size": page_size},
                json={"topic": "door_openings", "since": since, "until": until},
            )

            r.raise_for_status()

            data = r.json().get("data", {})
            chunk = data.get("hits", [])

            hits.extend(chunk)

            if len(chunk) < page_size:
                break

            page += 1

        return hits
