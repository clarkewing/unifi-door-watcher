from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict[str, object]:
    stream = request.app.state.stream
    ready = bool(stream and stream.connected)

    return {"ready": ready, "last_event_at": getattr(stream, "last_event_at", None)}


@router.get("/state")
async def state_dump(request: Request) -> dict[str, object]:
    cfg = request.app.state.config

    if not cfg.server.expose_state:
        return {"disabled": True}

    registry = request.app.state.registry
    sink = request.app.state.sink

    return {
        "emergency_evacuation_active": registry.emergency_evacuation_active,
        "failed_deliveries": getattr(sink, "failed_deliveries", 0),
        "doors": {
            door_id: {
                "dps": s.dps,
                "opened_at": s.opened_at,
                "last_unlock_at": s.last_unlock_at,
                "last_unlock_method": s.last_unlock_method,
                "schedule_unlock_active": s.schedule_unlock_active,
                "temporary_unlock_active": s.temporary_unlock_active,
            }
            for door_id, s in registry.states.items()
        },
    }
