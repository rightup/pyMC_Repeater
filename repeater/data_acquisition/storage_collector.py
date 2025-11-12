import json
import logging
import time
from pathlib import Path
from typing import Optional, Dict, Any

from .sqlite_handler import SQLiteHandler
from .rrdtool_handler import RRDToolHandler
from .mqtt_handler import MQTTHandler

logger = logging.getLogger("StorageCollector")


class StorageCollector:
    def __init__(self, config: dict):
        self.config = config
        self.storage_dir = Path(config.get("storage_dir", "/var/lib/pymc_repeater"))
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        node_name = config.get("repeater", {}).get("node_name", "unknown")
        
        self.sqlite_handler = SQLiteHandler(self.storage_dir)
        self.rrd_handler = RRDToolHandler(self.storage_dir)
        self.mqtt_handler = MQTTHandler(config.get("mqtt", {}), node_name)

    def record_packet(self, packet_record: dict):
        logger.debug(f"Recording packet: type={packet_record.get('type')}, transmitted={packet_record.get('transmitted')}")
        
        self.sqlite_handler.store_packet(packet_record)
        
        cumulative_counts = self.sqlite_handler.get_cumulative_counts()
        self.rrd_handler.update_packet_metrics(packet_record, cumulative_counts)
        self.mqtt_handler.publish(packet_record, "packet")

    def record_advert(self, advert_record: dict):
        self.sqlite_handler.store_advert(advert_record)
        self.mqtt_handler.publish(advert_record, "advert")

    def record_noise_floor(self, noise_floor_dbm: float):
        noise_record = {
            "timestamp": time.time(),
            "noise_floor_dbm": noise_floor_dbm
        }
        self.sqlite_handler.store_noise_floor(noise_record)
        self.mqtt_handler.publish(noise_record, "noise_floor")

    def get_packet_stats(self, hours: int = 24) -> dict:
        return self.sqlite_handler.get_packet_stats(hours)

    def get_recent_packets(self, limit: int = 100) -> list:
        return self.sqlite_handler.get_recent_packets(limit)

    def get_filtered_packets(self, 
                           packet_type: Optional[int] = None,
                           route: Optional[int] = None,
                           start_timestamp: Optional[float] = None,
                           end_timestamp: Optional[float] = None,
                           limit: int = 1000) -> list:
        return self.sqlite_handler.get_filtered_packets(
            packet_type, route, start_timestamp, end_timestamp, limit
        )

    def get_packet_by_hash(self, packet_hash: str) -> Optional[dict]:
        return self.sqlite_handler.get_packet_by_hash(packet_hash)

    def get_rrd_data(self, start_time: Optional[int] = None, end_time: Optional[int] = None, 
                     resolution: str = "average") -> Optional[dict]:
        return self.rrd_handler.get_data(start_time, end_time, resolution)

    def get_packet_type_stats(self, hours: int = 24) -> dict:
        rrd_stats = self.rrd_handler.get_packet_type_stats(hours)
        if rrd_stats:
            return rrd_stats
        
        logger.warning("Falling back to SQLite for packet type stats")
        return self.sqlite_handler.get_packet_type_stats(hours)

    def get_route_stats(self, hours: int = 24) -> dict:
        return self.sqlite_handler.get_route_stats(hours)

    def get_neighbors(self) -> dict:
        return self.sqlite_handler.get_neighbors()

    def cleanup_old_data(self, days: int = 7):
        self.sqlite_handler.cleanup_old_data(days)

    def get_noise_floor_history(self, hours: int = 24) -> list:
        return self.sqlite_handler.get_noise_floor_history(hours)

    def get_noise_floor_stats(self, hours: int = 24) -> dict:
        return self.sqlite_handler.get_noise_floor_stats(hours)

    def close(self):
        self.mqtt_handler.close()