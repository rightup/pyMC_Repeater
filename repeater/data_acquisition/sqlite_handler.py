import base64
import json
import logging
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("SQLiteHandler")


class SQLiteHandler:
    def __init__(self, storage_dir: Path):
        self.storage_dir = storage_dir
        self.sqlite_path = self.storage_dir / "repeater.db"
        self._api_token_last_used_updates = {}
        self._api_token_last_used_interval_sec = 300
        self._hot_cache_ttl_sec = 60
        self._packet_stats_cache = {}
        self._neighbors_cache = {"timestamp": 0.0, "value": None}
        # Thread-local storage for persistent SQLite connections.
        # Opening a new connection on every DB call is expensive on SD-card
        # storage: each sqlite3.connect() call triggers file-system operations
        # and each subsequent PRAGMA runs as a round-trip.  Thread-local keeps
        # one long-lived connection per thread (typically one for the write
        # executor and one for the event-loop / HTTP threads), eliminating
        # repeated setup overhead while maintaining correct isolation.
        self._local = threading.local()
        self._init_database()
        self._run_migrations()

    def _connect(self) -> sqlite3.Connection:
        """Return a persistent thread-local SQLite connection.

        The first call from a given thread opens the connection and configures
        it once.  Subsequent calls from the same thread return the cached
        connection, avoiding per-call connection overhead and repeated PRAGMA
        round-trips.

        WAL (Write-Ahead Logging) mode:
          Default journal mode (DELETE) takes an exclusive lock for every write,
          blocking all readers.  WAL allows one writer and multiple readers to
          operate concurrently — critical on SD-card storage where a single
          write can take 5–20 ms.

        synchronous=NORMAL:
          Default FULL flushes WAL frames to disk after every transaction.
          NORMAL flushes only at WAL checkpoints — safe (no data loss on power
          failure beyond the current transaction) and significantly faster on
          SD cards, which have slow fsync.

        busy_timeout=5000:
          Under concurrent access SQLite would immediately raise
          'database is locked'.  5 s of automatic retry eliminates transient
          contention errors when the write executor and the HTTP thread
          briefly compete for the WAL write lock.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.sqlite_path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _invalidate_hot_caches(self) -> None:
        self._packet_stats_cache.clear()
        self._neighbors_cache = {"timestamp": 0.0, "value": None}

    def _init_database(self):
        try:
            with self._connect() as conn:
                conn.execute(
                    """
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
                """
                )

                conn.execute(
                    """
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
                        is_new_neighbor BOOLEAN NOT NULL,
                        zero_hop BOOLEAN NOT NULL DEFAULT FALSE
                    )
                """
                )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS noise_floor (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        noise_floor_dbm REAL NOT NULL
                    )
                """
                )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS crc_errors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        count INTEGER NOT NULL DEFAULT 1
                    )
                """
                )

                conn.execute(
                    """
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
                """
                )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS api_tokens (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        token_hash TEXT NOT NULL UNIQUE,
                        created_at REAL NOT NULL,
                        last_used REAL
                    )
                """
                )

                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_packets_timestamp ON packets(timestamp)"
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_packets_type ON packets(type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_packets_hash ON packets(packet_hash)")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_packets_transmitted ON packets(transmitted)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_adverts_timestamp ON adverts(timestamp)"
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_adverts_pubkey ON adverts(pubkey)")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_noise_timestamp ON noise_floor(timestamp)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_crc_errors_timestamp ON crc_errors(timestamp)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_transport_keys_name ON transport_keys(name)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_transport_keys_parent ON transport_keys(parent_id)"
                )

                # Room server tables
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS room_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        room_hash TEXT NOT NULL,
                        author_pubkey TEXT NOT NULL,
                        post_timestamp REAL NOT NULL,
                        sender_timestamp REAL,
                        message_text TEXT NOT NULL,
                        txt_type INTEGER NOT NULL,
                        created_at REAL NOT NULL
                    )
                """
                )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS room_client_sync (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        room_hash TEXT NOT NULL,
                        client_pubkey TEXT NOT NULL,
                        sync_since REAL NOT NULL DEFAULT 0,
                        pending_ack_crc INTEGER DEFAULT 0,
                        push_post_timestamp REAL DEFAULT 0,
                        ack_timeout_time REAL DEFAULT 0,
                        push_failures INTEGER DEFAULT 0,
                        last_activity REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(room_hash, client_pubkey)
                    )
                """
                )

                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_room_messages_room ON room_messages(room_hash, post_timestamp)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_room_messages_author ON room_messages(author_pubkey)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_room_client_sync_room ON room_client_sync(room_hash, client_pubkey)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_room_client_sync_pending ON room_client_sync(pending_ack_crc)"
                )

                conn.commit()
                logger.info(f"SQLite database initialized: {self.sqlite_path}")

        except Exception as e:
            logger.error(f"Failed to initialize SQLite: {e}")

    def _run_migrations(self):
        """Run database migrations"""
        try:
            with self._connect() as conn:
                # Create migrations table if it doesn't exist
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS migrations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        migration_name TEXT NOT NULL UNIQUE,
                        applied_at REAL NOT NULL
                    )
                """
                )

                # Migration 1: Add zero_hop column to adverts table
                migration_name = "add_zero_hop_to_adverts"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()

                if not existing:
                    # Check if zero_hop column already exists
                    cursor = conn.execute("PRAGMA table_info(adverts)")
                    columns = [column[1] for column in cursor.fetchall()]

                    if "zero_hop" not in columns:
                        conn.execute(
                            "ALTER TABLE adverts ADD COLUMN zero_hop BOOLEAN NOT NULL DEFAULT FALSE"
                        )
                        logger.info("Added zero_hop column to adverts table")

                    # Mark migration as applied
                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 2: Add LBT metrics columns to packets table
                migration_name = "add_lbt_metrics_to_packets"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()

                if not existing:
                    # Check if columns already exist
                    cursor = conn.execute("PRAGMA table_info(packets)")
                    columns = [column[1] for column in cursor.fetchall()]

                    if "lbt_attempts" not in columns:
                        conn.execute(
                            "ALTER TABLE packets ADD COLUMN lbt_attempts INTEGER DEFAULT 0"
                        )
                        logger.info("Added lbt_attempts column to packets table")

                    if "lbt_backoff_delays_ms" not in columns:
                        conn.execute("ALTER TABLE packets ADD COLUMN lbt_backoff_delays_ms TEXT")
                        logger.info("Added lbt_backoff_delays_ms column to packets table")

                    if "lbt_channel_busy" not in columns:
                        conn.execute(
                            "ALTER TABLE packets ADD COLUMN lbt_channel_busy BOOLEAN DEFAULT FALSE"
                        )
                        logger.info("Added lbt_channel_busy column to packets table")

                    # Mark migration as applied
                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 3: Add api_tokens table
                migration_name = "add_api_tokens_table"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()

                if not existing:
                    # Check if api_tokens table already exists
                    cursor = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='api_tokens'"
                    )

                    if not cursor.fetchone():
                        conn.execute(
                            """
                            CREATE TABLE api_tokens (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                name TEXT NOT NULL,
                                token_hash TEXT NOT NULL UNIQUE,
                                created_at REAL NOT NULL,
                                last_used REAL
                            )
                        """
                        )
                        logger.info("Created api_tokens table")

                    # Mark migration as applied
                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 4: Add companion tables for companion identity persistence
                migration_name = "add_companion_tables"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()

                if not existing:
                    cursor = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='companion_contacts'"
                    )
                    if not cursor.fetchone():
                        conn.execute(
                            """
                            CREATE TABLE companion_contacts (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                companion_hash TEXT NOT NULL,
                                pubkey BLOB NOT NULL,
                                name TEXT NOT NULL,
                                adv_type INTEGER NOT NULL DEFAULT 0,
                                flags INTEGER NOT NULL DEFAULT 0,
                                out_path_len INTEGER NOT NULL DEFAULT -1,
                                out_path BLOB,
                                last_advert_timestamp INTEGER NOT NULL DEFAULT 0,
                                lastmod INTEGER NOT NULL DEFAULT 0,
                                gps_lat REAL NOT NULL DEFAULT 0,
                                gps_lon REAL NOT NULL DEFAULT 0,
                                sync_since INTEGER NOT NULL DEFAULT 0,
                                updated_at REAL NOT NULL
                            )
                        """
                        )
                        conn.execute(
                            """
                            CREATE TABLE companion_channels (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                companion_hash TEXT NOT NULL,
                                channel_idx INTEGER NOT NULL,
                                name TEXT NOT NULL,
                                secret BLOB NOT NULL,
                                updated_at REAL NOT NULL
                            )
                        """
                        )
                        conn.execute(
                            """
                            CREATE TABLE companion_messages (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                companion_hash TEXT NOT NULL,
                                sender_key BLOB NOT NULL,
                                txt_type INTEGER NOT NULL DEFAULT 0,
                                timestamp INTEGER NOT NULL DEFAULT 0,
                                text TEXT NOT NULL,
                                is_channel INTEGER NOT NULL DEFAULT 0,
                                channel_idx INTEGER NOT NULL DEFAULT 0,
                                path_len INTEGER NOT NULL DEFAULT 0,
                                packet_hash TEXT,
                                created_at REAL NOT NULL
                            )
                        """
                        )
                        conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_companion_contacts_hash ON companion_contacts(companion_hash)"
                        )
                        conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_companion_contacts_pubkey ON companion_contacts(companion_hash, pubkey)"
                        )
                        conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_companion_channels_hash ON companion_channels(companion_hash)"
                        )
                        conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_companion_messages_hash ON companion_messages(companion_hash)"
                        )
                        conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_companion_messages_hash_packet ON companion_messages(companion_hash, packet_hash)"
                        )
                        logger.info(
                            "Created companion_contacts, companion_channels, companion_messages tables"
                        )

                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 5: Add UNIQUE index on companion_contacts(companion_hash, pubkey)
                # Required for ON CONFLICT upsert in companion_upsert_contact.
                migration_name = "unique_companion_contacts_pubkey"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()

                if not existing:
                    # Replace the non-unique index with a UNIQUE one
                    conn.execute(
                        "DROP INDEX IF EXISTS idx_companion_contacts_pubkey"
                    )
                    conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_companion_contacts_hash_pubkey "
                        "ON companion_contacts (companion_hash, pubkey)"
                    )
                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 6: Normalize companion_hash to 0x-prefixed hex (match room_hash pattern)
                migration_name = "companion_hash_0x_prefix"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()

                if not existing:
                    for table in ("companion_contacts", "companion_channels", "companion_messages"):
                        conn.execute(
                            f"UPDATE {table} SET companion_hash = '0x' || companion_hash "
                            "WHERE companion_hash NOT LIKE '0x%'"
                        )
                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 7: Add companion_prefs table (JSON blob for full NodePrefs persistence)
                migration_name = "add_companion_prefs"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()

                if not existing:
                    cursor = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='companion_prefs'"
                    )
                    if not cursor.fetchone():
                        conn.execute(
                            """
                            CREATE TABLE companion_prefs (
                                companion_hash TEXT PRIMARY KEY,
                                prefs_json TEXT NOT NULL
                            )
                            """
                        )
                        logger.info("Created companion_prefs table")
                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 8: UNIQUE index on companion_messages for dedup by
                # (companion_hash, packet_hash).  Enables INSERT OR IGNORE
                # deduplication in companion_push_message, replacing the
                # Python-level SELECT + INSERT round-trip.
                migration_name = "companion_messages_packet_hash_unique"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()
                if not existing:
                    conn.execute(
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS idx_companion_messages_dedup
                        ON companion_messages(companion_hash, packet_hash)
                        WHERE packet_hash IS NOT NULL
                        """
                    )
                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 9: Deduplicate adverts and enforce UNIQUE on pubkey.
                # Without this index store_advert's ON CONFLICT clause cannot
                # function and each advert inserts a new row instead of updating
                # the existing one, causing unbounded table growth on busy meshes.
                migration_name = "adverts_unique_pubkey"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()
                if not existing:
                    # Keep only the most recently seen row per pubkey
                    conn.execute(
                        """
                        DELETE FROM adverts WHERE id NOT IN (
                            SELECT MAX(id) FROM adverts GROUP BY pubkey
                        )
                        """
                    )
                    conn.execute("DROP INDEX IF EXISTS idx_adverts_pubkey")
                    conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_adverts_pubkey ON adverts(pubkey)"
                    )
                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                conn.commit()

        except Exception as e:
            logger.error(f"Failed to run migrations: {e}")

    # API Token methods
    def create_api_token(self, name: str, token_hash: str) -> int:
        """Create a new API token entry"""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO api_tokens (name, token_hash, created_at) VALUES (?, ?, ?)",
                    (name, token_hash, time.time()),
                )
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Failed to create API token: {e}")
            raise

    def verify_api_token(self, token_hash: str) -> Optional[Dict[str, Any]]:
        """Verify API token and update last_used timestamp"""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "SELECT id, name, created_at, last_used FROM api_tokens WHERE token_hash = ?",
                    (token_hash,),
                )
                row = cursor.fetchone()

                if row:
                    token_id, name, created_at, _last_used = row
                    now = time.time()

                    # Throttle last_used updates to reduce write-lock contention.
                    last_update = self._api_token_last_used_updates.get(token_id, 0.0)
                    if now - last_update >= self._api_token_last_used_interval_sec:
                        conn.execute(
                            "UPDATE api_tokens SET last_used = ? WHERE id = ?", (now, token_id)
                        )
                        conn.commit()
                        self._api_token_last_used_updates[token_id] = now

                    return {"id": token_id, "name": name, "created_at": created_at}
                return None
        except Exception as e:
            logger.error(f"Failed to verify API token: {e}")
            return None

    def revoke_api_token(self, token_id: int) -> bool:
        """Revoke (delete) an API token"""
        try:
            with self._connect() as conn:
                cursor = conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to revoke API token: {e}")
            return False

    def list_api_tokens(self) -> List[Dict[str, Any]]:
        """List all API tokens (without sensitive data)"""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "SELECT id, name, created_at, last_used FROM api_tokens ORDER BY created_at DESC"
                )

                tokens = []
                for row in cursor.fetchall():
                    tokens.append(
                        {"id": row[0], "name": row[1], "created_at": row[2], "last_used": row[3]}
                    )
                return tokens
        except Exception as e:
            logger.error(f"Failed to list API tokens: {e}")
            return []

    def store_packet(self, record: dict):
        try:
            with self._connect() as conn:
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

                conn.execute(
                    """
                    INSERT INTO packets (
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        header, transport_codes, payload, payload_length,
                        tx_delay_ms, packet_hash, original_path, forwarded_path, raw_packet,
                        lbt_attempts, lbt_backoff_delays_ms, lbt_channel_busy
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
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
                        record.get("raw_packet"),
                        record.get("lbt_attempts", 0),
                        (
                            json.dumps(record.get("lbt_backoff_delays_ms"))
                            if record.get("lbt_backoff_delays_ms")
                            else None
                        ),
                        int(bool(record.get("lbt_channel_busy", False))),
                    ),
                )
                self._invalidate_hot_caches()

        except Exception as e:
            logger.error(f"Failed to store packet in SQLite: {e}")

    def store_advert(self, record: dict):
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                existing = conn.execute(
                    "SELECT pubkey, first_seen, advert_count, zero_hop, rssi, snr FROM adverts WHERE pubkey = ? ORDER BY last_seen DESC LIMIT 1",
                    (record.get("pubkey", ""),),
                ).fetchone()

                current_time = record.get("timestamp", time.time())

                if existing:
                    # Use incoming zero_hop value (already calculated from route_type + path_len)
                    incoming_zero_hop = record.get("zero_hop", False)
                    existing_zero_hop = bool(existing["zero_hop"])

                    # Signal measurement logic:
                    # - If incoming is zero-hop: ALWAYS store incoming rssi/snr (most recent zero-hop measurement)
                    # - If incoming is multi-hop and existing was zero-hop: preserve existing (don't overwrite zero-hop with multi-hop)
                    # - If both are multi-hop: signal measurements are not applicable
                    if incoming_zero_hop:
                        rssi_to_store = record.get("rssi")
                        snr_to_store = record.get("snr")
                        zero_hop_to_store = True
                    elif existing_zero_hop:
                        rssi_to_store = existing["rssi"]
                        snr_to_store = existing["snr"]
                        zero_hop_to_store = True
                    else:
                        rssi_to_store = None
                        snr_to_store = None
                        zero_hop_to_store = False

                    conn.execute(
                        """
                        UPDATE adverts
                        SET timestamp = ?, node_name = ?, is_repeater = ?, route_type = ?,
                            contact_type = ?, latitude = ?, longitude = ?, last_seen = ?,
                            rssi = ?, snr = ?, advert_count = advert_count + 1, is_new_neighbor = 0,
                            zero_hop = ?
                        WHERE pubkey = ?
                    """,
                        (
                            current_time,
                            record.get("node_name"),
                            record.get("is_repeater", False),
                            record.get("route_type"),
                            record.get("contact_type"),
                            record.get("latitude"),
                            record.get("longitude"),
                            current_time,
                            rssi_to_store,
                            snr_to_store,
                            zero_hop_to_store,
                            record.get("pubkey", ""),
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO adverts (
                            timestamp, pubkey, node_name, is_repeater, route_type, contact_type,
                            latitude, longitude, first_seen, last_seen, rssi, snr, advert_count,
                            is_new_neighbor, zero_hop
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
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
                            True,
                            record.get("zero_hop", False),
                        ),
                    )

                self._invalidate_hot_caches()

        except Exception as e:
            logger.error(f"Failed to store advert in SQLite: {e}")

    def store_noise_floor(self, record: dict):
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO noise_floor (timestamp, noise_floor_dbm)
                    VALUES (?, ?)
                """,
                    (record.get("timestamp", time.time()), record.get("noise_floor_dbm")),
                )
        except Exception as e:
            logger.error(f"Failed to store noise floor in SQLite: {e}")

    def store_crc_errors(self, record: dict):
        """Store a CRC error batch (delta count since last poll)."""
        try:
            with self._connect() as conn:
                conn.execute("""
                    INSERT INTO crc_errors (timestamp, count)
                    VALUES (?, ?)
                """, (
                    record.get("timestamp", time.time()),
                    record.get("count", 1)
                ))
        except Exception as e:
            logger.error(f"Failed to store CRC errors in SQLite: {e}")

    def get_crc_error_count(self, hours: int = 24) -> int:
        """Return total CRC errors within the given time window."""
        try:
            cutoff = time.time() - (hours * 3600)
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(count), 0) FROM crc_errors WHERE timestamp > ?",
                    (cutoff,)
                ).fetchone()
                return row[0] if row else 0
        except Exception as e:
            logger.error(f"Failed to get CRC error count: {e}")
            return 0

    def get_crc_error_history(self, hours: int = 24, limit: int = None) -> list:
        """Return CRC error records within the given time window (chronological)."""
        try:
            cutoff = time.time() - (hours * 3600)
            if limit is None:
                limit = 1000
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                query = """
                    SELECT timestamp, count
                    FROM crc_errors
                    WHERE timestamp > ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """
                rows = conn.execute(query, (cutoff, int(limit))).fetchall()
                return [{"timestamp": r["timestamp"], "count": r["count"]} for r in reversed(rows)]
        except Exception as e:
            logger.error(f"Failed to get CRC error history: {e}")
            return []

    def get_packet_stats(self, hours: int = 24) -> dict:
        try:
            now = time.time()
            cached = self._packet_stats_cache.get(hours)
            if cached and (now - cached["timestamp"]) < self._hot_cache_ttl_sec:
                return cached["value"]

            cutoff = now - (hours * 3600)

            with self._connect() as conn:
                conn.row_factory = sqlite3.Row

                stats = conn.execute(
                    """
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
                """,
                    (cutoff,),
                ).fetchone()

                types = conn.execute(
                    """
                    SELECT type, COUNT(*) as count
                    FROM packets
                    WHERE timestamp > ?
                    GROUP BY type
                    ORDER BY count DESC
                """,
                    (cutoff,),
                ).fetchall()

                drop_reasons = conn.execute(
                    """
                    SELECT drop_reason, COUNT(*) as count
                    FROM packets
                    WHERE timestamp > ? AND transmitted = 0 AND drop_reason IS NOT NULL
                    GROUP BY drop_reason
                    ORDER BY count DESC
                """,
                    (cutoff,),
                ).fetchall()

                result = {
                    "total_packets": stats["total_packets"],
                    "transmitted_packets": stats["transmitted_packets"],
                    "dropped_packets": stats["dropped_packets"],
                    "avg_rssi": round(stats["avg_rssi"] or 0, 1),
                    "avg_snr": round(stats["avg_snr"] or 0, 1),
                    "avg_score": round(stats["avg_score"] or 0, 3),
                    "avg_payload_length": round(stats["avg_payload_length"] or 0, 1),
                    "avg_tx_delay": round(stats["avg_tx_delay"] or 0, 1),
                    "packet_types": [{"type": row["type"], "count": row["count"]} for row in types],
                    "drop_reasons": [
                        {"reason": row["drop_reason"], "count": row["count"]}
                        for row in drop_reasons
                    ],
                }

                self._packet_stats_cache[hours] = {"timestamp": now, "value": result}
                return result

        except Exception as e:
            logger.error(f"Failed to get packet stats: {e}")
            return {}

    def get_recent_packets(self, limit: int = 100) -> list:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row

                packets = conn.execute(
                    """
                    SELECT
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        transport_codes, payload, payload_length,
                        tx_delay_ms, packet_hash, original_path, forwarded_path,
                        lbt_attempts, lbt_channel_busy
                    FROM packets
                    ORDER BY timestamp DESC
                    LIMIT ?
                """,
                    (limit,),
                ).fetchall()

                return [dict(row) for row in packets]

        except Exception as e:
            logger.error(f"Failed to get recent packets: {e}")
            return []

    def get_filtered_packets(
        self,
        packet_type: Optional[int] = None,
        route: Optional[int] = None,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list:
        try:
            with self._connect() as conn:
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
                        transport_codes, payload, payload_length,
                        tx_delay_ms, packet_hash, original_path, forwarded_path,
                        lbt_attempts, lbt_channel_busy
                    FROM packets
                """

                if where_clauses:
                    query = f"{base_query} WHERE {' AND '.join(where_clauses)}"
                else:
                    query = base_query

                query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
                params.append(limit)
                params.append(offset)

                packets = conn.execute(query, params).fetchall()

                return [dict(row) for row in packets]

        except Exception as e:
            logger.error(f"Failed to get filtered packets: {e}")
            return []

    def get_airtime_data(
        self,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
        limit: int = 50000,
    ) -> list:
        """Lightweight query returning only columns needed for airtime charting."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                where_clauses = []
                params: list = []
                if start_timestamp is not None:
                    where_clauses.append("timestamp >= ?")
                    params.append(start_timestamp)
                if end_timestamp is not None:
                    where_clauses.append("timestamp <= ?")
                    params.append(end_timestamp)
                query = "SELECT timestamp, length, payload_length, transmitted FROM packets"
                if where_clauses:
                    query += " WHERE " + " AND ".join(where_clauses)
                query += " ORDER BY timestamp DESC LIMIT ?"
                params.append(limit)
                return [dict(row) for row in conn.execute(query, params).fetchall()]
        except Exception as e:
            logger.error(f"Failed to get airtime data: {e}")
            return []

    def get_airtime_buckets(
        self,
        start_timestamp: float,
        end_timestamp: float,
        bucket_seconds: int = 60,
        sf: int = 9,
        bw_hz: int = 62500,
        cr: int = 5,
        preamble: int = 17,
    ) -> list:
        """Return pre-aggregated airtime buckets for chart rendering.

        Applies the Semtech LoRa airtime formula server-side and groups results
        into time buckets, drastically reducing response size vs raw packet rows.
        """
        import math

        bw_khz = bw_hz / 1000
        t_sym = (2**sf) / bw_khz  # ms per symbol
        t_preamble = (preamble + 4.25) * t_sym
        de = 1 if sf >= 11 and bw_hz <= 125000 else 0

        def _airtime_ms(length_bytes: int) -> float:
            length_bytes = max(length_bytes or 32, 1)
            numerator = max(8 * length_bytes - 4 * sf + 28 + 16, 0)  # CRC=1, H=0
            denominator = 4 * (sf - 2 * de)
            n_payload = 8 + math.ceil(numerator / denominator) * cr
            return t_preamble + n_payload * t_sym

        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT timestamp, length, transmitted FROM packets "
                    "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC",
                    (start_timestamp, end_timestamp),
                ).fetchall()

            buckets: dict = {}
            rx_total = 0
            tx_total = 0
            for row in rows:
                bucket_ts = int(row["timestamp"] / bucket_seconds) * bucket_seconds
                ms = _airtime_ms(row["length"])
                if bucket_ts not in buckets:
                    buckets[bucket_ts] = {"timestamp": bucket_ts, "rx_ms": 0.0, "tx_ms": 0.0, "rx_count": 0, "tx_count": 0}
                if row["transmitted"]:
                    buckets[bucket_ts]["tx_ms"] += ms
                    buckets[bucket_ts]["tx_count"] += 1
                    tx_total += 1
                else:
                    buckets[bucket_ts]["rx_ms"] += ms
                    buckets[bucket_ts]["rx_count"] += 1
                    rx_total += 1

            return {
                "buckets": sorted(buckets.values(), key=lambda x: x["timestamp"]),
                "bucket_seconds": bucket_seconds,
                "rx_total": rx_total,
                "tx_total": tx_total,
            }
        except Exception as e:
            logger.error(f"Failed to get airtime buckets: {e}")
            return {"buckets": [], "bucket_seconds": bucket_seconds, "rx_total": 0, "tx_total": 0}

    def get_packet_by_hash(self, packet_hash: str) -> Optional[dict]:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row

                packet = conn.execute(
                    """
                    SELECT
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        header, transport_codes, payload, payload_length,
                        tx_delay_ms, packet_hash, original_path, forwarded_path, raw_packet,
                        lbt_attempts, lbt_backoff_delays_ms, lbt_channel_busy
                    FROM packets
                    WHERE packet_hash = ?
                """,
                    (packet_hash,),
                ).fetchone()

                return dict(packet) if packet else None

        except Exception as e:
            logger.error(f"Failed to get packet by hash: {e}")
            return None

    def get_packet_type_stats(self, hours: int = 24) -> dict:
        try:
            cutoff = time.time() - (hours * 3600)

            # Align with pyMC_core feat/newRadios PAYLOAD_TYPES (0x0B = CONTROL)
            try:
                from pymc_core.protocol.utils import PAYLOAD_TYPES as _PT
                _human = {
                    "REQ": "Request",
                    "RESPONSE": "Response",
                    "TXT_MSG": "Plain Text Message",
                    "ACK": "Acknowledgment",
                    "ADVERT": "Node Advertisement",
                    "GRP_TXT": "Group Text Message",
                    "GRP_DATA": "Group Datagram",
                    "ANON_REQ": "Anonymous Request",
                    "PATH": "Returned Path",
                    "TRACE": "Trace",
                    "MULTIPART": "Multi-part Packet",
                    "CONTROL": "Control",
                    "RAW_CUSTOM": "Custom Packet",
                }
                packet_type_names = {}
                for i in range(16):
                    code = _PT.get(i)
                    if code:
                        label = _human.get(code, code.replace("_", " ").title())
                        packet_type_names[i] = f"{label} ({code})"
                    else:
                        packet_type_names[i] = f"Reserved Type {i}"
            except ImportError:
                packet_type_names = {
                    0: "Request (REQ)",
                    1: "Response (RESPONSE)",
                    2: "Plain Text Message (TXT_MSG)",
                    3: "Acknowledgment (ACK)",
                    4: "Node Advertisement (ADVERT)",
                    5: "Group Text Message (GRP_TXT)",
                    6: "Group Datagram (GRP_DATA)",
                    7: "Anonymous Request (ANON_REQ)",
                    8: "Returned Path (PATH)",
                    9: "Trace (TRACE)",
                    10: "Multi-part Packet (MULTIPART)",
                    11: "Control (CONTROL)",
                    12: "Reserved Type 12",
                    13: "Reserved Type 13",
                    14: "Reserved Type 14",
                    15: "Custom Packet (RAW_CUSTOM)",
                }

            with self._connect() as conn:
                conn.row_factory = sqlite3.Row

                type_rows = conn.execute(
                    """
                    SELECT type, COUNT(*) as count
                    FROM packets
                    WHERE timestamp > ?
                    GROUP BY type
                """,
                    (cutoff,),
                ).fetchall()

                type_counts = {}
                other_count = 0
                for row in type_rows:
                    pkt_type = int(row["type"])
                    count = int(row["count"])
                    if pkt_type <= 15:
                        type_name = packet_type_names.get(pkt_type, f"Type {pkt_type}")
                        type_counts[type_name] = count
                    else:
                        other_count += count

                if other_count > 0:
                    type_counts["Other Types (>15)"] = other_count

                return {
                    "hours": hours,
                    "packet_type_totals": type_counts,
                    "total_packets": sum(type_counts.values()),
                    "period": f"{hours} hours",
                    "data_source": "sqlite",
                }

        except Exception as e:
            logger.error(f"Failed to get packet type stats from SQLite: {e}")
            return {"error": str(e), "data_source": "error"}

    def get_route_stats(self, hours: int = 24) -> dict:

        try:
            cutoff = time.time() - (hours * 3600)

            with self._connect() as conn:
                conn.row_factory = sqlite3.Row

                route_rows = conn.execute(
                    """
                    SELECT route, COUNT(*) as count
                    FROM packets
                    WHERE timestamp > ?
                    GROUP BY route
                """,
                    (cutoff,),
                ).fetchall()

                route_counts = {}
                route_names = {0: "Transport Flood", 1: "Flood", 2: "Direct", 3: "Transport Direct"}
                other_count = 0

                for row in route_rows:
                    route_type = int(row["route"])
                    count = int(row["count"])
                    if route_type <= 3:
                        route_name = route_names.get(route_type, f"Route {route_type}")
                        route_counts[route_name] = count
                    else:
                        other_count += count

                if other_count > 0:
                    route_counts["Other Routes (>3)"] = other_count

                return {
                    "hours": hours,
                    "route_totals": route_counts,
                    "total_packets": sum(route_counts.values()),
                    "period": f"{hours} hours",
                    "data_source": "sqlite",
                }

        except Exception as e:
            logger.error(f"Failed to get route stats from SQLite: {e}")
            return {"error": str(e), "data_source": "error"}

    def get_neighbors(self) -> dict:
        try:
            now = time.time()
            cached = self._neighbors_cache.get("value")
            cached_ts = float(self._neighbors_cache.get("timestamp", 0.0))
            if cached is not None and (now - cached_ts) < self._hot_cache_ttl_sec:
                return cached

            with self._connect() as conn:
                conn.row_factory = sqlite3.Row

                neighbors = conn.execute(
                    """
                    SELECT pubkey, node_name, is_repeater, route_type, contact_type,
                           latitude, longitude, first_seen, last_seen, rssi, snr, advert_count, zero_hop
                    FROM (
                        SELECT
                            pubkey, node_name, is_repeater, route_type, contact_type,
                            latitude, longitude, first_seen, last_seen, rssi, snr, advert_count, zero_hop,
                            ROW_NUMBER() OVER (PARTITION BY pubkey ORDER BY last_seen DESC) AS rn
                        FROM adverts
                    ) latest
                    WHERE rn = 1
                    ORDER BY last_seen DESC
                """
                ).fetchall()

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
                        "zero_hop": bool(row["zero_hop"]),
                    }

                self._neighbors_cache = {"timestamp": now, "value": result}
                return result

        except Exception as e:
            logger.error(f"Failed to get neighbors: {e}")
            return {}

    def get_noise_floor_history(self, hours: int = 24, limit: int = None) -> list:
        try:
            cutoff = time.time() - (hours * 3600)

            if limit is None:
                limit = 1000

            with self._connect() as conn:
                conn.row_factory = sqlite3.Row

                query = """
                    SELECT timestamp, noise_floor_dbm
                    FROM noise_floor
                    WHERE timestamp > ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """

                measurements = conn.execute(query, (cutoff, int(limit))).fetchall()

                # Reverse to get chronological order (oldest to newest)
                result = [
                    {"timestamp": row["timestamp"], "noise_floor_dbm": row["noise_floor_dbm"]}
                    for row in reversed(measurements)
                ]

                return result

        except Exception as e:
            logger.error(f"Failed to get noise floor history: {e}")
            return []

    def get_noise_floor_stats(self, hours: int = 24) -> dict:
        try:
            cutoff = time.time() - (hours * 3600)

            with self._connect() as conn:
                conn.row_factory = sqlite3.Row

                stats = conn.execute(
                    """
                    SELECT
                        COUNT(*) as measurement_count,
                        AVG(noise_floor_dbm) as avg_noise_floor,
                        MIN(noise_floor_dbm) as min_noise_floor,
                        MAX(noise_floor_dbm) as max_noise_floor
                    FROM noise_floor
                    WHERE timestamp > ?
                """,
                    (cutoff,),
                ).fetchone()

                return {
                    "measurement_count": stats["measurement_count"],
                    "avg_noise_floor": round(stats["avg_noise_floor"] or 0, 1),
                    "min_noise_floor": round(stats["min_noise_floor"] or 0, 1),
                    "max_noise_floor": round(stats["max_noise_floor"] or 0, 1),
                    "hours": hours,
                }

        except Exception as e:
            logger.error(f"Failed to get noise floor stats: {e}")
            return {}

    def get_table_stats(self) -> dict:
        """Get row counts, date ranges, and storage info for all tables."""
        try:
            db_size = self.sqlite_path.stat().st_size if self.sqlite_path.exists() else 0

            tables_with_timestamp = {
                "packets": "timestamp",
                "adverts": "timestamp",
                "noise_floor": "timestamp",
                "crc_errors": "timestamp",
                "room_messages": "created_at",
                "companion_messages": "created_at",
            }
            tables_without_timestamp = [
                "transport_keys",
                "api_tokens",
                "room_client_sync",
                "companion_contacts",
                "companion_channels",
                "companion_prefs",
                "migrations",
            ]

            table_info = []
            with self._connect() as conn:
                # Get actual tables present in the database
                existing = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }

                for table, ts_col in tables_with_timestamp.items():
                    if table not in existing:
                        continue
                    row = conn.execute(
                        f"SELECT COUNT(*), MIN({ts_col}), MAX({ts_col}) FROM {table}"  # noqa: S608
                    ).fetchone()
                    count, oldest, newest = row[0], row[1], row[2]
                    table_info.append(
                        {
                            "name": table,
                            "row_count": count,
                            "oldest_timestamp": oldest,
                            "newest_timestamp": newest,
                            "has_timestamp": True,
                        }
                    )

                for table in tables_without_timestamp:
                    if table not in existing:
                        continue
                    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
                    table_info.append(
                        {
                            "name": table,
                            "row_count": count,
                            "has_timestamp": False,
                        }
                    )

            return {"database_size_bytes": db_size, "tables": table_info}

        except Exception as e:
            logger.error(f"Failed to get table stats: {e}")
            return {"database_size_bytes": 0, "tables": []}

    def purge_table(self, table_name: str) -> int:
        """Delete all rows from a specific table. Returns rows deleted."""
        # Hardcoded allowlist — never allow arbitrary table names
        PURGEABLE = {
            "packets",
            "adverts",
            "noise_floor",
            "crc_errors",
            "room_messages",
            "room_client_sync",
            "companion_contacts",
            "companion_channels",
            "companion_messages",
            "companion_prefs",
        }
        if table_name not in PURGEABLE:
            raise ValueError(f"Table '{table_name}' cannot be purged")

        try:
            with self._connect() as conn:
                result = conn.execute(f"DELETE FROM {table_name}")  # noqa: S608
                conn.commit()
                logger.info(f"Purged {result.rowcount} rows from {table_name}")
                return result.rowcount
        except Exception as e:
            logger.error(f"Failed to purge table {table_name}: {e}")
            raise

    def vacuum(self):
        """Reclaim disk space after purging tables."""
        try:
            with self._connect() as conn:
                conn.execute("VACUUM")
            logger.info("Database vacuumed successfully")
        except Exception as e:
            logger.error(f"Failed to vacuum database: {e}")
            raise

    def cleanup_old_data(self, days: int = 7):
        try:
            cutoff = time.time() - (days * 24 * 3600)

            with self._connect() as conn:
                result = conn.execute("DELETE FROM packets WHERE timestamp < ?", (cutoff,))
                packets_deleted = result.rowcount

                result = conn.execute("DELETE FROM adverts WHERE timestamp < ?", (cutoff,))
                adverts_deleted = result.rowcount

                result = conn.execute("DELETE FROM noise_floor WHERE timestamp < ?", (cutoff,))
                noise_deleted = result.rowcount

                result = conn.execute("DELETE FROM crc_errors WHERE timestamp < ?", (cutoff,))
                crc_deleted = result.rowcount

                conn.commit()

                if packets_deleted > 0 or adverts_deleted > 0 or noise_deleted > 0 or crc_deleted > 0:
                    logger.info(
                        f"Cleaned up {packets_deleted} old packets, {adverts_deleted} old adverts, {noise_deleted} old noise measurements, {crc_deleted} old CRC error records"
                    )

        except Exception as e:
            logger.error(f"Failed to cleanup old data: {e}")

    def get_cumulative_counts(self) -> dict:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row

                type_rows = conn.execute(
                    "SELECT type, COUNT(*) as count FROM packets GROUP BY type"
                ).fetchall()

                type_counts = {f"type_{i}": 0 for i in range(16)}
                type_counts["type_other"] = 0
                for row in type_rows:
                    pkt_type = int(row["type"])
                    count = int(row["count"])
                    if pkt_type <= 15:
                        type_counts[f"type_{pkt_type}"] = count
                    else:
                        type_counts["type_other"] += count

                totals = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS rx_total,
                        SUM(CASE WHEN transmitted = 1 THEN 1 ELSE 0 END) AS tx_total,
                        SUM(CASE WHEN transmitted = 0 THEN 1 ELSE 0 END) AS drop_total
                    FROM packets
                """
                ).fetchone()

                return {
                    "rx_total": int(totals["rx_total"] or 0),
                    "tx_total": int(totals["tx_total"] or 0),
                    "drop_total": int(totals["drop_total"] or 0),
                    "type_counts": type_counts,
                }

        except Exception as e:
            logger.error(f"Failed to get cumulative counts: {e}")
            return {"rx_total": 0, "tx_total": 0, "drop_total": 0, "type_counts": {}}

    def get_adverts_by_contact_type(
        self, contact_type: str, limit: Optional[int] = None, hours: Optional[int] = None
    ) -> List[dict]:

        try:
            if limit is None:
                limit = 500

            with self._connect() as conn:
                conn.row_factory = sqlite3.Row

                query = """
                    SELECT id, timestamp, pubkey, node_name, is_repeater, route_type,
                           contact_type, latitude, longitude, first_seen, last_seen,
                           rssi, snr, advert_count, is_new_neighbor, zero_hop
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
                        "is_new_neighbor": bool(row["is_new_neighbor"]),
                        "zero_hop": bool(row["zero_hop"]),
                    }
                    adverts.append(advert)

                return adverts

        except Exception as e:
            logger.error(f"Failed to get adverts by contact_type '{contact_type}': {e}")
            return []

    def generate_transport_key(self, name: str, key_length_bytes: int = 32) -> str:
        """
        Generate a transport key using the proper MeshCore key derivation.

        Args:
            name: The key name to derive the key from
            key_length_bytes: Length of the key in bytes (default: 32 bytes = 256 bits)

        Returns:
            A base64-encoded transport key derived from the name
        """
        try:
            from pymc_core.protocol.transport_keys import get_auto_key_for

            # Use the proper MeshCore key derivation function
            key_bytes = get_auto_key_for(name)

            # Encode to base64 for safe storage and transmission
            key = base64.b64encode(key_bytes).decode("utf-8")

            logger.debug(
                f"Generated transport key for '{name}' with {len(key_bytes)} bytes ({len(key)} base64 chars)"
            )
            return key

        except Exception as e:
            logger.error(f"Failed to generate transport key using get_auto_key_for: {e}")
            # Fallback to secure random if MeshCore function fails
            try:
                random_bytes = secrets.token_bytes(key_length_bytes)
                key = base64.b64encode(random_bytes).decode("utf-8")
                logger.warning(f"Using fallback random key generation for '{name}'")
                return key
            except Exception as fallback_e:
                logger.error(f"Fallback key generation also failed: {fallback_e}")
                raise

    def create_transport_key(
        self,
        name: str,
        flood_policy: str,
        transport_key: Optional[str] = None,
        parent_id: Optional[int] = None,
        last_used: Optional[float] = None,
    ) -> Optional[int]:
        try:
            # Generate key if not provided
            if transport_key is None:
                transport_key = self.generate_transport_key(name)

            current_time = time.time()
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO transport_keys (name, flood_policy, transport_key, parent_id, last_used, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        name,
                        flood_policy,
                        transport_key,
                        parent_id,
                        last_used,
                        current_time,
                        current_time,
                    ),
                )
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Failed to create transport key: {e}")
            return None

    def get_transport_keys(self) -> List[dict]:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT id, name, flood_policy, transport_key, parent_id, last_used, created_at, updated_at
                    FROM transport_keys
                    ORDER BY created_at ASC
                """
                ).fetchall()

                return [
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "flood_policy": row["flood_policy"],
                        "transport_key": row["transport_key"],
                        "parent_id": row["parent_id"],
                        "last_used": row["last_used"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"Failed to get transport keys: {e}")
            return []

    def get_transport_key_by_id(self, key_id: int) -> Optional[dict]:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT id, name, flood_policy, transport_key, parent_id, last_used, created_at, updated_at
                    FROM transport_keys WHERE id = ?
                """,
                    (key_id,),
                ).fetchone()

                if row:
                    return {
                        "id": row["id"],
                        "name": row["name"],
                        "flood_policy": row["flood_policy"],
                        "transport_key": row["transport_key"],
                        "parent_id": row["parent_id"],
                        "last_used": row["last_used"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                return None
        except Exception as e:
            logger.error(f"Failed to get transport key by id: {e}")
            return None

    def update_transport_key(
        self,
        key_id: int,
        name: Optional[str] = None,
        flood_policy: Optional[str] = None,
        transport_key: Optional[str] = None,
        parent_id: Optional[int] = None,
        last_used: Optional[float] = None,
    ) -> bool:
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

            with self._connect() as conn:
                cursor = conn.execute(
                    f"""
                    UPDATE transport_keys SET {', '.join(updates)}
                    WHERE id = ?
                """,
                    params,
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to update transport key: {e}")
            return False

    def delete_transport_key(self, key_id: int) -> bool:
        try:
            with self._connect() as conn:
                cursor = conn.execute("DELETE FROM transport_keys WHERE id = ?", (key_id,))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete transport key: {e}")
            return False

    def sync_transport_keys(self, entries: List[Dict[str, Any]]) -> Dict[str, int]:
        """
        Replace transport key tree from a canonical Glass payload.

        Args:
            entries: Flat list of nodes with fields:
                - node_id: unique stable id in payload
                - name: key/group display name
                - flood_policy: 'allow' | 'deny'
                - transport_key: optional explicit key material
                - parent_node_id: optional parent node reference

        Returns:
            Dict containing applied node count and generated key count.
        """
        if not isinstance(entries, list):
            raise ValueError("transport_keys payload must be a list")

        normalized: Dict[str, Dict[str, Any]] = {}
        used_names: set[str] = set()
        for raw in entries:
            if not isinstance(raw, dict):
                raise ValueError("Each transport key entry must be an object")
            node_id = str(raw.get("node_id", "")).strip()
            name = str(raw.get("name", "")).strip()
            flood_policy = str(raw.get("flood_policy", "")).strip().lower()
            parent_node_id = raw.get("parent_node_id")
            transport_key = raw.get("transport_key")
            if not node_id:
                raise ValueError("transport key entry is missing node_id")
            if node_id in normalized:
                raise ValueError(f"Duplicate node_id in payload: {node_id}")
            if not name:
                raise ValueError(f"transport key entry '{node_id}' is missing name")
            if name in used_names:
                raise ValueError(f"Duplicate transport key name in payload: {name}")
            if flood_policy not in {"allow", "deny"}:
                raise ValueError(f"Invalid flood_policy for '{name}': {flood_policy}")
            if transport_key is not None and not isinstance(transport_key, str):
                raise ValueError(f"transport_key for '{name}' must be a string or null")
            normalized[node_id] = {
                "node_id": node_id,
                "name": name,
                "flood_policy": flood_policy,
                "parent_node_id": str(parent_node_id).strip() if parent_node_id else None,
                "transport_key": transport_key.strip() if isinstance(transport_key, str) else None,
            }
            used_names.add(name)

        for node in normalized.values():
            parent_node_id = node.get("parent_node_id")
            if parent_node_id and parent_node_id not in normalized:
                raise ValueError(
                    f"Parent node '{parent_node_id}' does not exist for '{node['node_id']}'"
                )

        ordered: List[Dict[str, Any]] = []
        pending = dict(normalized)
        resolved_ids: set[str] = set()
        while pending:
            progressed = False
            for node_id, node in list(pending.items()):
                parent_node_id = node.get("parent_node_id")
                if parent_node_id and parent_node_id not in resolved_ids:
                    continue
                ordered.append(node)
                resolved_ids.add(node_id)
                pending.pop(node_id)
                progressed = True
            if not progressed:
                raise ValueError("Cycle detected in transport key tree payload")

        generated_keys = 0
        now = time.time()
        with self._connect() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("DELETE FROM transport_keys")
            db_ids: Dict[str, int] = {}
            for node in ordered:
                transport_key = node.get("transport_key")
                if not transport_key:
                    transport_key = self.generate_transport_key(node["name"])
                    generated_keys += 1
                parent_id = (
                    db_ids.get(node["parent_node_id"])
                    if node.get("parent_node_id")
                    else None
                )
                cursor = conn.execute(
                    """
                    INSERT INTO transport_keys (
                        name,
                        flood_policy,
                        transport_key,
                        parent_id,
                        last_used,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node["name"],
                        node["flood_policy"],
                        transport_key,
                        parent_id,
                        None,
                        now,
                        now,
                    ),
                )
                db_ids[node["node_id"]] = int(cursor.lastrowid)
            conn.commit()

        return {"applied_nodes": len(ordered), "generated_keys": generated_keys}

    def delete_advert(self, advert_id: int) -> bool:
        try:
            with self._connect() as conn:
                cursor = conn.execute("DELETE FROM adverts WHERE id = ?", (advert_id,))
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete advert: {e}")
            return False

    # ------------------------------------------------------------------
    # Room Server Methods
    # ------------------------------------------------------------------

    def insert_room_message(
        self,
        room_hash: str,
        author_pubkey: str,
        message_text: str,
        post_timestamp: float,
        sender_timestamp: float = None,
        txt_type: int = 0,
    ) -> Optional[int]:
        """Insert a new room message and return its ID."""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO room_messages (
                        room_hash, author_pubkey, post_timestamp, sender_timestamp,
                        message_text, txt_type, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        room_hash,
                        author_pubkey,
                        post_timestamp,
                        sender_timestamp,
                        message_text,
                        txt_type,
                        time.time(),
                    ),
                )
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Failed to insert room message: {e}")
            return None

    def get_unsynced_messages(
        self, room_hash: str, client_pubkey: str, sync_since: float, limit: int = 100
    ) -> List[Dict]:
        """Get messages for a room that client hasn't synced yet."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT * FROM room_messages
                    WHERE room_hash = ?
                    AND post_timestamp > ?
                    AND author_pubkey != ?
                    ORDER BY post_timestamp ASC
                    LIMIT ?
                """,
                    (room_hash, sync_since, client_pubkey, limit),
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get unsynced messages: {e}")
            return []

    def upsert_client_sync(self, room_hash: str, client_pubkey: str, **kwargs) -> bool:
        """Insert or update client sync state using single upsert operation."""
        try:
            with self._connect() as conn:
                now = time.time()
                kwargs["updated_at"] = now

                # Set defaults for insert path
                kwargs.setdefault("sync_since", 0)
                kwargs.setdefault("pending_ack_crc", 0)
                kwargs.setdefault("push_post_timestamp", 0)
                kwargs.setdefault("ack_timeout_time", 0)
                kwargs.setdefault("push_failures", 0)
                kwargs.setdefault("last_activity", now)

                columns = ["room_hash", "client_pubkey"] + list(kwargs.keys())
                placeholders = ["?"] * len(columns)
                values = [room_hash, client_pubkey] + list(kwargs.values())

                # Use INSERT OR REPLACE for single atomic upsert
                conn.execute(
                    f"""
                    INSERT OR REPLACE INTO room_client_sync ({', '.join(columns)})
                    VALUES ({', '.join(placeholders)})
                """,
                    values,
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to upsert client sync: {e}")
            return False

    def get_client_sync(self, room_hash: str, client_pubkey: str) -> Optional[Dict]:
        """Get client sync state."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT * FROM room_client_sync
                    WHERE room_hash = ? AND client_pubkey = ?
                """,
                    (room_hash, client_pubkey),
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Failed to get client sync: {e}")
            return None

    def get_all_room_clients(self, room_hash: str) -> List[Dict]:
        """Get all clients for a room."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT * FROM room_client_sync
                    WHERE room_hash = ?
                    ORDER BY last_activity DESC
                """,
                    (room_hash,),
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get room clients: {e}")
            return []

    def get_room_message_count(self, room_hash: str) -> int:
        """Get total number of messages in a room."""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    SELECT COUNT(*) FROM room_messages WHERE room_hash = ?
                """,
                    (room_hash,),
                )
                return cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"Failed to get room message count: {e}")
            return 0

    def get_room_messages(self, room_hash: str, limit: int = 50, offset: int = 0) -> List[Dict]:
        """Get messages from a room with pagination."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT * FROM room_messages
                    WHERE room_hash = ?
                    ORDER BY post_timestamp DESC
                    LIMIT ? OFFSET ?
                """,
                    (room_hash, limit, offset),
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get room messages: {e}")
            return []

    def get_messages_since(
        self, room_hash: str, since_timestamp: float, limit: int = 50
    ) -> List[Dict]:
        """Get messages posted after a specific timestamp."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT * FROM room_messages
                    WHERE room_hash = ? AND post_timestamp > ?
                    ORDER BY post_timestamp DESC
                    LIMIT ?
                """,
                    (room_hash, since_timestamp, limit),
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to get messages since timestamp: {e}")
            return []

    def get_unsynced_count(self, room_hash: str, client_pubkey: str, sync_since: float) -> int:
        """Get count of unsynced messages for a client.

        Note: a duplicate definition of this method existed earlier in the file
        with the same signature but reversed parameter-binding order in the SQL.
        Python silently uses the last definition; the first was dead code.
        The dead definition has been removed.
        """
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    SELECT COUNT(*) FROM room_messages
                    WHERE room_hash = ?
                    AND author_pubkey != ?
                    AND post_timestamp > ?
                """,
                    (room_hash, client_pubkey, sync_since),
                )
                return cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"Failed to get unsynced count: {e}")
            return 0

    def delete_room_message(self, room_hash: str, message_id: int) -> bool:
        """Delete a specific message by ID."""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    DELETE FROM room_messages
                    WHERE room_hash = ? AND id = ?
                """,
                    (room_hash, message_id),
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete message: {e}")
            return False

    def clear_room_messages(self, room_hash: str) -> int:
        """Clear all messages from a room."""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    DELETE FROM room_messages WHERE room_hash = ?
                """,
                    (room_hash,),
                )
                return cursor.rowcount
        except Exception as e:
            logger.error(f"Failed to clear room messages: {e}")
            return 0

    def cleanup_old_messages(self, room_hash: str, keep_count: int = 32) -> int:
        """Keep only the most recent N messages per room."""
        try:
            with self._connect() as conn:
                # First check if cleanup is needed
                cursor = conn.execute(
                    """
                    SELECT COUNT(*) FROM room_messages WHERE room_hash = ?
                """,
                    (room_hash,),
                )
                total_count = cursor.fetchone()[0]

                if total_count <= keep_count:
                    return 0  # No cleanup needed

                # Delete old messages
                cursor = conn.execute(
                    """
                    DELETE FROM room_messages
                    WHERE room_hash = ?
                    AND id NOT IN (
                        SELECT id FROM room_messages
                        WHERE room_hash = ?
                        ORDER BY post_timestamp DESC
                        LIMIT ?
                    )
                """,
                    (room_hash, room_hash, keep_count),
                )
                return cursor.rowcount
        except Exception as e:
            logger.error(f"Failed to cleanup old messages: {e}")
            return 0

    # Companion persistence methods
    def companion_load_contacts(self, companion_hash: str) -> List[Dict]:
        """Load contacts for a companion from storage."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT pubkey, name, adv_type, flags, out_path_len, out_path,
                           last_advert_timestamp, lastmod, gps_lat, gps_lon, sync_since
                    FROM companion_contacts WHERE companion_hash = ?
                """,
                    (companion_hash,),
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to load companion contacts: {e}")
            return []

    def companion_save_contacts(self, companion_hash: str, contacts: List[Dict]) -> bool:
        """Replace all contacts for a companion in storage using batch insert."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM companion_contacts WHERE companion_hash = ?", (companion_hash,)
                )
                now = time.time()
                # Batch insert all contacts at once instead of loop-based inserts
                rows = [
                    (
                        companion_hash,
                        c.get("pubkey", b""),
                        c.get("name", ""),
                        c.get("adv_type", 0),
                        c.get("flags", 0),
                        c.get("out_path_len", -1),
                        c.get("out_path", b""),
                        c.get("last_advert_timestamp", 0),
                        c.get("lastmod", 0),
                        c.get("gps_lat", 0.0),
                        c.get("gps_lon", 0.0),
                        c.get("sync_since", 0),
                        now,
                    )
                    for c in contacts
                ]
                if rows:
                    conn.executemany(
                        """
                        INSERT INTO companion_contacts
                        (companion_hash, pubkey, name, adv_type, flags, out_path_len, out_path,
                         last_advert_timestamp, lastmod, gps_lat, gps_lon, sync_since, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        rows,
                    )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to save companion contacts: {e}")
            return False

    def companion_upsert_contact(self, companion_hash: str, contact: dict) -> bool:
        """Insert or update a single contact for a companion in storage."""
        try:
            with self._connect() as conn:
                now = time.time()
                conn.execute(
                    """
                    INSERT INTO companion_contacts
                    (companion_hash, pubkey, name, adv_type, flags, out_path_len, out_path,
                     last_advert_timestamp, lastmod, gps_lat, gps_lon, sync_since, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(companion_hash, pubkey)
                    DO UPDATE SET
                        name=excluded.name, adv_type=excluded.adv_type,
                        flags=excluded.flags, out_path_len=excluded.out_path_len,
                        out_path=excluded.out_path,
                        last_advert_timestamp=excluded.last_advert_timestamp,
                        lastmod=excluded.lastmod, gps_lat=excluded.gps_lat,
                        gps_lon=excluded.gps_lon, sync_since=excluded.sync_since,
                        updated_at=excluded.updated_at
                """,
                    (
                        companion_hash,
                        contact.get("pubkey", b""),
                        contact.get("name", ""),
                        contact.get("adv_type", 0),
                        contact.get("flags", 0),
                        contact.get("out_path_len", -1),
                        contact.get("out_path", b""),
                        contact.get("last_advert_timestamp", 0),
                        contact.get("lastmod", 0),
                        contact.get("gps_lat", 0.0),
                        contact.get("gps_lon", 0.0),
                        contact.get("sync_since", 0),
                        now,
                    ),
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to upsert companion contact: {e}")
            return False

    def companion_import_repeater_contacts(
        self,
        companion_hash: str,
        contact_types: Optional[List[str]] = None,
        hours: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> int:
        """Import repeater adverts into a companion's contact store (one-time seed).

        Results are ordered by last_seen DESC so the most recent contacts are
        imported first. Optional hours filters to adverts seen within the last N hours;
        optional limit caps how many contacts are imported.
        """
        type_map = {"companion": 1, "repeater": 2, "room_server": 3, "sensor": 4}
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                query = (
                    "SELECT pubkey, node_name, contact_type, latitude, longitude, last_seen "
                    "FROM adverts WHERE pubkey IS NOT NULL"
                )
                params: list = []
                if contact_types:
                    placeholders = ",".join("?" * len(contact_types))
                    query += f" AND contact_type IN ({placeholders})"
                    params.extend(contact_types)
                if hours is not None:
                    cutoff = time.time() - (hours * 3600)
                    query += " AND last_seen >= ?"
                    params.append(cutoff)
                query += " ORDER BY last_seen DESC"
                if limit is not None:
                    query += " LIMIT ?"
                    params.append(limit)
                rows = conn.execute(query, params).fetchall()

            # Batch insert all contacts at once instead of loop-based upserts
            now = time.time()
            contact_rows = []
            for row in rows:
                raw_type = row["contact_type"] or ""
                normalized_type = raw_type.lower().replace(" ", "_").strip()
                adv_type = type_map.get(normalized_type, 0)
                contact_rows.append(
                    (
                        companion_hash,
                        bytes.fromhex(row["pubkey"]),
                        row["node_name"] or "",
                        adv_type,
                        0,  # flags
                        -1,  # out_path_len
                        b"",  # out_path
                        int(row["last_seen"] or 0),  # last_advert_timestamp
                        int(row["last_seen"] or 0),  # lastmod
                        row["latitude"] or 0.0,  # gps_lat
                        row["longitude"] or 0.0,  # gps_lon
                        0,  # sync_since
                        now,  # updated_at
                    )
                )

            if contact_rows:
                with self._connect() as conn:
                    conn.executemany(
                        """
                        INSERT INTO companion_contacts
                        (companion_hash, pubkey, name, adv_type, flags, out_path_len, out_path,
                         last_advert_timestamp, lastmod, gps_lat, gps_lon, sync_since, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(companion_hash, pubkey)
                        DO UPDATE SET
                            name=excluded.name, adv_type=excluded.adv_type,
                            flags=excluded.flags, out_path_len=excluded.out_path_len,
                            out_path=excluded.out_path,
                            last_advert_timestamp=excluded.last_advert_timestamp,
                            lastmod=excluded.lastmod, gps_lat=excluded.gps_lat,
                            gps_lon=excluded.gps_lon, sync_since=excluded.sync_since,
                            updated_at=excluded.updated_at
                    """,
                        contact_rows,
                    )
                    conn.commit()
            return len(contact_rows)
        except Exception as e:
            logger.error(f"Failed to import repeater contacts: {e}")
            return 0

    def companion_load_prefs(self, companion_hash: str) -> Optional[Dict]:
        """Load persisted prefs for a companion. Returns parsed JSON dict or None if no row."""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "SELECT prefs_json FROM companion_prefs WHERE companion_hash = ?",
                    (companion_hash,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return json.loads(row[0])
        except Exception as e:
            logger.error(f"Failed to load companion prefs: {e}")
            return None

    def companion_save_prefs(self, companion_hash: str, prefs: Dict) -> bool:
        """Persist prefs for a companion as JSON. Upserts by companion_hash."""
        try:
            prefs_json = json.dumps(prefs)
            key = str(companion_hash) if companion_hash is not None else ""
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO companion_prefs (companion_hash, prefs_json)
                    VALUES (?, ?)
                    ON CONFLICT(companion_hash) DO UPDATE SET prefs_json = excluded.prefs_json
                    """,
                    (key, prefs_json),
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to save companion prefs: {e}")
            return False

    def companion_load_channels(self, companion_hash: str) -> List[Dict]:
        """Load channels for a companion from storage."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT channel_idx, name, secret FROM companion_channels
                    WHERE companion_hash = ? ORDER BY channel_idx
                """,
                    (companion_hash,),
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to load companion channels: {e}")
            return []

    def companion_save_channels(self, companion_hash: str, channels: List[Dict]) -> bool:
        """Replace all channels for a companion in storage using batch insert."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM companion_channels WHERE companion_hash = ?", (companion_hash,)
                )
                now = time.time()
                # Batch insert all channels at once instead of loop-based inserts
                rows = [
                    (
                        companion_hash,
                        ch.get("channel_idx", 0),
                        ch.get("name", ""),
                        ch.get("secret", b""),
                        now,
                    )
                    for ch in channels
                ]
                if rows:
                    conn.executemany(
                        """
                        INSERT INTO companion_channels
                        (companion_hash, channel_idx, name, secret, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                    """,
                        rows,
                    )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to save companion channels: {e}")
            return False

    def companion_load_messages(self, companion_hash: str, limit: int = 100) -> List[Dict]:
        """Load queued messages for a companion (oldest first for queue order)."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT sender_key, txt_type, timestamp, text, is_channel, channel_idx, path_len
                    FROM companion_messages WHERE companion_hash = ?
                    ORDER BY created_at ASC LIMIT ?
                """,
                    (companion_hash, limit),
                )
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to load companion messages: {e}")
            return []

    def companion_push_message(self, companion_hash: str, msg: Dict) -> bool:
        """Append a message to the companion's queue.

        Deduplicates by (companion_hash, packet_hash) using INSERT OR IGNORE
        backed by the UNIQUE index added in migration 8.  This replaces the
        previous SELECT + INSERT round-trip (two statements, two SD-card reads)
        with a single atomic statement.

        Returns True if inserted, False if the message was a duplicate (skipped).
        """
        try:
            packet_hash = msg.get("packet_hash") or None
            if isinstance(packet_hash, bytes):
                packet_hash = packet_hash.decode("utf-8", errors="replace") if packet_hash else None
            sender_key = msg.get("sender_key", b"")
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO companion_messages
                    (companion_hash, sender_key, txt_type, timestamp, text,
                     is_channel, channel_idx, path_len, packet_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        companion_hash,
                        sender_key,
                        msg.get("txt_type", 0),
                        msg.get("timestamp", 0),
                        msg.get("text", ""),
                        int(msg.get("is_channel", False)),
                        msg.get("channel_idx", 0),
                        msg.get("path_len", 0),
                        packet_hash,
                        time.time(),
                    ),
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to push companion message: {e}")
            return False

    def companion_pop_message(self, companion_hash: str) -> Optional[Dict]:
        """Remove and return the oldest message from the companion's queue."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT id, sender_key, txt_type, timestamp, text, is_channel, channel_idx, path_len
                    FROM companion_messages WHERE companion_hash = ?
                    ORDER BY created_at ASC LIMIT 1
                """,
                    (companion_hash,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                msg = dict(row)
                conn.execute("DELETE FROM companion_messages WHERE id = ?", (msg["id"],))
                conn.commit()
                return {k: v for k, v in msg.items() if k != "id"}
        except Exception as e:
            logger.error(f"Failed to pop companion message: {e}")
            return None
