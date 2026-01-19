import json
import logging
import binascii
import base64
import paho.mqtt.client as mqtt
import threading

from datetime import datetime, timedelta, UTC
from nacl.signing import SigningKey
from typing import Callable, Optional, List, Dict
from .. import __version__


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
        on_connect_callback: Optional[Callable] = None,
        on_disconnect_callback: Optional[Callable] = None,
    ):
        self.broker = broker
        self.local_identity = local_identity
        self.public_key = public_key.upper()
        self.iata_code = iata_code
        self.jwt_expiry_minutes = jwt_expiry_minutes
        self.use_tls = use_tls
        self.email = email
        self.owner = owner
        self._on_connect_callback = on_connect_callback
        self._on_disconnect_callback = on_disconnect_callback
        self._connect_time = None
        self._tls_verified = False
        self._running = False
        self._should_be_connected = False
        self._reconnect_attempts = 0
        self._reconnect_timer = None
        self._max_reconnect_delay = 300  # 5 minutes max

        # MQTT WebSocket client - unique client ID per broker
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
        signature = self.local_identity.sign(signing_input)
        signature_hex = binascii.hexlify(signature).decode()
        token = f"{header_b64}.{payload_b64}.{signature_hex}"

        return token

    def _on_connect(self, client, userdata, flags, rc):
        """MQTT connection callback"""
        if rc == 0:
            logging.info(f"Connected to {self.broker['name']}")
            self._running = True
            self._reconnect_attempts = 0  # Reset counter on success
            if self._on_connect_callback:
                self._on_connect_callback(self.broker["name"])
        else:
            logging.error(f"Failed to connect to {self.broker['name']} (rc={rc})")
            self._schedule_reconnect()

    def _on_disconnect(self, client, userdata, rc):
        """MQTT disconnection callback"""
        was_running = self._running
        self._running = False

        # Log disconnect reason
        if rc == 0:
            logging.info(f"Disconnect received from {self.broker['name']}")
        else:
            logging.warning(f"Unexpected disconnect from {self.broker['name']} (rc={rc})")

        # Reconnect if we should be connected, regardless of rc value
        if self._should_be_connected:
            logging.info(f"Connection lost but should be connected - scheduling reconnect")
            self._schedule_reconnect()

        if self._on_disconnect_callback:
            self._on_disconnect_callback(self.broker["name"])

    def _schedule_reconnect(self):
        """Schedule reconnection with exponential backoff"""
        if self._reconnect_timer:
            self._reconnect_timer.cancel()
        
        # Exponential backoff: 5s, 10s, 20s, 40s, 80s, up to max
        delay = min(5 * (2 ** self._reconnect_attempts), self._max_reconnect_delay)
        self._reconnect_attempts += 1
        
        logging.info(f"Scheduling reconnect to {self.broker['name']} in {delay}s (attempt {self._reconnect_attempts})")
        self._reconnect_timer = threading.Timer(delay, self._attempt_reconnect)
        self._reconnect_timer.daemon = True
        self._reconnect_timer.start()
    
    def _attempt_reconnect(self):
        """Attempt to reconnect to broker"""
        try:
            logging.info(f"Attempting reconnection to {self.broker['name']}...")
            self.refresh_jwt_token()  # Refresh token before reconnecting
            self.client.reconnect()
        except Exception as e:
            logging.error(f"Reconnection failed for {self.broker['name']}: {e}")
            self._schedule_reconnect()  # Try again later
    
    def refresh_jwt_token(self):
        """Refresh JWT token for MQTT authentication"""
        token = self._generate_jwt()
        username = f"v1_{self.public_key}"
        self.client.username_pw_set(username=username, password=token)
        self._connect_time = datetime.now(UTC)
        logging.debug(f"JWT token refreshed for {self.broker['name']}")

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

        # Generate and set JWT token
        self.refresh_jwt_token()

        logging.info(
            f"Connecting to {self.broker['name']} "
            f"({protocol}://{self.broker['host']}:{self.broker['port']}) ..."
        )

        self._should_be_connected = True
        self.client.connect(self.broker["host"], self.broker["port"], keepalive=60)
        self.client.loop_start()

    def disconnect(self):
        """Disconnect from broker"""
        self._should_be_connected = False
        self._running = False
        
        # Cancel any pending reconnection
        if self._reconnect_timer:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None
        
        self.client.loop_stop()
        self.client.disconnect()
        logging.info(f"Disconnected from {self.broker['name']}")

    def publish(self, topic: str, payload: str, retain: bool = False):
        """Publish message to broker"""
        if self._running:
            result = self.client.publish(topic, payload, retain=retain)
            return result
        return None

    def is_connected(self) -> bool:
        """Check if connection is active"""
        return self._running

    def should_refresh_token(self) -> bool:
        """Check if JWT token needs refresh (at 80% of expiry)"""
        if not self._connect_time:
            return False
        elapsed = (datetime.now(UTC) - self._connect_time).total_seconds()
        expiry_seconds = self.jwt_expiry_minutes * 60
        return elapsed >= expiry_seconds * 0.8


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
            logging.info("Custom broker mode: using only user-defined brokers")
        elif broker_index is None or broker_index == -1:
            # Connect to all built-in brokers + additional ones
            self.brokers = LETSMESH_BROKERS.copy()
            logging.info(f"Multi-broker mode: connecting to all {len(LETSMESH_BROKERS)} built-in brokers")
        else:
            # Single broker mode (backward compatibility)
            if broker_index >= len(LETSMESH_BROKERS):
                raise ValueError(f"Invalid broker_index {broker_index}")
            self.brokers = [LETSMESH_BROKERS[broker_index]]
            logging.info(f"Single broker mode: connecting to {self.brokers[0]['name']}")

        # Add additional brokers from config
        if additional_brokers:
            for broker_config in additional_brokers:
                if all(k in broker_config for k in ["name", "host", "port", "audience"]):
                    self.brokers.append(broker_config)
                    logging.info(f"Added custom broker: {broker_config['name']}")
                else:
                    logging.warning(f"Skipping invalid broker config: {broker_config}")
        
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
        for broker in self.brokers:
            conn = _BrokerConnection(
                broker=broker,
                local_identity=self.local_identity,
                public_key=self.public_key,
                iata_code=self.iata_code,
                jwt_expiry_minutes=self.jwt_expiry_minutes,
                use_tls=self.use_tls,
                email=self.email,
                owner=self.owner,
                on_connect_callback=self._on_broker_connected,
                on_disconnect_callback=self._on_broker_disconnected,
            )
            self.connections.append(conn)

        logging.info(f"Initialized with {len(self.connections)} broker connection(s)")

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
            logging.info(f"Started status heartbeat (interval: {self.status_interval}s)")

    def _on_broker_disconnected(self, broker_name: str):
        """Callback when a broker disconnects"""
        # Check if all connections are down
        all_down = all(not conn.is_connected() for conn in self.connections)
        if all_down:
            logging.warning("All broker connections lost")
            self._running = False

    def connect(self):
        """Establish connections to all configured brokers"""
        for conn in self.connections:
            try:
                conn.connect()
            except Exception as e:
                logging.error(f"Failed to connect to {conn.broker['name']}: {e}")

    def disconnect(self):
        """Disconnect from all brokers"""
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
                logging.error(f"Error disconnecting from {conn.broker['name']}: {e}")

        logging.info("Disconnected from all brokers")

    def _status_heartbeat_loop(self):
        """Background thread that publishes periodic status updates"""
        import time

        while self._running:
            try:
                # Refresh JWT tokens for all connections before they expire
                for conn in self.connections:
                    if conn.is_connected() and conn.should_refresh_token():
                        conn.refresh_jwt_token()

                self.publish_status(
                    state="online", origin=self.node_name, radio_config=self.radio_config
                )
                logging.debug(f"Status heartbeat sent (next in {self.status_interval}s)")
                time.sleep(self.status_interval)
            except Exception as e:
                logging.error(f"Status heartbeat error: {e}")
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
                    logging.debug(f"Published to {conn.broker['name']}/{topic}")

        # Log if no brokers were available
        if not results:
            logging.warning(f"No active broker connections for publishing to {topic}")

        return results

