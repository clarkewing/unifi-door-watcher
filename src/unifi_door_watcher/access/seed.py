"""Startup state seeding.

Without this, the service comes up with `dps=unknown` and no awareness of
in-progress unlock schedules or temporary unlocks. Practical consequences:

  * A door that was open at restart never triggers `held_open` because we
    never saw the `dps_status: open` event that schedules the watchdog.
  * A door under an active scheduled unlock at restart — we miss the
    `unlock_schedule.activate` event, so when the schedule ends and the
    `.deactivate` arrives, our `schedule_unlock_active` flag was `False`
    all along and the held-open synthesis in `_free_pass_end` doesn't fire.

The seed runs once at lifespan startup, after the sink is up but before
the ws stream subscribes. Failures here log and continue — a degraded but
running service is better than a service that won't start because the
controller was briefly unreachable."""

from __future__ import annotations

import logging
from typing import Any

from ..detect.pipeline import DetectionPipeline
from ..state import DoorStateRegistry
from .client import AccessClient

log = logging.getLogger(__name__)


# `data.type` values returned by `/api/v1/developer/doors/:id/lock_rule`
# (see API doc §7.11). `keep_lock` and `lock_early` mean the door is
# currently locked (or transitioning to it) — no free-pass for us to track.
_SCHEDULE_TYPES = frozenset({"schedule"})
_TEMPORARY_TYPES = frozenset({"keep_unlock", "custom"})


async def seed_initial_state(
    pipeline: DetectionPipeline,
    client: AccessClient,
    registry: DoorStateRegistry,
) -> None:
    try:
        doors = await client.list_doors()
    except Exception as e:
        log.warning("startup seed: failed to list doors (%s) — running with empty state", e)

        return

    seeded = 0
    skipped_unknown = 0

    for door_payload in doors:
        door_id = door_payload.get("id")

        if not door_id:
            continue

        if door_id not in registry.doors_by_id:
            # Door exists in UniFi but not in our config. Common after a
            # door is added in the UI without re-running bootstrap_doors.
            skipped_unknown += 1

            continue

        dps = _coerce_dps(door_payload.get("door_position_status"))
        schedule_active, temp_active = await _classify_lock_rule(
            client, door_id, door_payload.get("door_lock_relay_status")
        )

        await pipeline.seed_door(
            door_id,
            dps=dps,
            schedule_unlock_active=schedule_active,
            temporary_unlock_active=temp_active,
        )

        seeded += 1

    log.info(
        "startup seed complete: %d configured doors seeded, %d unknown doors skipped",
        seeded,
        skipped_unknown,
    )


def _coerce_dps(raw: Any) -> str | None:
    """Map the controller's DPS field to our internal state.

    `door_position_status` is either "open", "close", or `null` (no DPS
    sensor wired). The bootstrap script already filters DPS-less doors
    out of the config, but defending against drift is cheap."""
    if isinstance(raw, str):
        v = raw.lower()

        if v in ("open", "close"):
            return v

    return None


async def _classify_lock_rule(
    client: AccessClient, door_id: str, lock_relay_status: Any
) -> tuple[bool, bool]:
    """Returns `(schedule_active, temporary_active)`. Only queries the
    lock_rule endpoint when the relay is currently unlocked — a locked
    door has nothing to classify."""
    if (lock_relay_status or "").lower() != "unlock":
        return False, False

    try:
        rule = await client.fetch_lock_rule(door_id)
    except Exception as e:
        log.warning("startup seed: failed to fetch lock_rule for %s: %s", door_id, e)

        return False, False

    rule_type = (rule.get("type") or "").lower()

    if rule_type in _SCHEDULE_TYPES:
        return True, False

    if rule_type in _TEMPORARY_TYPES:
        return False, True

    return False, False
