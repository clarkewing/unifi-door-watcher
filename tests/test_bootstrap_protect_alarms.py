"""Tests for the pure-Python bits of `scripts/bootstrap_protect_alarms.py`.

The HTTP calls against Protect are exercised via a mock transport-ish path,
but the meat of the coverage is on the deterministic helpers: naming,
payload shape, receiver resolution, and config-file rewriting. The actual
network dance (login, POST/PATCH) is covered by a smoke test with a
MockTransport-backed httpx client."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The script isn't in the installed package — add it to sys.path so we can
# import it directly here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bootstrap_protect_alarms as boot


def test_alarm_name_uses_door_name_and_suffix():
    assert boot.alarm_name("Front Entrance", "unauthorized") == (
        "Front Entrance — Unauthorized opening"
    )
    assert boot.alarm_name("IT Closet", "held_open") == "IT Closet — Held open"


def test_alarm_body_mentions_door_name_with_leading_emoji():
    """Body starts with an emoji so it's visible in the lock-screen
    preview even when the title truncates, and includes the door name
    so the door is identifiable from the body alone."""
    unauth = boot.alarm_body("Front Entrance", "unauthorized")
    held = boot.alarm_body("IT Closet", "held_open")
    assert unauth.startswith("🚨")
    assert held.startswith("🚨")
    assert "Front Entrance" in unauth
    assert "IT Closet" in held
    assert unauth == "🚨 Front Entrance: Opened without authorization."
    assert held == "🚨 IT Closet: Held open too long."


def test_webhook_uuid_from_url_extracts_trailing_segment():
    url = "https://protect.example/proxy/protect/integration/v1/alarm-manager/webhook/abc-123"
    assert boot.webhook_uuid_from_url(url) == "abc-123"


def test_webhook_uuid_from_url_tolerates_trailing_slash():
    url = "https://protect.example/webhook/abc-123/"
    assert boot.webhook_uuid_from_url(url) == "abc-123"


def test_webhook_uuid_from_url_none_for_empty_input():
    assert boot.webhook_uuid_from_url(None) is None
    assert boot.webhook_uuid_from_url("") is None


def test_trigger_url_shape():
    url = boot.trigger_url("10.0.0.1", "abc-123")
    assert url == "https://10.0.0.1/proxy/protect/integration/v1/alarm-manager/webhook/abc-123"


def test_resolve_receiver_ids_by_id_email_and_full_name():
    users = [
        {"id": "u1", "email": "alice@example.com", "firstName": "Alice", "lastName": "Ada"},
        {"id": "u2", "email": "bob@example.com", "firstName": "Bob", "lastName": "Byte"},
    ]
    ids = boot.resolve_receiver_ids(users, ["u1", "bob@example.com", "Alice Ada"])
    assert ids == ["u1", "u2", "u1"]


def test_resolve_receiver_ids_case_insensitive_email_and_name():
    users = [
        {"id": "u1", "email": "Alice@Example.COM", "firstName": "Alice", "lastName": "Ada"},
    ]
    assert boot.resolve_receiver_ids(users, ["alice@example.com"]) == ["u1"]
    assert boot.resolve_receiver_ids(users, ["alice ada"]) == ["u1"]


def test_resolve_receiver_ids_unknown_raises_with_available_list():
    users = [{"id": "u1", "email": "alice@example.com", "firstName": "Alice"}]
    with pytest.raises(RuntimeError) as ei:
        boot.resolve_receiver_ids(users, ["missing@example.com"])
    msg = str(ei.value)
    assert "missing@example.com" in msg
    assert "Alice" in msg  # available users listed for troubleshooting


def test_make_payload_structure_matches_captured_shape():
    payload = boot.make_payload(
        name="Front — Unauthorized opening",
        body_text="🚨 Front: Opened without authorization.",
        webhook_uuid="uuid-1",
        receiver_ids=["u1", "u2"],
        channels=["push"],
        cooldown_seconds=30,
    )
    assert payload["name"] == "Front — Unauthorized opening"
    assert payload["enable"] is True
    assert payload["conditions"][0]["condition"] == {
        "type": "is",
        "source": "webhook",
        "value": "uuid-1",
    }
    assert payload["actions"][0]["type"] == "SEND_NOTIFICATION"
    assert payload["actions"][0]["metadata"]["text"] == "🚨 Front: Opened without authorization."
    receivers = payload["actions"][0]["metadata"]["receivers"]
    assert [r["user"] for r in receivers] == ["u1", "u2"]
    assert all(r["channels"] == ["push"] for r in receivers)
    assert payload["cooldown"] == {"enable": True, "timeout": 30_000}
    assert payload["isBlockedByArmMode"] is False


def test_make_payload_cooldown_zero_disables_it():
    payload = boot.make_payload("n", "b", "u", ["r"], ["push"], cooldown_seconds=0)
    assert payload["cooldown"] == {"enable": False, "timeout": 0}


def test_existing_webhook_uuid_extracts_from_conditions():
    automation = {
        "id": "a1",
        "conditions": [{"condition": {"type": "is", "source": "webhook", "value": "wh-uuid"}}],
    }
    assert boot.existing_webhook_uuid(automation) == "wh-uuid"


def test_existing_webhook_uuid_returns_none_for_non_webhook_trigger():
    automation = {
        "id": "a1",
        "conditions": [{"condition": {"type": "is", "source": "person"}}],
    }
    assert boot.existing_webhook_uuid(automation) is None


def test_update_config_urls_writes_per_door_fields(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        """
[access]
host = "10.0.0.1"
token = "t"

[protect]

[[door]]
id = "a"
name = "A"

[[door]]
id = "b"
name = "B"
"""
    )
    n = boot.update_config_urls(
        config,
        {
            ("a", "unauthorized"): "https://protect.example/a-unauth",
            ("a", "held_open"): "https://protect.example/a-held",
            ("b", "unauthorized"): "https://protect.example/b-unauth",
            ("b", "held_open"): "https://protect.example/b-held",
        },
    )
    assert n == 2
    content = config.read_text()
    assert 'unauthorized_webhook_url = "https://protect.example/a-unauth"' in content
    assert 'held_open_webhook_url = "https://protect.example/a-held"' in content
    assert 'unauthorized_webhook_url = "https://protect.example/b-unauth"' in content


def test_update_config_urls_skips_doors_not_in_input(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        """
[access]
host = "10.0.0.1"
token = "t"

[protect]

[[door]]
id = "a"
name = "A"

[[door]]
id = "b"
name = "B"
"""
    )
    n = boot.update_config_urls(
        config,
        {
            ("a", "unauthorized"): "https://protect.example/a-unauth",
            ("a", "held_open"): "https://protect.example/a-held",
        },
    )
    assert n == 1
    content = config.read_text()
    assert "https://protect.example/a-unauth" in content
    assert "b-unauth" not in content
