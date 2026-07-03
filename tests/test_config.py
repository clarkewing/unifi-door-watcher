from __future__ import annotations

from pathlib import Path

import pytest

from unifi_door_watcher.config import _MissingEnvVar, load_config

CONFIG_TEMPLATE = """
[access]
host = "10.0.0.1"
token = "${ACCESS_TOKEN}"

[protect]
unauthorized_webhook_url = "https://protect.example/unauth"
held_open_webhook_url    = "https://protect.example/held"

[defaults]
grace_seconds = 8
held_open_seconds = 60

[[door]]
id = "ext-1"
name = "Front"
grace_seconds = 0
held_open_seconds = 30

[[door]]
id = "int-1"
name = "IT Closet"
grace_seconds = 15

[[door]]
id = "int-2"
name = "Storage"
"""


def _write(tmp_path: Path, body: str = CONFIG_TEMPLATE) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body)
    return p


def test_env_interpolation(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCESS_TOKEN", "secret-123")
    cfg = load_config(_write(tmp_path))
    assert cfg.access.token == "secret-123"


def test_missing_env_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    with pytest.raises(_MissingEnvVar):
        load_config(_write(tmp_path))


def test_defaults_applied_and_overrides_win(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCESS_TOKEN", "t")
    cfg = load_config(_write(tmp_path))
    by_id = cfg.doors_by_id

    assert by_id["ext-1"].grace_seconds == 0
    assert by_id["ext-1"].held_open_seconds == 30

    assert by_id["int-1"].grace_seconds == 15
    assert by_id["int-1"].held_open_seconds == 60  # from defaults

    assert by_id["int-2"].grace_seconds == 8  # from defaults
    assert by_id["int-2"].held_open_seconds == 60


def test_duplicate_door_id_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCESS_TOKEN", "t")
    body = CONFIG_TEMPLATE + '\n[[door]]\nid = "ext-1"\nname = "dup"\n'
    with pytest.raises(Exception) as ei:
        load_config(_write(tmp_path, body))
    assert "Duplicate" in str(ei.value)


def test_urls_parsed(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCESS_TOKEN", "t")
    cfg = load_config(_write(tmp_path))
    assert str(cfg.protect.unauthorized_webhook_url).startswith("https://")
    assert cfg.access.ws_url.startswith("wss://")
