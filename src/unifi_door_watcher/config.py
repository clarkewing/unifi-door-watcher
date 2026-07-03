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


class ProtectBootstrapConfig(BaseModel):
    """Config used only by `scripts/bootstrap_protect_alarms.py` to create/update
    Protect Alarm Manager automations. Not read by the runtime watcher — the
    watcher only needs the trigger URLs on each door (or the global fallbacks)
    plus [protect].token."""

    host: str
    username: str
    password: str
    # Protect user IDs, emails, or full names of people to notify. The script
    # resolves names/emails to IDs via /proxy/protect/api/users.
    notification_users: list[str] = Field(default_factory=list)
    # Notification channels per receiver — Protect accepts "push" and "email".
    notification_channels: list[str] = Field(default_factory=lambda: ["push"])
    # Per-alarm cooldown in seconds. Since the watcher already dedupes at
    # emit time, we keep this modest so a real second event isn't swallowed.
    cooldown_seconds: int = 30
    verify_tls: bool = False


class ProtectConfig(BaseModel):
    # Global fallback URLs. Optional — if every door has its own per-door
    # override (see DoorConfig), the globals can be omitted entirely.
    # `AppConfig` cross-validates that each (door, alert_type) pair is
    # covered by either a per-door URL or the matching global.
    unauthorized_webhook_url: HttpUrl | None = None
    held_open_webhook_url: HttpUrl | None = None
    # Bearer token (`X-API-Key`) for the Protect Integration API. Required
    # for the /proxy/protect/integration/v1/… trigger endpoints.
    token: str | None = None
    request_timeout_seconds: float = 5.0
    retry_attempts: int = 3
    dedupe_window_seconds: float = 30.0
    # Optional bootstrap-only credentials — populated when you use the
    # bootstrap_protect_alarms.py script. The runtime watcher ignores this.
    bootstrap: ProtectBootstrapConfig | None = None


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
    # Per-door Protect Alarm Manager webhook URLs. Populated by
    # `bootstrap_protect_alarms.py` so each (door, alert_type) pair gets its
    # own alarm with a door-specific title/body. When unset, the
    # corresponding [protect].*_webhook_url global is used.
    unauthorized_webhook_url: HttpUrl | None = None
    held_open_webhook_url: HttpUrl | None = None

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

            # Every door must have *some* destination for each alert type.
            # Fail loudly at startup rather than silently dropping alerts.
            if d.unauthorized_webhook_url is None and self.protect.unauthorized_webhook_url is None:
                raise ValueError(
                    f"door {d.id!r} ({d.name!r}) has no unauthorized webhook: "
                    f"set [protect].unauthorized_webhook_url or the per-door "
                    f"unauthorized_webhook_url"
                )
            if d.held_open_webhook_url is None and self.protect.held_open_webhook_url is None:
                raise ValueError(
                    f"door {d.id!r} ({d.name!r}) has no held_open webhook: "
                    f"set [protect].held_open_webhook_url or the per-door "
                    f"held_open_webhook_url"
                )

        return self

    @property
    def doors_by_id(self) -> dict[str, DoorConfig]:
        return {d.id: d for d in self.doors}


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)

    with path.open("rb") as f:
        raw: dict[str, Any] = tomllib.load(f)

    return AppConfig.model_validate(_expand_env(raw))
