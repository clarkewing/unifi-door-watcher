from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


class _MissingEnvVar(RuntimeError):
    def __init__(self, var: str) -> None:
        super().__init__(f"Required environment variable {var!r} is not set")
        self.var = var


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):

        def sub(match: re.Match[str]) -> str:
            var = match.group(1)
            try:
                return os.environ[var]
            except KeyError as e:
                raise _MissingEnvVar(var) from e

        return _ENV_PATTERN.sub(sub, value)

    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}

    if isinstance(value, list):
        return [_expand_env(v) for v in value]

    return value


class AccessConfig(BaseModel):
    host: str
    port: int = 12445
    token: str
    verify_tls: bool = False
    reconnect_backoff_seconds: list[float] = Field(default_factory=lambda: [1, 2, 5, 15, 30, 60])
    reconcile_lookback_seconds: int = 120
    liveness_timeout_seconds: int = 90

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"wss://{self.host}:{self.port}/api/v1/developer/devices/notifications"


class ProtectConfig(BaseModel):
    unauthorized_webhook_url: HttpUrl
    held_open_webhook_url: HttpUrl
    # Bearer token for the Protect Integration API. Required when the
    # webhook URL points at `/proxy/protect/integration/v1/…`; leave unset
    # for the legacy "incoming webhook" form whose URL embeds its own
    # secret. Generated in Protect → Settings → Control Plane → Integrations.
    token: str | None = None
    request_timeout_seconds: float = 5.0
    retry_attempts: int = 3
    dedupe_window_seconds: float = 30.0


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    expose_state: bool = True


class Defaults(BaseModel):
    grace_seconds: int = 8
    held_open_seconds: int = 30


class DoorConfig(BaseModel):
    id: str
    name: str
    grace_seconds: int | None = None
    held_open_seconds: int | None = None

    @field_validator("grace_seconds", "held_open_seconds")
    @classmethod
    def _non_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("must be >= 0")

        return v


class AppConfig(BaseModel):
    access: AccessConfig
    protect: ProtectConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
    defaults: Defaults = Field(default_factory=Defaults)
    doors: list[DoorConfig] = Field(default_factory=list, alias="door")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _apply_defaults_and_check_uniqueness(self) -> AppConfig:
        seen: set[str] = set()

        for d in self.doors:
            if d.id in seen:
                raise ValueError(f"Duplicate door id {d.id!r} in config")

            seen.add(d.id)

            if d.grace_seconds is None:
                d.grace_seconds = self.defaults.grace_seconds

            if d.held_open_seconds is None:
                d.held_open_seconds = self.defaults.held_open_seconds

        return self

    @property
    def doors_by_id(self) -> dict[str, DoorConfig]:
        return {d.id: d for d in self.doors}


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)

    with path.open("rb") as f:
        raw: dict[str, Any] = tomllib.load(f)

    return AppConfig.model_validate(_expand_env(raw))
