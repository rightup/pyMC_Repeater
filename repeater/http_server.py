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
    
    def __init__(self, daemon_instance=None, event_loop=None):
        self.daemon_instance = daemon_instance
        self.event_loop = event_loop
        self.running = False
        self.results = {}
        self.current_test = None
        self.progress = {"current": 0, "total": 0}
        self.clients = set()  # SSE clients
        self.calibration_thread = None
        
    def get_test_ranges(self, spreading_factor: int):
        """Get CAD test ranges"""
        # Higher values = less sensitive, lower values = more sensitive
        # Test from LESS sensitive to MORE sensitive to find the sweet spot
        sf_ranges = {
            7:  (range(22, 30, 1), range(12, 20, 1)), 
            8:  (range(22, 30, 1), range(12, 20, 1)),   
            9:  (range(24, 32, 1), range(14, 22, 1)), 
            10: (range(26, 34, 1), range(16, 24, 1)), 
            11: (range(28, 36, 1), range(18, 26, 1)), 
            12: (range(30, 38, 1), range(20, 28, 1)), 
        }
        return sf_ranges.get(spreading_factor, sf_ranges[8])
    
    async def test_cad_config(self, radio, det_peak: int, det_min: int, samples: int = 20) -> Dict[str, Any]:
        """Test CAD configuration with proper spacing and baseline measurement"""
        detections = 0
        baseline_detections = 0
        
        # First, get baseline with very insensitive settings (should detect nothing)
        baseline_samples = 5
        for _ in range(baseline_samples):
            try:
                # Use very high thresholds that should detect nothing
                baseline_result = await radio.perform_cad(det_peak=35, det_min=25, timeout=0.3)
                if baseline_result:
                    baseline_detections += 1
            except Exception:
                pass
            await asyncio.sleep(0.1)  # 100ms between baseline samples
        
        # Wait before actual test
        await asyncio.sleep(0.5)
        
        # Now test the actual configuration
        for i in range(samples):
            try:
                result = await radio.perform_cad(det_peak=det_peak, det_min=det_min, timeout=0.3)
                if result:
                    detections += 1
            except Exception:
                pass
            
            # Variable delay to avoid sampling artifacts
            delay = 0.05 + (i % 3) * 0.05  # 50ms, 100ms, 150ms rotation
            await asyncio.sleep(delay)
        
        # Calculate adjusted detection rate
        baseline_rate = (baseline_detections / baseline_samples) * 100
        detection_rate = (detections / samples) * 100
        
        # Subtract baseline noise
        adjusted_rate = max(0, detection_rate - baseline_rate)
        
        return {
            'det_peak': det_peak,
            'det_min': det_min,
            'samples': samples,
            'detections': detections,
            'detection_rate': detection_rate,
            'baseline_rate': baseline_rate,
            'adjusted_rate': adjusted_rate,  # This is the useful metric
            'sensitivity_score': self._calculate_sensitivity_score(det_peak, det_min, adjusted_rate)
        }
    
    def _calculate_sensitivity_score(self, det_peak: int, det_min: int, adjusted_rate: float) -> float:
        """Calculate a sensitivity score - higher is better balance"""
        # Ideal detection rate is around 10-30% for good sensitivity without false positives
        ideal_rate = 20.0
        rate_penalty = abs(adjusted_rate - ideal_rate) / ideal_rate
        
        # Prefer moderate sensitivity settings (not too extreme)
        sensitivity_penalty = (abs(det_peak - 25) + abs(det_min - 15)) / 20.0
        
        # Lower penalty = higher score
        score = max(0, 100 - (rate_penalty * 50) - (sensitivity_penalty * 20))
        return score
    
    def broadcast_to_clients(self, data):
        """Send data to all connected SSE clients"""
        # Store the message for clients to pick up
        self.last_message = data
        # Also store in a queue for clients to consume
        if not hasattr(self, 'message_queue'):
            self.message_queue = []
        self.message_queue.append(data)
    
    def calibration_worker(self, samples: int, delay_ms: int):
        """Worker thread for calibration process"""
        try:
            # Get radio from daemon instance
            if not self.daemon_instance:
                self.broadcast_to_clients({"type": "error", "message": "No daemon instance available"})
                return
                
            radio = getattr(self.daemon_instance, 'radio', None)
            if not radio:
                self.broadcast_to_clients({"type": "error", "message": "Radio instance not available"})
                return
            if not hasattr(radio, 'perform_cad'):
                self.broadcast_to_clients({"type": "error", "message": "Radio does not support CAD"})
                return
            
            # Get spreading factor from daemon instance
            config = getattr(self.daemon_instance, 'config', {})
            radio_config = config.get("radio", {})
            sf = radio_config.get("spreading_factor", 8)
            
            # Get test ranges
            peak_range, min_range = self.get_test_ranges(sf)
            
            total_tests = len(peak_range) * len(min_range)
            self.progress = {"current": 0, "total": total_tests}
            
            self.broadcast_to_clients({
                "type": "status", 
                "message": f"Starting calibration: SF{sf}, {total_tests} tests",
                "test_ranges": {
                    "peak_min": min(peak_range),
                    "peak_max": max(peak_range),
                    "min_min": min(min_range),
                    "min_max": max(min_range),
                    "spreading_factor": sf,
                    "total_tests": total_tests
                }
            })
            
            current = 0
            
            import random
            

            peak_list = list(peak_range)
            min_list = list(min_range)
            
            # Create all test combinations
            test_combinations = []
            for det_peak in peak_list:
                for det_min in min_list:
                    test_combinations.append((det_peak, det_min))
            
            # Sort by distance from center for center-out pattern
            peak_center = (max(peak_list) + min(peak_list)) / 2
            min_center = (max(min_list) + min(min_list)) / 2
            
            def distance_from_center(combo):
                peak, min_val = combo
                return ((peak - peak_center) ** 2 + (min_val - min_center) ** 2) ** 0.5
            
            # Sort by distance from center
            test_combinations.sort(key=distance_from_center)
            

            band_size = max(1, len(test_combinations) // 8)  # Create 8 bands
            randomized_combinations = []
            
            for i in range(0, len(test_combinations), band_size):
                band = test_combinations[i:i + band_size]
                random.shuffle(band)  # Randomize within each band
                randomized_combinations.extend(band)
            
            # Run calibration in event loop with center-out randomized pattern
            if self.event_loop:
                for det_peak, det_min in randomized_combinations:
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
                # Find best result based on sensitivity score (not just detection rate)
                best_result = None
                recommended_result = None
                if self.results:
                    # Find result with highest sensitivity score (best balance)
                    best_result = max(self.results.values(), key=lambda x: x.get('sensitivity_score', 0))
                    
                    # Also find result with ideal adjusted detection rate (10-30%)
                    ideal_results = [r for r in self.results.values() if 10 <= r.get('adjusted_rate', 0) <= 30]
                    if ideal_results:
                        # Among ideal results, pick the one with best sensitivity score
                        recommended_result = max(ideal_results, key=lambda x: x.get('sensitivity_score', 0))
                    else:
                        recommended_result = best_result
                
                self.broadcast_to_clients({
                    "type": "completed", 
                    "message": "Calibration completed",
                    "results": {
                        "best": best_result,
                        "recommended": recommended_result,
                        "total_tests": len(self.results)
                    } if best_result else None
                })
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
        self.clear_message_queue()  # Clear any old messages
        
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
    
    def clear_message_queue(self):
        """Clear the message queue when starting a new calibration"""
        if hasattr(self, 'message_queue'):
            self.message_queue.clear()
class APIEndpoints:

    def __init__(
        self,
        stats_getter: Optional[Callable] = None,
        send_advert_func: Optional[Callable] = None,
        config: Optional[dict] = None,
        event_loop=None,
        daemon_instance=None,
        config_path=None,
    ):

        self.stats_getter = stats_getter
        self.send_advert_func = send_advert_func
        self.config = config or {}
        self.event_loop = event_loop
        self.daemon_instance = daemon_instance
        self._config_path = config_path or '/etc/pymc_repeater/config.yaml'
        
        # Initialize CAD calibration engine
        self.cad_calibration = CADCalibrationEngine(daemon_instance, event_loop)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def stats(self):

        try:
            stats = self.stats_getter() if self.stats_getter else {}
            stats["version"] = __version__
            
            # Add pyMC_Core version
            try:
                import pymc_core
                stats["core_version"] = pymc_core.__version__
            except ImportError:
                stats["core_version"] = "unknown"

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
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def save_cad_settings(self):
        """Save CAD calibration settings to config"""
        if cherrypy.request.method != "POST":
            return {"success": False, "error": "Method not allowed"}
        
        try:
            data = cherrypy.request.json or {}
            peak = data.get("peak")
            min_val = data.get("min_val")
            detection_rate = data.get("detection_rate", 0)
            
            if peak is None or min_val is None:
                return {"success": False, "error": "Missing peak or min_val parameters"}
            
            # Update the radio immediately if available
            if self.daemon_instance and hasattr(self.daemon_instance, 'radio') and self.daemon_instance.radio:
                if hasattr(self.daemon_instance.radio, 'set_custom_cad_thresholds'):
                    self.daemon_instance.radio.set_custom_cad_thresholds(peak=peak, min_val=min_val)
                    logger.info(f"Applied CAD settings to radio: peak={peak}, min={min_val}")
            
            # Update the in-memory config
            if "radio" not in self.config:
                self.config["radio"] = {}
            if "cad" not in self.config["radio"]:
                self.config["radio"]["cad"] = {}
            
            self.config["radio"]["cad"]["peak_threshold"] = peak
            self.config["radio"]["cad"]["min_threshold"] = min_val
            
            # Save to config file
            config_path = getattr(self, '_config_path', '/etc/pymc_repeater/config.yaml')
            self._save_config_to_file(config_path)
            
            logger.info(f"Saved CAD settings to config: peak={peak}, min={min_val}, rate={detection_rate:.1f}%")
            return {
                "success": True, 
                "message": f"CAD settings saved: peak={peak}, min={min_val}",
                "settings": {"peak": peak, "min_val": min_val, "detection_rate": detection_rate}
            }
            
        except Exception as e:
            logger.error(f"Error saving CAD settings: {e}")
            return {"success": False, "error": str(e)}

    def _save_config_to_file(self, config_path):
        """Save current config to YAML file"""
        try:
            import yaml
            import os
            
            # Ensure directory exists
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            
            # Write config to file
            with open(config_path, 'w') as f:
                yaml.dump(self.config, f, default_flow_style=False, indent=2)
                
            logger.info(f"Configuration saved to {config_path}")
            
        except Exception as e:
            logger.error(f"Failed to save config to {config_path}: {e}")
            raise

    @cherrypy.expose
    def cad_calibration_stream(self):
        """Server-Sent Events stream for real-time updates"""
        cherrypy.response.headers['Content-Type'] = 'text/event-stream'
        cherrypy.response.headers['Cache-Control'] = 'no-cache'
        cherrypy.response.headers['Connection'] = 'keep-alive'
        cherrypy.response.headers['Access-Control-Allow-Origin'] = '*'
        
        def generate():
            
            if not hasattr(self.cad_calibration, 'message_queue'):
                self.cad_calibration.message_queue = []
            
            try:
          
                yield f"data: {json.dumps({'type': 'connected', 'message': 'Connected to CAD calibration stream'})}\n\n"
                
               
                if self.cad_calibration.running:
             
                    config = getattr(self.cad_calibration.daemon_instance, 'config', {})
                    radio_config = config.get("radio", {})
                    sf = radio_config.get("spreading_factor", 8)
                    
         
                    peak_range, min_range = self.cad_calibration.get_test_ranges(sf)
                    total_tests = len(peak_range) * len(min_range)
                    
              
                    status_message = {
                        "type": "status", 
                        "message": f"Calibration in progress: SF{sf}, {total_tests} tests",
                        "test_ranges": {
                            "peak_min": min(peak_range),
                            "peak_max": max(peak_range),
                            "min_min": min(min_range),
                            "min_max": max(min_range),
                            "spreading_factor": sf,
                            "total_tests": total_tests
                        }
                    }
                    yield f"data: {json.dumps(status_message)}\n\n"
                
                last_message_index = len(self.cad_calibration.message_queue)
                
               
                while True:
                   
                    current_queue_length = len(self.cad_calibration.message_queue)
                    if current_queue_length > last_message_index:
                    
                        for i in range(last_message_index, current_queue_length):
                            message = self.cad_calibration.message_queue[i]
                            yield f"data: {json.dumps(message)}\n\n"
                        last_message_index = current_queue_length
                    else:
                       
                        yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"
                    
                    time.sleep(0.5)  
                    
            except Exception as e:
                logger.error(f"SSE stream error: {e}")
            finally:
                pass  
        
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
