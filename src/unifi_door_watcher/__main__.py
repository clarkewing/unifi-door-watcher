from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn

from .app import create_app
from .config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unifi-door-watcher")
    parser.add_argument(
        "--config",
        default=os.environ.get("UNIFI_DOOR_WATCHER_CONFIG", "/etc/unifi-door-watcher/config.toml"),
        help="Path to config.toml",
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level=args.log_level,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
