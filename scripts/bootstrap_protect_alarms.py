"""Bulk create/update Protect Alarm Manager automations — one per door per
alert type — so each door produces its own named notification.

UniFi Protect's Alarm Manager UI creates alarms via the private
`/proxy/protect/api/automations` endpoint. It authenticates via session
cookie (UniFi OS unified login), not the Integration API's X-API-Key.
This script logs in with a local Protect account, upserts one alarm per
`(door, alert_type)` pair with a descriptive `name` (used as the
notification title) and `metadata.text` (used as the notification body),
then patches the resulting webhook URLs back into config.toml under
each door's `unauthorized_webhook_url` / `held_open_webhook_url` fields.

Idempotent: alarms are matched by exact `name` (with the marker prefix
`[door-watcher]`). Re-run after adding doors, renaming them, or changing
the notification recipient list.

Usage:
    python -m scripts.bootstrap_protect_alarms --config local.config.toml --dry-run
    python -m scripts.bootstrap_protect_alarms --config local.config.toml --write

The `--dry-run` mode prints the planned changes without touching Protect
or the config file. `--write` applies both.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unifi_door_watcher.config import AppConfig, load_config

log = logging.getLogger("bootstrap_protect_alarms")

# All alarms this script creates carry this prefix in their `name` field.
# Match logic uses the full name (prefix + door name + alert-type suffix)
# so hand-created alarms with other names are never touched.
NAME_PREFIX = "[door-watcher]"

ALERT_LABELS: dict[str, tuple[str, str]] = {
    # alert_type → (title suffix, notification body template)
    "unauthorized": (
        "Unauthorized opening",
        "Unauthorized opening detected at {door_name}.",
    ),
    "held_open": (
        "Held open",
        "{door_name} has been held open beyond its threshold.",
    ),
}


# ---- naming ---------------------------------------------------------------


def alarm_name(door_name: str, alert_type: str) -> str:
    suffix, _ = ALERT_LABELS[alert_type]
    return f"{NAME_PREFIX} {door_name} — {suffix}"


def alarm_body(door_name: str, alert_type: str) -> str:
    _, template = ALERT_LABELS[alert_type]
    return template.format(door_name=door_name)


def trigger_url(host: str, webhook_uuid: str) -> str:
    return f"https://{host}/proxy/protect/integration/v1/alarm-manager/webhook/{webhook_uuid}"


# ---- Protect client -------------------------------------------------------


class ProtectClient:
    """Thin session-cookie client for Protect's private automations API.

    The public Integration API (X-API-Key) doesn't expose CRUD for
    automations — only the trigger endpoint. So the bootstrap script
    needs the same auth flow the web UI uses: POST /api/auth/login →
    JWT cookie + X-CSRF-Token header, both used on every subsequent
    request.
    """

    def __init__(self, host: str, verify_tls: bool = False) -> None:
        self._host = host
        self._client = httpx.AsyncClient(
            base_url=f"https://{host}",
            verify=verify_tls,
            timeout=30.0,
            follow_redirects=False,
        )
        self._csrf: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def login(self, username: str, password: str) -> None:
        r = await self._client.post(
            "/api/auth/login",
            json={"username": username, "password": password, "rememberMe": False},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Protect login failed ({r.status_code}): {r.text[:200]}")
        # UniFi OS returns the CSRF token as a response header on login.
        csrf = r.headers.get("x-csrf-token")
        if not csrf:
            raise RuntimeError("Protect login succeeded but no X-CSRF-Token header returned")
        self._csrf = csrf
        log.info("logged in to Protect at %s", self._host)

    def _headers(self) -> dict[str, str]:
        if self._csrf is None:
            raise RuntimeError("not logged in — call login() first")
        return {"X-CSRF-Token": self._csrf, "Accept": "application/json"}

    async def list_users(self) -> list[dict[str, Any]]:
        r = await self._client.get("/proxy/protect/api/users", headers=self._headers())
        r.raise_for_status()
        return r.json()

    async def list_automations(self) -> list[dict[str, Any]]:
        r = await self._client.get("/proxy/protect/api/automations", headers=self._headers())
        r.raise_for_status()
        return r.json()

    async def create_automation(self, payload: dict[str, Any]) -> dict[str, Any]:
        r = await self._client.post(
            "/proxy/protect/api/automations",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"Protect create automation failed ({r.status_code}): {r.text[:400]}"
            )
        return r.json()

    async def update_automation(
        self, automation_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        r = await self._client.patch(
            f"/proxy/protect/api/automations/{automation_id}",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"Protect update automation {automation_id!r} failed "
                f"({r.status_code}): {r.text[:400]}"
            )
        return r.json()


# ---- receiver resolution --------------------------------------------------


def resolve_receiver_ids(users: list[dict[str, Any]], identifiers: list[str]) -> list[str]:
    """Turn a list of user identifiers (Protect user ID, email, or full name)
    into a list of Protect user IDs. Raises on any that can't be matched.
    Case-insensitive on emails and names."""
    by_id: dict[str, dict[str, Any]] = {u.get("id", ""): u for u in users if u.get("id")}
    by_email: dict[str, dict[str, Any]] = {
        (u.get("email") or "").lower(): u for u in users if u.get("email")
    }
    by_full_name: dict[str, dict[str, Any]] = {}
    for u in users:
        first = (u.get("firstName") or "").strip()
        last = (u.get("lastName") or "").strip()
        full = f"{first} {last}".strip().lower()
        if full:
            by_full_name[full] = u

    resolved: list[str] = []
    for ident in identifiers:
        u = by_id.get(ident) or by_email.get(ident.lower()) or by_full_name.get(ident.lower())
        if u is None:
            raise RuntimeError(
                f"could not resolve Protect user {ident!r}. Available: "
                + ", ".join(
                    sorted(
                        {
                            f"{u.get('firstName', '')} {u.get('lastName', '')}".strip()
                            or u.get("email", "")
                            or u.get("id", "")
                            for u in users
                        }
                    )
                )
            )
        resolved.append(u["id"])
    return resolved


# ---- payload building -----------------------------------------------------


def make_payload(
    name: str,
    body_text: str,
    webhook_uuid: str,
    receiver_ids: list[str],
    channels: list[str],
    cooldown_seconds: int,
) -> dict[str, Any]:
    """Build an automation payload matching the shape captured from the UI."""
    return {
        "name": name,
        "enable": True,
        "sources": [],
        "conditions": [
            {
                "condition": {
                    "type": "is",
                    "source": "webhook",
                    "value": webhook_uuid,
                }
            }
        ],
        "schedules": [],
        "actions": [
            {
                "type": "SEND_NOTIFICATION",
                "metadata": {
                    "receivers": [
                        {"user": uid, "channels": channels, "schedules": []} for uid in receiver_ids
                    ],
                    "text": body_text,
                },
                "order": -1,
            }
        ],
        "isCreatedBySystem": False,
        "editable": True,
        "cooldown": {
            "enable": cooldown_seconds > 0,
            "timeout": cooldown_seconds * 1000,
        },
        "isBlockedByArmMode": False,
        "usedInArmMode": False,
        "armProfileIds": [],
    }


def existing_webhook_uuid(automation: dict[str, Any]) -> str | None:
    """Pluck the webhook UUID from an existing automation, or None if not
    a webhook-triggered alarm."""
    for c in automation.get("conditions") or []:
        cond = c.get("condition") or {}
        if cond.get("source") == "webhook":
            return cond.get("value")
    return None


# ---- config writing -------------------------------------------------------


def update_config_urls(config_path: Path, urls: dict[tuple[str, str], str]) -> int:
    """Write per-door webhook URLs back into config.toml under each
    `[[door]]` block. `urls` maps `(door_id, alert_type)` → URL. Returns
    the number of doors touched."""
    doc = tomlkit.parse(config_path.read_text())
    touched = 0
    for door_block in doc.get("door", []):
        door_id = door_block.get("id")
        if not door_id:
            continue
        u = urls.get((door_id, "unauthorized"))
        h = urls.get((door_id, "held_open"))
        if u is None and h is None:
            continue
        if u is not None:
            door_block["unauthorized_webhook_url"] = u
        if h is not None:
            door_block["held_open_webhook_url"] = h
        touched += 1
    config_path.write_text(tomlkit.dumps(doc))
    return touched


# ---- orchestration --------------------------------------------------------


async def bootstrap(config: AppConfig, config_path: Path, dry_run: bool, write_config: bool) -> int:
    if config.protect.bootstrap is None:
        raise RuntimeError(
            "no [protect.bootstrap] section in config — add host/username/"
            "password/notification_users to run this script"
        )
    boot = config.protect.bootstrap

    client = ProtectClient(host=boot.host, verify_tls=boot.verify_tls)
    try:
        await client.login(boot.username, boot.password)

        users = await client.list_users()
        receiver_ids = resolve_receiver_ids(users, boot.notification_users)
        log.info(
            "notifications will go to %d user(s): %s",
            len(receiver_ids),
            ", ".join(receiver_ids),
        )

        existing = await client.list_automations()
        by_name: dict[str, dict[str, Any]] = {a["name"]: a for a in existing}

        result_urls: dict[tuple[str, str], str] = {}
        created = updated = 0
        for door in config.doors:
            for alert_type in ("unauthorized", "held_open"):
                name = alarm_name(door.name, alert_type)
                body = alarm_body(door.name, alert_type)

                if name in by_name:
                    prev = by_name[name]
                    webhook_uuid = existing_webhook_uuid(prev) or str(uuid.uuid4())
                    payload = make_payload(
                        name,
                        body,
                        webhook_uuid,
                        receiver_ids,
                        boot.notification_channels,
                        boot.cooldown_seconds,
                    )
                    if dry_run:
                        log.info("[dry-run] UPDATE %s → %s", name, webhook_uuid)
                    else:
                        await client.update_automation(prev["id"], payload)
                        log.info("UPDATE %s → %s", name, webhook_uuid)
                    updated += 1
                else:
                    webhook_uuid = str(uuid.uuid4())
                    payload = make_payload(
                        name,
                        body,
                        webhook_uuid,
                        receiver_ids,
                        boot.notification_channels,
                        boot.cooldown_seconds,
                    )
                    if dry_run:
                        log.info("[dry-run] CREATE %s → %s", name, webhook_uuid)
                    else:
                        await client.create_automation(payload)
                        log.info("CREATE %s → %s", name, webhook_uuid)
                    created += 1

                result_urls[(door.id, alert_type)] = trigger_url(boot.host, webhook_uuid)

        log.info(
            "%s %d automations (%d created, %d updated) covering %d doors",
            "would have processed" if dry_run else "processed",
            created + updated,
            created,
            updated,
            len(config.doors),
        )

        if write_config and not dry_run:
            n = update_config_urls(config_path, result_urls)
            log.info("wrote per-door webhook URLs into %s (%d doors touched)", config_path, n)
        elif dry_run and write_config:
            log.info("[dry-run] would have written per-door URLs into %s", config_path)

        return 0
    finally:
        await client.aclose()


# ---- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bootstrap_protect_alarms")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned changes without touching Protect or the config file.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write per-door webhook URLs back into the config file after upserts.",
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    return asyncio.run(bootstrap(config, args.config, args.dry_run, args.write))


if __name__ == "__main__":
    sys.exit(main())
