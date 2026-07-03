from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .access.client import AccessClient
from .access.seed import seed_initial_state
from .access.stream import AccessEventStream
from .config import AppConfig
from .detect.pipeline import DetectionPipeline
from .notify.protect import ProtectAlertSink
from .routes.health import router as health_router
from .state import DoorStateRegistry

log = logging.getLogger(__name__)


def create_app(config: AppConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        registry = DoorStateRegistry(doors_by_id=config.doors_by_id)
        client = AccessClient(config.access)
        sink = ProtectAlertSink(config.protect, doors_by_id=config.doors_by_id)
        pipeline = DetectionPipeline(registry, sink)
        stream = AccessEventStream(config.access, on_event=pipeline.handle, client=client)

        app.state.config = config
        app.state.registry = registry
        app.state.client = client
        app.state.sink = sink
        app.state.pipeline = pipeline
        app.state.stream = stream

        await sink.start()

        # Seed per-door state BEFORE subscribing to the ws — otherwise live
        # events can race against the seed and leave inconsistent state.
        await seed_initial_state(pipeline, client, registry)

        await stream.start()

        log.info("unifi-door-watcher started with %d doors", len(config.doors))

        try:
            yield
        finally:
            await stream.stop()
            await sink.stop()
            await client.aclose()

    app = FastAPI(title="unifi-door-watcher", lifespan=lifespan)
    app.include_router(health_router)

    return app
