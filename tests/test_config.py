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


def test_per_door_webhook_overrides_loaded(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCESS_TOKEN", "t")
    body = CONFIG_TEMPLATE + (
        "\n[[door]]\n"
        'id = "vip"\n'
        'name = "VIP Entrance"\n'
        'unauthorized_webhook_url = "https://protect.example/vip-unauth"\n'
        'held_open_webhook_url    = "https://protect.example/vip-held"\n'
    )
    cfg = load_config(_write(tmp_path, body))
    door = cfg.doors_by_id["vip"]
    assert str(door.unauthorized_webhook_url) == "https://protect.example/vip-unauth"
    assert str(door.held_open_webhook_url) == "https://protect.example/vip-held"


def test_global_urls_optional_when_all_doors_have_overrides(monkeypatch, tmp_path):
    """A config that drops both [protect].*_webhook_url globals must be
    valid as long as every door brings its own pair."""
    monkeypatch.setenv("ACCESS_TOKEN", "t")
    body = """
[access]
host = "10.0.0.1"
token = "${ACCESS_TOKEN}"

[protect]

[[door]]
id = "a"
name = "A"
unauthorized_webhook_url = "https://protect.example/a-unauth"
held_open_webhook_url    = "https://protect.example/a-held"
"""
    cfg = load_config(_write(tmp_path, body))
    assert cfg.protect.unauthorized_webhook_url is None
    assert cfg.doors_by_id["a"].unauthorized_webhook_url is not None


def test_door_without_any_webhook_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("ACCESS_TOKEN", "t")
    body = """
[access]
host = "10.0.0.1"
token = "${ACCESS_TOKEN}"

[protect]

[[door]]
id = "lonely"
name = "Lonely Door"
"""
    with pytest.raises(Exception) as ei:
        load_config(_write(tmp_path, body))
    assert "no unauthorized webhook" in str(ei.value)


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
