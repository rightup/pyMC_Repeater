import json
import logging
import time
from datetime import datetime
from typing import Callable, Optional
import cherrypy
from repeater import __version__
from repeater.config import update_global_flood_policy
from .cad_calibration_engine import CADCalibrationEngine

logger = logging.getLogger("HTTPServer")


def add_cors_headers():
    """Add CORS headers to allow cross-origin requests"""
    cherrypy.response.headers['Access-Control-Allow-Origin'] = '*'
    cherrypy.response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    cherrypy.response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'


def cors_enabled(func):
    """Decorator to enable CORS for API endpoints"""
    def wrapper(*args, **kwargs):
        add_cors_headers()
        return func(*args, **kwargs)
    return wrapper


# system systems
# GET /api/stats
# GET /api/logs

# # Packets
# GET /api/packet_stats?hours=24
# GET /api/recent_packets?limit=100
# GET /api/filtered_packets?type=4&route=1&start_timestamp=X&end_timestamp=Y&limit=1000
# GET /api/packet_by_hash?packet_hash=abc123
# GET /api/packet_type_stats?hours=24

# Charts & RRD
# GET /api/rrd_data?start_time=X&end_time=Y&resolution=average
# GET /api/packet_type_graph_data?hours=24&resolution=average&types=all
# GET /api/metrics_graph_data?hours=24&resolution=average&metrics=all

# Noise Floor
# GET /api/noise_floor_history?hours=24
# GET /api/noise_floor_stats?hours=24  
# GET /api/noise_floor_chart_data?hours=24

# Repeater Control
# POST /api/send_advert
# POST /api/set_mode {"mode": "forward|monitor"}
# POST /api/set_duty_cycle {"enabled": true|false}

# CAD Calibration
# POST /api/cad_calibration_start {"samples": 8, "delay": 100}
# POST /api/cad_calibration_stop
# POST /api/save_cad_settings {"peak": 127, "min_val": 64}
# GET  /api/cad_calibration_stream (SSE)


# Common Parameters
# hours - Time range (default: 24)
# resolution - 'average', 'max', 'min' (default: 'average')
# limit - Max results (default varies)
# type - Packet type 0-15
# route - Route type 1-3



class APIEndpoints:
    def __init__(self, stats_getter: Optional[Callable] = None, send_advert_func: Optional[Callable] = None, config: Optional[dict] = None, event_loop=None, daemon_instance=None, config_path=None):
        self.stats_getter = stats_getter
        self.send_advert_func = send_advert_func
        self.config = config or {}
        self.event_loop = event_loop
        self.daemon_instance = daemon_instance
        self._config_path = config_path or '/etc/pymc_repeater/config.yaml'
        self.cad_calibration = CADCalibrationEngine(daemon_instance, event_loop)

    @cherrypy.expose
    def default(self, *args, **kwargs):
        """Handle OPTIONS requests for CORS preflight"""
        if cherrypy.request.method == "OPTIONS":
            add_cors_headers()
            return ""
        # For non-OPTIONS requests, return 404
        raise cherrypy.HTTPError(404)

    def _get_storage(self):
        if not self.daemon_instance:
            raise Exception("Daemon not available")
        
        if not hasattr(self.daemon_instance, 'repeater_handler') or not self.daemon_instance.repeater_handler:
            raise Exception("Repeater handler not initialized")
            
        if not hasattr(self.daemon_instance.repeater_handler, 'storage') or not self.daemon_instance.repeater_handler.storage:
            raise Exception("Storage not initialized in repeater handler")
            
        return self.daemon_instance.repeater_handler.storage

    def _success(self, data, **kwargs):
        result = {"success": True, "data": data}
        result.update(kwargs)
        return result

    def _error(self, error):
        return {"success": False, "error": str(error)}

    def _get_params(self, defaults):
        params = cherrypy.request.params
        result = {}
        for key, default in defaults.items():
            value = params.get(key, default)
            if isinstance(default, int):
                result[key] = int(value) if value is not None else None
            elif isinstance(default, float):
                result[key] = float(value) if value is not None else None
            else:
                result[key] = value
        return result

    def _require_post(self):
        if cherrypy.request.method != "POST":
            raise Exception("Method not allowed")

    def _get_time_range(self, hours):
        end_time = int(time.time())
        return end_time - (hours * 3600), end_time

    def _process_counter_data(self, data_points, timestamps_ms):
        rates = []
        prev_value = None
        for value in data_points:
            if value is None:
                rates.append(0)
            elif prev_value is None:
                rates.append(0)
            else:
                rates.append(max(0, value - prev_value))
            prev_value = value
        return [[timestamps_ms[i], rates[i]] for i in range(min(len(rates), len(timestamps_ms)))]

    def _process_gauge_data(self, data_points, timestamps_ms):
        values = [v if v is not None else 0 for v in data_points]
        return [[timestamps_ms[i], values[i]] for i in range(min(len(values), len(timestamps_ms)))]

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cors_enabled
    def stats(self):
        try:
            stats = self.stats_getter() if self.stats_getter else {}
            stats["version"] = __version__
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
    @cors_enabled
    def send_advert(self):
        try:
            self._require_post()
            if not self.send_advert_func:
                return self._error("Send advert function not configured")
            if self.event_loop is None:
                return self._error("Event loop not available")
            import asyncio
            future = asyncio.run_coroutine_threadsafe(self.send_advert_func(), self.event_loop)
            result = future.result(timeout=10)
            return self._success("Advert sent successfully") if result else self._error("Failed to send advert")
        except Exception as e:
            logger.error(f"Error sending advert: {e}", exc_info=True)
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    @cors_enabled
    def set_mode(self):
        try:
            self._require_post()
            data = cherrypy.request.json
            new_mode = data.get("mode", "forward")
            if new_mode not in ["forward", "monitor"]:
                return self._error("Invalid mode. Must be 'forward' or 'monitor'")
            if "repeater" not in self.config:
                self.config["repeater"] = {}
            self.config["repeater"]["mode"] = new_mode
            logger.info(f"Mode changed to: {new_mode}")
            return {"success": True, "mode": new_mode}
        except Exception as e:
            logger.error(f"Error setting mode: {e}", exc_info=True)
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    @cors_enabled
    def set_duty_cycle(self):
        try:
            self._require_post()
            data = cherrypy.request.json
            enabled = data.get("enabled", True)
            if "duty_cycle" not in self.config:
                self.config["duty_cycle"] = {}
            self.config["duty_cycle"]["enforcement_enabled"] = enabled
            logger.info(f"Duty cycle enforcement {'enabled' if enabled else 'disabled'}")
            return {"success": True, "enabled": enabled}
        except Exception as e:
            logger.error(f"Error setting duty cycle: {e}", exc_info=True)
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cors_enabled
    def logs(self):
        from .http_server import _log_buffer
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

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cors_enabled
    def packet_stats(self, hours=24):
        try:
            hours = int(hours)
            stats = self._get_storage().get_packet_stats(hours=hours)
            return self._success(stats)
        except Exception as e:
            logger.error(f"Error getting packet stats: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cors_enabled
    def packet_type_stats(self, hours=24):
        try:
            hours = int(hours)
            stats = self._get_storage().get_packet_type_stats(hours=hours)
            return self._success(stats)
        except Exception as e:
            logger.error(f"Error getting packet type stats: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cors_enabled
    def route_stats(self, hours=24):
        try:
            hours = int(hours)
            stats = self._get_storage().get_route_stats(hours=hours)
            return self._success(stats)
        except Exception as e:
            logger.error(f"Error getting route stats: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cors_enabled
    def recent_packets(self, limit=100):
        try:
            limit = int(limit)
            packets = self._get_storage().get_recent_packets(limit=limit)
            return self._success(packets, count=len(packets))
        except Exception as e:
            logger.error(f"Error getting recent packets: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def filtered_packets(self):
        try:
            params = self._get_params({
                'type': None,
                'route': None, 
                'start_timestamp': None,
                'end_timestamp': None,
                'limit': 1000
            })
            packets = self._get_storage().get_filtered_packets(**params)
            return self._success(packets, count=len(packets), filters=params)
        except ValueError as e:
            return self._error(f"Invalid parameter format: {e}")
        except Exception as e:
            logger.error(f"Error getting filtered packets: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def packet_by_hash(self, packet_hash=None):
        try:
            if not packet_hash:
                return self._error("packet_hash parameter required")
            packet = self._get_storage().get_packet_by_hash(packet_hash)
            return self._success(packet) if packet else self._error("Packet not found")
        except Exception as e:
            logger.error(f"Error getting packet by hash: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def packet_type_stats(self, hours=24):
        try:
            hours = int(hours)
            stats = self._get_storage().get_packet_type_stats(hours=hours)
            return self._success(stats)
        except Exception as e:
            logger.error(f"Error getting packet type stats: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def rrd_data(self):
        try:
            params = self._get_params({
                'start_time': None,
                'end_time': None,
                'resolution': 'average'
            })
            data = self._get_storage().get_rrd_data(**params)
            return self._success(data) if data else self._error("No RRD data available")
        except ValueError as e:
            return self._error(f"Invalid parameter format: {e}")
        except Exception as e:
            logger.error(f"Error getting RRD data: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cors_enabled
    def packet_type_graph_data(self, hours=24, resolution='average', types='all'):
        try:
            hours = int(hours)
            start_time, end_time = self._get_time_range(hours)
            
            storage = self._get_storage()
            
            stats = storage.sqlite_handler.get_packet_type_stats(hours)
            if 'error' in stats:
                return self._error(stats['error'])
            
            packet_type_totals = stats.get('packet_type_totals', {})
            
            # Create simple bar chart data format for packet types
            series = []
            for type_name, count in packet_type_totals.items():
                if count > 0:  # Only include types with actual data
                    series.append({
                        "name": type_name,
                        "type": type_name.lower().replace(' ', '_').replace('(', '').replace(')', ''),
                        "data": [[end_time * 1000, count]]  # Single data point with total count
                    })
            
            # Sort series by count (descending)
            series.sort(key=lambda x: x['data'][0][1], reverse=True)
            
            graph_data = {
                "start_time": start_time,
                "end_time": end_time,
                "step": 3600,  # 1 hour step for simple bar chart
                "timestamps": [start_time, end_time],
                "series": series,
                "data_source": "sqlite",
                "chart_type": "bar"  # Indicate this is bar chart data
            }
            
            return self._success(graph_data)
            
        except ValueError as e:
            return self._error(f"Invalid parameter format: {e}")
        except Exception as e:
            logger.error(f"Error getting packet type graph data: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cors_enabled
    def metrics_graph_data(self, hours=24, resolution='average', metrics='all'):
        try:
            hours = int(hours)
            start_time, end_time = self._get_time_range(hours)
            
            rrd_data = self._get_storage().get_rrd_data(
                start_time=start_time, end_time=end_time, resolution=resolution
            )
            
            if not rrd_data or 'metrics' not in rrd_data:
                return self._error("No RRD data available")
            
            metric_names = {
                'rx_count': 'Received Packets', 'tx_count': 'Transmitted Packets',
                'drop_count': 'Dropped Packets', 'avg_rssi': 'Average RSSI (dBm)',
                'avg_snr': 'Average SNR (dB)', 'avg_length': 'Average Packet Length',
                'avg_score': 'Average Score', 'neighbor_count': 'Neighbor Count'
            }
            
            counter_metrics = ['rx_count', 'tx_count', 'drop_count']
            
            if metrics != 'all':
                requested_metrics = [m.strip() for m in metrics.split(',')]
            else:
                requested_metrics = list(rrd_data['metrics'].keys())
            
            timestamps_ms = [ts * 1000 for ts in rrd_data['timestamps']]
            series = []
            
            for metric_key in requested_metrics:
                if metric_key in rrd_data['metrics']:
                    if metric_key in counter_metrics:
                        chart_data = self._process_counter_data(rrd_data['metrics'][metric_key], timestamps_ms)
                    else:
                        chart_data = self._process_gauge_data(rrd_data['metrics'][metric_key], timestamps_ms)
                    
                    series.append({
                        "name": metric_names.get(metric_key, metric_key),
                        "type": metric_key,
                        "data": chart_data
                    })
            
            graph_data = {
                "start_time": rrd_data['start_time'],
                "end_time": rrd_data['end_time'],
                "step": rrd_data['step'],
                "timestamps": rrd_data['timestamps'],
                "series": series
            }
            
            return self._success(graph_data)
            
        except ValueError as e:
            return self._error(f"Invalid parameter format: {e}")
        except Exception as e:
            logger.error(f"Error getting metrics graph data: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()  
    @cherrypy.tools.json_in()
    @cors_enabled
    def cad_calibration_start(self):
        try:
            self._require_post()
            data = cherrypy.request.json or {}
            samples = data.get("samples", 8)
            delay = data.get("delay", 100)
            if self.cad_calibration.start_calibration(samples, delay):
                return self._success("Calibration started")
            else:
                return self._error("Calibration already running")
        except Exception as e:
            logger.error(f"Error starting CAD calibration: {e}")
            return self._error(e)
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cors_enabled
    def cad_calibration_stop(self):
        try:
            self._require_post()
            self.cad_calibration.stop_calibration()
            return self._success("Calibration stopped")
        except Exception as e:
            logger.error(f"Error stopping CAD calibration: {e}")
            return self._error(e)
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    @cors_enabled
    def save_cad_settings(self):
        try:
            self._require_post()
            data = cherrypy.request.json or {}
            peak = data.get("peak")
            min_val = data.get("min_val")
            detection_rate = data.get("detection_rate", 0)
            
            if peak is None or min_val is None:
                return self._error("Missing peak or min_val parameters")
            
            if self.daemon_instance and hasattr(self.daemon_instance, 'radio') and self.daemon_instance.radio:
                if hasattr(self.daemon_instance.radio, 'set_custom_cad_thresholds'):
                    self.daemon_instance.radio.set_custom_cad_thresholds(peak=peak, min_val=min_val)
                    logger.info(f"Applied CAD settings to radio: peak={peak}, min={min_val}")
            
            if "radio" not in self.config:
                self.config["radio"] = {}
            if "cad" not in self.config["radio"]:
                self.config["radio"]["cad"] = {}
            
            self.config["radio"]["cad"]["peak_threshold"] = peak
            self.config["radio"]["cad"]["min_threshold"] = min_val
            
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
            return self._error(e)

    def _save_config_to_file(self, config_path):
        try:
            import yaml
            import os
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, 'w') as f:
                yaml.dump(self.config, f, default_flow_style=False, indent=2)
            logger.info(f"Configuration saved to {config_path}")
        except Exception as e:
            logger.error(f"Failed to save config to {config_path}: {e}")
            raise

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cors_enabled
    def noise_floor_history(self, hours: int = 24):
        try:
            storage = self._get_storage()
            hours = int(hours)
            history = storage.get_noise_floor_history(hours=hours)
            
            return self._success({
                "history": history,
                "hours": hours,
                "count": len(history)
            })
        except Exception as e:
            logger.error(f"Error fetching noise floor history: {e}")
            return self._error(e)
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cors_enabled
    def noise_floor_stats(self, hours: int = 24):
        try:
            storage = self._get_storage()
            hours = int(hours)
            stats = storage.get_noise_floor_stats(hours=hours)
            
            return self._success({
                "stats": stats,
                "hours": hours
            })
        except Exception as e:
            logger.error(f"Error fetching noise floor stats: {e}")
            return self._error(e)
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cors_enabled
    def noise_floor_chart_data(self, hours: int = 24):
        try:
            storage = self._get_storage()
            hours = int(hours)
            chart_data = storage.get_noise_floor_rrd(hours=hours)
            
            return self._success({
                "chart_data": chart_data,
                "hours": hours
            })
        except Exception as e:
            logger.error(f"Error fetching noise floor chart data: {e}")
            return self._error(e)

    @cherrypy.expose
    def cad_calibration_stream(self):
        cherrypy.response.headers['Content-Type'] = 'text/event-stream'
        cherrypy.response.headers['Cache-Control'] = 'no-cache'
        cherrypy.response.headers['Connection'] = 'keep-alive'
        cherrypy.response.headers['Access-Control-Allow-Origin'] = '*'
        
        if not hasattr(self.cad_calibration, 'message_queue'):
            self.cad_calibration.message_queue = []
        
        def generate():
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
        
        return generate()

    cad_calibration_stream._cp_config = {'response.stream': True}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cors_enabled
    def adverts_by_contact_type(self, contact_type=None, limit=None, hours=None):

        try:
            if not contact_type:
                return self._error("contact_type parameter is required")
            
            limit_int = int(limit) if limit is not None else None
            hours_int = int(hours) if hours is not None else None
            
            storage = self._get_storage()
            adverts = storage.sqlite_handler.get_adverts_by_contact_type(
                contact_type=contact_type,
                limit=limit_int,
                hours=hours_int
            )
            
            return self._success(adverts, 
                                count=len(adverts),
                                contact_type=contact_type,
                                filters={
                                    "contact_type": contact_type,
                                    "limit": limit_int,
                                    "hours": hours_int
                                })
            
        except ValueError as e:
            return self._error(f"Invalid parameter format: {e}")
        except Exception as e:
            logger.error(f"Error getting adverts by contact type: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    @cors_enabled
    def transport_keys(self):
        if cherrypy.request.method == "GET":
            try:
                storage = self._get_storage()
                keys = storage.get_transport_keys()
                return self._success(keys, count=len(keys))
            except Exception as e:
                logger.error(f"Error getting transport keys: {e}")
                return self._error(e)
        
        elif cherrypy.request.method == "POST":
            try:
                data = cherrypy.request.json or {}
                name = data.get("name")
                flood_policy = data.get("flood_policy")
                transport_key = data.get("transport_key")  # Optional now
                parent_id = data.get("parent_id")
                last_used = data.get("last_used")
                
                if not name or not flood_policy:
                    return self._error("Missing required fields: name, flood_policy")
                
                if flood_policy not in ["allow", "deny"]:
                    return self._error("flood_policy must be 'allow' or 'deny'")
                
                # Convert ISO timestamp string to float if provided
                if last_used:
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(last_used.replace('Z', '+00:00'))
                        last_used = dt.timestamp()
                    except (ValueError, AttributeError):
                        # If conversion fails, use current time
                        last_used = time.time()
                else:
                    last_used = time.time()
                
                storage = self._get_storage()
                key_id = storage.create_transport_key(name, flood_policy, transport_key, parent_id, last_used)
                
                if key_id:
                    return self._success({"id": key_id}, message="Transport key created successfully")
                else:
                    return self._error("Failed to create transport key")
            except Exception as e:
                logger.error(f"Error creating transport key: {e}")
                return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    @cors_enabled
    def transport_key(self, key_id):
        if cherrypy.request.method == "GET":
            try:
                key_id = int(key_id)
                storage = self._get_storage()
                key = storage.get_transport_key_by_id(key_id)
                if key:
                    return self._success(key)
                else:
                    return self._error("Transport key not found")
            except ValueError:
                return self._error("Invalid key_id format")
            except Exception as e:
                logger.error(f"Error getting transport key: {e}")
                return self._error(e)
        
        elif cherrypy.request.method == "PUT":
            try:
                key_id = int(key_id)
                data = cherrypy.request.json or {}
                
                name = data.get("name")
                flood_policy = data.get("flood_policy")
                transport_key = data.get("transport_key")
                parent_id = data.get("parent_id")
                last_used = data.get("last_used")
                
                if flood_policy and flood_policy not in ["allow", "deny"]:
                    return self._error("flood_policy must be 'allow' or 'deny'")
                
                # Convert ISO timestamp string to float if provided
                if last_used:
                    try:
                        dt = datetime.fromisoformat(last_used.replace('Z', '+00:00'))
                        last_used = dt.timestamp()
                    except (ValueError, AttributeError):
                        # If conversion fails, leave as None to not update
                        last_used = None
                
                storage = self._get_storage()
                success = storage.update_transport_key(key_id, name, flood_policy, transport_key, parent_id, last_used)
                
                if success:
                    return self._success({"id": key_id}, message="Transport key updated successfully")
                else:
                    return self._error("Failed to update transport key or key not found")
            except ValueError:
                return self._error("Invalid key_id format")
            except Exception as e:
                logger.error(f"Error updating transport key: {e}")
                return self._error(e)
        
        elif cherrypy.request.method == "DELETE":
            try:
                key_id = int(key_id)
                storage = self._get_storage()
                success = storage.delete_transport_key(key_id)
                
                if success:
                    return self._success({"id": key_id}, message="Transport key deleted successfully")
                else:
                    return self._error("Failed to delete transport key or key not found")
            except ValueError:
                return self._error("Invalid key_id format")
            except Exception as e:
                logger.error(f"Error deleting transport key: {e}")
                return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    @cors_enabled
    def global_flood_policy(self):
        """
        Update global flood policy configuration
        
        POST /global_flood_policy
        Body: {"global_flood_allow": true/false}
        """
        if cherrypy.request.method == "POST":
            try:
                data = cherrypy.request.json or {}
                global_flood_allow = data.get("global_flood_allow")
                
                if global_flood_allow is None:
                    return self._error("Missing required field: global_flood_allow")
                
                if not isinstance(global_flood_allow, bool):
                    return self._error("global_flood_allow must be a boolean value")
                
                # Update the running configuration first (like CAD settings)
                if "mesh" not in self.config:
                    self.config["mesh"] = {}
                self.config["mesh"]["global_flood_allow"] = global_flood_allow
                
                # Get the actual config path from daemon instance (same as CAD settings)
                config_path = getattr(self, '_config_path', '/etc/pymc_repeater/config.yaml')
                if self.daemon_instance and hasattr(self.daemon_instance, 'config_path'):
                    config_path = self.daemon_instance.config_path
                
                logger.info(f"Using config path for global flood policy: {config_path}")
                
                # Update the configuration file using the same method as CAD
                try:
                    self._save_config_to_file(config_path)
                    logger.info(f"Updated running config and saved global flood policy to file: {'allow' if global_flood_allow else 'deny'}")
                except Exception as e:
                    logger.error(f"Failed to save global flood policy to file: {e}")
                    return self._error(f"Failed to save configuration to file: {e}")
                
                return self._success(
                    {"global_flood_allow": global_flood_allow},
                    message=f"Global flood policy updated to {'allow' if global_flood_allow else 'deny'} (live and saved)"
                )
                    
            except Exception as e:
                logger.error(f"Error updating global flood policy: {e}")
                return self._error(e)
        else:
            return self._error("Method not supported")