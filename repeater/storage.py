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
                        path_hash TEXT,
                        header TEXT,
                        payload TEXT,
                        payload_length INTEGER,
                        tx_delay_ms REAL,
                        packet_hash TEXT,
                        original_path TEXT,
                        forwarded_path TEXT
                    )
                """)
                
                # Adverts/neighbors table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS adverts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        pubkey TEXT NOT NULL,
                        node_name TEXT,
                        is_repeater BOOLEAN NOT NULL,
                        route_type INTEGER,
                        contact_type TEXT,
                        latitude REAL,
                        longitude REAL,
                        first_seen REAL NOT NULL,
                        last_seen REAL NOT NULL,
                        rssi INTEGER,
                        snr REAL,
                        advert_count INTEGER NOT NULL DEFAULT 1,
                        is_new_neighbor BOOLEAN NOT NULL
                    )
                """)
                
                # Create indexes for performance
                conn.execute("CREATE INDEX IF NOT EXISTS idx_packets_timestamp ON packets(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_packets_type ON packets(type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_packets_hash ON packets(packet_hash)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_packets_transmitted ON packets(transmitted)")
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
                
                # Data sources - Basic metrics
                "DS:rx_count:COUNTER:120:0:U",        # Received packets
                "DS:tx_count:COUNTER:120:0:U",        # Transmitted packets
                "DS:drop_count:COUNTER:120:0:U",      # Dropped packets
                "DS:avg_rssi:GAUGE:120:-200:0",       # Average RSSI
                "DS:avg_snr:GAUGE:120:-30:30",        # Average SNR
                "DS:avg_length:GAUGE:120:0:256",      # Average packet length
                "DS:avg_score:GAUGE:120:0:1",         # Average packet score
                "DS:neighbor_count:GAUGE:120:0:U",    # Number of neighbors
                
                # Packet type counters (based on pyMC payload types)
                "DS:type_0:COUNTER:120:0:U",          # Request (PAYLOAD_TYPE_REQ)
                "DS:type_1:COUNTER:120:0:U",          # Response (PAYLOAD_TYPE_RESPONSE)
                "DS:type_2:COUNTER:120:0:U",          # Text Message (PAYLOAD_TYPE_TXT_MSG)
                "DS:type_3:COUNTER:120:0:U",          # ACK (PAYLOAD_TYPE_ACK)
                "DS:type_4:COUNTER:120:0:U",          # Advert (PAYLOAD_TYPE_ADVERT)
                "DS:type_5:COUNTER:120:0:U",          # Group Text (PAYLOAD_TYPE_GRP_TXT)
                "DS:type_6:COUNTER:120:0:U",          # Group Data (PAYLOAD_TYPE_GRP_DATA)
                "DS:type_7:COUNTER:120:0:U",          # Anonymous Request (PAYLOAD_TYPE_ANON_REQ)
                "DS:type_8:COUNTER:120:0:U",          # Path (PAYLOAD_TYPE_PATH)
                "DS:type_9:COUNTER:120:0:U",          # Trace (PAYLOAD_TYPE_TRACE)
                "DS:type_10:COUNTER:120:0:U",         # Reserved for future use
                "DS:type_11:COUNTER:120:0:U",         # Reserved for future use
                "DS:type_12:COUNTER:120:0:U",         # Reserved for future use
                "DS:type_13:COUNTER:120:0:U",         # Reserved for future use
                "DS:type_14:COUNTER:120:0:U",         # Reserved for future use
                "DS:type_15:COUNTER:120:0:U",         # Reserved for future use
                "DS:type_other:COUNTER:120:0:U",      # Other packet types (>15)
                
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
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        header, payload, payload_length, tx_delay_ms, packet_hash,
                        original_path, forwarded_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    record.get("path_hash"),
                    record.get("header"),
                    record.get("payload"),
                    record.get("payload_length"),
                    record.get("tx_delay_ms"),
                    record.get("packet_hash"),
                    record.get("original_path"),
                    record.get("forwarded_path")
                ))
                
        except Exception as e:
            logger.error(f"Failed to store packet in SQLite: {e}")

    def _store_advert_sqlite(self, record: dict):
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                # Check if this pubkey already exists
                existing = conn.execute(
                    "SELECT pubkey, first_seen, advert_count FROM adverts WHERE pubkey = ? ORDER BY last_seen DESC LIMIT 1",
                    (record.get("pubkey", ""),)
                ).fetchone()
                
                current_time = record.get("timestamp", time.time())
                
                if existing:
                    # Update existing neighbor
                    conn.execute("""
                        UPDATE adverts 
                        SET timestamp = ?, node_name = ?, is_repeater = ?, route_type = ?,
                            contact_type = ?, latitude = ?, longitude = ?, last_seen = ?,
                            rssi = ?, snr = ?, advert_count = advert_count + 1, is_new_neighbor = 0
                        WHERE pubkey = ?
                    """, (
                        current_time,
                        record.get("node_name"),
                        record.get("is_repeater", False),
                        record.get("route_type"),
                        record.get("contact_type"),
                        record.get("latitude"),
                        record.get("longitude"),
                        current_time,
                        record.get("rssi"),
                        record.get("snr"),
                        record.get("pubkey", "")
                    ))
                else:
                    # Insert new neighbor
                    conn.execute("""
                        INSERT INTO adverts (
                            timestamp, pubkey, node_name, is_repeater, route_type, contact_type, 
                            latitude, longitude, first_seen, last_seen, rssi, snr, advert_count, is_new_neighbor
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        current_time,
                        record.get("pubkey", ""),
                        record.get("node_name"),
                        record.get("is_repeater", False),
                        record.get("route_type"),
                        record.get("contact_type"),
                        record.get("latitude"),
                        record.get("longitude"),
                        current_time,  # first_seen
                        current_time,  # last_seen
                        record.get("rssi"),
                        record.get("snr"),
                        1,  # advert_count
                        True  # is_new_neighbor
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
                # Get packet type for counter tracking
                packet_type = record.get("type", 0)
                
                # For packets, we update counters and gauges
                rx_inc = 1
                tx_inc = 1 if record.get("transmitted", False) else 0
                drop_inc = 0 if record.get("transmitted", False) else 1
                
                # Initialize packet type counters (all start with 0)
                type_counters = ["0"] * 17  # type_0 through type_15 plus type_other
                
                # Increment the appropriate packet type counter
                if 0 <= packet_type <= 15:
                    type_counters[packet_type] = "1"
                else:
                    type_counters[16] = "1"  # type_other for packet types > 15
                
                # Build the values string: basic metrics + packet type counters
                basic_values = f"{timestamp}:{rx_inc}:{tx_inc}:{drop_inc}:" \
                              f"{record.get('rssi', 'U')}:{record.get('snr', 'U')}:" \
                              f"{record.get('length', 'U')}:{record.get('score', 'U')}:U"
                
                type_values = ":".join(type_counters)
                values = f"{basic_values}:{type_values}"
                        
            elif record_type == "advert":
                # For adverts, we mainly update gauges, packet type counters stay at 0
                type_counters = ["0"] * 17  # All packet type counters set to 0
                type_values = ":".join(type_counters)
                
                basic_values = f"{timestamp}:0:0:0:" \
                              f"{record.get('rssi', 'U')}:{record.get('snr', 'U')}:" \
                              f"U:U:1"
                
                values = f"{basic_values}:{type_values}"
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
                        AVG(score) as avg_score,
                        AVG(payload_length) as avg_payload_length,
                        AVG(tx_delay_ms) as avg_tx_delay
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
                
                # Drop reasons
                drop_reasons = conn.execute("""
                    SELECT drop_reason, COUNT(*) as count
                    FROM packets 
                    WHERE timestamp > ? AND transmitted = 0 AND drop_reason IS NOT NULL
                    GROUP BY drop_reason
                    ORDER BY count DESC
                """, (cutoff,)).fetchall()
                
                return {
                    "total_packets": stats["total_packets"],
                    "transmitted_packets": stats["transmitted_packets"],
                    "dropped_packets": stats["dropped_packets"],
                    "avg_rssi": round(stats["avg_rssi"] or 0, 1),
                    "avg_snr": round(stats["avg_snr"] or 0, 1),
                    "avg_score": round(stats["avg_score"] or 0, 3),
                    "avg_payload_length": round(stats["avg_payload_length"] or 0, 1),
                    "avg_tx_delay": round(stats["avg_tx_delay"] or 0, 1),
                    "packet_types": [{"type": row["type"], "count": row["count"]} for row in types],
                    "drop_reasons": [{"reason": row["drop_reason"], "count": row["count"]} for row in drop_reasons]
                }
                
        except Exception as e:
            logger.error(f"Failed to get packet stats: {e}")
            return {}

    def get_recent_packets(self, limit: int = 100) -> list:
        """Get recent packets with all fields for debugging/analysis."""
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                
                packets = conn.execute("""
                    SELECT 
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        header, payload, payload_length, tx_delay_ms, packet_hash,
                        original_path, forwarded_path
                    FROM packets 
                    ORDER BY timestamp DESC
                    LIMIT ?
                """, (limit,)).fetchall()
                
                return [dict(row) for row in packets]
                
        except Exception as e:
            logger.error(f"Failed to get recent packets: {e}")
            return []

    def get_filtered_packets(self, 
                           packet_type: Optional[int] = None,
                           route: Optional[int] = None,
                           start_timestamp: Optional[float] = None,
                           end_timestamp: Optional[float] = None,
                           limit: int = 1000) -> list:
        """Get packets filtered by type, route, and timestamp range."""
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                
                # Build dynamic query based on filters
                where_clauses = []
                params = []
                
                if packet_type is not None:
                    where_clauses.append("type = ?")
                    params.append(packet_type)
                
                if route is not None:
                    where_clauses.append("route = ?")
                    params.append(route)
                
                if start_timestamp is not None:
                    where_clauses.append("timestamp >= ?")
                    params.append(start_timestamp)
                
                if end_timestamp is not None:
                    where_clauses.append("timestamp <= ?")
                    params.append(end_timestamp)
                
                # Build the complete query
                base_query = """
                    SELECT 
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        header, payload, payload_length, tx_delay_ms, packet_hash,
                        original_path, forwarded_path
                    FROM packets
                """
                
                if where_clauses:
                    query = f"{base_query} WHERE {' AND '.join(where_clauses)}"
                else:
                    query = base_query
                
                query += " ORDER BY timestamp DESC LIMIT ?"
                params.append(limit)
                
                packets = conn.execute(query, params).fetchall()
                
                return [dict(row) for row in packets]
                
        except Exception as e:
            logger.error(f"Failed to get filtered packets: {e}")
            return []

    def get_packet_by_hash(self, packet_hash: str) -> Optional[dict]:
        """Get a specific packet by its hash."""
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                
                packet = conn.execute("""
                    SELECT 
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        header, payload, payload_length, tx_delay_ms, packet_hash,
                        original_path, forwarded_path
                    FROM packets 
                    WHERE packet_hash = ?
                """, (packet_hash,)).fetchone()
                
                return dict(packet) if packet else None
                
        except Exception as e:
            logger.error(f"Failed to get packet by hash: {e}")
            return None

    def get_rrd_data(self, start_time: Optional[int] = None, end_time: Optional[int] = None, 
                     resolution: str = "average") -> Optional[dict]:
        """Get RRD time series data including packet type statistics."""
        if not RRDTOOL_AVAILABLE or not self.rrd_path.exists():
            return None
            
        try:
            # Default to last 24 hours if no time specified
            if end_time is None:
                end_time = int(time.time())
            if start_time is None:
                start_time = end_time - (24 * 3600)  # 24 hours ago
                
            # Fetch data from RRD
            fetch_result = rrdtool.fetch(
                str(self.rrd_path),
                resolution.upper(),
                "--start", str(start_time),
                "--end", str(end_time)
            )
            
            if not fetch_result:
                return None
                
            (start, end, step), data_sources, data_points = fetch_result
            
            # Create structured response
            result = {
                "start_time": start,
                "end_time": end,
                "step": step,
                "data_sources": data_sources,
                "packet_types": {},
                "metrics": {}
            }
            
            # Process data points
            timestamps = []
            current_time = start
            
            # Initialize data arrays
            for ds in data_sources:
                if ds.startswith('type_'):
                    if 'packet_types' not in result:
                        result['packet_types'] = {}
                    result['packet_types'][ds] = []
                else:
                    result['metrics'][ds] = []
            
            # Process each data point
            for point in data_points:
                timestamps.append(current_time)
                
                for i, value in enumerate(point):
                    ds_name = data_sources[i]
                    if ds_name.startswith('type_'):
                        result['packet_types'][ds_name].append(value)
                    else:
                        result['metrics'][ds_name].append(value)
                        
                current_time += step
            
            result['timestamps'] = timestamps
            return result
            
        except Exception as e:
            logger.error(f"Failed to get RRD data: {e}")
            return None

    def get_packet_type_stats(self, hours: int = 24) -> dict:
        """Get packet type statistics for the specified time period."""
        try:
            # Get RRD data for packet types
            end_time = int(time.time())
            start_time = end_time - (hours * 3600)
            
            rrd_data = self.get_rrd_data(start_time, end_time)
            if not rrd_data or 'packet_types' not in rrd_data:
                return {"error": "No RRD data available"}
            
            # Calculate totals for each packet type
            type_totals = {}
            packet_type_names = {
                'type_0': 'Request (REQ)',
                'type_1': 'Response (RESPONSE)', 
                'type_2': 'Text Message (TXT_MSG)',
                'type_3': 'ACK (ACK)',
                'type_4': 'Advert (ADVERT)',
                'type_5': 'Group Text (GRP_TXT)',
                'type_6': 'Group Data (GRP_DATA)',
                'type_7': 'Anonymous Request (ANON_REQ)',
                'type_8': 'Path (PATH)',
                'type_9': 'Trace (TRACE)',
                'type_10': 'Reserved Type 10',
                'type_11': 'Reserved Type 11',
                'type_12': 'Reserved Type 12',
                'type_13': 'Reserved Type 13',
                'type_14': 'Reserved Type 14',
                'type_15': 'Reserved Type 15',
                'type_other': 'Other Types (>15)'
            }
            
            for type_key, data_points in rrd_data['packet_types'].items():
                # Calculate total (last value minus first value for counter data)
                valid_points = [p for p in data_points if p is not None]
                if len(valid_points) >= 2:
                    total = valid_points[-1] - valid_points[0]
                else:
                    total = valid_points[0] if valid_points else 0
                    
                type_name = packet_type_names.get(type_key, type_key)
                type_totals[type_name] = max(0, total or 0)
            
            return {
                "hours": hours,
                "packet_type_totals": type_totals,
                "total_packets": sum(type_totals.values()),
                "period": f"{hours} hours"
            }
            
        except Exception as e:
            logger.error(f"Failed to get packet type stats: {e}")
            return {"error": str(e)}

    def get_neighbors(self) -> dict:
        """Get all neighbors from the database formatted like the in-memory neighbors dict."""
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                
                # Get the most recent record for each pubkey
                neighbors = conn.execute("""
                    SELECT pubkey, node_name, is_repeater, route_type, contact_type,
                           latitude, longitude, first_seen, last_seen, rssi, snr, advert_count
                    FROM adverts a1
                    WHERE last_seen = (
                        SELECT MAX(last_seen) 
                        FROM adverts a2 
                        WHERE a2.pubkey = a1.pubkey
                    )
                    ORDER BY last_seen DESC
                """).fetchall()
                
                # Convert to the same format as the in-memory neighbors dict
                result = {}
                for row in neighbors:
                    result[row["pubkey"]] = {
                        "node_name": row["node_name"],
                        "is_repeater": bool(row["is_repeater"]),
                        "route_type": row["route_type"],
                        "contact_type": row["contact_type"],
                        "latitude": row["latitude"],
                        "longitude": row["longitude"],
                        "first_seen": row["first_seen"],
                        "last_seen": row["last_seen"],
                        "rssi": row["rssi"],
                        "snr": row["snr"],
                        "advert_count": row["advert_count"],
                    }
                
                return result
                
        except Exception as e:
            logger.error(f"Failed to get neighbors: {e}")
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