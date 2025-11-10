import json
import logging
import os
import re
from collections import deque
from datetime import datetime
from typing import Callable, Optional

import cherrypy
from pymc_core.protocol.utils import PAYLOAD_TYPES, ROUTE_TYPES

from repeater import __version__
from .api_endpoints import APIEndpoints

logger = logging.getLogger("HTTPServer")


# In-memory log buffer
class LogBuffer(logging.Handler):

    def __init__(self, max_lines=100):
        super().__init__()
        self.logs = deque(maxlen=max_lines)
        self.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))

    def emit(self, record):

        try:
            msg = self.format(record)
            self.logs.append(
                {
                    "message": msg,
                    "timestamp": datetime.fromtimestamp(record.created).isoformat(),
                    "level": record.levelname,
                }
            )
        except Exception:
            self.handleError(record)


# Global log buffer instance
_log_buffer = LogBuffer(max_lines=100)

class StatsApp:

    def __init__(
        self,
        stats_getter: Optional[Callable] = None,
        template_dir: Optional[str] = None,
        node_name: str = "Repeater",
        pub_key: str = "",
        send_advert_func: Optional[Callable] = None,
        config: Optional[dict] = None,
        event_loop=None,
        daemon_instance=None,
        config_path=None,
    ):

        self.stats_getter = stats_getter
        self.template_dir = template_dir
        self.node_name = node_name
        self.pub_key = pub_key
        self.dashboard_template = None
        self.config = config or {}

        # Create nested API object for routing
        self.api = APIEndpoints(stats_getter, send_advert_func, self.config, event_loop, daemon_instance, config_path)


    # @cherrypy.expose
    # def index(self):
    #     """Serve dashboard HTML."""
    #     return self._serve_template("dashboard.html")


class HTTPStatsServer:

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8000,
        stats_getter: Optional[Callable] = None,
        template_dir: Optional[str] = None,
        node_name: str = "Repeater",
        pub_key: str = "",
        send_advert_func: Optional[Callable] = None,
        config: Optional[dict] = None,
        event_loop=None,
        daemon_instance=None,
        config_path=None,
    ):

        self.host = host
        self.port = port
        self.app = StatsApp(
            stats_getter, template_dir, node_name, pub_key, send_advert_func, config, event_loop, daemon_instance, config_path
        )

    def start(self):

        try:
            # Serve static files from templates directory
            static_dir = (
                self.app.template_dir if self.app.template_dir else os.path.dirname(__file__)
            )

            config = {
                "/": {
                    "tools.sessions.on": False,
                },
                "/static": {
                    "tools.staticdir.on": True,
                    "tools.staticdir.dir": static_dir,
                },
            }

            cherrypy.config.update(
                {
                    "server.socket_host": self.host,
                    "server.socket_port": self.port,
                    "engine.autoreload.on": False,
                    "log.screen": False,
                    "log.access_file": "",  # Disable access log file
                    "log.error_file": "",  # Disable error log file
                }
            )

            cherrypy.tree.mount(self.app, "/", config)

            # Completely disable access logging
            cherrypy.log.access_log.propagate = False
            cherrypy.log.error_log.setLevel(logging.ERROR)

            cherrypy.engine.start()
            server_url = "http://{}:{}".format(self.host, self.port)
            logger.info(f"HTTP stats server started on {server_url}")

        except Exception as e:
            logger.error(f"Failed to start HTTP server: {e}")
            raise

    def stop(self):
        try:
            cherrypy.engine.exit()
            logger.info("HTTP stats server stopped")
        except Exception as e:
            logger.warning(f"Error stopping HTTP server: {e}")
