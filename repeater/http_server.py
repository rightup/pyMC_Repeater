import asyncio
import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from typing import Callable, Optional, Dict, Any

import cherrypy
from pymc_core.protocol.utils import PAYLOAD_TYPES, ROUTE_TYPES

from repeater import __version__

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


class CADCalibrationEngine:
    """Real-time CAD calibration engine"""
    
    def __init__(self, stats_getter: Optional[Callable] = None, event_loop=None):
        self.stats_getter = stats_getter
        self.event_loop = event_loop
        self.running = False
        self.results = {}
        self.current_test = None
        self.progress = {"current": 0, "total": 0}
        self.clients = set()  # SSE clients
        self.calibration_thread = None
        
    def get_test_ranges(self, spreading_factor: int):
        """Get CAD test ranges based on spreading factor"""
        sf_ranges = {
            7:  (range(16, 29, 1), range(6, 15, 1)),
            8:  (range(16, 29, 1), range(6, 15, 1)),  
            9:  (range(18, 31, 1), range(7, 16, 1)),
            10: (range(20, 33, 1), range(8, 16, 1)),
            11: (range(22, 35, 1), range(9, 17, 1)),
            12: (range(24, 37, 1), range(10, 18, 1)),
        }
        return sf_ranges.get(spreading_factor, sf_ranges[8])
    
    async def test_cad_config(self, radio, det_peak: int, det_min: int, samples: int = 8) -> Dict[str, Any]:
        """Test a single CAD configuration with multiple samples"""
        detections = 0
        
        for _ in range(samples):
            try:
                result = await radio.perform_cad(det_peak=det_peak, det_min=det_min, timeout=0.6)
                if result:
                    detections += 1
            except Exception:
                pass
            await asyncio.sleep(0.03)
        
        return {
            'det_peak': det_peak,
            'det_min': det_min,
            'samples': samples,  
            'detections': detections,
            'detection_rate': (detections / samples) * 100,
        }
    
    def broadcast_to_clients(self, data):
        """Send data to all connected SSE clients"""
        message = f"data: {json.dumps(data)}\n\n"
        for client in self.clients.copy():
            try:
                client.write(message.encode())
                client.flush()
            except Exception:
                self.clients.discard(client)
    
    def calibration_worker(self, samples: int, delay_ms: int):
        """Worker thread for calibration process"""
        try:
            # Get radio from stats
            if not self.stats_getter:
                self.broadcast_to_clients({"type": "error", "message": "No stats getter available"})
                return
                
            stats = self.stats_getter()
            if not stats or "radio_instance" not in stats:
                self.broadcast_to_clients({"type": "error", "message": "Radio instance not available"})
                return
                
            radio = stats["radio_instance"]
            if not hasattr(radio, 'perform_cad'):
                self.broadcast_to_clients({"type": "error", "message": "Radio does not support CAD"})
                return
            
            # Get spreading factor
            config = stats.get("config", {})
            radio_config = config.get("radio", {})
            sf = radio_config.get("spreading_factor", 8)
            
            # Get test ranges
            peak_range, min_range = self.get_test_ranges(sf)
            
            total_tests = len(peak_range) * len(min_range)
            self.progress = {"current": 0, "total": total_tests}
            
            self.broadcast_to_clients({
                "type": "status", 
                "message": f"Starting calibration: SF{sf}, {total_tests} tests"
            })
            
            current = 0
            
            # Run calibration in event loop
            if self.event_loop:
                for det_peak in peak_range:
                    if not self.running:
                        break
                        
                    for det_min in min_range:
                        if not self.running:
                            break
                            
                        current += 1
                        self.progress["current"] = current
                        
                        # Update progress
                        self.broadcast_to_clients({
                            "type": "progress",
                            "current": current,
                            "total": total_tests,
                            "peak": det_peak,
                            "min": det_min
                        })
                        
                        # Run the test
                        future = asyncio.run_coroutine_threadsafe(
                            self.test_cad_config(radio, det_peak, det_min, samples),
                            self.event_loop
                        )
                        
                        try:
                            result = future.result(timeout=30)  # 30 second timeout per test
                            
                            # Store result
                            key = f"{det_peak}-{det_min}"
                            self.results[key] = result
                            
                            # Send result to clients
                            self.broadcast_to_clients({
                                "type": "result",
                                **result
                            })
                        except Exception as e:
                            logger.error(f"CAD test failed for peak={det_peak}, min={det_min}: {e}")
                            
                        # Delay between tests
                        if self.running and delay_ms > 0:
                            time.sleep(delay_ms / 1000.0)
            
            if self.running:
                self.broadcast_to_clients({"type": "completed", "message": "Calibration completed"})
            else:
                self.broadcast_to_clients({"type": "status", "message": "Calibration stopped"})
                
        except Exception as e:
            logger.error(f"Calibration worker error: {e}")
            self.broadcast_to_clients({"type": "error", "message": str(e)})
        finally:
            self.running = False
    
    def start_calibration(self, samples: int = 8, delay_ms: int = 100):
        """Start calibration process"""
        if self.running:
            return False
            
        self.running = True
        self.results.clear()
        self.progress = {"current": 0, "total": 0}
        
        # Start calibration in separate thread
        self.calibration_thread = threading.Thread(
            target=self.calibration_worker,
            args=(samples, delay_ms)
        )
        self.calibration_thread.daemon = True
        self.calibration_thread.start()
        
        return True
    
    def stop_calibration(self):
        """Stop calibration process"""
        self.running = False
        if self.calibration_thread:
            self.calibration_thread.join(timeout=2)
    
    def add_client(self, response_stream):
        """Add SSE client"""
        self.clients.add(response_stream)
    
    def remove_client(self, response_stream):
        """Remove SSE client"""
        self.clients.discard(response_stream)


class APIEndpoints:

    def __init__(
        self,
        stats_getter: Optional[Callable] = None,
        send_advert_func: Optional[Callable] = None,
        config: Optional[dict] = None,
        event_loop=None,
    ):

        self.stats_getter = stats_getter
        self.send_advert_func = send_advert_func
        self.config = config or {}
        self.event_loop = event_loop  # Store reference to main event loop
        
        # Initialize CAD calibration engine
        self.cad_calibration = CADCalibrationEngine(stats_getter, event_loop)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def stats(self):

        try:
            stats = self.stats_getter() if self.stats_getter else {}
            stats["version"] = __version__

            return stats
        except Exception as e:
            logger.error(f"Error serving stats: {e}")
            return {"error": str(e)}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def send_advert(self):

        if cherrypy.request.method != "POST":
            return {"success": False, "error": "Method not allowed"}

        if not self.send_advert_func:
            return {"success": False, "error": "Send advert function not configured"}

        try:
            import asyncio

            if self.event_loop is None:
                return {"success": False, "error": "Event loop not available"}

            future = asyncio.run_coroutine_threadsafe(self.send_advert_func(), self.event_loop)
            result = future.result(timeout=10)  # Wait up to 10 seconds

            if result:
                return {"success": True, "message": "Advert sent successfully"}
            else:
                return {"success": False, "error": "Failed to send advert"}
        except Exception as e:
            logger.error(f"Error sending advert: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def set_mode(self):

        if cherrypy.request.method != "POST":
            return {"success": False, "error": "Method not allowed"}

        try:
            data = cherrypy.request.json
            new_mode = data.get("mode", "forward")

            if new_mode not in ["forward", "monitor"]:
                return {"success": False, "error": "Invalid mode. Must be 'forward' or 'monitor'"}

            # Update config
            if "repeater" not in self.config:
                self.config["repeater"] = {}
            self.config["repeater"]["mode"] = new_mode

            logger.info(f"Mode changed to: {new_mode}")
            return {"success": True, "mode": new_mode}
        except Exception as e:
            logger.error(f"Error setting mode: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def set_duty_cycle(self):

        if cherrypy.request.method != "POST":
            return {"success": False, "error": "Method not allowed"}

        try:
            data = cherrypy.request.json
            enabled = data.get("enabled", True)

            # Update config
            if "duty_cycle" not in self.config:
                self.config["duty_cycle"] = {}
            self.config["duty_cycle"]["enforcement_enabled"] = enabled

            logger.info(f"Duty cycle enforcement {'enabled' if enabled else 'disabled'}")
            return {"success": True, "enabled": enabled}
        except Exception as e:
            logger.error(f"Error setting duty cycle: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def logs(self):

        try:
            logs = list(_log_buffer.logs)
            return {
                "logs": (
                    logs
                    if logs
                    else [
                        {
                            "message": "No logs available",
                            "timestamp": datetime.now().isoformat(),
                            "level": "INFO",
                        }
                    ]
                )
            }
        except Exception as e:
            logger.error(f"Error fetching logs: {e}")
            return {"error": str(e), "logs": []}

    # CAD Calibration endpoints
    @cherrypy.expose
    @cherrypy.tools.json_out()  
    @cherrypy.tools.json_in()
    def cad_calibration_start(self):
        """Start CAD calibration"""
        if cherrypy.request.method != "POST":
            return {"success": False, "error": "Method not allowed"}
        
        try:
            data = cherrypy.request.json or {}
            samples = data.get("samples", 8)
            delay = data.get("delay", 100)
            
            if self.cad_calibration.start_calibration(samples, delay):
                return {"success": True, "message": "Calibration started"}
            else:
                return {"success": False, "error": "Calibration already running"}
                
        except Exception as e:
            logger.error(f"Error starting CAD calibration: {e}")
            return {"success": False, "error": str(e)}
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def cad_calibration_stop(self):
        """Stop CAD calibration"""
        if cherrypy.request.method != "POST":
            return {"success": False, "error": "Method not allowed"}
        
        try:
            self.cad_calibration.stop_calibration()
            return {"success": True, "message": "Calibration stopped"}
        except Exception as e:
            logger.error(f"Error stopping CAD calibration: {e}")
            return {"success": False, "error": str(e)}
    
    @cherrypy.expose
    def cad_calibration_stream(self):
        """Server-Sent Events stream for real-time updates"""
        cherrypy.response.headers['Content-Type'] = 'text/event-stream'
        cherrypy.response.headers['Cache-Control'] = 'no-cache'
        cherrypy.response.headers['Connection'] = 'keep-alive'
        cherrypy.response.headers['Access-Control-Allow-Origin'] = '*'
        
        def generate():
            # Add client to calibration engine
            response = cherrypy.response
            self.cad_calibration.add_client(response)
            
            try:
                # Send initial connection message
                yield f"data: {json.dumps({'type': 'connected', 'message': 'Connected to CAD calibration stream'})}\n\n"
                
                # Keep connection alive - the calibration engine will send data
                while True:
                    time.sleep(1)
                    # Send keepalive every second
                    yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"
                    
            except Exception as e:
                logger.error(f"SSE stream error: {e}")
            finally:
                # Remove client when connection closes
                self.cad_calibration.remove_client(response)
        
        return generate()
    
    cad_calibration_stream._cp_config = {'response.stream': True}




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
    ):

        self.stats_getter = stats_getter
        self.template_dir = template_dir
        self.node_name = node_name
        self.pub_key = pub_key
        self.dashboard_template = None
        self.config = config or {}

        # Create nested API object for routing
        self.api = APIEndpoints(stats_getter, send_advert_func, self.config, event_loop)

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
    ):

        self.host = host
        self.port = port
        self.app = StatsApp(
            stats_getter, template_dir, node_name, pub_key, send_advert_func, config, event_loop
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
