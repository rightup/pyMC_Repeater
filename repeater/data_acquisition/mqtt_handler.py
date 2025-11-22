import json
import logging
from typing import Dict, Any, Optional

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
            self.client = mqtt.Client()
            
            username = self.mqtt_config.get("username")
            password = self.mqtt_config.get("password")
            if username:
                self.client.username_pw_set(username, password)
            
            broker = self.mqtt_config.get("broker", "localhost")
            port = self.mqtt_config.get("port", 1883)
            
            self.client.connect(broker, port, 60)
            self.client.loop_start()
            
            logger.info(f"MQTT client connected to {broker}:{port}")
            
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
                    record,
                    origin=self.node_name,
                    origin_id=self.node_id
                )
                if not packet_record:
                    logger.debug("Skipping MQTT publish: packet missing required data for PacketRecord")
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