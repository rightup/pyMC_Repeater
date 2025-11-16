import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger("SQLiteHandler")


class SQLiteHandler:
    def __init__(self, storage_dir: Path):
        self.storage_dir = storage_dir
        self.sqlite_path = self.storage_dir / "repeater.db"
        self._init_database()

    def _init_database(self):
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
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
                        transport_codes TEXT,
                        payload TEXT,
                        payload_length INTEGER,
                        tx_delay_ms REAL,
                        packet_hash TEXT,
                        original_path TEXT,
                        forwarded_path TEXT,
                        raw_packet TEXT
                    )
                """)
                
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
                
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS noise_floor (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        noise_floor_dbm REAL NOT NULL
                    )
                """)
                
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS transport_keys (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        flood_policy TEXT NOT NULL CHECK (flood_policy IN ('allow', 'deny')),
                        transport_key TEXT NOT NULL,
                        last_used REAL,
                        parent_id INTEGER,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        FOREIGN KEY (parent_id) REFERENCES transport_keys(id)
                    )
                """)
                
                conn.execute("CREATE INDEX IF NOT EXISTS idx_packets_timestamp ON packets(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_packets_type ON packets(type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_packets_hash ON packets(packet_hash)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_packets_transmitted ON packets(transmitted)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_adverts_timestamp ON adverts(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_adverts_pubkey ON adverts(pubkey)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_noise_timestamp ON noise_floor(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_transport_keys_name ON transport_keys(name)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_transport_keys_parent ON transport_keys(parent_id)")
                
                conn.commit()
                logger.info(f"SQLite database initialized: {self.sqlite_path}")
                
        except Exception as e:
            logger.error(f"Failed to initialize SQLite: {e}")

    def store_packet(self, record: dict):
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                orig_path = record.get("original_path")
                fwd_path = record.get("forwarded_path")
                try:
                    orig_path_val = json.dumps(orig_path) if orig_path is not None else None
                except Exception:
                    orig_path_val = str(orig_path)
                try:
                    fwd_path_val = json.dumps(fwd_path) if fwd_path is not None else None
                except Exception:
                    fwd_path_val = str(fwd_path)

                conn.execute("""
                    INSERT INTO packets (
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        header, transport_codes, payload, payload_length, 
                        tx_delay_ms, packet_hash, original_path, forwarded_path, raw_packet
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    record.get("timestamp", time.time()),
                    record.get("type", 0),
                    record.get("route", 0),
                    record.get("length", 0),
                    record.get("rssi"),
                    record.get("snr"),
                    record.get("score"),
                    int(bool(record.get("transmitted", False))),
                    int(bool(record.get("is_duplicate", False))),
                    record.get("drop_reason"),
                    record.get("src_hash"),
                    record.get("dst_hash"),
                    record.get("path_hash"),
                    record.get("header"),
                    record.get("transport_codes"),
                    record.get("payload"),
                    record.get("payload_length"),
                    record.get("tx_delay_ms"),
                    record.get("packet_hash"),
                    orig_path_val,
                    fwd_path_val,
                    record.get("raw_packet")
                ))
                
        except Exception as e:
            logger.error(f"Failed to store packet in SQLite: {e}")

    def store_advert(self, record: dict):
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                existing = conn.execute(
                    "SELECT pubkey, first_seen, advert_count FROM adverts WHERE pubkey = ? ORDER BY last_seen DESC LIMIT 1",
                    (record.get("pubkey", ""),)
                ).fetchone()
                
                current_time = record.get("timestamp", time.time())
                
                if existing:
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
                        current_time,
                        current_time,
                        record.get("rssi"),
                        record.get("snr"),
                        1,
                        True
                    ))
                
        except Exception as e:
            logger.error(f"Failed to store advert in SQLite: {e}")

    def store_noise_floor(self, record: dict):
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.execute("""
                    INSERT INTO noise_floor (timestamp, noise_floor_dbm)
                    VALUES (?, ?)
                """, (
                    record.get("timestamp", time.time()),
                    record.get("noise_floor_dbm")
                ))
        except Exception as e:
            logger.error(f"Failed to store noise floor in SQLite: {e}")

    def get_packet_stats(self, hours: int = 24) -> dict:
        try:
            cutoff = time.time() - (hours * 3600)
            
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                
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
                
                types = conn.execute("""
                    SELECT type, COUNT(*) as count
                    FROM packets 
                    WHERE timestamp > ?
                    GROUP BY type
                    ORDER BY count DESC
                """, (cutoff,)).fetchall()
                
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
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                
                packets = conn.execute("""
                    SELECT 
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        header, transport_codes, payload, payload_length, 
                        tx_delay_ms, packet_hash, original_path, forwarded_path, raw_packet
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
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                
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
                
                base_query = """
                    SELECT 
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        header, transport_codes, payload, payload_length, 
                        tx_delay_ms, packet_hash, original_path, forwarded_path, raw_packet
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
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                
                packet = conn.execute("""
                    SELECT 
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        header, transport_codes, payload, payload_length, 
                        tx_delay_ms, packet_hash, original_path, forwarded_path, raw_packet
                    FROM packets 
                    WHERE packet_hash = ?
                """, (packet_hash,)).fetchone()
                
                return dict(packet) if packet else None
                
        except Exception as e:
            logger.error(f"Failed to get packet by hash: {e}")
            return None

    def get_packet_type_stats(self, hours: int = 24) -> dict:
        try:
            cutoff = time.time() - (hours * 3600)
            
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                
                type_counts = {}
                packet_type_names = {
                    0: 'Request (REQ)', 1: 'Response (RESPONSE)', 
                    2: 'Plain Text Message (TXT_MSG)', 3: 'Acknowledgment (ACK)',
                    4: 'Node Advertisement (ADVERT)', 5: 'Group Text Message (GRP_TXT)',
                    6: 'Group Datagram (GRP_DATA)', 7: 'Anonymous Request (ANON_REQ)',
                    8: 'Returned Path (PATH)', 9: 'Trace (TRACE)',
                    10: 'Multi-part Packet', 11: 'Reserved Type 11',
                    12: 'Reserved Type 12', 13: 'Reserved Type 13',
                    14: 'Reserved Type 14', 15: 'Custom Packet (RAW_CUSTOM)'
                }
                
                for packet_type in range(16):
                    count = conn.execute(
                        "SELECT COUNT(*) FROM packets WHERE type = ? AND timestamp > ?", 
                        (packet_type, cutoff)
                    ).fetchone()[0]
                    
                    type_name = packet_type_names.get(packet_type, f'Type {packet_type}')
                    if count > 0:
                        type_counts[type_name] = count
                
                other_count = conn.execute(
                    "SELECT COUNT(*) FROM packets WHERE type > 15 AND timestamp > ?", 
                    (cutoff,)
                ).fetchone()[0]
                if other_count > 0:
                    type_counts['Other Types (>15)'] = other_count
                
                return {
                    "hours": hours,
                    "packet_type_totals": type_counts,
                    "total_packets": sum(type_counts.values()),
                    "period": f"{hours} hours",
                    "data_source": "sqlite"
                }
                
        except Exception as e:
            logger.error(f"Failed to get packet type stats from SQLite: {e}")
            return {"error": str(e), "data_source": "error"}

    def get_route_stats(self, hours: int = 24) -> dict:

        try:
            cutoff = time.time() - (hours * 3600)
            
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                
                route_counts = {}
                route_names = {
                    0: 'Transport Flood',
                    1: 'Flood', 
                    2: 'Direct',
                    3: 'Transport Direct'
                }

                for route_type in range(4):
                    count = conn.execute(
                        "SELECT COUNT(*) FROM packets WHERE route = ? AND timestamp > ?", 
                        (route_type, cutoff)
                    ).fetchone()[0]
                    
                    route_name = route_names.get(route_type, f'Route {route_type}')
                    if count > 0:
                        route_counts[route_name] = count
                
                # Count any other route types > 3
                other_count = conn.execute(
                    "SELECT COUNT(*) FROM packets WHERE route > 3 AND timestamp > ?", 
                    (cutoff,)
                ).fetchone()[0]
                if other_count > 0:
                    route_counts['Other Routes (>3)'] = other_count
                
                return {
                    "hours": hours,
                    "route_totals": route_counts,
                    "total_packets": sum(route_counts.values()),
                    "period": f"{hours} hours",
                    "data_source": "sqlite"
                }
                
        except Exception as e:
            logger.error(f"Failed to get route stats from SQLite: {e}")
            return {"error": str(e), "data_source": "error"}

    def get_neighbors(self) -> dict:
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                
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

    def get_noise_floor_history(self, hours: int = 24) -> list:
        try:
            cutoff = time.time() - (hours * 3600)
            
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                
                measurements = conn.execute("""
                    SELECT timestamp, noise_floor_dbm
                    FROM noise_floor 
                    WHERE timestamp > ?
                    ORDER BY timestamp ASC
                """, (cutoff,)).fetchall()
                
                return [{"timestamp": row["timestamp"], "noise_floor_dbm": row["noise_floor_dbm"]} 
                        for row in measurements]
                
        except Exception as e:
            logger.error(f"Failed to get noise floor history: {e}")
            return []

    def get_noise_floor_stats(self, hours: int = 24) -> dict:
        try:
            cutoff = time.time() - (hours * 3600)
            
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                
                stats = conn.execute("""
                    SELECT 
                        COUNT(*) as measurement_count,
                        AVG(noise_floor_dbm) as avg_noise_floor,
                        MIN(noise_floor_dbm) as min_noise_floor,
                        MAX(noise_floor_dbm) as max_noise_floor
                    FROM noise_floor 
                    WHERE timestamp > ?
                """, (cutoff,)).fetchone()
                
                return {
                    "measurement_count": stats["measurement_count"],
                    "avg_noise_floor": round(stats["avg_noise_floor"] or 0, 1),
                    "min_noise_floor": round(stats["min_noise_floor"] or 0, 1),
                    "max_noise_floor": round(stats["max_noise_floor"] or 0, 1),
                    "hours": hours
                }
                
        except Exception as e:
            logger.error(f"Failed to get noise floor stats: {e}")
            return {}

    def cleanup_old_data(self, days: int = 7):
        try:
            cutoff = time.time() - (days * 24 * 3600)
            
            with sqlite3.connect(self.sqlite_path) as conn:
                result = conn.execute("DELETE FROM packets WHERE timestamp < ?", (cutoff,))
                packets_deleted = result.rowcount
                
                result = conn.execute("DELETE FROM adverts WHERE timestamp < ?", (cutoff,))
                adverts_deleted = result.rowcount
                
                result = conn.execute("DELETE FROM noise_floor WHERE timestamp < ?", (cutoff,))
                noise_deleted = result.rowcount
                
                conn.commit()
                
                if packets_deleted > 0 or adverts_deleted > 0 or noise_deleted > 0:
                    logger.info(f"Cleaned up {packets_deleted} old packets, {adverts_deleted} old adverts, {noise_deleted} old noise measurements")
                    
        except Exception as e:
            logger.error(f"Failed to cleanup old data: {e}")

    def get_cumulative_counts(self) -> dict:
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                type_counts = {}
                for i in range(16):
                    count = conn.execute("SELECT COUNT(*) FROM packets WHERE type = ?", (i,)).fetchone()[0]
                    type_counts[f"type_{i}"] = count
                
                other_count = conn.execute("SELECT COUNT(*) FROM packets WHERE type > 15").fetchone()[0]
                type_counts["type_other"] = other_count
                
                rx_total = conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
                tx_total = conn.execute("SELECT COUNT(*) FROM packets WHERE transmitted = 1").fetchone()[0] 
                drop_total = conn.execute("SELECT COUNT(*) FROM packets WHERE transmitted = 0").fetchone()[0]
                
                return {
                    "rx_total": rx_total,
                    "tx_total": tx_total,
                    "drop_total": drop_total,
                    "type_counts": type_counts
                }
                
        except Exception as e:
            logger.error(f"Failed to get cumulative counts: {e}")
            return {
                "rx_total": 0,
                "tx_total": 0,
                "drop_total": 0,
                "type_counts": {}
            }

    def get_adverts_by_contact_type(self, contact_type: str, limit: Optional[int] = None, hours: Optional[int] = None) -> List[dict]:
  
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                
                query = """
                    SELECT id, timestamp, pubkey, node_name, is_repeater, route_type, 
                           contact_type, latitude, longitude, first_seen, last_seen, 
                           rssi, snr, advert_count, is_new_neighbor
                    FROM adverts 
                    WHERE contact_type = ?
                """
                params = [contact_type]
                
                if hours is not None:
                    cutoff = time.time() - (hours * 3600)
                    query += " AND timestamp > ?"
                    params.append(cutoff)
                
                query += " ORDER BY timestamp DESC"
                
                if limit is not None:
                    query += " LIMIT ?"
                    params.append(limit)
                
                rows = conn.execute(query, params).fetchall()
                
                adverts = []
                for row in rows:
                    advert = {
                        "id": row["id"],
                        "timestamp": row["timestamp"],
                        "pubkey": row["pubkey"],
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
                        "is_new_neighbor": bool(row["is_new_neighbor"])
                    }
                    adverts.append(advert)
                
                logger.debug(f"Found {len(adverts)} adverts with contact_type '{contact_type}'")
                return adverts
                
        except Exception as e:
            logger.error(f"Failed to get adverts by contact_type '{contact_type}': {e}")
            return []

    def create_transport_key(self, name: str, flood_policy: str, transport_key: str, parent_id: Optional[int] = None, last_used: Optional[float] = None) -> Optional[int]:
        try:
            current_time = time.time()
            with sqlite3.connect(self.sqlite_path) as conn:
                cursor = conn.execute("""
                    INSERT INTO transport_keys (name, flood_policy, transport_key, parent_id, last_used, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (name, flood_policy, transport_key, parent_id, last_used, current_time, current_time))
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Failed to create transport key: {e}")
            return None

    def get_transport_keys(self) -> List[dict]:
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT id, name, flood_policy, transport_key, parent_id, last_used, created_at, updated_at
                    FROM transport_keys
                    ORDER BY created_at ASC
                """).fetchall()
                
                return [{
                    "id": row["id"],
                    "name": row["name"],
                    "flood_policy": row["flood_policy"],
                    "transport_key": row["transport_key"],
                    "parent_id": row["parent_id"],
                    "last_used": row["last_used"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"]
                } for row in rows]
        except Exception as e:
            logger.error(f"Failed to get transport keys: {e}")
            return []

    def get_transport_key_by_id(self, key_id: int) -> Optional[dict]:
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("""
                    SELECT id, name, flood_policy, transport_key, parent_id, last_used, created_at, updated_at
                    FROM transport_keys WHERE id = ?
                """, (key_id,)).fetchone()
                
                if row:
                    return {
                        "id": row["id"],
                        "name": row["name"],
                        "flood_policy": row["flood_policy"],
                        "transport_key": row["transport_key"],
                        "parent_id": row["parent_id"],
                        "last_used": row["last_used"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"]
                    }
                return None
        except Exception as e:
            logger.error(f"Failed to get transport key by id: {e}")
            return None

    def update_transport_key(self, key_id: int, name: Optional[str] = None, flood_policy: Optional[str] = None, transport_key: Optional[str] = None, parent_id: Optional[int] = None, last_used: Optional[float] = None) -> bool:
        try:
            updates = []
            params = []
            
            if name is not None:
                updates.append("name = ?")
                params.append(name)
            if flood_policy is not None:
                updates.append("flood_policy = ?")
                params.append(flood_policy)
            if transport_key is not None:
                updates.append("transport_key = ?")
                params.append(transport_key)
            if parent_id is not None:
                updates.append("parent_id = ?")
                params.append(parent_id)
            if last_used is not None:
                updates.append("last_used = ?")
                params.append(last_used)
            
            if not updates:
                return False
            
            updates.append("updated_at = ?")
            params.append(time.time())
            params.append(key_id)
            
            with sqlite3.connect(self.sqlite_path) as conn:
                cursor = conn.execute(f"""
                    UPDATE transport_keys SET {', '.join(updates)}
                    WHERE id = ?
                """, params)
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to update transport key: {e}")
            return False

    def delete_transport_key(self, key_id: int) -> bool:
        try:
            with sqlite3.connect(self.sqlite_path) as conn:
                cursor = conn.execute("DELETE FROM transport_keys WHERE id = ?", (key_id,))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete transport key: {e}")
            return False