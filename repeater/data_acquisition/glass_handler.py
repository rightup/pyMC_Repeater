import asyncio
import hashlib
import json
import logging
import os
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from urllib import error, request

import psutil
try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

from repeater import __version__
from repeater.service_utils import restart_service

logger = logging.getLogger("GlassHandler")
_SENSITIVE_KEY_MARKERS = (
    "password",
    "passphrase",
    "secret",
    "token",
    "private_key",
    "identity_key",
    "client_key",
    "api_key",
)
_SENSITIVE_KEY_EXCEPTIONS = ("pubkey", "public_key")


class GlassHandler:
    def __init__(self, config: dict, daemon_instance=None, config_manager=None):
        self.config = config
        self.daemon_instance = daemon_instance
        self.config_manager = config_manager

        self.enabled = False
        self.base_url = "http://localhost:8080"
        self.request_timeout_seconds = 10
        self.verify_tls = True
        self.api_token = ""
        self.inform_interval_seconds = 30
        self.cert_store_dir = "/etc/pymc_repeater/glass"
        self._cert_expires_at: Optional[str] = None
        self.mqtt_enabled = False
        self.mqtt_broker_host = "localhost"
        self.mqtt_broker_port = 1883
        self.mqtt_base_topic = "glass"
        self.mqtt_tls_enabled = False
        self.mqtt_username: Optional[str] = None
        self.mqtt_password: Optional[str] = None
        self.client_cert_path: Optional[str] = None
        self.client_key_path: Optional[str] = None
        self.ca_cert_path: Optional[str] = None
        self._mqtt_client = None
        self._mqtt_ready = False
        self._mqtt_runtime_signature: Optional[
            Tuple[
                str,
                int,
                str,
                bool,
                bool,
                Optional[str],
                Optional[str],
                Optional[str],
                Optional[str],
                Optional[str],
            ]
        ] = None
        self._managed_settings_filename = "managed.json"

        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._pending_command_results: List[Dict[str, Any]] = []
        self._pending_lock = asyncio.Lock()

        self._reload_runtime_settings()

    async def start(self) -> None:
        self._reload_runtime_settings()
        if not self.enabled:
            logger.info("Glass integration disabled")
            self._close_mqtt_publisher()
            return

        if self._task and not self._task.done():
            return
        self._sync_mqtt_publisher()

        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="glass-inform-loop")
        logger.info(
            "Glass integration started (base_url=%s, inform_interval=%ss)",
            self.base_url,
            self.inform_interval_seconds,
        )

    async def stop(self) -> None:
        if self._task:
            if self._stop_event:
                self._stop_event.set()

            try:
                await self._task
            except Exception as exc:
                logger.debug("Glass task stop ignored exception: %s", exc)
            finally:
                self._task = None
                self._stop_event = None

        self._close_mqtt_publisher()

    def _reload_runtime_settings(self) -> None:
        glass_cfg = self.config.get("glass", {})
        self.enabled = bool(glass_cfg.get("enabled", False))

        base_url = str(glass_cfg.get("base_url", "http://localhost:8080")).strip()
        self.base_url = base_url.rstrip("/") if base_url else "http://localhost:8080"

        self.request_timeout_seconds = max(3, int(glass_cfg.get("request_timeout_seconds", 10)))
        self.verify_tls = bool(glass_cfg.get("verify_tls", True))
        self.api_token = str(glass_cfg.get("api_token", "") or "").strip()
        self.inform_interval_seconds = self._clamp_interval(
            int(glass_cfg.get("inform_interval_seconds", self.inform_interval_seconds))
        )
        self.cert_store_dir = str(
            glass_cfg.get("cert_store_dir", "/etc/pymc_repeater/glass") or "/etc/pymc_repeater/glass"
        )
        self.client_cert_path = (
            str(glass_cfg.get("client_cert_path")).strip()
            if glass_cfg.get("client_cert_path")
            else None
        )
        self.client_key_path = (
            str(glass_cfg.get("client_key_path")).strip()
            if glass_cfg.get("client_key_path")
            else None
        )
        self.ca_cert_path = (
            str(glass_cfg.get("ca_cert_path")).strip()
            if glass_cfg.get("ca_cert_path")
            else None
        )
        managed_cfg = self._load_managed_settings()
        parsed_base_url = urlparse(self.base_url)
        default_host = parsed_base_url.hostname or "localhost"

        self.mqtt_enabled = bool(managed_cfg.get("mqtt_enabled", False))
        host_value = managed_cfg.get("mqtt_broker_host", default_host)
        self.mqtt_broker_host = str(host_value or default_host).strip() or default_host
        try:
            self.mqtt_broker_port = max(1, int(managed_cfg.get("mqtt_broker_port", 1883)))
        except (TypeError, ValueError):
            self.mqtt_broker_port = 1883
        topic_value = managed_cfg.get("mqtt_base_topic", "glass")
        self.mqtt_base_topic = str(topic_value or "glass").strip("/")
        self.mqtt_tls_enabled = bool(managed_cfg.get("mqtt_tls_enabled", False))
        username = managed_cfg.get("mqtt_username")
        password = managed_cfg.get("mqtt_password")
        self.mqtt_username = str(username).strip() if isinstance(username, str) and username else None
        self.mqtt_password = str(password) if isinstance(password, str) and password else None

    def _managed_settings_path(self) -> Path:
        return Path(self.cert_store_dir) / self._managed_settings_filename

    def _load_managed_settings(self) -> Dict[str, Any]:
        path = self._managed_settings_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Invalid Glass managed settings file at %s: %s", path, exc)
            return {}
        if not isinstance(raw, dict):
            logger.warning("Ignoring non-object Glass managed settings file at %s", path)
            return {}
        return raw

    def _save_managed_settings(self, updates: Dict[str, Any], *, replace: bool) -> Tuple[bool, str]:
        if not isinstance(updates, dict):
            return False, "glass_managed must be an object"

        path = self._managed_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        current = {} if replace else self._load_managed_settings()
        if not isinstance(current, dict):
            current = {}
        merged = dict(current)
        merged.update(updates)
        try:
            path.write_text(
                json.dumps(merged, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
            return True, "Managed settings updated"
        except Exception as exc:
            return False, f"Failed writing managed settings: {exc}"

    async def _run_loop(self) -> None:
        while self._stop_event and not self._stop_event.is_set():
            self._reload_runtime_settings()
            self._sync_mqtt_publisher()
            try:
                interval = await self._inform_once()
            except Exception as exc:
                logger.warning("Glass inform failed: %s", exc)
                interval = self.inform_interval_seconds

            wait_seconds = self._clamp_interval(interval)
            if not self._stop_event:
                break
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                continue

    async def _inform_once(self) -> int:
        self._reload_runtime_settings()
        if not self.enabled:
            return self.inform_interval_seconds

        payload = await self._build_inform_payload()
        response = await self._post_inform(payload)

        if payload.get("command_results"):
            async with self._pending_lock:
                self._pending_command_results = []

        response_type = str(response.get("type", "noop"))
        response_interval = response.get("interval")

        if response_type == "command":
            await self._handle_command_response(response)
        elif response_type == "config_update":
            ok, message = self._apply_config_update(
                response.get("config", {}),
                str(response.get("merge_mode", "patch")),
            )
            if ok:
                logger.info("Applied Glass config update")
            else:
                logger.warning("Failed to apply Glass config update: %s", message)
        elif response_type == "cert_renewal":
            ok, message = self._apply_cert_renewal(response)
            if ok:
                logger.info("Applied Glass certificate renewal")
            else:
                logger.warning("Failed to apply Glass certificate renewal: %s", message)
        elif response_type == "upgrade":
            logger.warning("Glass upgrade action received but not implemented on repeater")
        elif response_type != "noop":
            logger.warning("Unknown Glass response type: %s", response_type)

        if isinstance(response_interval, int):
            self.inform_interval_seconds = self._clamp_interval(response_interval)
        return self.inform_interval_seconds

    async def _build_inform_payload(self) -> Dict[str, Any]:
        if not self.daemon_instance or not getattr(self.daemon_instance, "local_identity", None):
            raise RuntimeError("Local identity not available for Glass inform")

        stats = self.daemon_instance.get_stats() if self.daemon_instance else {}
        local_identity = self.daemon_instance.local_identity
        public_key = bytes(local_identity.get_public_key()).hex()
        node_name = self.config.get("repeater", {}).get("node_name", "unknown-repeater")

        uptime_seconds = int(stats.get("uptime_seconds", 0))
        if uptime_seconds <= 0:
            repeater_handler = getattr(self.daemon_instance, "repeater_handler", None)
            if repeater_handler and getattr(repeater_handler, "start_time", None):
                uptime_seconds = max(0, int(time.time() - repeater_handler.start_time))

        tx_total = int(stats.get("sent_flood_count", 0)) + int(stats.get("sent_direct_count", 0))
        if tx_total <= 0:
            tx_total = int(stats.get("forwarded_count", 0))

        command_results = await self._get_pending_command_results()
        settings_snapshot = self._build_settings_snapshot()
        location = self._extract_location_from_settings(settings_snapshot)

        return {
            "type": "inform",
            "version": 1,
            "node_name": node_name,
            "pubkey": f"0x{public_key}",
            "software_version": __version__,
            "state": self.config.get("repeater", {}).get("mode", "forward"),
            "location": location,
            "uptime_seconds": uptime_seconds,
            "config_hash": self._compute_config_hash(self.config),
            "cert_expires_at": self._cert_expires_at,
            "system": self._collect_system_stats(),
            "radio": {
                "frequency": int(self.config.get("radio", {}).get("frequency", 0)),
                "spreading_factor": int(self.config.get("radio", {}).get("spreading_factor", 7)),
                "bandwidth": int(self.config.get("radio", {}).get("bandwidth", 0)),
                "tx_power": int(self.config.get("radio", {}).get("tx_power", 0)),
                "noise_floor_dbm": stats.get("noise_floor_dbm"),
                "mode": self.config.get("repeater", {}).get("mode", "forward"),
            },
            "counters": {
                "rx_total": int(stats.get("rx_count", 0)),
                "tx_total": max(0, tx_total),
                "forwarded": int(stats.get("forwarded_count", 0)),
                "dropped": int(stats.get("dropped_count", 0)),
                "duplicates": int(stats.get("flood_dup_count", 0))
                + int(stats.get("direct_dup_count", 0)),
                "airtime_percent": float(stats.get("utilization_percent", 0.0)),
            },
            "settings": settings_snapshot,
            "command_results": command_results,
        }

    def _build_settings_snapshot(self) -> Dict[str, Any]:
        normalized = self._normalize_for_hash(self.config)
        sanitized = self._sanitize_settings_for_export(normalized)
        if isinstance(sanitized, dict):
            return sanitized
        return {}

    def _sanitize_settings_for_export(self, value: Any, key_name: Optional[str] = None) -> Any:
        if isinstance(value, dict):
            output: Dict[str, Any] = {}
            for child_key, child_value in value.items():
                if self._is_sensitive_key(child_key):
                    output[child_key] = "<redacted>"
                    continue
                output[child_key] = self._sanitize_settings_for_export(child_value, child_key)
            return output
        if isinstance(value, list):
            return [self._sanitize_settings_for_export(item, key_name) for item in value]
        return value

    @staticmethod
    def _is_sensitive_key(key: str) -> bool:
        lowered = str(key).lower()
        if any(exception in lowered for exception in _SENSITIVE_KEY_EXCEPTIONS):
            return False
        return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)

    @staticmethod
    def _normalize_location(value: Any) -> Optional[str]:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            parts = [part.strip() for part in text.split(",")]
            if len(parts) != 2:
                return None
            try:
                lat = float(parts[0])
                lng = float(parts[1])
            except ValueError:
                return None
        elif isinstance(value, dict):
            lat = value.get("lat", value.get("latitude"))
            lng = value.get("lng", value.get("longitude"))
            try:
                if lat is None or lng is None:
                    return None
                lat = float(lat)
                lng = float(lng)
            except (TypeError, ValueError):
                return None
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            try:
                lat = float(value[0])
                lng = float(value[1])
            except (TypeError, ValueError):
                return None
        else:
            return None

        if lat < -90 or lat > 90 or lng < -180 or lng > 180:
            return None
        return f"{lat:.6f},{lng:.6f}"

    def _extract_location_from_settings(self, settings: Dict[str, Any]) -> Optional[str]:
        repeater_settings = settings.get("repeater")
        repeater_dict = repeater_settings if isinstance(repeater_settings, dict) else {}
        candidates = [
            settings.get("location"),
            repeater_dict.get("location"),
            settings.get("gps"),
            repeater_dict.get("gps"),
            {
                "lat": repeater_dict.get("latitude"),
                "lng": repeater_dict.get("longitude"),
            },
        ]
        for candidate in candidates:
            location = self._normalize_location(candidate)
            if location:
                return location
        return None

    def _collect_system_stats(self) -> Dict[str, Any]:
        temperature_c = None
        try:
            temperatures = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
            if temperatures:
                for values in temperatures.values():
                    if values:
                        temperature_c = values[0].current
                        break
        except Exception:
            temperature_c = None

        load_avg_1m = None
        try:
            if hasattr(os, "getloadavg"):
                load_avg_1m = float(os.getloadavg()[0])
        except Exception:
            load_avg_1m = None

        return {
            "cpu_percent": float(psutil.cpu_percent(interval=None)),
            "memory_percent": float(psutil.virtual_memory().percent),
            "disk_percent": float(psutil.disk_usage("/").percent),
            "temperature_c": temperature_c,
            "load_avg_1m": load_avg_1m,
        }

    async def _post_inform(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._post_inform_sync, payload)

    def _post_inform_sync(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/inform"
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        body = json.dumps(payload).encode("utf-8")
        req = request.Request(url=url, data=body, method="POST", headers=headers)
        ssl_context = self._build_ssl_context(url)

        try:
            with request.urlopen(
                req,
                timeout=self.request_timeout_seconds,
                context=ssl_context,
            ) as response:
                response_bytes = response.read()
        except error.HTTPError as exc:
            details = ""
            try:
                details = exc.read().decode("utf-8")
            except Exception:
                details = str(exc)
            raise RuntimeError(f"HTTP {exc.code}: {details}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Connection error: {exc}") from exc

        if not response_bytes:
            return {"type": "noop", "interval": self.inform_interval_seconds}

        try:
            response_payload = json.loads(response_bytes.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("Invalid JSON response from Glass backend") from exc

        if not isinstance(response_payload, dict):
            raise RuntimeError("Invalid response payload from Glass backend")
        return response_payload

    def _build_ssl_context(self, url: str) -> Optional[ssl.SSLContext]:
        if not str(url).startswith("https"):
            return None

        if self.verify_tls:
            if self.ca_cert_path:
                ca_path = self._require_ssl_file(self.ca_cert_path, "ca_cert_path")
                context = ssl.create_default_context(cafile=ca_path)
            else:
                context = ssl.create_default_context()
        else:
            context = ssl._create_unverified_context()

        if self.client_cert_path or self.client_key_path:
            cert_path = self._require_ssl_file(self.client_cert_path, "client_cert_path")
            key_path = self._require_ssl_file(self.client_key_path, "client_key_path")
            context.load_cert_chain(certfile=cert_path, keyfile=key_path)

        return context

    @staticmethod
    def _require_ssl_file(path_value: Optional[str], field_name: str) -> str:
        if not path_value or not str(path_value).strip():
            raise RuntimeError(f"Missing {field_name} for Glass TLS configuration")
        normalized = str(path_value).strip()
        if not Path(normalized).exists():
            raise RuntimeError(f"Configured {field_name} does not exist: {normalized}")
        return normalized

    async def _handle_command_response(self, response: Dict[str, Any]) -> None:
        command_id = str(response.get("command_id", "")).strip()
        action = str(response.get("action", "")).strip()
        params = response.get("params", {})

        if not command_id or not action:
            logger.warning("Glass command response missing command_id or action")
            return

        success = False
        message = "Action failed"
        details: Optional[Dict[str, Any]] = None
        try:
            success, message, details = await self._execute_command_action(action, params)
        except Exception as exc:
            success = False
            message = f"Exception executing action: {exc}"
            details = None

        await self._queue_command_result(
            command_id=command_id,
            status="success" if success else "failed",
            message=message,
            details=details,
        )

    async def _execute_command_action(
        self,
        action: str,
        params: Any,
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        params = params if isinstance(params, dict) else {}

        if action == "restart_service":
            success, message = restart_service()
            return success, message, None

        if action == "send_advert":
            if not self.daemon_instance or not hasattr(self.daemon_instance, "send_advert"):
                return False, "send_advert unavailable", None
            success = await self.daemon_instance.send_advert()
            return success, "Advert sent" if success else "Failed to send advert", None

        if action == "set_mode":
            mode = str(params.get("mode", "")).strip()
            if mode not in ("forward", "monitor", "no_tx"):
                return False, "Invalid mode parameter", None
            success, message = self._apply_config_update(
                {"repeater": {"mode": mode}},
                merge_mode="patch",
            )
            return success, message, None

        if action == "set_inform_interval":
            interval = params.get("interval_seconds", params.get("interval"))
            if not isinstance(interval, int):
                return False, "interval_seconds must be an integer", None
            interval = self._clamp_interval(interval)
            self.inform_interval_seconds = interval
            success, message = self._apply_config_update(
                {"glass": {"inform_interval_seconds": interval}},
                merge_mode="patch",
            )
            return success, message, None
        if action == "rotate_cert":
            return True, "Certificate rotation requested", None

        if action == "config_update":
            config_patch = params.get("config", params)
            merge_mode = str(params.get("merge_mode", "patch"))
            success, message = self._apply_config_update(config_patch, merge_mode=merge_mode)
            return success, message, None

        if action == "transport_keys_sync":
            success, message, details = self._apply_transport_keys_sync(params)
            return success, message, details

        if action == "set_radio":
            radio_values = params.get("radio", params)
            if not isinstance(radio_values, dict):
                return False, "radio settings must be an object", None
            success, message = self._apply_config_update({"radio": radio_values}, merge_mode="patch")
            return success, message, None

        if action == "run_diagnostic":
            stats = self.daemon_instance.get_stats() if self.daemon_instance else {}
            return True, (
                f"rx={int(stats.get('rx_count', 0))}, "
                f"tx={int(stats.get('forwarded_count', 0))}, "
                f"dropped={int(stats.get('dropped_count', 0))}"
            ), None

        if action == "export_config":
            normalized_config = self._normalize_for_hash(self.config)
            return (
                True,
                "Configuration exported",
                {
                    "config": normalized_config,
                    "config_hash": self._compute_config_hash(self.config),
                },
            )

        return False, f"Unsupported action: {action}", None

    def _apply_config_update(self, updates: Any, merge_mode: str = "patch") -> Tuple[bool, str]:
        if not isinstance(updates, dict) or not updates:
            return False, "Config update payload must be a non-empty object"
        merge_mode = merge_mode.lower().strip()

        if merge_mode not in ("patch", "replace"):
            return False, f"Unsupported merge_mode: {merge_mode}"
        updates_to_apply = dict(updates)
        managed_updates = updates_to_apply.pop("glass_managed", None)
        if managed_updates is not None:
            managed_ok, managed_message = self._save_managed_settings(
                managed_updates,
                replace=merge_mode == "replace",
            )
            if not managed_ok:
                return False, managed_message
            self._reload_runtime_settings()
            self._sync_mqtt_publisher()

        if not updates_to_apply:
            return True, "Managed settings updated"

        sections = list(updates_to_apply.keys())

        if merge_mode == "replace":
            for section, value in updates_to_apply.items():
                self.config[section] = value
            if self.config_manager:
                saved = self.config_manager.save_to_file()
                live_updated = self.config_manager.live_update_daemon(sections)
                return (
                    bool(saved and live_updated),
                    "Config replaced" if saved and live_updated else "Failed to persist replace update",
                )
            return True, "Config replaced"

        # patch mode
        if self.config_manager:
            result = self.config_manager.update_and_save(
                updates=updates_to_apply,
                live_update=True,
                live_update_sections=sections,
            )
            if result.get("success"):
                if "glass" in sections:
                    self._reload_runtime_settings()
                    self._sync_mqtt_publisher()
                return True, "Config patched"
            return False, str(result.get("error", "Failed to patch config"))
        self._deep_merge(self.config, updates_to_apply)
        if "glass" in sections:
            self._reload_runtime_settings()
            self._sync_mqtt_publisher()
        return True, "Config patched"

    def _get_sqlite_handler(self):
        if not self.daemon_instance:
            return None
        repeater_handler = getattr(self.daemon_instance, "repeater_handler", None)
        storage = getattr(repeater_handler, "storage", None)
        return getattr(storage, "sqlite_handler", None)

    def _apply_transport_keys_sync(
        self,
        params: Dict[str, Any],
    ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        if not isinstance(params, dict):
            return False, "transport_keys_sync params must be an object", None
        entries = params.get("transport_keys")
        if not isinstance(entries, list):
            return False, "transport_keys_sync payload must include a transport_keys list", None
        sqlite_handler = self._get_sqlite_handler()
        if sqlite_handler is None:
            return False, "SQLite handler unavailable for transport key sync", None
        try:
            result = sqlite_handler.sync_transport_keys(entries)
        except Exception as exc:
            return False, f"Transport key sync failed: {exc}", None
        payload_hash = params.get("payload_hash")
        details: Dict[str, Any] = {
            "applied_nodes": int(result.get("applied_nodes", 0)),
            "generated_keys": int(result.get("generated_keys", 0)),
        }
        if isinstance(payload_hash, str) and payload_hash.strip():
            details["payload_hash"] = payload_hash
        return True, f"Applied transport key sync ({details['applied_nodes']} nodes)", details

    def _apply_cert_renewal(self, response: Dict[str, Any]) -> Tuple[bool, str]:
        client_cert = response.get("client_cert")
        client_key = response.get("client_key")
        ca_cert = response.get("ca_cert")

        if not all(isinstance(item, str) and item.strip() for item in (client_cert, client_key, ca_cert)):
            return False, "Missing certificate payload values"

        cert_dir = Path(self.cert_store_dir)
        cert_dir.mkdir(parents=True, exist_ok=True)

        client_cert_path = cert_dir / "glass-client.crt"
        client_key_path = cert_dir / "glass-client.key"
        ca_cert_path = cert_dir / "glass-ca.crt"

        client_cert_path.write_text(client_cert, encoding="utf-8")
        client_key_path.write_text(client_key, encoding="utf-8")
        ca_cert_path.write_text(ca_cert, encoding="utf-8")
        os.chmod(client_key_path, 0o600)

        return self._apply_config_update(
            {
                "glass": {
                    "client_cert_path": str(client_cert_path),
                    "client_key_path": str(client_key_path),
                    "ca_cert_path": str(ca_cert_path),
                }
            },
            merge_mode="patch",
        )

    async def _get_pending_command_results(self) -> List[Dict[str, Any]]:
        async with self._pending_lock:
            return list(self._pending_command_results)

    async def _queue_command_result(
        self,
        command_id: str,
        status: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        result = {
            "command_id": command_id,
            "status": status,
            "message": message[:1024] if message else "",
            "completed_at": completed_at,
        }
        if details:
            result["details"] = details
        async with self._pending_lock:
            self._pending_command_results.append(result)

    def publish_telemetry(self, record_type: str, record: Dict[str, Any]) -> None:
        if not self.enabled or not self.mqtt_enabled or not self._mqtt_ready:
            return
        if not self._mqtt_client:
            return

        node_name = self.config.get("repeater", {}).get("node_name", "unknown-repeater")
        event_type = "event"
        event_name: Optional[str] = record_type
        if record_type in ("packet", "advert"):
            event_type = record_type
            event_name = None

        topic = self._mqtt_topic_for_record(node_name=node_name, record_type=record_type)
        timestamp = self._to_rfc3339_timestamp(record.get("timestamp"))
        payload = self._normalize_for_hash(record)

        envelope: Dict[str, Any] = {
            "version": 1,
            "type": event_type,
            "topic": topic,
            "node_name": node_name,
            "timestamp": timestamp,
            "payload": payload,
        }
        if event_type == "event" and event_name:
            envelope["event_name"] = event_name

        try:
            message = json.dumps(envelope, separators=(",", ":"), sort_keys=True, default=str)
            self._mqtt_client.publish(topic, message, qos=0, retain=False)
        except Exception as exc:
            logger.debug("Failed publishing Glass telemetry MQTT message: %s", exc)

    def _mqtt_topic_for_record(self, *, node_name: str, record_type: str) -> str:
        base = self.mqtt_base_topic.strip("/") or "glass"
        if record_type in ("packet", "advert"):
            return f"{base}/{node_name}/{record_type}"
        return f"{base}/{node_name}/event/{record_type}"

    def _to_rfc3339_timestamp(self, value: Any) -> str:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(float(value), timezone.utc)
        elif isinstance(value, str):
            normalized = value.strip()
            if normalized.endswith("Z"):
                return normalized
            try:
                dt = datetime.fromisoformat(normalized)
            except ValueError:
                dt = datetime.now(timezone.utc)
        elif isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.now(timezone.utc)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")

    def _init_mqtt_publisher(self) -> None:
        if not self.mqtt_enabled:
            self._close_mqtt_publisher()
            return
        if mqtt is None:
            logger.warning("Glass MQTT telemetry publishing enabled but paho-mqtt is unavailable")
            self._close_mqtt_publisher()
            return
        if self._mqtt_client is not None:
            return

        try:
            client = mqtt.Client()
            if self.mqtt_username:
                client.username_pw_set(self.mqtt_username, self.mqtt_password)
            if self.mqtt_tls_enabled:
                ca_certs = self._require_ssl_file(self.ca_cert_path, "ca_cert_path") if self.ca_cert_path else None
                certfile = None
                keyfile = None
                if self.client_cert_path or self.client_key_path:
                    certfile = self._require_ssl_file(self.client_cert_path, "client_cert_path")
                    keyfile = self._require_ssl_file(self.client_key_path, "client_key_path")
                cert_reqs = ssl.CERT_REQUIRED if self.verify_tls else ssl.CERT_NONE
                client.tls_set(
                    ca_certs=ca_certs,
                    certfile=certfile,
                    keyfile=keyfile,
                    cert_reqs=cert_reqs,
                    tls_version=ssl.PROTOCOL_TLS_CLIENT,
                )
                if not self.verify_tls:
                    client.tls_insecure_set(True)
            client.on_connect = self._on_mqtt_connect
            client.on_disconnect = self._on_mqtt_disconnect
            client.connect_async(self.mqtt_broker_host, self.mqtt_broker_port, 60)
            client.loop_start()
            self._mqtt_client = client
            self._mqtt_runtime_signature = self._current_mqtt_signature()
            logger.info(
                "Glass MQTT telemetry publisher started (%s:%s, base_topic=%s)",
                self.mqtt_broker_host,
                self.mqtt_broker_port,
                self.mqtt_base_topic,
            )
        except Exception as exc:
            self._mqtt_client = None
            self._mqtt_ready = False
            self._mqtt_runtime_signature = None
            logger.warning("Failed to start Glass MQTT telemetry publisher: %s", exc)

    def _close_mqtt_publisher(self) -> None:
        client = self._mqtt_client
        self._mqtt_client = None
        self._mqtt_ready = False
        self._mqtt_runtime_signature = None
        if client is None:
            return
        try:
            client.loop_stop()
            client.disconnect()
        except Exception as exc:
            logger.debug("Error stopping Glass MQTT telemetry publisher: %s", exc)

    def _on_mqtt_connect(self, _client, _userdata, _flags, reason_code, _properties=None) -> None:
        rc = getattr(reason_code, "value", reason_code)
        if rc == 0:
            self._mqtt_ready = True
            logger.info("Glass MQTT telemetry publisher connected")
            return
        self._mqtt_ready = False
        logger.warning("Glass MQTT telemetry publisher connect failed (code=%s)", rc)

    def _on_mqtt_disconnect(self, _client, _userdata, reason_code, _properties=None) -> None:
        self._mqtt_ready = False
        rc = getattr(reason_code, "value", reason_code)
        if rc:
            logger.warning("Glass MQTT telemetry publisher disconnected (code=%s)", rc)

    def _current_mqtt_signature(
        self,
    ) -> Tuple[str, int, str, bool, bool, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
        return (
            self.mqtt_broker_host,
            self.mqtt_broker_port,
            self.mqtt_base_topic,
            self.mqtt_tls_enabled,
            self.verify_tls,
            self.ca_cert_path,
            self.client_cert_path,
            self.client_key_path,
            self.mqtt_username,
            self.mqtt_password,
        )

    def _sync_mqtt_publisher(self) -> None:
        if not self.enabled or not self.mqtt_enabled:
            self._close_mqtt_publisher()
            return
        if mqtt is None:
            self._close_mqtt_publisher()
            return

        signature = self._current_mqtt_signature()
        if self._mqtt_client is None:
            self._init_mqtt_publisher()
            return
        if self._mqtt_runtime_signature != signature:
            self._close_mqtt_publisher()
            self._init_mqtt_publisher()

    @staticmethod
    def _deep_merge(target: Dict[str, Any], source: Dict[str, Any]) -> None:
        for key, value in source.items():
            if (
                isinstance(value, dict)
                and isinstance(target.get(key), dict)
            ):
                GlassHandler._deep_merge(target[key], value)
            else:
                target[key] = value

    @staticmethod
    def _normalize_for_hash(value: Any) -> Any:
        if isinstance(value, bytes):
            return value.hex()
        if isinstance(value, dict):
            return {k: GlassHandler._normalize_for_hash(v) for k, v in value.items()}
        if isinstance(value, list):
            return [GlassHandler._normalize_for_hash(v) for v in value]
        return value

    @staticmethod
    def _compute_config_hash(config: dict) -> str:
        normalized = GlassHandler._normalize_for_hash(config)
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        return f"sha256:{digest}"

    @staticmethod
    def _clamp_interval(interval_seconds: int) -> int:
        if interval_seconds < 5:
            return 5
        if interval_seconds > 3600:
            return 3600
        return interval_seconds
