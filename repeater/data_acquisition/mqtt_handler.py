import json
import logging
import ssl
from typing import Any, Dict, Optional

try:
    import paho.mqtt.client as mqtt

    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False

from .storage_utils import PacketRecord

logger = logging.getLogger("MQTTHandler")


class MQTTHandler:
    def __init__(self, mqtt_config: dict, node_name: str = "unknown", node_id: str = "unknown"):
        self.mqtt_config = mqtt_config
        self.node_name = node_name
        self.node_id = node_id
        self.client = None
        self.available = MQTT_AVAILABLE
        self._init_client()

    def _init_client(self):
        if not self.available or not self.mqtt_config.get("enabled", False):
            logger.info("MQTT disabled or not available")
            return
            
        try:
            # Use WebSocket transport if configured, otherwise use standard TCP
            transport = "websockets" if self.mqtt_config.get("use_websockets", False) else "tcp"
            self.client = mqtt.Client(transport=transport)
            
            if transport == "websockets":
                logger.info("Using WebSocket transport for MQTT")
            
            # Configure TLS/SSL if enabled
            tls_config = self.mqtt_config.get("tls", {})
            if tls_config.get("enabled", False):
                tls_params = {
                    "cert_reqs": ssl.CERT_REQUIRED,
                    "tls_version": ssl.PROTOCOL_TLS,
                }
                
                # CA certificate for server verification (optional - uses system certs if not specified)
                ca_cert = tls_config.get("ca_cert")
                if ca_cert:
                    tls_params["ca_certs"] = ca_cert
                    logger.info("Using custom CA certificate for MQTT TLS")
                else:
                    logger.info("Using system default CA certificates for MQTT TLS")
                
                # Client certificate and key (for mutual TLS)
                client_cert = tls_config.get("client_cert")
                client_key = tls_config.get("client_key")
                if client_cert:
                    tls_params["certfile"] = client_cert
                if client_key:
                    tls_params["keyfile"] = client_key
                
                # Allow insecure connections (skip cert verification)
                if tls_config.get("insecure", False):
                    tls_params["cert_reqs"] = ssl.CERT_NONE
                    logger.warning("MQTT TLS certificate verification disabled (insecure mode)")
                
                self.client.tls_set(**tls_params)
                logger.info("MQTT TLS/SSL configured")
            
            username = self.mqtt_config.get("username")
            password = self.mqtt_config.get("password")
            if username:
                self.client.username_pw_set(username, password)
            
            broker = self.mqtt_config.get("broker", "localhost")
            port = self.mqtt_config.get("port", 1883)
            
            secure = "(TLS)" if tls_config.get("enabled", False) else ""
            logger.info(f"Connecting to MQTT broker {broker}:{port} {secure}...")
            
            self.client.connect(broker, port, 60)
            self.client.loop_start()
            logger.info(f"MQTT client successfully connected")
            
        except Exception as e:
            logger.error(f"Failed to initialize MQTT: {e}")
            self.client = None

    def publish(self, record: dict, record_type: str):
        """
        Publish record to MQTT.
        Packets MUST use PacketRecord format. Non-packet records use original format.
        
        Args:
            record: The record dictionary to publish
            record_type: Type of record (packet, advert, noise_floor, etc.)
        """
        if not self.client:
            return
            
        try:
            base_topic = self.mqtt_config.get("base_topic", "meshcore/repeater")
            topic = f"{base_topic}/{self.node_name}/{record_type}"

            if record_type == "packet":
                packet_record = PacketRecord.from_packet_record(
                    record, origin=self.node_name, origin_id=self.node_id
                )
                if not packet_record:
                    logger.debug(
                        "Skipping MQTT publish: packet missing required data for PacketRecord"
                    )
                    return

                payload = packet_record.to_dict()
                logger.debug("Publishing packet using PacketRecord format")
            else:
                payload = {k: v for k, v in record.items() if v is not None}
            
            message = json.dumps(payload, default=str)
            self.client.publish(topic, message, qos=0, retain=False)
            logger.debug(f"Published to {topic}")
            
        except Exception as e:
            logger.error(f"Failed to publish to MQTT: {e}")

    def close(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            logger.info("MQTT client disconnected")