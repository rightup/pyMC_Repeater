import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import rrdtool
    RRDTOOL_AVAILABLE = True
except ImportError:
    RRDTOOL_AVAILABLE = False

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False

logger = logging.getLogger("StorageCollector")


class StorageCollector:

    def __init__(self, config: dict):
        self.config = config
        self.storage_dir = Path(config.get("storage_dir", "/var/lib/pymc_repeater"))
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        self.sqlite_path = self.storage_dir / "repeater.db"
        self.rrd_path = self.storage_dir / "metrics.rrd"

        # MQTT configuration
        self.mqtt_config = config.get("mqtt", {})
        self.mqtt_client = None

        # Initialize storage systems
        self._init_sqlite()
        self._init_rrd()
        self._init_mqtt()

    def _init_sqlite(self):
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                # Packets table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS packets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        type INTEGER NOT NULL,
                        route INTEGER NOT NULL,
                        length INTEGER NOT NULL,
                        rssi INTEGER,
                        snr REAL,
                        score REAL,
                        transmitted BOOLEAN NOT NULL,
                        is_duplicate BOOLEAN NOT NULL,
                        drop_reason TEXT,
                        src_hash TEXT,
                        dst_hash TEXT,
                        path_hash TEXT
                    )
                """)
                
                # Adverts/neighbors table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS adverts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        pubkey TEXT NOT NULL,
                        node_name TEXT,
                        contact_type TEXT,
                        latitude REAL,
                        longitude REAL,
                        rssi INTEGER,
                        snr REAL,
                        is_new_neighbor BOOLEAN NOT NULL
                    )
                """)
                
                # Create indexes for performance
                conn.execute("CREATE INDEX IF NOT EXISTS idx_packets_timestamp ON packets(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_packets_type ON packets(type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_adverts_timestamp ON adverts(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_adverts_pubkey ON adverts(pubkey)")
                
                conn.commit()
                logger.info(f"SQLite database initialized: {self.sqlite_path}")
                
        except Exception as e:
            logger.error(f"Failed to initialize SQLite: {e}")

    def _init_rrd(self):
   
        if not RRDTOOL_AVAILABLE:
            logger.warning("RRDTool not available - skipping RRD initialization")
            return
            
        if self.rrd_path.exists():
            logger.info(f"RRD database exists: {self.rrd_path}")
            return
            
        try:
            # Create RRD with 1-minute resolution, keep 1 week of detailed data
            # and longer periods at reduced resolution
            rrdtool.create(
                str(self.rrd_path),
                "--step", "60",  # 1-minute steps
                "--start", str(int(time.time() - 60)),
                
                # Data sources
                "DS:rx_count:COUNTER:120:0:U",        # Received packets
                "DS:tx_count:COUNTER:120:0:U",        # Transmitted packets
                "DS:drop_count:COUNTER:120:0:U",      # Dropped packets
                "DS:avg_rssi:GAUGE:120:-200:0",       # Average RSSI
                "DS:avg_snr:GAUGE:120:-30:30",        # Average SNR
                "DS:avg_length:GAUGE:120:0:256",      # Average packet length
                "DS:avg_score:GAUGE:120:0:1",         # Average packet score
                "DS:neighbor_count:GAUGE:120:0:U",    # Number of neighbors
                
                # Round Robin Archives (resolution:keep_time)
                "RRA:AVERAGE:0.5:1:10080",    # 1min for 1 week
                "RRA:AVERAGE:0.5:5:8640",     # 5min for 1 month  
                "RRA:AVERAGE:0.5:60:8760",    # 1hour for 1 year
                "RRA:MAX:0.5:1:10080",        # 1min max values for 1 week
                "RRA:MIN:0.5:1:10080"         # 1min min values for 1 week
            )
            logger.info(f"RRD database created: {self.rrd_path}")
            
        except Exception as e:
            logger.error(f"Failed to create RRD database: {e}")

    def _init_mqtt(self):
 
        if not MQTT_AVAILABLE or not self.mqtt_config.get("enabled", False):
            logger.info("MQTT disabled or not available")
            return
            
        try:
            self.mqtt_client = mqtt.Client()
            
            # Configure authentication if provided
            username = self.mqtt_config.get("username")
            password = self.mqtt_config.get("password")
            if username:
                self.mqtt_client.username_pw_set(username, password)
            
            # Connect to broker
            broker = self.mqtt_config.get("broker", "localhost")
            port = self.mqtt_config.get("port", 1883)
            
            self.mqtt_client.connect(broker, port, 60)
            self.mqtt_client.loop_start()
            
            logger.info(f"MQTT client connected to {broker}:{port}")
            
        except Exception as e:
            logger.error(f"Failed to initialize MQTT: {e}")
            self.mqtt_client = None

    def record_packet(self, packet_record: dict):
 
        self._store_packet_sqlite(packet_record)
        self._update_rrd_metrics(packet_record, record_type="packet")
        self._publish_mqtt(packet_record, "packet")

    def record_advert(self, advert_record: dict):
 
        self._store_advert_sqlite(advert_record)
        self._update_rrd_metrics(advert_record, record_type="advert")
        self._publish_mqtt(advert_record, "advert")

    def _store_packet_sqlite(self, record: dict):

        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.execute("""
                    INSERT INTO packets (
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    record.get("timestamp", time.time()),
                    record.get("type", 0),
                    record.get("route", 0),
                    record.get("length", 0),
                    record.get("rssi"),
                    record.get("snr"),
                    record.get("score"),
                    record.get("transmitted", False),
                    record.get("is_duplicate", False),
                    record.get("drop_reason"),
                    record.get("src_hash"),
                    record.get("dst_hash"),
                    record.get("path_hash")
                ))
                
        except Exception as e:
            logger.error(f"Failed to store packet in SQLite: {e}")

    def _store_advert_sqlite(self, record: dict):
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.execute("""
                    INSERT INTO adverts (
                        timestamp, pubkey, node_name, contact_type, latitude,
                        longitude, rssi, snr, is_new_neighbor
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    record.get("timestamp", time.time()),
                    record.get("pubkey", ""),
                    record.get("node_name"),
                    record.get("contact_type"),
                    record.get("latitude"),
                    record.get("longitude"),
                    record.get("rssi"),
                    record.get("snr"),
                    record.get("is_new_neighbor", False)
                ))
                
        except Exception as e:
            logger.error(f"Failed to store advert in SQLite: {e}")

    def _update_rrd_metrics(self, record: dict, record_type: str):
        if not RRDTOOL_AVAILABLE or not self.rrd_path.exists():
            return
            
        try:
            # Get current timestamp
            timestamp = int(record.get("timestamp", time.time()))
            
            # Get current values from RRD (for counters we need to increment)
            try:
                info = rrdtool.info(str(self.rrd_path))
                last_update = int(info.get("last_update", timestamp - 60))
                
                # Skip if trying to update with old data
                if timestamp <= last_update:
                    return
                    
            except Exception:
                # If we can't read info, proceed with update
                pass
            
            # Prepare update values based on record type
            if record_type == "packet":
                # For packets, we update counters and gauges
                rx_inc = 1
                tx_inc = 1 if record.get("transmitted", False) else 0
                drop_inc = 0 if record.get("transmitted", False) else 1
                
                values = f"{timestamp}:{rx_inc}:{tx_inc}:{drop_inc}:" \
                        f"{record.get('rssi', 'U')}:{record.get('snr', 'U')}:" \
                        f"{record.get('length', 'U')}:{record.get('score', 'U')}:U"
                        
            elif record_type == "advert":
                # For adverts, we mainly update gauges
                values = f"{timestamp}:0:0:0:" \
                        f"{record.get('rssi', 'U')}:{record.get('snr', 'U')}:" \
                        f"U:U:1"
            else:
                return
                
            rrdtool.update(str(self.rrd_path), values)
            
        except Exception as e:
            logger.error(f"Failed to update RRD metrics: {e}")

    def _publish_mqtt(self, record: dict, record_type: str):
        """Publish record to MQTT broker."""
        if not self.mqtt_client:
            return
            
        try:
            base_topic = self.mqtt_config.get("base_topic", "meshcore/repeater")
            node_name = self.config.get("repeater", {}).get("node_name", "unknown")
            
            topic = f"{base_topic}/{node_name}/{record_type}"
            
            # Create clean payload (remove non-serializable items)
            payload = {k: v for k, v in record.items() if v is not None}
            
            # Convert to JSON
            message = json.dumps(payload, default=str)
            
            # Publish
            self.mqtt_client.publish(topic, message, qos=0, retain=False)
            
        except Exception as e:
            logger.error(f"Failed to publish to MQTT: {e}")

    def get_packet_stats(self, hours: int = 24) -> dict:
        try:
            cutoff = time.time() - (hours * 3600)
            
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                
                # Basic counts
                stats = conn.execute("""
                    SELECT 
                        COUNT(*) as total_packets,
                        SUM(transmitted) as transmitted_packets,
                        SUM(CASE WHEN transmitted = 0 THEN 1 ELSE 0 END) as dropped_packets,
                        AVG(rssi) as avg_rssi,
                        AVG(snr) as avg_snr,
                        AVG(score) as avg_score
                    FROM packets 
                    WHERE timestamp > ?
                """, (cutoff,)).fetchone()
                
                # Packet types
                types = conn.execute("""
                    SELECT type, COUNT(*) as count
                    FROM packets 
                    WHERE timestamp > ?
                    GROUP BY type
                    ORDER BY count DESC
                """, (cutoff,)).fetchall()
                
                return {
                    "total_packets": stats["total_packets"],
                    "transmitted_packets": stats["transmitted_packets"],
                    "dropped_packets": stats["dropped_packets"],
                    "avg_rssi": round(stats["avg_rssi"] or 0, 1),
                    "avg_snr": round(stats["avg_snr"] or 0, 1),
                    "avg_score": round(stats["avg_score"] or 0, 3),
                    "packet_types": [{"type": row["type"], "count": row["count"]} for row in types]
                }
                
        except Exception as e:
            logger.error(f"Failed to get packet stats: {e}")
            return {}

    def cleanup_old_data(self, days: int = 7):
        try:
            cutoff = time.time() - (days * 24 * 3600)
            
            with sqlite3.connect(self.sqlite_path) as conn:
                # Clean old packets
                result = conn.execute("DELETE FROM packets WHERE timestamp < ?", (cutoff,))
                packets_deleted = result.rowcount
                
                # Clean old adverts
                result = conn.execute("DELETE FROM adverts WHERE timestamp < ?", (cutoff,))
                adverts_deleted = result.rowcount
                
                conn.commit()
                
                if packets_deleted > 0 or adverts_deleted > 0:
                    logger.info(f"Cleaned up {packets_deleted} old packets and {adverts_deleted} old adverts")
                    
        except Exception as e:
            logger.error(f"Failed to cleanup old data: {e}")

    def close(self):
        """Clean shutdown of storage systems."""
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            logger.info("MQTT client disconnected")
