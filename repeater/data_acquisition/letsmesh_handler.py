import json
import logging
import binascii
import base64
import paho.mqtt.client as mqtt

from datetime import datetime, timedelta, UTC
from dataclasses import dataclass, asdict
from nacl.signing import SigningKey
from typing import Callable, Optional
from .. import __version__

# --------------------------------------------------------------------
# Helper: Base64URL without padding (required by MeshCore broker)
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
        "audience": "mqtt-eu-v1.letsmesh.net"
    },
    {
        "name": "US West (LetsMesh v1)",
        "host": "mqtt-us-v1.letsmesh.net",
        "port": 443,
        "audience": "mqtt-us-v1.letsmesh.net"
    },
    {
        "name": "Europe (LetsMesh v1)",
        "host": "mqtt-eu-v1.letsmesh.net",
        "port": 443,
        "audience": "mqtt-eu-v1.letsmesh.net"
    }
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

        # MQTT WebSocket client
        self.client = mqtt.Client(
            client_id=f"meshcore_{self.public_key}",
            transport="websockets"
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    # ----------------------------------------------------------------
    # MeshCore-style Ed25519 token generator
    # ----------------------------------------------------------------
    def _generate_jwt(self) -> str:
        now = datetime.now(UTC)

        header = {
            "alg": "Ed25519",
            "typ": "JWT"
        }

        payload = {
            "publicKey": self.public_key,
            "aud": self.broker["audience"],
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=self.jwt_expiry_minutes)).timestamp())
        }

        # Encode header and payload (compact JSON - no spaces)
        header_b64 = b64url(json.dumps(header, separators=(',', ':')).encode())
        payload_b64 = b64url(json.dumps(payload, separators=(',', ':')).encode())

        signing_input = f"{header_b64}.{payload_b64}".encode()
        seed32 = binascii.unhexlify(self.private_key_hex)
        signer = SigningKey(seed32)
        
        # Verify the public key matches what we expect
        derived_public = binascii.hexlify(bytes(signer.verify_key)).decode()
        if derived_public.upper() != self.public_key.upper():
            raise ValueError(
                f"Public key mismatch! "
                f"Derived: {derived_public}, Expected: {self.public_key}"
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
                state="online",
                origin=self.node_name,
                radio_config=self.radio_config
            )
        else:
            logging.error(f"Failed with code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        logging.warning(f"Disconnected (rc={rc})")
        self._running = False

    # ----------------------------------------------------------------
    # Connect using WebSockets + TLS + MeshCore token auth
    # ----------------------------------------------------------------
    def connect(self):

        token = self._generate_jwt()
        username = f"v1_{self.public_key}"

        self.client.username_pw_set(username=username, password=token)
        
        # Conditional TLS setup
        if self.use_tls:
            self.client.tls_set()
            protocol = "wss"
        else:
            protocol = "ws"

        logging.info(
            f"Connecting to {self.broker['name']} "
            f"({protocol}://{self.broker['host']}:{self.broker['port']}) ..."
        )

        # Must use raw hostname without wss://
        self.client.connect(self.broker["host"], self.broker["port"], keepalive=60)
        self.client.loop_start()
        
        # Start status heartbeat if interval is set
        if self.status_interval > 0:
            import threading
            self._status_task = threading.Thread(target=self._status_heartbeat_loop, daemon=True)
            self._status_task.start()
            logging.info(f"Started status heartbeat (interval: {self.status_interval}s)")

    def disconnect(self):
        self._running = False
        # Publish offline status before disconnecting
        self.publish_status(
            state="offline",
            origin=self.node_name,
            radio_config=self.radio_config
        )
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
                self.publish_status(
                    state="online", 
                    origin=self.node_name,
                    radio_config=self.radio_config
                )
                time.sleep(self.status_interval)
            except Exception as e:
                logging.error(f"Status heartbeat error: {e}")
                time.sleep(self.status_interval)

    # ----------------------------------------------------------------
    # Packet helpers
    # ----------------------------------------------------------------
    def _process_packet(self, pkt: dict) -> dict:
        return {
            "timestamp": datetime.now(UTC).isoformat(),
            "origin_id": self.public_key,
            **pkt
        }

    def _topic(self, subtopic: str) -> str:
        return f"meshcore/{self.iata_code}/{self.public_key}/{subtopic}"

    def publish_packet(self, pkt: dict, subtopic="packets", retain=False):
        return self.publish(subtopic, self._process_packet(pkt), retain)

    def publish_raw_data(self, raw_hex: str, subtopic="raw", retain=False):
        pkt = {
            "type": "raw",
            "data": raw_hex,
            "bytes": len(raw_hex) // 2
        }
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
            live_stats = {
                "uptime_secs": 0,
                "packets_sent": 0,
                "packets_received": 0
            }
        
        status = {
            "status": state,
            "timestamp": datetime.now(UTC).isoformat(),
            "origin": origin or self.node_name,
            "origin_id": self.public_key,
            "model": "PyMC-Repeater",
            "firmware_version": self.app_version,
            "radio": radio_config or self.radio_config,
            "client_version": f"pyMC_repeater/{self.app_version}",
            "stats": {
                **live_stats,
                "errors": 0,
                "queue_len": 0,
                **(extra_stats or {})
            }
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


# ====================================================================
# LetsMesh Packet Data Class
# ====================================================================

@dataclass
class LetsMeshPacket:
    """
    Data class for LetsMesh packet format.
    Converts internal packet_record format to LetsMesh publish format.
    """
    origin: str
    origin_id: str
    timestamp: str
    type: str
    direction: str
    time: str
    date: str
    len: str
    packet_type: str
    route: str
    payload_len: str
    raw: str
    SNR: str
    RSSI: str
    score: str
    duration: str
    hash: str

    @classmethod
    def from_packet_record(cls, packet_record: dict, origin: str, origin_id: str) -> Optional['LetsMeshPacket']:
        """
        Create LetsMeshPacket from internal packet_record format.
        
        Args:
            packet_record: Internal packet record dictionary
            origin: Node name
            origin_id: Public key of the node
            
        Returns:
            LetsMeshPacket instance or None if raw_packet is missing
        """
        if "raw_packet" not in packet_record or not packet_record["raw_packet"]:
            return None
        
        # Extract timestamp and format date/time
        timestamp = packet_record.get("timestamp", 0)
        dt = datetime.fromtimestamp(timestamp)
        
        # Format route type (1=Flood->F, 2=Direct->D, etc)
        route_map = {1: "F", 2: "D"}
        route = route_map.get(packet_record.get("route", 0), str(packet_record.get("route", 0)))
        
        return cls(
            origin=origin,
            origin_id=origin_id,
            timestamp=dt.isoformat(),
            type="PACKET",
            direction="rx",
            time=dt.strftime("%H:%M:%S"),
            date=dt.strftime("%-d/%-m/%Y"),
            len=str(len(packet_record["raw_packet"]) // 2),
            packet_type=str(packet_record.get("type", 0)),
            route=route,
            payload_len=str(packet_record.get("payload_length", 0)),
            raw=packet_record["raw_packet"],
            SNR=str(packet_record.get("snr", 0)),
            RSSI=str(packet_record.get("rssi", 0)),
            score=str(int(packet_record.get("score", 0) * 1000)),
            duration="0",
            hash=packet_record.get("packet_hash", "")
        )
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        return asdict(self)
