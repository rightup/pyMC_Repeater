"""CLI entry for the openHop plugin manager process.

Usage:
  openhop-plugin-manager --config /etc/openhop_repeater/config.yaml
  python -m repeater.plugins --plugins-root /var/lib/openhop_repeater/plugins
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .ipc import PluginIPCServer
from .manager import PluginManager
from .runtime import PluginRuntime
from .storage import (
    PluginStorage,
    resolve_catalogue_url,
    resolve_plugin_socket_path,
    resolve_plugins_root,
)


def _load_config(config_path: Optional[str]) -> dict[str, Any]:
    if not config_path:
        return {}
    try:
        from repeater.config import load_config

        return load_config(config_path)
    except Exception:
        # Fall back to raw YAML if full load_config has radio side-effects
        import yaml

        path = Path(config_path)
        if not path.is_file():
            return {}
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="openHop plugin manager")
    p.add_argument("--config", default=None, help="Path to Repeater config.yaml")
    p.add_argument(
        "--plugins-root",
        default=None,
        help="Override plugin install root (default: {storage_dir}/plugins)",
    )
    p.add_argument(
        "--socket",
        default=None,
        help="Unix domain socket path (default: {storage_dir}/plugin-manager.sock)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO)",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("PluginManagerMain")

    config = _load_config(args.config)
    plugins_cfg = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    if plugins_cfg.get("enabled") is False:
        logger.error("plugins.enabled is false in config; refusing to start")
        return 2

    if args.plugins_root:
        plugins_root = Path(args.plugins_root).expanduser().resolve()
    else:
        plugins_root = resolve_plugins_root(config)

    if args.socket:
        socket_path = Path(args.socket).expanduser().resolve()
    else:
        socket_path = resolve_plugin_socket_path(config)

    storage = PluginStorage(plugins_root)
    runtime = PluginRuntime(storage)
    manager = PluginManager(storage, runtime, catalogue_url=resolve_catalogue_url(config))
    server = PluginIPCServer(socket_path, manager)

    stop = {"flag": False}

    def _handle_signal(signum, frame):  # noqa: ARG001
        logger.info("Signal %s received, shutting down", signum)
        stop["flag"] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except Exception:
            pass

    logger.info("Plugin root: %s", plugins_root)
    logger.info("IPC socket: %s", socket_path)
    manager.start()
    server.start()

    try:
        while not stop["flag"]:
            time.sleep(0.5)
    finally:
        logger.info("Stopping plugin manager")
        try:
            server.stop()
        except Exception as exc:
            logger.debug("IPC stop: %s", exc)
        try:
            manager.stop_all()
        except Exception as exc:
            logger.debug("manager stop_all: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
