import base64
import binascii
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

import paho.mqtt.client as mqtt
from nacl.signing import SigningKey

# Try to import datetime.UTC (Python 3.11+) otherwise fallback to timezone.utc
try:
    from datetime import UTC
except Exception:
    from datetime import timezone
    UTC = timezone.utc

from repeater import __version__

# Try to import paho-mqtt error code mappings
try:
    from paho.mqtt.reasoncodes import ReasonCode

    HAS_REASON_CODES = True
except ImportError:
    HAS_REASON_CODES = False

logger = logging.getLogger("LetsMeshHandler")


# --------------------------------------------------------------------
# Helper: Base64URL without padding
# --------------------------------------------------------------------
def b64url(x: bytes) -> str:
    return base64.urlsafe_b64encode(x).rstrip(b"=").decode()


# --------------------------------------------------------------------
# Let's Mesh MQTT Broker List (WebSocket Secure)
# --------------------------------------------------------------------
LETSMESH_BROKERS = [
    {
        "name": "Europe (LetsMesh v1)",
        "host": "mqtt-eu-v1.letsmesh.net",
        "port": 443,
        "audience": "mqtt-eu-v1.letsmesh.net",
    },
    {
        "name": "US West (LetsMesh v1)",
        "host": "mqtt-us-v1.letsmesh.net",
        "port": 443,
        "audience": "mqtt-us-v1.letsmesh.net",
    },
]


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
        use_tls: bool,
        email: str,
        owner: str,
        broker_index: int = 0,
        on_connect_callback: Optional[Callable] = None,
        on_disconnect_callback: Optional[Callable] = None,
    ):
        self.broker = broker
        self.local_identity = local_identity
        self.public_key = public_key.upper()
        self.iata_code = iata_code
        self.jwt_expiry_minutes = jwt_expiry_minutes
        self.broker_index = broker_index
        self.use_tls = use_tls
        self.email = email
        self.owner = owner
        self._on_connect_callback = on_connect_callback
        self._on_disconnect_callback = on_disconnect_callback
        self._connect_time = None
        self._tls_verified = False
        self._running = False
        self._reconnect_attempts = 0
        self._reconnect_timer = None
        self._max_reconnect_delay = 300  # 5 minutes max
        self._jwt_refresh_timer = None
        client_id = f"meshcore_{self.public_key}_{broker['host']}"
        self.client = mqtt.Client(client_id=client_id, transport="websockets")
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

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

        # Only include email/owner for verified TLS connections
        if self.use_tls and self._tls_verified and (self.email or self.owner):
            payload["email"] = self.email
            payload["owner"] = self.owner
        else:
            payload["email"] = ""
            payload["owner"] = ""

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
            self._schedule_jwt_refresh()  # Schedule proactive JWT refresh
            if self._on_connect_callback:
                self._on_connect_callback(self.broker["name"])
        else:
            error_msg = get_mqtt_error_message(rc, is_disconnect=False)
            logger.error(f"Failed to connect to {self.broker['name']}: {error_msg}")
            self._schedule_reconnect()

    def _on_disconnect(self, client, userdata, rc):
        """MQTT disconnection callback"""
        was_running = self._running
        self._running = False

        if rc != 0:  # Unexpected disconnect
            error_msg = get_mqtt_error_message(rc, is_disconnect=True)
            logger.warning(f"Disconnected from {self.broker['name']} (rc={rc}): {error_msg}")
            if was_running:  # Only reconnect if we were intentionally connected
                self._schedule_reconnect(reason=error_msg)
        else:
            logger.info(f"Clean disconnect from {self.broker['name']}")

        if self._on_disconnect_callback:
            self._on_disconnect_callback(self.broker["name"])

    def _schedule_reconnect(self, reason: str = "connection lost"):
        """Schedule reconnection with exponential backoff"""
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
        try:
            logger.info(f"Attempting reconnection to {self.broker['name']} (reason: {reason})...")

            # Stop the loop if it's still running (websocket mode requires clean restart)
            try:
                self.client.loop_stop()
            except:
                pass

            self._set_jwt_credentials()

            # Reconnect and restart loop
            self.client.connect(self.broker["host"], self.broker["port"], keepalive=60)
            self.client.loop_start()
            self._loop_running = True
        except Exception as e:
            logger.error(f"Reconnection failed for {self.broker['name']}: {e}")
            self._schedule_reconnect()  # Try again later

    def _set_jwt_credentials(self):
        """Set JWT token credentials before connecting (CONNECT handshake only)"""
        try:
            token = self._generate_jwt()
            username = f"v1_{self.public_key}"
            self.client.username_pw_set(username=username, password=token)
            self._connect_time = datetime.now(UTC)
            logger.debug(f"JWT credentials set for {self.broker['name']}")
            logger.debug(f"Using username: {username}")
            logger.debug(f"Public key: {self.public_key[:16]}...{self.public_key[-16:]}")
        except Exception as e:
            logger.error(f"Failed to set JWT credentials for {self.broker['name']}: {e}")
            raise

    def connect(self):
        """Establish connection to broker"""
        # Conditional TLS setup
        if self.use_tls:
            import ssl

            self.client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
            self.client.tls_insecure_set(False)
            self._tls_verified = True
            protocol = "wss"
        else:
            protocol = "ws"

        # Set JWT credentials before CONNECT handshake
        self._set_jwt_credentials()

        logger.info(
            f"Connecting to {self.broker['name']} "
            f"({protocol}://{self.broker['host']}:{self.broker['port']}) ..."
        )

        self.client.connect(self.broker["host"], self.broker["port"], keepalive=60)
        self.client.loop_start()
        self._loop_running = True

    def disconnect(self):
        """Disconnect from broker"""
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

    def publish(self, topic: str, payload: str, retain: bool = False):
        """Publish message to broker"""
        if self._running:
            result = self.client.publish(topic, payload, retain=retain)
            return result
        return None

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
        self.client.disconnect()  # Triggers clean disconnect, then reconnect via timer
        self._schedule_reconnect(reason="JWT token expiry")


# ====================================================================
# MeshCore → MQTT Publisher with Ed25519 auth token
# ====================================================================
class MeshCoreToMqttJwtPusher:

    def __init__(
        self,
        local_identity,
        config: dict,
        jwt_expiry_minutes: int = 10,
        use_tls: bool = True,
        stats_provider: Optional[Callable[[], dict]] = None,
    ):
        # Store local identity and get public key
        self.local_identity = local_identity
        public_key = local_identity.get_public_key().hex().upper()

        # Extract values from config
        from ..config import get_node_info

        node_info = get_node_info(config)

        iata_code = node_info["iata_code"]
        broker_index = node_info.get("broker_index")
        self.email = node_info.get("email", "")
        self.owner = node_info.get("owner", "")
        status_interval = node_info["status_interval"]
        node_name = node_info["node_name"]
        radio_config = node_info["radio_config"]

        # Get additional brokers from config (optional)
        letsmesh_config = config.get("letsmesh", {})
        additional_brokers = letsmesh_config.get("additional_brokers", [])

        # Determine which brokers to connect to
        if broker_index == -2:
            # Custom brokers only - no built-in brokers
            self.brokers = []
            logger.info("Custom broker mode: using only user-defined brokers")
        elif broker_index is None or broker_index == -1:
            # Connect to all built-in brokers + additional ones
            self.brokers = LETSMESH_BROKERS.copy()
            logger.info(
                f"Multi-broker mode: connecting to all {len(LETSMESH_BROKERS)} built-in brokers"
            )
        else:

            if broker_index >= len(LETSMESH_BROKERS):
                raise ValueError(f"Invalid broker_index {broker_index}")
            self.brokers = [LETSMESH_BROKERS[broker_index]]
            logger.info(f"Single broker mode: connecting to {self.brokers[0]['name']}")

        # Add additional brokers from config
        if additional_brokers:
            for broker_config in additional_brokers:
                if all(k in broker_config for k in ["name", "host", "port", "audience"]):
                    self.brokers.append(broker_config)
                    logger.info(f"Added custom broker: {broker_config['name']}")
                else:
                    logger.warning(f"Skipping invalid broker config: {broker_config}")

        # Validate that we have at least one broker
        if not self.brokers:
            raise ValueError(
                "No brokers configured. Either set broker_index to a valid value "
                "or provide additional_brokers in config."
            )

        self.local_identity = local_identity
        self.public_key = public_key
        self.iata_code = iata_code
        self.jwt_expiry_minutes = jwt_expiry_minutes
        self.use_tls = use_tls
        self.status_interval = status_interval
        self.app_version = __version__
        self.node_name = node_name
        self.radio_config = radio_config
        self.stats_provider = stats_provider
        self._status_task = None
        self._running = False
        self._lock = threading.Lock()

        # Create broker connections
        self.connections: List[_BrokerConnection] = []
        for idx, broker in enumerate(self.brokers):
            conn = _BrokerConnection(
                broker=broker,
                local_identity=self.local_identity,
                public_key=self.public_key,
                iata_code=self.iata_code,
                jwt_expiry_minutes=self.jwt_expiry_minutes,
                use_tls=self.use_tls,
                email=self.email,
                owner=self.owner,
                broker_index=idx,
                on_connect_callback=self._on_broker_connected,
                on_disconnect_callback=self._on_broker_disconnected,
            )
            self.connections.append(conn)

        logger.info(f"Initialized with {len(self.connections)} broker connection(s)")

    def _on_broker_connected(self, broker_name: str):
        """Callback when a broker connects"""
        # Publish initial status on first connection
        if not self._status_task and self.status_interval > 0:
            self._running = True
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
            except Exception as e:
                logger.error(f"Failed to connect to {conn.broker['name']}: {e}")

    def _delayed_connect(self, conn):
        """Connect a broker after a delay (called by timer)"""
        try:
            conn.connect()
        except Exception as e:
            logger.error(f"Failed to connect to {conn.broker['name']}: {e}")

    def disconnect(self):
        """Disconnect from all brokers"""
        # Stop the heartbeat loop
        self._running = False

        # Publish offline status before disconnecting
        self.publish_status(state="offline", origin=self.node_name, radio_config=self.radio_config)

        import time

        time.sleep(0.5)  # Give time for messages to be sent

        # Disconnect all brokers
        for conn in self.connections:
            try:
                conn.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting from {conn.broker['name']}: {e}")

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

    def _topic(self, subtopic: str) -> str:
        return f"meshcore/{self.iata_code}/{self.public_key}/{subtopic}"

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

        return self.publish("status", status, retain=False)

    def publish(self, subtopic: str, payload: dict, retain: bool = False):
        """Publish message to all connected brokers"""
        topic = self._topic(subtopic)
        message = json.dumps(payload)

        results = []
        with self._lock:
            for conn in self.connections:
                if conn.is_connected():
                    result = conn.publish(topic, message, retain=retain)
                    results.append((conn.broker["name"], result))
                    logger.debug(f"Published to {conn.broker['name']}/{topic}")

        if not results:
            logger.warning(f"No active broker connections for publishing to {topic}")

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
    if HAS_REASON_CODES:
        try:
            # ReasonCode object has getName() method and value property
            reason = ReasonCode(mqtt.CONNACK if not is_disconnect else mqtt.DISCONNECT, identifier=rc)
            name = reason.getName() if hasattr(reason, 'getName') else str(reason)
            return f"{name} (code {rc})"
        except Exception as e:
            # Log the exception for debugging
            logger.debug(f"Could not decode reason code {rc}: {e}")

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
        16: "Connection lost / protocol error",
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

    error_dict = disconnect_errors if is_disconnect else connect_errors
    return error_dict.get(rc, f"Unknown error code {rc}")
