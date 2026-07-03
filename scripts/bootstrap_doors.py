"""Populate the [[door]] section of config.toml from the live Access API.

Usage:
  python -m scripts.bootstrap_doors --config /etc/unifi-door-watcher/config.toml          # print to stdout
  python -m scripts.bootstrap_doors --config /etc/unifi-door-watcher/config.toml --write  # update file in place

Doors without a DPS sensor are excluded with a warning — they cannot trigger
unauthorized-open detection.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import tomlkit

# Ensure src/ is importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unifi_door_watcher.access.client import AccessClient
from unifi_door_watcher.config import load_config


async def fetch_doors(config_path: Path) -> list[dict]:
    config = load_config(config_path)
    client = AccessClient(config.access)

    try:
        return await client.list_doors()
    finally:
        await client.aclose()


def build_door_blocks(doors: list[dict]) -> tomlkit.items.AoT:
    aot = tomlkit.aot()

    for d in doors:
        unifi_name = d.get("name", "")
        block = tomlkit.table()
        block.add(tomlkit.comment(f"{d.get('full_name', unifi_name)}"))
        block["id"] = d["id"]
        # `name` is the operator-facing label that appears in Protect alerts.
        # Defaults to UniFi's name; safe to edit to anything human-friendly —
        # the watcher matches events by `id` and `event_object_id`, never name.
        block["name"] = unifi_name

        aot.append(block)

    return aot


def filter_doors_with_dps(doors: list[dict]) -> tuple[list[dict], list[dict]]:
    with_dps = [d for d in doors if d.get("door_position_status") is not None]
    without_dps = [d for d in doors if d.get("door_position_status") is None]

    return with_dps, without_dps


def render(config_path: Path, write: bool) -> int:
    doors = asyncio.run(fetch_doors(config_path))
    with_dps, without_dps = filter_doors_with_dps(doors)

    if without_dps:
        print(
            "# WARNING: the following doors have no DPS sensor and are excluded:", file=sys.stderr
        )

        for d in without_dps:
            print(f"#   - {d.get('name')} ({d.get('id')})", file=sys.stderr)

    blocks = build_door_blocks(with_dps)

    if not write:
        doc = tomlkit.document()
        doc["door"] = blocks

        sys.stdout.write(tomlkit.dumps(doc))

        return 0

    text = config_path.read_text()
    doc = tomlkit.parse(text)
    doc["door"] = blocks

    config_path.write_text(tomlkit.dumps(doc))

    print(f"Updated {config_path} with {len(with_dps)} doors", file=sys.stderr)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--write", action="store_true", help="Update the config file in place")

    args = parser.parse_args(argv)

    return render(args.config, args.write)


if __name__ == "__main__":
    sys.exit(main())
