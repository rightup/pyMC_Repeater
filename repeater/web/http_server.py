import json
import logging
import os
import re
from collections import deque
from datetime import datetime
from typing import Callable, Optional

import cherrypy
import cherrypy_cors
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
        node_name: str = "Repeater",
        pub_key: str = "",
        send_advert_func: Optional[Callable] = None,
        config: Optional[dict] = None,
        event_loop=None,
        daemon_instance=None,
        config_path=None,
    ):

        self.stats_getter = stats_getter
        self.node_name = node_name
        self.pub_key = pub_key
        self.dashboard_template = None
        self.config = config or {}
        
        # Path to the compiled Vue.js application
        self.html_dir = os.path.join(os.path.dirname(__file__), "html")

        # Create nested API object for routing
        self.api = APIEndpoints(stats_getter, send_advert_func, self.config, event_loop, daemon_instance, config_path)

    @cherrypy.expose
    def index(self):
        """Serve the Vue.js application index.html."""
        index_path = os.path.join(self.html_dir, "index.html")
        try:
            with open(index_path, 'r', encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            raise cherrypy.HTTPError(404, "Application not found. Please build the frontend first.")
        except Exception as e:
            logger.error(f"Error serving index.html: {e}")
            raise cherrypy.HTTPError(500, "Internal server error")

    @cherrypy.expose
    def default(self, *args, **kwargs):
        """Handle client-side routing - serve index.html for all non-API routes."""
        # Handle OPTIONS requests for any path
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        # Let API routes pass through
        if args and args[0] == 'api':
            raise cherrypy.NotFound()
        
        # For all other routes, serve the Vue.js app (client-side routing)
        return self.index()


class HTTPStatsServer:

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8000,
        stats_getter: Optional[Callable] = None,
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
        self.config = config or {}
        self.app = StatsApp(
            stats_getter, node_name, pub_key, send_advert_func, config, event_loop, daemon_instance, config_path
        )
        
        # Set up CORS at the server level if enabled
        self._cors_enabled = self.config.get("web", {}).get("cors_enabled", False)
        logger.info(f"CORS enabled: {self._cors_enabled}")

    def _setup_server_cors(self):
        """Set up CORS using cherrypy_cors.install()"""
        cherrypy_cors.install()
        logger.info("CORS support enabled")

    def start(self):

        try:
   
            if self._cors_enabled:
                self._setup_server_cors()
            
            # Serve static files from the html directory (compiled Vue.js app)
            html_dir = os.path.join(os.path.dirname(__file__), "html")
            assets_dir = os.path.join(html_dir, "assets")

            # Build config with conditional CORS settings
            config = {
                "/": {
                    "tools.sessions.on": False,
                    # Ensure proper content types for Vue.js files
                    "tools.staticfile.content_types": {
                        'js': 'application/javascript',
                        'css': 'text/css',
                        'html': 'text/html; charset=utf-8'
                    },
                },
                "/assets": {
                    "tools.staticdir.on": True,
                    "tools.staticdir.dir": assets_dir,
                    # Set proper content types for assets
                    "tools.staticdir.content_types": {
                        'js': 'application/javascript',
                        'css': 'text/css',
                        'map': 'application/json'
                    },
                },
                "/favicon.ico": {
                    "tools.staticfile.on": True,
                    "tools.staticfile.filename": os.path.join(html_dir, "favicon.ico"),
                },
            }

            # Only add CORS config entries if CORS is enabled
            if self._cors_enabled:
                config["/"]["cors.expose.on"] = True
                config["/assets"]["cors.expose.on"] = True
                config["/favicon.ico"]["cors.expose.on"] = True

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
