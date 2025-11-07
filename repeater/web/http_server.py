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

        # Load template on init
        if template_dir:
            template_path = os.path.join(template_dir, "dashboard.html")
            try:
                with open(template_path, "r") as f:
                    self.dashboard_template = f.read()
                logger.info(f"Loaded template from {template_path}")
            except FileNotFoundError:
                logger.error(f"Template not found: {template_path}")

    @cherrypy.expose
    def index(self):
        """Serve dashboard HTML."""
        return self._serve_template("dashboard.html")

    @cherrypy.expose
    def neighbors(self):
        """Serve neighbors page."""
        return self._serve_template("neighbors.html")

    @cherrypy.expose
    def statistics(self):
        """Serve statistics page."""
        return self._serve_template("statistics.html")

    @cherrypy.expose
    def configuration(self):
        """Serve configuration page."""
        return self._serve_template("configuration.html")

    @cherrypy.expose
    def logs(self):
        """Serve logs page."""
        return self._serve_template("logs.html")

    @cherrypy.expose
    def help(self):
        """Serve help documentation."""
        return self._serve_template("help.html")

    @cherrypy.expose
    def cad_calibration(self):
        """Serve CAD calibration page."""
        return self._serve_template("cad-calibration.html")

    def _serve_template(self, template_name: str):
        """Serve HTML template with stats."""
        if not self.template_dir:
            return "<h1>Error</h1><p>Template directory not configured</p>"

        if not self.dashboard_template:
            return "<h1>Error</h1><p>Template not loaded</p>"

        try:

            template_path = os.path.join(self.template_dir, template_name)
            with open(template_path, "r") as f:
                template_content = f.read()

            nav_path = os.path.join(self.template_dir, "nav.html")
            nav_content = ""
            try:
                with open(nav_path, "r") as f:
                    nav_content = f.read()
            except FileNotFoundError:
                logger.warning(f"Navigation template not found: {nav_path}")

            stats = self.stats_getter() if self.stats_getter else {}

            if "uptime_seconds" not in stats or not isinstance(
                stats.get("uptime_seconds"), (int, float)
            ):
                stats["uptime_seconds"] = 0

            # Calculate uptime in hours
            uptime_seconds = stats.get("uptime_seconds", 0)
            uptime_hours = int(uptime_seconds // 3600) if uptime_seconds else 0

            # Determine current page for nav highlighting
            page_map = {
                "dashboard.html": "dashboard",
                "neighbors.html": "neighbors",
                "statistics.html": "statistics",
                "configuration.html": "configuration",
                "cad-calibration.html": "cad-calibration",
                "logs.html": "logs",
                "help.html": "help",
            }
            current_page = page_map.get(template_name, "")

            # Prepare basic substitutions
            html = template_content
            html = html.replace("{{ node_name }}", str(self.node_name))
            html = html.replace("{{ last_updated }}", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            html = html.replace("{{ page }}", current_page)

            # Replace navigation placeholder with actual nav content
            if "<!-- NAVIGATION_PLACEHOLDER -->" in html:
                nav_substitutions = nav_content
                nav_substitutions = nav_substitutions.replace(
                    "{{ node_name }}", str(self.node_name)
                )
                nav_substitutions = nav_substitutions.replace("{{ pub_key }}", str(self.pub_key))
                nav_substitutions = nav_substitutions.replace(
                    "{{ last_updated }}", datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                )

                # Handle active state for nav items
                nav_substitutions = nav_substitutions.replace(
                    "{{ ' active' if page == 'dashboard' else '' }}",
                    " active" if current_page == "dashboard" else "",
                )
                nav_substitutions = nav_substitutions.replace(
                    "{{ ' active' if page == 'neighbors' else '' }}",
                    " active" if current_page == "neighbors" else "",
                )
                nav_substitutions = nav_substitutions.replace(
                    "{{ ' active' if page == 'statistics' else '' }}",
                    " active" if current_page == "statistics" else "",
                )
                nav_substitutions = nav_substitutions.replace(
                    "{{ ' active' if page == 'configuration' else '' }}",
                    " active" if current_page == "configuration" else "",
                )
                nav_substitutions = nav_substitutions.replace(
                    "{{ ' active' if page == 'logs' else '' }}",
                    " active" if current_page == "logs" else "",
                )
                nav_substitutions = nav_substitutions.replace(
                    "{{ ' active' if page == 'help' else '' }}",
                    " active" if current_page == "help" else "",
                )

                html = html.replace("<!-- NAVIGATION_PLACEHOLDER -->", nav_substitutions)

            # Build packets table HTML for dashboard
            if template_name == "dashboard.html":
                recent_packets = stats.get("recent_packets", [])
                packets_table = ""

                if recent_packets:
                    for pkt in recent_packets[-20:]:  # Last 20 packets
                        time_obj = datetime.fromtimestamp(pkt.get("timestamp", 0))
                        time_str = time_obj.strftime("%H:%M:%S")
                        pkt_type = PAYLOAD_TYPES.get(
                            pkt.get("type", 0), f"0x{pkt.get('type', 0): 02x}"
                        )
                        route_type = pkt.get("route", 0)
                        route = ROUTE_TYPES.get(route_type, f"UNKNOWN_{route_type}")
                        status = "OK TX" if pkt.get("transmitted") else "WAIT"

                        # Get proper CSS class for route type
                        route_class = route.lower().replace("_", "-")
                        snr_val = pkt.get("snr", 0.0)
                        score_val = pkt.get("score", 0)
                        delay_val = pkt.get("tx_delay_ms", 0)

                        packets_table += (
                            "<tr>"
                            f"<td>{time_str}</td>"
                            f'<td><span class="packet-type">{pkt_type}</span></td>'
                            f'<td><span class="route-{route_class}">{route}</span></td>'
                            f"<td>{pkt.get('length', 0)}</td>"
                            f"<td>{pkt.get('rssi', 0)}</td>"
                            f"<td>{snr_val: .1f}</td>"
                            f'<td><span class="score">{score_val: .2f}</span></td>'
                            f"<td>{delay_val: .0f}</td>"
                            f"<td>{status}</td>"
                            "</tr>"
                        )
                else:
                    packets_table = """
                            <tr>
                                <td colspan="9" class="empty-message">
                                    No packets received yet - waiting for traffic...
                                </td>
                            </tr>
                    """

                # Add dashboard-specific substitutions
                html = html.replace("{{ rx_count }}", str(stats.get("rx_count", 0)))
                html = html.replace("{{ forwarded_count }}", str(stats.get("forwarded_count", 0)))
                html = html.replace("{{ dropped_count }}", str(stats.get("dropped_count", 0)))
                html = html.replace("{{ uptime_hours }}", str(uptime_hours))

                # Replace tbody with actual packets
                tbody_pattern = r'<tbody id="packet-table">.*?</tbody>'
                tbody_replacement = f'<tbody id="packet-table">\n{packets_table}\n</tbody>'
                html = re.sub(
                    tbody_pattern,
                    tbody_replacement,
                    html,
                    flags=re.DOTALL,
                )

            return html

        except Exception as e:
            logger.error(f"Error rendering template {template_name}: {e}", exc_info=True)
            return f"<h1>Error</h1><p>{str(e)}</p>"


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
