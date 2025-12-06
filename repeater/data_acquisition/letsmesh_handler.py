import json
import logging
import binascii
import base64
import paho.mqtt.client as mqtt

# Python 3.11 introduced datetime.UTC; provide a fallback for 3.8–3.10
try:  # pragma: no cover - tiny compatibility shim
    from datetime import datetime, timedelta, UTC
except ImportError:  # Python < 3.11
    from datetime import datetime, timedelta, timezone
    UTC = timezone.utc

from nacl.signing import SigningKey
from typing import Callable, Optional
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
# MeshCore → MQTT Publisher with Ed25519 auth token
# ====================================================================
class MeshCoreToMqttJwtPusher:
    """
    Push-only MQTT publisher for Let's Mesh MQTT brokers.
    Implements MeshCore-style Ed25519 token signing.
    No modifications to crypto.py.
    """

    def __init__(
        self,
        private_key: str,
        public_key: str,
        config: dict,
        jwt_expiry_minutes: int = 10,
        use_tls: bool = True,
        stats_provider: Optional[Callable[[], dict]] = None,
    ):
        # Extract values from config
        from ..config import get_node_info

        node_info = get_node_info(config)

        iata_code = node_info["iata_code"]
        broker_index = node_info["broker_index"]
        self.email = node_info.get("email", "")
        self.owner = node_info.get("owner", "")
        status_interval = node_info["status_interval"]
        node_name = node_info["node_name"]
        radio_config = node_info["radio_config"]

        if broker_index >= len(LETSMESH_BROKERS):
            raise ValueError(f"Invalid broker_index {broker_index}")

        self.broker = LETSMESH_BROKERS[broker_index]
        self.private_key_hex = private_key
        self.public_key = public_key.upper()
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
        self._connect_time = None
        self._tls_verified = False

        # MQTT WebSocket client
        self.client = mqtt.Client(client_id=f"meshcore_{self.public_key}", transport="websockets")
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    # ----------------------------------------------------------------
    # MeshCore-style Ed25519 token generator
    # ----------------------------------------------------------------
    def _generate_jwt(self) -> str:
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
            logging.debug("JWT includes email/owner (TLS verified)")
        else:
            payload["email"] = ""
            payload["owner"] = ""
            if not self.use_tls:
                logging.debug("JWT excludes email/owner (TLS disabled)")
            elif not self._tls_verified:
                logging.debug("JWT excludes email/owner (TLS not verified yet)")
            else:
                logging.debug("JWT excludes email/owner (email/owner not configured)")

        # Encode header and payload (compact JSON - no spaces)
        header_b64 = b64url(json.dumps(header, separators=(",", ":")).encode())
        payload_b64 = b64url(json.dumps(payload, separators=(",", ":")).encode())

        signing_input = f"{header_b64}.{payload_b64}".encode()
        seed32 = binascii.unhexlify(self.private_key_hex)
        signer = SigningKey(seed32)

        # Verify the public key matches what we expect
        derived_public = binascii.hexlify(bytes(signer.verify_key)).decode()
        if derived_public.upper() != self.public_key.upper():
            raise ValueError(
                f"Public key mismatch! " f"Derived: {derived_public}, Expected: {self.public_key}"
            )

        # Sign the message
        signature = signer.sign(signing_input).signature
        signature_hex = binascii.hexlify(signature).decode()
        token = f"{header_b64}.{payload_b64}.{signature_hex}"

        logging.debug(f"Generated MeshCore token: {token}")
        return token

    # ----------------------------------------------------------------
    # MQTT setup
    # ----------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logging.info(f"Connected to {self.broker['name']}")
            self._running = True
            
            # Publish initial status on connect
            self.publish_status(
                state="online", origin=self.node_name, radio_config=self.radio_config
            )
            
            # connected start heartbeat thread
            if self.status_interval > 0 and not self._status_task:
                import threading
                self._status_task = threading.Thread(target=self._status_heartbeat_loop, daemon=True)
                self._status_task.start()
                logging.info(f"Started status heartbeat (interval: {self.status_interval}s)")
        else:
            logging.error(f"Failed with code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        logging.warning(f"Disconnected (rc={rc})")
        self._running = False

    def _refresh_jwt_token(self):
        """Refresh JWT token for MQTT authentication"""
        token = self._generate_jwt()
        username = f"v1_{self.public_key}"
        self.client.username_pw_set(username=username, password=token)
        self._connect_time = datetime.now(UTC)
        logging.info("JWT token refreshed")

    # ----------------------------------------------------------------
    # Connect using WebSockets + TLS + MeshCore token auth
    # ----------------------------------------------------------------
    def connect(self):
        # Conditional TLS setup
        if self.use_tls:
            import ssl
            # Enable TLS with certificate verification
            self.client.tls_set(
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT
            )
            self.client.tls_insecure_set(False)  # Enforce hostname verification
            # Mark as verified - if connection fails, we won't connect anyway
            self._tls_verified = True
            if self.email or self.owner:
                logging.info("TLS enabled with certificate verification - email/owner will be included")
            protocol = "wss"
        else:
            protocol = "ws"

        # Generate JWT token (will include email/owner if TLS verified)
        token = self._generate_jwt()
        username = f"v1_{self.public_key}"
        self.client.username_pw_set(username=username, password=token)

        logging.info(
            f"Connecting to {self.broker['name']} "
            f"({protocol}://{self.broker['host']}:{self.broker['port']}) ..."
        )

        # Must use raw hostname without wss://
        self.client.connect(self.broker["host"], self.broker["port"], keepalive=60)
        self.client.loop_start()
        self._connect_time = datetime.now(UTC)

    def disconnect(self):
        self._running = False
        # Publish offline status before disconnecting
        self.publish_status(state="offline", origin=self.node_name, radio_config=self.radio_config)
        import time

        time.sleep(0.5)  # Give time for the message to be sent

        self.client.loop_stop()
        self.client.disconnect()
        logging.info("Disconnected")

    def _status_heartbeat_loop(self):
        """Background thread that publishes periodic status updates"""
        import time

        while self._running:
            try:
                # Refresh JWT token before it expires (at 80% of expiry time)
                if self._connect_time:
                    elapsed = (datetime.now(UTC) - self._connect_time).total_seconds()
                    expiry_seconds = self.jwt_expiry_minutes * 60
                    if elapsed >= expiry_seconds * 0.8:
                        self._refresh_jwt_token()
                
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
        topic = self._topic(subtopic)
        message = json.dumps(payload)
        result = self.client.publish(topic, message, retain=retain)
        logging.debug(f"Published to {topic}: {message}")
        return result

