import json
import logging
import time
from datetime import datetime
from typing import Callable, Optional
import cherrypy
from repeater import __version__
from .cad_calibration_engine import CADCalibrationEngine

logger = logging.getLogger("HTTPServer")


# # system stars
# GET /api/stats
# GET /api/logs

# # Packets
# GET /api/packet_stats?hours=24
# GET /api/recent_packets?limit=100
# GET /api/filtered_packets?type=4&route=1&start_timestamp=X&end_timestamp=Y&limit=1000
# GET /api/packet_by_hash?packet_hash=abc123
# GET /api/packet_type_stats?hours=24

# # Charts & RRD
# GET /api/rrd_data?start_time=X&end_time=Y&resolution=average
# GET /api/packet_type_graph_data?hours=24&resolution=average&types=all
# GET /api/metrics_graph_data?hours=24&resolution=average&metrics=all

# # Noise Floor
# GET /api/noise_floor_history?hours=24
# GET /api/noise_floor_stats?hours=24  
# GET /api/noise_floor_chart_data?hours=24

# #   Repeater Control
# POST /api/send_advert
# POST /api/set_mode {"mode": "forward|monitor"}
# POST /api/set_duty_cycle {"enabled": true|false}

# # CAD Calibration
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
    def packet_stats(self):
        try:
            hours = int(cherrypy.request.params.get('hours', 24))
            stats = self._get_storage().get_packet_stats(hours=hours)
            return self._success(stats)
        except Exception as e:
            logger.error(f"Error getting packet stats: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def recent_packets(self):
        try:
            limit = int(cherrypy.request.params.get('limit', 100))
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
    def packet_type_stats(self):
        try:
            hours = int(cherrypy.request.params.get('hours', 24))
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
    def packet_type_graph_data(self):
        try:
            params = self._get_params({'hours': 24, 'resolution': 'average', 'types': 'all'})
            start_time, end_time = self._get_time_range(params['hours'])
            
            rrd_data = self._get_storage().get_rrd_data(
                start_time=start_time, end_time=end_time, resolution=params['resolution']
            )
            
            if not rrd_data or 'packet_types' not in rrd_data:
                return self._error("No RRD data available")
            
            packet_type_names = {
                'type_0': 'Request (REQ)', 'type_1': 'Response (RESPONSE)', 
                'type_2': 'Text Message (TXT_MSG)', 'type_3': 'ACK (ACK)',
                'type_4': 'Advert (ADVERT)', 'type_5': 'Group Text (GRP_TXT)',
                'type_6': 'Group Data (GRP_DATA)', 'type_7': 'Anonymous Request (ANON_REQ)',
                'type_8': 'Path (PATH)', 'type_9': 'Trace (TRACE)',
                'type_10': 'Reserved Type 10', 'type_11': 'Reserved Type 11',
                'type_12': 'Reserved Type 12', 'type_13': 'Reserved Type 13',
                'type_14': 'Reserved Type 14', 'type_15': 'Reserved Type 15',
                'type_other': 'Other Types (>15)'
            }
            
            if params['types'] != 'all':
                requested_types = [f'type_{t.strip()}' for t in params['types'].split(',')]
                if 'other' in params['types'].lower():
                    requested_types.append('type_other')
            else:
                requested_types = list(rrd_data['packet_types'].keys())
            
            timestamps_ms = [ts * 1000 for ts in rrd_data['timestamps']]
            series = []
            
            for type_key in requested_types:
                if type_key in rrd_data['packet_types']:
                    chart_data = self._process_counter_data(rrd_data['packet_types'][type_key], timestamps_ms)
                    series.append({
                        "name": packet_type_names.get(type_key, type_key),
                        "type": type_key,
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
            logger.error(f"Error getting packet type graph data: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def metrics_graph_data(self):
        try:
            params = self._get_params({'hours': 24, 'resolution': 'average', 'metrics': 'all'})
            start_time, end_time = self._get_time_range(params['hours'])
            
            rrd_data = self._get_storage().get_rrd_data(
                start_time=start_time, end_time=end_time, resolution=params['resolution']
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
            
            if params['metrics'] != 'all':
                requested_metrics = [m.strip() for m in params['metrics'].split(',')]
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