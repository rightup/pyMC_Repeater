import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import cherrypy
from pymc_core.protocol import CryptoUtils

from repeater import __version__
from repeater.companion.identity_resolve import (
    derive_companion_public_key_hex,
    find_companion_index,
    heal_companion_empty_names,
)
from repeater.config import resolve_storage_dir, update_unscoped_flood_policy
from repeater.service_utils import get_buildroot_image_info

from .auth.middleware import require_auth
from .auth_endpoints import AuthAPIEndpoints
from .cad_calibration_engine import CADCalibrationEngine
from .companion_endpoints import CompanionAPIEndpoints
from .update_endpoints import UpdateAPIEndpoints

logger = logging.getLogger("HTTPServer")


# ============================================================================
# API ENDPOINT DOCUMENTATION
# ============================================================================

# Authentication (see auth_endpoints.py for implementation)
# POST   /auth/login - Authenticate and get JWT token
# POST   /auth/refresh - Refresh JWT token
# GET    /auth/verify - Verify current authentication
# POST   /auth/change_password - Change admin password
# GET    /api/auth/tokens - List all API tokens (RESTful)
# POST   /api/auth/tokens - Create new API token (RESTful)
# DELETE /api/auth/tokens/{token_id} - Revoke API token (RESTful)

# System
# GET    /api/stats - Get system statistics
# GET    /api/gps - Get local GPS diagnostics and parsed NMEA attributes
# GET    /api/gps_stream - GPS diagnostics SSE stream
# GET    /api/logs - Get system logs
# GET    /api/hardware_stats - Get hardware statistics
# GET    /api/hardware_processes - Get process information
# POST   /api/restart_service - Restart the repeater service
# GET    /api/openapi - Get OpenAPI specification

# Repeater Control
# POST   /api/send_advert - Send repeater advertisement
# POST   /api/set_mode {"mode": "forward|monitor|no_tx"} - Set repeater mode
# POST   /api/set_duty_cycle {"enabled": true|false} - Enable/disable duty cycle
# POST   /api/update_duty_cycle_config {"enabled": true, "on_time": 300, "off_time": 60} - Update duty cycle config
# POST   /api/update_radio_config - Update radio configuration
# POST   /api/update_advert_rate_limit_config - Update advert rate limiting settings
# GET    /api/mqtt_status - Get MQTT Observer connection status
# POST   /api/update_mqtt_config - Update MQTT Observer configuration

# Packets
# GET    /api/packet_stats?hours=24 - Get packet statistics
# GET    /api/packet_type_stats?hours=24 - Get packet type statistics
# GET    /api/route_stats?hours=24 - Get route statistics
# GET    /api/recent_packets?limit=100 - Get recent packets
# GET    /api/filtered_packets?type=4&route=1&start_timestamp=X&end_timestamp=Y&limit=1000 - Get filtered packets
# GET    /api/packet_by_hash?packet_hash=abc123 - Get specific packet by hash

# Charts & RRD
# GET    /api/rrd_data?start_time=X&end_time=Y&resolution=average - Get RRD data
# GET    /api/packet_type_graph_data?hours=24&resolution=average&types=all - Get packet type graph data
# GET    /api/metrics_graph_data?hours=24&resolution=average&metrics=all - Get metrics graph data

# Noise Floor
# GET    /api/noise_floor_history?hours=24 - Get noise floor history
# GET    /api/noise_floor_stats?hours=24 - Get noise floor statistics
# GET    /api/noise_floor_chart_data?hours=24 - Get noise floor chart data

# CAD Calibration
# POST   /api/cad_calibration_start {"samples": 8, "delay": 100} - Start CAD calibration
# POST   /api/cad_calibration_stop - Stop CAD calibration
# POST   /api/save_cad_settings {"peak": 127, "min_val": 64} - Save CAD settings
# GET    /api/cad_calibration_stream - CAD calibration SSE stream

# Adverts & Contacts
# GET    /api/adverts_by_contact_type?contact_type=X&limit=100&hours=24 - Get adverts by contact type
# GET    /api/advert?advert_id=123 - Get specific advert
# GET    /api/advert_rate_limit_stats - Get advert rate limiting and adaptive tier stats

# Transport Keys
# GET    /api/transport_keys - List all transport keys
# POST   /api/transport_keys - Create new transport key
# GET    /api/transport_key?key_id=X - Get specific transport key
# DELETE /api/transport_key?key_id=X - Delete transport key

# Network Policy
# GET    /api/unscoped_flood_policy - Get unscoped flood policy
# POST   /api/unscoped_flood_policy - Update unscoped flood policy
# POST   /api/ping_neighbor - Ping a neighbor node

# Identity Management
# GET    /api/identities - List all identities
# GET    /api/identity?name=<name> - Get specific identity
# POST   /api/create_identity {"name": "...", "identity_key": "...", "type": "room_server", "settings": {...}} - Create identity
# PUT    /api/update_identity {"name": "...", "new_name": "...", "identity_key": "...", "settings": {...}} - Update identity
# DELETE /api/delete_identity?name=<name> - Delete identity
# POST   /api/send_room_server_advert {"name": "...", "node_name": "...", "latitude": 0.0, "longitude": 0.0} - Send room server advert

# ACL (Access Control List)
# GET    /api/acl_info - Get ACL configuration and stats for all identities
# GET    /api/acl_clients?identity_hash=0x42&identity_name=repeater - List authenticated clients
# POST   /api/acl_remove_client {"public_key": "...", "identity_hash": "0x42"} - Remove client from ACL
# GET    /api/acl_stats - Overall ACL statistics

# Room Server
# GET    /api/room_messages?room_name=General&limit=50&offset=0&since_timestamp=X - Get messages from room
# GET    /api/room_messages?room_hash=0x42&limit=50 - Get messages by room hash
# POST   /api/room_post_message {"room_name": "General", "message": "Hello", "author_pubkey": "abc123"} - Post message
# GET    /api/room_stats?room_name=General - Get room statistics
# GET    /api/room_stats - Get all rooms statistics
# GET    /api/room_clients?room_name=General - Get clients synced to room
# DELETE /api/room_message?room_name=General&message_id=123 - Delete specific message
# DELETE /api/room_messages_clear?room_name=General - Clear all messages in room

# OTA Updates
# GET    /api/update/status          - Current + latest version, channel, state
# POST   /api/update/check           - Force fresh GitHub version check
# POST   /api/update/install         - Start background upgrade; stream via /progress
# GET    /api/update/progress        - SSE stream of live install log lines
# GET    /api/update/channels        - List available release channels (branches)
# POST   /api/update/set_channel     - Switch release channel {"channel": "dev"}

# Setup Wizard
# GET    /api/needs_setup - Check if repeater needs initial setup
# GET    /api/hardware_options - Get available hardware configurations
# GET    /api/radio_presets - Get radio preset configurations
# POST   /api/setup_wizard - Complete initial setup wizard

# Backup & Restore
# GET    /api/config_export - Export config as JSON (redacts secrets, ?include_secrets=true for full backup)
# POST   /api/config_import - Import config JSON and apply (supports full backup restore with secrets)
# GET    /api/identity_export - Export repeater identity key as hex string
# POST   /api/generate_vanity_key - Generate Ed25519 key with hex prefix {"prefix": "F8", "apply": false}
#
# Database Management
# GET    /api/db_stats  - Get table row counts, date ranges, database size
# POST   /api/db_purge  - Purge (empty) one or more tables
# POST   /api/db_vacuum - Reclaim disk space (VACUUM)

# Common Parameters
# hours - Time range in hours (default: 24)
# resolution - Data resolution: 'average', 'max', 'min' (default: 'average')
# limit - Maximum results (default varies by endpoint)
# offset - Result offset for pagination (default: 0)
# type - Packet type 0-15
# route - Route type 1-3
# ============================================================================


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
        self._config_path = config_path or "/etc/pymc_repeater/config.yaml"
        self.cad_calibration = CADCalibrationEngine(daemon_instance, event_loop)

        # Initialize ConfigManager for centralized config management
        from repeater.config_manager import ConfigManager

        self.config_manager = ConfigManager(
            config_path=self._config_path, config=self.config, daemon_instance=daemon_instance
        )

        # Create nested auth object for /api/auth/* routes
        self.auth = AuthAPIEndpoints()

        # Create nested companion object for /api/companion/* routes
        self.companion = CompanionAPIEndpoints(
            daemon_instance, event_loop, self.config, self.config_manager
        )

        # Create nested update object for /api/update/* routes
        self.update = UpdateAPIEndpoints()

    def _is_cors_enabled(self):
        return self.config.get("web", {}).get("cors_enabled", False)

    def _set_cors_headers(self):
        if self._is_cors_enabled():
            cherrypy.response.headers["Access-Control-Allow-Origin"] = "*"
            cherrypy.response.headers["Access-Control-Allow-Methods"] = (
                "GET, POST, PUT, DELETE, OPTIONS"
            )
            cherrypy.response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, Authorization"
            )

    @cherrypy.expose
    def default(self, *args, **kwargs):
        """Handle default requests"""
        if cherrypy.request.method == "OPTIONS":
            return ""

        raise cherrypy.HTTPError(404)

    def _get_storage(self):
        if not self.daemon_instance:
            raise Exception("Daemon not available")

        if (
            not hasattr(self.daemon_instance, "repeater_handler")
            or not self.daemon_instance.repeater_handler
        ):
            raise Exception("Repeater handler not initialized")

        if (
            not hasattr(self.daemon_instance.repeater_handler, "storage")
            or not self.daemon_instance.repeater_handler.storage
        ):
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
            cherrypy.response.headers["Allow"] = "POST"
            raise cherrypy.HTTPError(405, "Method not allowed. This endpoint requires POST.")

    def _fmt_hash(self, pubkey: bytes) -> str:
        """Format a node hash as a hex string respecting the configured path_hash_mode.

        path_hash_mode 0 (default) → 1-byte  "0x19"
        path_hash_mode 1           → 2-byte  "0x1927"
        path_hash_mode 2           → 3-byte  "0x192722"
        """
        mode = self.config.get("mesh", {}).get("path_hash_mode", 0)
        byte_count = {0: 1, 1: 2, 2: 3}.get(mode, 1)
        hex_chars = byte_count * 2
        value = int.from_bytes(bytes(pubkey[:byte_count]), "big")
        return f"0x{value:0{hex_chars}X}"

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

    # ============================================================================
    # SETUP WIZARD ENDPOINTS
    # ============================================================================

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def needs_setup(self):
        """Check if the repeater needs initial setup configuration"""
        try:
            config = self.config

            # Check for default values that indicate first-time setup
            node_name = config.get("repeater", {}).get("node_name", "")
            has_default_name = node_name in ["mesh-repeater-01", ""]

            admin_password = (
                config.get("repeater", {}).get("security", {}).get("admin_password", "")
            )
            has_default_password = admin_password in ["admin123", ""]

            needs_setup = has_default_name or has_default_password

            return {
                "needs_setup": needs_setup,
                "reasons": {
                    "default_name": has_default_name,
                    "default_password": has_default_password,
                },
            }
        except Exception as e:
            logger.error(f"Error checking setup status: {e}")
            return {"needs_setup": False, "error": str(e)}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def hardware_options(self):
        """Get available hardware configurations from radio-settings.json"""
        try:
            import json

            # Check config-based location first, then development location
            config_dir = resolve_storage_dir(self.config, config_path=self._config_path)
            installed_path = config_dir / "radio-settings.json"
            dev_path = os.path.join(os.path.dirname(__file__), "..", "..", "radio-settings.json")

            hardware_file = str(installed_path) if installed_path.exists() else dev_path
            hardware_list = []

            if os.path.exists(hardware_file):
                with open(hardware_file, "r") as f:
                    hardware_data = json.load(f)
                hardware_configs = hardware_data.get("hardware", {})
                for hw_key, hw_config in hardware_configs.items():
                    if isinstance(hw_config, dict):
                        hardware_list.append(
                            {
                                "key": hw_key,
                                "name": hw_config.get("name", hw_key),
                                "description": hw_config.get("description", ""),
                                "config": hw_config,
                            }
                        )

            # Add MeshCore KISS modem option (serial TNC)
            hardware_list.append(
                {
                    "key": "kiss",
                    "name": "KISS modem (serial)",
                    "description": "MeshCore KISS modem over serial – requires pyMC_core with KISS support",
                    "config": {},
                }
            )

            return {"hardware": hardware_list}
        except Exception as e:
            logger.error(f"Error loading hardware options: {e}")
            return {"error": str(e)}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def radio_presets(self):
        """Get radio preset configurations from local file"""
        try:
            import json

            # Check config-based location first, then development location
            config_dir = resolve_storage_dir(self.config, config_path=self._config_path)
            installed_path = config_dir / "radio-presets.json"
            dev_path = os.path.join(os.path.dirname(__file__), "..", "..", "radio-presets.json")

            presets_file = str(installed_path) if installed_path.exists() else dev_path

            if not os.path.exists(presets_file):
                logger.error(f"Presets file not found. Tried: {installed_path}, {dev_path}")
                return {"error": "Radio presets file not found"}

            with open(presets_file, "r") as f:
                presets_data = json.load(f)

            # Extract entries from local file
            entries = (
                presets_data.get("config", {})
                .get("suggested_radio_settings", {})
                .get("entries", [])
            )
            return {"presets": entries, "source": "local"}

        except Exception as e:
            logger.error(f"Error loading radio presets: {e}")
            return {"error": str(e)}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def setup_wizard(self):
        """Complete initial setup wizard configuration"""
        try:
            self._require_post()
            data = cherrypy.request.json

            # Validate required fields
            node_name = data.get("node_name", "").strip()
            if not node_name:
                return {"success": False, "error": "Node name is required"}
            # Validate UTF-8 byte length (31 bytes max + 1 null terminator = 32 bytes total)
            if len(node_name.encode("utf-8")) > 31:
                return {"success": False, "error": "Node name too long (max 31 bytes in UTF-8)"}

            hardware_key = data.get("hardware_key", "").strip()
            if not hardware_key:
                return {"success": False, "error": "Hardware selection is required"}

            radio_preset = data.get("radio_preset", {})
            if not radio_preset:
                return {"success": False, "error": "Radio preset selection is required"}

            admin_password = data.get("admin_password", "").strip()
            if not admin_password or len(admin_password) < 6:
                return {"success": False, "error": "Admin password must be at least 6 characters"}

            import json

            config_dir = resolve_storage_dir(self.config, config_path=self._config_path)
            installed_path = config_dir / "radio-settings.json"
            dev_path = os.path.join(os.path.dirname(__file__), "..", "..", "radio-settings.json")
            hardware_file = str(installed_path) if installed_path.exists() else dev_path

            if hardware_key != "kiss":
                if not os.path.exists(hardware_file):
                    logger.error(f"Hardware file not found. Tried: {installed_path}, {dev_path}")
                    return {"success": False, "error": "Hardware configuration file not found"}
                with open(hardware_file, "r") as f:
                    hardware_data = json.load(f)
                hardware_configs = hardware_data.get("hardware", {})
                hw_config = hardware_configs.get(hardware_key, {})
                if not hw_config:
                    return {"success": False, "error": f"Hardware configuration not found: {hardware_key}"}
            else:
                hw_config = {}

            import yaml

            # Read current config first so we can update it
            with open(self._config_path, "r") as f:
                config_yaml = yaml.safe_load(f)

            # Update repeater settings
            if "repeater" not in config_yaml:
                config_yaml["repeater"] = {}
            config_yaml["repeater"]["node_name"] = node_name

            if "security" not in config_yaml["repeater"]:
                config_yaml["repeater"]["security"] = {}
            config_yaml["repeater"]["security"]["admin_password"] = admin_password

            # Update radio settings - convert MHz/kHz to Hz (used for both SX1262 and KISS modem)
            if "radio" not in config_yaml:
                config_yaml["radio"] = {}
            freq_mhz = float(radio_preset.get("frequency", 0))
            bw_khz = float(radio_preset.get("bandwidth", 0))
            config_yaml["radio"]["frequency"] = int(freq_mhz * 1000000)
            config_yaml["radio"]["spreading_factor"] = int(radio_preset.get("spreading_factor", 7))
            config_yaml["radio"]["bandwidth"] = int(bw_khz * 1000)
            config_yaml["radio"]["coding_rate"] = int(radio_preset.get("coding_rate", 5))

            if hardware_key == "kiss":
                # KISS modem: set radio_type and kiss section (port/baud from request or defaults)
                config_yaml["radio_type"] = "kiss"
                kiss_port = (data.get("kiss_port") or "").strip() or "/dev/ttyUSB0"
                kiss_baud = int(data.get("kiss_baud_rate", data.get("kiss_baud", 115200)))
                config_yaml["kiss"] = {"port": kiss_port, "baud_rate": kiss_baud}
                config_yaml["radio"]["tx_power"] = int(radio_preset.get("tx_power", 14))
                if "preamble_length" not in config_yaml["radio"]:
                    config_yaml["radio"]["preamble_length"] = 17
            else:
                # SX1262 / sx1262_ch341: radio_type and optional CH341 from hw_config
                if "radio_type" in hw_config:
                    config_yaml["radio_type"] = hw_config.get("radio_type")
                else:
                    config_yaml["radio_type"] = "sx1262"

                ch341_cfg = hw_config.get("ch341") if isinstance(hw_config.get("ch341"), dict) else None
                vid = (ch341_cfg or {}).get("vid", hw_config.get("vid"))
                pid = (ch341_cfg or {}).get("pid", hw_config.get("pid"))
                if vid is not None or pid is not None:
                    if "ch341" not in config_yaml:
                        config_yaml["ch341"] = {}
                    if vid is not None:
                        config_yaml["ch341"]["vid"] = vid
                    if pid is not None:
                        config_yaml["ch341"]["pid"] = pid

                if "tx_power" in hw_config:
                    config_yaml["radio"]["tx_power"] = hw_config.get("tx_power", 22)
                if "preamble_length" in hw_config:
                    config_yaml["radio"]["preamble_length"] = hw_config.get("preamble_length", 17)

                if "sx1262" not in config_yaml:
                    config_yaml["sx1262"] = {}
                if "bus_id" in hw_config:
                    config_yaml["sx1262"]["bus_id"] = hw_config.get("bus_id", 0)
                if "cs_id" in hw_config:
                    config_yaml["sx1262"]["cs_id"] = hw_config.get("cs_id", 0)
                if "reset_pin" in hw_config:
                    config_yaml["sx1262"]["reset_pin"] = hw_config.get("reset_pin", 22)
                if "busy_pin" in hw_config:
                    config_yaml["sx1262"]["busy_pin"] = hw_config.get("busy_pin", 17)
                if "irq_pin" in hw_config:
                    config_yaml["sx1262"]["irq_pin"] = hw_config.get("irq_pin", 16)
                if "txen_pin" in hw_config:
                    config_yaml["sx1262"]["txen_pin"] = hw_config.get("txen_pin", -1)
                if "rxen_pin" in hw_config:
                    config_yaml["sx1262"]["rxen_pin"] = hw_config.get("rxen_pin", -1)
                if "en_pin" in hw_config:
                    config_yaml["sx1262"]["en_pin"] = hw_config.get("en_pin", -1)
                if "en_pins" in hw_config:
                    config_yaml["sx1262"]["en_pins"] = hw_config.get("en_pins", [])
                if "cs_pin" in hw_config:
                    config_yaml["sx1262"]["cs_pin"] = hw_config.get("cs_pin", -1)
                if "txled_pin" in hw_config:
                    config_yaml["sx1262"]["txled_pin"] = hw_config.get("txled_pin", -1)
                if "rxled_pin" in hw_config:
                    config_yaml["sx1262"]["rxled_pin"] = hw_config.get("rxled_pin", -1)
                if "use_dio3_tcxo" in hw_config:
                    config_yaml["sx1262"]["use_dio3_tcxo"] = hw_config.get("use_dio3_tcxo", False)
                if "dio3_tcxo_voltage" in hw_config:
                    config_yaml["sx1262"]["dio3_tcxo_voltage"] = hw_config.get("dio3_tcxo_voltage", 1.8)
                if "use_dio2_rf" in hw_config:
                    config_yaml["sx1262"]["use_dio2_rf"] = hw_config.get("use_dio2_rf", False)
                if "is_waveshare" in hw_config:
                    config_yaml["sx1262"]["is_waveshare"] = hw_config.get("is_waveshare", False)
            # Write updated config
            with open(self._config_path, "w") as f:
                yaml.dump(config_yaml, f, default_flow_style=False, sort_keys=False)

            logger.info(
                f"Setup wizard completed: node_name={node_name}, hardware={hardware_key}, freq={freq_mhz}MHz"
            )

            # Trigger service restart after setup
            import subprocess
            import threading

            def delayed_restart():
                import time

                time.sleep(2)  # Give time for response to be sent
                try:
                    from repeater.service_utils import restart_service
                    restart_service()
                except Exception as e:
                    logger.error(f"Failed to restart service: {e}")

            # Start restart in background thread
            restart_thread = threading.Thread(target=delayed_restart, daemon=True)
            restart_thread.start()

            result_config = {
                "node_name": node_name,
                "hardware": hardware_key,
                "radio_type": config_yaml.get("radio_type", "sx1262"),
                "frequency": freq_mhz,
                "spreading_factor": radio_preset.get("spreading_factor"),
                "bandwidth": radio_preset.get("bandwidth"),
                "coding_rate": radio_preset.get("coding_rate"),
            }
            if hardware_key == "kiss":
                result_config["kiss_port"] = config_yaml.get("kiss", {}).get("port")
                result_config["kiss_baud_rate"] = config_yaml.get("kiss", {}).get("baud_rate")
            return {
                "success": True,
                "message": "Setup completed successfully. Service is restarting...",
                "config": result_config,
            }

        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error completing setup wizard: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # ============================================================================
    # SYSTEM ENDPOINTS
    # ============================================================================

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
            image_info = get_buildroot_image_info()
            if image_info:
                if image_info.get("image_name"):
                    stats["image_name"] = image_info["image_name"]
                if image_info.get("image_version"):
                    stats["image_version"] = image_info["image_version"]
            return stats
        except Exception as e:
            logger.error(f"Error serving stats: {e}")
            return {"error": str(e)}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def gps(self):
        """Get full local GPS diagnostics and parsed NMEA attributes."""
        try:
            gps_service = getattr(self.daemon_instance, "gps_service", None)
            if gps_service:
                return self._success(gps_service.get_snapshot())

            return self._success(
                {
                    "enabled": False,
                    "running": False,
                    "source": self.config.get("gps", {}),
                    "status": {
                        "state": "disabled",
                        "fix_valid": False,
                        "stale": True,
                        "age_seconds": None,
                        "last_update": None,
                        "last_error": "GPS service is not initialized",
                    },
                    "fix": {
                        "valid": False,
                        "status": None,
                        "quality": None,
                        "quality_label": "no fix",
                        "gsa_fix_type": None,
                        "gsa_fix_type_label": None,
                    },
                    "position": {
                        "latitude": None,
                        "longitude": None,
                        "altitude_m": None,
                        "geoid_separation_m": None,
                    },
                    "motion": {
                        "speed_knots": None,
                        "speed_kmh": None,
                        "course_degrees": None,
                        "magnetic_variation_degrees": None,
                    },
                    "accuracy": {"hdop": None, "pdop": None, "vdop": None},
                    "time": {"utc_time": None, "date": None, "datetime_utc": None},
                    "location_update": {
                        "enabled": False,
                        "state": "disabled",
                        "last_attempt": None,
                        "last_success": None,
                        "last_error": None,
                        "last_latitude": None,
                        "last_longitude": None,
                        "interval_seconds": None,
                    },
                    "satellites": {
                        "used_count": None,
                        "used_prns": [],
                        "in_view_count": None,
                        "in_view": [],
                        "snr": {"min": None, "max": None, "avg": None},
                    },
                    "nmea": {
                        "last_sentence": None,
                        "last_sentence_type": None,
                        "last_talker": None,
                        "seen_sentence_types": [],
                        "sentence_counters": {},
                        "valid_checksum_count": 0,
                        "invalid_checksum_count": 0,
                        "missing_checksum_count": 0,
                        "recent_sentences": [],
                    },
                    "raw_attributes": {},
                }
            )
        except Exception as e:
            logger.error(f"Error serving GPS diagnostics: {e}", exc_info=True)
            return self._error(e)

    @cherrypy.expose
    def gps_stream(self):
        """Server-Sent Events stream for GPS diagnostics snapshots."""
        cherrypy.response.headers["Content-Type"] = "text/event-stream"
        cherrypy.response.headers["Cache-Control"] = "no-cache"
        cherrypy.response.headers["Connection"] = "keep-alive"

        def generate():
            last_snapshot_json: Optional[str] = None
            last_keepalive = time.time()

            try:
                yield (
                    f"data: {json.dumps({'type': 'connected', 'message': 'Connected to GPS stream'})}"
                    "\n\n"
                )

                while True:
                    response = self.gps()
                    if response.get("success"):
                        snapshot = response.get("data")
                        snapshot_json = json.dumps(snapshot, sort_keys=True, default=str)

                        if snapshot_json != last_snapshot_json:
                            yield (
                                f"data: {json.dumps({'type': 'snapshot', 'data': snapshot})}"
                                "\n\n"
                            )
                            last_snapshot_json = snapshot_json
                            last_keepalive = time.time()
                        elif (time.time() - last_keepalive) >= 15:
                            yield f"data: {json.dumps({'type': 'keepalive'})}\n\n"
                            last_keepalive = time.time()
                    else:
                        yield (
                            f"data: {json.dumps({'type': 'error', 'error': response.get('error', 'GPS stream error')})}"
                            "\n\n"
                        )

                    time.sleep(1.0)
            except GeneratorExit:
                logger.debug("GPS SSE stream closed by client")
            except Exception as exc:
                logger.error(f"GPS SSE stream error: {exc}", exc_info=True)

        return generate()

    gps_stream._cp_config = {"response.stream": True}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def send_advert(self):
        # Enable CORS for this endpoint
        self._set_cors_headers()

        if cherrypy.request.method == "OPTIONS":
            return ""

        try:
            self._require_post()
            if not self.send_advert_func:
                return self._error("Send advert function not configured")
            if self.event_loop is None:
                return self._error("Event loop not available")
            import asyncio

            future = asyncio.run_coroutine_threadsafe(self.send_advert_func(), self.event_loop)
            result = future.result(timeout=10)
            return (
                self._success("Advert sent successfully")
                if result
                else self._error("Failed to send advert")
            )
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
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()

        if cherrypy.request.method == "OPTIONS":
            return ""

        try:
            self._require_post()
            data = cherrypy.request.json
            new_mode = data.get("mode", "forward")
            if new_mode not in ["forward", "monitor", "no_tx"]:
                return self._error("Invalid mode. Must be 'forward', 'monitor', or 'no_tx'")
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
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()

        if cherrypy.request.method == "OPTIONS":
            return ""

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
    @cherrypy.tools.json_in()
    def update_duty_cycle_config(self):
        self._set_cors_headers()

        if cherrypy.request.method == "OPTIONS":
            return ""

        try:
            self._require_post()
            data = cherrypy.request.json or {}

            applied = []

            # Ensure config section exists
            if "duty_cycle" not in self.config:
                self.config["duty_cycle"] = {}

            # Update max airtime percentage
            if "max_airtime_percent" in data:
                percent = float(data["max_airtime_percent"])
                if percent < 0.1 or percent > 100.0:
                    return self._error("Max airtime percent must be 0.1-100.0")
                # Convert percent to milliseconds per minute
                max_airtime_ms = int((percent / 100) * 60000)
                self.config["duty_cycle"]["max_airtime_per_minute"] = max_airtime_ms
                applied.append(f"max_airtime={percent}%")

            # Update enforcement enabled/disabled
            if "enforcement_enabled" in data:
                enabled = bool(data["enforcement_enabled"])
                self.config["duty_cycle"]["enforcement_enabled"] = enabled
                applied.append(f"enforcement={'enabled' if enabled else 'disabled'}")

            if not applied:
                return self._error("No valid settings provided")

            # Save to config file and live update daemon
            result = self.config_manager.update_and_save(
                updates={}, live_update=True, live_update_sections=["duty_cycle"]
            )

            if not result.get("saved", False):
                return self._error(result.get("error", "Failed to save configuration to file"))

            logger.info(f"Duty cycle config updated: {', '.join(applied)}")

            return self._success(
                {
                    "applied": applied,
                    "persisted": True,
                    "live_update": result.get("live_updated", False),
                    "restart_required": False,
                    "message": "Duty cycle settings applied immediately.",
                }
            )

        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error updating duty cycle config: {e}")
            return self._error(str(e))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def update_advert_rate_limit_config(self):
        """Update advert rate limiting configuration using ConfigManager.
        
        POST /api/update_advert_rate_limit_config
        Body: {
            "rate_limit_enabled": true,
            "bucket_capacity": 2,
            "refill_tokens": 1,
            "refill_interval_seconds": 36000,
            "min_interval_seconds": 3600,
            "penalty_enabled": true,
            "violation_threshold": 2,
            "violation_decay_seconds": 43200,
            "base_penalty_seconds": 21600,
            "penalty_multiplier": 2.0,
            "max_penalty_seconds": 86400,
            "adaptive_enabled": true,
            "ewma_alpha": 0.1,
            "hysteresis_seconds": 300,
            "quiet_max": 0.05,
            "normal_max": 0.20,
            "busy_max": 0.50
        }
        """
        self._set_cors_headers()
        
        if cherrypy.request.method == "OPTIONS":
            return ""
        
        try:
            self._require_post()
            data = cherrypy.request.json or {}
            
            applied = []
            
            # Ensure config sections exist
            if "repeater" not in self.config:
                self.config["repeater"] = {}
            if "advert_rate_limit" not in self.config["repeater"]:
                self.config["repeater"]["advert_rate_limit"] = {}
            if "advert_penalty_box" not in self.config["repeater"]:
                self.config["repeater"]["advert_penalty_box"] = {}
            if "advert_adaptive" not in self.config["repeater"]:
                self.config["repeater"]["advert_adaptive"] = {"thresholds": {}}
            
            rate_cfg = self.config["repeater"]["advert_rate_limit"]
            penalty_cfg = self.config["repeater"]["advert_penalty_box"]
            adaptive_cfg = self.config["repeater"]["advert_adaptive"]
            
            # Rate limit settings
            if "rate_limit_enabled" in data:
                rate_cfg["enabled"] = bool(data["rate_limit_enabled"])
                applied.append(f"rate_limit={'enabled' if rate_cfg['enabled'] else 'disabled'}")
            
            if "bucket_capacity" in data:
                cap = max(1, int(data["bucket_capacity"]))
                rate_cfg["bucket_capacity"] = cap
                applied.append(f"bucket_capacity={cap}")
            
            if "refill_tokens" in data:
                tokens = max(1, int(data["refill_tokens"]))
                rate_cfg["refill_tokens"] = tokens
                applied.append(f"refill_tokens={tokens}")
            
            if "refill_interval_seconds" in data:
                interval = max(60, int(data["refill_interval_seconds"]))
                rate_cfg["refill_interval_seconds"] = interval
                applied.append(f"refill_interval={interval}s")
            
            if "min_interval_seconds" in data:
                min_int = max(0, int(data["min_interval_seconds"]))
                rate_cfg["min_interval_seconds"] = min_int
                applied.append(f"min_interval={min_int}s")
            
            # Penalty box settings
            if "penalty_enabled" in data:
                penalty_cfg["enabled"] = bool(data["penalty_enabled"])
                applied.append(f"penalty={'enabled' if penalty_cfg['enabled'] else 'disabled'}")
            
            if "violation_threshold" in data:
                thresh = max(1, int(data["violation_threshold"]))
                penalty_cfg["violation_threshold"] = thresh
                applied.append(f"violation_threshold={thresh}")
            
            if "violation_decay_seconds" in data:
                decay = max(60, int(data["violation_decay_seconds"]))
                penalty_cfg["violation_decay_seconds"] = decay
                applied.append(f"violation_decay={decay}s")
            
            if "base_penalty_seconds" in data:
                base = max(60, int(data["base_penalty_seconds"]))
                penalty_cfg["base_penalty_seconds"] = base
                applied.append(f"base_penalty={base}s")
            
            if "penalty_multiplier" in data:
                mult = max(1.0, float(data["penalty_multiplier"]))
                penalty_cfg["penalty_multiplier"] = mult
                applied.append(f"penalty_multiplier={mult}")
            
            if "max_penalty_seconds" in data:
                max_pen = max(60, int(data["max_penalty_seconds"]))
                penalty_cfg["max_penalty_seconds"] = max_pen
                applied.append(f"max_penalty={max_pen}s")
            
            # Adaptive settings
            if "adaptive_enabled" in data:
                adaptive_cfg["enabled"] = bool(data["adaptive_enabled"])
                applied.append(f"adaptive={'enabled' if adaptive_cfg['enabled'] else 'disabled'}")
            
            if "ewma_alpha" in data:
                alpha = max(0.01, min(1.0, float(data["ewma_alpha"])))
                adaptive_cfg["ewma_alpha"] = alpha
                applied.append(f"ewma_alpha={alpha}")
            
            if "hysteresis_seconds" in data:
                hyst = max(0, int(data["hysteresis_seconds"]))
                adaptive_cfg["hysteresis_seconds"] = hyst
                applied.append(f"hysteresis={hyst}s")
            
            # Adaptive thresholds
            if "thresholds" not in adaptive_cfg:
                adaptive_cfg["thresholds"] = {}
            
            if "quiet_max" in data:
                adaptive_cfg["thresholds"]["quiet_max"] = float(data["quiet_max"])
                applied.append(f"quiet_max={data['quiet_max']}")
            
            if "normal_max" in data:
                adaptive_cfg["thresholds"]["normal_max"] = float(data["normal_max"])
                applied.append(f"normal_max={data['normal_max']}")
            
            if "busy_max" in data:
                adaptive_cfg["thresholds"]["busy_max"] = float(data["busy_max"])
                applied.append(f"busy_max={data['busy_max']}")
            
            if not applied:
                return self._error("No valid settings provided")
            
            # Save to config file and live update daemon
            result = self.config_manager.update_and_save(
                updates={},
                live_update=True,
                live_update_sections=['repeater']
            )
            
            logger.info(f"Advert rate limit config updated: {', '.join(applied)}")
            
            return self._success({
                "applied": applied,
                "persisted": result.get("saved", False),
                "live_update": result.get("live_updated", False),
                "restart_required": False,
                "message": "Advert rate limit settings applied immediately."
            })
            
        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error updating advert rate limit config: {e}")
            return self._error(str(e))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def check_pymc_console(self):
        """Check if PyMC Console directory exists."""
        self._set_cors_headers()

        if cherrypy.request.method == "OPTIONS":
            return ""

        try:
            pymc_console_path = "/opt/pymc_console/web/html"
            exists = os.path.isdir(pymc_console_path)

            return self._success({"exists": exists, "path": pymc_console_path})
        except Exception as e:
            logger.error(f"Error checking PyMC Console directory: {e}")
            return self._error(str(e))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def update_web_config(self):
        """Update web configuration (CORS, frontend path) using ConfigManager."""
        self._set_cors_headers()

        if cherrypy.request.method == "OPTIONS":
            return ""

        try:
            self._require_post()
            updates = cherrypy.request.json or {}

            if not updates:
                return self._error("No configuration updates provided")

            # Use ConfigManager to update and save configuration
            # Web changes (CORS, web_path) don't require live update
            result = self.config_manager.update_and_save(updates=updates, live_update=False)

            if result.get("success"):
                logger.info(f"Web configuration updated: {list(updates.keys())}")
                return self._success(
                    {
                        "persisted": result.get("saved", False),
                        "message": "Web configuration saved successfully. Restart required for changes to take effect.",
                    }
                )
            else:
                return self._error(result.get("error", "Failed to update web configuration"))

        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error updating web config: {e}")
            return self._error(str(e))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def mqtt_status(self):
        """Get MQTT connection status and configuration."""
        self._set_cors_headers()
        try:
            # mqtt_cfg = self.config.get("mqtt_brokers", {})

            # Walk the chain to the mqtt_handler
            handler = None
            try:
                storage = self._get_storage()
                handler = getattr(storage, "mqtt_handler", None)
            except Exception:
                pass

            connected_brokers = []
            if handler:
                for conn in getattr(handler, "connections", []):
                    connected_brokers.append({
                        "enabled": conn.enabled,
                        "name": conn.broker.get("name", ""),
                        "host": conn.broker.get("host", ""),
                        "status": {
                            "connected": conn.is_connected(),
                            "reconnecting": conn.has_pending_reconnect(),
                        },
                        "format": conn.format
                    })

            return self._success({
                "handler_active": handler is not None,
                "brokers": connected_brokers,
            })
        except Exception as e:
            logger.error(f"Error getting MQTT status: {e}")
            return self._error(str(e))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def update_mqtt_config(self):
        """Update MQTT Observer configuration.

        POST /api/update_mqtt_config
        Body: {
            "iata_code": "SFO",
            "status_interval": 300,
            "owner": "Callsign",
            "email": "user@example.com",
            "brokers": [
            {
            
            }]
        }
        """
        self._set_cors_headers()

        if cherrypy.request.method == "OPTIONS":
            return ""

        try:
            self._require_post()
            data = cherrypy.request.json or {}

            if not data:
                return self._error("No configuration updates provided")

            mqtt_updates = {}

            if "iata_code" in data:
                mqtt_updates["iata_code"] = str(data["iata_code"]).strip()
            if "status_interval" in data:
                mqtt_updates["status_interval"] = max(60, int(data["status_interval"]))
            if "owner" in data:
                mqtt_updates["owner"] = str(data["owner"]).strip()
            if "email" in data:
                mqtt_updates["email"] = str(data["email"]).strip()
            # if "disallowed_packet_types" in data:
            #     mqtt_updates["disallowed_packet_types"] = list(data["disallowed_packet_types"])
            if "brokers" in data:
                brokers = data["brokers"]
                if not isinstance(brokers, list):
                    return self._error("brokers must be a list")
                validated = []
                for i, b in enumerate(brokers):
                    if not isinstance(b, dict):
                        return self._error(f"Broker at index {i} must be an object")

                    # Bundled preset reference: {preset: <name>}. Pass through
                    # unchanged - the MQTT handler expands it on next start.
                    if "preset" in b and "name" not in b:
                        validated.append({"preset": str(b["preset"]).strip()})
                        continue

                    for field in ("name", "host", "port", "format"):
                        if not b.get(field, ""):
                            return self._error(f"Broker at index {i} missing required field: {field}")
                    
                    try:
                        port = int(b.get("port", 443))
                    except (ValueError, TypeError):
                        return self._error(f"Broker at index {i} has invalid port")
                    
                    new_broker = {
                        "name":      str(b["name"]).strip(),
                        "enabled":   b.get("enabled", False),
                        "transport": str(b.get("transport", "websockets")).strip(),
                        "host":      str(b["host"]).strip(),
                        "port":      port,
                        "format":    str(b["format"]).strip(),
                        "disallowed_packet_types": list(b.get("disallowed_packet_types", [])),
                        "retain_status": bool(b.get("retain_status", False)),
                        "tls": {
                            "enabled": bool(b.get("tls", {}).get("enabled", True if port == 443 else False)),
                            "insecure": bool(b.get("tls", {}).get("insecure", False)),
                        }
                    }
                    
                    if b.get("use_jwt_auth", False):
                        new_broker["use_jwt_auth"] = True
                        new_broker["audience"] = str(b["audience"]).strip()
                    else:
                        new_broker["use_jwt_auth"] = False
                        new_broker["username"] = b.get("username", None)
                        new_broker["password"] = b.get("password", None)

                    validated.append(new_broker)

                mqtt_updates["brokers"] = validated

            if not mqtt_updates:
                return self._error("No valid settings provided")

            result = self.config_manager.update_and_save(
                updates={"mqtt_brokers": mqtt_updates, "mqtt": None, "letsmesh": None},
                live_update=False,  # Restart required for MQTT handler changes
            )

            if result.get("success"):
                logger.info(f"MQTT config updated: {list(mqtt_updates.keys())}")
                return self._success({
                    "persisted": result.get("saved", False),
                    "restart_required": True,
                    "message": "Observer settings saved. Restart the service for changes to take effect.",
                })
            else:
                return self._error(result.get("error", "Failed to update LetsMesh configuration"))

        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error updating LetsMesh config: {e}")
            return self._error(str(e))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def restart_service(self):
        """Restart the pymc-repeater service via systemctl."""
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()

        if cherrypy.request.method == "OPTIONS":
            return ""

        try:
            self._require_post()
            from repeater.service_utils import restart_service as do_restart

            logger.warning("Service restart requested via API")
            success, message = do_restart()

            if success:
                return {"success": True, "message": message}
            else:
                return self._error(message)

        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error in restart_service endpoint: {e}", exc_info=True)
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
    @cherrypy.tools.json_in()
    def memory_debug(self, **kwargs):
        """Memory diagnostics endpoint.

        GET  — returns current status + data if tracing is active.
        POST {"action": "start"} — starts tracemalloc and captures baseline.
        POST {"action": "stop"}  — stops tracemalloc and clears data.
        """
        import tracemalloc

        self._set_cors_headers()
        if cherrypy.request.method == "OPTIONS":
            return ""

        # ---------- POST: start / stop ----------
        if cherrypy.request.method == "POST":
            data = cherrypy.request.json or {}
            action = data.get("action")

            if action == "start":
                if not tracemalloc.is_tracing():
                    # Use 1 frame instead of 10 — much less overhead & faster snapshots
                    tracemalloc.start(1)
                self._tracemalloc_baseline = tracemalloc.take_snapshot().filter_traces((
                    tracemalloc.Filter(False, tracemalloc.__file__),
                    tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
                ))
                logger.info("Memory tracing started")
                return self._success({
                    "tracing": True,
                    "message": "Tracing started — check again after some time to see growth",
                })

            if action == "stop":
                if tracemalloc.is_tracing():
                    tracemalloc.stop()
                self._tracemalloc_baseline = None
                logger.info("Memory tracing stopped")
                return self._success({"tracing": False})

            return self._error("Invalid action — use 'start' or 'stop'")

        # ---------- GET: status + data ----------
        tracing = tracemalloc.is_tracing()
        result: dict = {"tracing": tracing}

        # Always include RSS regardless of tracing state
        try:
            import resource
            rusage = resource.getrusage(resource.RUSAGE_SELF)
            result["rss_mb"] = round(rusage.ru_maxrss / 1024, 1)
        except Exception:
            pass

        if not tracing:
            return self._success(result)

        # Filter out tracemalloc's own allocations to keep snapshot small & fast
        current = tracemalloc.take_snapshot().filter_traces((
            tracemalloc.Filter(False, tracemalloc.__file__),
            tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
        ))
        baseline = getattr(self, "_tracemalloc_baseline", None)

        # Top 20 allocations right now
        top_current = current.statistics("lineno")[:20]
        current_stats = []
        for stat in top_current:
            current_stats.append({
                "file": str(stat.traceback),
                "size_kb": round(stat.size / 1024, 1),
                "count": stat.count,
            })
        result["current_top_20"] = current_stats

        # Growth since baseline
        if baseline:
            diff = current.compare_to(baseline, "lineno")
            growth = [d for d in diff if d.size_diff > 0]
            growth.sort(key=lambda d: d.size_diff, reverse=True)
            growth_stats = []
            for stat in growth[:20]:
                growth_stats.append({
                    "file": str(stat.traceback),
                    "size_diff_kb": round(stat.size_diff / 1024, 1),
                    "count_diff": stat.count_diff,
                    "current_size_kb": round(stat.size / 1024, 1),
                })
            result["growth_since_baseline"] = growth_stats

        traced_current, traced_peak = tracemalloc.get_traced_memory()
        result["traced_current_mb"] = round(traced_current / (1024 * 1024), 2)
        result["traced_peak_mb"] = round(traced_peak / (1024 * 1024), 2)

        return self._success(result)

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
                    return self._error(
                        "Process information not available (psutil may not be installed)"
                    )
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
    @cherrypy.tools.gzip(compress_level=6)
    @cherrypy.tools.json_out()
    def bulk_packets(self, limit=1000, offset=0, start_timestamp=None, end_timestamp=None):
        """
        Optimized bulk packet retrieval with gzip compression and DB-level pagination.
        """
        try:
            # Enforce reasonable limits
            limit = min(int(limit), 10000)
            offset = max(int(offset), 0)

            # Get packets from storage with TRUE DB-level pagination
            # Uses SQL "LIMIT ? OFFSET ?" - no Python slicing needed!
            storage = self._get_storage()
            packets = storage.get_filtered_packets(
                packet_type=None,
                route=None,
                start_timestamp=float(start_timestamp) if start_timestamp else None,
                end_timestamp=float(end_timestamp) if end_timestamp else None,
                limit=limit,
                offset=offset,
            )

            response = {
                "success": True,
                "data": packets,
                "count": len(packets),
                "offset": offset,
                "limit": limit,
                "compressed": True,
            }

            return response

        except Exception as e:
            logger.error(f"Error getting bulk packets: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def filtered_packets(
        self, start_timestamp=None, end_timestamp=None, limit=1000, type=None, route=None
    ):
        # Handle OPTIONS request for CORS preflight
        if cherrypy.request.method == "OPTIONS":
            self._set_cors_headers()
            return ""

        try:
            # Convert 'type' parameter to 'packet_type' for storage method
            packet_type = int(type) if type is not None else None
            route_int = int(route) if route is not None else None
            start_ts = float(start_timestamp) if start_timestamp is not None else None
            end_ts = float(end_timestamp) if end_timestamp is not None else None
            limit_int = int(limit) if limit is not None else 1000

            packets = self._get_storage().get_filtered_packets(
                packet_type=packet_type,
                route=route_int,
                start_timestamp=start_ts,
                end_timestamp=end_ts,
                limit=limit_int,
            )
            return self._success(
                packets,
                count=len(packets),
                filters={
                    "type": packet_type,
                    "route": route_int,
                    "start_timestamp": start_ts,
                    "end_timestamp": end_ts,
                    "limit": limit_int,
                },
            )
        except ValueError as e:
            return self._error(f"Invalid parameter format: {e}")
        except Exception as e:
            logger.error(f"Error getting filtered packets: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def airtime_data(self, start_timestamp=None, end_timestamp=None, limit=50000):
        """Lightweight endpoint returning only columns needed for airtime charting."""
        try:
            start_ts = float(start_timestamp) if start_timestamp is not None else None
            end_ts = float(end_timestamp) if end_timestamp is not None else None
            limit_int = min(int(limit), 50000)
            packets = self._get_storage().get_airtime_data(
                start_timestamp=start_ts, end_timestamp=end_ts, limit=limit_int,
            )
            return self._success(packets, count=len(packets))
        except Exception as e:
            logger.error(f"Error getting airtime data: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def airtime_chart_data(
        self,
        start_timestamp=None,
        end_timestamp=None,
        bucket_seconds=60,
        sf=9,
        bw_hz=62500,
        cr=5,
        preamble=17,
    ):
        """Server-side aggregated airtime utilization for chart rendering.

        Returns pre-bucketed rx_ms/tx_ms per time bucket instead of raw packet rows,
        reducing response size from potentially hundreds of KB to a few KB.
        """
        try:
            now = __import__("time").time()
            start_ts = float(start_timestamp) if start_timestamp is not None else now - 86400
            end_ts = float(end_timestamp) if end_timestamp is not None else now
            bucket_s = max(10, min(int(bucket_seconds), 3600))
            result = self._get_storage().get_airtime_buckets(
                start_timestamp=start_ts,
                end_timestamp=end_ts,
                bucket_seconds=bucket_s,
                sf=int(sf),
                bw_hz=int(bw_hz),
                cr=int(cr),
                preamble=int(preamble),
            )
            return self._success(result)
        except Exception as e:
            logger.error(f"Error getting airtime chart data: {e}")
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
            params = self._get_params(
                {"start_time": None, "end_time": None, "resolution": "average"}
            )
            data = self._get_storage().get_rrd_data(**params)
            return self._success(data) if data else self._error("No RRD data available")
        except ValueError as e:
            return self._error(f"Invalid parameter format: {e}")
        except Exception as e:
            logger.error(f"Error getting RRD data: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def packet_type_graph_data(self, hours=24, resolution="average", types="all"):

        try:
            hours = int(hours)
            start_time, end_time = self._get_time_range(hours)

            storage = self._get_storage()

            stats = storage.sqlite_handler.get_packet_type_stats(hours)
            if "error" in stats:
                return self._error(stats["error"])

            packet_type_totals = stats.get("packet_type_totals", {})

            # Create simple bar chart data format for packet types
            series = []
            for type_name, count in packet_type_totals.items():
                if count > 0:  # Only include types with actual data
                    series.append(
                        {
                            "name": type_name,
                            "type": type_name.lower()
                            .replace(" ", "_")
                            .replace("(", "")
                            .replace(")", ""),
                            "data": [
                                [end_time * 1000, count]
                            ],  # Single data point with total count
                        }
                    )

            # Sort series by count (descending)
            series.sort(key=lambda x: x["data"][0][1], reverse=True)

            graph_data = {
                "start_time": start_time,
                "end_time": end_time,
                "step": 3600,  # 1 hour step for simple bar chart
                "timestamps": [start_time, end_time],
                "series": series,
                "data_source": "sqlite",
                "chart_type": "bar",  # Indicate this is bar chart data
            }

            return self._success(graph_data)

        except ValueError as e:
            return self._error(f"Invalid parameter format: {e}")
        except Exception as e:
            logger.error(f"Error getting packet type graph data: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def metrics_graph_data(self, hours=24, resolution="average", metrics="all"):

        try:
            hours = int(hours)
            start_time, end_time = self._get_time_range(hours)

            rrd_data = self._get_storage().get_rrd_data(
                start_time=start_time, end_time=end_time, resolution=resolution
            )

            if not rrd_data or "metrics" not in rrd_data:
                return self._error("No RRD data available")

            metric_names = {
                "rx_count": "Received Packets",
                "tx_count": "Transmitted Packets",
                "drop_count": "Dropped Packets",
                "avg_rssi": "Average RSSI (dBm)",
                "avg_snr": "Average SNR (dB)",
                "avg_length": "Average Packet Length",
                "avg_score": "Average Score",
                "neighbor_count": "Neighbor Count",
            }

            counter_metrics = ["rx_count", "tx_count", "drop_count"]

            if metrics != "all":
                requested_metrics = [m.strip() for m in metrics.split(",")]
            else:
                requested_metrics = list(rrd_data["metrics"].keys())

            timestamps_ms = [ts * 1000 for ts in rrd_data["timestamps"]]
            series = []

            for metric_key in requested_metrics:
                if metric_key in rrd_data["metrics"]:
                    if metric_key in counter_metrics:
                        chart_data = self._process_counter_data(
                            rrd_data["metrics"][metric_key], timestamps_ms
                        )
                    else:
                        chart_data = self._process_gauge_data(
                            rrd_data["metrics"][metric_key], timestamps_ms
                        )

                    series.append(
                        {
                            "name": metric_names.get(metric_key, metric_key),
                            "type": metric_key,
                            "data": chart_data,
                        }
                    )

            graph_data = {
                "start_time": rrd_data["start_time"],
                "end_time": rrd_data["end_time"],
                "step": rrd_data["step"],
                "timestamps": rrd_data["timestamps"],
                "series": series,
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

            if (
                self.daemon_instance
                and hasattr(self.daemon_instance, "radio")
                and self.daemon_instance.radio
            ):
                if hasattr(self.daemon_instance.radio, "set_custom_cad_thresholds"):
                    self.daemon_instance.radio.set_custom_cad_thresholds(peak=peak, min_val=min_val)
                    logger.info(f"Applied CAD settings to radio: peak={peak}, min={min_val}")

            if "radio" not in self.config:
                self.config["radio"] = {}
            if "cad" not in self.config["radio"]:
                self.config["radio"]["cad"] = {}

            self.config["radio"]["cad"]["peak_threshold"] = peak
            self.config["radio"]["cad"]["min_threshold"] = min_val

            config_path = getattr(self, "_config_path", "/etc/pymc_repeater/config.yaml")
            saved = self.config_manager.save_to_file()
            if not saved:
                return self._error("Failed to save configuration to file")

            logger.info(
                f"Saved CAD settings to config: peak={peak}, min={min_val}, rate={detection_rate:.1f}%"
            )
            return {
                "success": True,
                "message": f"CAD settings saved: peak={peak}, min={min_val}",
                "settings": {"peak": peak, "min_val": min_val, "detection_rate": detection_rate},
            }
        except cherrypy.HTTPError:
            # Re-raise HTTP errors (like 405 Method Not Allowed) without logging
            raise
        except Exception as e:
            logger.error(f"Error saving CAD settings: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def update_radio_config(self):
        """Update radio and repeater configuration with live updates.

        POST /api/update_radio_config
        Body: {
            "tx_power": 22,              # TX power in dBm (2-30)
            "frequency": 869618000,      # Frequency in Hz (100-1000 MHz)
            "bandwidth": 62500,          # Bandwidth in Hz (valid: 7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125, 250, 500 kHz)
            "spreading_factor": 8,       # Spreading factor (5-12)
            "coding_rate": 8,            # Coding rate (5-8 for 4/5 to 4/8)
            "tx_delay_factor": 1.0,      # TX delay factor (0.0-5.0)
            "direct_tx_delay_factor": 0.5,  # Direct TX delay (0.0-5.0)
            "rx_delay_base": 0.0,        # RX delay base (>= 0)
            "node_name": "MyNode",       # Node name
            "latitude": 0.0,             # Latitude (-90 to 90)
            "longitude": 0.0,            # Longitude (-180 to 180)
            "max_flood_hops": 64,         # Max flood hops (0-64)
            "flood_advert_interval_hours": 10,  # Flood advert interval (0 or 3-48)
            "advert_interval_minutes": 120      # Local advert interval (0 or 1-10080)
        }

        Note: Radio hardware changes (frequency, bandwidth, SF, CR) require restart to apply.

        Returns: {"success": true, "data": {"applied": [...], "live_update": true}}
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()

        if cherrypy.request.method == "OPTIONS":
            return ""

        try:
            self._require_post()
            data = cherrypy.request.json or {}

            applied = []

            # Ensure config sections exist
            if "radio" not in self.config:
                self.config["radio"] = {}
            if "delays" not in self.config:
                self.config["delays"] = {}
            if "repeater" not in self.config:
                self.config["repeater"] = {}
            if "mesh" not in self.config:
                self.config["mesh"] = {}

            # Update TX power (up to 30 dBm for high-power radios)
            if "tx_power" in data:
                power = int(data["tx_power"])
                if power < 2 or power > 30:
                    return self._error("TX power must be 2-30 dBm")
                self.config["radio"]["tx_power"] = power
                applied.append(f"power={power}dBm")

            # Update frequency (in Hz)
            if "frequency" in data:
                freq = float(data["frequency"])
                if freq < 100_000_000 or freq > 1_000_000_000:
                    return self._error("Frequency must be 100-1000 MHz")
                self.config["radio"]["frequency"] = freq
                applied.append(f"freq={freq/1_000_000:.3f}MHz")

            # Update bandwidth (in Hz)
            if "bandwidth" in data:
                bw = int(float(data["bandwidth"]))
                valid_bw = [7800, 10400, 15600, 20800, 31250, 41700, 62500, 125000, 250000, 500000]
                if bw not in valid_bw:
                    return self._error(f"Bandwidth must be one of {[b/1000 for b in valid_bw]} kHz")
                self.config["radio"]["bandwidth"] = bw
                applied.append(f"bw={bw/1000}kHz")

            # Update spreading factor
            if "spreading_factor" in data:
                sf = int(data["spreading_factor"])
                if sf < 5 or sf > 12:
                    return self._error("Spreading factor must be 5-12")
                self.config["radio"]["spreading_factor"] = sf
                applied.append(f"sf={sf}")

            # Update coding rate
            if "coding_rate" in data:
                cr = int(data["coding_rate"])
                if cr < 5 or cr > 8:
                    return self._error("Coding rate must be 5-8 (for 4/5 to 4/8)")
                self.config["radio"]["coding_rate"] = cr
                applied.append(f"cr=4/{cr}")

            # Update TX delay factor
            if "tx_delay_factor" in data:
                tdf = float(data["tx_delay_factor"])
                if tdf < 0.0 or tdf > 5.0:
                    return self._error("TX delay factor must be 0.0-5.0")
                self.config["delays"]["tx_delay_factor"] = tdf
                applied.append(f"txdelay={tdf}")

            # Update direct TX delay factor
            if "direct_tx_delay_factor" in data:
                dtdf = float(data["direct_tx_delay_factor"])
                if dtdf < 0.0 or dtdf > 5.0:
                    return self._error("Direct TX delay factor must be 0.0-5.0")
                self.config["delays"]["direct_tx_delay_factor"] = dtdf
                applied.append(f"direct.txdelay={dtdf}")

            # Update RX delay base
            if "rx_delay_base" in data:
                rxd = float(data["rx_delay_base"])
                if rxd < 0.0:
                    return self._error("RX delay cannot be negative")
                self.config["delays"]["rx_delay_base"] = rxd
                applied.append(f"rxdelay={rxd}")

            # Update node name
            if "node_name" in data:
                name = str(data["node_name"]).strip()
                if not name:
                    return self._error("Node name cannot be empty")
                # Validate UTF-8 byte length (31 bytes max + 1 null terminator = 32 bytes total)
                if len(name.encode("utf-8")) > 31:
                    return self._error("Node name too long (max 31 bytes in UTF-8)")
                self.config["repeater"]["node_name"] = name
                applied.append(f"name={name}")

            # Update latitude
            if "latitude" in data:
                lat = float(data["latitude"])
                if lat < -90 or lat > 90:
                    return self._error("Latitude must be -90 to 90")
                self.config["repeater"]["latitude"] = lat
                applied.append(f"lat={lat}")

            # Update longitude
            if "longitude" in data:
                lon = float(data["longitude"])
                if lon < -180 or lon > 180:
                    return self._error("Longitude must be -180 to 180")
                self.config["repeater"]["longitude"] = lon
                applied.append(f"lon={lon}")

            # Update max flood hops
            if "max_flood_hops" in data:
                hops = int(data["max_flood_hops"])
                if hops < 0 or hops > 64:
                    return self._error("Max flood hops must be 0-64")
                self.config["repeater"]["max_flood_hops"] = hops
                applied.append(f"flood.max={hops}")

            # Update flood advert interval (hours)
            if "flood_advert_interval_hours" in data:
                hours = int(data["flood_advert_interval_hours"])
                if hours != 0 and (hours < 3 or hours > 48):
                    return self._error("Flood advert interval must be 0 (off) or 3-48 hours")
                self.config["repeater"]["send_advert_interval_hours"] = hours
                applied.append(f"flood.advert.interval={hours}h")

            # Update local advert interval (minutes)
            if "advert_interval_minutes" in data:
                mins = int(data["advert_interval_minutes"])
                if mins != 0 and (mins < 1 or mins > 10080):
                    return self._error("Advert interval must be 0 (off) or 1-10080 minutes")
                self.config["repeater"]["advert_interval_minutes"] = mins
                applied.append(f"advert.interval={mins}m")

            # Update path hash mode (mesh: 0=1-byte, 1=2-byte, 2=3-byte)
            if "path_hash_mode" in data:
                phm = int(data["path_hash_mode"])
                if phm not in (0, 1, 2):
                    return self._error("Path hash mode must be 0 (1-byte), 1 (2-byte), or 2 (3-byte)")
                self.config["mesh"]["path_hash_mode"] = phm
                applied.append(f"path_hash_mode={phm}")

            # KISS modem settings (only when radio_type is kiss)
            if "kiss_port" in data or "kiss_baud_rate" in data:
                if self.config.get("radio_type") != "kiss":
                    return self._error("KISS settings only apply when radio_type is kiss")
                if "kiss" not in self.config:
                    self.config["kiss"] = {}
                if "kiss_port" in data:
                    self.config["kiss"]["port"] = str(data["kiss_port"]).strip()
                    applied.append("kiss.port")
                if "kiss_baud_rate" in data:
                    self.config["kiss"]["baud_rate"] = int(data["kiss_baud_rate"])
                    applied.append("kiss.baud_rate")

            # Update flood loop detection mode
            if "loop_detect" in data:
                mode = str(data["loop_detect"]).strip().lower()
                if mode not in ("off", "minimal", "moderate", "strict"):
                    return self._error("loop_detect must be one of: off, minimal, moderate, strict")
                if "mesh" not in self.config:
                    self.config["mesh"] = {}
                self.config["mesh"]["loop_detect"] = mode
                applied.append(f"loop_detect={mode}")

            if not applied:
                return self._error("No valid settings provided")

            live_sections = ["repeater", "delays", "radio"]
            if "mesh" in self.config and any(k in data for k in ("path_hash_mode", "loop_detect")):
                live_sections.append("mesh")
            if "kiss" in self.config:
                live_sections.append("kiss")
            # Save to config file and live update daemon in one operation
            result = self.config_manager.update_and_save(
                updates={},  # Updates already applied to self.config above
                live_update=True,
                live_update_sections=live_sections,
            )

            if not result.get("saved", False):
                return self._error(result.get("error", "Failed to save configuration to file"))

            logger.info(f"Radio config updated: {', '.join(applied)}")

            return self._success(
                {
                    "applied": applied,
                    "persisted": True,
                    "live_update": result.get("live_updated", False),
                    "restart_required": not result.get("live_updated", False),
                    "message": (
                        "Settings applied immediately."
                        if result.get("live_updated")
                        else "Settings saved. Restart service to apply changes."
                    ),
                }
            )

        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error updating radio config: {e}")
            return self._error(str(e))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def noise_floor_history(self, hours: int = 24, limit: int = None):

        try:
            storage = self._get_storage()
            hours = int(hours)
            limit = int(limit) if limit else None
            history = storage.get_noise_floor_history(hours=hours, limit=limit)

            return self._success({"history": history, "hours": hours, "count": len(history)})
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

            return self._success({"stats": stats, "hours": hours})
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

            return self._success({"chart_data": chart_data, "hours": hours})
        except Exception as e:
            logger.error(f"Error fetching noise floor chart data: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def crc_error_count(self, hours: int = 24):
        """Return total CRC errors within the given time window."""
        try:
            storage = self._get_storage()
            hours = int(hours)
            count = storage.get_crc_error_count(hours=hours)
            return self._success({
                "crc_error_count": count,
                "hours": hours
            })
        except Exception as e:
            logger.error(f"Error fetching CRC error count: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def crc_error_history(self, hours: int = 24, limit: int = None):
        """Return CRC error records within the given time window."""
        try:
            storage = self._get_storage()
            hours = int(hours)
            limit = int(limit) if limit else None
            history = storage.get_crc_error_history(hours=hours, limit=limit)
            return self._success({
                "history": history,
                "hours": hours,
                "count": len(history)
            })
        except Exception as e:
            logger.error(f"Error fetching CRC error history: {e}")
            return self._error(e)

    @cherrypy.expose
    def cad_calibration_stream(self):
        cherrypy.response.headers["Content-Type"] = "text/event-stream"
        cherrypy.response.headers["Cache-Control"] = "no-cache"
        cherrypy.response.headers["Connection"] = "keep-alive"

        if not hasattr(self.cad_calibration, "message_queue"):
            self.cad_calibration.message_queue = []

        def generate():
            try:
                yield f"data: {json.dumps({'type': 'connected', 'message': 'Connected to CAD calibration stream'})}\n\n"

                if self.cad_calibration.running:
                    config = getattr(self.cad_calibration.daemon_instance, "config", {})
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
                            "total_tests": total_tests,
                        },
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

    cad_calibration_stream._cp_config = {"response.stream": True}

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
                contact_type=contact_type, limit=limit_int, hours=hours_int
            )

            return self._success(
                adverts,
                count=len(adverts),
                contact_type=contact_type,
                filters={"contact_type": contact_type, "limit": limit_int, "hours": hours_int},
            )

        except ValueError as e:
            return self._error(f"Invalid parameter format: {e}")
        except Exception as e:
            logger.error(f"Error getting adverts by contact type: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def advert_rate_limit_stats(self):
        """Get advert rate limiting statistics and adaptive tier info."""
        try:
            if not self.daemon_instance or not hasattr(self.daemon_instance, 'advert_helper'):
                return self._error("Advert helper not available")
            
            advert_helper = self.daemon_instance.advert_helper
            if not advert_helper:
                return self._error("Advert helper not initialized")
            
            if not hasattr(advert_helper, 'get_rate_limit_stats'):
                return self._error("Rate limit stats not supported by this advert helper version")
            
            stats = advert_helper.get_rate_limit_stats()
            return self._success(stats)
            
        except Exception as e:
            logger.error(f"Error getting advert rate limit stats: {e}")
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

                        dt = datetime.fromisoformat(last_used.replace("Z", "+00:00"))
                        last_used = dt.timestamp()
                    except (ValueError, AttributeError):
                        # If conversion fails, use current time
                        last_used = time.time()
                else:
                    last_used = time.time()

                storage = self._get_storage()
                key_id = storage.create_transport_key(
                    name, flood_policy, transport_key, parent_id, last_used
                )

                if key_id:
                    return self._success(
                        {"id": key_id}, message="Transport key created successfully"
                    )
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
                        dt = datetime.fromisoformat(last_used.replace("Z", "+00:00"))
                        last_used = dt.timestamp()
                    except (ValueError, AttributeError):
                        # If conversion fails, leave as None to not update
                        last_used = None

                storage = self._get_storage()
                success = storage.update_transport_key(
                    key_id, name, flood_policy, transport_key, parent_id, last_used
                )

                if success:
                    return self._success(
                        {"id": key_id}, message="Transport key updated successfully"
                    )
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
                    return self._success(
                        {"id": key_id}, message="Transport key deleted successfully"
                    )
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
    def unscoped_flood_policy(self):
        """
        Update unscoped flood policy configuration

        POST /unscoped_flood_policy
        Body: {"unscoped_flood_allow": true/false}
        """
        if cherrypy.request.method == "POST":
            try:
                data = cherrypy.request.json or {}
                unscoped_flood_allow = data.get("unscoped_flood_allow")

                if unscoped_flood_allow is None:
                    return self._error("Missing required field: unscoped_flood_allow")

                if not isinstance(unscoped_flood_allow, bool):
                    return self._error("unscoped_flood_allow must be a boolean value")

                # Update the running configuration first (like CAD settings)
                if "mesh" not in self.config:
                    self.config["mesh"] = {}
                self.config["mesh"]["unscoped_flood_allow"] = unscoped_flood_allow

                # Get the actual config path from daemon instance (same as CAD settings)
                config_path = getattr(self, "_config_path", "/etc/pymc_repeater/config.yaml")
                if self.daemon_instance and hasattr(self.daemon_instance, "config_path"):
                    config_path = self.daemon_instance.config_path

                logger.info(f"Using config path for unscoped flood policy: {config_path}")

                # Update the configuration file using ConfigManager
                try:
                    saved = self.config_manager.save_to_file()
                    if saved:
                        logger.info(
                            f"Updated running config and saved unscoped flood policy to file: {'allow' if unscoped_flood_allow else 'deny'}"
                        )
                    else:
                        logger.error("Failed to save unscoped flood policy to file")
                        return self._error("Failed to save configuration to file")
                except Exception as e:
                    logger.error(f"Failed to save unscoped flood policy to file: {e}")
                    return self._error(f"Failed to save configuration to file: {e}")

                return self._success(
                    {"unscoped_flood_allow": unscoped_flood_allow},
                    message=f"Unscoped flood policy updated to {'allow' if unscoped_flood_allow else 'deny'} (live and saved)",
                )

            except Exception as e:
                logger.error(f"Error updating unscoped flood policy: {e}")
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

        # Handle OPTIONS request for CORS preflight
        if cherrypy.request.method == "OPTIONS":
            return ""

        try:
            self._require_post()
            data = cherrypy.request.json or {}
            target_id = data.get("target_id")
            timeout = int(data.get("timeout", 10))

            if not target_id:
                return self._error("Missing target_id parameter")

            # Derive byte width from path_hash_mode (issue #133):
            # 0 = 1-byte (legacy), 1 = 2-byte, 2 = 3-byte
            path_hash_mode = self.config.get("mesh", {}).get("path_hash_mode", 0)
            byte_count = {0: 1, 1: 2, 2: 3}.get(path_hash_mode, 1)
            hex_chars = byte_count * 2
            max_hash = (1 << (byte_count * 8)) - 1

            # Parse target hash (accepts hex string like "0xA5", "0xA5F0", or bare hex)
            try:
                target_hash = int(target_id, 16) if isinstance(target_id, str) else int(target_id)
                if target_hash < 0 or target_hash > max_hash:
                    return self._error(
                        f"target_id must be a valid {byte_count}-byte hash "
                        f"(0x00-0x{max_hash:0{hex_chars}X})"
                    )
            except ValueError:
                return self._error(f"Invalid target_id format: {target_id}")

            # Check if router and trace_helper are available
            if not hasattr(self.daemon_instance, "router"):
                return self._error("Packet router not available")

            router = self.daemon_instance.router
            if not hasattr(self.daemon_instance, "trace_helper"):
                return self._error("Trace helper not available")

            trace_helper = self.daemon_instance.trace_helper

            # Generate unique tag for this ping
            import random

            trace_tag = random.randint(0, 0xFFFFFFFF)

            # Create trace packet
            from pymc_core.protocol import PacketBuilder

            path_bytes = list(target_hash.to_bytes(byte_count, "big"))
            packet = PacketBuilder.create_trace(
                tag=trace_tag, auth_code=0x12345678, flags=0x00, path=path_bytes
            )

            # Wait for response with timeout
            import asyncio

            async def send_and_wait():
                """Async helper to send ping and wait for response"""
                # Register ping with TraceHelper (must be done in async context)
                event = trace_helper.register_ping(trace_tag, target_hash)

                # Send packet via router
                await router.inject_packet(packet)
                logger.info(f"Ping sent to 0x{target_hash:0{hex_chars}x} with tag {trace_tag} (path_hash_mode={path_hash_mode})")

                try:
                    await asyncio.wait_for(event.wait(), timeout=timeout)
                    return True
                except asyncio.TimeoutError:
                    return False

            # Run the async send and wait in the daemon's event loop
            try:
                if self.event_loop is None:
                    return self._error("Event loop not available")

                future = asyncio.run_coroutine_threadsafe(send_and_wait(), self.event_loop)
                response_received = future.result(timeout=timeout + 1)
            except Exception as e:
                logger.error(f"Error waiting for ping response: {e}")
                trace_helper.pending_pings.pop(trace_tag, None)
                return self._error(f"Error waiting for response: {str(e)}")

            if response_received:
                # Get result
                ping_info = trace_helper.pending_pings.pop(trace_tag, None)
                if not ping_info:
                    return self._error("Ping info not found after response")

                result = ping_info.get("result")
                if result:
                    # Calculate round-trip time
                    rtt_ms = (result["received_at"] - ping_info["sent_at"]) * 1000

                    # Prefer structured hops from TraceHelper; else legacy flat list.
                    if result.get("trace_hops"):
                        grouped_path = [
                            int.from_bytes(bytes(h), "big") for h in result["trace_hops"]
                        ]
                    else:
                        raw_path = result["path"]
                        if byte_count > 1:
                            grouped_path = [
                                int.from_bytes(bytes(raw_path[i : i + byte_count]), "big")
                                for i in range(0, len(raw_path), byte_count)
                            ]
                        else:
                            grouped_path = raw_path

                    return self._success(
                        {
                            "target_id": f"0x{target_hash:0{hex_chars}x}",
                            "rtt_ms": round(rtt_ms, 2),
                            "snr_db": result["snr"],
                            "rssi": result["rssi"],
                            "path": [f"0x{h:0{hex_chars}x}" for h in grouped_path],
                            "tag": trace_tag,
                            "path_hash_mode": path_hash_mode,
                        },
                        message="Ping successful",
                    )
                else:
                    return self._error("Received response but no data")
            else:
                # Timeout
                trace_helper.pending_pings.pop(trace_tag, None)
                return self._error(f"Ping timeout after {timeout}s")

        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error pinging neighbor: {e}", exc_info=True)
            return self._error(str(e))

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
            if not self.daemon_instance or not hasattr(self.daemon_instance, "identity_manager"):
                return self._error("Identity manager not available")

            # Get runtime registered identities
            identity_manager = self.daemon_instance.identity_manager
            registered_identities = identity_manager.list_identities()

            # Get configured identities from config
            identities_config = self.config.get("identities", {})
            room_servers = identities_config.get("room_servers") or []

            companions_cfg = identities_config.get("companions") or []
            if heal_companion_empty_names(companions_cfg):
                self.config.setdefault("identities", {})["companions"] = companions_cfg
                if self.config_manager:
                    if self.config_manager.save_to_file():
                        logger.info(
                            "Healed companion registration name(s): empty name -> companion_<pubkeyPrefix>"
                        )
                    else:
                        logger.warning("Failed to save config after healing companion name(s)")

            # Enhance with config data (room servers)
            configured = []
            for room_config in room_servers:
                name = room_config.get("name")
                identity_key = room_config.get("identity_key", "")
                settings = room_config.get("settings", {})

                # Find matching registered identity for additional data
                matching = next(
                    (r for r in registered_identities if r["name"] == f"room_server:{name}"), None
                )

                configured.append(
                    {
                        "name": name,
                        "type": "room_server",
                        "identity_key": (
                            identity_key[:16] + "..." if len(identity_key) > 16 else identity_key
                        ),
                        "identity_key_length": len(identity_key),
                        "settings": settings,
                        "hash": matching["hash"] if matching else None,
                        "address": matching["address"] if matching else None,
                        "registered": matching is not None,
                    }
                )

            # Configured companions (same pattern as room servers)
            companions = identities_config.get("companions") or []
            configured_companions = []
            for comp_config in companions:
                name = comp_config.get("name")
                raw_ik = comp_config.get("identity_key", "")
                if isinstance(raw_ik, bytes):
                    ik_hex = raw_ik.hex()
                else:
                    ik_hex = str(raw_ik)
                settings = comp_config.get("settings", {})

                matching = next(
                    (
                        r
                        for r in registered_identities
                        if r["name"] == f"companion:{name}"
                    ),
                    None,
                )

                pk_display = None
                if matching:
                    pk_display = matching.get("public_key")
                else:
                    pk_display = derive_companion_public_key_hex(comp_config.get("identity_key"))

                configured_companions.append(
                    {
                        "name": name,
                        "type": "companion",
                        "identity_key": (
                            ik_hex[:16] + "..." if len(ik_hex) > 16 else ik_hex
                        ),
                        "identity_key_length": len(ik_hex),
                        "settings": settings,
                        "hash": matching["hash"] if matching else None,
                        "public_key": pk_display,
                        "registered": matching is not None,
                    }
                )

            return self._success(
                {
                    "registered": registered_identities,
                    "configured": configured,
                    "configured_companions": configured_companions,
                    "total_registered": len(registered_identities),
                    "total_configured": len(configured),
                    "total_configured_companions": len(configured_companions),
                }
            )

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
            companions = identities_config.get("companions") or []

            # Find the identity in config (room servers first, then companions)
            identity_config = next((r for r in room_servers if r.get("name") == name), None)
            if identity_config is None:
                identity_config = next((c for c in companions if c.get("name") == name), None)

            if not identity_config:
                return self._error(f"Identity '{name}' not found")

            # Get runtime info if available (identity_manager uses name for both types)
            if self.daemon_instance and hasattr(self.daemon_instance, "identity_manager"):
                identity_manager = self.daemon_instance.identity_manager
                runtime_info = identity_manager.get_identity_by_name(name)

                if runtime_info:
                    identity_obj, config, identity_type = runtime_info
                    identity_config["runtime"] = {
                        "hash": self._fmt_hash(identity_obj.get_public_key()),
                        "address": identity_obj.get_address_bytes().hex(),
                        "type": identity_type,
                        "registered": True,
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

            raw_name = data.get("name")
            name = str(raw_name).strip() if raw_name is not None else ""
            identity_key = data.get("identity_key")
            identity_type = data.get("type", "room_server")
            settings = data.get("settings", {})

            if not name:
                return self._error("Missing required field: name")

            # Validate identity type
            if identity_type not in ["room_server", "companion"]:
                return self._error(
                    f"Invalid identity type: {identity_type}. Only 'room_server' and 'companion' are supported."
                )

            # Room server: validate passwords are different if both provided
            if identity_type == "room_server":
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

            identities_config = self.config.get("identities", {})
            if "identities" not in self.config:
                self.config["identities"] = {}

            if identity_type == "companion":
                # Companion: validate key length (32 or 64 bytes hex), normalize settings
                if identity_key:
                    try:
                        key_bytes = bytes.fromhex(identity_key)
                        if len(key_bytes) not in (32, 64):
                            return self._error(
                                "Companion identity_key must be 32 or 64 bytes (64 or 128 hex chars)"
                            )
                    except ValueError:
                        return self._error("Companion identity_key must be a valid hex string")

                companions = identities_config.get("companions") or []
                if any(str(c.get("name") or "").strip() == name for c in companions):
                    return self._error(f"Companion with name '{name}' already exists")

                comp_settings = {
                    "node_name": settings.get("node_name") or name,
                    "tcp_port": settings.get("tcp_port", 5000),
                    "bind_address": settings.get("bind_address", "0.0.0.0"),
                }
                if "tcp_timeout" in settings:
                    comp_settings["tcp_timeout"] = settings["tcp_timeout"]
                new_identity = {
                    "name": name,
                    "identity_key": identity_key,
                    "type": identity_type,
                    "settings": comp_settings,
                }
                companions.append(new_identity)
                self.config["identities"]["companions"] = companions
            else:
                # Room server
                room_servers = identities_config.get("room_servers") or []
                if any(str(r.get("name") or "").strip() == name for r in room_servers):
                    return self._error(f"Identity with name '{name}' already exists")

                new_identity = {
                    "name": name,
                    "identity_key": identity_key,
                    "type": identity_type,
                    "settings": settings,
                }
                room_servers.append(new_identity)
                self.config["identities"]["room_servers"] = room_servers

            # Save to file
            saved = self.config_manager.save_to_file()
            if not saved:
                return self._error("Failed to save configuration to file")

            logger.info(
                f"Created new identity: {name} (type: {identity_type}){' with auto-generated key' if key_was_generated else ''}"
            )

            # Hot reload - register identity immediately
            registration_success = False
            if identity_type == "room_server" and self.daemon_instance:
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
                            identity_key_bytes = (
                                identity_key.encode("latin-1")
                                if len(identity_key) == 32
                                else identity_key.encode("utf-8")
                            )
                    else:
                        logger.error(f"Unknown identity_key type: {type(identity_key)}")
                        identity_key_bytes = bytes(identity_key)

                    room_identity = LocalIdentity(seed=identity_key_bytes)

                    # Use the consolidated registration method
                    if hasattr(self.daemon_instance, "_register_identity_everywhere"):
                        registration_success = self.daemon_instance._register_identity_everywhere(
                            name=name,
                            identity=room_identity,
                            config=new_identity,
                            identity_type=identity_type,
                        )
                        if registration_success:
                            logger.info(
                                f"Hot reload: Registered identity '{name}' with all systems"
                            )
                        else:
                            logger.warning(f"Hot reload: Failed to register identity '{name}'")

                except Exception as reg_error:
                    logger.error(
                        f"Failed to hot reload identity {name}: {reg_error}", exc_info=True
                    )

            elif identity_type == "companion" and self.daemon_instance and self.event_loop:
                try:
                    import asyncio

                    future = asyncio.run_coroutine_threadsafe(
                        self.daemon_instance.add_companion_from_config(new_identity),
                        self.event_loop,
                    )
                    future.result(timeout=15)
                    registration_success = True
                    logger.info(f"Hot reload: Companion '{name}' activated immediately")
                except Exception as comp_error:
                    logger.warning(
                        f"Hot reload companion '{name}' failed: {comp_error}. Restart required to activate.",
                        exc_info=True,
                    )

            if identity_type == "companion":
                message = (
                    f"Companion '{name}' created successfully and activated immediately!"
                    if registration_success
                    else f"Companion '{name}' created successfully. Restart required to activate."
                )
            else:
                message = (
                    f"Identity '{name}' created successfully and activated immediately!"
                    if registration_success
                    else f"Identity '{name}' created successfully. Restart required to activate."
                )
            if key_was_generated:
                message += " Identity key was auto-generated."

            return self._success(new_identity, message=message)

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
                cherrypy.response.headers["Allow"] = "PUT"
                raise cherrypy.HTTPError(405, "Method not allowed. This endpoint requires PUT.")

            data = cherrypy.request.json or {}

            name = data.get("name")
            name_s = str(name).strip() if name is not None else ""
            lookup_identity_key = data.get("lookup_identity_key")
            public_key_prefix = data.get("public_key_prefix")

            identity_type = data.get("type", "room_server")
            if identity_type not in ["room_server", "companion"]:
                return self._error(
                    f"Invalid identity type: {identity_type}. Only 'room_server' and 'companion' are supported."
                )

            identities_config = self.config.get("identities", {})

            if identity_type == "companion":
                companions = identities_config.get("companions") or []
                if name_s:
                    identity_index, err = find_companion_index(companions, name=name_s)
                else:
                    identity_index, err = find_companion_index(
                        companions,
                        identity_key=lookup_identity_key,
                        public_key_prefix=public_key_prefix,
                    )
                if err:
                    return self._error(err)
                identity = companions[identity_index]
                resolved_name = str(identity.get("name") or "").strip()

                if "new_name" in data:
                    new_name = data["new_name"]
                    new_name = str(new_name).strip() if new_name is not None else ""
                    if not new_name:
                        return self._error("new_name cannot be empty")
                    if any(
                        str(c.get("name") or "").strip() == new_name
                        for i, c in enumerate(companions)
                        if i != identity_index
                    ):
                        return self._error(f"Companion with name '{new_name}' already exists")
                    identity["name"] = new_name

                if "identity_key" in data and data["identity_key"]:
                    new_key = data["identity_key"]
                    if "..." not in new_key:
                        try:
                            key_bytes = bytes.fromhex(new_key)
                            if len(key_bytes) in (32, 64):
                                identity["identity_key"] = new_key
                                logger.info(
                                    f"Updated identity_key for companion '{resolved_name}'"
                                )
                        except ValueError:
                            pass

                if "settings" in data:
                    if "settings" not in identity:
                        identity["settings"] = {}
                    # Only allow companion settings
                    for k, v in data["settings"].items():
                        if k in ("node_name", "tcp_port", "bind_address", "tcp_timeout"):
                            identity["settings"][k] = v

                companions[identity_index] = identity
                self.config["identities"]["companions"] = companions
                saved = self.config_manager.save_to_file()
                if not saved:
                    return self._error("Failed to save configuration to file")
                logger.info(f"Updated companion: {resolved_name}")
                message = (
                    f"Companion '{resolved_name}' updated successfully. "
                    "Restart required to apply changes."
                )
                return self._success(identity, message=message)

            # Room server path
            if not name_s:
                return self._error("Missing required field: name")

            room_servers = identities_config.get("room_servers") or []
            identity_index = next(
                (
                    i
                    for i, r in enumerate(room_servers)
                    if str(r.get("name") or "").strip() == name_s
                ),
                None,
            )

            if identity_index is None:
                return self._error(f"Identity '{name_s}' not found")

            # Update fields
            identity = room_servers[identity_index]

            if "new_name" in data:
                new_name = data["new_name"]
                new_name = str(new_name).strip() if new_name is not None else ""
                if not new_name:
                    return self._error("new_name cannot be empty")
                # Check if new name conflicts
                if any(
                    str(r.get("name") or "").strip() == new_name
                    for i, r in enumerate(room_servers)
                    if i != identity_index
                ):
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
                        logger.info(f"Updated identity_key for '{name_s}'")
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

            saved = self.config_manager.save_to_file()
            if not saved:
                return self._error("Failed to save configuration to file")

            logger.info(f"Updated identity: {name_s}")

            # Hot reload - re-register identity if key changed or name changed
            registration_success = False
            # Only reload if identity_key was actually provided and not empty, or if name changed
            needs_reload = data.get("identity_key") or "new_name" in data

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
                            logger.error(
                                f"Identity key for {final_name} is not valid hex string: {e}"
                            )
                            identity_key_bytes = (
                                identity_key.encode("latin-1")
                                if len(identity_key) == 32
                                else identity_key.encode("utf-8")
                            )
                    else:
                        logger.error(f"Unknown identity_key type: {type(identity_key)}")
                        identity_key_bytes = bytes(identity_key)

                    room_identity = LocalIdentity(seed=identity_key_bytes)

                    # Use the consolidated registration method
                    if hasattr(self.daemon_instance, "_register_identity_everywhere"):
                        registration_success = self.daemon_instance._register_identity_everywhere(
                            name=final_name,
                            identity=room_identity,
                            config=identity,
                            identity_type="room_server",
                        )
                        if registration_success:
                            logger.info(
                                f"Hot reload: Re-registered identity '{final_name}' with all systems"
                            )
                        else:
                            logger.warning(
                                f"Hot reload: Failed to re-register identity '{final_name}'"
                            )

                except Exception as reg_error:
                    logger.error(
                        f"Failed to hot reload identity {name_s}: {reg_error}", exc_info=True
                    )

            if needs_reload:
                message = (
                    f"Identity '{name_s}' updated successfully and changes applied immediately!"
                    if registration_success
                    else f"Identity '{name_s}' updated successfully. Restart required to apply changes."
                )
            else:
                message = (
                    f"Identity '{name_s}' updated successfully (settings only, no reload needed)."
                )

            return self._success(identity, message=message)

        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error updating identity: {e}")
            return self._error(e)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def delete_identity(self, name=None, type=None, lookup_identity_key=None, public_key_prefix=None):
        """
        DELETE /api/delete_identity?name=<name>&type=<room_server|companion> - Delete an identity
        Companions may also be deleted with lookup_identity_key or public_key_prefix when name is empty.
        """
        # Enable CORS for this endpoint only if configured
        self._set_cors_headers()

        if cherrypy.request.method == "OPTIONS":
            return ""

        try:
            if cherrypy.request.method != "DELETE":
                cherrypy.response.status = 405
                cherrypy.response.headers["Allow"] = "DELETE"
                raise cherrypy.HTTPError(405, "Method not allowed. This endpoint requires DELETE.")

            name_s = str(name).strip() if name is not None else ""

            identity_type = (type or "room_server").lower()
            if identity_type not in ["room_server", "companion"]:
                return self._error(
                    f"Invalid type: {type}. Use 'room_server' or 'companion'."
                )

            identities_config = self.config.get("identities", {})

            if identity_type == "companion":
                if not name_s and not lookup_identity_key and not public_key_prefix:
                    return self._error(
                        "Missing name parameter or lookup_identity_key or public_key_prefix"
                    )
                companions = identities_config.get("companions") or []
                if name_s:
                    idx, err = find_companion_index(companions, name=name_s)
                else:
                    idx, err = find_companion_index(
                        companions,
                        identity_key=lookup_identity_key,
                        public_key_prefix=public_key_prefix,
                    )
                if err:
                    return self._error(err)
                resolved_name = str(companions[idx].get("name") or "").strip()
                companions.pop(idx)
                self.config["identities"]["companions"] = companions
                saved = self.config_manager.save_to_file()
                if not saved:
                    return self._error("Failed to save configuration to file")
                logger.info(f"Deleted companion: {resolved_name}")
                unregister_success = False
                if self.daemon_instance and hasattr(self.daemon_instance, "identity_manager"):
                    identity_manager = self.daemon_instance.identity_manager
                    if resolved_name and resolved_name in identity_manager.named_identities:
                        del identity_manager.named_identities[resolved_name]
                        logger.info(f"Removed companion {resolved_name} from named_identities")
                        unregister_success = True
                message = (
                    f"Companion '{resolved_name}' deleted successfully and deactivated immediately!"
                    if unregister_success
                    else (
                        f"Companion '{resolved_name}' deleted successfully. "
                        "Restart required to fully remove."
                    )
                )
                return self._success({"name": resolved_name}, message=message)

            # Room server path
            if not name_s:
                return self._error("Missing name parameter")

            room_servers = identities_config.get("room_servers") or []

            # Find and remove the identity
            initial_count = len(room_servers)
            room_servers = [
                r for r in room_servers if str(r.get("name") or "").strip() != name_s
            ]

            if len(room_servers) == initial_count:
                return self._error(f"Identity '{name_s}' not found")

            # Update config
            self.config["identities"]["room_servers"] = room_servers

            saved = self.config_manager.save_to_file()
            if not saved:
                return self._error("Failed to save configuration to file")

            logger.info(f"Deleted identity: {name_s}")

            unregister_success = False
            if self.daemon_instance:
                try:
                    if hasattr(self.daemon_instance, "identity_manager"):
                        identity_manager = self.daemon_instance.identity_manager

                        # Remove from named_identities dict
                        if name_s in identity_manager.named_identities:
                            del identity_manager.named_identities[name_s]
                            logger.info(f"Removed identity {name_s} from named_identities")
                            unregister_success = True

                        # Note: We don't remove from identities dict (keyed by hash)
                        # because we'd need to look up the hash first, and there could
                        # be multiple identities with the same hash
                        # Full cleanup happens on restart

                except Exception as unreg_error:
                    logger.error(
                        f"Failed to unregister identity {name_s}: {unreg_error}", exc_info=True
                    )

            message = (
                f"Identity '{name_s}' deleted successfully and deactivated immediately!"
                if unregister_success
                else f"Identity '{name_s}' deleted successfully. Restart required to fully remove."
            )

            return self._success({"name": name_s}, message=message)

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
            if not hasattr(self.daemon_instance, "identity_manager"):
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
                    disable_fwd=disable_fwd,
                ),
                self.event_loop,
            )

            result = future.result(timeout=10)

            if result:
                return self._success(
                    {
                        "name": name,
                        "node_name": node_name,
                        "latitude": latitude,
                        "longitude": longitude,
                    },
                    message=f"Advert sent for room server '{node_name}'",
                )
            else:
                return self._error(f"Failed to send advert for room server '{name}'")

        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error sending room server advert: {e}", exc_info=True)
            return self._error(e)

    async def _send_room_server_advert_async(
        self, identity, node_name, latitude, longitude, disable_fwd
    ):
        """Send advert for a room server identity"""
        try:
            from pymc_core.protocol import PacketBuilder
            from pymc_core.protocol.constants import (
                ADVERT_FLAG_HAS_NAME,
                ADVERT_FLAG_IS_ROOM_SERVER,
            )

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

            logger.info(
                f"Sent flood advert for room server '{node_name}' at ({latitude:.6f}, {longitude:.6f})"
            )
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
            if not self.daemon_instance or not hasattr(self.daemon_instance, "login_helper"):
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
                    acl_info_list.append(
                        {
                            "name": "repeater",
                            "type": "repeater",
                            "hash": self._fmt_hash(self.daemon_instance.local_identity.get_public_key()),
                            "max_clients": repeater_acl.max_clients,
                            "authenticated_clients": repeater_acl.get_num_clients(),
                            "has_admin_password": bool(repeater_acl.admin_password),
                            "has_guest_password": bool(repeater_acl.guest_password),
                            "allow_read_only": repeater_acl.allow_read_only,
                        }
                    )

            # Add room server identities
            for name, identity, config in identity_manager.get_identities_by_type("room_server"):
                hash_byte = identity.get_public_key()[0]
                acl = acl_dict.get(hash_byte)

                if acl:
                    acl_info_list.append(
                        {
                            "name": name,
                            "type": "room_server",
                            "hash": self._fmt_hash(identity.get_public_key()),
                            "max_clients": acl.max_clients,
                            "authenticated_clients": acl.get_num_clients(),
                            "has_admin_password": bool(acl.admin_password),
                            "has_guest_password": bool(acl.guest_password),
                            "allow_read_only": acl.allow_read_only,
                        }
                    )

            # Add companion identities (no login/ACL fields; use registered + active for status)
            companion_bridges = getattr(self.daemon_instance, "companion_bridges", {})
            # Build hash -> active TCP connection and client IP (frame server has at most one client)
            active_by_hash = {}
            client_ip_by_hash = {}
            for fs in getattr(self.daemon_instance, "companion_frame_servers", []):
                try:
                    ch = getattr(fs, "companion_hash", None)
                    h = (
                        int(ch, 16)
                        if isinstance(ch, str) and ch.startswith("0x")
                        else (int(ch) if ch is not None else None)
                    )
                    if h is not None:
                        writer = getattr(fs, "_client_writer", None)
                        active_by_hash[h] = writer is not None
                        if writer is not None:
                            peername = writer.get_extra_info("peername") if hasattr(writer, "get_extra_info") else None
                            client_ip_by_hash[h] = str(peername[0]) if peername else None
                except (ValueError, TypeError):
                    pass
            for name, identity, config in identity_manager.get_identities_by_type("companion"):
                hash_byte = identity.get_public_key()[0]
                active = active_by_hash.get(hash_byte, False)
                entry = {
                    "name": name,
                    "type": "companion",
                    "hash": f"0x{hash_byte:02X}",
                    "registered": hash_byte in companion_bridges,
                    "active": active,
                }
                if active:
                    entry["client_ip"] = client_ip_by_hash.get(hash_byte)
                else:
                    entry["client_ip"] = None
                acl_info_list.append(entry)

            return self._success(
                {
                    "acls": acl_info_list,
                    "total_identities": len(acl_info_list),
                    "total_authenticated_clients": sum(
                        a.get("authenticated_clients", 0) for a in acl_info_list
                    ),
                }
            )

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
            if not self.daemon_instance or not hasattr(self.daemon_instance, "login_helper"):
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
                    "hash": self._fmt_hash(self.daemon_instance.local_identity.get_public_key()),
                }

            # Add room servers
            for name, identity, config in identity_manager.get_identities_by_type("room_server"):
                hash_byte = identity.get_public_key()[0]
                identity_map[hash_byte] = {
                    "name": name,
                    "type": "room_server",
                    "hash": self._fmt_hash(identity.get_public_key()),
                }

            # Add companions
            for name, identity, config in identity_manager.get_identities_by_type("companion"):
                hash_byte = identity.get_public_key()[0]
                identity_map[hash_byte] = {
                    "name": name,
                    "type": "companion",
                    "hash": f"0x{hash_byte:02X}",
                }

            # Filter by identity if requested
            target_hash = None
            if identity_hash:
                # Convert "0x42" to int
                try:
                    target_hash = (
                        int(identity_hash, 16)
                        if identity_hash.startswith("0x")
                        else int(identity_hash)
                    )
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

                identity_info = identity_map.get(
                    hash_byte, {"name": "unknown", "type": "unknown", "hash": f"0x{hash_byte:02X}"}
                )

                all_clients = acl.get_all_clients()
                logger.info(
                    f"Identity {identity_info['name']} (0x{hash_byte:02X}) has {len(all_clients)} clients"
                )

                for client in all_clients:
                    try:
                        pub_key = client.id.get_public_key()

                        # Compute address from public key (first byte of SHA256)
                        address_bytes = CryptoUtils.sha256(pub_key)[:1]

                        clients_list.append(
                            {
                                "public_key": pub_key[:8].hex() + "..." + pub_key[-4:].hex(),
                                "public_key_full": pub_key.hex(),
                                "address": address_bytes.hex(),
                                "permissions": "admin" if client.is_admin() else "guest",
                                "last_activity": client.last_activity,
                                "last_login_success": client.last_login_success,
                                "last_timestamp": client.last_timestamp,
                                "identity_name": identity_info["name"],
                                "identity_type": identity_info["type"],
                                "identity_hash": identity_info["hash"],
                            }
                        )
                    except Exception as client_error:
                        logger.error(f"Error processing client: {client_error}", exc_info=True)
                        continue

            logger.info(f"Returning {len(clients_list)} total clients")

            return self._success(
                {
                    "clients": clients_list,
                    "count": len(clients_list),
                    "filter": (
                        {"identity_hash": identity_hash, "identity_name": identity_name}
                        if (identity_hash or identity_name)
                        else None
                    ),
                }
            )

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

            if not self.daemon_instance or not hasattr(self.daemon_instance, "login_helper"):
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
                    target_hash = (
                        int(identity_hash_str, 16)
                        if identity_hash_str.startswith("0x")
                        else int(identity_hash_str)
                    )
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
                return self._success(
                    {"removed_count": removed_count, "removed_from": removed_from},
                    message=f"Client removed from {removed_count} ACL(s)",
                )
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
            if not self.daemon_instance or not hasattr(self.daemon_instance, "login_helper"):
                return self._error("Login helper not available")

            login_helper = self.daemon_instance.login_helper
            identity_manager = self.daemon_instance.identity_manager
            acl_dict = login_helper.get_acl_dict()

            total_clients = 0
            admin_count = 0
            guest_count = 0

            identity_stats = {
                "repeater": {"count": 0, "clients": 0},
                "room_server": {"count": 0, "clients": 0},
                "companion": {"count": 0, "clients": 0},
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

            # Count companions (no admin/guest; they use frame server, not OTA login)
            companions = identity_manager.get_identities_by_type("companion")
            identity_stats["companion"]["count"] = len(companions)

            for name, identity, config in companions:
                hash_byte = identity.get_public_key()[0]
                acl = acl_dict.get(hash_byte)
                if acl:
                    clients = acl.get_all_clients()
                    identity_stats["companion"]["clients"] += len(clients)
                    total_clients += len(clients)

            return self._success(
                {
                    "total_identities": len(acl_dict),
                    "total_clients": total_clients,
                    "admin_clients": admin_count,
                    "guest_clients": guest_count,
                    "by_identity_type": identity_stats,
                }
            )

        except Exception as e:
            logger.error(f"Error getting ACL stats: {e}")
            return self._error(e)

    # ======================
    # Room Server Endpoints
    # ======================

    def _get_room_server_by_name_or_hash(self, room_name=None, room_hash=None):
        """Helper to get room server instance and metadata by name or hash."""
        if not self.daemon_instance or not hasattr(self.daemon_instance, "text_helper"):
            raise Exception("Text helper not available")

        text_helper = self.daemon_instance.text_helper
        if not text_helper or not hasattr(text_helper, "room_servers"):
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
                            "room_server": room_server,
                            "name": name,
                            "hash": hash_byte,
                            "identity": identity,
                            "config": config,
                        }
            raise Exception(f"Room '{room_name}' not found")

        # Find by hash
        if room_hash:
            if isinstance(room_hash, str):
                if room_hash.startswith("0x"):
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
                            "room_server": room_server,
                            "name": name,
                            "hash": hash_byte,
                            "identity": identity,
                            "config": config,
                        }
                # Found server but no name match
                return {
                    "room_server": room_server,
                    "name": f"Room_0x{hash_byte:02X}",
                    "hash": hash_byte,
                    "identity": None,
                    "config": {},
                }
            raise Exception(f"Room with hash {room_hash} not found")

        raise Exception("Must provide room_name or room_hash")

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def room_messages(
        self, room_name=None, room_hash=None, limit=50, offset=0, since_timestamp=None
    ):
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
            room_server = room_info["room_server"]

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
                    limit=int(limit),
                )
            else:
                messages = db.get_room_messages(
                    room_hash=room_hash_str, limit=int(limit), offset=int(offset)
                )

            # Format messages with author prefix and lookup sender names
            storage = self._get_storage()
            formatted_messages = []
            for msg in messages:
                author_pubkey = msg["author_pubkey"]
                formatted_msg = {
                    "id": msg["id"],
                    "author_pubkey": author_pubkey,
                    "author_prefix": author_pubkey[:8] if author_pubkey else "",
                    "post_timestamp": msg["post_timestamp"],
                    "sender_timestamp": msg["sender_timestamp"],
                    "message_text": msg["message_text"],
                    "txt_type": msg["txt_type"],
                    "created_at": msg.get("created_at", msg["post_timestamp"]),
                }

                # Lookup sender name from adverts table
                if author_pubkey:
                    author_name = storage.get_node_name_by_pubkey(author_pubkey)
                    if author_name:
                        formatted_msg["author_name"] = author_name

                formatted_messages.append(formatted_msg)

            return self._success(
                {
                    "room_name": room_info["name"],
                    "room_hash": room_hash_str,
                    "messages": formatted_messages,
                    "count": len(formatted_messages),
                    "total": total_count,
                    "limit": int(limit),
                    "offset": int(offset),
                }
            )

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
            room_name = data.get("room_name")
            room_hash = data.get("room_hash")
            message = data.get("message")
            author_pubkey = data.get("author_pubkey")
            txt_type = data.get("txt_type", 0)

            if not message:
                return self._error("message is required")
            if not author_pubkey:
                return self._error("author_pubkey is required")

            # Convert author_pubkey to bytes
            try:
                # Special case: "server" or "system" = use room server's public key
                # This allows clients to identify which room server sent the message
                if isinstance(author_pubkey, str) and author_pubkey.lower() in ("server", "system"):
                    # Get room server first to access its identity
                    room_info = self._get_room_server_by_name_or_hash(room_name, room_hash)
                    room_server = room_info["room_server"]
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
            if not isinstance(author_pubkey, str) or author_pubkey.lower() not in (
                "server",
                "system",
            ):
                room_info = self._get_room_server_by_name_or_hash(room_name, room_hash)
                room_server = room_info["room_server"]

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
                        allow_server_author=is_server_message,  # Allow server key from API
                    ),
                    self.event_loop,
                )
                success = future.result(timeout=5)

                if success:
                    # Get the message ID (last inserted)
                    db = room_server.db
                    room_hash_str = f"0x{room_info['hash']:02X}"
                    messages = db.get_room_messages(room_hash_str, limit=1, offset=0)
                    message_id = messages[0]["id"] if messages else None

                    return self._success(
                        {
                            "message_id": message_id,
                            "room_name": room_info["name"],
                            "room_hash": room_hash_str,
                            "queued_for_distribution": True,
                            "is_server_message": is_server_message,
                            "author_filter_note": (
                                "Server messages go to ALL clients"
                                if is_server_message
                                else "Message will NOT be sent to author"
                            ),
                        }
                    )
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
            if not self.daemon_instance or not hasattr(self.daemon_instance, "text_helper"):
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
                    active_clients = sum(
                        1 for c in all_clients_sync if c.get("last_activity", 0) > 0
                    )

                    all_rooms.append(
                        {
                            "room_name": room_name_found,
                            "room_hash": room_hash_str,
                            "total_messages": total_messages,
                            "total_clients": len(all_clients_sync),
                            "active_clients": active_clients,
                            "max_posts": room_server.max_posts,
                            "sync_running": room_server._running,
                        }
                    )

                return self._success({"rooms": all_rooms, "total_rooms": len(all_rooms)})

            # Get specific room stats
            room_info = self._get_room_server_by_name_or_hash(room_name, room_hash)
            room_server = room_info["room_server"]
            db = room_server.db
            room_hash_str = f"0x{room_info['hash']:02X}"

            # Get message count
            total_messages = db.get_room_message_count(room_hash_str)

            # Get client sync states
            all_clients_sync = db.get_all_room_clients(room_hash_str)

            # Get ACL for this room
            acl = None
            if room_info["hash"] in text_helper.acl_dict:
                acl = text_helper.acl_dict[room_info["hash"]]

            # Format client info
            clients_info = []
            active_count = 0
            for client_sync in all_clients_sync:
                pubkey_hex = client_sync["client_pubkey"]
                pubkey_bytes = bytes.fromhex(pubkey_hex)

                # Check if still in ACL
                in_acl = False
                if acl:
                    acl_clients = acl.get_all_clients()
                    in_acl = any(c.id.get_public_key() == pubkey_bytes for c in acl_clients)

                unsynced_count = db.get_unsynced_count(
                    room_hash=room_hash_str,
                    client_pubkey=pubkey_hex,
                    sync_since=client_sync.get("sync_since", 0),
                )

                is_active = client_sync.get("last_activity", 0) > 0
                if is_active:
                    active_count += 1

                clients_info.append(
                    {
                        "pubkey": pubkey_hex,
                        "pubkey_prefix": pubkey_hex[:8],
                        "sync_since": client_sync.get("sync_since", 0),
                        "unsynced_count": unsynced_count,
                        "pending_ack": client_sync.get("pending_ack_crc", 0) != 0,
                        "pending_ack_crc": client_sync.get("pending_ack_crc", 0),
                        "push_failures": client_sync.get("push_failures", 0),
                        "last_activity": client_sync.get("last_activity", 0),
                        "in_acl": in_acl,
                        "is_active": is_active,
                    }
                )

            return self._success(
                {
                    "room_name": room_info["name"],
                    "room_hash": room_hash_str,
                    "total_messages": total_messages,
                    "total_clients": len(all_clients_sync),
                    "active_clients": active_count,
                    "max_posts": room_server.max_posts,
                    "sync_running": room_server._running,
                    "next_push_time": room_server.next_push_time,
                    "last_cleanup_time": room_server.last_cleanup_time,
                    "clients": clients_info,
                }
            )

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
            if stats.get("success") and "clients" in stats.get("data", {}):
                data = stats["data"]
                return self._success(
                    {
                        "room_name": data["room_name"],
                        "room_hash": data["room_hash"],
                        "clients": data["clients"],
                        "total": len(data["clients"]),
                        "active": data["active_clients"],
                    }
                )
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
            room_server = room_info["room_server"]
            db = room_server.db
            room_hash_str = f"0x{room_info['hash']:02X}"

            # Delete message
            deleted = db.delete_room_message(room_hash_str, int(message_id))

            if deleted:
                return self._success(
                    {"deleted": True, "message_id": int(message_id), "room_name": room_info["name"]}
                )
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
            room_server = room_info["room_server"]
            db = room_server.db
            room_hash_str = f"0x{room_info['hash']:02X}"

            # Get count before deleting
            count_before = db.get_room_message_count(room_hash_str)

            # Clear all messages
            deleted = db.clear_room_messages(room_hash_str)

            return self._success(
                {
                    "deleted_count": deleted or count_before,
                    "room_name": room_info["name"],
                    "room_hash": room_hash_str,
                }
            )

        except Exception as e:
            logger.error(f"Error clearing room messages: {e}")
            return self._error(e)

    # ======================
    # CLI Command Endpoint
    # ======================

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    @require_auth
    def cli(self):
        """Execute a CLI command on the running repeater.
        POST /api/cli {"command": "get name"}
        Returns {"success": true, "reply": "..."}
        """
        self._set_cors_headers()
        if cherrypy.request.method == "OPTIONS":
            return ""
        try:
            self._require_post()
            data = cherrypy.request.json
            command = data.get("command", "").strip()
            if not command:
                return self._error("Missing 'command' field")

            if not self.daemon_instance or not hasattr(self.daemon_instance, "text_helper"):
                return self._error("Repeater not initialized")
            text_helper = self.daemon_instance.text_helper
            if not text_helper or not hasattr(text_helper, "cli") or not text_helper.cli:
                return self._error("CLI handler not available")

            reply = text_helper.cli.handle_command(
                sender_pubkey=b"api-cli",
                command=command,
                is_admin=True,
            )
            return self._success({"reply": reply})
        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"CLI endpoint error: {e}", exc_info=True)
            return self._error(str(e))

    # ======================
    # Backup & Restore
    # ======================

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def config_export(self, include_secrets=None):
        """Export the full configuration as JSON.

        GET /api/config_export
        GET /api/config_export?include_secrets=true   (full backup with secrets)

        By default, sensitive fields (passwords, JWT secrets, identity keys)
        are redacted.  Pass ?include_secrets=true for a full backup that
        includes all secrets — required for restoring to a new device.

        Returns: {"success": true, "data": {"meta": {...}, "config": {...}}}
        """
        self._set_cors_headers()
        if cherrypy.request.method == "OPTIONS":
            return ""
        try:
            import copy

            full_backup = str(include_secrets).lower() in ("true", "1", "yes")
            exported = copy.deepcopy(self.config)

            if full_backup:
                # Convert binary identity key to hex for JSON serialisation
                rep = exported.get("repeater", {})
                if "identity_key" in rep and isinstance(rep["identity_key"], bytes):
                    rep["identity_key"] = rep["identity_key"].hex()

                # Convert identity keys in companion / room_server configs
                for section in ("room_servers", "companions"):
                    entries = exported.get("identities", {}).get(section, []) or []
                    for entry in entries:
                        if isinstance(entry.get("identity_key"), bytes):
                            entry["identity_key"] = entry["identity_key"].hex()
            else:
                # Redact sensitive fields
                sec = exported.get("repeater", {}).get("security", {})
                for field in ("admin_password", "guest_password", "jwt_secret"):
                    if field in sec:
                        sec[field] = "*** REDACTED ***"

                # Redact repeater identity key
                rep = exported.get("repeater", {})
                if "identity_key" in rep:
                    del rep["identity_key"]

                # Redact identity keys in companion / room_server configs
                for section in ("room_servers", "companions"):
                    entries = exported.get("identities", {}).get(section, []) or []
                    for entry in entries:
                        if "identity_key" in entry:
                            entry["identity_key"] = "*** REDACTED ***"

            # Ensure all bytes values are converted to hex for JSON serialisation
            def _sanitize(obj):
                if isinstance(obj, bytes):
                    return obj.hex()
                if isinstance(obj, dict):
                    return {k: _sanitize(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_sanitize(v) for v in obj]
                return obj

            exported = _sanitize(exported)

            meta = {
                "exported_at": datetime.utcnow().isoformat() + "Z",
                "version": __version__,
                "config_path": self._config_path,
                "includes_secrets": full_backup,
            }

            return {"success": True, "data": {"meta": meta, "config": exported}}

        except Exception as e:
            logger.error(f"Config export error: {e}", exc_info=True)
            return self._error(str(e))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def config_import(self):
        """Import a configuration JSON and apply it.

        POST /api/config_import
        Body: {"config": { ... }, "restart_after": false}

        The imported config is merged section-by-section into the current config.
        Sections present in the import will overwrite current values.
        Redacted sentinel values ("*** REDACTED ***") are skipped so that
        existing passwords / keys are preserved.

        If the import contains a non-redacted identity_key (from a full backup),
        it will be restored.  Redacted or missing identity keys are left unchanged.

        Returns: {"success": true, "message": "...", "restart_required": true,
                  "sections_updated": [...]}
        """
        self._set_cors_headers()
        if cherrypy.request.method == "OPTIONS":
            return ""
        try:
            self._require_post()
            data = cherrypy.request.json
            imported_config = data.get("config")

            if not imported_config or not isinstance(imported_config, dict):
                return self._error("Missing or invalid 'config' object in request body")

            # Sections we allow to be imported
            ALLOWED_SECTIONS = {
                "repeater", "mesh", "radio", "identities", "delays",
                "ch341", "web", "letsmesh", "glass", "logging", "radio_type",
            }

            updated_sections = []
            restart_required = False

            for section, value in imported_config.items():
                if section not in ALLOWED_SECTIONS:
                    logger.info(f"Config import: skipping unknown section '{section}'")
                    continue

                if section == "repeater" and isinstance(value, dict):
                    # Preserve security secrets that are redacted
                    sec = value.get("security", {})
                    if isinstance(sec, dict):
                        cur_sec = self.config.get("repeater", {}).get("security", {})
                        for field in ("admin_password", "guest_password", "jwt_secret"):
                            if sec.get(field) == "*** REDACTED ***":
                                sec[field] = cur_sec.get(field, "")
                    # Restore identity_key only if a real (non-redacted) hex value is provided
                    ik = value.get("identity_key")
                    if ik and isinstance(ik, str) and ik != "*** REDACTED ***":
                        try:
                            value["identity_key"] = bytes.fromhex(ik)
                        except ValueError:
                            logger.warning("Config import: invalid identity_key hex, skipping")
                            value.pop("identity_key", None)
                    else:
                        value.pop("identity_key", None)
                    value.pop("identity_file", None)

                if section == "identities" and isinstance(value, dict):
                    # Preserve identity keys that are redacted
                    for id_section in ("room_servers", "companions"):
                        entries = value.get(id_section, []) or []
                        cur_entries = (
                            self.config.get("identities", {}).get(id_section, []) or []
                        )
                        cur_by_name = {e.get("name"): e for e in cur_entries}
                        for entry in entries:
                            if entry.get("identity_key") == "*** REDACTED ***":
                                existing = cur_by_name.get(entry.get("name"), {})
                                entry["identity_key"] = existing.get("identity_key", "")

                if section == "radio":
                    restart_required = True

                if section == "radio_type":
                    # radio_type is a top-level scalar, not a dict
                    self.config[section] = value
                else:
                    if section not in self.config:
                        self.config[section] = {}
                    if isinstance(value, dict) and isinstance(self.config[section], dict):
                        self.config[section].update(value)
                    else:
                        self.config[section] = value

                updated_sections.append(section)

            if not updated_sections:
                return self._error("No valid configuration sections found in import")

            # Persist and live-reload
            result = self.config_manager.update_and_save(
                updates={},  # Already applied above
                live_update=True,
                live_update_sections=updated_sections,
            )

            # Save to file (update_and_save with empty updates may not save)
            saved = self.config_manager.save_to_file()

            return {
                "success": True,
                "message": f"Imported {len(updated_sections)} config section(s)",
                "sections_updated": updated_sections,
                "saved": saved,
                "restart_required": restart_required,
            }

        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Config import error: {e}", exc_info=True)
            return self._error(str(e))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def identity_export(self):
        """Export the repeater's identity key as a hex string.

        GET /api/identity_export

        WARNING: This transmits the private key over the network.
        Only use on trusted networks.

        Returns: {"success": true, "data": {"identity_key_hex": "abcdef...",
                  "key_length_bytes": 32, "public_key_hex": "...",
                  "node_address": "0x42"}}
        """
        self._set_cors_headers()
        if cherrypy.request.method == "OPTIONS":
            return ""
        try:
            identity_key = self.config.get("repeater", {}).get("identity_key")
            if not identity_key:
                return self._error("No identity key configured")

            # Convert to hex
            if isinstance(identity_key, bytes):
                key_hex = identity_key.hex()
            elif isinstance(identity_key, str):
                key_hex = identity_key
            else:
                return self._error(f"Identity key has unexpected type: {type(identity_key).__name__}")

            result = {
                "identity_key_hex": key_hex,
                "key_length_bytes": len(bytes.fromhex(key_hex)),
            }

            # Try to derive public key info
            try:
                if self.daemon_instance and hasattr(self.daemon_instance, "local_identity"):
                    li = self.daemon_instance.local_identity
                    pub = li.get_public_key()
                    result["public_key_hex"] = bytes(pub).hex()
                    result["node_address"] = f"0x{pub[0]:02x}"
            except Exception:
                pass  # Not critical

            return {"success": True, "data": result}

        except Exception as e:
            logger.error(f"Identity export error: {e}", exc_info=True)
            return self._error(str(e))

    # ======================
    # Vanity Key Generation
    # ======================

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def generate_vanity_key(self):
        """Generate a MeshCore Ed25519 key whose public key starts with a hex prefix.

        POST /api/generate_vanity_key
        Body: {"prefix": "F8A1", "apply": false}

        prefix: 1-4 hex characters (required)
        apply:  if true, save the generated key as the repeater identity key

        Returns: {"success": true, "data": {"public_hex": "...", "private_hex": "...",
                  "attempts": 1234}}
        """
        self._set_cors_headers()
        if cherrypy.request.method == "OPTIONS":
            return ""
        try:
            self._require_post()
            data = cherrypy.request.json

            prefix = (data.get("prefix") or "").strip().upper()
            if not prefix or len(prefix) > 8:
                return self._error("Prefix must be 1-8 hex characters")
            try:
                int(prefix, 16)
            except ValueError:
                return self._error("Prefix must be valid hexadecimal characters")

            apply_key = bool(data.get("apply", False))

            from repeater.keygen import generate_vanity_key as _gen

            # Max iterations scales with prefix length: ~16^n * 20 safety margin
            max_iter = min(20_000_000, max(500_000, (16 ** len(prefix)) * 20))
            result = _gen(prefix, max_iterations=max_iter)

            if result is None:
                return self._error(
                    f"Could not find a key with prefix '{prefix}' within {max_iter:,} attempts. "
                    "Try a shorter prefix."
                )

            if apply_key:
                # Save as the repeater identity key
                self.config.setdefault("repeater", {})["identity_key"] = bytes.fromhex(
                    result["private_hex"]
                )
                self.config_manager.save_to_file()
                result["applied"] = True
                logger.info(
                    f"Applied new vanity identity key (prefix={prefix}, "
                    f"pub={result['public_hex'][:16]}...)"
                )

            return {"success": True, "data": result}

        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Vanity key generation error: {e}", exc_info=True)
            return self._error(str(e))

    # ======================
    # Database Management
    # ======================

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def db_stats(self):
        """Get database table statistics.

        GET /api/db_stats

        Returns row counts, date ranges, and total database size.
        """
        self._set_cors_headers()
        if cherrypy.request.method == "OPTIONS":
            return ""
        try:
            storage = self._get_storage()
            stats = storage.sqlite_handler.get_table_stats()

            # Add RRD file size if it exists
            rrd_path = storage.sqlite_handler.storage_dir / "metrics.rrd"
            stats["rrd_size_bytes"] = (
                rrd_path.stat().st_size if rrd_path.exists() else 0
            )

            return {"success": True, "data": stats}
        except Exception as e:
            logger.error(f"DB stats error: {e}", exc_info=True)
            return self._error(str(e))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def db_purge(self):
        """Purge (empty) one or more database tables.

        POST /api/db_purge
        Body: {"tables": ["packets", "adverts"]}
              or {"tables": "all"} to purge all data tables

        Returns per-table row counts deleted.
        """
        self._set_cors_headers()
        if cherrypy.request.method == "OPTIONS":
            return ""
        try:
            self._require_post()
            data = cherrypy.request.json
            tables_param = data.get("tables")

            if not tables_param:
                return self._error("Missing 'tables' parameter")

            ALL_PURGEABLE = [
                "packets", "adverts", "noise_floor", "crc_errors",
                "room_messages", "room_client_sync",
                "companion_contacts", "companion_channels",
                "companion_messages", "companion_prefs",
            ]

            if tables_param == "all":
                tables = ALL_PURGEABLE
            elif isinstance(tables_param, list):
                tables = tables_param
            else:
                return self._error("'tables' must be a list of table names or 'all'")

            storage = self._get_storage()
            results = {}
            for table in tables:
                try:
                    deleted = storage.sqlite_handler.purge_table(table)
                    results[table] = {"deleted": deleted}
                except ValueError as ve:
                    results[table] = {"error": str(ve)}

            return {
                "success": True,
                "data": results,
                "message": f"Purged {len([r for r in results.values() if 'deleted' in r])} table(s)",
            }
        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"DB purge error: {e}", exc_info=True)
            return self._error(str(e))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def db_vacuum(self):
        """Reclaim disk space after purging tables.

        POST /api/db_vacuum

        Runs SQLite VACUUM to compact the database file.
        """
        self._set_cors_headers()
        if cherrypy.request.method == "OPTIONS":
            return ""
        try:
            self._require_post()
            storage = self._get_storage()
            size_before = storage.sqlite_handler.sqlite_path.stat().st_size
            storage.sqlite_handler.vacuum()
            size_after = storage.sqlite_handler.sqlite_path.stat().st_size
            return {
                "success": True,
                "data": {
                    "size_before": size_before,
                    "size_after": size_after,
                    "freed_bytes": size_before - size_after,
                },
            }
        except cherrypy.HTTPError:
            raise
        except Exception as e:
            logger.error(f"DB vacuum error: {e}", exc_info=True)
            return self._error(str(e))

    # ======================
    # OpenAPI Documentation
    # ======================

    @cherrypy.expose
    def openapi(self):
        """Serve OpenAPI specification in YAML format."""
        import os

        spec_path = os.path.join(os.path.dirname(__file__), "openapi.yaml")
        try:
            with open(spec_path, "r") as f:
                spec_content = f.read()
            cherrypy.response.headers["Content-Type"] = "application/x-yaml"
            return spec_content.encode("utf-8")
        except FileNotFoundError:
            cherrypy.response.status = 404
            return b"OpenAPI spec not found"
        except Exception as e:
            cherrypy.response.status = 500
            return f"Error loading OpenAPI spec: {e}".encode("utf-8")

    @cherrypy.expose
    def docs(self):
        """Serve Swagger UI for interactive API documentation."""
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>pyMC Repeater API Documentation</title>
    <link rel="stylesheet" type="text/css" href="https://unpkg.com/swagger-ui-dist@5.10.0/swagger-ui.css">
    <style>
        body {
            margin: 0;
            padding: 0;
        }
    </style>
</head>
<body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5.10.0/swagger-ui-bundle.js"></script>
    <script src="https://unpkg.com/swagger-ui-dist@5.10.0/swagger-ui-standalone-preset.js"></script>
    <script>
        window.onload = function() {
            window.ui = SwaggerUIBundle({
                url: '/api/openapi',
                dom_id: '#swagger-ui',
                deepLinking: true,
                presets: [
                    SwaggerUIBundle.presets.apis,
                    SwaggerUIStandalonePreset
                ],
                plugins: [
                    SwaggerUIBundle.plugins.DownloadUrl
                ],
                layout: "StandaloneLayout"
            });
        };
    </script>
</body>
</html>"""
        cherrypy.response.headers["Content-Type"] = "text/html"
        return html.encode("utf-8")
