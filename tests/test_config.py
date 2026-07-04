from __future__ import annotations

from pathlib import Path

import pytest

from unifi_door_watcher.config import _MissingEnvVar, load_config

CONFIG_TEMPLATE = """
[access]
host = "10.0.0.1"
token = "${ACCESS_TOKEN}"

[protect]

[defaults]
grace_seconds = 8
held_open_seconds = 60

[[door]]
id = "ext-1"
name = "Front"
grace_seconds = 0
held_open_seconds = 30
unauthorized_webhook_url = "https://protect.example/ext-1-unauth"
held_open_webhook_url    = "https://protect.example/ext-1-held"

[[door]]
id = "int-1"
name = "IT Closet"
grace_seconds = 15
unauthorized_webhook_url = "https://protect.example/int-1-unauth"
held_open_webhook_url    = "https://protect.example/int-1-held"

[[door]]
id = "int-2"
name = "Storage"
unauthorized_webhook_url = "https://protect.example/int-2-unauth"
held_open_webhook_url    = "https://protect.example/int-2-held"
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
    body = CONFIG_TEMPLATE + (
        "\n[[door]]\n"
        'id = "ext-1"\n'
        'name = "dup"\n'
        'unauthorized_webhook_url = "https://protect.example/dup-unauth"\n'
        'held_open_webhook_url    = "https://protect.example/dup-held"\n'
    )
    with pytest.raises(Exception) as ei:
        load_config(_write(tmp_path, body))
    assert "Duplicate" in str(ei.value)


def test_per_door_webhook_urls_loaded(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCESS_TOKEN", "t")
    cfg = load_config(_write(tmp_path))
    door = cfg.doors_by_id["ext-1"]
    assert str(door.unauthorized_webhook_url) == "https://protect.example/ext-1-unauth"
    assert str(door.held_open_webhook_url) == "https://protect.example/ext-1-held"


def test_door_missing_unauthorized_url_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCESS_TOKEN", "t")
    body = """
[access]
host = "10.0.0.1"
token = "${ACCESS_TOKEN}"

[protect]

[[door]]
id = "lonely"
name = "Lonely Door"
held_open_webhook_url = "https://protect.example/lonely-held"
"""
    with pytest.raises(Exception) as ei:
        load_config(_write(tmp_path, body))
    msg = str(ei.value)
    assert "unauthorized_webhook_url" in msg
    assert "bootstrap_protect_alarms" in msg  # error guides the operator


def test_door_missing_held_open_url_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCESS_TOKEN", "t")
    body = """
[access]
host = "10.0.0.1"
token = "${ACCESS_TOKEN}"

[protect]

[[door]]
id = "lonely"
name = "Lonely Door"
unauthorized_webhook_url = "https://protect.example/lonely-unauth"
"""
    with pytest.raises(Exception) as ei:
        load_config(_write(tmp_path, body))
    assert "held_open_webhook_url" in str(ei.value)


def test_protect_bootstrap_section_loaded(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCESS_TOKEN", "t")
    body = CONFIG_TEMPLATE + (
        "\n[protect.bootstrap]\n"
        'host = "10.12.120.216"\n'
        'username = "bot"\n'
        'password = "hunter2"\n'
        'notification_users = ["alice@example.com", "Bob Example"]\n'
        'notification_channels = ["push", "email"]\n'
        "cooldown_seconds = 45\n"
    )
    cfg = load_config(_write(tmp_path, body))
    boot = cfg.protect.bootstrap
    assert boot is not None
    assert boot.host == "10.12.120.216"
    assert boot.username == "bot"
    assert boot.password == "hunter2"
    assert boot.notification_users == ["alice@example.com", "Bob Example"]
    assert boot.notification_channels == ["push", "email"]
    assert boot.cooldown_seconds == 45


def test_protect_bootstrap_section_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCESS_TOKEN", "t")
    cfg = load_config(_write(tmp_path))
    assert cfg.protect.bootstrap is None
