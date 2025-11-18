import json
import logging
import binascii
import base64
import paho.mqtt.client as mqtt

from datetime import datetime, timedelta, UTC
from nacl.signing import SigningKey


# --------------------------------------------------------------------
# Helper: Base64URL without padding (required by MeshCore broker)
# --------------------------------------------------------------------
def b64url(x: bytes) -> str:
    return base64.urlsafe_b64encode(x).rstrip(b"=").decode()


# --------------------------------------------------------------------
# Let's Mesh MQTT Broker List (WebSocket Secure)
# --------------------------------------------------------------------
LETSMESH_BROKERS = [
    # {
    #     "name": "test",
    #     "host": "localhost",
    #     "port": 8883,
    #     "audience": "mqtt.yourdomain.com"
    # },
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
        iata_code: str,
        broker_index: int = 0,
        topic_prefix: str = "meshcore",
        jwt_expiry_minutes: int = 10,
        use_tls: bool = True,
        status_interval: int = 60,  # Heartbeat interval in seconds
        model: str = "PyMC-Repeater",
        app_version: str = "0.0.0",
        node_name: str = None,
        radio_config: str = None,
    ):

        if broker_index >= len(LETSMESH_BROKERS):
            raise ValueError(f"Invalid broker_index {broker_index}")

        self.broker = LETSMESH_BROKERS[broker_index]
        self.private_key_hex = private_key
        self.public_key = public_key.upper()
        self.iata_code = iata_code
        self.topic_prefix = topic_prefix
        self.jwt_expiry_minutes = jwt_expiry_minutes
        self.use_tls = use_tls
        self.status_interval = status_interval
        self.model = model
        self.app_version = app_version
        self.node_name = node_name or "PyMC-Repeater"
        self.radio_config = radio_config or "915.0,125.0,7,5"
        self._status_task = None
        self._running = False
        self._packet_stats = {
            "packets_sent": 0,
            "packets_received": 0,
            "start_time": datetime.now(UTC)
        }

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
        return f"{self.topic_prefix}/{self.iata_code}/{self.public_key}/{subtopic}"

    def publish_packet(self, pkt: dict, subtopic="packets", retain=False):
        return self.publish(subtopic, self._process_packet(pkt), retain)

    def publish_raw_data(self, raw_hex: str, subtopic="raw", retain=False):
        pkt = {
            "type": "raw",
            "data": raw_hex,
            "bytes": len(raw_hex) // 2
        }
        self._packet_stats["packets_sent"] += 1
        return self.publish_packet(pkt, subtopic, retain)
    
    def publish_status(self, state: str = "online", location: dict = None, extra_stats: dict = None, 
                      origin: str = None, radio_config: str = None):
        """
        Publish device status/heartbeat message
        
        Args:
            state: Device state (online/offline)
            location: Optional dict with latitude/longitude
            extra_stats: Optional additional statistics to include
            origin: Node name/description
            radio_config: Radio configuration string (freq,bw,sf,cr)
        """
        uptime_secs = int((datetime.now(UTC) - self._packet_stats["start_time"]).total_seconds())
        
        status = {
            "status": state,
            "timestamp": datetime.now(UTC).isoformat(),
            "origin": origin or "PyMC-Repeater",
            "origin_id": self.public_key,
            "model": self.model,
            "firmware_version": self.app_version,
            "radio": radio_config or "0.0,0.0,0,0",
            "client_version": f"pyMC_repeater_{self.app_version}",
            "stats": {
                "uptime_secs": uptime_secs,
                "packets_sent": self._packet_stats["packets_sent"],
                "packets_received": self._packet_stats["packets_received"],
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


# --------------------------------------------------------------------
# Example Usage
# --------------------------------------------------------------------
if __name__ == "__main__":
    import time
    import sys
    
    logging.basicConfig(level=logging.INFO)

    # Parse command line arguments
    mode = sys.argv[1] if len(sys.argv) > 1 else "live"
    
    if mode not in ["live", "send"]:
        print("Usage: python letsmesh_handler.py [live|send]")
        print("  live - Run in live mode with periodic status updates")
        print("  send - Send a single test packet and exit")
        sys.exit(1)

    test_packet = (
        "1D02DB013E6203740EEFAAC93AEE45D0854005CB3327ECB8D7626792D4D934F81E"
        "2559DC27BCA0DFC407ED729E84E713AB2DDB5A04A509"
    )

    # Use saved test keypair (or generate new ones)
    USE_SAVED_KEYS = True  # Set to False to generate new keys
    
    if USE_SAVED_KEYS:
        # Saved test keypair
        private_key = "2d2893a803b6eaed8c7e92189b8f7b76098e043c3e4a4f6a247cb730866c6fc9"
        public_key = "66508c1711742e7633384659dc8139fa32c972cea9a50043da13bb9cb498de34"

        print(f"\n=== Using Saved Test Keypair ===")
        print(f"Public key: {public_key}")
    else:
        # Generate a valid test keypair
        print("Generating new test keypair...")
        test_signer = SigningKey.generate()
        private_key = binascii.hexlify(bytes(test_signer)).decode()
        public_key = binascii.hexlify(bytes(test_signer.verify_key)).decode()
        
        print(f"\n=== Valid Test Keypair ===")
        print(f"Private key: {private_key}")
        print(f"Public key:  {public_key}")
        print(f"\nSave these for future testing!")
    
    print()

    # Create pusher with appropriate configuration
    pusher = MeshCoreToMqttJwtPusher(
        private_key=private_key,
        public_key=public_key,
        iata_code="test",
        broker_index=0,
        status_interval=30 if mode == "live" else 0,  # 30s heartbeat in live mode
        model="PyMC-Gateway",
        app_version="1.0.0"
    )

    pusher.connect()
    
    # Wait for connection to complete
    print(f"Mode: {mode.upper()}")
    print("Waiting for connection to complete...")
    time.sleep(2)
    
    if mode == "send":
        # Send mode: publish one packet and exit
        print("Publishing test packet...")
        pusher.publish_raw_data(test_packet)
        
        # Wait for publish to complete
        time.sleep(1)
        
        print("Disconnecting...")
        pusher.disconnect()
        print("Done!")
        
    elif mode == "live":
        # Live mode: stay connected and send periodic status updates
        print("Connected! Publishing status updates every 30s...")
        print("\nPress Ctrl+C to disconnect\n")
        
        try:
            # Keep running until interrupted
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\nShutting down...")
            pusher.disconnect()
            print("Done!")
