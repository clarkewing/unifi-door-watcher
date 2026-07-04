# unifi-door-watcher

[![CI](https://github.com/clarkewing/unifi-door-watcher/actions/workflows/ci.yml/badge.svg)](https://github.com/clarkewing/unifi-door-watcher/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Long-running service that watches a UniFi Access installation over the
developer WebSocket API and fires a UniFi Protect Alarm Manager webhook
when a door is opened without authorization or held open beyond a
per-door threshold. Designed to run as a systemd service on a small
always-on host (Raspberry Pi, small VM, etc.) on the same LAN as the
UniFi controller.

## What it detects

- **Unauthorized opening.** The door position sensor (DPS) reports
  *open* with no preceding authenticated unlock inside the door's
  configured grace period. Exterior doors typically use `grace_seconds
  = 0` for strict "any-open-without-unlock is unauthorized" semantics.
  Interior doors use a small grace period to absorb legitimate
  handle-side egress.
- **Held open.** DPS stays *open* past `held_open_seconds`. Covers both
  badge-unlock auto-relock and scheduled/temporary unlock windows.

Alerts are POSTed to Protect Alarm Manager webhooks — one URL per
alert type. The alarm's title/body is configured in the Protect UI;
the watcher just triggers it.

## What it handles correctly

- Scheduled unlocks and admin-initiated temporary unlocks suppress
  alerts while active. When they end, the watcher checks whether the
  door is still open and starts a fresh held-open watchdog from that
  moment.
- Emergency evacuation mode suppresses all alerts globally; clearing
  evacuation sweeps every still-open door.
- WebSocket reconnects with exponential backoff. On each successful
  reconnect the watcher polls `system/logs` for the gap window and
  replays events, deduplicated against the live stream.
- Startup state seeding: on boot, the REST API is polled to learn each
  door's current DPS state and whether it's currently under an active
  schedule or temporary unlock, so we don't have a blind spot after a
  restart.
- Actor enrichment for admin/portal remote unlocks: the `access.data
  .device.remote_unlock` event doesn't include the user's name, but the
  audit-log event that follows ~1s later does. The watcher correlates
  them via `event_object_id` (the UAH device ID) so held-open alerts
  triggered after a remote unlock carry the person's name.

## Requirements

- UniFi Access with the developer API enabled (a token generated from
  the UniFi UI). The developer API is unavailable if the controller has
  been upgraded to Identity Enterprise.
- UniFi Protect with an Integration API token that has permission to
  trigger Alarm Manager webhooks.
- Python 3.11+.
- A host that can reach the controller on port 12445 (Access WebSocket
  + REST) and 443 (Protect).

## Configuration

Config lives at `/etc/unifi-door-watcher/config.toml`. Secrets come
from `/etc/unifi-door-watcher/env` via systemd's `EnvironmentFile` and
are interpolated as `${VAR}` at startup. Templates are in `deploy/`.

```toml
[access]
host  = "192.168.1.1"
port  = 12445
token = "${ACCESS_TOKEN}"

[protect]
token = "${PROTECT_TOKEN}"

[defaults]
grace_seconds     = 8
held_open_seconds = 60

[[door]]
id   = "abc-…"
name = "Front Entrance"
grace_seconds     = 0        # exterior, strict
held_open_seconds = 30
unauthorized_webhook_url = "https://…/webhook/…"
held_open_webhook_url    = "https://…/webhook/…"
```

Every door needs an `id` (the door UUID from the Access API), a
`name`, and its own `unauthorized_webhook_url` /
`held_open_webhook_url`. Per-door `grace_seconds` / `held_open_seconds`
override the `[defaults]`. The watcher refuses to start until every
door has both URLs — populate them via the bootstrap workflow below or
by hand from the Protect Alarm Manager UI. See
`deploy/config.example.toml` for the full annotated form.

## Bootstrap workflow

Two scripts turn an empty config into a fully-populated one. Both are
idempotent — re-run whenever things change on either the Access or
Protect side.

### 1. Populate the door list from Access

```
python -m scripts.bootstrap_doors --config /etc/unifi-door-watcher/config.toml --write
```

Fetches the door list from the Access developer API and writes one
`[[door]]` block per DPS-equipped door, keyed by UUID. Doors without a
DPS sensor are excluded with a warning — they can't trigger the
watcher's unauthorized-open detection. Re-run whenever doors are added
or renamed in Access.

### 2. Create per-door alarms in Protect

```
python -m scripts.bootstrap_protect_alarms --config /etc/unifi-door-watcher/config.toml --write
```

For each `(door, alert_type)` pair, creates a UniFi Protect Alarm
Manager alarm whose title is the door name and whose recipient list is
whatever you configured in `[protect.bootstrap].notification_users`.
Writes each alarm's trigger URL back into the door's config block.

Protect's Alarm Manager doesn't template notification text from the
webhook body — the alarm's static `name`/`message` are what get pushed
to phones. So getting per-door notification text means one alarm per
door per alert type. This script does that with two API calls per door
instead of 48 clicks in the UI.

Requires a **local Protect user** (Protect → Users → Add User → Local
Access Only) with permission to manage automations, plus the endpoint
details in `[protect.bootstrap]`. Uses session-cookie auth (which
Protect's private automations API requires), not the `X-API-Key` token
the runtime uses for firing webhooks.

If you'd rather not run the script, you can create the alarms by hand
in Protect's UI and paste the resulting webhook URLs into each door's
`unauthorized_webhook_url` and `held_open_webhook_url` fields.

## Deployment

The `deploy/` directory has a systemd unit template. Typical install:

```
useradd --system --shell /usr/sbin/nologin unifi-door-watcher
mkdir -p /opt/unifi-door-watcher /etc/unifi-door-watcher
# ... copy source, create venv, install with `pip install -e .`
cp deploy/unifi-door-watcher.service /etc/systemd/system/
cp deploy/config.example.toml /etc/unifi-door-watcher/config.toml
cp deploy/env.example /etc/unifi-door-watcher/env
chmod 0600 /etc/unifi-door-watcher/env
systemctl daemon-reload
systemctl enable --now unifi-door-watcher
journalctl -u unifi-door-watcher -f
```

## Observability

The service exposes a small HTTP surface on `127.0.0.1:8080` for
health checks:

- `GET /healthz` — process is up.
- `GET /readyz` — WebSocket is connected.
- `GET /state` — per-door DPS, flags, and delivery counters (when
  `expose_state = true`).

## Development

```
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
make all          # ruff format --check + ruff check + pytest
make test         # tests only
make lint-fix     # apply safe auto-fixes
make fmt          # apply formatter
```

## License

MIT — see [LICENSE](LICENSE).

## Not affiliated with Ubiquiti

This is an independent tool built against UniFi's public developer
APIs. Not endorsed by, associated with, or supported by Ubiquiti.
