import json
import jwt  # PyJWT library
import logging
import paho.mqtt.client as mqtt
from datetime import datetime, timedelta
from typing import Dict, Any

# Let's Mesh MQTT Broker Hostnames (WebSocket Secure)
LETSMESH_BROKERS = [
    {
        "name": "US West", 
        "host": "wss://mqtt-us-west.letsmesh.net",
        "port": 443,
        "audience": "mqtt-us-west.letsmesh.net"
    },
    {
        "name": "US East", 
        "host": "wss://mqtt-us-east.letsmesh.net", 
        "port": 443,
        "audience": "mqtt-us-east.letsmesh.net"
    },
    {
        "name": "Europe", 
        "host": "wss://mqtt-eu.letsmesh.net",
        "port": 443, 
        "audience": "mqtt-eu.letsmesh.net"
    }
]

class MeshCoreToMqttJwtPusher:
    """
    Simple push-only MQTT publisher for Let's Mesh integration.
    Handles MeshCore packet publishing with JWT authentication.
    """
    
    def __init__(self, private_key: str, public_key: str, iata_code: str, 
                 broker_index: int = 0, topic_prefix: str = "meshcore", 
                 jwt_expiry_minutes: int = 10):
        """
        Initialize the MeshCore to MQTT pusher.
        
        Args:
            private_key: Ed25519 private key for JWT signing
            public_key: Ed25519 public key (64 hex chars)
            iata_code: 3-letter IATA airport code or "test"
            broker_index: Index into LETSMESH_BROKERS array (default: 0 = US West)
            topic_prefix: MQTT topic prefix (default: "meshcore")
            jwt_expiry_minutes: JWT token expiry time in minutes
        """
        if broker_index >= len(LETSMESH_BROKERS):
            raise ValueError(f"Invalid broker_index {broker_index}. Max: {len(LETSMESH_BROKERS) - 1}")
            
        self.broker_config = LETSMESH_BROKERS[broker_index]
        self.private_key = private_key
        self.public_key = public_key.upper()
        self.iata_code = iata_code
        self.topic_prefix = topic_prefix
        self.jwt_expiry_minutes = jwt_expiry_minutes

        # Create MQTT client
        self.client = mqtt.Client(client_id=f"meshcore_{self.public_key}")
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    # ---------- JWT handling ----------
    def _generate_jwt(self) -> str:
        """Generate JWT token for authentication."""
        now = datetime.utcnow()
        payload = {
            "publicKey": self.public_key,
            "aud": self.broker_config["audience"],
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=self.jwt_expiry_minutes)).timestamp())
        }
        token = jwt.encode(payload, self.private_key, algorithm="ES256")
        logging.debug(f"Generated JWT for {self.broker_config['name']}: {payload}")
        return token

    # ---------- MQTT setup ----------
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logging.info(f"Connected to {self.broker_config['name']}")
        else:
            logging.error(f"Connection failed with code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        logging.warning(f"Disconnected from MQTT broker (code {rc})")

    def connect(self):
        """Connect to the MQTT broker."""
        jwt_token = self._generate_jwt()
        username = f"v1_{self.public_key}"
        self.client.username_pw_set(username=username, password=jwt_token)
        self.client.tls_set()

        broker_host = self.broker_config["host"].replace("wss://", "")
        broker_port = self.broker_config["port"] 
        
        logging.info(f"Connecting to {self.broker_config['name']} at {self.broker_config['host']}")
        self.client.connect(broker_host, broker_port, keepalive=60)
        self.client.loop_start()

    def disconnect(self):
        """Disconnect from the MQTT broker."""
        self.client.loop_stop()
        self.client.disconnect()
        logging.info(f"Disconnected from {self.broker_config['name']}")

    # ---------- MeshCore packet processing ----------
    def _process_packet(self, packet_data: dict) -> dict:
        """Process MeshCore packet data and add required fields."""
        processed = {
            "timestamp": datetime.utcnow().isoformat(),
            "origin_id": self.public_key,  # Required by Let's Mesh broker
        }
        processed.update(packet_data)
        return processed

    def publish_packet(self, packet_data: dict, subtopic: str = "packets", retain: bool = False):
        """Publish a single MeshCore packet."""
        processed_packet = self._process_packet(packet_data)
        return self.publish(subtopic, processed_packet, retain=retain)

    def publish_raw_data(self, raw_hex: str, subtopic: str = "raw", retain: bool = False):
        """Publish raw hex data from MeshCore."""
        packet_data = {
            "type": "raw", 
            "data": raw_hex,
            "bytes": len(raw_hex) // 2  # hex string is 2x the actual byte count
        }
        return self.publish_packet(packet_data, subtopic, retain)

    # ---------- Publish logic ----------
    def _make_topic(self, subtopic: str) -> str:
        """Generate topic string in Let's Mesh format."""
        return f"{self.topic_prefix}/{self.iata_code}/{self.public_key}/{subtopic}"

    def publish(self, subtopic: str, payload: dict, retain: bool = False):
        """Publish data to MQTT topic."""
        topic = self._make_topic(subtopic)
        message = json.dumps(payload, default=str)
        result = self.client.publish(topic, message, retain=retain)
        logging.debug(f"Published to {topic}: {message}")
        return result
