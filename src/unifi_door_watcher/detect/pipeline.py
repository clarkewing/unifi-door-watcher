from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from ..clock import Clock, default_clock
from ..config import DoorConfig
from ..models import Alert
from ..notify.protect import ProtectAlertSink
from ..state import DoorState, DoorStateRegistry

log = logging.getLogger(__name__)


# --- low-level field plucking ------------------------------------------------


def _door_id(payload: dict) -> str | None:
    """Pluck the door UUID. Most events nest it under `data.location.id`,
    but `access.data.device.remote_unlock` puts it at `data.unique_id`."""
    data = payload.get("data") or {}
    loc_id = (data.get("location") or {}).get("id")
    if loc_id:
        return loc_id
    # remote_unlock shape: door fields live directly on `data`.
    if (data.get("location_type") or "").lower() == "door":
        return data.get("unique_id")
    return None


def _door_name(payload: dict) -> str | None:
    data = payload.get("data") or {}
    return (data.get("location") or {}).get("name") or data.get("name")


def _actor(payload: dict) -> tuple[str | None, str | None]:
    actor = (payload.get("data") or {}).get("actor") or {}
    return actor.get("id"), actor.get("name")


def _obj(payload: dict) -> dict:
    return (payload.get("data") or {}).get("object") or {}


# --- pipeline ---------------------------------------------------------------


class DetectionPipeline:
    """Routes parsed Access events to the per-door state machine and emits alerts."""

    def __init__(
        self,
        registry: DoorStateRegistry,
        sink: ProtectAlertSink,
        clock: Clock | None = None,
    ) -> None:
        self._registry = registry
        self._sink = sink
        self._clock = clock or default_clock()
        # Hold strong refs to fire-and-forget delivery tasks so the GC doesn't
        # cancel them mid-flight; discard each on completion.
        self._pending_emits: set[asyncio.Task[None]] = set()
        # NOTE: `access.doorbell.incoming.REN` is intentionally NOT handled.
        # REN = Request-to-ENTER (the outside doorbell button), not Request-
        # to-Exit. A doorbell ring does not authorize a door open — the
        # request still has to be granted via a normal unlock. Interior-door
        # handle-side egress without any electrical signal is absorbed by
        # the per-door `grace_seconds` instead.
        self._handlers = {
            "access.door.unlock": self._on_unlock_event,
            "access.data.device.remote_unlock": self._on_remote_unlock,
            "access.logs.add": self._on_access_log,
            "access.device.dps_status": self._on_dps,
            "access.unlock_schedule.activate": self._on_schedule_activate,
            "access.unlock_schedule.deactivate": self._on_schedule_deactivate,
            "access.temporary_unlock.start": self._on_temporary_start,
            "access.temporary_unlock.end": self._on_temporary_end,
            "access.device.emergency_status": self._on_emergency,
        }

    async def handle(self, event: dict) -> None:
        kind = event.get("event")
        handler = self._handlers.get(kind or "")

        if not handler:
            log.debug("ignoring event %s", kind)

            return

        try:
            await handler(event)
        except Exception:  # pragma: no cover
            log.exception("handler %s failed for event: %r", kind, event)

    # ---- internal helpers -------------------------------------------------

    def _resolve(self, payload: dict) -> tuple[DoorConfig, DoorState] | None:
        door_id = _door_id(payload)

        if not door_id:
            log.debug("event without door id: %s", payload.get("event"))

            return None

        door = self._registry.doors_by_id.get(door_id)

        if not door:
            log.warning(
                "event for unconfigured door %s (%s) — rerun bootstrap_doors",
                door_id,
                _door_name(payload),
            )

            return None

        state = self._registry.states[door_id]

        return door, state

    def _now(self) -> float:
        return self._clock.now()

    # ---- startup seeding --------------------------------------------------

    async def seed_door(
        self,
        door_id: str,
        *,
        dps: str | None,
        schedule_unlock_active: bool = False,
        temporary_unlock_active: bool = False,
    ) -> None:
        """Apply a startup state snapshot for one door.

        Called by `access.seed.seed_initial_state` after polling the REST
        API but before the live ws stream is consumed.

        Open-door handling delegates to `_handle_open` with the synthetic
        source `"startup_open"`. That reuses the existing logic that
        already does the right thing for non-`watchdog` sources: skips the
        unauthorized check (no prior unlock context exists), respects the
        free-pass flags (no watchdog while a schedule/temp unlock is
        active), and schedules a held-open watchdog otherwise. It's the
        same pattern the free-pass-end handlers use.
        """
        state = self._registry.states.get(door_id)

        if state is None:
            return

        state.schedule_unlock_active = schedule_unlock_active
        state.temporary_unlock_active = temporary_unlock_active

        if dps not in ("open", "close"):
            return

        if dps == "close":
            state.dps = "close"
            return

        # dps == "open" — log a noteworthy line then delegate.
        door = self._registry.doors_by_id[door_id]

        if self._registry.free_pass(state):
            log.info(
                "door %s open at startup under active free-pass (%s) — "
                "watchdog deferred until free-pass ends",
                door.name,
                self._free_pass_label(state),
            )
        else:
            log.warning(
                "door %s open at startup with no free-pass — held_open will "
                "fire in %ds if still open",
                door.name,
                door.held_open_seconds or 0,
            )

        await self._handle_open(door, state, self._now(), source="startup_open")

    def _emit(
        self,
        alert_type: str,
        door: DoorConfig,
        details: dict[str, Any],
    ) -> None:
        alert = Alert(
            alert_type=alert_type,  # type: ignore[arg-type]
            door_id=door.id,
            door_name=door.name,
            occurred_at=datetime.now(UTC),
            details=details,
        )

        # Fire-and-forget — the sink's `send()` returns immediately after
        # queueing (or dedupe). Keep a strong ref so the task can't be GC'd.
        task = asyncio.create_task(self._sink.send(alert))
        self._pending_emits.add(task)
        task.add_done_callback(self._pending_emits.discard)

    # ---- unlock handlers --------------------------------------------------

    async def _on_unlock_event(self, payload: dict) -> None:
        resolved = self._resolve(payload)

        if not resolved:
            return

        door, state = resolved
        obj = _obj(payload)
        result = (obj.get("result") or "").lower()

        # Only Access-Granted unlocks count. "Access Denied" / "BLOCKED" should
        # not authorize a subsequent open.
        if result and "grant" not in result and "success" not in result:
            log.debug("ignoring non-granted unlock for %s: %r", door.name, obj.get("result"))

            return

        actor_id, actor_name = _actor(payload)

        state.last_unlock_at = self._now()
        state.last_unlock_method = obj.get("authentication_type") or "unknown"
        state.last_unlock_actor_id = actor_id
        state.last_unlock_actor_name = actor_name

        log.info("unlock %s on %s by %s", state.last_unlock_method, door.name, actor_name or "?")

    async def _on_remote_unlock(self, payload: dict) -> None:
        """Admin/API-initiated unlock via the portal, mobile app, or our own
        REST `/doors/:id/unlock` call. Counts as a legitimate unlock for
        grace-period purposes. The payload shape differs from `access.door
        .unlock` — door fields live directly on `data`, and actor info may
        or may not be present depending on the initiator."""
        resolved = self._resolve(payload)

        if not resolved:
            return

        door, state = resolved
        actor_id, actor_name = _actor(payload)

        state.last_unlock_at = self._now()
        state.last_unlock_method = "remote"
        state.last_unlock_actor_id = actor_id
        state.last_unlock_actor_name = actor_name
        # Stash the UAH device ID so `_on_access_log` can attribute the
        # actor when the audit-log event arrives ~1s later.
        state.last_unlock_event_object_id = payload.get("event_object_id")

        log.info("remote unlock on %s by %s", door.name, actor_name or "(unknown)")

    async def _on_access_log(self, payload: dict) -> None:
        """Enrich `last_unlock_actor` from the canonical audit-log event.

        `access.logs.add` arrives ~1 second after `access.data.device
        .remote_unlock` and is the only event in that sequence that carries
        the actor's display name. We don't use it to *create* an unlock —
        that's the job of `_on_unlock_event` / `_on_remote_unlock`, which
        fire immediately and so cover grace_seconds=0 doors.

        Door identity is via the `event_object_id` field (the UAH device
        ID), which `_on_remote_unlock` stashed on the door state. This
        avoids any reliance on display-name matching, so the operator can
        rename `name` in config freely.
        """
        source = (payload.get("data") or {}).get("_source") or {}
        event_info = source.get("event") or {}

        if (event_info.get("result") or "").upper() != "ACCESS":
            return

        event_object_id = payload.get("event_object_id")
        if not event_object_id:
            return

        # 10s window is generous — the logs.add event typically lands within
        # ~1s of the unlock that produced it.
        state = self._registry.find_recent_unlock_by_event_object_id(
            event_object_id, max_age=10, now=self._now()
        )
        if state is None:
            log.debug(
                "access.logs.add with no matching recent unlock (event_object_id=%s)",
                event_object_id,
            )

            return

        # Already attributed (e.g. came from access.door.unlock which carries
        # actor inline). Don't overwrite — keeps the per-event handlers as
        # source of truth where they have the info.
        if state.last_unlock_actor_id is not None:
            return

        actor = source.get("actor") or {}
        state.last_unlock_actor_id = actor.get("id")
        state.last_unlock_actor_name = actor.get("display_name")
        door = self._registry.doors_by_id[state.door_id]

        log.info(
            "attributed %s unlock on %s to %s",
            state.last_unlock_method or "?",
            door.name,
            state.last_unlock_actor_name or "(unknown)",
        )

    # ---- DPS handlers -----------------------------------------------------

    async def _on_dps(self, payload: dict) -> None:
        resolved = self._resolve(payload)

        if not resolved:
            return

        door, state = resolved
        obj = _obj(payload)
        status = (obj.get("status") or "").lower()
        t = self._now()

        if status == "open":
            await self._handle_open(door, state, t, source="watchdog")
        elif status == "close":
            await self._handle_close(door, state, t)
        else:
            log.debug("dps_status with unknown status %r for %s", status, door.name)

    async def _handle_open(
        self,
        door: DoorConfig,
        state: DoorState,
        t: float,
        source: str,
    ) -> None:
        state.dps = "open"
        state.opened_at = t
        state.held_open_source = source

        # While free-pass is active, suppress alerts entirely. The held-open
        # watchdog is started by the free-pass-end handler instead.
        if self._registry.free_pass(state):
            log.debug(
                "open on %s during free-pass (%s) — suppressing alerts",
                door.name,
                self._free_pass_label(state),
            )

            return

        # Unauthorized check — only on the natural path (not on synthetic opens
        # spawned by a free-pass-end transition).
        if source == "watchdog":
            grace = door.grace_seconds or 0
            authorized = state.last_unlock_at is not None and (t - state.last_unlock_at) <= grace

            if not authorized and not state.unauthorized_fired:
                state.unauthorized_fired = True

                self._emit(
                    "unauthorized",
                    door,
                    {
                        "last_unlock_at": _epoch(state.last_unlock_at, t),
                        "last_unlock_method": state.last_unlock_method,
                        "last_unlock_actor": _actor_blob(state),
                        "grace_seconds": grace,
                    },
                )

        # Schedule the held-open watchdog (cancels any previous).
        self._cancel_held_open(state)
        held = door.held_open_seconds or 0

        state.held_open_task = asyncio.create_task(
            self._held_open_watchdog(door, state, fire_in=held, source=source),
            name=f"held-open:{door.id}",
        )

    async def _handle_close(self, door: DoorConfig, state: DoorState, t: float) -> None:
        state.dps = "close"
        state.opened_at = None
        state.unauthorized_fired = False
        state.held_open_fired = False
        state.held_open_source = "watchdog"

        self._cancel_held_open(state)

        log.debug("close on %s", door.name)

    def _cancel_held_open(self, state: DoorState) -> None:
        if state.held_open_task is not None and not state.held_open_task.done():
            state.held_open_task.cancel()

        state.held_open_task = None

    async def _held_open_watchdog(
        self, door: DoorConfig, state: DoorState, fire_in: float, source: str
    ) -> None:
        try:
            await asyncio.sleep(fire_in)
        except asyncio.CancelledError:
            return

        # State may have changed while we slept.
        if state.dps != "open" or state.held_open_fired:
            return

        if self._registry.free_pass(state):
            log.debug("held-open suppressed for %s — free-pass became active", door.name)

            return

        state.held_open_fired = True

        now = self._now()
        opened_dt = (
            datetime.fromtimestamp(_epoch_seconds_from_monotonic(state.opened_at, now), UTC)
            if state.opened_at is not None
            else None
        )

        self._emit(
            "held_open",
            door,
            {
                "source": source,
                "opened_at": opened_dt.isoformat() if opened_dt else None,
                "held_open_seconds": door.held_open_seconds,
                "last_unlock_at": _epoch(state.last_unlock_at, now),
                "last_unlock_method": state.last_unlock_method,
                "last_unlock_actor": _actor_blob(state),
            },
        )

    # ---- free-pass handlers -----------------------------------------------

    async def _on_schedule_activate(self, payload: dict) -> None:
        resolved = self._resolve(payload)

        if not resolved:
            return

        _, state = resolved
        state.schedule_unlock_active = True

        # If the held-open watchdog was already running, cancel it — door is
        # legitimately unlocked now.
        self._cancel_held_open(state)

    async def _on_schedule_deactivate(self, payload: dict) -> None:
        await self._free_pass_end(payload, "schedule")

    async def _on_temporary_start(self, payload: dict) -> None:
        resolved = self._resolve(payload)

        if not resolved:
            return

        _, state = resolved
        state.temporary_unlock_active = True
        actor_id, actor_name = _actor(payload)
        state.last_unlock_at = self._now()
        state.last_unlock_method = "temporary"
        state.last_unlock_actor_id = actor_id
        state.last_unlock_actor_name = actor_name

        self._cancel_held_open(state)

    async def _on_temporary_end(self, payload: dict) -> None:
        await self._free_pass_end(payload, "temporary")

    async def _free_pass_end(self, payload: dict, source_kind: str) -> None:
        resolved = self._resolve(payload)

        if not resolved:
            return

        door, state = resolved

        if source_kind == "schedule":
            state.schedule_unlock_active = False
        else:
            state.temporary_unlock_active = False

        # If another free-pass source is still active, do nothing yet.
        if self._registry.free_pass(state):
            return

        # If the door is still open, synthesize an on_open so the held-open
        # watchdog runs for the normal `held_open_seconds` grace before firing.
        if state.dps == "open":
            await self._handle_open(
                door,
                state,
                self._now(),
                source=f"{source_kind}_ended",
            )

    async def _on_emergency(self, payload: dict) -> None:
        mode = (_obj(payload).get("mode") or "").lower()
        prev = self._registry.emergency_evacuation_active
        is_evac = mode == "evacuation"

        # "lockdown" does NOT suppress alerts; only evacuation does.
        if is_evac:
            self._registry.emergency_evacuation_active = True

            log.warning("EMERGENCY evacuation active — alerts suppressed")

            # Cancel any pending held-open watchdogs.
            for s in self._registry.states.values():
                self._cancel_held_open(s)

            return

        # Any non-evacuation mode (lockdown, normal, …) clears the evac flag.
        if prev:
            self._registry.emergency_evacuation_active = False

            log.warning("emergency cleared (mode=%s) — sweeping open doors", mode)

            now = self._now()

            for door_id, state in self._registry.states.items():
                if state.dps == "open":
                    door = self._registry.doors_by_id[door_id]

                    if not self._registry.free_pass(state):
                        await self._handle_open(door, state, now, source="emergency_ended")

    # ---- formatting helpers ----------------------------------------------

    def _free_pass_label(self, state: DoorState) -> str:
        parts = []

        if state.schedule_unlock_active:
            parts.append("schedule")

        if state.temporary_unlock_active:
            parts.append("temporary")

        if self._registry.emergency_evacuation_active:
            parts.append("evacuation")

        return ",".join(parts) or "none"


def _actor_blob(state: DoorState) -> dict[str, str] | None:
    if state.last_unlock_actor_id or state.last_unlock_actor_name:
        return {
            "id": state.last_unlock_actor_id or "",
            "name": state.last_unlock_actor_name or "",
        }

    return None


def _epoch(mono_value: float | None, mono_now: float) -> str | None:
    """Convert a monotonic timestamp recorded earlier into an ISO-8601 string,
    approximating against the current wall clock."""
    if mono_value is None:
        return None

    wall = datetime.now(UTC).timestamp() - (mono_now - mono_value)

    return datetime.fromtimestamp(wall, UTC).isoformat()


def _epoch_seconds_from_monotonic(mono_value: float, mono_now: float) -> float:
    wall_now = datetime.now(UTC).timestamp()

    return wall_now - (mono_now - mono_value)
