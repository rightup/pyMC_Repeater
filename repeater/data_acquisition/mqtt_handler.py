import base64
import binascii
import json
import logging
import string
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import paho.mqtt.client as mqtt
from nacl.signing import SigningKey

# Try to import datetime.UTC (Python 3.11+) otherwise fallback to timezone.utc
try:
    from datetime import UTC
except Exception:
    from datetime import timezone
    UTC = timezone.utc

from repeater import __version__, config
from repeater.presets import get_preset

# Try to import paho-mqtt error code mappings
try:
    from paho.mqtt.reasoncodes import ReasonCode

    HAS_REASON_CODES = True
except ImportError:
    ReasonCode = None
    HAS_REASON_CODES = False

logger = logging.getLogger("MQTTHandler")


# --------------------------------------------------------------------
# Helper: Base64URL without padding
# --------------------------------------------------------------------
def b64url(x: bytes) -> str:
    return base64.urlsafe_b64encode(x).rstrip(b"=").decode()


def _summarize_payload_for_log(payload: Any, message: Optional[str] = None) -> str:
    """Return a compact single-line payload summary for debug logging."""
    if message is None:
        try:
            message = json.dumps(payload)
        except Exception:
            message = str(payload)

    if isinstance(payload, list):
        item_bits = []
        if payload and isinstance(payload[0], dict):
            item_bits.append(_summarize_payload_for_log(payload[0]))
        return f"items={len(payload)}, bytes={len(message.encode('utf-8'))}" + (
            f", first={{ {item_bits[0]} }}" if item_bits else ""
        )

    if not isinstance(payload, dict):
        return f"type={type(payload).__name__}, bytes={len(message.encode('utf-8'))}"

    summary_fields = []
    for key in ("type", "packet_type", "route", "origin", "status", "hash", "RSSI", "SNR"):
        value = payload.get(key)
        if value not in (None, ""):
            summary_fields.append(f"{key}={value}")

    if "len" in payload:
        summary_fields.append(f"len={payload['len']}")
    if "payload_len" in payload:
        summary_fields.append(f"payload_len={payload['payload_len']}")
    if "raw" in payload:
        summary_fields.append(f"raw_bytes={len(str(payload['raw'])) // 2}")

    if not summary_fields:
        keys = ",".join(sorted(payload.keys())[:6])
        if len(payload) > 6:
            keys += ",..."
        summary_fields.append(f"keys={keys}")

    summary_fields.append(f"bytes={len(message.encode('utf-8'))}")
    return ", ".join(summary_fields)


def _truncate_middle(text: str, prefix: int = 24, suffix: int = 16) -> str:
    """Truncate long strings by keeping the beginning and end visible."""
    if len(text) <= prefix + suffix + 5:
        return text
    return f"{text[:prefix]} ... {text[-suffix:]}"


# --------------------------------------------------------------------
# Format family
# --------------------------------------------------------------------
# Format values that belong to the MeshCoreToMQTT (MC2MQTT) protocol family.
# All MC2MQTT brokers share the topic structure: meshcore/{IATA}/{PUBLIC_KEY}/...
# Adding a new MC2MQTT-compatible platform = add its format value here AND
# drop a matching presets/<name>.yaml. The legacy "mqtt" format is NOT in this
# tuple - it speaks the operator-defined custom topic structure instead.
MC2MQTT_FORMATS = ("meshcoretomqtt", "letsmesh", "waev")


# --------------------------------------------------------------------
# Preset expansion helpers (used during broker-list assembly below).
#
# A user's brokers: list may contain a mix of:
#   * preset references:  {preset: <name>}  -> expands to N broker dicts
#   * full broker dicts:   {name, host, port, ...}  -> used as-is
#
# Expansion is two passes so the rules are obvious and order-independent
# in spirit, but order-dependent in practice (later wins on name collision):
#   Pass 1 - replace every {preset: ...} entry in place with the bundled
#            broker list. Unknown preset is dropped with a warning.
#   Pass 2 - walk left-to-right; if an entry's name already appeared
#            earlier, shallow-merge the LATER entry onto the EARLIER one
#            and drop the duplicate. Place override entries AFTER preset
#            entries to make them win.
# --------------------------------------------------------------------
def _expand_preset_entries(brokers: List[dict]) -> List[dict]:
    """Pass 1: replace every {preset: <name>} entry with the preset's brokers."""
    expanded: List[dict] = []
    for entry in brokers:
        if isinstance(entry, dict) and "preset" in entry and "name" not in entry:
            preset_name = entry["preset"]
            preset = get_preset(preset_name)
            preset_brokers = preset.get("brokers", []) if preset else []
            if not preset_brokers:
                logger.warning(f"Unknown or empty broker preset '{preset_name}' - skipping")
                continue
            logger.info(f"Expanded preset '{preset_name}' into {len(preset_brokers)} broker(s)")
            expanded.extend(preset_brokers)
        else:
            expanded.append(entry)
    return expanded


def _merge_overrides_by_name(brokers: List[dict]) -> List[dict]:
    """Pass 2: collapse duplicates by name; the LATER entry's fields win."""
    by_index: Dict[str, int] = {}
    result: List[dict] = []
    for entry in brokers:
        if not isinstance(entry, dict):
            result.append(entry)
            continue
        name = entry.get("name")
        if name and name in by_index:
            # Shallow-merge: later entry's keys overwrite earlier entry's keys.
            result[by_index[name]] = {**result[by_index[name]], **entry}
        else:
            if name:
                by_index[name] = len(result)
            result.append(entry)
    return result


# ====================================================================
# Single Broker Connection Manager
# ====================================================================
class _BrokerConnection:
    """
    Manages a single MQTT broker connection with independent lifecycle.
    Internal class - not exposed publicly.
    """

    def __init__(
        self,
        broker: dict,
        local_identity,
        public_key: str,
        iata_code: str,
        jwt_expiry_minutes: int,
        email: str,
        owner: str,
        broker_index: int,
        node_name: str,
        on_connect_callback: Optional[Callable] = None,
        on_disconnect_callback: Optional[Callable] = None
    ):
        self.broker = broker
        self.local_identity = local_identity
        self.public_key = public_key.upper()
        self.iata_code = iata_code
        self.jwt_expiry_minutes = jwt_expiry_minutes
        self.email = email
        self.owner = owner
        self.node_name = node_name
        self.broker_index = broker_index
        self._on_connect_callback = on_connect_callback
        self._on_disconnect_callback = on_disconnect_callback
        self._connect_time = None
        self._running = False
        self._reconnect_attempts = 0
        self._reconnect_timer = None
        self._max_reconnect_delay = 300  # 5 minutes max
        self._keepalive = broker.get("keepalive", 30)  # default tighter than paho's 60s to beat NAT/proxy timeouts
        self._jwt_refresh_timer = None
        self._shutdown_requested = False
        self._last_jwt_claims = None
        self.transport = broker.get('transport', 'websockets')
        
        self.use_jwt_auth = broker.get('use_jwt_auth', False)
        self.username = broker.get('username', None)
        self.password = broker.get('password', None)

        self.format=broker.get("format", "letsmesh") 
        self.tls=broker.get("tls", {
            "enabled": False,
            "insecure": False,
        })
        
        client_id = f"meshcore_{self.public_key}_{broker['host']}_{self.format}"
        self.client = mqtt.Client(client_id=client_id, transport=self.transport)
        if hasattr(self.client, "on_pre_connect"):
            self.client.on_pre_connect = self._on_pre_connect
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

        # If None, will be use defaults depending on the format value
        self.base_topic=broker.get("base_topic", None)

        self.enabled = broker.get("enabled", False)
        self.retain_status = broker.get("retain_status", False)

        self._tls_verified = False

        if self.base_topic is None:
            if self.format == "mqtt":
                # Custom MQTT family: operator-defined topic, not meshcore convention.
                self.base_topic = f"meshcore/repeater/{self.node_name}"
            elif self.format in MC2MQTT_FORMATS:
                # MC2MQTT family: canonical meshcoretomqtt topic structure.
                self.base_topic = f"meshcore/{self.iata_code}/{self.public_key}"
            else:
                logger.warning(f"Unknown broker format '{self.format}' for {self.broker['name']}, defaulting to MC2MQTT topic structure")
                self.base_topic = f"meshcore/{self.iata_code}/{self.public_key}"
        
        from pymc_core.protocol.utils import PAYLOAD_TYPES
        
        disallowed_types = broker.get("disallowed_packet_types", [])
        type_name_map = {name: code for code, name in PAYLOAD_TYPES.items()}
        
        self.disallowed_types = [type_name_map.get(name.upper(), None) for name in disallowed_types]
        self.disallowed_types = [val for val in self.disallowed_types if val is not None]  # Filter out invalid names
    

    def _generate_jwt(self) -> str:
        """Generate MeshCore-style Ed25519 JWT token"""
        now = datetime.now(UTC)

        header = {"alg": "Ed25519", "typ": "JWT"}

        payload = {
            "publicKey": self.public_key.upper(),
            "aud": self.broker["audience"],
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=self.jwt_expiry_minutes)).timestamp()),
        }

        if "audience" in self.broker:
            payload["aud"] = self.broker["audience"]

        # Only include email/owner for verified TLS connections
        if self.tls and self.tls.get("enabled", False) and self._tls_verified and (self.email or self.owner):
            payload["email"] = self.email
            payload["owner"] = self.owner
        else:
            payload["email"] = ""
            payload["owner"] = ""

        self._last_jwt_claims = {
            "aud": payload.get("aud"),
            "iat": payload.get("iat"),
            "exp": payload.get("exp"),
            "public_key_suffix": self.public_key[-12:],
        }

        # Encode header and payload (compact JSON - no spaces)
        header_b64 = b64url(json.dumps(header, separators=(",", ":")).encode())
        payload_b64 = b64url(json.dumps(payload, separators=(",", ":")).encode())

        signing_input = f"{header_b64}.{payload_b64}".encode()

        # Sign using LocalIdentity (supports both standard and firmware keys)
        try:
            signature = self.local_identity.sign(signing_input)
        except Exception as e:
            logger.error(f"JWT signing failed for {self.broker['name']}: {e}")
            logger.error(f"  - public_key: {self.public_key}")
            logger.error(f"  - signing_input length: {len(signing_input)}")
            raise

        signature_hex = binascii.hexlify(signature).decode()
        token = f"{header_b64}.{payload_b64}.{signature_hex}"

        logger.debug(f"JWT token generated for {self.broker['name']}: {token[:50]}...")

        return token

    def _on_connect(self, client, userdata, flags, rc):
        """MQTT connection callback"""
        if rc == 0:
            logger.info(f"Connected to {self.broker['name']}")
            self._running = True
            self._reconnect_attempts = 0  # Reset counter on success
            # Successful connect can race with a previously scheduled timer; cancel it.
            if self._reconnect_timer:
                self._reconnect_timer.cancel()
                self._reconnect_timer = None
            if self.use_jwt_auth:
                self._schedule_jwt_refresh()  # Schedule proactive JWT refresh
            if self._on_connect_callback:
                self._on_connect_callback(self.broker["name"])
        else:
            error_msg = get_mqtt_error_message(rc, is_disconnect=False)
            logger.error(f"Failed to connect to {self.broker['name']}: {error_msg}")
            self._schedule_reconnect(reason=error_msg)

    def _on_pre_connect(self, client, userdata):
        """Refresh credentials before each connect/reconnect attempt."""
        if self._shutdown_requested:
            return
        if self.use_jwt_auth:
            self._set_credentials()

    def _on_disconnect(self, client, userdata, rc):
        """MQTT disconnection callback"""
        was_running = self._running
        self._running = False

        if self._shutdown_requested:
            logger.info(f"Clean disconnect from {self.broker['name']}")
            if self._on_disconnect_callback:
                self._on_disconnect_callback(self.broker["name"])
            return

        if rc != 0:  # Unexpected disconnect
            error_msg = get_mqtt_error_message(rc, is_disconnect=True)
            if was_running:
                logger.warning(f"Disconnected from {self.broker['name']} (rc={rc}): {error_msg}")
            else:
                logger.debug(
                    f"Duplicate disconnect callback from {self.broker['name']} while already disconnected "
                    f"(rc={rc}): {error_msg}"
                )
            if was_running:  # Only reconnect if we were intentionally connected
                self._schedule_reconnect(reason=error_msg)
        else:
            logger.info(f"Clean disconnect from {self.broker['name']}")

        if self._on_disconnect_callback:
            self._on_disconnect_callback(self.broker["name"])

    def _schedule_reconnect(self, reason: str = "connection lost"):
        """Schedule reconnection with exponential backoff"""
        if self._shutdown_requested:
            return

        if self._reconnect_timer:
            self._reconnect_timer.cancel()

        # Exponential backoff: 5s, 10s, 20s, 40s, 80s, up to max
        delay = min(5 * (2**self._reconnect_attempts), self._max_reconnect_delay)
        self._reconnect_attempts += 1

        logger.info(
            f"Scheduling reconnect to {self.broker['name']} in {delay}s (attempt {self._reconnect_attempts}, reason: {reason})"
        )
        self._reconnect_timer = threading.Timer(delay, lambda: self._attempt_reconnect(reason))
        self._reconnect_timer.daemon = True
        self._reconnect_timer.start()

    def _attempt_reconnect(self, reason: str = "connection lost"):
        """Attempt to reconnect to broker with fresh JWT"""
        if self._shutdown_requested:
            return

        # Timer has fired; clear handle so state reflects reality.
        self._reconnect_timer = None

        if self._running:
            logger.debug(f"Skipping reconnect to {self.broker['name']} - already connected")
            return

        try:
            logger.info(f"Attempting reconnection to {self.broker['name']} (reason: {reason})...")

            # Stop the loop if it's still running (websocket mode requires clean restart)
            try:
                self.client.loop_stop()
            except:
                pass

            self._set_credentials()

            # Reconnect and restart loop
            self.client.connect(self.broker["host"], self.broker["port"], keepalive=self._keepalive)
            self.client.loop_start()
            self._loop_running = True
        except Exception as e:
            logger.error(f"Reconnection failed for {self.broker['name']}: {e}")
            self._schedule_reconnect()  # Try again later

    def _set_credentials(self):
        """Set credentials before connecting (CONNECT handshake only)"""
        try:
            if self.use_jwt_auth:
                logger.debug(f"Generating JWT credentials for {self.broker['name']}...")
                token = self._generate_jwt()
                username = f"v1_{self.public_key}"
                self.client.username_pw_set(username=username, password=token)
                logger.debug(
                    f"Credentials set for {self.broker['name']}: "
                    f"user=v1_{self.public_key[:8]}...{self.public_key[-8:]}"
                )
            elif self.username and self.password:
                logger.info(f"Using provided credentials for {self.broker['name']} (username: {self.username})")
                self.client.username_pw_set(username=self.username, password=self.password)
            else:
                logger.info(f"No credentials set for {self.broker['name']} (JWT auth disabled and no username/password provided)")
            
            self._connect_time = datetime.now(UTC)
            
        except Exception as e:
            logger.error(f"Failed to set JWT credentials for {self.broker['name']}: {e}")
            raise

    def connect(self):
        """Establish connection to broker"""
        self._shutdown_requested = False

        # Conditional TLS setup
        if self.enabled == False:
            logger.info(f"Connection to {self.broker['name']} is disabled in configuration")
            return

        if self.transport == "websockets":
                protocol = "ws"
        elif self.transport == "tcp":
            protocol = "mqtt"
        else:
            raise ValueError(f"Invalid transport '{self.transport}' for {self.broker['name']}")
        
        # Setup TLS independent of transport - MQTT over TLS can be used with both websockets and raw TCP
        if self.tls and self.tls.get("enabled", False):
            import ssl
            self.client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
            self.client.tls_insecure_set(self.tls.get("insecure", False))
            self._tls_verified = True

            # Ensure to update the protocol is we're running TLS on websockets
            if( self.transport == "websockets" ):
                protocol = "wss"

        # Set JWT credentials before CONNECT handshake
        self._set_credentials()

        logger.info(
            f"Connecting to {self.broker['name']} "
            f"({protocol}://{self.broker['host']}:{self.broker['port']}) ..."
        )

        self.client.connect(self.broker["host"], self.broker["port"], keepalive=self._keepalive)
        self.client.loop_start()
        self._loop_running = True

    def disconnect(self):
        """Disconnect from broker"""
        self._shutdown_requested = False
        self._running = False
        self._loop_running = False

        # Cancel any pending timers
        if self._reconnect_timer:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None
        if self._jwt_refresh_timer:
            self._jwt_refresh_timer.cancel()
            self._jwt_refresh_timer = None

        self.client.loop_stop()
        self.client.disconnect()
        logger.info(f"Disconnected from {self.broker['name']}")

    def publish(self, subtopic: str, payload: str, retain: bool = False, qos: int = 0):
        """Publish message to broker"""
        
        # Legacy MQTT config uses singular "packet" topic, while LetsMesh uses "packets". Handle this for compatibility.
        if self.format == "mqtt" and subtopic == "packets":
            subtopic = "packet"
        
        if(subtopic == "status"): # Override the status topic retain and qos settings based on broker configuration
            retain = self.retain_status
            qos = 1 if self.retain_status else 0

        full_topic = f"{self.base_topic}/{subtopic}"
        logger.debug(
            f"Publishing topic='{_truncate_middle(full_topic)}', bytes={len(payload.encode('utf-8'))}, "
            f"running={self._running}, retain={retain}, qos={qos}"
        )
        if self._running:
            result = self.client.publish(full_topic, payload, retain=retain, qos=qos)
            return result
        else:
            logger.warning(f"Cannot publish to {self.broker['name']} - not connected")
        return None

    def is_enabled(self) -> bool:
        """Check if connection is enabled"""
        return self.enabled

    def is_connected(self) -> bool:
        """Check if connection is active"""
        return self._running

    def has_pending_reconnect(self) -> bool:
        """Check if a reconnection is scheduled"""
        return self._reconnect_timer is not None and self._reconnect_timer.is_alive()

    def should_reconnect_for_token_expiry(self) -> bool:
        """Check if connection should be reconnected due to JWT expiry (at 80% of lifetime)"""
        if not self._connect_time:
            return False
        elapsed = (datetime.now(UTC) - self._connect_time).total_seconds()
        expiry_seconds = self.jwt_expiry_minutes * 60
        # Stagger refresh by 5% per broker to prevent simultaneous disconnects
        # Broker 0: 80%, Broker 1: 85%, Broker 2: 90%, etc.
        stagger_offset = self.broker_index * 0.05
        refresh_threshold = 0.80 + stagger_offset
        return elapsed >= expiry_seconds * refresh_threshold

    def _schedule_jwt_refresh(self):
        """Schedule proactive JWT refresh before token expires"""
        if self._jwt_refresh_timer:
            self._jwt_refresh_timer.cancel()

        expiry_seconds = self.jwt_expiry_minutes * 60
        # Stagger refresh by 5% per broker to prevent simultaneous disconnects
        # Broker 0: 80%, Broker 1: 85%, Broker 2: 90%, etc.
        stagger_offset = self.broker_index * 0.05
        refresh_threshold = 0.80 + stagger_offset
        refresh_delay = expiry_seconds * refresh_threshold

        logger.info(
            f"JWT refresh scheduled for {self.broker['name']} in {refresh_delay:.0f}s "
            f"({refresh_threshold*100:.0f}% of {self.jwt_expiry_minutes}min token lifetime)"
        )
        self._jwt_refresh_timer = threading.Timer(refresh_delay, self.reconnect_for_token_expiry)
        self._jwt_refresh_timer.daemon = True
        self._jwt_refresh_timer.start()

    def reconnect_for_token_expiry(self):
        """Proactively reconnect with new JWT before current one expires"""
        if not self._running:
            return

        logger.info(f"JWT token expiring soon for {self.broker['name']}, refreshing...")
        self._running = False
        self._jwt_refresh_timer = None

        self._schedule_reconnect(reason="JWT token expiry")
        self.client.disconnect()


# ====================================================================
# MeshCore → MQTT Publisher
# ====================================================================
class MeshCoreToMqttPusher:

    def __init__(
        self,
        local_identity,
        config: dict,
        jwt_expiry_minutes: int = 10,
        stats_provider: Optional[Callable[[], dict]] = None,
    ):
        # Store local identity and get public key
        self.local_identity = local_identity
        public_key = local_identity.get_public_key().hex().upper()

        # Extract values from config
        from ..config import get_node_info

        node_info = get_node_info(config)

        self.iata_code = node_info["iata_code"]
        self.email = node_info.get("email", "")
        self.owner = node_info.get("owner", "")
        self.status_interval = node_info["status_interval"]
        self.node_name = node_info["node_name"]
        self.local_identity = local_identity
        self.public_key = public_key
        self.jwt_expiry_minutes = jwt_expiry_minutes
        self.app_version = __version__
        self.radio_config = node_info["radio_config"]
        self.stats_provider = stats_provider
        self._status_task = None
        self._running = False
        self._shutdown_requested = False        
        self._lock = threading.Lock()
        self._connect_timers: List[threading.Timer] = []

        # Initialize brokers list        
        mqtt_brokers_config = config.get("mqtt_brokers", {})
        letsmesh_config = config.get("letsmesh", {})
        mqtt_config = config.get("mqtt", {})

        brokers = []
        if mqtt_brokers_config:
            # Pull in brokers from mqtt_brokers config
            brokers.extend(mqtt_brokers_config.get("brokers", []))

            if letsmesh_config or mqtt_config:
                logger.warning("Multiple MQTT broker configurations found (mqtt_brokers, letsmesh, mqtt). Only mqtt_brokers will be used")

        else:
            if mqtt_config:
                imported_mqtt_config = self.convert_mqtt_to_broker_config(mqtt_config)
                brokers.append(imported_mqtt_config)

            if letsmesh_config:
                imported_letsmesh_configs = self.convert_letsmesh_to_broker_config(letsmesh_config)
                brokers.extend(imported_letsmesh_configs)

        # Expand any {preset: <name>} entries (from user config OR legacy
        # migrators) into concrete broker dicts, then collapse duplicates so
        # later overrides win. Result feeds the validation loop unchanged.
        brokers = _expand_preset_entries(brokers)
        brokers = _merge_overrides_by_name(brokers)

        # Known MC2MQTT hostnames that must never use format: mqtt.
        # If a user's saved config has the wrong format (common after manual editing
        # or an old UI version), auto-correct it so diagnostic topics aren't sent
        # to brokers that will reject them and close the connection (rc=16).
        _MC2MQTT_HOSTS = {
            "letsmesh.net",
            "waev.app",
            "meshcoretomqtt",
        }

        self.brokers = []
        if brokers:
            for broker_config in brokers:
                if all(k in broker_config for k in ["name", "host", "port", "enabled"]):
                    host = broker_config.get("host", "")
                    fmt = broker_config.get("format", "")
                    if fmt == "mqtt" and any(mc2 in host for mc2 in _MC2MQTT_HOSTS):
                        corrected = broker_config.get("name", host)
                        logger.warning(
                            f"Broker '{corrected}' has format=mqtt but host '{host}' is a MC2MQTT "
                            f"endpoint — auto-correcting to format=letsmesh. "
                            f"Update your config.yaml to silence this warning."
                        )
                        broker_config = {**broker_config, "format": "letsmesh"}
                    self.brokers.append(broker_config)
                    logger.info(f"Added broker: {broker_config['name']} (format={broker_config.get('format', 'unknown')})")
                else:
                    logger.warning(f"Skipping invalid broker config: {broker_config}")



        # Create broker connections
        self.connections: List[_BrokerConnection] = []
        for idx, broker in enumerate(self.brokers):
            conn = _BrokerConnection(
                broker=broker,
                local_identity=self.local_identity,
                public_key=self.public_key,
                iata_code=self.iata_code,
                jwt_expiry_minutes=self.jwt_expiry_minutes,
                email=self.email,
                owner=self.owner,
                broker_index=idx,
                node_name=self.node_name,
                on_connect_callback=self._on_broker_connected,
                on_disconnect_callback=self._on_broker_disconnected,
            )
            self.connections.append(conn)

        logger.info(f"Initialized with {len(self.connections)} broker connection(s)")

        # Convert legacy configration to new one
        if not mqtt_brokers_config:
            logger.info("Storing mqtt_brokers config from legacy mqtt/letsmesh configuration")
            mqtt_brokers_config = {
                "iata_code": self.iata_code,
                "status_interval": self.status_interval,
                "owner": self.owner,
                "email": self.email,
                "brokers": brokers
            }

            # Update the configuration with the new configuration
            config["mqtt_brokers"] = mqtt_brokers_config

    def convert_mqtt_to_broker_config(self, mqtt_cfg: dict) -> dict:
        """Convert legacy MQTT config format to internal broker config format"""
        logger.info(f"Imported MQTT broker from 'mqtt' config: {mqtt_cfg['broker']}")
        transport = "websockets" if mqtt_cfg.get("use_websockets", False) else "tcp"
        return {
            "enabled": mqtt_cfg.get("enabled", False),
            "name": mqtt_cfg["broker"],
            "host": mqtt_cfg["broker"],
            "port": mqtt_cfg["port"],
            "use_jwt_auth": False, # The legacy MQTT config does not support JWT auth, so we set this to False
            "username": mqtt_cfg.get("username", None),
            "password": mqtt_cfg.get("password", None),
            "transport": transport,
            "tls": mqtt_cfg.get("tls", None),
            "format": "mqtt",
            "base_topic": mqtt_cfg.get("base_topic", None),
        }

    def convert_letsmesh_to_broker_config(self, letsmesh_cfg: dict) -> List[dict]:
        """Migrate the legacy ``letsmesh:`` config block into preset-style entries.

        Emits a list that the preset-expansion pass will turn into the same
        broker dicts the old hard-coded migrator used to produce. Specifically:

        * ``broker_index == -1``  -> both LetsMesh brokers (the preset default).
        * ``broker_index == 0``   -> Europe only (US disabled via override).
        * ``broker_index == 1``   -> US only (Europe disabled via override).
        * ``additional_brokers``  -> appended verbatim after the preset reference.
        * ``enabled: false``      -> overrides every preset broker as disabled.
        """
        enabled = letsmesh_cfg.get("enabled", False)
        idx = letsmesh_cfg.get("broker_index", -1)

        # Resolve preset broker names so we can build accurate "disable" overrides.
        preset_brokers = get_preset("letsmesh").get("brokers", [])
        eu_name = preset_brokers[0]["name"] if len(preset_brokers) > 0 else "Europe (LetsMesh v1)"
        us_name = preset_brokers[1]["name"] if len(preset_brokers) > 1 else "US West (LetsMesh v1)"

        entries: List[dict] = [{"preset": "letsmesh"}]

        # Honor broker_index by disabling the broker the legacy user did not pick.
        if idx == 0:
            entries.append({"name": us_name, "enabled": False})
        elif idx == 1:
            entries.append({"name": eu_name, "enabled": False})

        # Honor the legacy enabled flag by overriding every preset broker.
        if not enabled:
            for b in preset_brokers:
                entries.append({"name": b["name"], "enabled": False})

        # Append any user-defined additional brokers as full entries.
        for add_broker in letsmesh_cfg.get("additional_brokers", []):
            logger.info(f"Imported additional LetsMesh broker from 'letsmesh' config: {add_broker.get('name')}")
            entries.append({
                "enabled": enabled,
                "name": add_broker["name"],
                "host": add_broker["host"],
                "port": add_broker["port"],
                "audience": add_broker["audience"],
                "use_jwt_auth": add_broker.get("use_jwt_auth", True),
                "transport": add_broker.get("transport", "websockets"),
                "format": "letsmesh",
                "base_topic": None,
                "retain_status": False,
                "tls": {
                    "enabled": add_broker.get("tls", {}).get("enabled", True),
                    "insecure": add_broker.get("tls", {}).get("insecure", False),
                },
            })

        return entries

    def _on_broker_connected(self, broker_name: str):
        """Callback when a broker connects"""
        if self._shutdown_requested:
            return

        # Publish initial status on first connection
        if not self._status_task and self.status_interval > 0:
            self._running = True
            logger.info(f"Publishing initial status for {broker_name}...")
            self.publish_status(
                state="online", origin=self.node_name, radio_config=self.radio_config
            )
            # Start heartbeat thread
            self._status_task = threading.Thread(target=self._status_heartbeat_loop, daemon=True)
            self._status_task.start()
            logger.info(f"Started status heartbeat (interval: {self.status_interval}s)")

    def _on_broker_disconnected(self, broker_name: str):
        """Callback when a broker disconnects"""
        # Check if all connections are down AND none have pending reconnects
        all_down = all(not conn.is_connected() for conn in self.connections)
        any_reconnecting = any(conn.has_pending_reconnect() for conn in self.connections)

        if all_down and not any_reconnecting:
            logger.warning("All broker connections lost with no pending reconnects")
        elif all_down:
            logger.info("All brokers temporarily disconnected, reconnects pending")

    def connect(self):
        """Establish connections to all configured brokers"""
        self._shutdown_requested = False
        self._connect_timers = []

        for idx, conn in enumerate(self.connections):
            try:
                if idx == 0:
                    # Connect first broker immediately
                    conn.connect()
                else:
                    # Stagger additional brokers using background timers
                    delay = idx * 30
                    logger.info(f"Staggering connection to {conn.broker['name']} by {delay}s")
                    timer = threading.Timer(delay, lambda c=conn: self._delayed_connect(c))
                    timer.daemon = True
                    timer.start()
                    self._connect_timers.append(timer)                    
            except Exception as e:
                logger.error(f"Failed to connect to {conn.broker['name']}: {e}")

    def _delayed_connect(self, conn):
        """Connect a broker after a delay (called by timer)"""
        if self._shutdown_requested:
            return

        try:
            conn.connect()
        except Exception as e:
            logger.error(f"Failed to connect to {conn.broker['name']}: {e}")

    def disconnect(self):
        """Disconnect from all brokers"""
        self._shutdown_requested = True

        # Cancel any delayed connect timers first.
        for timer in self._connect_timers:
            try:
                timer.cancel()
            except Exception:
                pass
        self._connect_timers = []

        # Stop the heartbeat loop
        self._running = False

        # Publish offline status before disconnecting
        try:
            self.publish_status(state="offline", origin=self.node_name, radio_config=self.radio_config)
        except Exception:
            pass

        # Disconnect all brokers
        for conn in self.connections:
            try:
                conn.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting from {conn.broker['name']}: {e}")
        
        self._status_task = None
        logger.info("Disconnected from all brokers")

    def _status_heartbeat_loop(self):
        """Background thread that publishes periodic status updates"""
        import time

        while self._running:
            try:
                # Publish status (JWT refresh now handled by individual broker timers)
                self.publish_status(
                    state="online", origin=self.node_name, radio_config=self.radio_config
                )
                logger.debug(f"Status heartbeat sent (next in {self.status_interval}s)")

                time.sleep(self.status_interval)
            except Exception as e:
                logger.error(f"Status heartbeat error: {e}")
                time.sleep(self.status_interval)

    # ----------------------------------------------------------------
    # Packet helpers
    # ----------------------------------------------------------------
    def _process_packet(self, pkt: dict) -> dict:
        return {"timestamp": datetime.now(UTC).isoformat(), "origin_id": self.public_key, **pkt}

    def publish_packet(self, pkt: dict, subtopic="packets", retain=False):
        return self.publish(subtopic, self._process_packet(pkt), retain)

    def publish_raw_data(self, raw_hex: str, subtopic="raw", retain=False):
        pkt = {"type": "raw", "data": raw_hex, "bytes": len(raw_hex) // 2}
        return self.publish_packet(pkt, subtopic, retain)

    def publish_status(
        self,
        state: str = "online",
        location: Optional[dict] = None,
        extra_stats: Optional[dict] = None,
        origin: Optional[str] = None,
        radio_config: Optional[str] = None,
    ):
        """
        Publish device status/heartbeat message

        Args:
            state: Device state (online/offline)
            location: Optional dict with latitude/longitude
            extra_stats: Optional additional statistics to include
            origin: Node name/description
            radio_config: Radio configuration string (freq,bw,sf,cr)
        """
        # Get live stats from provider if available
        if self.stats_provider:
            live_stats = self.stats_provider()
        else:
            live_stats = {"uptime_secs": 0, "packets_sent": 0, "packets_received": 0}

        status = {
            "status": state,
            "timestamp": datetime.now(UTC).isoformat(),
            "origin": origin or self.node_name,
            "origin_id": self.public_key,
            "model": "PyMC-Repeater",
            "firmware_version": self.app_version,
            "radio": radio_config or self.radio_config,
            "client_version": f"pyMC_repeater/{self.app_version}",
            "stats": {**live_stats, "errors": 0, "queue_len": 0, **(extra_stats or {})},
        }

        if location:
            status["location"] = location

        return self.publish("status", status, retain=True, qos=1)

    def publish(self, subtopic: str, payload: dict, retain: bool = False, qos: int = 0):
        """Publish message to all connected brokers"""
        message = json.dumps(payload)

        # _BrokerConnection now handles topic prefixing, so we only log the subtopic here
        logger.debug(f"Publishing topic='{subtopic}', {_summarize_payload_for_log(payload, message)}")

        packet_type = payload.get("type")

        results = []
        with self._lock:
            for conn in self.connections:
                if conn.enabled and conn.is_connected():
                    if packet_type in conn.disallowed_types:
                        logger.debug(f"Skipped publishing packet type 0x{packet_type:02X} (disallowed)")
                        continue
                    result = conn.publish(subtopic, message, retain=retain, qos=qos)
                    results.append((conn.broker["name"], result))
                    logger.debug(f"Published to {conn.broker['name']} -- {subtopic}")
                elif conn.enabled == False:
                    results.append((conn.broker["name"], "Skipped due to being disabled"))

        if not results:
            logger.warning(f"No active broker connections for publishing to {subtopic}")

        return results
    

    def publish_mqtt(self, payload: dict, subtopic: str, retain: bool = False, qos: int = 0):
        """Publish message to brokers using the legacy custom-MQTT format only.

        This path exists for diagnostic streams (advert, noise_floor, crc_errors)
        that were originally only published to ``format: mqtt`` brokers. MC2MQTT
        family brokers (letsmesh, waev, meshcoretomqtt) intentionally skip these
        topics today.
        """
        message = json.dumps(payload)

        # _BrokerConnection now handles topic prefixing, so we only log the subtopic here
        logger.debug(f"Publishing topic='{subtopic}', {_summarize_payload_for_log(payload, message)}")

        results = []
        with self._lock:
            for conn in self.connections:
                if conn.enabled and conn.is_connected():
                    if conn.format != "mqtt":
                        # Custom-MQTT-only path; MC2MQTT brokers are intentionally skipped here.
                        logger.debug(
                            f"Skipped publishing to {conn.broker['name']} "
                            f"(intentional: publish_mqtt only targets legacy mqtt format; broker format={conn.format})"
                        )
                        results.append((conn.broker["name"], None))
                        continue
                    result = conn.publish(subtopic, message, retain=retain, qos=qos)
                    results.append((conn.broker["name"], result))
                    logger.debug(f"Published to {conn.broker['name']} (format={conn.format}) -- {subtopic}")
                elif conn.enabled == False:
                    results.append((conn.broker["name"], "Skipped due to being disabled"))

        if not results:
            logger.warning(f"No active broker connections for publishing to {subtopic}")

        return results


# ====================================================================
# Helper Functions
# ====================================================================


def get_mqtt_error_message(rc: int, is_disconnect: bool = False) -> str:
    """
    Get human-readable MQTT error message.

    Args:
        rc: Return code from paho-mqtt
        is_disconnect: True if from on_disconnect, False if from on_connect

    Returns:
        Human-readable error message
    """
    # Fallback to manual mappings - Extended with MQTT v5 codes
    connect_errors = {
        0: "Connection accepted",
        1: "Incorrect protocol version",
        2: "Invalid client identifier",
        3: "Server unavailable",
        4: "Bad username or password (JWT invalid)",
        5: "Not authorized (JWT signature/format invalid)",
        # MQTT v5 codes
        128: "Unspecified error",
        129: "Malformed packet",
        130: "Protocol error",
        131: "Implementation specific error",
        132: "Unsupported protocol version",
        133: "Client identifier not valid",
        134: "Bad username or password",
        135: "Not authorized",
        136: "Server unavailable",
        137: "Server busy",
        138: "Banned",
        140: "Bad authentication method",
        144: "Topic name invalid",
        149: "Packet too large",
        151: "Quota exceeded",
        153: "Payload format invalid",
        154: "Retain not supported",
        155: "QoS not supported",
        156: "Use another server",
        157: "Server moved",
        159: "Connection rate exceeded",
    }

    disconnect_errors = {
        0: "Normal disconnect",
        1: "Unacceptable protocol version",
        2: "Identifier rejected",
        3: "Server unavailable",
        4: "Bad username or password",
        5: "Not authorized",
        7: "Connection lost / network error",
        16: "The connection was lost.",
        17: "Client timeout",
        # MQTT v5 codes
        4: "Disconnect with Will message",
        128: "Unspecified error",
        129: "Malformed packet",
        130: "Protocol error",
        131: "Implementation specific error",
        135: "Not authorized",
        137: "Server busy",
        139: "Server shutting down",
        141: "Keep alive timeout",
        142: "Session taken over",
        143: "Topic filter invalid",
        144: "Topic name invalid",
        147: "Receive maximum exceeded",
        148: "Topic alias invalid",
        149: "Packet too large",
        150: "Message rate too high",
        151: "Quota exceeded",
        152: "Administrative action",
        153: "Payload format invalid",
        154: "Retain not supported",
        155: "QoS not supported",
        156: "Use another server",
        157: "Server moved",
        158: "Shared subscriptions not supported",
        159: "Connection rate exceeded",
        160: "Maximum connect time",
        161: "Subscription identifiers not supported",
        162: "Wildcard subscriptions not supported",
    }

    if HAS_REASON_CODES and ReasonCode is not None:
        try:

            reason = ReasonCode(mqtt.CONNACK if not is_disconnect else mqtt.DISCONNECT, identifier=rc)
            name = reason.getName() if hasattr(reason, 'getName') else str(reason)
            return f"{name} (code {rc})"
        except Exception as e:

            _fallback = (disconnect_errors if is_disconnect else connect_errors).get(rc)
            if _fallback is None:
                logger.debug(f"Could not decode reason code {rc}: {e}")

    if is_disconnect:
        try:
            paho_error = mqtt.error_string(rc)
            if paho_error and paho_error != "Unknown error.":
                return paho_error
        except Exception:
            pass

    error_dict = disconnect_errors if is_disconnect else connect_errors
    return error_dict.get(rc, f"Unknown error code {rc}")
