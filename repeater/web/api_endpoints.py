import json
import logging
import os
import time
from datetime import datetime
from typing import Callable, Optional
import cherrypy
from repeater import __version__
from repeater.config import update_global_flood_policy
from .cad_calibration_engine import CADCalibrationEngine
from pymc_core.protocol import CryptoUtils

logger = logging.getLogger("HTTPServer")


# System
# GET /api/stats
# GET /api/logs

# Packets
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

# Identity Management
# GET    /api/identities - List all identities
# GET    /api/identity?name=<name> - Get specific identity
# POST   /api/create_identity {"name": "...", "identity_key": "...", "type": "room_server", "settings": {...}}
# PUT    /api/update_identity {"name": "...", "new_name": "...", "identity_key": "...", "settings": {...}}
# DELETE /api/delete_identity?name=<name>
# POST   /api/send_room_server_advert {"name": "..."} - Send advert for room server

# ACL (Access Control List)
# GET  /api/acl_info - Get ACL configuration and stats for all identities
# GET  /api/acl_clients?identity_hash=0x42&identity_name=repeater - List authenticated clients
# POST /api/acl_remove_client {"public_key": "...", "identity_hash": "0x42"} - Remove client from ACL
# GET  /api/acl_stats - Overall ACL statistics

# Room Server
# GET  /api/room_messages?room_name=General&limit=50&offset=0 - Get messages from room
# GET  /api/room_messages?room_hash=0x42&limit=50 - Get messages by hash
# POST /api/room_post_message {"room_name": "General", "message": "Hello", "author_pubkey": "abc123"} - Post message
# GET  /api/room_stats?room_name=General - Get room statistics
# GET  /api/room_stats - Get all rooms statistics
# GET  /api/room_clients?room_name=General - Get clients synced to room
# DELETE /api/room_message?room_name=General&message_id=123 - Delete specific message
# DELETE /api/room_messages?room_name=General - Clear all messages in room

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

    def _is_cors_enabled(self):
        return self.config.get("web", {}).get("cors_enabled", False)

    def _set_cors_headers(self):
        if self._is_cors_enabled():
            cherrypy.response.headers['Access-Control-Allow-Origin'] = '*'
            cherrypy.response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
            cherrypy.response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'

    @cherrypy.expose
    def default(self, *args, **kwargs):
        """Handle default requests"""
        if cherrypy.request.method == "OPTIONS":
            return ""
        
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
            cherrypy.response.status = 405  # Method Not Allowed
            cherrypy.response.headers['Allow'] = 'POST'
            raise cherrypy.HTTPError(405, "Method not allowed. This endpoint requires POST.")

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
        except cherrypy.HTTPError:
            # Re-raise HTTP errors (like 405 Method Not Allowed) without logging
            raise
        except Exception as e:
            logger.error(f"Error sending advert: {e}", exc_info=True)
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
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
        except cherrypy.HTTPError:
            # Re-raise HTTP errors (like 405 Method Not Allowed) without logging
            raise
        except Exception as e:
            logger.error(f"Error setting mode: {e}", exc_info=True)
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
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
        except cherrypy.HTTPError:
            # Re-raise HTTP errors (like 405 Method Not Allowed) without logging
            raise
        except Exception as e:
            logger.error(f"Error setting duty cycle: {e}", exc_info=True)
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
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
    def hardware_stats(self):
        """Get comprehensive hardware statistics"""
        try:
            # Get hardware stats from storage collector
            storage = self._get_storage()
            if storage:
                stats = storage.get_hardware_stats()
                if stats:
                    return self._success(stats)
                else:
                    return self._error("Hardware stats not available (psutil may not be installed)")
            else:
                return self._error("Storage collector not available")
        except Exception as e:
            logger.error(f"Error getting hardware stats: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def hardware_processes(self):
        """Get summary of top processes"""
        try:
            # Get process stats from storage collector
            storage = self._get_storage()
            if storage:
                processes = storage.get_hardware_processes()
                if processes:
                    return self._success(processes)
                else:
                    return self._error("Process information not available (psutil may not be installed)")
            else:
                return self._error("Storage collector not available")
        except Exception as e:
            logger.error(f"Error getting process stats: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
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
        except cherrypy.HTTPError:
            # Re-raise HTTP errors (like 405 Method Not Allowed) without logging
            raise
        except Exception as e:
            logger.error(f"Error starting CAD calibration: {e}")
            return self._error(e)
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def cad_calibration_stop(self):
        
        try:
            self._require_post()
            self.cad_calibration.stop_calibration()
            return self._success("Calibration stopped")
        except cherrypy.HTTPError:
            # Re-raise HTTP errors (like 405 Method Not Allowed) without logging
            raise
        except Exception as e:
            logger.error(f"Error stopping CAD calibration: {e}")
            return self._error(e)
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
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
        except cherrypy.HTTPError:
            # Re-raise HTTP errors (like 405 Method Not Allowed) without logging
            raise
        except Exception as e:
            logger.error(f"Error saving CAD settings: {e}")
            return self._error(e)

    def _save_config_to_file(self, config_path):
        try:
            import yaml
            import os
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, 'w') as f:
                # Use safe_dump with explicit width to prevent line wrapping
                # Setting width to a very large number prevents truncation of long strings like identity keys
                yaml.safe_dump(
                    self.config, 
                    f, 
                    default_flow_style=False, 
                    indent=2, 
                    width=1000000,  # Very large width to prevent any line wrapping
                    sort_keys=False,
                    allow_unicode=True
                )
            logger.info(f"Configuration saved to {config_path}")
        except Exception as e:
            logger.error(f"Failed to save config to {config_path}: {e}")
            raise

    @cherrypy.expose
    @cherrypy.tools.json_out()
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

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def advert(self, advert_id):
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        elif cherrypy.request.method == "DELETE":
            try:
                advert_id = int(advert_id)
                storage = self._get_storage()
                success = storage.delete_advert(advert_id)
                
                if success:
                    return self._success({"id": advert_id}, message="Neighbor deleted successfully")
                else:
                    return self._error("Failed to delete neighbor or neighbor not found")
            except ValueError:
                return self._error("Invalid advert_id format")
            except Exception as e:
                logger.error(f"Error deleting neighbor: {e}")
                return self._error(e)
        else:
            return self._error("Method not supported")

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def ping_neighbor(self):
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        try:
            self._require_post()
            data = cherrypy.request.json or {}
            target_id = data.get("target_id")
            
            if not target_id:
                return self._error("Missing target_id parameter")
            
            # TODO: Implement actual ping functionality when available
            # For now, return success to indicate the endpoint works
            logger.info(f"Ping request for neighbor: {target_id}")
            return self._success({"target_id": target_id}, message="Ping sent successfully")
            
        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error pinging neighbor: {e}")
            return self._error(e)

    # ========== Identity Management Endpoints ==========
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def identities(self):
        """
        GET /api/identities - List all registered identities
        
        Returns both the in-memory registered identities and the configured ones from YAML
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            if not self.daemon_instance or not hasattr(self.daemon_instance, 'identity_manager'):
                return self._error("Identity manager not available")
            
            # Get runtime registered identities
            identity_manager = self.daemon_instance.identity_manager
            registered_identities = identity_manager.list_identities()
            
            # Get configured identities from config
            identities_config = self.config.get("identities", {})
            room_servers = identities_config.get("room_servers") or []
            
            # Enhance with config data
            configured = []
            for room_config in room_servers:
                name = room_config.get("name")
                identity_key = room_config.get("identity_key", "")
                settings = room_config.get("settings", {})
                
                # Find matching registered identity for additional data
                matching = next(
                    (r for r in registered_identities if r["name"] == f"room_server:{name}"),
                    None
                )
                
                configured.append({
                    "name": name,
                    "type": "room_server",
                    "identity_key": identity_key[:16] + "..." if len(identity_key) > 16 else identity_key,
                    "identity_key_length": len(identity_key),
                    "settings": settings,
                    "hash": matching["hash"] if matching else None,
                    "address": matching["address"] if matching else None,
                    "registered": matching is not None
                })
            
            return self._success({
                "registered": registered_identities,
                "configured": configured,
                "total_registered": len(registered_identities),
                "total_configured": len(configured)
            })
            
        except Exception as e:
            logger.error(f"Error listing identities: {e}")
            return self._error(e)
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def identity(self, name=None):
        """
        GET /api/identity?name=<name> - Get a specific identity by name
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            if not name:
                return self._error("Missing name parameter")
            
            identities_config = self.config.get("identities", {})
            room_servers = identities_config.get("room_servers") or []
            
            # Find the identity in config
            identity_config = next(
                (r for r in room_servers if r.get("name") == name),
                None
            )
            
            if not identity_config:
                return self._error(f"Identity '{name}' not found")
            
            # Get runtime info if available
            if self.daemon_instance and hasattr(self.daemon_instance, 'identity_manager'):
                identity_manager = self.daemon_instance.identity_manager
                runtime_info = identity_manager.get_identity_by_name(name)
                
                if runtime_info:
                    identity_obj, config, identity_type = runtime_info
                    identity_config["runtime"] = {
                        "hash": f"0x{identity_obj.get_public_key()[0]:02X}",
                        "address": identity_obj.get_address_bytes().hex(),
                        "type": identity_type,
                        "registered": True
                    }
                else:
                    identity_config["runtime"] = {"registered": False}
            
            return self._success(identity_config)
            
        except Exception as e:
            logger.error(f"Error getting identity: {e}")
            return self._error(e)
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def create_identity(self):
        """
        POST /api/create_identity - Create a new identity
        
        Body: {
            "name": "MyRoomServer",
            "identity_key": "hex_key_string",  # Optional - will be auto-generated if not provided
            "type": "room_server",
            "settings": {
                "node_name": "My Room",
                "latitude": 0.0,
                "longitude": 0.0,
                "disable_fwd": true,
                "admin_password": "secret123",  # Optional - admin access password
                "guest_password": "guest456"    # Optional - guest/read-only access password
            }
        }
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            self._require_post()
            data = cherrypy.request.json or {}
            
            name = data.get("name")
            identity_key = data.get("identity_key")
            identity_type = data.get("type", "room_server")
            settings = data.get("settings", {})
            
            if not name:
                return self._error("Missing required field: name")
            
            # Validate passwords are different if both provided
            admin_pw = settings.get("admin_password")
            guest_pw = settings.get("guest_password")
            if admin_pw and guest_pw and admin_pw == guest_pw:
                return self._error("admin_password and guest_password must be different")
            
            # Auto-generate identity key if not provided
            key_was_generated = False
            if not identity_key:
                try:
                    # Generate a new random 32-byte key (same method as config.py)
                    random_key = os.urandom(32)
                    identity_key = random_key.hex()
                    key_was_generated = True
                    logger.info(f"Auto-generated identity key for '{name}': {identity_key[:16]}...")
                except Exception as gen_error:
                    logger.error(f"Failed to auto-generate identity key: {gen_error}")
                    return self._error(f"Failed to auto-generate identity key: {gen_error}")
            
            # Validate identity type
            if identity_type not in ["room_server"]:
                return self._error(f"Invalid identity type: {identity_type}. Only 'room_server' is supported.")
            
            # Check if identity already exists
            identities_config = self.config.get("identities", {})
            room_servers = identities_config.get("room_servers") or []
            
            if any(r.get("name") == name for r in room_servers):
                return self._error(f"Identity with name '{name}' already exists")
            
            # Create new identity config
            new_identity = {
                "name": name,
                "identity_key": identity_key,
                "type": identity_type,
                "settings": settings
            }
            
            # Add to config
            room_servers.append(new_identity)
            
            if "identities" not in self.config:
                self.config["identities"] = {}
            self.config["identities"]["room_servers"] = room_servers
            
            # Save to file
            config_path = getattr(self, '_config_path', '/etc/pymc_repeater/config.yaml')
            if self.daemon_instance and hasattr(self.daemon_instance, 'config_path'):
                config_path = self.daemon_instance.config_path
            
            self._save_config_to_file(config_path)
            
            logger.info(f"Created new identity: {name} (type: {identity_type}){' with auto-generated key' if key_was_generated else ''}")
            
            # Hot reload - register identity immediately
            registration_success = False
            if self.daemon_instance:
                try:
                    from pymc_core import LocalIdentity
                    
                    # Create LocalIdentity from the key (convert hex string to bytes)
                    if isinstance(identity_key, bytes):
                        identity_key_bytes = identity_key
                    elif isinstance(identity_key, str):
                        try:
                            identity_key_bytes = bytes.fromhex(identity_key)
                        except ValueError as e:
                            logger.error(f"Identity key for {name} is not valid hex string: {e}")
                            identity_key_bytes = identity_key.encode('latin-1') if len(identity_key) == 32 else identity_key.encode('utf-8')
                    else:
                        logger.error(f"Unknown identity_key type: {type(identity_key)}")
                        identity_key_bytes = bytes(identity_key)
                    
                    room_identity = LocalIdentity(seed=identity_key_bytes)
                    
                    # Use the consolidated registration method
                    if hasattr(self.daemon_instance, '_register_identity_everywhere'):
                        registration_success = self.daemon_instance._register_identity_everywhere(
                            name=name,
                            identity=room_identity,
                            config=new_identity,
                            identity_type=identity_type
                        )
                        if registration_success:
                            logger.info(f"Hot reload: Registered identity '{name}' with all systems")
                        else:
                            logger.warning(f"Hot reload: Failed to register identity '{name}'")
                    
                except Exception as reg_error:
                    logger.error(f"Failed to hot reload identity {name}: {reg_error}", exc_info=True)
            
            message = f"Identity '{name}' created successfully and activated immediately!" if registration_success else f"Identity '{name}' created successfully. Restart required to activate."
            if key_was_generated:
                message += " Identity key was auto-generated."
            
            return self._success(
                new_identity,
                message=message
            )
            
        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error creating identity: {e}")
            return self._error(e)
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def update_identity(self):
        """
        PUT /api/update_identity - Update an existing identity
        
        Body: {
            "name": "MyRoomServer",  # Required - used to find identity
            "new_name": "RenamedRoom",  # Optional - rename identity
            "identity_key": "new_hex_key",  # Optional - update key
            "settings": {  # Optional - update settings
                "node_name": "Updated Room Name",
                "latitude": 1.0,
                "longitude": 2.0,
                "admin_password": "newsecret",  # Optional - admin password
                "guest_password": "newguest"    # Optional - guest password
            }
        }
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            if cherrypy.request.method != "PUT":
                cherrypy.response.status = 405
                cherrypy.response.headers['Allow'] = 'PUT'
                raise cherrypy.HTTPError(405, "Method not allowed. This endpoint requires PUT.")
            
            data = cherrypy.request.json or {}
            
            name = data.get("name")
            if not name:
                return self._error("Missing required field: name")
            
            identities_config = self.config.get("identities", {})
            room_servers = identities_config.get("room_servers") or []
            
            # Find the identity
            identity_index = next(
                (i for i, r in enumerate(room_servers) if r.get("name") == name),
                None
            )
            
            if identity_index is None:
                return self._error(f"Identity '{name}' not found")
            
            # Update fields
            identity = room_servers[identity_index]
            
            if "new_name" in data:
                new_name = data["new_name"]
                # Check if new name conflicts
                if any(r.get("name") == new_name for i, r in enumerate(room_servers) if i != identity_index):
                    return self._error(f"Identity with name '{new_name}' already exists")
                identity["name"] = new_name
            
            # Only update identity_key if a valid full key is provided
            # Silently reject truncated keys (containing "...") or invalid hex strings
            if "identity_key" in data and data["identity_key"]:
                new_key = data["identity_key"]
                # Check if it's a truncated key (contains "...") or not a valid 64-char hex string
                if "..." not in new_key and len(new_key) == 64:
                    try:
                        # Validate it's proper hex
                        bytes.fromhex(new_key)
                        identity["identity_key"] = new_key
                        logger.info(f"Updated identity_key for '{name}'")
                    except ValueError:
                        # Invalid hex, silently ignore
                        pass
            
            if "settings" in data:
                # Merge settings
                if "settings" not in identity:
                    identity["settings"] = {}
                identity["settings"].update(data["settings"])
                
                # Validate passwords are different if both are now set
                admin_pw = identity["settings"].get("admin_password")
                guest_pw = identity["settings"].get("guest_password")
                if admin_pw and guest_pw and admin_pw == guest_pw:
                    return self._error("admin_password and guest_password must be different")
            
            # Save to config
            room_servers[identity_index] = identity
            self.config["identities"]["room_servers"] = room_servers
            
            config_path = getattr(self, '_config_path', '/etc/pymc_repeater/config.yaml')
            if self.daemon_instance and hasattr(self.daemon_instance, 'config_path'):
                config_path = self.daemon_instance.config_path
            
            self._save_config_to_file(config_path)
            
            logger.info(f"Updated identity: {name}")
            
            # Hot reload - re-register identity if key changed or name changed
            registration_success = False
            # Only reload if identity_key was actually provided and not empty, or if name changed
            needs_reload = (data.get("identity_key") or "new_name" in data)
            
            if needs_reload and self.daemon_instance:
                try:
                    from pymc_core import LocalIdentity
                    
                    final_name = identity["name"]  # Could be new_name
                    identity_key = identity["identity_key"]
                    
                    # Create LocalIdentity from the key (convert hex string to bytes)
                    if isinstance(identity_key, bytes):
                        identity_key_bytes = identity_key
                    elif isinstance(identity_key, str):
                        try:
                            identity_key_bytes = bytes.fromhex(identity_key)
                        except ValueError as e:
                            logger.error(f"Identity key for {final_name} is not valid hex string: {e}")
                            identity_key_bytes = identity_key.encode('latin-1') if len(identity_key) == 32 else identity_key.encode('utf-8')
                    else:
                        logger.error(f"Unknown identity_key type: {type(identity_key)}")
                        identity_key_bytes = bytes(identity_key)
                    
                    room_identity = LocalIdentity(seed=identity_key_bytes)
                    
                    # Use the consolidated registration method
                    if hasattr(self.daemon_instance, '_register_identity_everywhere'):
                        registration_success = self.daemon_instance._register_identity_everywhere(
                            name=final_name,
                            identity=room_identity,
                            config=identity,
                            identity_type="room_server"
                        )
                        if registration_success:
                            logger.info(f"Hot reload: Re-registered identity '{final_name}' with all systems")
                        else:
                            logger.warning(f"Hot reload: Failed to re-register identity '{final_name}'")
                    
                except Exception as reg_error:
                    logger.error(f"Failed to hot reload identity {name}: {reg_error}", exc_info=True)
            
            if needs_reload:
                message = f"Identity '{name}' updated successfully and changes applied immediately!" if registration_success else f"Identity '{name}' updated successfully. Restart required to apply changes."
            else:
                message = f"Identity '{name}' updated successfully (settings only, no reload needed)."
            
            return self._success(
                identity,
                message=message
            )
            
        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error updating identity: {e}")
            return self._error(e)
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def delete_identity(self, name=None):
        """
        DELETE /api/delete_identity?name=<name> - Delete an identity
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            if cherrypy.request.method != "DELETE":
                cherrypy.response.status = 405
                cherrypy.response.headers['Allow'] = 'DELETE'
                raise cherrypy.HTTPError(405, "Method not allowed. This endpoint requires DELETE.")
            
            if not name:
                return self._error("Missing name parameter")
            
            identities_config = self.config.get("identities", {})
            room_servers = identities_config.get("room_servers") or []
            
            # Find and remove the identity
            initial_count = len(room_servers)
            room_servers = [r for r in room_servers if r.get("name") != name]
            
            if len(room_servers) == initial_count:
                return self._error(f"Identity '{name}' not found")
            
            # Update config
            self.config["identities"]["room_servers"] = room_servers
            
            config_path = getattr(self, '_config_path', '/etc/pymc_repeater/config.yaml')
            if self.daemon_instance and hasattr(self.daemon_instance, 'config_path'):
                config_path = self.daemon_instance.config_path
            
            self._save_config_to_file(config_path)
            
            logger.info(f"Deleted identity: {name}")
            
            unregister_success = False
            if self.daemon_instance:
                try:
                    if hasattr(self.daemon_instance, 'identity_manager'):
                        identity_manager = self.daemon_instance.identity_manager
                        
                        # Remove from named_identities dict
                        if name in identity_manager.named_identities:
                            del identity_manager.named_identities[name]
                            logger.info(f"Removed identity {name} from named_identities")
                            unregister_success = True
                        
                        # Note: We don't remove from identities dict (keyed by hash)
                        # because we'd need to look up the hash first, and there could
                        # be multiple identities with the same hash
                        # Full cleanup happens on restart
                    
                except Exception as unreg_error:
                    logger.error(f"Failed to unregister identity {name}: {unreg_error}", exc_info=True)
            
            message = f"Identity '{name}' deleted successfully and deactivated immediately!" if unregister_success else f"Identity '{name}' deleted successfully. Restart required to fully remove."
            
            return self._success(
                {"name": name},
                message=message
            )
            
        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error deleting identity: {e}")
            return self._error(e)
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def send_room_server_advert(self):
        """
        POST /api/send_room_server_advert - Send advert for a room server
        
        Body: {
            "name": "MyRoomServer"
        }
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            self._require_post()
            
            if not self.daemon_instance:
                return self._error("Daemon not available")
            
            data = cherrypy.request.json or {}
            name = data.get("name")
            
            if not name:
                return self._error("Missing required field: name")
            
            # Get the identity from identity manager
            if not hasattr(self.daemon_instance, 'identity_manager'):
                return self._error("Identity manager not available")
            
            identity_manager = self.daemon_instance.identity_manager
            identity_info = identity_manager.get_identity_by_name(name)
            
            if not identity_info:
                return self._error(f"Room server '{name}' not found or not registered")
            
            identity, config, identity_type = identity_info
            
            if identity_type != "room_server":
                return self._error(f"Identity '{name}' is not a room server")
            
            # Get settings from config
            settings = config.get("settings", {})
            node_name = settings.get("node_name", name)
            latitude = settings.get("latitude", 0.0)
            longitude = settings.get("longitude", 0.0)
            disable_fwd = settings.get("disable_fwd", False)
            
            # Send the advert asynchronously
            if self.event_loop is None:
                return self._error("Event loop not available")
            
            import asyncio
            future = asyncio.run_coroutine_threadsafe(
                self._send_room_server_advert_async(
                    identity=identity,
                    node_name=node_name,
                    latitude=latitude,
                    longitude=longitude,
                    disable_fwd=disable_fwd
                ),
                self.event_loop
            )
            
            result = future.result(timeout=10)
            
            if result:
                return self._success({
                    "name": name,
                    "node_name": node_name,
                    "latitude": latitude,
                    "longitude": longitude
                }, message=f"Advert sent for room server '{node_name}'")
            else:
                return self._error(f"Failed to send advert for room server '{name}'")
            
        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error sending room server advert: {e}", exc_info=True)
            return self._error(e)
    
    async def _send_room_server_advert_async(self, identity, node_name, latitude, longitude, disable_fwd):
        """Send advert for a room server identity"""
        try:
            from pymc_core.protocol import PacketBuilder
            from pymc_core.protocol.constants import ADVERT_FLAG_HAS_NAME, ADVERT_FLAG_IS_ROOM_SERVER
            
            if not self.daemon_instance or not self.daemon_instance.dispatcher:
                logger.error("Cannot send advert: dispatcher not initialized")
                return False
            
            # Build flags - just use HAS_NAME for room servers
            flags = ADVERT_FLAG_IS_ROOM_SERVER | ADVERT_FLAG_HAS_NAME
            
            packet = PacketBuilder.create_advert(
                local_identity=identity,
                name=node_name,
                lat=latitude,
                lon=longitude,
                feature1=0,
                feature2=0,
                flags=flags,
                route_type="flood",
            )
            
            # Send via dispatcher
            await self.daemon_instance.dispatcher.send_packet(packet, wait_for_ack=False)
            
            # Mark as seen to prevent re-forwarding
            if self.daemon_instance.repeater_handler:
                self.daemon_instance.repeater_handler.mark_seen(packet)
                logger.debug(f"Marked room server advert '{node_name}' as seen in duplicate cache")
            
            logger.info(f"Sent flood advert for room server '{node_name}' at ({latitude:.6f}, {longitude:.6f})")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send room server advert: {e}", exc_info=True)
            return False

    # ========== ACL (Access Control List) Endpoints ==========
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def acl_info(self):
        """
        GET /api/acl_info - Get ACL configuration and statistics
        
        Returns ACL settings for all registered identities including:
        - Identity name, type, and hash
        - Max clients allowed
        - Number of authenticated clients
        - Password configuration status
        - Read-only access setting
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            if not self.daemon_instance or not hasattr(self.daemon_instance, 'login_helper'):
                return self._error("Login helper not available")
            
            login_helper = self.daemon_instance.login_helper
            identity_manager = self.daemon_instance.identity_manager
            
            acl_dict = login_helper.get_acl_dict()
            
            acl_info_list = []
            
            # Add repeater identity
            if self.daemon_instance.local_identity:
                repeater_hash = self.daemon_instance.local_identity.get_public_key()[0]
                repeater_acl = acl_dict.get(repeater_hash)
                
                if repeater_acl:
                    acl_info_list.append({
                        "name": "repeater",
                        "type": "repeater",
                        "hash": f"0x{repeater_hash:02X}",
                        "max_clients": repeater_acl.max_clients,
                        "authenticated_clients": repeater_acl.get_num_clients(),
                        "has_admin_password": bool(repeater_acl.admin_password),
                        "has_guest_password": bool(repeater_acl.guest_password),
                        "allow_read_only": repeater_acl.allow_read_only
                    })
            
            # Add room server identities
            for name, identity, config in identity_manager.get_identities_by_type("room_server"):
                hash_byte = identity.get_public_key()[0]
                acl = acl_dict.get(hash_byte)
                
                if acl:
                    acl_info_list.append({
                        "name": name,
                        "type": "room_server",
                        "hash": f"0x{hash_byte:02X}",
                        "max_clients": acl.max_clients,
                        "authenticated_clients": acl.get_num_clients(),
                        "has_admin_password": bool(acl.admin_password),
                        "has_guest_password": bool(acl.guest_password),
                        "allow_read_only": acl.allow_read_only
                    })
            
            return self._success({
                "acls": acl_info_list,
                "total_identities": len(acl_info_list),
                "total_authenticated_clients": sum(a["authenticated_clients"] for a in acl_info_list)
            })
            
        except Exception as e:
            logger.error(f"Error getting ACL info: {e}")
            return self._error(e)
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def acl_clients(self, identity_hash=None, identity_name=None):
        """
        GET /api/acl_clients - Get authenticated clients
        
        Query parameters:
        - identity_hash: Filter by identity hash (e.g., "0x42")
        - identity_name: Filter by identity name (e.g., "repeater" or room server name)
        
        Returns list of authenticated clients with:
        - Public key (truncated)
        - Full address
        - Permissions (admin/guest)
        - Last activity timestamp
        - Last login timestamp
        - Identity they're authenticated to
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            if not self.daemon_instance or not hasattr(self.daemon_instance, 'login_helper'):
                return self._error("Login helper not available")
            
            login_helper = self.daemon_instance.login_helper
            identity_manager = self.daemon_instance.identity_manager
            acl_dict = login_helper.get_acl_dict()
            
            # Build a mapping of hash to identity info
            identity_map = {}
            
            # Add repeater
            if self.daemon_instance.local_identity:
                repeater_hash = self.daemon_instance.local_identity.get_public_key()[0]
                identity_map[repeater_hash] = {
                    "name": "repeater",
                    "type": "repeater",
                    "hash": f"0x{repeater_hash:02X}"
                }
            
            # Add room servers
            for name, identity, config in identity_manager.get_identities_by_type("room_server"):
                hash_byte = identity.get_public_key()[0]
                identity_map[hash_byte] = {
                    "name": name,
                    "type": "room_server",
                    "hash": f"0x{hash_byte:02X}"
                }
            
            # Filter by identity if requested
            target_hash = None
            if identity_hash:
                # Convert "0x42" to int
                try:
                    target_hash = int(identity_hash, 16) if identity_hash.startswith("0x") else int(identity_hash)
                except ValueError:
                    return self._error(f"Invalid identity_hash format: {identity_hash}")
            elif identity_name:
                # Find hash by name
                for hash_byte, info in identity_map.items():
                    if info["name"] == identity_name:
                        target_hash = hash_byte
                        break
                if target_hash is None:
                    return self._error(f"Identity '{identity_name}' not found")
            
            # Collect clients
            clients_list = []
            
            logger.info(f"ACL dict has {len(acl_dict)} identities")
            
            for hash_byte, acl in acl_dict.items():
                # Skip if filtering by specific identity
                if target_hash is not None and hash_byte != target_hash:
                    continue
                
                identity_info = identity_map.get(hash_byte, {
                    "name": "unknown",
                    "type": "unknown",
                    "hash": f"0x{hash_byte:02X}"
                })
                
                all_clients = acl.get_all_clients()
                logger.info(f"Identity {identity_info['name']} (0x{hash_byte:02X}) has {len(all_clients)} clients")
                
                for client in all_clients:
                    try:
                        pub_key = client.id.get_public_key()
                        
                        # Compute address from public key (first byte of SHA256)
                        address_bytes = CryptoUtils.sha256(pub_key)[:1]
                        
                        clients_list.append({
                            "public_key": pub_key[:8].hex() + "..." + pub_key[-4:].hex(),
                            "public_key_full": pub_key.hex(),
                            "address": address_bytes.hex(),
                            "permissions": "admin" if client.is_admin() else "guest",
                            "last_activity": client.last_activity,
                            "last_login_success": client.last_login_success,
                            "last_timestamp": client.last_timestamp,
                            "identity_name": identity_info["name"],
                            "identity_type": identity_info["type"],
                            "identity_hash": identity_info["hash"]
                        })
                    except Exception as client_error:
                        logger.error(f"Error processing client: {client_error}", exc_info=True)
                        continue
            
            logger.info(f"Returning {len(clients_list)} total clients")
            
            return self._success({
                "clients": clients_list,
                "count": len(clients_list),
                "filter": {
                    "identity_hash": identity_hash,
                    "identity_name": identity_name
                } if (identity_hash or identity_name) else None
            })
            
        except Exception as e:
            logger.error(f"Error getting ACL clients: {e}")
            return self._error(e)
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def acl_remove_client(self):
        """
        POST /api/acl_remove_client - Remove an authenticated client from ACL
        
        Body: {
            "public_key": "full_hex_string",
            "identity_hash": "0x42"  # Optional - if not provided, removes from all ACLs
        }
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            self._require_post()
            
            if not self.daemon_instance or not hasattr(self.daemon_instance, 'login_helper'):
                return self._error("Login helper not available")
            
            data = cherrypy.request.json or {}
            public_key_hex = data.get("public_key")
            identity_hash_str = data.get("identity_hash")
            
            if not public_key_hex:
                return self._error("Missing required field: public_key")
            
            # Convert hex to bytes
            try:
                public_key = bytes.fromhex(public_key_hex)
            except ValueError:
                return self._error("Invalid public_key format (must be hex string)")
            
            login_helper = self.daemon_instance.login_helper
            acl_dict = login_helper.get_acl_dict()
            
            # Determine which ACLs to remove from
            target_hashes = []
            if identity_hash_str:
                try:
                    target_hash = int(identity_hash_str, 16) if identity_hash_str.startswith("0x") else int(identity_hash_str)
                    target_hashes = [target_hash]
                except ValueError:
                    return self._error(f"Invalid identity_hash format: {identity_hash_str}")
            else:
                # Remove from all ACLs
                target_hashes = list(acl_dict.keys())
            
            removed_count = 0
            removed_from = []
            
            for hash_byte in target_hashes:
                acl = acl_dict.get(hash_byte)
                if acl and acl.remove_client(public_key):
                    removed_count += 1
                    removed_from.append(f"0x{hash_byte:02X}")
            
            if removed_count > 0:
                logger.info(f"Removed client {public_key[:6].hex()}... from {removed_count} ACL(s)")
                return self._success({
                    "removed_count": removed_count,
                    "removed_from": removed_from
                }, message=f"Client removed from {removed_count} ACL(s)")
            else:
                return self._error("Client not found in any ACL")
            
        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error removing client from ACL: {e}")
            return self._error(e)
    
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def acl_stats(self):
        """
        GET /api/acl_stats - Get overall ACL statistics
        
        Returns:
        - Total identities with ACLs
        - Total authenticated clients across all identities
        - Breakdown by identity type
        - Admin vs guest counts
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            if not self.daemon_instance or not hasattr(self.daemon_instance, 'login_helper'):
                return self._error("Login helper not available")
            
            login_helper = self.daemon_instance.login_helper
            identity_manager = self.daemon_instance.identity_manager
            acl_dict = login_helper.get_acl_dict()
            
            total_clients = 0
            admin_count = 0
            guest_count = 0
            
            identity_stats = {
                "repeater": {"count": 0, "clients": 0},
                "room_server": {"count": 0, "clients": 0}
            }
            
            # Count repeater
            if self.daemon_instance.local_identity:
                repeater_hash = self.daemon_instance.local_identity.get_public_key()[0]
                repeater_acl = acl_dict.get(repeater_hash)
                if repeater_acl:
                    identity_stats["repeater"]["count"] = 1
                    clients = repeater_acl.get_all_clients()
                    identity_stats["repeater"]["clients"] = len(clients)
                    total_clients += len(clients)
                    
                    for client in clients:
                        if client.is_admin():
                            admin_count += 1
                        else:
                            guest_count += 1
            
            # Count room servers
            room_servers = identity_manager.get_identities_by_type("room_server")
            identity_stats["room_server"]["count"] = len(room_servers)
            
            for name, identity, config in room_servers:
                hash_byte = identity.get_public_key()[0]
                acl = acl_dict.get(hash_byte)
                if acl:
                    clients = acl.get_all_clients()
                    identity_stats["room_server"]["clients"] += len(clients)
                    total_clients += len(clients)
                    
                    for client in clients:
                        if client.is_admin():
                            admin_count += 1
                        else:
                            guest_count += 1
            
            return self._success({
                "total_identities": len(acl_dict),
                "total_clients": total_clients,
                "admin_clients": admin_count,
                "guest_clients": guest_count,
                "by_identity_type": identity_stats
            })
            
        except Exception as e:
            logger.error(f"Error getting ACL stats: {e}")
            return self._error(e)

    # ======================
    # Room Server Endpoints
    # ======================

    def _get_room_server_by_name_or_hash(self, room_name=None, room_hash=None):
        """Helper to get room server instance and metadata by name or hash."""
        if not self.daemon_instance or not hasattr(self.daemon_instance, 'text_helper'):
            raise Exception("Text helper not available")
        
        text_helper = self.daemon_instance.text_helper
        if not text_helper or not hasattr(text_helper, 'room_servers'):
            raise Exception("Room servers not initialized")
        
        identity_manager = text_helper.identity_manager
        
        # Find by name first
        if room_name:
            identities = identity_manager.get_identities_by_type("room_server")
            for name, identity, config in identities:
                if name == room_name:
                    hash_byte = identity.get_public_key()[0]
                    room_server = text_helper.room_servers.get(hash_byte)
                    if room_server:
                        return {
                            'room_server': room_server,
                            'name': name,
                            'hash': hash_byte,
                            'identity': identity,
                            'config': config
                        }
            raise Exception(f"Room '{room_name}' not found")
        
        # Find by hash
        if room_hash:
            if isinstance(room_hash, str):
                if room_hash.startswith('0x'):
                    hash_byte = int(room_hash, 16)
                else:
                    hash_byte = int(room_hash)
            else:
                hash_byte = room_hash
            
            room_server = text_helper.room_servers.get(hash_byte)
            if room_server:
                # Find name
                identities = identity_manager.get_identities_by_type("room_server")
                for name, identity, config in identities:
                    if identity.get_public_key()[0] == hash_byte:
                        return {
                            'room_server': room_server,
                            'name': name,
                            'hash': hash_byte,
                            'identity': identity,
                            'config': config
                        }
                # Found server but no name match
                return {
                    'room_server': room_server,
                    'name': f"Room_0x{hash_byte:02X}",
                    'hash': hash_byte,
                    'identity': None,
                    'config': {}
                }
            raise Exception(f"Room with hash {room_hash} not found")
        
        raise Exception("Must provide room_name or room_hash")

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def room_messages(self, room_name=None, room_hash=None, limit=50, offset=0, since_timestamp=None):
        """
        Get messages from a room server.
        
        Parameters:
            room_name: Name of the room
            room_hash: Hash of room identity (alternative to name)
            limit: Max messages to return (default 50)
            offset: Skip first N messages (default 0)
            since_timestamp: Only return messages after this timestamp
        
        Returns:
            {
                "success": true,
                "data": {
                    "room_name": "General",
                    "room_hash": "0x42",
                    "messages": [
                        {
                            "id": 1,
                            "author_pubkey": "abc123...",
                            "author_prefix": "abc1",
                            "post_timestamp": 1234567890.0,
                            "sender_timestamp": 1234567890,
                            "message_text": "Hello world",
                            "txt_type": 0,
                            "created_at": 1234567890.0
                        }
                    ],
                    "count": 1,
                    "total": 100,
                    "limit": 50,
                    "offset": 0
                }
            }
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            room_info = self._get_room_server_by_name_or_hash(room_name, room_hash)
            room_server = room_info['room_server']
            
            # Get messages from database
            db = room_server.db
            room_hash_str = f"0x{room_info['hash']:02X}"
            
            # Get total count
            total_count = db.get_room_message_count(room_hash_str)
            
            # Get messages
            if since_timestamp:
                messages = db.get_messages_since(
                    room_hash=room_hash_str,
                    since_timestamp=float(since_timestamp),
                    limit=int(limit)
                )
            else:
                messages = db.get_room_messages(
                    room_hash=room_hash_str,
                    limit=int(limit),
                    offset=int(offset)
                )
            
            # Format messages with author prefix and lookup sender names
            storage = self._get_storage()
            formatted_messages = []
            for msg in messages:
                author_pubkey = msg['author_pubkey']
                formatted_msg = {
                    'id': msg['id'],
                    'author_pubkey': author_pubkey,
                    'author_prefix': author_pubkey[:8] if author_pubkey else '',
                    'post_timestamp': msg['post_timestamp'],
                    'sender_timestamp': msg['sender_timestamp'],
                    'message_text': msg['message_text'],
                    'txt_type': msg['txt_type'],
                    'created_at': msg.get('created_at', msg['post_timestamp'])
                }
                
                # Lookup sender name from adverts table
                if author_pubkey:
                    author_name = storage.get_node_name_by_pubkey(author_pubkey)
                    if author_name:
                        formatted_msg['author_name'] = author_name
                
                formatted_messages.append(formatted_msg)
            
            return self._success({
                'room_name': room_info['name'],
                'room_hash': room_hash_str,
                'messages': formatted_messages,
                'count': len(formatted_messages),
                'total': total_count,
                'limit': int(limit),
                'offset': int(offset)
            })
            
        except Exception as e:
            logger.error(f"Error getting room messages: {e}", exc_info=True)
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def room_post_message(self):
        """
        Post a message to a room server.
        
        POST Body:
            {
                "room_name": "General",  // or "room_hash": "0x42"
                "message": "Hello world",
                "author_pubkey": "abc123...",  // hex string, or "server" for system messages
                "txt_type": 0  // optional, default 0
            }
        
        Special Values for author_pubkey:
            - "server" or "system": Uses SERVER_AUTHOR_PUBKEY (all zeros), message goes to ALL clients
            - Any other hex string: Normal behavior, message NOT sent to that client
        
        Returns:
            {"success": true, "data": {"message_id": 123}}
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            self._require_post()
            
            data = cherrypy.request.json
            room_name = data.get('room_name')
            room_hash = data.get('room_hash')
            message = data.get('message')
            author_pubkey = data.get('author_pubkey')
            txt_type = data.get('txt_type', 0)
            
            if not message:
                return self._error("message is required")
            if not author_pubkey:
                return self._error("author_pubkey is required")
            
            # Convert author_pubkey to bytes
            try:
                # Special case: "server" or "system" = use room server's public key
                # This allows clients to identify which room server sent the message
                if isinstance(author_pubkey, str) and author_pubkey.lower() in ('server', 'system'):
                    # Get room server first to access its identity
                    room_info = self._get_room_server_by_name_or_hash(room_name, room_hash)
                    room_server = room_info['room_server']
                    # Use the room server's actual public key
                    author_bytes = room_server.local_identity.get_public_key()
                    author_pubkey = author_bytes.hex()
                    is_server_message = True
                elif isinstance(author_pubkey, str):
                    author_bytes = bytes.fromhex(author_pubkey)
                    is_server_message = False
                else:
                    author_bytes = bytes(author_pubkey)
                    is_server_message = False
            except Exception as e:
                return self._error(f"Invalid author_pubkey: {e}")
            
            # Get room server (if not already retrieved above)
            if not isinstance(author_pubkey, str) or author_pubkey.lower() not in ('server', 'system'):
                room_info = self._get_room_server_by_name_or_hash(room_name, room_hash)
                room_server = room_info['room_server']
            
            # Add post to room (will be distributed asynchronously)
            import asyncio
            if self.event_loop:
                sender_timestamp = int(time.time())
                # SECURITY: Server messages (using room server's key) go to ALL clients
                # API is allowed to send these (TODO: Add authentication/authorization)
                future = asyncio.run_coroutine_threadsafe(
                    room_server.add_post(
                        client_pubkey=author_bytes,
                        message_text=message,
                        sender_timestamp=sender_timestamp,
                        txt_type=txt_type,
                        allow_server_author=is_server_message  # Allow server key from API
                    ),
                    self.event_loop
                )
                success = future.result(timeout=5)
                
                if success:
                    # Get the message ID (last inserted)
                    db = room_server.db
                    room_hash_str = f"0x{room_info['hash']:02X}"
                    messages = db.get_room_messages(room_hash_str, limit=1, offset=0)
                    message_id = messages[0]['id'] if messages else None
                    
                    return self._success({
                        'message_id': message_id,
                        'room_name': room_info['name'],
                        'room_hash': room_hash_str,
                        'queued_for_distribution': True,
                        'is_server_message': is_server_message,
                        'author_filter_note': 'Server messages go to ALL clients' if is_server_message else 'Message will NOT be sent to author'
                    })
                else:
                    return self._error("Failed to add message (rate limit or validation error)")
            else:
                return self._error("Event loop not available")
                
        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error posting room message: {e}", exc_info=True)
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def room_stats(self, room_name=None, room_hash=None):
        """
        Get statistics for one or all room servers.
        
        Parameters:
            room_name: Name of specific room (optional)
            room_hash: Hash of specific room (optional)
        
        If no parameters, returns stats for all rooms.
        
        Returns:
            {
                "success": true,
                "data": {
                    "room_name": "General",
                    "room_hash": "0x42",
                    "total_messages": 100,
                    "total_clients": 5,
                    "active_clients": 3,
                    "max_posts": 32,
                    "sync_running": true,
                    "clients": [
                        {
                            "pubkey": "abc123...",
                            "pubkey_prefix": "abc1",
                            "sync_since": 1234567890.0,
                            "unsynced_count": 2,
                            "pending_ack": false,
                            "push_failures": 0,
                            "last_activity": 1234567890.0
                        }
                    ]
                }
            }
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            if not self.daemon_instance or not hasattr(self.daemon_instance, 'text_helper'):
                return self._error("Text helper not available")
            
            text_helper = self.daemon_instance.text_helper
            
            # Get all rooms if no specific room requested
            if not room_name and not room_hash:
                all_rooms = []
                for hash_byte, room_server in text_helper.room_servers.items():
                    # Find room name
                    room_name_found = f"Room_0x{hash_byte:02X}"
                    identities = text_helper.identity_manager.get_identities_by_type("room_server")
                    for name, identity, config in identities:
                        if identity.get_public_key()[0] == hash_byte:
                            room_name_found = name
                            break
                    
                    db = room_server.db
                    room_hash_str = f"0x{hash_byte:02X}"
                    
                    # Get basic stats
                    total_messages = db.get_room_message_count(room_hash_str)
                    all_clients_sync = db.get_all_room_clients(room_hash_str)
                    active_clients = sum(1 for c in all_clients_sync if c.get('last_activity', 0) > 0)
                    
                    all_rooms.append({
                        'room_name': room_name_found,
                        'room_hash': room_hash_str,
                        'total_messages': total_messages,
                        'total_clients': len(all_clients_sync),
                        'active_clients': active_clients,
                        'max_posts': room_server.max_posts,
                        'sync_running': room_server._running
                    })
                
                return self._success({
                    'rooms': all_rooms,
                    'total_rooms': len(all_rooms)
                })
            
            # Get specific room stats
            room_info = self._get_room_server_by_name_or_hash(room_name, room_hash)
            room_server = room_info['room_server']
            db = room_server.db
            room_hash_str = f"0x{room_info['hash']:02X}"
            
            # Get message count
            total_messages = db.get_room_message_count(room_hash_str)
            
            # Get client sync states
            all_clients_sync = db.get_all_room_clients(room_hash_str)
            
            # Get ACL for this room
            acl = None
            if room_info['hash'] in text_helper.acl_dict:
                acl = text_helper.acl_dict[room_info['hash']]
            
            # Format client info
            clients_info = []
            active_count = 0
            for client_sync in all_clients_sync:
                pubkey_hex = client_sync['client_pubkey']
                pubkey_bytes = bytes.fromhex(pubkey_hex)
                
                # Check if still in ACL
                in_acl = False
                if acl:
                    acl_clients = acl.get_all_clients()
                    in_acl = any(c.id.get_public_key() == pubkey_bytes for c in acl_clients)
                
                unsynced_count = db.get_unsynced_count(
                    room_hash=room_hash_str,
                    client_pubkey=pubkey_hex,
                    sync_since=client_sync.get('sync_since', 0)
                )
                
                is_active = client_sync.get('last_activity', 0) > 0
                if is_active:
                    active_count += 1
                
                clients_info.append({
                    'pubkey': pubkey_hex,
                    'pubkey_prefix': pubkey_hex[:8],
                    'sync_since': client_sync.get('sync_since', 0),
                    'unsynced_count': unsynced_count,
                    'pending_ack': client_sync.get('pending_ack_crc', 0) != 0,
                    'pending_ack_crc': client_sync.get('pending_ack_crc', 0),
                    'push_failures': client_sync.get('push_failures', 0),
                    'last_activity': client_sync.get('last_activity', 0),
                    'in_acl': in_acl,
                    'is_active': is_active
                })
            
            return self._success({
                'room_name': room_info['name'],
                'room_hash': room_hash_str,
                'total_messages': total_messages,
                'total_clients': len(all_clients_sync),
                'active_clients': active_count,
                'max_posts': room_server.max_posts,
                'sync_running': room_server._running,
                'next_push_time': room_server.next_push_time,
                'last_cleanup_time': room_server.last_cleanup_time,
                'clients': clients_info
            })
            
        except Exception as e:
            logger.error(f"Error getting room stats: {e}", exc_info=True)
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def room_clients(self, room_name=None, room_hash=None):
        """
        Get list of clients synced to a room.
        
        Parameters:
            room_name: Name of the room
            room_hash: Hash of room identity
        
        Returns:
            {
                "success": true,
                "data": {
                    "room_name": "General",
                    "room_hash": "0x42",
                    "clients": [...]
                }
            }
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            # Reuse room_stats logic but return only clients
            stats = self.room_stats(room_name=room_name, room_hash=room_hash)
            if stats.get('success') and 'clients' in stats.get('data', {}):
                data = stats['data']
                return self._success({
                    'room_name': data['room_name'],
                    'room_hash': data['room_hash'],
                    'clients': data['clients'],
                    'total': len(data['clients']),
                    'active': data['active_clients']
                })
            else:
                return stats
        except Exception as e:
            logger.error(f"Error getting room clients: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def room_message(self, room_name=None, room_hash=None, message_id=None):
        """
        Delete a specific message from a room.
        
        Parameters:
            room_name: Name of the room
            room_hash: Hash of room identity
            message_id: ID of message to delete
        
        Returns:
            {"success": true}
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            if cherrypy.request.method != "DELETE":
                cherrypy.response.status = 405
                return self._error("Method not allowed. Use DELETE.")
            
            if not message_id:
                return self._error("message_id is required")
            
            room_info = self._get_room_server_by_name_or_hash(room_name, room_hash)
            room_server = room_info['room_server']
            db = room_server.db
            room_hash_str = f"0x{room_info['hash']:02X}"
            
            # Delete message
            deleted = db.delete_room_message(room_hash_str, int(message_id))
            
            if deleted:
                return self._success({
                    'deleted': True,
                    'message_id': int(message_id),
                    'room_name': room_info['name']
                })
            else:
                return self._error("Message not found or already deleted")
                
        except Exception as e:
            logger.error(f"Error deleting room message: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def room_messages_clear(self, room_name=None, room_hash=None):
        """
        Clear all messages from a room.
        
        Parameters:
            room_name: Name of the room
            room_hash: Hash of room identity
        
        Returns:
            {"success": true, "data": {"deleted_count": 123}}
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            if cherrypy.request.method != "DELETE":
                cherrypy.response.status = 405
                return self._error("Method not allowed. Use DELETE.")
            
            room_info = self._get_room_server_by_name_or_hash(room_name, room_hash)
            room_server = room_info['room_server']
            db = room_server.db
            room_hash_str = f"0x{room_info['hash']:02X}"
            
            # Get count before deleting
            count_before = db.get_room_message_count(room_hash_str)
            
            # Clear all messages
            deleted = db.clear_room_messages(room_hash_str)
            
            return self._success({
                'deleted_count': deleted or count_before,
                'room_name': room_info['name'],
                'room_hash': room_hash_str
            })
            
        except Exception as e:
            logger.error(f"Error clearing room messages: {e}")
            return self._error(e)

    # ======================
    # OpenAPI Documentation
    # ======================

    @cherrypy.expose
    def openapi(self):
        """Serve OpenAPI specification in YAML format."""
        import os
        spec_path = os.path.join(os.path.dirname(__file__), 'openapi.yaml')
        try:
            with open(spec_path, 'r') as f:
                spec_content = f.read()
            cherrypy.response.headers['Content-Type'] = 'application/x-yaml'
            return spec_content.encode('utf-8')
        except FileNotFoundError:
            cherrypy.response.status = 404
            return b"OpenAPI spec not found"
        except Exception as e:
            cherrypy.response.status = 500
            return f"Error loading OpenAPI spec: {e}".encode('utf-8')

    @cherrypy.expose
    def docs(self):
        """Serve Swagger UI for interactive API documentation."""
        # Load HTML template from file
        template_path = os.path.join(os.path.dirname(__file__), 'html', 'swagger-ui.html')
        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                swagger_html = f.read()
        except FileNotFoundError:
            logger.error(f"Swagger UI template not found at {template_path}")
            cherrypy.response.status = 500
            return b"Swagger UI template not found"
        
        cherrypy.response.headers['Content-Type'] = 'text/html'
        return swagger_html.encode('utf-8')