import base64
import json
import logging
import math
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from openhop_core.companion.constants import MAX_GROUP_DATA_LENGTH
from openhop_core.protocol.constants import MAX_TEXT_LEN

from repeater.companion.utils import (
    companion_device_principal_id,
    parse_companion_send_response,
    strict_json_loads,
)
from repeater.retention import (
    DEFAULT_RETENTION_DAYS,
    validate_positive_seconds,
    validate_retention_days,
)

logger = logging.getLogger("SQLiteHandler")


def _validated_response_json(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("idempotency response must be JSON text")
    parse_companion_send_response(value)
    return value


def _validated_json_object(value: Any) -> str:
    """Validate the generic legacy idempotency payload contract."""

    if not isinstance(value, str):
        raise ValueError("idempotency response must be JSON text")
    parsed = strict_json_loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("idempotency response must be a JSON object")
    return value


def _finite_storage_float(value: Any, field: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    return parsed


def _optional_finite_storage_float(value: Any, field: str) -> Optional[float]:
    if value is None:
        return None
    return _finite_storage_float(value, field)


_COMPANION_MESSAGE_DIRECTIONS = frozenset({"in", "out"})
_COMPANION_MESSAGE_STATES = frozenset(
    {
        "received",
        "pending",
        "transmitted",
        "heard_repeated",
        "confirmed",
        "failed",
        "indeterminate",
    }
)
_COMPANION_MESSAGE_SOURCES = frozenset({"radio", "rest", "frame", "operator"})
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_SQLITE_MAX_ROW_ID = (1 << 63) - 1
_UINT8_MAX = (1 << 8) - 1
_UINT16_MAX = (1 << 16) - 1
_UINT32_MAX = (1 << 32) - 1


def _strict_storage_integer(
    value: Any,
    field: str,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """Return one persisted INTEGER without silently coercing corrupt storage."""

    if type(value) is not int:
        raise ValueError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} must be at most {maximum}")
    return value


def _strict_storage_float(
    value: Any,
    field: str,
    *,
    nullable: bool = False,
) -> Optional[float]:
    """Return one persisted numeric value without accepting numeric text."""

    if value is None:
        if nullable:
            return None
        raise ValueError(f"{field} must be numeric")
    if type(value) not in (int, float):
        raise ValueError(f"{field} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    return parsed


def _strict_hex_storage_text(
    value: Any,
    field: str,
    *,
    allow_empty: bool,
) -> str:
    """Validate hexadecimal TEXT stored for a public companion message."""

    if not isinstance(value, str):
        raise ValueError(f"{field} must be hexadecimal text")
    if not value:
        if allow_empty:
            return value
        raise ValueError(f"{field} must not be empty")
    if len(value) % 2 or any(character not in _HEX_DIGITS for character in value):
        raise ValueError(f"{field} must be even-length hexadecimal text")
    return value


def _strict_companion_packet_hash(value: Any) -> Optional[str]:
    """Validate a stored hash representation accepted by the public normalizer."""

    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("companion message packet_hash must be hexadecimal text")
    payload = value[2:] if value.lower().startswith("0x") else value
    if len(payload) not in {16, 64} or any(character not in _HEX_DIGITS for character in payload):
        raise ValueError(
            "companion message packet_hash must contain exactly 16 or 64 hexadecimal characters"
        )
    return value


class CompanionStorageError(RuntimeError):
    """A companion storage operation could not be completed or verified.

    Legacy storage methods keep their historical ``False``/``None`` error
    contract. Strict v1 reads and transaction helpers raise this exception so
    callers fail closed instead of treating an unavailable database as empty
    state or transmitting again.
    """


class CompanionNamespaceCollisionError(CompanionStorageError):
    """A one-byte companion namespace belongs to a different public identity."""


class SQLiteHandler:
    def __init__(self, storage_dir: Path):
        self.storage_dir = storage_dir
        self.sqlite_path = self.storage_dir / "repeater.db"
        self._companion_lineage_path = self.storage_dir / ".companion-journal-lineage"
        self._api_token_last_used_updates = {}
        self._api_token_last_used_interval_sec = 300
        self._hot_cache_ttl_sec = 60
        self._packet_stats_cache = {}
        self._packet_type_stats_cache = {}
        self._neighbors_cache = {"timestamp": 0.0, "value": None}
        # Short time-based cache for the per-packet cumulative-counts aggregate
        # (two full-table scans). The storage writer thread calls this once per
        # recorded packet/duplicate; a few seconds of staleness is fine for the
        # RRD/UI counters and stops a full scan running on every packet.
        # Intentionally NOT cleared by _invalidate_hot_caches() — that runs on
        # every write, which would defeat the cache under load.
        self._cumulative_counts_cache = {"timestamp": 0.0, "value": None}
        self._cumulative_counts_ttl_sec = 3.0
        # Optional callback fired after any transport_keys (region) write, so the
        # daemon can rebuild the flood-reply RegionMap. Fired after commit, in the
        # writer's thread; see set_transport_keys_changed_callback.
        self._transport_keys_changed_cb = None
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
        self._reconcile_companion_journal_lineage()
        self.companion_idempotency_recover_incomplete()

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
          NORMAL preserves database consistency but a power loss can discard a
          recently committed transaction. It is significantly faster on SD
          cards, which have slow fsync. The idempotency reservation written
          before RF transmission uses a scoped FULL transaction instead.

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
            self._secure_database_files()
        return conn

    @contextmanager
    def _companion_durable_transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit a pre-RF idempotency reservation to stable storage.

        The radio may transmit as soon as this context exits, so losing this
        particular commit after a power failure could make a retry transmit
        twice. Keep high-volume storage at NORMAL, raise this thread's
        connection to FULL only for the reservation, then restore NORMAL on
        both success and failure.
        """
        conn = self._connect()
        if conn.in_transaction:
            raise CompanionStorageError(
                "Cannot reserve an outbound send inside an active transaction"
            )

        try:
            # SQLite rejects synchronous changes inside a transaction.
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            restore_error = None
            try:
                if conn.in_transaction:
                    conn.rollback()
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error as exc:
                restore_error = exc
            if restore_error is not None:
                # Never leave a cached connection at FULL (or in an unknown
                # transaction state) for unrelated high-volume writes.
                try:
                    conn.close()
                finally:
                    if getattr(self._local, "conn", None) is conn:
                        del self._local.conn
                raise CompanionStorageError(
                    "Failed to restore normal SQLite durability"
                ) from restore_error

    def _secure_database_files(self) -> None:
        """Keep the local database and SQLite sidecars owner-readable only."""

        for path in (
            self.sqlite_path,
            Path(f"{self.sqlite_path}-wal"),
            Path(f"{self.sqlite_path}-shm"),
            self._companion_lineage_path,
        ):
            try:
                if path.exists():
                    path.chmod(0o600)
            except OSError as exc:
                logger.warning("Could not restrict permissions on %s: %s", path, exc)

    def _reconcile_companion_journal_lineage(self) -> None:
        """Rotate the cursor epoch when the SQLite file was restored/replaced.

        The database carries one random lineage token and the storage
        directory carries a tiny sidecar copy. A normal restart preserves
        both. Restoring an older ``repeater.db`` leaves the current sidecar,
        producing a mismatch and an automatic epoch rotation before any API
        client can present a falsely-valid old cursor.

        If the sidecar is missing (first upgrade, copied DB, or accidental
        deletion), rotating once is the safe choice.
        """
        sidecar_lineage = None
        try:
            if self._companion_lineage_path.exists():
                candidate = self._companion_lineage_path.read_text(encoding="ascii").strip()
                if len(candidate) == 32:
                    int(candidate, 16)
                    sidecar_lineage = candidate
        except (OSError, ValueError):
            logger.warning("Companion journal lineage sidecar is unreadable; rotating epoch")

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT value FROM companion_journal_meta
                WHERE key = 'database_lineage'
                """
            ).fetchone()
            database_lineage = str(row[0]) if row and row[0] else None
            if (
                sidecar_lineage is not None
                and database_lineage is not None
                and secrets.compare_digest(sidecar_lineage, database_lineage)
            ):
                return

            new_lineage = secrets.token_hex(16)
            new_epoch = secrets.token_hex(8)
            conn.execute(
                """
                INSERT INTO companion_journal_meta (key, value)
                VALUES ('database_lineage', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (new_lineage,),
            )
            conn.execute(
                """
                INSERT INTO companion_journal_meta (key, value)
                VALUES ('journal_epoch', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (new_epoch,),
            )
            conn.commit()

        temporary = self._companion_lineage_path.with_suffix(".tmp")
        try:
            temporary.write_text(new_lineage, encoding="ascii")
            temporary.chmod(0o600)
            temporary.replace(self._companion_lineage_path)
            self._companion_lineage_path.chmod(0o600)
        except OSError as exc:
            logger.error(
                "Could not persist companion journal lineage sidecar %s: %s",
                self._companion_lineage_path,
                exc,
            )

    def _invalidate_hot_caches(self) -> None:
        self._packet_stats_cache.clear()
        self._neighbors_cache = {"timestamp": 0.0, "value": None}

    def set_transport_keys_changed_callback(self, callback) -> None:
        """Register a callback fired after any transport-key write commits."""
        self._transport_keys_changed_cb = callback

    def _notify_transport_keys_changed(self) -> None:
        callback = self._transport_keys_changed_cb
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:
            logger.error("transport_keys change callback failed: %s", exc, exc_info=True)

    @staticmethod
    def _reconcile_companion_message_schema(conn: sqlite3.Connection) -> None:
        """Ensure additive message columns exist before startup recovery.

        Migration markers record history, but they are not a safe substitute
        for inspecting the live table.  A restored or externally rebuilt
        database can retain the markers while carrying an older
        ``companion_messages`` shape.  Recovery and every current message read
        use these columns, so reconcile them idempotently after the numbered
        migrations and before recovery runs.
        """

        columns = {column[1] for column in conn.execute("PRAGMA table_info(companion_messages)")}
        additions = (
            ("sender_prefix", "sender_prefix TEXT NOT NULL DEFAULT ''"),
            ("snr", "snr REAL"),
            ("rssi", "rssi INTEGER"),
            ("channel_data_type", "channel_data_type INTEGER"),
            ("channel_data_payload", "channel_data_payload BLOB"),
            ("consumed_at", "consumed_at REAL"),
            (
                "observation_count",
                "observation_count INTEGER NOT NULL DEFAULT 1",
            ),
            (
                "unique_path_count",
                "unique_path_count INTEGER NOT NULL DEFAULT 1",
            ),
            ("direction", "direction TEXT NOT NULL DEFAULT 'in'"),
            ("state", "state TEXT NOT NULL DEFAULT 'received'"),
            ("recipient_key", "recipient_key BLOB"),
            ("expected_ack", "expected_ack INTEGER"),
            ("source", "source TEXT"),
            (
                "pending_for_frame",
                "pending_for_frame INTEGER NOT NULL DEFAULT 1",
            ),
        )
        added = set()
        for name, definition in additions:
            if name in columns:
                continue
            conn.execute(f"ALTER TABLE companion_messages ADD COLUMN {definition}")
            columns.add(name)
            added.add(name)
            logger.info("Reconciled companion_messages.%s", name)

        if "source" in added:
            conn.execute(
                "UPDATE companion_messages SET source = 'radio' "
                "WHERE direction = 'in' AND source IS NULL"
            )
        if "pending_for_frame" in added:
            conn.execute(
                "UPDATE companion_messages SET pending_for_frame = "
                "CASE WHEN consumed_at IS NULL THEN 1 ELSE 0 END"
            )

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
                        upstream_hash TEXT,
                        upstream_hash_size INTEGER,
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
                    "CREATE INDEX IF NOT EXISTS idx_packets_upstream_time "
                    "ON packets(upstream_hash, upstream_hash_size, timestamp)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_packets_transmitted ON packets(transmitted)"
                )
                # Covering index for the airtime/utilization charts. get_airtime_data
                # and get_airtime_buckets range-scan and order by timestamp, selecting
                # only these columns; keeping them all in the index lets SQLite serve
                # the query index-only, avoiding a full scan of the (large) row heap.
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_packets_airtime "
                    "ON packets(timestamp, length, payload_length, transmitted)"
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
                # A standalone HTTP embedding or a second process can open the
                # same database while the daemon is starting. Serialize the
                # read-marker/apply/write-marker sequence so two constructors
                # cannot both apply one migration from the same stale view.
                conn.execute("BEGIN IMMEDIATE")
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
                                last_advert_packet BLOB,
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
                                sender_prefix TEXT NOT NULL DEFAULT '',
                                snr REAL,
                                rssi INTEGER,
                                channel_data_type INTEGER,
                                channel_data_payload BLOB,
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
                    conn.execute("DROP INDEX IF EXISTS idx_companion_contacts_pubkey")
                    # Pre-index databases could contain repeated imports of
                    # one contact. Keep the newest row instead of making a
                    # routine upgrade fail on otherwise recoverable state.
                    conn.execute(
                        """
                        DELETE FROM companion_contacts
                        WHERE id NOT IN (
                            SELECT MAX(id)
                            FROM companion_contacts
                            GROUP BY companion_hash, pubkey
                        )
                        """
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
                    conn.execute(
                        "UPDATE companion_contacts SET companion_hash = '0x' || companion_hash "
                        "WHERE companion_hash NOT LIKE '0x%'"
                    )
                    conn.execute(
                        "UPDATE companion_channels SET companion_hash = '0x' || companion_hash "
                        "WHERE companion_hash NOT LIKE '0x%'"
                    )
                    conn.execute(
                        "UPDATE companion_messages SET companion_hash = '0x' || companion_hash "
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
                    message_columns = {
                        column[1]
                        for column in conn.execute("PRAGMA table_info(companion_messages)")
                    }
                    if "direction" not in message_columns:
                        # Every row in this historical shape is inbound. A
                        # pre-index race may have stored the same packet twice;
                        # preserve its first queue position while upgrading.
                        conn.execute(
                            """
                            DELETE FROM companion_messages
                            WHERE packet_hash IS NOT NULL
                              AND id NOT IN (
                                  SELECT MIN(id)
                                  FROM companion_messages
                                  WHERE packet_hash IS NOT NULL
                                  GROUP BY companion_hash, packet_hash
                              )
                            """
                        )
                        conn.execute(
                            """
                            CREATE UNIQUE INDEX IF NOT EXISTS idx_companion_messages_dedup
                            ON companion_messages(companion_hash, packet_hash)
                            WHERE packet_hash IS NOT NULL
                            """
                        )
                    # A restored modern schema may have lost only its migration
                    # ledger. Do not recreate the obsolete global index there:
                    # legitimate Frame/REST outbound rows can share hashes.
                    # Migration 21 below reconciles the inbound-only index.
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

                # Migration 10: Add sender_prefix column (hex text) to
                # companion_messages.  TXT_TYPE_SIGNED_PLAIN room posts carry a
                # 4-byte author pubkey prefix; without it, posts replayed from
                # SQLite show a zero-padded author in the app frame.
                migration_name = "add_sender_prefix_to_companion_messages"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()
                if not existing:
                    cursor = conn.execute("PRAGMA table_info(companion_messages)")
                    columns = [column[1] for column in cursor.fetchall()]
                    if "sender_prefix" not in columns:
                        conn.execute(
                            "ALTER TABLE companion_messages "
                            "ADD COLUMN sender_prefix TEXT NOT NULL DEFAULT ''"
                        )
                        logger.info("Added sender_prefix column to companion_messages table")
                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 11: Preserve the exact verified ADVERT wire packet
                # for MeshCore-compatible CMD_EXPORT_CONTACT after restart.
                migration_name = "add_last_advert_packet_to_companion_contacts"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()
                if not existing:
                    cursor = conn.execute("PRAGMA table_info(companion_contacts)")
                    columns = [column[1] for column in cursor.fetchall()]
                    if "last_advert_packet" not in columns:
                        conn.execute(
                            "ALTER TABLE companion_contacts ADD COLUMN last_advert_packet BLOB"
                        )
                        logger.info("Added last_advert_packet column to companion_contacts")
                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 12: Add signal metadata and channel-data columns to
                # companion_messages.  Without snr/channel_data_type/
                # channel_data_payload, a message replayed from SQLite rebuilds
                # with a zero SNR byte and a binary channel-data (GRP_DATA) frame
                # collapses to an empty channel-text frame.
                migration_name = "add_signal_and_channel_data_to_companion_messages"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()
                if not existing:
                    cursor = conn.execute("PRAGMA table_info(companion_messages)")
                    columns = [column[1] for column in cursor.fetchall()]
                    if "snr" not in columns:
                        conn.execute("ALTER TABLE companion_messages ADD COLUMN snr REAL")
                        logger.info("Added snr column to companion_messages table")
                    if "rssi" not in columns:
                        conn.execute("ALTER TABLE companion_messages ADD COLUMN rssi INTEGER")
                        logger.info("Added rssi column to companion_messages table")
                    if "channel_data_type" not in columns:
                        conn.execute(
                            "ALTER TABLE companion_messages ADD COLUMN channel_data_type INTEGER"
                        )
                        logger.info("Added channel_data_type column to companion_messages table")
                    if "channel_data_payload" not in columns:
                        conn.execute(
                            "ALTER TABLE companion_messages ADD COLUMN channel_data_payload BLOB"
                        )
                        logger.info("Added channel_data_payload column to companion_messages table")
                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 13: Add upstream hash fields to packets for
                # neighbour-link history lookups and indexing.
                migration_name = "add_upstream_hash_to_packets"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()
                if not existing:
                    cursor = conn.execute("PRAGMA table_info(packets)")
                    columns = [column[1] for column in cursor.fetchall()]

                    if "upstream_hash" not in columns:
                        conn.execute("ALTER TABLE packets ADD COLUMN upstream_hash TEXT")
                        logger.info("Added upstream_hash column to packets table")

                    if "upstream_hash_size" not in columns:
                        conn.execute("ALTER TABLE packets ADD COLUMN upstream_hash_size INTEGER")
                        logger.info("Added upstream_hash_size column to packets table")

                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_packets_upstream_time "
                        "ON packets(upstream_hash, upstream_hash_size, timestamp)"
                    )
                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 14: Mobile Companion API phase 1 — event journal.
                # companion_events is the canonical sync mechanism (design doc
                # §5): every companion-scoped state change appends one row,
                # and a client's sync state is a single seq integer.
                # AUTOINCREMENT is required so pruned/deleted rowids are never
                # reused — that guarantee is what makes client cursors safe
                # across retention pruning. companion_journal_meta carries the
                # journal epoch (bumped on DB reset) and the prune floor.
                # companion_messages gains consumed_at so the frame-protocol
                # queue becomes soft-consume: popped rows are marked consumed
                # instead of deleted, turning the destructive queue into a
                # durable history that still behaves like a queue for the
                # frame protocol (see companion_pop_message/companion_push_message).
                migration_name = "add_companion_event_journal"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()
                if not existing:
                    cursor = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='companion_events'"
                    )
                    if not cursor.fetchone():
                        conn.execute(
                            """
                            CREATE TABLE companion_events (
                                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                                companion_hash TEXT NOT NULL,
                                event_type TEXT NOT NULL,
                                created_at REAL NOT NULL,
                                ref_table TEXT,
                                ref_id INTEGER,
                                packet_hash TEXT,
                                payload TEXT NOT NULL
                            )
                            """
                        )
                        conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_companion_events_sync "
                            "ON companion_events(companion_hash, seq)"
                        )
                        logger.info("Created companion_events table")

                    cursor = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='companion_journal_meta'"
                    )
                    if not cursor.fetchone():
                        conn.execute(
                            """
                            CREATE TABLE companion_journal_meta (
                                key TEXT PRIMARY KEY,
                                value TEXT
                            )
                            """
                        )
                        logger.info("Created companion_journal_meta table")

                    cursor = conn.execute("PRAGMA table_info(companion_messages)")
                    columns = [column[1] for column in cursor.fetchall()]
                    if "consumed_at" not in columns:
                        conn.execute("ALTER TABLE companion_messages ADD COLUMN consumed_at REAL")
                        logger.info("Added consumed_at column to companion_messages table")

                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 15: Mobile Companion API phase 2 — device pairing
                # and send idempotency (design doc §5.4, §11.1).
                # companion_devices links a device API token 1:1 to a paired
                # phone; companion_idempotency lets POST …/messages replay the
                # original response for a retried Idempotency-Key instead of
                # re-sending the RF packet (design doc §6). api_tokens gains a
                # nullable 'scope' column: NULL means a pre-migration token,
                # treated as 'admin' for backward compatibility (§11.1) — that
                # defaulting happens in verify_api_token's returned dict, not
                # by backfilling existing rows.
                migration_name = "add_companion_devices_and_idempotency"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()
                if not existing:
                    cursor = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='companion_devices'"
                    )
                    if not cursor.fetchone():
                        conn.execute(
                            """
                            CREATE TABLE companion_devices (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                companion_hash TEXT NOT NULL,
                                device_id TEXT NOT NULL UNIQUE,
                                name TEXT NOT NULL,
                                token_id INTEGER NOT NULL,
                                platform TEXT,
                                push_token TEXT,
                                push_relay_url TEXT,
                                created_at REAL NOT NULL,
                                last_seen REAL,
                                last_synced_seq INTEGER
                            )
                            """
                        )
                        logger.info("Created companion_devices table")

                    cursor = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='companion_idempotency'"
                    )
                    if not cursor.fetchone():
                        conn.execute(
                            """
                            CREATE TABLE companion_idempotency (
                                device_id TEXT NOT NULL,
                                idempotency_key TEXT NOT NULL,
                                request_hash TEXT NOT NULL,
                                response_json TEXT NOT NULL,
                                created_at REAL NOT NULL,
                                PRIMARY KEY (device_id, idempotency_key)
                            )
                            """
                        )
                        logger.info("Created companion_idempotency table")

                    cursor = conn.execute("PRAGMA table_info(api_tokens)")
                    columns = [column[1] for column in cursor.fetchall()]
                    if "scope" not in columns:
                        conn.execute("ALTER TABLE api_tokens ADD COLUMN scope TEXT")
                        logger.info("Added scope column to api_tokens table")

                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 16: Mobile Companion API phase 3 — live RF
                # correlation derived counters (design doc §10.6).
                # ``observation_count`` / ``unique_path_count`` track total OTA
                # copies observed (including the original reception) and
                # distinct incoming paths, so headline counts survive
                # ``packets`` retention pruning even after raw rows age out.
                # DEFAULT 1 makes existing rows an honest lower bound (one
                # observation, one path) — no backfill needed.
                migration_name = "add_companion_message_observation_counters"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()
                if not existing:
                    cursor = conn.execute("PRAGMA table_info(companion_messages)")
                    columns = [column[1] for column in cursor.fetchall()]

                    if "observation_count" not in columns:
                        conn.execute(
                            "ALTER TABLE companion_messages "
                            "ADD COLUMN observation_count INTEGER NOT NULL DEFAULT 1"
                        )
                        logger.info("Added observation_count column to companion_messages table")

                    if "unique_path_count" not in columns:
                        conn.execute(
                            "ALTER TABLE companion_messages "
                            "ADD COLUMN unique_path_count INTEGER NOT NULL DEFAULT 1"
                        )
                        logger.info("Added unique_path_count column to companion_messages table")

                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 17: per-device push notification detail level
                # (design doc §12.2). 'none' = payload-free wake (the default,
                # keeps the relay low-trust); 'count' = badge hint; 'preview'
                # = alert text (opt-in, sends content through the relay).
                migration_name = "add_companion_device_push_detail"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()
                if not existing:
                    cursor = conn.execute("PRAGMA table_info(companion_devices)")
                    columns = [column[1] for column in cursor.fetchall()]
                    if "push_detail" not in columns:
                        conn.execute(
                            "ALTER TABLE companion_devices "
                            "ADD COLUMN push_detail TEXT NOT NULL DEFAULT 'none'"
                        )
                        logger.info("Added push_detail column to companion_devices table")

                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 18: per-device mention alerts (design doc §12.2).
                # mention_push toggles the content-free "you were mentioned"
                # alert class; mention_keywords is an optional JSON array of
                # trigger strings (NULL -> default to the companion node_name).
                migration_name = "add_companion_device_mentions"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()
                if not existing:
                    cursor = conn.execute("PRAGMA table_info(companion_devices)")
                    columns = [column[1] for column in cursor.fetchall()]
                    if "mention_push" not in columns:
                        conn.execute(
                            "ALTER TABLE companion_devices "
                            "ADD COLUMN mention_push INTEGER NOT NULL DEFAULT 0"
                        )
                        logger.info("Added mention_push column to companion_devices table")
                    if "mention_keywords" not in columns:
                        conn.execute(
                            "ALTER TABLE companion_devices ADD COLUMN mention_keywords TEXT"
                        )
                        logger.info("Added mention_keywords column to companion_devices table")

                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 19: correctness primitives shared by the frame and
                # HTTP companion transports.
                #
                # - prune floors are per companion (a global floor invalidates
                #   quiet companions whenever a busy companion is pruned);
                # - messages carry an explicit direction/lifecycle and frame
                #   pending bit, so durable history is independent of the
                #   frame protocol's bounded pending queue;
                # - idempotency rows are reserved before RF transmission and
                #   retain an observable state through ambiguous outcomes;
                # - paired devices can bind to the full immutable identity,
                #   rather than relying on an eight-bit companion hash.
                migration_name = "companion_api_correctness_primitives"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()
                if not existing:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS companion_journal_floors (
                            companion_hash TEXT PRIMARY KEY,
                            prune_floor INTEGER NOT NULL DEFAULT 0
                        )
                        """
                    )

                    message_columns = {
                        column[1]
                        for column in conn.execute("PRAGMA table_info(companion_messages)")
                    }
                    if "direction" not in message_columns:
                        conn.execute(
                            "ALTER TABLE companion_messages "
                            "ADD COLUMN direction TEXT NOT NULL DEFAULT 'in'"
                        )
                    if "state" not in message_columns:
                        conn.execute(
                            "ALTER TABLE companion_messages "
                            "ADD COLUMN state TEXT NOT NULL DEFAULT 'received'"
                        )
                    if "recipient_key" not in message_columns:
                        conn.execute("ALTER TABLE companion_messages ADD COLUMN recipient_key BLOB")
                    if "expected_ack" not in message_columns:
                        conn.execute(
                            "ALTER TABLE companion_messages ADD COLUMN expected_ack INTEGER"
                        )
                    if "source" not in message_columns:
                        conn.execute("ALTER TABLE companion_messages ADD COLUMN source TEXT")
                        conn.execute(
                            "UPDATE companion_messages SET source = 'radio' "
                            "WHERE direction = 'in' AND source IS NULL"
                        )
                    if "pending_for_frame" not in message_columns:
                        conn.execute(
                            "ALTER TABLE companion_messages "
                            "ADD COLUMN pending_for_frame INTEGER NOT NULL DEFAULT 1"
                        )
                        conn.execute(
                            "UPDATE companion_messages SET pending_for_frame = "
                            "CASE WHEN consumed_at IS NULL THEN 1 ELSE 0 END"
                        )

                    idempotency_columns = {
                        column[1]
                        for column in conn.execute("PRAGMA table_info(companion_idempotency)")
                    }
                    if "principal_type" not in idempotency_columns:
                        conn.execute(
                            "ALTER TABLE companion_idempotency "
                            "ADD COLUMN principal_type TEXT NOT NULL DEFAULT 'legacy'"
                        )
                    if "principal_id" not in idempotency_columns:
                        conn.execute(
                            "ALTER TABLE companion_idempotency "
                            "ADD COLUMN principal_id TEXT NOT NULL DEFAULT ''"
                        )
                        conn.execute(
                            "UPDATE companion_idempotency SET principal_id = device_id "
                            "WHERE principal_id = ''"
                        )
                    if "state" not in idempotency_columns:
                        conn.execute(
                            "ALTER TABLE companion_idempotency "
                            "ADD COLUMN state TEXT NOT NULL DEFAULT 'complete'"
                        )
                    if "updated_at" not in idempotency_columns:
                        conn.execute("ALTER TABLE companion_idempotency ADD COLUMN updated_at REAL")
                        conn.execute(
                            "UPDATE companion_idempotency SET updated_at = created_at "
                            "WHERE updated_at IS NULL"
                        )
                    if "message_id" not in idempotency_columns:
                        conn.execute(
                            "ALTER TABLE companion_idempotency ADD COLUMN message_id INTEGER"
                        )
                    if "packet_hash" not in idempotency_columns:
                        conn.execute(
                            "ALTER TABLE companion_idempotency ADD COLUMN packet_hash TEXT"
                        )
                    if "expected_ack" not in idempotency_columns:
                        conn.execute(
                            "ALTER TABLE companion_idempotency ADD COLUMN expected_ack INTEGER"
                        )
                    conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_companion_idempotency_principal "
                        "ON companion_idempotency"
                        "(principal_type, principal_id, idempotency_key)"
                    )

                    device_columns = {
                        column[1] for column in conn.execute("PRAGMA table_info(companion_devices)")
                    }
                    if "companion_identity" not in device_columns:
                        conn.execute(
                            "ALTER TABLE companion_devices ADD COLUMN companion_identity TEXT"
                        )
                    # Historical databases may contain more than one device
                    # row for a token because the old schema did not enforce
                    # the documented one-to-one relationship.  Keep migration
                    # non-destructive; the new transactional pairing helper
                    # rejects new duplicates and revocation removes all legacy
                    # rows sharing a token.
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_companion_devices_token_id "
                        "ON companion_devices(token_id)"
                    )

                    # Preserve the old global floor for old clients, while
                    # seeding a conservative-but-valid floor for each known
                    # companion.  min(global_floor, own_head) avoids the old
                    # permanent-invalid-cursor failure for quiet companions.
                    global_floor_row = conn.execute(
                        "SELECT value FROM companion_journal_meta WHERE key = 'prune_floor'"
                    ).fetchone()
                    global_floor = (
                        int(global_floor_row[0])
                        if global_floor_row and global_floor_row[0] is not None
                        else 0
                    )
                    conn.execute(
                        """
                        WITH known_companions AS (
                            SELECT companion_hash FROM companion_events
                            UNION SELECT companion_hash FROM companion_devices
                            UNION SELECT companion_hash FROM companion_messages
                            UNION SELECT companion_hash FROM companion_contacts
                            UNION SELECT companion_hash FROM companion_channels
                            UNION SELECT companion_hash FROM companion_prefs
                        )
                        INSERT OR IGNORE INTO companion_journal_floors
                            (companion_hash, prune_floor)
                        SELECT known_companions.companion_hash,
                               MIN(
                                   ?,
                                   COALESCE(
                                       (
                                           SELECT MAX(seq) FROM companion_events
                                           WHERE companion_hash =
                                               known_companions.companion_hash
                                       ),
                                       0
                                   )
                               )
                        FROM known_companions
                        """,
                        (global_floor,),
                    )

                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                # Migration 20: permanently bind each one-byte companion
                # namespace to the full public identity that first activates
                # it. All companion state is keyed by companion_hash, so
                # allowing another full key with the same first byte to reuse
                # the namespace would expose the first identity's contacts,
                # messages, devices, and preferences.
                migration_name = "bind_companion_namespaces_to_public_identities"
                existing = conn.execute(
                    "SELECT migration_name FROM migrations WHERE migration_name = ?",
                    (migration_name,),
                ).fetchone()
                if not existing:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS companion_namespace_bindings (
                            companion_hash TEXT PRIMARY KEY,
                            companion_identity TEXT NOT NULL UNIQUE,
                            bound_at REAL NOT NULL
                        )
                        """
                    )
                    # Legacy hash-scoped rows cannot prove which full identity
                    # created them. A paired-device identity proves only that
                    # device row, not that contacts/messages/events sharing its
                    # eight-bit hash have the same owner. Leave every legacy
                    # namespace unbound; activation either auto-binds a truly
                    # empty namespace or requires an explicit operator adoption.
                    conn.execute(
                        "INSERT INTO migrations (migration_name, applied_at) VALUES (?, ?)",
                        (migration_name, time.time()),
                    )
                    logger.info(f"Migration '{migration_name}' applied successfully")

                self._reconcile_companion_message_schema(conn)

                # Migration 21: packet-hash deduplication is an inbound
                # delivery rule, not a global message identity rule.  A local
                # outbound send may legitimately have the same truncated hash
                # as an inbound packet (or another outbound send); sharing one
                # radio must not make either API's history insert disappear.
                #
                # Inspect the live index as well as the marker.  Restored or
                # externally rebuilt databases can preserve migration rows
                # while losing schema objects.
                migration_name = "scope_companion_message_dedup_to_inbound"
                conn.execute(
                    """
                    UPDATE companion_messages
                    SET direction = 'in'
                    WHERE direction IS NULL OR TRIM(direction) = ''
                    """
                )
                index_row = conn.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'index'
                      AND name = 'idx_companion_messages_dedup'
                    """
                ).fetchone()
                index_sql = (
                    " ".join(str(index_row[0]).lower().split())
                    if index_row is not None and index_row[0]
                    else ""
                )
                inbound_scoped = "where packet_hash is not null" in index_sql and (
                    "direction = 'in'" in index_sql or "direction='in'" in index_sql
                )
                if not inbound_scoped:
                    conn.execute("DROP INDEX IF EXISTS idx_companion_messages_dedup")
                    conn.execute(
                        """
                        CREATE UNIQUE INDEX idx_companion_messages_dedup
                        ON companion_messages(companion_hash, packet_hash)
                        WHERE packet_hash IS NOT NULL AND direction = 'in'
                        """
                    )
                    logger.info("Scoped companion packet-hash deduplication to inbound messages")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO migrations
                        (migration_name, applied_at)
                    VALUES (?, ?)
                    """,
                    (migration_name, time.time()),
                )

                # Migration 22: a paired device's database row id changes
                # after revoke/re-pair. Preserve RF idempotency across that
                # lifecycle by rekeying every currently resolvable numeric
                # principal to the immutable companion identity plus stable
                # client device id.
                migration_name = "stabilize_companion_device_principals"
                result = self._migrate_device_idempotency_principals(conn)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO migrations
                        (migration_name, applied_at)
                    VALUES (?, ?)
                    """,
                    (migration_name, time.time()),
                )
                if result["rekeyed"] or result["collisions"]:
                    logger.info(
                        "Stabilized %d companion device idempotency principal(s); "
                        "%d collision(s) retained fail-closed",
                        result["rekeyed"],
                        result["collisions"],
                    )
                conn.commit()

        except Exception as e:
            logger.error(f"Failed to run migrations: {e}")
            raise CompanionStorageError("Failed to run SQLite migrations") from e

    # Companion namespace ownership
    @staticmethod
    def _normalize_companion_namespace(
        companion_hash: str, companion_identity: str
    ) -> Tuple[str, str]:
        hash_value = str(companion_hash).strip().lower()
        identity_value = str(companion_identity).strip().lower()
        try:
            valid_hash = (
                len(hash_value) == 4
                and hash_value.startswith("0x")
                and 0 <= int(hash_value[2:], 16) <= 255
            )
        except ValueError:
            valid_hash = False
        try:
            valid_identity = len(identity_value) == 64 and len(bytes.fromhex(identity_value)) == 32
        except ValueError:
            valid_identity = False
        if not valid_hash:
            raise ValueError("companion_hash must be 0x followed by two hex digits")
        if not valid_identity:
            raise ValueError("companion_identity must be a 32-byte public key in hex")
        if identity_value[:2] != hash_value[2:]:
            raise ValueError("companion_identity does not belong to the requested companion_hash")
        return hash_value, identity_value

    @staticmethod
    def _companion_namespace_has_state(
        conn: sqlite3.Connection,
        companion_hash: str,
    ) -> bool:
        """Return whether any durable row already occupies one hash namespace.

        Each lookup is an indexed, bounded existence check. A journal floor
        counts as state because it proves that history existed even when all
        corresponding event rows have already been pruned.
        """

        tables = (
            "companion_contacts",
            "companion_channels",
            "companion_messages",
            "companion_prefs",
            "companion_events",
            "companion_devices",
            "companion_journal_floors",
        )
        return any(
            # The table name comes from the fixed tuple above.
            conn.execute(
                f"SELECT 1 FROM {table} WHERE companion_hash = ? LIMIT 1",  # nosec B608
                (companion_hash,),
            ).fetchone()
            is not None
            for table in tables
        )

    def companion_bind_namespace(
        self,
        companion_hash: str,
        companion_identity: str,
        *,
        adopt_legacy_namespace: bool = False,
    ) -> str:
        """Claim or verify one durable hash namespace for a full public key.

        The binding is immutable and survives config deletion. Re-activating
        the same key reuses its history; a different key with the same first
        byte raises :class:`CompanionNamespaceCollisionError` without reading
        or mutating any hash-scoped state. A truly empty namespace binds
        automatically. Existing unbound state requires the operator's explicit
        ``adopt_legacy_namespace`` acknowledgement because its eight-bit key
        cannot prove a full historical owner.
        """

        if type(adopt_legacy_namespace) is not bool:
            raise ValueError("adopt_legacy_namespace must be a boolean")
        hash_value, identity_value = self._normalize_companion_namespace(
            companion_hash, companion_identity
        )
        try:
            with self._connect() as conn:
                # Serialize the state-existence check with the immutable claim.
                # If another process is writing this namespace, fail closed
                # instead of making a decision from a stale read snapshot.
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """
                    SELECT companion_identity
                    FROM companion_namespace_bindings
                    WHERE companion_hash = ?
                    """,
                    (hash_value,),
                ).fetchone()
                if row is None:
                    if (
                        self._companion_namespace_has_state(conn, hash_value)
                        and not adopt_legacy_namespace
                    ):
                        raise CompanionNamespaceCollisionError(
                            f"Companion namespace {hash_value} contains legacy "
                            "persisted state without a full-identity binding; "
                            f"refusing to guess whether {identity_value} owns it. "
                            "After verifying the configured public identity and "
                            "backup, set settings.adopt_legacy_namespace=true "
                            "for one activation."
                        )
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO companion_namespace_bindings
                            (companion_hash, companion_identity, bound_at)
                        VALUES (?, ?, ?)
                        """,
                        (hash_value, identity_value, time.time()),
                    )
                    row = conn.execute(
                        """
                        SELECT companion_identity
                        FROM companion_namespace_bindings
                        WHERE companion_hash = ?
                        """,
                        (hash_value,),
                    ).fetchone()
                owner = str(row[0]).strip().lower() if row is not None else ""
                if owner != identity_value:
                    raise CompanionNamespaceCollisionError(
                        f"Companion namespace {hash_value} is already bound to "
                        f"public identity {owner}; refusing activation for "
                        f"{identity_value}. Adoption never replaces an existing "
                        "binding; restore the original identity or choose a key "
                        "with a different first byte."
                    )
                conn.commit()
                return owner
        except CompanionNamespaceCollisionError:
            raise
        except Exception as exc:
            raise CompanionStorageError(
                f"Could not verify companion namespace ownership for {hash_value}; "
                "refusing activation"
            ) from exc

    def companion_namespace_binding(self, companion_hash: str) -> Optional[str]:
        """Return a durable namespace owner, raising on an uncertain read."""

        hash_value = str(companion_hash).strip().lower()
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT companion_identity
                    FROM companion_namespace_bindings
                    WHERE companion_hash = ?
                    """,
                    (hash_value,),
                ).fetchone()
                return str(row[0]) if row is not None else None
        except Exception as exc:
            raise CompanionStorageError(
                f"Could not read companion namespace ownership for {hash_value}"
            ) from exc

    # API Token methods
    def create_api_token(self, name: str, token_hash: str, scope: Optional[str] = None) -> int:
        """Create a new API token entry.

        ``scope`` follows the mobile companion API's scope model (design doc
        §11.1): ``companion:{name}``, ``companion:*``, or ``admin``. Leaving
        it None is the existing (pre-companion-API) behavior and is treated
        as 'admin' by verify_api_token for backward compatibility.
        """
        return self.create_api_token_strict(name, token_hash, scope=scope)

    def create_api_token_strict(
        self,
        name: str,
        token_hash: str,
        scope: Optional[str] = None,
    ) -> int:
        """Create an API token, raising when durable storage is unavailable."""

        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO api_tokens (name, token_hash, created_at, scope) VALUES (?, ?, ?, ?)",
                    (name, token_hash, time.time(), scope),
                )
                token_id = cursor.lastrowid
                if token_id is None:
                    raise CompanionStorageError("Created API token has no durable row ID")
                return int(token_id)
        except CompanionStorageError:
            raise
        except Exception as exc:
            raise CompanionStorageError("Failed to create API token") from exc

    def verify_api_token(self, token_hash: str) -> Optional[Dict[str, Any]]:
        """Verify an API token using the legacy fail-closed return contract."""

        try:
            return self.verify_api_token_strict(token_hash)
        except CompanionStorageError as exc:
            logger.error("Failed to verify API token: %s", exc)
            return None

    def verify_api_token_strict(self, token_hash: str) -> Optional[Dict[str, Any]]:
        """Verify an API token, raising when storage cannot be consulted."""

        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "SELECT id, name, created_at, last_used, scope FROM api_tokens WHERE token_hash = ?",
                    (token_hash,),
                )
                row = cursor.fetchone()

                if row:
                    token_id, name, created_at, _last_used, scope = row
                    now = time.time()

                    # Throttle last_used updates to reduce write-lock contention.
                    last_update = self._api_token_last_used_updates.get(token_id, 0.0)
                    if now - last_update >= self._api_token_last_used_interval_sec:
                        conn.execute(
                            "UPDATE api_tokens SET last_used = ? WHERE id = ?", (now, token_id)
                        )
                        conn.commit()
                        self._api_token_last_used_updates[token_id] = now

                    # NULL scope means a pre-migration token; treat as 'admin'
                    # for backward compatibility (design doc §11.1) rather
                    # than backfilling existing rows.
                    return {
                        "id": token_id,
                        "name": name,
                        "created_at": _finite_storage_float(
                            created_at,
                            "API token created_at",
                        ),
                        "scope": scope if scope is not None else "admin",
                    }
                return None
        except Exception as exc:
            raise CompanionStorageError("Failed to verify API token") from exc

    def get_api_token_by_id_strict(
        self,
        token_id: int,
    ) -> Optional[Dict[str, Any]]:
        """Read nonsensitive token metadata for a long-lived auth recheck."""

        if type(token_id) is not int or token_id <= 0:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT id, name, created_at, last_used, scope
                    FROM api_tokens
                    WHERE id = ?
                    """,
                    (token_id,),
                ).fetchone()
                if row is None:
                    return None
                return {
                    "id": row[0],
                    "name": row[1],
                    "created_at": _finite_storage_float(
                        row[2],
                        "API token created_at",
                    ),
                    "last_used": _optional_finite_storage_float(
                        row[3],
                        "API token last_used",
                    ),
                    "scope": row[4] if row[4] is not None else "admin",
                }
        except Exception as exc:
            raise CompanionStorageError("Failed to read API token") from exc

    def revoke_api_token(self, token_id: int) -> bool:
        """Revoke a token and every companion-device row bound to it."""

        try:
            return self.revoke_api_token_strict(token_id)
        except CompanionStorageError as exc:
            logger.error("Failed to revoke API token: %s", exc)
            return False

    def revoke_api_token_strict(self, token_id: int) -> bool:
        """Revoke an API token without losing a paired device's retry guard."""

        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                # The legacy operator endpoint revokes by token ID rather than
                # device ID. Preserve any pre-upgrade numeric idempotency
                # principal while its stable device mapping still exists.
                self._migrate_device_idempotency_principals(
                    conn,
                    token_id=token_id,
                )
                conn.execute("DELETE FROM companion_devices WHERE token_id = ?", (token_id,))
                cursor = conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as exc:
            raise CompanionStorageError("Failed to revoke API token") from exc

    def list_api_tokens(self) -> List[Dict[str, Any]]:
        """List all API tokens (without sensitive data)"""
        try:
            return self.list_api_tokens_strict()
        except CompanionStorageError as e:
            logger.error(f"Failed to list API tokens: {e}")
            return []

    def list_api_tokens_strict(self) -> List[Dict[str, Any]]:
        """List API-token metadata, raising when storage is unavailable."""

        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "SELECT id, name, created_at, last_used, scope "
                    "FROM api_tokens ORDER BY created_at DESC"
                )
                return [
                    {
                        "id": row[0],
                        "name": row[1],
                        "created_at": _finite_storage_float(
                            row[2],
                            "API token created_at",
                        ),
                        "last_used": _optional_finite_storage_float(
                            row[3],
                            "API token last_used",
                        ),
                        "scope": row[4] if row[4] is not None else "admin",
                    }
                    for row in cursor.fetchall()
                ]
        except Exception as e:
            raise CompanionStorageError("Failed to list API tokens") from e

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

                cursor = conn.execute(
                    """
                    INSERT INTO packets (
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        upstream_hash, upstream_hash_size,
                        header, transport_codes, payload, payload_length,
                        tx_delay_ms, packet_hash, original_path, forwarded_path, raw_packet,
                        lbt_attempts, lbt_backoff_delays_ms, lbt_channel_busy
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        record.get("upstream_hash"),
                        record.get("upstream_hash_size"),
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
                return cursor.lastrowid

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
                conn.execute(
                    """
                    INSERT INTO crc_errors (timestamp, count)
                    VALUES (?, ?)
                """,
                    (record.get("timestamp", time.time()), record.get("count", 1)),
                )
        except Exception as e:
            logger.error(f"Failed to store CRC errors in SQLite: {e}")

    def get_crc_error_count(self, hours: int = 24) -> int:
        """Return total CRC errors within the given time window."""
        try:
            cutoff = time.time() - (hours * 3600)
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(count), 0) FROM crc_errors WHERE timestamp > ?", (cutoff,)
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

    def get_policy_event_counts(
        self,
        start_timestamp: float,
        end_timestamp: float,
        bucket_seconds: int = 60,
    ) -> list:
        """Return policy-blocked packet counts grouped by bucket timestamp.

        A policy event is represented by a packet drop reason that starts with
        "Policy blocked packet".
        """
        try:
            bucket_seconds = max(1, int(bucket_seconds))
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT
                        CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts,
                        COUNT(*) AS count
                    FROM packets
                    WHERE timestamp >= ?
                      AND timestamp <= ?
                      AND drop_reason LIKE 'Policy blocked packet%'
                    GROUP BY bucket_ts
                    ORDER BY bucket_ts ASC
                    """,
                    (bucket_seconds, bucket_seconds, start_timestamp, end_timestamp),
                ).fetchall()

                return [
                    {
                        "timestamp": int(row["bucket_ts"]),
                        "count": int(row["count"]),
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"Failed to get policy event counts: {e}")
            return []

    def get_lbt_diagnostics(
        self,
        start_timestamp: float,
        end_timestamp: float,
        bucket_seconds: int = 300,
        severe_attempt_threshold: int = 4,
    ) -> dict:
        """Return aggregated LBT diagnostics for TX-path packets.

        LBT metadata in packets is persisted as "extra attempts/backoffs" where:
          - lbt_attempts == 0 means first CAD/LBT check was clear
          - total attempts/checks ~= lbt_attempts + 1

        This method avoids returning raw packet rows and instead returns
        bucketed aggregates + summary metrics for efficient dashboard refreshes.
        """

        def _weighted_percentile(attempt_counts: dict, q: float) -> Optional[float]:
            total = sum(int(v) for v in attempt_counts.values())
            if total <= 0:
                return None

            q = max(0.0, min(1.0, float(q)))
            # Use nearest-rank percentile so p95 on sparse samples doesn't
            # systematically under-report tail attempts.
            rank = max(1, int(math.ceil(total * q)))
            running = 0
            for attempt in sorted(int(k) for k in attempt_counts.keys()):
                running += int(attempt_counts.get(attempt, 0))
                if running >= rank:
                    return float(attempt)
            return float(max(int(k) for k in attempt_counts.keys()))

        def _packet_type_name(pkt_type: int) -> str:
            try:
                from openhop_core.protocol.utils import PAYLOAD_TYPES as _PT

                labels = {
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
                code = _PT.get(pkt_type)
                if not code:
                    return (
                        f"Reserved Type {pkt_type}" if 0 <= pkt_type <= 15 else f"Type {pkt_type}"
                    )
                return f"{labels.get(code, code.replace('_', ' ').title())} ({code})"
            except Exception:
                return f"Reserved Type {pkt_type}" if 0 <= pkt_type <= 15 else f"Type {pkt_type}"

        try:
            bucket_seconds = max(60, min(int(bucket_seconds), 3600))
            severe_attempt_threshold = max(2, int(severe_attempt_threshold))

            if end_timestamp < start_timestamp:
                start_timestamp, end_timestamp = end_timestamp, start_timestamp

            with self._connect() as conn:
                conn.row_factory = sqlite3.Row

                aggregate_rows = conn.execute(
                    """
                    WITH tx_packets AS (
                        SELECT
                            CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts,
                            CASE
                                WHEN lbt_attempts IS NULL OR lbt_attempts < 0 THEN 1
                                ELSE lbt_attempts + 1
                            END AS attempts_total,
                            CASE WHEN transmitted = 1 THEN 1 ELSE 0 END AS tx_success,
                            CASE
                                WHEN transmitted = 0 AND drop_reason LIKE 'TX failed%' THEN 1
                                ELSE 0
                            END AS failed_tx,
                            CASE WHEN COALESCE(lbt_channel_busy, 0) = 1 THEN 1 ELSE 0 END AS busy
                        FROM packets INDEXED BY idx_packets_timestamp
                        WHERE timestamp >= ?
                          AND timestamp <= ?
                                                    AND (transmitted = 1 OR lbt_attempts > 0 OR drop_reason LIKE 'TX failed%')
                    )
                    SELECT
                        bucket_ts,
                        COUNT(*) AS transmissions,
                        SUM(attempts_total) AS total_attempts,
                        SUM(CASE WHEN attempts_total = 1 THEN 1 ELSE 0 END) AS attempts_1,
                        SUM(CASE WHEN attempts_total = 2 THEN 1 ELSE 0 END) AS attempts_2,
                        SUM(CASE WHEN attempts_total = 3 THEN 1 ELSE 0 END) AS attempts_3,
                        SUM(CASE WHEN attempts_total >= 4 THEN 1 ELSE 0 END) AS attempts_4_plus,
                        SUM(CASE WHEN attempts_total > 1 THEN 1 ELSE 0 END) AS retry_packets,
                        SUM(CASE WHEN tx_success = 1 AND attempts_total = 1 THEN 1 ELSE 0 END) AS first_attempt_success,
                        SUM(failed_tx) AS failed_transmissions,
                        SUM(busy) AS busy_channel_events,
                        SUM(CASE WHEN attempts_total >= ? THEN 1 ELSE 0 END) AS severe_contention_count,
                        MAX(attempts_total) AS max_attempts
                    FROM tx_packets
                    GROUP BY bucket_ts
                    ORDER BY bucket_ts ASC
                    """,
                    (
                        bucket_seconds,
                        bucket_seconds,
                        float(start_timestamp),
                        float(end_timestamp),
                        severe_attempt_threshold,
                    ),
                ).fetchall()

                dist_rows = conn.execute(
                    """
                    WITH tx_packets AS (
                        SELECT
                            CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts,
                            CASE
                                WHEN lbt_attempts IS NULL OR lbt_attempts < 0 THEN 1
                                ELSE lbt_attempts + 1
                            END AS attempts_total
                        FROM packets INDEXED BY idx_packets_timestamp
                        WHERE timestamp >= ?
                          AND timestamp <= ?
                                                    AND (transmitted = 1 OR lbt_attempts > 0 OR drop_reason LIKE 'TX failed%')
                    )
                    SELECT bucket_ts, attempts_total, COUNT(*) AS cnt
                    FROM tx_packets
                    GROUP BY bucket_ts, attempts_total
                    ORDER BY bucket_ts ASC, attempts_total ASC
                    """,
                    (
                        bucket_seconds,
                        bucket_seconds,
                        float(start_timestamp),
                        float(end_timestamp),
                    ),
                ).fetchall()

                type_rows = conn.execute(
                    """
                    WITH tx_packets AS (
                        SELECT
                            CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts,
                            type AS packet_type,
                            CASE
                                WHEN lbt_attempts IS NULL OR lbt_attempts < 0 THEN 1
                                ELSE lbt_attempts + 1
                            END AS attempts_total,
                            CASE WHEN transmitted = 1 THEN 1 ELSE 0 END AS tx_success,
                            CASE
                                WHEN transmitted = 0 AND drop_reason LIKE 'TX failed%' THEN 1
                                ELSE 0
                            END AS failed_tx
                        FROM packets INDEXED BY idx_packets_timestamp
                        WHERE timestamp >= ?
                          AND timestamp <= ?
                                                    AND (transmitted = 1 OR lbt_attempts > 0 OR drop_reason LIKE 'TX failed%')
                    )
                    SELECT
                        bucket_ts,
                        packet_type,
                        COUNT(*) AS transmissions,
                        SUM(attempts_total) AS total_attempts,
                        SUM(CASE WHEN attempts_total = 1 THEN 1 ELSE 0 END) AS attempts_1,
                        SUM(CASE WHEN attempts_total = 2 THEN 1 ELSE 0 END) AS attempts_2,
                        SUM(CASE WHEN attempts_total = 3 THEN 1 ELSE 0 END) AS attempts_3,
                        SUM(CASE WHEN attempts_total >= 4 THEN 1 ELSE 0 END) AS attempts_4_plus,
                        SUM(CASE WHEN attempts_total > 1 THEN 1 ELSE 0 END) AS retry_packets,
                        SUM(CASE WHEN tx_success = 1 AND attempts_total = 1 THEN 1 ELSE 0 END) AS first_attempt_success,
                        SUM(failed_tx) AS failed_transmissions,
                        SUM(CASE WHEN attempts_total >= ? THEN 1 ELSE 0 END) AS severe_contention_count,
                        MAX(attempts_total) AS max_attempts
                    FROM tx_packets
                    GROUP BY bucket_ts, packet_type
                    ORDER BY bucket_ts ASC, packet_type ASC
                    """,
                    (
                        bucket_seconds,
                        bucket_seconds,
                        float(start_timestamp),
                        float(end_timestamp),
                        severe_attempt_threshold,
                    ),
                ).fetchall()

            dist_by_bucket: dict = {}
            overall_dist: dict = {}
            for row in dist_rows:
                bucket_ts = int(row["bucket_ts"])
                attempt = int(row["attempts_total"])
                count = int(row["cnt"])
                bucket_dist = dist_by_bucket.setdefault(bucket_ts, {})
                bucket_dist[attempt] = bucket_dist.get(attempt, 0) + count
                overall_dist[attempt] = overall_dist.get(attempt, 0) + count

            bucket_map: dict = {}
            start_bucket = int(float(start_timestamp) // bucket_seconds) * bucket_seconds
            end_bucket = int(float(end_timestamp) // bucket_seconds) * bucket_seconds
            for bucket_ts in range(start_bucket, end_bucket + 1, bucket_seconds):
                bucket_map[bucket_ts] = {
                    "timestamp": bucket_ts,
                    "transmissions": 0,
                    "total_attempts": 0,
                    "attempts_1": 0,
                    "attempts_2": 0,
                    "attempts_3": 0,
                    "attempts_4_plus": 0,
                    "retry_packets": 0,
                    "first_attempt_success": 0,
                    "failed_transmissions": 0,
                    "busy_channel_events": 0,
                    "severe_contention_count": 0,
                    "max_attempts": 0,
                }

            for row in aggregate_rows:
                bucket_ts = int(row["bucket_ts"])
                if bucket_ts not in bucket_map:
                    bucket_map[bucket_ts] = {
                        "timestamp": bucket_ts,
                        "transmissions": 0,
                        "total_attempts": 0,
                        "attempts_1": 0,
                        "attempts_2": 0,
                        "attempts_3": 0,
                        "attempts_4_plus": 0,
                        "retry_packets": 0,
                        "first_attempt_success": 0,
                        "failed_transmissions": 0,
                        "busy_channel_events": 0,
                        "severe_contention_count": 0,
                        "max_attempts": 0,
                    }
                bucket_map[bucket_ts].update(
                    {
                        "transmissions": int(row["transmissions"] or 0),
                        "total_attempts": int(row["total_attempts"] or 0),
                        "attempts_1": int(row["attempts_1"] or 0),
                        "attempts_2": int(row["attempts_2"] or 0),
                        "attempts_3": int(row["attempts_3"] or 0),
                        "attempts_4_plus": int(row["attempts_4_plus"] or 0),
                        "retry_packets": int(row["retry_packets"] or 0),
                        "first_attempt_success": int(row["first_attempt_success"] or 0),
                        "failed_transmissions": int(row["failed_transmissions"] or 0),
                        "busy_channel_events": int(row["busy_channel_events"] or 0),
                        "severe_contention_count": int(row["severe_contention_count"] or 0),
                        "max_attempts": int(row["max_attempts"] or 0),
                    }
                )

            buckets = []
            for bucket_ts in sorted(bucket_map.keys()):
                bucket = bucket_map[bucket_ts]
                transmissions = int(bucket["transmissions"])
                total_attempts = int(bucket["total_attempts"])
                attempts_3_plus = int(bucket["attempts_3"] + bucket["attempts_4_plus"])

                median_attempts = _weighted_percentile(dist_by_bucket.get(bucket_ts, {}), 0.5)
                p95_attempts = _weighted_percentile(dist_by_bucket.get(bucket_ts, {}), 0.95)

                retry_rate_pct = None
                first_attempt_success_rate_pct = None
                avg_attempts = None
                attempts_3_plus_pct = None
                attempts_4_plus_pct = None
                severe_contention_pct = None

                if transmissions > 0:
                    retry_rate_pct = (bucket["retry_packets"] * 100.0) / transmissions
                    first_attempt_success_rate_pct = (
                        bucket["first_attempt_success"] * 100.0
                    ) / transmissions
                    avg_attempts = total_attempts / transmissions
                    attempts_3_plus_pct = (attempts_3_plus * 100.0) / transmissions
                    attempts_4_plus_pct = (bucket["attempts_4_plus"] * 100.0) / transmissions
                    severe_contention_pct = (
                        bucket["severe_contention_count"] * 100.0
                    ) / transmissions

                buckets.append(
                    {
                        "timestamp": bucket_ts,
                        "transmissions": transmissions,
                        "total_attempts": total_attempts,
                        "first_attempt_success": int(bucket["first_attempt_success"]),
                        "retry_packets": int(bucket["retry_packets"]),
                        "retry_rate_pct": retry_rate_pct,
                        "first_attempt_success_rate_pct": first_attempt_success_rate_pct,
                        "avg_attempts": avg_attempts,
                        "median_attempts": median_attempts,
                        "p95_attempts": p95_attempts,
                        "max_attempts": int(bucket["max_attempts"]),
                        "attempts_1": int(bucket["attempts_1"]),
                        "attempts_2": int(bucket["attempts_2"]),
                        "attempts_3": int(bucket["attempts_3"]),
                        "attempts_4_plus": int(bucket["attempts_4_plus"]),
                        "attempts_3_plus": int(attempts_3_plus),
                        "attempts_3_plus_pct": attempts_3_plus_pct,
                        "attempts_4_plus_pct": attempts_4_plus_pct,
                        "failed_transmissions": int(bucket["failed_transmissions"]),
                        "busy_channel_events": int(bucket["busy_channel_events"]),
                        "severe_contention_count": int(bucket["severe_contention_count"]),
                        "severe_contention_pct": severe_contention_pct,
                    }
                )

            total_transmissions = int(sum(b["transmissions"] for b in buckets))
            total_attempts = int(sum(b["total_attempts"] for b in buckets))
            first_attempt_success = int(sum(b["first_attempt_success"] for b in buckets))
            retry_packets = int(sum(b["retry_packets"] for b in buckets))
            attempts_1 = int(sum(b["attempts_1"] for b in buckets))
            attempts_2 = int(sum(b["attempts_2"] for b in buckets))
            attempts_3 = int(sum(b["attempts_3"] for b in buckets))
            attempts_4_plus = int(sum(b["attempts_4_plus"] for b in buckets))
            attempts_3_plus = int(attempts_3 + attempts_4_plus)
            failed_transmissions = int(sum(b["failed_transmissions"] for b in buckets))
            busy_channel_events = int(sum(b["busy_channel_events"] for b in buckets))
            severe_contention_count = int(sum(b["severe_contention_count"] for b in buckets))
            max_attempts = int(max([b["max_attempts"] for b in buckets], default=0))

            retry_rate_pct = None
            first_attempt_success_rate_pct = None
            avg_attempts = None
            attempts_3_plus_pct = None
            attempts_4_plus_pct = None
            severe_contention_pct = None

            if total_transmissions > 0:
                retry_rate_pct = (retry_packets * 100.0) / total_transmissions
                first_attempt_success_rate_pct = (
                    first_attempt_success * 100.0
                ) / total_transmissions
                avg_attempts = total_attempts / total_transmissions
                attempts_3_plus_pct = (attempts_3_plus * 100.0) / total_transmissions
                attempts_4_plus_pct = (attempts_4_plus * 100.0) / total_transmissions
                severe_contention_pct = (severe_contention_count * 100.0) / total_transmissions

            worst_bucket = None
            scored_buckets = [
                b
                for b in buckets
                if int(b.get("transmissions", 0)) > 0 and b.get("retry_rate_pct") is not None
            ]
            if scored_buckets:
                worst = max(
                    scored_buckets, key=lambda item: float(item.get("retry_rate_pct") or 0.0)
                )
                worst_bucket = {
                    "timestamp": int(worst["timestamp"]),
                    "retry_rate_pct": float(worst.get("retry_rate_pct") or 0.0),
                    "attempts_3_plus_pct": float(worst.get("attempts_3_plus_pct") or 0.0),
                    "max_attempts": int(worst.get("max_attempts") or 0),
                    "transmissions": int(worst.get("transmissions") or 0),
                }

            summary = {
                "total_transmissions": total_transmissions,
                "total_attempts": total_attempts,
                "first_attempt_success": first_attempt_success,
                "retry_packets": retry_packets,
                "retry_rate_pct": retry_rate_pct,
                "first_attempt_success_rate_pct": first_attempt_success_rate_pct,
                "avg_attempts": avg_attempts,
                "median_attempts": _weighted_percentile(overall_dist, 0.5),
                "p95_attempts": _weighted_percentile(overall_dist, 0.95),
                "max_attempts": max_attempts,
                "attempts_1": attempts_1,
                "attempts_2": attempts_2,
                "attempts_3": attempts_3,
                "attempts_4_plus": attempts_4_plus,
                "attempts_3_plus": attempts_3_plus,
                "attempts_3_plus_pct": attempts_3_plus_pct,
                "attempts_4_plus_pct": attempts_4_plus_pct,
                "failed_transmissions": failed_transmissions,
                "busy_channel_events": busy_channel_events,
                "severe_contention_count": severe_contention_count,
                "severe_contention_pct": severe_contention_pct,
                "severe_attempt_threshold": severe_attempt_threshold,
                "has_lbt_data": total_transmissions > 0,
                "worst_bucket": worst_bucket,
            }

            packet_type_totals: dict = {}
            packet_type_buckets = []
            for row in type_rows:
                bucket_ts = int(row["bucket_ts"])
                packet_type = int(row["packet_type"] if row["packet_type"] is not None else -1)
                transmissions = int(row["transmissions"] or 0)
                total_attempts_for_type = int(row["total_attempts"] or 0)
                attempts_3_plus = int((row["attempts_3"] or 0) + (row["attempts_4_plus"] or 0))

                retry_rate_pct_for_type = None
                first_attempt_success_rate_pct_for_type = None
                avg_attempts_for_type = None
                attempts_3_plus_pct_for_type = None
                if transmissions > 0:
                    retry_rate_pct_for_type = (
                        int(row["retry_packets"] or 0) * 100.0
                    ) / transmissions
                    first_attempt_success_rate_pct_for_type = (
                        int(row["first_attempt_success"] or 0) * 100.0
                    ) / transmissions
                    avg_attempts_for_type = total_attempts_for_type / transmissions
                    attempts_3_plus_pct_for_type = (attempts_3_plus * 100.0) / transmissions

                packet_type_buckets.append(
                    {
                        "timestamp": bucket_ts,
                        "packet_type": packet_type,
                        "packet_type_label": _packet_type_name(packet_type),
                        "transmissions": transmissions,
                        "total_attempts": total_attempts_for_type,
                        "first_attempt_success": int(row["first_attempt_success"] or 0),
                        "retry_packets": int(row["retry_packets"] or 0),
                        "retry_rate_pct": retry_rate_pct_for_type,
                        "first_attempt_success_rate_pct": first_attempt_success_rate_pct_for_type,
                        "avg_attempts": avg_attempts_for_type,
                        "attempts_1": int(row["attempts_1"] or 0),
                        "attempts_2": int(row["attempts_2"] or 0),
                        "attempts_3": int(row["attempts_3"] or 0),
                        "attempts_4_plus": int(row["attempts_4_plus"] or 0),
                        "attempts_3_plus": attempts_3_plus,
                        "attempts_3_plus_pct": attempts_3_plus_pct_for_type,
                        "max_attempts": int(row["max_attempts"] or 0),
                        "failed_transmissions": int(row["failed_transmissions"] or 0),
                        "severe_contention_count": int(row["severe_contention_count"] or 0),
                    }
                )

                total_entry = packet_type_totals.setdefault(
                    packet_type,
                    {
                        "packet_type": packet_type,
                        "packet_type_label": _packet_type_name(packet_type),
                        "transmissions": 0,
                        "retry_packets": 0,
                    },
                )
                total_entry["transmissions"] += transmissions
                total_entry["retry_packets"] += int(row["retry_packets"] or 0)

            packet_types = []
            for pkt_type in sorted(
                packet_type_totals.keys(),
                key=lambda key: packet_type_totals[key]["transmissions"],
                reverse=True,
            ):
                entry = packet_type_totals[pkt_type]
                transmissions = int(entry["transmissions"])
                retry_rate_pct_for_type = None
                if transmissions > 0:
                    retry_rate_pct_for_type = (int(entry["retry_packets"]) * 100.0) / transmissions
                packet_types.append(
                    {
                        "packet_type": int(entry["packet_type"]),
                        "packet_type_label": str(entry["packet_type_label"]),
                        "transmissions": transmissions,
                        "retry_packets": int(entry["retry_packets"]),
                        "retry_rate_pct": retry_rate_pct_for_type,
                    }
                )

            return {
                "start_time": int(start_timestamp),
                "end_time": int(end_timestamp),
                "bucket_seconds": bucket_seconds,
                "summary": summary,
                "buckets": buckets,
                "packet_types": packet_types,
                "packet_type_buckets": packet_type_buckets,
            }

        except Exception as e:
            logger.error(f"Failed to get LBT diagnostics: {e}")
            return {
                "start_time": int(start_timestamp),
                "end_time": int(end_timestamp),
                "bucket_seconds": max(60, min(int(bucket_seconds), 3600)),
                "summary": {
                    "total_transmissions": 0,
                    "total_attempts": 0,
                    "first_attempt_success": 0,
                    "retry_packets": 0,
                    "retry_rate_pct": None,
                    "first_attempt_success_rate_pct": None,
                    "avg_attempts": None,
                    "median_attempts": None,
                    "p95_attempts": None,
                    "max_attempts": 0,
                    "attempts_1": 0,
                    "attempts_2": 0,
                    "attempts_3": 0,
                    "attempts_4_plus": 0,
                    "attempts_3_plus": 0,
                    "attempts_3_plus_pct": None,
                    "attempts_4_plus_pct": None,
                    "failed_transmissions": 0,
                    "busy_channel_events": 0,
                    "severe_contention_count": 0,
                    "severe_contention_pct": None,
                    "severe_attempt_threshold": max(2, int(severe_attempt_threshold)),
                    "has_lbt_data": False,
                    "worst_bucket": None,
                },
                "buckets": [],
                "packet_types": [],
                "packet_type_buckets": [],
            }

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

                # INDEXED BY forces the timestamp range scan. Without it the
                # planner picks idx_packets_type / idx_packets_transmitted to get
                # grouping for free, then heap-checks the timestamp filter across
                # the entire table — turning a bounded window into a full scan
                # (~5s vs ~0.1s at 1.5M rows). A small temp b-tree over the
                # windowed rows is far cheaper.
                types = conn.execute(
                    """
                    SELECT type, COUNT(*) as count
                    FROM packets INDEXED BY idx_packets_timestamp
                    WHERE timestamp > ?
                    GROUP BY type
                    ORDER BY count DESC
                """,
                    (cutoff,),
                ).fetchall()

                drop_reasons = conn.execute(
                    """
                    SELECT drop_reason, COUNT(*) as count
                    FROM packets INDEXED BY idx_packets_timestamp
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

    def get_metrics_data(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        resolution: str = "average",
    ) -> dict:
        resolution_key = str(resolution or "average").lower()
        gauge_aggregates = {
            "average": "AVG",
            "max": "MAX",
            "min": "MIN",
        }
        gauge_aggregate = gauge_aggregates.get(resolution_key, "AVG")

        if end_time is None:
            end_ts = int(time.time())
        else:
            end_ts = int(end_time)

        if start_time is None:
            start_ts = end_ts - (24 * 3600)
        else:
            start_ts = int(start_time)

        if end_ts < start_ts:
            start_ts, end_ts = end_ts, start_ts

        range_seconds = max(0, end_ts - start_ts)
        if range_seconds <= 7 * 24 * 3600:
            bucket_seconds = 60
        elif range_seconds <= 30 * 24 * 3600:
            bucket_seconds = 300
        else:
            bucket_seconds = 3600

        aligned_start = int(start_ts / bucket_seconds) * bucket_seconds
        aligned_end = int(end_ts / bucket_seconds) * bucket_seconds
        timestamps = list(range(aligned_start, aligned_end + bucket_seconds, bucket_seconds))

        metric_names = [
            "rx_count",
            "tx_count",
            "drop_count",
            "avg_rssi",
            "avg_snr",
            "avg_length",
            "avg_score",
            "neighbor_count",
        ]
        packet_type_names = [f"type_{i}" for i in range(16)] + ["type_other"]

        metrics = {
            "rx_count": [],
            "tx_count": [],
            "drop_count": [],
            "avg_rssi": [],
            "avg_snr": [],
            "avg_length": [],
            "avg_score": [],
            # Historical neighbor counts are not stored in packets, so the
            # existing schema cannot reconstruct past values per time bucket.
            "neighbor_count": [],
        }
        packet_types = {name: [] for name in packet_type_names}

        bucket_metrics = {
            ts: {
                "rx_count": 0,
                "tx_count": 0,
                "drop_count": 0,
                "avg_rssi": None,
                "avg_snr": None,
                "avg_length": None,
                "avg_score": None,
                "neighbor_count": None,
            }
            for ts in timestamps
        }
        bucket_packet_types = {ts: {name: 0 for name in packet_type_names} for ts in timestamps}

        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row

                if gauge_aggregate == "MAX":
                    aggregate_query = """
                    SELECT
                        CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts,
                        COUNT(*) AS rx_count,
                        SUM(CASE WHEN transmitted = 1 THEN 1 ELSE 0 END) AS tx_count,
                        SUM(CASE WHEN transmitted = 0 THEN 1 ELSE 0 END) AS drop_count,
                        MAX(rssi) AS avg_rssi,
                        MAX(snr) AS avg_snr,
                        MAX(length) AS avg_length,
                        MAX(score) AS avg_score
                    FROM packets INDEXED BY idx_packets_timestamp
                    WHERE timestamp >= ? AND timestamp <= ?
                    GROUP BY bucket_ts
                    ORDER BY bucket_ts ASC
                    """
                elif gauge_aggregate == "MIN":
                    aggregate_query = """
                    SELECT
                        CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts,
                        COUNT(*) AS rx_count,
                        SUM(CASE WHEN transmitted = 1 THEN 1 ELSE 0 END) AS tx_count,
                        SUM(CASE WHEN transmitted = 0 THEN 1 ELSE 0 END) AS drop_count,
                        MIN(rssi) AS avg_rssi,
                        MIN(snr) AS avg_snr,
                        MIN(length) AS avg_length,
                        MIN(score) AS avg_score
                    FROM packets INDEXED BY idx_packets_timestamp
                    WHERE timestamp >= ? AND timestamp <= ?
                    GROUP BY bucket_ts
                    ORDER BY bucket_ts ASC
                    """
                else:
                    aggregate_query = """
                    SELECT
                        CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts,
                        COUNT(*) AS rx_count,
                        SUM(CASE WHEN transmitted = 1 THEN 1 ELSE 0 END) AS tx_count,
                        SUM(CASE WHEN transmitted = 0 THEN 1 ELSE 0 END) AS drop_count,
                        AVG(rssi) AS avg_rssi,
                        AVG(snr) AS avg_snr,
                        AVG(length) AS avg_length,
                        AVG(score) AS avg_score
                    FROM packets INDEXED BY idx_packets_timestamp
                    WHERE timestamp >= ? AND timestamp <= ?
                    GROUP BY bucket_ts
                    ORDER BY bucket_ts ASC
                    """

                aggregate_rows = conn.execute(
                    aggregate_query,
                    (bucket_seconds, bucket_seconds, start_ts, end_ts),
                ).fetchall()

                packet_type_rows = conn.execute(
                    """
                    SELECT
                        CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts,
                        CASE
                            WHEN type BETWEEN 0 AND 15 THEN CAST(type AS INTEGER)
                            ELSE 16
                        END AS type_bucket,
                        COUNT(*) AS count
                    FROM packets INDEXED BY idx_packets_timestamp
                    WHERE timestamp >= ? AND timestamp <= ?
                    GROUP BY bucket_ts, type_bucket
                    ORDER BY bucket_ts ASC, type_bucket ASC
                    """,
                    (bucket_seconds, bucket_seconds, start_ts, end_ts),
                ).fetchall()

            for row in aggregate_rows:
                bucket_ts = int(row["bucket_ts"])
                if bucket_ts not in bucket_metrics:
                    continue

                bucket_metrics[bucket_ts] = {
                    "rx_count": int(row["rx_count"] or 0),
                    "tx_count": int(row["tx_count"] or 0),
                    "drop_count": int(row["drop_count"] or 0),
                    "avg_rssi": row["avg_rssi"],
                    "avg_snr": row["avg_snr"],
                    "avg_length": row["avg_length"],
                    "avg_score": row["avg_score"],
                    "neighbor_count": None,
                }

            for row in packet_type_rows:
                bucket_ts = int(row["bucket_ts"])
                if bucket_ts not in bucket_packet_types:
                    continue

                type_bucket = int(row["type_bucket"])
                type_name = f"type_{type_bucket}" if 0 <= type_bucket <= 15 else "type_other"
                bucket_packet_types[bucket_ts][type_name] = int(row["count"] or 0)

            for timestamp in timestamps:
                bucket = bucket_metrics[timestamp]
                for name in metric_names:
                    metrics[name].append(bucket[name])

                packet_bucket = bucket_packet_types[timestamp]
                for name in packet_type_names:
                    packet_types[name].append(packet_bucket[name])

            return {
                "start_time": aligned_start,
                "end_time": aligned_end,
                "step": bucket_seconds,
                "timestamps": timestamps,
                "data_sources": metric_names + packet_type_names,
                "packet_types": packet_types,
                "metrics": metrics,
                "data_source": "sqlite",
                "counter_mode": "bucket_count",
            }
        except Exception as e:
            logger.error(f"Failed to get SQLite metrics data: {e}", exc_info=True)
            raise

    def get_recent_packets(self, limit: int = 100) -> list:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row

                packets = conn.execute(
                    """
                    SELECT
                        id,
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        upstream_hash, upstream_hash_size,
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
                        id,
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        upstream_hash, upstream_hash_size,
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
                    buckets[bucket_ts] = {
                        "timestamp": bucket_ts,
                        "rx_ms": 0.0,
                        "tx_ms": 0.0,
                        "rx_count": 0,
                        "tx_count": 0,
                    }
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
                        id,
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        upstream_hash, upstream_hash_size,
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

    def get_packet_by_id(self, packet_id: int) -> Optional[dict]:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row

                packet = conn.execute(
                    """
                    SELECT
                        id,
                        timestamp, type, route, length, rssi, snr, score,
                        transmitted, is_duplicate, drop_reason, src_hash, dst_hash, path_hash,
                        upstream_hash, upstream_hash_size,
                        header, transport_codes, payload, payload_length,
                        tx_delay_ms, packet_hash, original_path, forwarded_path, raw_packet,
                        lbt_attempts, lbt_backoff_delays_ms, lbt_channel_busy
                    FROM packets
                    WHERE id = ?
                """,
                    (packet_id,),
                ).fetchone()

                return dict(packet) if packet else None

        except Exception as e:
            logger.error(f"Failed to get packet by id: {e}")
            return None

    def get_neighbor_link_history(
        self,
        *,
        peer_hash: str,
        path_hash_size: int,
        hours: int = 24,
        limit: int = 1000,
    ) -> list:
        try:
            normalized_hash = str(peer_hash or "").strip().upper()
            if not normalized_hash:
                return []

            path_hash_size = int(path_hash_size)
            hours = max(1, int(hours))
            limit = max(1, min(int(limit), 5000))
            cutoff = time.time() - (hours * 3600)

            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT
                        timestamp,
                        rssi,
                        snr,
                        score,
                        is_duplicate,
                        packet_hash,
                        type,
                        route,
                        original_path
                    FROM packets INDEXED BY idx_packets_upstream_time
                    WHERE upstream_hash = ?
                      AND upstream_hash_size = ?
                      AND timestamp >= ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (normalized_hash, path_hash_size, cutoff, limit),
                ).fetchall()

                history = []
                for row in rows:
                    hop_count = None
                    original_path = row["original_path"]
                    if original_path:
                        try:
                            parsed = json.loads(original_path)
                            if isinstance(parsed, list):
                                hop_count = len(parsed)
                        except Exception:
                            hop_count = None

                    history.append(
                        {
                            "timestamp": row["timestamp"],
                            "rssi": row["rssi"],
                            "snr": row["snr"],
                            "score": row["score"],
                            "is_duplicate": bool(row["is_duplicate"]),
                            "packet_hash": row["packet_hash"],
                            "packet_type": row["type"],
                            "route_type": row["route"],
                            "path_hop_count": hop_count,
                        }
                    )

                history.reverse()
                return history
        except Exception as e:
            logger.error(f"Failed to get neighbor link history: {e}")
            return []

    def get_packet_type_stats(self, hours: int = 24) -> dict:
        try:
            now = time.time()
            cached = self._packet_type_stats_cache.get(hours)
            if cached and (now - cached["timestamp"]) < self._hot_cache_ttl_sec:
                return cached["value"]
            cutoff = now - (hours * 3600)

            # Align with openhop-core feat/newRadios PAYLOAD_TYPES (0x0B = CONTROL)
            try:
                from openhop_core.protocol.utils import PAYLOAD_TYPES as _PT

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

                # See get_packet_stats: force the timestamp range scan so the
                # windowed GROUP BY doesn't degrade into a full-table scan.
                type_rows = conn.execute(
                    """
                    SELECT type, COUNT(*) as count
                    FROM packets INDEXED BY idx_packets_timestamp
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

                result = {
                    "hours": hours,
                    "packet_type_totals": type_counts,
                    "total_packets": sum(type_counts.values()),
                    "period": f"{hours} hours",
                    "data_source": "sqlite",
                }
                self._packet_type_stats_cache[hours] = {"timestamp": now, "value": result}
                return result

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

            tables_with_timestamp = [
                "packets",
                "adverts",
                "noise_floor",
                "crc_errors",
                "room_messages",
                "companion_messages",
            ]
            stats_queries = {
                "packets": "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM packets",
                "adverts": "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM adverts",
                "noise_floor": "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM noise_floor",
                "crc_errors": "SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM crc_errors",
                "room_messages": "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM room_messages",
                "companion_messages": "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM companion_messages",
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
            count_queries = {
                "transport_keys": "SELECT COUNT(*) FROM transport_keys",
                "api_tokens": "SELECT COUNT(*) FROM api_tokens",
                "room_client_sync": "SELECT COUNT(*) FROM room_client_sync",
                "companion_contacts": "SELECT COUNT(*) FROM companion_contacts",
                "companion_channels": "SELECT COUNT(*) FROM companion_channels",
                "companion_prefs": "SELECT COUNT(*) FROM companion_prefs",
                "migrations": "SELECT COUNT(*) FROM migrations",
            }

            table_info = []
            with self._connect() as conn:
                # Get actual tables present in the database
                existing = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }

                for table in tables_with_timestamp:
                    if table not in existing:
                        continue
                    row = conn.execute(stats_queries[table]).fetchone()
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
                    count = conn.execute(count_queries[table]).fetchone()[0]
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

        purge_queries = {
            "packets": "DELETE FROM packets",
            "adverts": "DELETE FROM adverts",
            "noise_floor": "DELETE FROM noise_floor",
            "crc_errors": "DELETE FROM crc_errors",
            "room_messages": "DELETE FROM room_messages",
            "room_client_sync": "DELETE FROM room_client_sync",
            "companion_contacts": "DELETE FROM companion_contacts",
            "companion_channels": "DELETE FROM companion_channels",
            "companion_messages": "DELETE FROM companion_messages",
            "companion_prefs": "DELETE FROM companion_prefs",
        }

        try:
            with self._connect() as conn:
                result = conn.execute(purge_queries[table_name])
                if result.rowcount > 0 and table_name.startswith("companion_"):
                    # A bulk companion-state delete cannot be represented as
                    # ordinary per-row sync events. Rotate the cursor epoch in
                    # this same transaction so a crash can never commit the
                    # delete while leaving clients on a falsely-valid cursor.
                    self._companion_rotate_epoch_in_transaction(conn)
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

    def cleanup_old_data(
        self,
        days: int = DEFAULT_RETENTION_DAYS,
        companion_events_days: Optional[int] = None,
    ):
        """Prune retention-bounded tables, including the companion journal.

        ``companion_events_days`` mirrors how the caller reads
        ``storage.retention.sqlite_cleanup_days`` for ``days`` (see
        engine.py's periodic cleanup call): it should be read from
        ``storage.retention.companion_events_days`` (default 31) and passed
        in. When omitted (e.g. an existing call site that only knows about
        ``days``), it defaults to the same 31-day default as the config key,
        so companion journal/history pruning still runs on the existing
        cleanup schedule without requiring every caller to be updated.
        Invalid settings and storage failures propagate to the caller; the
        periodic engine loop logs them without reporting a false success.
        """
        packet_retention = validate_retention_days(
            days,
            "storage.retention.sqlite_cleanup_days",
        )
        companion_retention = validate_retention_days(
            (DEFAULT_RETENTION_DAYS if companion_events_days is None else companion_events_days),
            "storage.retention.companion_events_days",
        )
        cutoff = time.time() - (packet_retention * 24 * 3600)

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

        events_deleted = self.companion_prune_events(companion_retention)
        consumed_deleted = self.companion_prune_consumed_messages(companion_retention)
        idempotency_deleted = self.companion_idempotency_prune()
        if events_deleted > 0 or consumed_deleted > 0 or idempotency_deleted > 0:
            logger.info(
                f"Cleaned up {events_deleted} old companion journal events, "
                f"{consumed_deleted} old consumed companion messages, "
                f"{idempotency_deleted} old companion idempotency records"
            )

    def get_cumulative_counts(self) -> dict:
        now = time.time()
        cached = self._cumulative_counts_cache.get("value")
        cached_ts = float(self._cumulative_counts_cache.get("timestamp", 0.0))
        if cached is not None and (now - cached_ts) < self._cumulative_counts_ttl_sec:
            return cached
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

                result = {
                    "rx_total": int(totals["rx_total"] or 0),
                    "tx_total": int(totals["tx_total"] or 0),
                    "drop_total": int(totals["drop_total"] or 0),
                    "type_counts": type_counts,
                }
                self._cumulative_counts_cache = {"timestamp": now, "value": result}
                return result

        except Exception as e:
            logger.error(f"Failed to get cumulative counts: {e}")
            return {"rx_total": 0, "tx_total": 0, "drop_total": 0, "type_counts": {}}

    def get_adverts_by_contact_type(
        self,
        contact_type: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        hours: Optional[int] = None,
    ) -> List[dict]:

        try:
            if limit is None:
                limit = 500
            if offset is None:
                offset = 0

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
                    query += " LIMIT ? OFFSET ?"
                    params.append(limit)
                    params.append(offset)

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

    def get_adverts_count_by_contact_type(
        self, contact_type: str, hours: Optional[int] = None
    ) -> int:
        """Get total count of adverts for a specific contact type."""
        try:
            with self._connect() as conn:
                query = "SELECT COUNT(*) as total FROM adverts WHERE contact_type = ?"
                params = [contact_type]

                if hours is not None:
                    cutoff = time.time() - (hours * 3600)
                    query += " AND timestamp > ?"
                    params.append(cutoff)

                row = conn.execute(query, params).fetchone()
                return row[0] if row else 0

        except Exception as e:
            logger.error(f"Failed to get adverts count for contact_type '{contact_type}': {e}")
            return 0

    def generate_transport_key(self, name: str, key_length_bytes: int = 16) -> str:
        """
        Generate a transport key using MeshCore-compatible key derivation.

        Args:
            name: The key name to derive the key from
            key_length_bytes: Fallback random key length in bytes (default: 16)

        Returns:
            A base64-encoded transport key derived from the name
        """
        try:
            from openhop_core.protocol.transport_keys import get_auto_key_for

            key_bytes = get_auto_key_for(name)

            # Encode to base64 for safe storage and transmission
            key = base64.b64encode(key_bytes).decode("utf-8")

            logger.debug(
                f"Generated transport key for '{name}' with {len(key_bytes)} bytes ({len(key)} base64 chars)"
            )
            return key

        except Exception as e:
            logger.error(f"Failed to generate transport key using get_auto_key_for: {e}")
            # Fallback to a transport-compatible random key if derivation fails.
            try:
                fallback_length = max(1, int(key_length_bytes))
                random_bytes = secrets.token_bytes(fallback_length)
                key = base64.b64encode(random_bytes).decode("utf-8")
                logger.warning(
                    f"Using fallback random key generation for '{name}' with {fallback_length} bytes"
                )
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
                new_id = cursor.lastrowid
            self._notify_transport_keys_changed()
            return new_id
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
            has_name = name is not None
            has_flood_policy = flood_policy is not None
            has_transport_key = transport_key is not None
            has_parent_id = parent_id is not None
            has_last_used = last_used is not None

            if not any(
                [
                    has_name,
                    has_flood_policy,
                    has_transport_key,
                    has_parent_id,
                    has_last_used,
                ]
            ):
                return False

            params = (
                int(has_name),
                name,
                int(has_flood_policy),
                flood_policy,
                int(has_transport_key),
                transport_key,
                int(has_parent_id),
                parent_id,
                int(has_last_used),
                last_used,
                time.time(),
                key_id,
            )

            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE transport_keys
                    SET
                        name = CASE WHEN ? THEN ? ELSE name END,
                        flood_policy = CASE WHEN ? THEN ? ELSE flood_policy END,
                        transport_key = CASE WHEN ? THEN ? ELSE transport_key END,
                        parent_id = CASE WHEN ? THEN ? ELSE parent_id END,
                        last_used = CASE WHEN ? THEN ? ELSE last_used END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    params,
                )
                changed = cursor.rowcount > 0
            if changed:
                self._notify_transport_keys_changed()
            return changed
        except Exception as e:
            logger.error(f"Failed to update transport key: {e}")
            return False

    def delete_transport_key(self, key_id: int) -> bool:
        try:
            with self._connect() as conn:
                cursor = conn.execute("DELETE FROM transport_keys WHERE id = ?", (key_id,))
                changed = cursor.rowcount > 0
            if changed:
                self._notify_transport_keys_changed()
            return changed
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
                    db_ids.get(node["parent_node_id"]) if node.get("parent_node_id") else None
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

        self._notify_transport_keys_changed()
        return {"applied_nodes": len(ordered), "generated_keys": generated_keys}

    def delete_advert(self, advert_id: int) -> bool:
        try:
            with self._connect() as conn:
                cursor = conn.execute("DELETE FROM adverts WHERE id = ?", (advert_id,))
                self._neighbors_cache = {"timestamp": 0.0, "value": None}
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete advert: {e}")
            return False

    def delete_neighbors_by_pubkey_prefix(self, pubkey_prefix: Optional[str]) -> int:
        """Delete neighbor adverts by pubkey prefix (or all when prefix is None)."""
        try:
            with self._connect() as conn:
                if pubkey_prefix is None:
                    cursor = conn.execute("DELETE FROM adverts")
                else:
                    cursor = conn.execute(
                        "DELETE FROM adverts WHERE lower(pubkey) LIKE ?",
                        (f"{pubkey_prefix.lower()}%",),
                    )
                self._neighbors_cache = {"timestamp": 0.0, "value": None}
                return int(cursor.rowcount)
        except Exception as e:
            logger.error(f"Failed to delete neighbors by prefix: {e}")
            raise

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
        """Insert or update client sync state without clobbering unspecified fields."""
        try:
            allowed_fields = {
                "sync_since",
                "pending_ack_crc",
                "push_post_timestamp",
                "ack_timeout_time",
                "push_failures",
                "last_activity",
            }
            unknown_fields = set(kwargs) - allowed_fields
            if unknown_fields:
                logger.error(
                    "Refusing unknown room client sync fields: %r",
                    sorted(unknown_fields),
                )
                return False

            with self._connect() as conn:
                now = time.time()
                update_fields = dict(kwargs)
                update_fields["updated_at"] = now

                # INSERT must satisfy NOT NULL columns (last_activity), while
                # ON CONFLICT updates should only touch supplied fields.
                insert_fields = dict(update_fields)
                if insert_fields.get("last_activity") is None:
                    insert_fields["last_activity"] = now

                columns = ["room_hash", "client_pubkey"] + list(insert_fields.keys())
                placeholders = ["?"] * len(columns)
                values = [room_hash, client_pubkey] + list(insert_fields.values())

                # Update only supplied columns on conflict so partial updates don't
                # reset counters/state such as push_failures. Every interpolated
                # identifier came through the allowlist above; values stay bound.
                update_set = ", ".join(f"{col}=excluded.{col}" for col in update_fields.keys())
                query_template = """
                    INSERT INTO room_client_sync ({columns})
                    VALUES ({placeholders})
                    ON CONFLICT(room_hash, client_pubkey)
                    DO UPDATE SET {update_set}
                """
                query = query_template.format(  # nosec B608
                    columns=", ".join(columns),
                    placeholders=", ".join(placeholders),
                    update_set=update_set,
                )
                conn.execute(query, values)
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
    def companion_count_contacts(self, companion_hash: str) -> int:
        """Return the number of persisted contacts, or 0 on a legacy read failure."""
        try:
            return self.companion_count_contacts_strict(companion_hash)
        except CompanionStorageError as e:
            logger.error(f"Failed to count companion contacts for {companion_hash}: {e}")
            return 0

    def companion_count_contacts_strict(self, companion_hash: str) -> int:
        """Return the persisted contact count, raising when it cannot be proved."""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM companion_contacts WHERE companion_hash = ?",
                    (companion_hash,),
                )
                row = cursor.fetchone()
                return int(row[0]) if row else 0
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to count companion contacts for {companion_hash}"
            ) from e

    def companion_load_contacts(self, companion_hash: str) -> Optional[List[Dict]]:
        """Load contacts for a companion from storage.

        Returns [] when the companion has no persisted contacts, or None when
        the load failed — callers must not treat a failed load as "no data".
        """
        try:
            return self.companion_load_contacts_strict(companion_hash)
        except CompanionStorageError as e:
            logger.error(f"Failed to load companion contacts for {companion_hash}: {e}")
            return None

    def companion_load_contacts_strict(self, companion_hash: str) -> List[Dict]:
        """Load contacts, raising when storage cannot prove the result."""

        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT pubkey, name, adv_type, flags, out_path_len, out_path,
                           last_advert_timestamp, last_advert_packet,
                           lastmod, gps_lat, gps_lon, sync_since
                    FROM companion_contacts WHERE companion_hash = ?
                """,
                    (companion_hash,),
                )
                contacts = []
                for row in cursor.fetchall():
                    contact = dict(row)
                    contact["gps_lat"] = _finite_storage_float(
                        contact["gps_lat"],
                        "companion contact gps_lat",
                    )
                    contact["gps_lon"] = _finite_storage_float(
                        contact["gps_lon"],
                        "companion contact gps_lon",
                    )
                    contacts.append(contact)
                return contacts
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to load companion contacts for {companion_hash}"
            ) from e

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
                        c.get("last_advert_packet"),
                        c.get("lastmod", 0),
                        _finite_storage_float(
                            c.get("gps_lat", 0.0),
                            "companion contact gps_lat",
                        ),
                        _finite_storage_float(
                            c.get("gps_lon", 0.0),
                            "companion contact gps_lon",
                        ),
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
                         last_advert_timestamp, last_advert_packet,
                         lastmod, gps_lat, gps_lon, sync_since, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        rows,
                    )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to save companion contacts: {e}")
            return False

    def companion_delete_contact(self, companion_hash: str, pubkey: bytes) -> bool:
        """Delete one contact. Returns True only if a row was actually removed.

        Targeted counterpart to ``companion_save_contacts``, which persists a
        removal only by rewriting the whole table (DELETE-all + re-insert).
        The API's per-contact delete should not rewrite every row, and needs to
        distinguish "deleted" from "was not there" for its 404.
        """
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM companion_contacts WHERE companion_hash = ? AND pubkey = ?",
                    (companion_hash, pubkey),
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete companion contact: {e}")
            return False

    @staticmethod
    def _companion_upsert_contact_row(
        conn: sqlite3.Connection,
        companion_hash: str,
        contact: Dict[str, Any],
        now: float,
    ) -> None:
        """Upsert one contact on an existing transaction."""
        conn.execute(
            """
            INSERT INTO companion_contacts
            (companion_hash, pubkey, name, adv_type, flags, out_path_len, out_path,
             last_advert_timestamp, last_advert_packet,
             lastmod, gps_lat, gps_lon, sync_since, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(companion_hash, pubkey)
            DO UPDATE SET
                name=excluded.name, adv_type=excluded.adv_type,
                flags=excluded.flags, out_path_len=excluded.out_path_len,
                out_path=excluded.out_path,
                last_advert_timestamp=excluded.last_advert_timestamp,
                last_advert_packet=excluded.last_advert_packet,
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
                contact.get("last_advert_packet"),
                contact.get("lastmod", 0),
                _finite_storage_float(
                    contact.get("gps_lat", 0.0),
                    "companion contact gps_lat",
                ),
                _finite_storage_float(
                    contact.get("gps_lon", 0.0),
                    "companion contact gps_lon",
                ),
                contact.get("sync_since", 0),
                now,
            ),
        )

    def companion_upsert_contact(self, companion_hash: str, contact: dict) -> bool:
        """Insert or update a single contact for a companion in storage."""
        try:
            with self._connect() as conn:
                self._companion_upsert_contact_row(
                    conn,
                    companion_hash,
                    contact,
                    time.time(),
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to upsert companion contact: {e}")
            return False

    def companion_upsert_contact_with_event(
        self,
        companion_hash: str,
        contact: Dict[str, Any],
        change: str = "update",
    ) -> Dict[str, Any]:
        """Upsert one contact and append its event in the same transaction."""
        try:
            payload = dict(contact)
            payload["change"] = change
            with self._connect() as conn:
                now = time.time()
                self._companion_upsert_contact_row(
                    conn,
                    companion_hash,
                    contact,
                    now,
                )
                event = self._companion_append_event_row(
                    conn,
                    companion_hash,
                    "contact",
                    payload,
                    created_at=now,
                )
                conn.commit()
                return {"event_seq": event["seq"], "event": event}
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to store contact event for companion {companion_hash}"
            ) from e

    def companion_delete_contact_with_event(
        self, companion_hash: str, pubkey: bytes
    ) -> Optional[Dict[str, Any]]:
        """Delete one contact and journal the removal atomically."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """
                    SELECT pubkey, name, adv_type, flags, out_path_len, out_path,
                           last_advert_timestamp, last_advert_packet, lastmod,
                           gps_lat, gps_lon, sync_since
                    FROM companion_contacts
                    WHERE companion_hash = ? AND pubkey = ?
                    """,
                    (companion_hash, pubkey),
                ).fetchone()
                if row is None:
                    return None
                conn.execute(
                    """
                    DELETE FROM companion_contacts
                    WHERE companion_hash = ? AND pubkey = ?
                    """,
                    (companion_hash, pubkey),
                )
                payload = dict(row)
                payload["change"] = "remove"
                event = self._companion_append_event_row(
                    conn,
                    companion_hash,
                    "contact",
                    payload,
                )
                conn.commit()
                return {"event_seq": event["seq"], "event": event}
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to delete contact event for companion {companion_hash}"
            ) from e

    def companion_apply_contact_changes(
        self,
        companion_hash: str,
        changes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Commit a bounded contact diff and all of its events atomically.

        A single advert can replace the oldest contact and add a new one.
        Treating that as two independent transactions would let snapshots see
        or restore half of the operation after a storage failure.
        """
        try:
            events = []
            with self._connect() as conn:
                now = time.time()
                for item in changes:
                    change = str(item.get("change") or "update")
                    contact = dict(item.get("contact") or {})
                    pubkey = self._companion_key_bytes(contact.get("pubkey"))
                    if len(pubkey) != 32:
                        raise ValueError("Companion contact public key must be 32 bytes")
                    contact["pubkey"] = pubkey
                    if change == "remove":
                        conn.execute(
                            """
                            DELETE FROM companion_contacts
                            WHERE companion_hash = ? AND pubkey = ?
                            """,
                            (companion_hash, pubkey),
                        )
                    elif change in {"new", "update", "path"}:
                        self._companion_upsert_contact_row(
                            conn,
                            companion_hash,
                            contact,
                            now,
                        )
                    else:
                        raise ValueError(f"Unknown companion contact change: {change}")
                    payload = dict(contact)
                    payload["change"] = change
                    events.append(
                        self._companion_append_event_row(
                            conn,
                            companion_hash,
                            "contact",
                            payload,
                            created_at=now,
                        )
                    )
                conn.commit()
                return {"events": events}
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to apply contact changes for companion {companion_hash}"
            ) from e

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
                        _finite_storage_float(
                            row["latitude"] or 0.0,
                            "imported contact gps_lat",
                        ),
                        _finite_storage_float(
                            row["longitude"] or 0.0,
                            "imported contact gps_lon",
                        ),
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
        """Load one valid prefs object, returning ``None`` only when absent."""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "SELECT prefs_json FROM companion_prefs WHERE companion_hash = ?",
                    (companion_hash,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                prefs = strict_json_loads(row[0])
                if not isinstance(prefs, dict):
                    raise CompanionStorageError(
                        f"Persisted prefs for companion {companion_hash} are not a JSON object"
                    )
                return prefs
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to load prefs for companion {companion_hash}"
            ) from e

    def companion_save_prefs(self, companion_hash: str, prefs: Dict) -> bool:
        """Persist prefs for a companion as JSON. Upserts by companion_hash."""
        try:
            prefs_json = json.dumps(prefs, allow_nan=False)
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

    def companion_save_prefs_with_event(
        self,
        companion_hash: str,
        prefs: Dict[str, Any],
        event_fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Persist full prefs and journal only explicitly public changed fields."""
        try:
            prefs_json = json.dumps(
                self._companion_json_safe(prefs),
                allow_nan=False,
            )
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO companion_prefs (companion_hash, prefs_json)
                    VALUES (?, ?)
                    ON CONFLICT(companion_hash) DO UPDATE SET
                        prefs_json = excluded.prefs_json
                    """,
                    (str(companion_hash), prefs_json),
                )
                event = self._companion_append_event_row(
                    conn,
                    companion_hash,
                    "prefs",
                    event_fields,
                )
                conn.commit()
                return {"event_seq": event["seq"], "event": event}
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to store prefs event for companion {companion_hash}"
            ) from e

    def companion_count_channels(self, companion_hash: str) -> int:
        """Return the number of persisted channels for a companion."""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM companion_channels WHERE companion_hash = ?",
                    (companion_hash,),
                )
                row = cursor.fetchone()
                return int(row[0]) if row else 0
        except Exception as e:
            logger.error(f"Failed to count companion channels: {e}")
            return 0

    def companion_load_channels(self, companion_hash: str) -> Optional[List[Dict]]:
        """Load channels for a companion from storage.

        Returns [] when the companion has no persisted channels, or None when
        the load failed — callers must not treat a failed load as "no data".
        """
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
            logger.error(f"Failed to load companion channels for {companion_hash}: {e}")
            return None

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

    def companion_set_channel_with_event(
        self,
        companion_hash: str,
        channel_idx: int,
        name: Optional[str],
        secret: Optional[bytes],
    ) -> Dict[str, Any]:
        """Set or remove one channel and append a secret-free event atomically."""
        try:
            index = int(channel_idx)
            with self._connect() as conn:
                now = time.time()
                conn.execute(
                    """
                    DELETE FROM companion_channels
                    WHERE companion_hash = ? AND channel_idx = ?
                    """,
                    (companion_hash, index),
                )
                removing = name is None or secret is None
                if not removing:
                    conn.execute(
                        """
                        INSERT INTO companion_channels
                        (companion_hash, channel_idx, name, secret, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (companion_hash, index, str(name), bytes(secret), now),
                    )
                payload = {
                    "index": index,
                    "name": None if removing else str(name),
                    "change": "remove" if removing else "update",
                }
                event = self._companion_append_event_row(
                    conn,
                    companion_hash,
                    "channel",
                    payload,
                    created_at=now,
                )
                conn.commit()
                return {"event_seq": event["seq"], "event": event}
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to store channel event for companion {companion_hash}"
            ) from e

    def companion_count_messages(self, companion_hash: str) -> int:
        """Return the number of live (unconsumed) queued messages for a companion.

        Soft-consumed rows are retained history, not part of the pending
        queue, so they're excluded here to stay consistent with
        companion_load_messages (callers that compare a load's row count
        against this count to detect a silently-failed load require both to
        reflect the same query).
        """
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    SELECT COUNT(*) FROM companion_messages
                    WHERE companion_hash = ? AND consumed_at IS NULL
                    """,
                    (companion_hash,),
                )
                row = cursor.fetchone()
                return int(row[0]) if row else 0
        except Exception as e:
            logger.error(f"Failed to count companion messages: {e}")
            return 0

    def companion_load_messages(
        self, companion_hash: str, limit: int = 100
    ) -> Optional[List[Dict]]:
        """Load live (unconsumed) queued messages, oldest first for queue order.

        Soft-consumed rows (already popped, or evicted to make room) are
        durable history now, not part of the pending queue, so they're
        excluded here — this is the boot-restore read path and must reflect
        only what's still waiting for delivery. Returns [] when the companion
        has no pending messages, or None when the load failed — callers must
        not treat a failed load as "no data".
        """
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT sender_key, txt_type, timestamp, text, is_channel, channel_idx,
                           path_len, sender_prefix, snr, rssi, channel_data_type,
                           channel_data_payload
                    FROM companion_messages WHERE companion_hash = ? AND consumed_at IS NULL
                    ORDER BY id ASC LIMIT ?
                """,
                    (companion_hash, limit),
                )
                rows = [dict(row) for row in cursor.fetchall()]
                for msg in rows:
                    msg["sender_prefix"] = bytes.fromhex(msg.get("sender_prefix") or "")
                    msg["snr"] = float(msg.get("snr") or 0.0)
                    msg["rssi"] = int(msg.get("rssi") or 0)
                    msg["channel_data_type"] = int(msg.get("channel_data_type") or 0)
                    msg["channel_data_payload"] = bytes(msg.get("channel_data_payload") or b"")
                return rows
        except Exception as e:
            logger.error(f"Failed to load companion messages for {companion_hash}: {e}")
            return None

    @staticmethod
    def _companion_packet_hash(value: Any) -> Optional[str]:
        if isinstance(value, bytes):
            return value.hex() if value else None
        text = str(value).strip() if value is not None else ""
        return text or None

    @staticmethod
    def _companion_key_bytes(value: Any) -> bytes:
        if value is None:
            return b""
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, str):
            text = value.removeprefix("0x")
            try:
                return bytes.fromhex(text)
            except ValueError as e:
                raise ValueError("companion key must be bytes or hexadecimal text") from e
        return bytes(value)

    @staticmethod
    def _validate_companion_message_row(message: Dict[str, Any]) -> None:
        """Validate the complete persisted row before a strict public read.

        SQLite's dynamic typing means a damaged row can satisfy the SELECT
        while carrying text in an INTEGER column, invalid enum values, or
        malformed blobs.  Public readers must reject that row instead of
        coercing it into an authoritative-looking message.
        """

        required_fields = {
            "id",
            "companion_hash",
            "sender_key",
            "recipient_key",
            "txt_type",
            "timestamp",
            "text",
            "is_channel",
            "channel_idx",
            "path_len",
            "sender_prefix",
            "snr",
            "rssi",
            "channel_data_type",
            "channel_data_payload",
            "packet_hash",
            "created_at",
            "consumed_at",
            "observation_count",
            "unique_path_count",
            "direction",
            "state",
            "expected_ack",
            "source",
            "pending_for_frame",
        }
        missing = sorted(required_fields.difference(message))
        if missing:
            raise ValueError("companion message row is missing " + ", ".join(missing))

        _strict_storage_integer(
            message["id"],
            "companion message id",
            minimum=1,
            maximum=_SQLITE_MAX_ROW_ID,
        )

        companion_hash = message["companion_hash"]
        if (
            not isinstance(companion_hash, str)
            or len(companion_hash) != 4
            or not companion_hash.startswith("0x")
            or companion_hash != companion_hash.lower()
            or any(character not in _HEX_DIGITS for character in companion_hash[2:])
        ):
            raise ValueError(
                "companion message companion_hash must match 0x followed by two lowercase hex digits"
            )

        sender_key = message["sender_key"]
        if not isinstance(sender_key, bytes) or len(sender_key) not in {0, 32}:
            raise ValueError("companion message sender_key must contain zero or 32 bytes")
        recipient_key = message["recipient_key"]
        if recipient_key is not None and (
            not isinstance(recipient_key, bytes) or len(recipient_key) not in {0, 32}
        ):
            raise ValueError("companion message recipient_key must contain zero or 32 bytes")

        _strict_storage_integer(
            message["txt_type"],
            "companion message txt_type",
            minimum=0,
            maximum=0x3F,
        )
        _strict_storage_integer(
            message["timestamp"],
            "companion message timestamp",
            minimum=0,
            maximum=_UINT32_MAX,
        )
        if not isinstance(message["text"], str):
            raise ValueError("companion message text must be text")
        try:
            text_size = len(message["text"].encode("utf-8"))
        except UnicodeEncodeError as e:
            raise ValueError("companion message text must be valid UTF-8") from e
        if text_size > MAX_TEXT_LEN:
            raise ValueError(f"companion message text must not exceed {MAX_TEXT_LEN} UTF-8 bytes")
        _strict_storage_integer(
            message["is_channel"],
            "companion message is_channel",
            minimum=0,
            maximum=1,
        )
        _strict_storage_integer(
            message["channel_idx"],
            "companion message channel_idx",
            minimum=0,
            maximum=_UINT8_MAX,
        )
        _strict_storage_integer(
            message["path_len"],
            "companion message path_len",
            minimum=0,
            maximum=_UINT8_MAX,
        )
        sender_prefix = _strict_hex_storage_text(
            message["sender_prefix"],
            "companion message sender_prefix",
            allow_empty=True,
        )
        if len(sender_prefix) not in {0, 8}:
            raise ValueError("companion message sender_prefix must contain zero or four bytes")

        _strict_storage_float(
            message["snr"],
            "companion message snr",
            nullable=True,
        )
        if message["rssi"] is not None:
            _strict_storage_integer(message["rssi"], "companion message rssi")
        if message["channel_data_type"] is not None:
            _strict_storage_integer(
                message["channel_data_type"],
                "companion message channel_data_type",
                minimum=0,
                maximum=_UINT16_MAX,
            )
        channel_data_payload = message["channel_data_payload"]
        if channel_data_payload is not None and not isinstance(channel_data_payload, bytes):
            raise ValueError("companion message channel_data_payload must be bytes")
        if channel_data_payload is not None and len(channel_data_payload) > MAX_GROUP_DATA_LENGTH:
            raise ValueError(
                "companion message channel_data_payload must not exceed "
                f"{MAX_GROUP_DATA_LENGTH} bytes"
            )

        _strict_companion_packet_hash(message["packet_hash"])
        _strict_storage_float(
            message["created_at"],
            "companion message created_at",
        )
        _strict_storage_float(
            message["consumed_at"],
            "companion message consumed_at",
            nullable=True,
        )

        observation_count = _strict_storage_integer(
            message["observation_count"],
            "companion message observation_count",
            minimum=0,
        )
        unique_path_count = _strict_storage_integer(
            message["unique_path_count"],
            "companion message unique_path_count",
            minimum=0,
        )
        if unique_path_count > observation_count:
            raise ValueError("companion message unique_path_count cannot exceed observation_count")

        direction = message["direction"]
        if direction not in _COMPANION_MESSAGE_DIRECTIONS:
            raise ValueError("companion message direction must be 'in' or 'out'")
        state = message["state"]
        if state not in _COMPANION_MESSAGE_STATES:
            raise ValueError("companion message state is invalid")
        if direction == "in" and state != "received":
            raise ValueError("an inbound companion message must be in received state")
        if direction == "out" and state == "received":
            raise ValueError("an outbound companion message cannot be in received state")

        expected_ack = message["expected_ack"]
        if expected_ack is not None:
            _strict_storage_integer(
                expected_ack,
                "companion message expected_ack",
                minimum=0,
                maximum=_UINT32_MAX,
            )
        if direction == "in" and expected_ack is not None:
            raise ValueError("an inbound companion message cannot expect an ACK")
        source = message["source"]
        if source is not None and source not in _COMPANION_MESSAGE_SOURCES:
            raise ValueError("companion message source is invalid")
        if direction == "in" and source not in {None, "radio"}:
            raise ValueError("an inbound companion message has an invalid source")
        if direction == "out" and source not in {"rest", "frame", "operator"}:
            raise ValueError("an outbound companion message has an invalid source")

        pending_for_frame = _strict_storage_integer(
            message["pending_for_frame"],
            "companion message pending_for_frame",
            minimum=0,
            maximum=1,
        )
        if pending_for_frame and message["consumed_at"] is not None:
            raise ValueError("a consumed companion message cannot remain pending for Frame")
        if not pending_for_frame and message["consumed_at"] is None:
            raise ValueError("a non-pending companion message must have a consumed timestamp")
        if direction == "out" and pending_for_frame:
            raise ValueError("an outbound companion message cannot be pending for Frame")

    @staticmethod
    def _companion_message_row_to_dict(
        row: sqlite3.Row,
        *,
        strict: bool = False,
    ) -> Dict[str, Any]:
        message = dict(row)
        if strict:
            SQLiteHandler._validate_companion_message_row(message)
        message["sender_key"] = bytes(message.get("sender_key") or b"").hex()
        message["recipient_key"] = bytes(message.get("recipient_key") or b"").hex()
        message["channel_data_payload"] = bytes(message.get("channel_data_payload") or b"").hex()
        if strict:
            message["sender_prefix"] = message["sender_prefix"].lower()
        message["is_channel"] = bool(message.get("is_channel"))
        message["pending_for_frame"] = (
            bool(message.get("pending_for_frame")) and message.get("consumed_at") is None
        )
        message["snr"] = _finite_storage_float(
            message.get("snr") or 0.0,
            "companion message snr",
        )
        message["created_at"] = _finite_storage_float(
            message["created_at"],
            "companion message created_at",
        )
        if message.get("consumed_at") is not None:
            message["consumed_at"] = _finite_storage_float(
                message["consumed_at"],
                "companion message consumed_at",
            )
        message["rssi"] = int(message.get("rssi") or 0)
        message["channel_data_type"] = int(message.get("channel_data_type") or 0)
        return message

    @staticmethod
    def _companion_message_select() -> str:
        return """
            SELECT id, companion_hash, sender_key, recipient_key, txt_type,
                   timestamp, text, is_channel, channel_idx, path_len,
                   sender_prefix, snr, rssi, channel_data_type,
                   channel_data_payload, packet_hash, created_at, consumed_at,
                   observation_count, unique_path_count, direction, state,
                   expected_ack, source, pending_for_frame
            FROM companion_messages
        """

    def companion_store_inbound_message(
        self,
        companion_hash: str,
        msg: Dict[str, Any],
        max_pending: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Atomically store inbound history, pending state, and its journal event.

        ``max_pending`` limits only frame delivery.  A message that cannot fit
        is still committed to durable history and journaled exactly once.
        """
        try:
            packet_hash = self._companion_packet_hash(msg.get("packet_hash"))
            sender_prefix = msg.get("sender_prefix", b"")
            if not isinstance(sender_prefix, str):
                sender_prefix = bytes(sender_prefix or b"").hex()
            now = time.time()
            queued = max_pending is None or int(max_pending) > 0
            consumed_at = None if queued else now

            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO companion_messages
                    (companion_hash, sender_key, recipient_key, txt_type,
                     timestamp, text, is_channel, channel_idx, path_len,
                     sender_prefix, snr, rssi, channel_data_type,
                     channel_data_payload, packet_hash, created_at, consumed_at,
                     observation_count, unique_path_count, direction, state,
                     source, pending_for_frame)
                    VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            1, 1, 'in', 'received', 'radio', ?)
                    """,
                    (
                        companion_hash,
                        self._companion_key_bytes(msg.get("sender_key", b"")),
                        int(msg.get("txt_type", 0)),
                        int(msg.get("timestamp", 0)),
                        str(msg.get("text", "")),
                        1 if msg.get("is_channel", False) else 0,
                        int(msg.get("channel_idx", 0)),
                        int(msg.get("path_len", 0)),
                        sender_prefix,
                        _finite_storage_float(
                            msg.get("snr") or 0.0,
                            "companion message snr",
                        ),
                        int(msg.get("rssi") or 0),
                        int(msg.get("channel_data_type") or 0),
                        bytes(msg.get("channel_data_payload") or b""),
                        packet_hash,
                        now,
                        consumed_at,
                        1 if queued else 0,
                    ),
                )
                inserted = cursor.rowcount > 0
                if not inserted:
                    if packet_hash is None:
                        raise CompanionStorageError(
                            "Inbound message insert was ignored without a packet hash"
                        )
                    existing = conn.execute(
                        self._companion_message_select()
                        + """
                          WHERE companion_hash = ? AND packet_hash = ?
                            AND direction = 'in'
                          """,
                        (companion_hash, packet_hash),
                    ).fetchone()
                    if existing is None:
                        raise CompanionStorageError(
                            "Inbound message deduplication row could not be read"
                        )
                    message = self._companion_message_row_to_dict(existing)
                    return {
                        "inserted": False,
                        "queued": message["pending_for_frame"],
                        "message_id": message["id"],
                        "message": message,
                        "event_seq": None,
                        "event": None,
                    }

                message_id = int(cursor.lastrowid)
                if queued and max_pending is not None:
                    limit = max(0, int(max_pending))
                    count = int(
                        conn.execute(
                            """
                            SELECT COUNT(*) FROM companion_messages
                            WHERE companion_hash = ? AND pending_for_frame = 1
                            """,
                            (companion_hash,),
                        ).fetchone()[0]
                    )
                    excess = max(0, count - limit)
                    if excess:
                        evict_ids = [
                            int(row[0])
                            for row in conn.execute(
                                """
                                SELECT id FROM companion_messages
                                WHERE companion_hash = ? AND is_channel = 1
                                  AND pending_for_frame = 1 AND id != ?
                                ORDER BY id ASC LIMIT ?
                                """,
                                (companion_hash, message_id, excess),
                            ).fetchall()
                        ]
                        if evict_ids:
                            conn.executemany(
                                """
                                UPDATE companion_messages
                                SET pending_for_frame = 0, consumed_at = ?
                                WHERE id = ?
                                """,
                                [(now, evict_id) for evict_id in evict_ids],
                            )
                        remaining = excess - len(evict_ids)
                        if remaining > 0:
                            # No retained direct message is displaced.  The new
                            # row remains durable history but is not offered to
                            # the bounded frame queue.
                            conn.execute(
                                """
                                UPDATE companion_messages
                                SET pending_for_frame = 0, consumed_at = ?
                                WHERE id = ?
                                """,
                                (now, message_id),
                            )
                            queued = False

                row = conn.execute(
                    self._companion_message_select() + " WHERE id = ?",
                    (message_id,),
                ).fetchone()
                if row is None:
                    raise CompanionStorageError("Stored inbound message could not be read")
                message = self._companion_message_row_to_dict(row)
                event = self._companion_append_event_row(
                    conn,
                    companion_hash,
                    "message",
                    message,
                    ref_table="companion_messages",
                    ref_id=message_id,
                    packet_hash=packet_hash,
                    created_at=now,
                )
                conn.commit()
                return {
                    "inserted": True,
                    "queued": queued,
                    "message_id": message_id,
                    "message": message,
                    "event_seq": event["seq"],
                    "event": event,
                }
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to store inbound message for companion {companion_hash}"
            ) from e

    def _companion_insert_outbound_message_row(
        self,
        conn: sqlite3.Connection,
        companion_hash: str,
        msg: Dict[str, Any],
        source: str,
        state: str,
        now: float,
    ) -> Dict[str, Any]:
        """Insert an outbound row and event inside the caller's transaction."""
        if source not in {"rest", "frame", "operator"}:
            raise ValueError("source must be 'rest', 'frame', or 'operator'")
        if state not in {"pending", "transmitted", "confirmed", "failed", "indeterminate"}:
            raise ValueError("invalid outbound message state")
        packet_hash = self._companion_packet_hash(msg.get("packet_hash"))
        recipient_key = self._companion_key_bytes(msg.get("recipient_key", msg.get("to")))
        is_channel = (
            bool(msg.get("is_channel"))
            if "is_channel" in msg
            else msg.get("channel_idx") is not None
        )
        cursor = conn.execute(
            """
            INSERT INTO companion_messages
            (companion_hash, sender_key, recipient_key, txt_type,
             timestamp, text, is_channel, channel_idx, path_len,
             sender_prefix, snr, rssi, channel_data_type,
             channel_data_payload, packet_hash, created_at, consumed_at,
             observation_count, unique_path_count, direction, state,
             expected_ack, source, pending_for_frame)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '', 0, 0, 0, X'', ?, ?, ?,
                    0, 0, 'out', ?, ?, ?, 0)
            """,
            (
                companion_hash,
                self._companion_key_bytes(msg.get("sender_key")),
                recipient_key or None,
                int(msg.get("txt_type", 0)),
                int(msg.get("timestamp", now)),
                str(msg.get("text", "")),
                1 if is_channel else 0,
                int(msg.get("channel_idx") or 0),
                packet_hash,
                now,
                now,
                state,
                (int(msg["expected_ack"]) if msg.get("expected_ack") is not None else None),
                source,
            ),
        )
        message_id = int(cursor.lastrowid)
        row = conn.execute(
            self._companion_message_select() + " WHERE id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            raise CompanionStorageError("Stored outbound message could not be read")
        message = self._companion_message_row_to_dict(row)
        event = self._companion_append_event_row(
            conn,
            companion_hash,
            "message",
            message,
            ref_table="companion_messages",
            ref_id=message_id,
            packet_hash=packet_hash,
            created_at=now,
        )
        return {
            "message_id": message_id,
            "message": message,
            "event_seq": event["seq"],
            "event": event,
        }

    def companion_store_outbound_message(
        self,
        companion_hash: str,
        msg: Dict[str, Any],
        source: str,
        state: str = "pending",
    ) -> Dict[str, Any]:
        """Atomically create one outbound message row and journal event."""
        try:
            now = time.time()
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                result = self._companion_insert_outbound_message_row(
                    conn,
                    companion_hash,
                    msg,
                    source,
                    state,
                    now,
                )
                conn.commit()
                return result
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to store outbound message for companion {companion_hash}"
            ) from e

    def _companion_advance_outbound_state_row(
        self,
        conn: sqlite3.Connection,
        companion_hash: str,
        message_id: int,
        state: str,
        packet_hash: Optional[str],
        expected_ack: Optional[int],
        now: float,
    ) -> Dict[str, Any]:
        """Advance one outbound lifecycle inside the caller's transaction."""
        if state not in {
            "pending",
            "transmitted",
            "heard_repeated",
            "confirmed",
            "failed",
            "indeterminate",
        }:
            raise ValueError("invalid outbound message state")
        normalized_hash = self._companion_packet_hash(packet_hash)
        existing = conn.execute(
            """
            SELECT state, packet_hash, expected_ack
            FROM companion_messages
            WHERE id = ? AND companion_hash = ? AND direction = 'out'
            """,
            (int(message_id), companion_hash),
        ).fetchone()
        if existing is None:
            raise CompanionStorageError(f"Outbound message {message_id} does not exist")
        current_state = str(existing["state"])
        allowed_next = {
            "pending": {
                "pending",
                "transmitted",
                "heard_repeated",
                "confirmed",
                "failed",
                "indeterminate",
            },
            "transmitted": {
                "transmitted",
                "heard_repeated",
                "confirmed",
                "indeterminate",
            },
            "indeterminate": {"indeterminate", "heard_repeated", "confirmed"},
            "failed": {"failed", "heard_repeated", "confirmed"},
            "heard_repeated": {"heard_repeated", "confirmed"},
            "confirmed": {"confirmed"},
        }
        effective_state = (
            state if state in allowed_next.get(current_state, {current_state}) else current_state
        )
        next_hash = normalized_hash or existing["packet_hash"]
        next_ack = int(expected_ack) if expected_ack is not None else existing["expected_ack"]
        changed = (
            effective_state != current_state
            or next_hash != existing["packet_hash"]
            or next_ack != existing["expected_ack"]
        )
        if not changed:
            row = conn.execute(
                self._companion_message_select() + " WHERE id = ? AND companion_hash = ?",
                (int(message_id), companion_hash),
            ).fetchone()
            message = self._companion_message_row_to_dict(row)
            return {
                "message_id": int(message_id),
                "message": message,
                "event_seq": None,
                "event": None,
                "transition_applied": effective_state == state,
            }

        conn.execute(
            """
            UPDATE companion_messages
            SET state = ?,
                packet_hash = COALESCE(?, packet_hash),
                expected_ack = COALESCE(?, expected_ack)
            WHERE id = ? AND companion_hash = ? AND direction = 'out'
            """,
            (
                effective_state,
                normalized_hash,
                int(expected_ack) if expected_ack is not None else None,
                int(message_id),
                companion_hash,
            ),
        )
        row = conn.execute(
            self._companion_message_select() + " WHERE id = ? AND companion_hash = ?",
            (int(message_id), companion_hash),
        ).fetchone()
        message = self._companion_message_row_to_dict(row)
        payload = {
            "message_id": int(message_id),
            "state": effective_state,
            "packet_hash": message.get("packet_hash"),
            "expected_ack": message.get("expected_ack"),
        }
        event = self._companion_append_event_row(
            conn,
            companion_hash,
            "message_send_state",
            payload,
            ref_table="companion_messages",
            ref_id=int(message_id),
            packet_hash=message.get("packet_hash"),
            created_at=now,
        )
        return {
            "message_id": int(message_id),
            "message": message,
            "event_seq": event["seq"],
            "event": event,
            "transition_applied": effective_state == state,
        }

    def companion_update_outbound_state(
        self,
        companion_hash: str,
        message_id: int,
        state: str,
        packet_hash: Optional[str] = None,
        expected_ack: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Atomically advance and journal an outbound transmission state.

        Lifecycle evidence is monotonic. In particular, a fast ACK or heard
        repeat may commit before the original send coroutine returns; that
        later ``transmitted`` write must never regress ``confirmed`` or
        ``heard_repeated``.
        """
        try:
            now = time.time()
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                result = self._companion_advance_outbound_state_row(
                    conn,
                    companion_hash,
                    message_id,
                    state,
                    packet_hash,
                    expected_ack,
                    now,
                )
                conn.commit()
                return result
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(f"Failed to update outbound message {message_id}") from e

    def companion_record_outbound_heard_repeat(
        self,
        companion_hash: str,
        message_id: int,
        correlation: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Atomically retain heard-repeat evidence on an outbound message.

        Each post-promotion observation gets a detailed journal event. Copies
        heard before the message row commits are retained as one bounded
        aggregate carrying complete running counts and the latest RF detail.
        The durable lifecycle advances to ``heard_repeated`` unless it is
        already ``confirmed``; confirmation is stronger evidence and is never
        regressed. The observed (truncated) correlation hash is journaled but
        does not replace the message row's original packet hash.
        """
        try:
            now = time.time()
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    """
                    SELECT state
                    FROM companion_messages
                    WHERE id = ? AND companion_hash = ? AND direction = 'out'
                    """,
                    (int(message_id), companion_hash),
                ).fetchone()
                if existing is None:
                    raise CompanionStorageError(f"Outbound message {message_id} does not exist")

                current_state = str(existing["state"])
                if current_state in {
                    "pending",
                    "transmitted",
                    "failed",
                    "indeterminate",
                    "heard_repeated",
                }:
                    effective_state = "heard_repeated"
                else:
                    # ``confirmed`` (and any future terminal state) wins over
                    # later, weaker RF evidence.
                    effective_state = current_state
                if effective_state != current_state:
                    conn.execute(
                        """
                        UPDATE companion_messages
                        SET state = ?
                        WHERE id = ? AND companion_hash = ? AND direction = 'out'
                        """,
                        (effective_state, int(message_id), companion_hash),
                    )

                row = conn.execute(
                    self._companion_message_select() + " WHERE id = ? AND companion_hash = ?",
                    (int(message_id), companion_hash),
                ).fetchone()
                if row is None:
                    raise CompanionStorageError(f"Outbound message {message_id} could not be read")
                message = self._companion_message_row_to_dict(row)
                observed_hash = self._companion_packet_hash(
                    correlation.get("packet_hash")
                ) or message.get("packet_hash")
                payload = {
                    "message_id": int(message_id),
                    "state": effective_state,
                    "packet_hash": observed_hash,
                    "path": correlation.get("path") or [],
                    "terminal_repeater_hash": correlation.get("terminal_hash"),
                    "rssi": correlation.get("rssi"),
                    "snr": correlation.get("snr"),
                    "observed_at": correlation.get("observed_at"),
                    "heard_repeat_count": correlation.get("heard_repeat_count"),
                    "unique_repeater_count": correlation.get("unique_repeater_count"),
                }
                event = self._companion_append_event_row(
                    conn,
                    companion_hash,
                    "message_send_state",
                    payload,
                    ref_table="companion_messages",
                    ref_id=int(message_id),
                    packet_hash=observed_hash,
                    created_at=now,
                )
                conn.commit()
                return {
                    "message_id": int(message_id),
                    "message": message,
                    "event_seq": event["seq"],
                    "event": event,
                    "transition_applied": effective_state == "heard_repeated",
                }
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to record heard repeat for outbound message {message_id}"
            ) from e

    def companion_push_message(
        self, companion_hash: str, msg: Dict, max_messages: Optional[int] = None
    ) -> bool:
        """Append a message to the companion's queue.

        Deduplicates by (companion_hash, packet_hash) using INSERT OR IGNORE
        backed by the UNIQUE index added in migration 8.  This replaces the
        previous SELECT + INSERT round-trip (two statements, two SD-card reads)
        with a single atomic statement.

        When ``max_messages`` is set, capacity follows MeshCore's offline queue
        policy: evict the oldest channel message first and never displace a
        retained direct message. The insert and any eviction share one
        transaction.

        Returns True if the message is retained, False if it is a duplicate or
        the protected queue cannot make room for it.
        """
        try:
            if max_messages is not None and max_messages <= 0:
                return False
            packet_hash = msg.get("packet_hash") or None
            if isinstance(packet_hash, bytes):
                packet_hash = packet_hash.decode("utf-8", errors="replace") if packet_hash else None
            sender_key = msg.get("sender_key", b"")
            sender_prefix = msg.get("sender_prefix", b"")
            if not isinstance(sender_prefix, str):
                sender_prefix = bytes(sender_prefix or b"").hex()
            with self._connect() as conn:
                conn.execute("SAVEPOINT companion_message_push")
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO companion_messages
                    (companion_hash, sender_key, txt_type, timestamp, text,
                     is_channel, channel_idx, path_len, sender_prefix, snr, rssi,
                     channel_data_type, channel_data_payload, packet_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        sender_prefix,
                        float(msg.get("snr") or 0.0),
                        int(msg.get("rssi") or 0),
                        int(msg.get("channel_data_type") or 0),
                        bytes(msg.get("channel_data_payload") or b""),
                        packet_hash,
                        time.time(),
                    ),
                )
                inserted = cursor.rowcount > 0
                if not inserted:
                    conn.execute("RELEASE SAVEPOINT companion_message_push")
                    conn.commit()
                    return False
                if max_messages is not None:
                    last_id = cursor.lastrowid
                    # Capacity accounting counts only unconsumed rows: consumed
                    # rows are retained history (companion_get_messages), not
                    # part of the live offline queue, and must not count
                    # against the MeshCore queue-depth limit.
                    count = conn.execute(
                        """
                        SELECT COUNT(*) FROM companion_messages
                        WHERE companion_hash = ? AND consumed_at IS NULL
                        """,
                        (companion_hash,),
                    ).fetchone()[0]
                    excess = count - max_messages
                    if excess > 0:
                        # Eviction is ordered by id (an AUTOINCREMENT rowid, so
                        # insertion order) rather than created_at, keeping the
                        # policy immune to backwards clock steps. The incoming
                        # row is excluded so it is never evicted to make room
                        # for itself.
                        evictable = conn.execute(
                            """
                            SELECT COUNT(*) FROM companion_messages
                            WHERE companion_hash = ? AND is_channel = 1
                              AND consumed_at IS NULL AND id != ?
                            """,
                            (companion_hash, last_id),
                        ).fetchone()[0]
                        if evictable < excess:
                            # Not enough channel rows to make room without
                            # displacing a retained direct message. Undo the
                            # insert and every would-be eviction as one unit,
                            # keeping every prior row intact.
                            conn.execute("ROLLBACK TO SAVEPOINT companion_message_push")
                            conn.execute("RELEASE SAVEPOINT companion_message_push")
                            conn.commit()
                            return False
                        # Eviction marks rows consumed instead of deleting them:
                        # queue semantics are preserved (the row no longer
                        # counts toward capacity or is delivered again) while
                        # the row survives as history, same as a normal pop.
                        columns = {
                            row[1] for row in conn.execute("PRAGMA table_info(companion_messages)")
                        }
                        if "pending_for_frame" in columns:
                            eviction_query = """
                            UPDATE companion_messages
                            SET consumed_at = ?, pending_for_frame = 0
                            WHERE id IN (
                                SELECT id FROM companion_messages
                                WHERE companion_hash = ? AND is_channel = 1
                                  AND consumed_at IS NULL AND id != ?
                                ORDER BY id ASC LIMIT ?
                            )
                            """
                        else:
                            eviction_query = """
                            UPDATE companion_messages
                            SET consumed_at = ?
                            WHERE id IN (
                                SELECT id FROM companion_messages
                                WHERE companion_hash = ? AND is_channel = 1
                                  AND consumed_at IS NULL AND id != ?
                                ORDER BY id ASC LIMIT ?
                            )
                            """
                        conn.execute(
                            eviction_query,
                            (time.time(), companion_hash, last_id, excess),
                        )
                conn.execute("RELEASE SAVEPOINT companion_message_push")
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to push companion message: {e}")
            return False

    def companion_get_message_id(self, companion_hash: str, packet_hash) -> Optional[int]:
        """Return an inbound row id by ``packet_hash``, or ``None``.

        Used to register a freshly persisted inbound message with the RF
        correlation tracker (design doc §10.4). ``companion_push_message``
        intentionally keeps its plain-bool return contract (existing tests
        assert on it directly), so the id is fetched separately via the same
        inbound-only unique index used by the insert.
        """
        if isinstance(packet_hash, bytes):
            packet_hash = packet_hash.decode("utf-8", errors="replace") if packet_hash else None
        if not packet_hash:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT id FROM companion_messages
                    WHERE companion_hash = ? AND packet_hash = ?
                      AND direction = 'in'
                    """,
                    (companion_hash, packet_hash),
                ).fetchone()
                return int(row[0]) if row else None
        except Exception as e:
            logger.error(f"Failed to get companion message id for {companion_hash}: {e}")
            return None

    def companion_update_message_observations(
        self, message_id: int, observation_count: int, unique_path_count: int
    ) -> bool:
        """Write-through the running reception counters onto a message row
        (design doc §10.6): keeps headline counts alive in ``companion_messages``
        after the raw ``packets`` rows they were derived from age out of
        retention. Bounded ``UPDATE`` by primary key; volume is bounded by the
        companion's own message rate, not the mesh's packet rate.
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE companion_messages
                    SET observation_count = ?, unique_path_count = ?
                    WHERE id = ?
                    """,
                    (int(observation_count), int(unique_path_count), int(message_id)),
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to update companion message observations for {message_id}: {e}")
            return False

    def companion_record_inbound_reception(
        self,
        companion_hash: str,
        message_id: int,
        correlation: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Atomically advance inbound counters and append their RF event."""
        try:
            now = time.time()
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    """
                    SELECT observation_count, unique_path_count, packet_hash
                    FROM companion_messages
                    WHERE id = ? AND companion_hash = ? AND direction = 'in'
                    """,
                    (int(message_id), companion_hash),
                ).fetchone()
                if existing is None:
                    raise CompanionStorageError(f"Inbound message {message_id} does not exist")

                observation_count = max(
                    int(existing["observation_count"]),
                    int(correlation["observation_count"]),
                )
                unique_path_count = max(
                    int(existing["unique_path_count"]),
                    int(correlation["unique_path_count"]),
                )
                conn.execute(
                    """
                    UPDATE companion_messages
                    SET observation_count = ?, unique_path_count = ?
                    WHERE id = ? AND companion_hash = ? AND direction = 'in'
                    """,
                    (
                        observation_count,
                        unique_path_count,
                        int(message_id),
                        companion_hash,
                    ),
                )
                observed_hash = (
                    self._companion_packet_hash(correlation.get("packet_hash"))
                    or existing["packet_hash"]
                )
                payload = {
                    "message_id": int(message_id),
                    "packet_hash": observed_hash,
                    "path": correlation.get("path") or [],
                    "rssi": correlation.get("rssi"),
                    "snr": correlation.get("snr"),
                    "observed_at": correlation.get("observed_at"),
                    "observation_count": observation_count,
                    "unique_path_count": unique_path_count,
                }
                event = self._companion_append_event_row(
                    conn,
                    companion_hash,
                    "message_reception",
                    payload,
                    ref_table="companion_messages",
                    ref_id=int(message_id),
                    packet_hash=observed_hash,
                    created_at=now,
                )
                conn.commit()
                return {
                    "message_id": int(message_id),
                    "observation_count": observation_count,
                    "unique_path_count": unique_path_count,
                    "event_seq": event["seq"],
                    "event": event,
                }
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to record reception for inbound message {message_id}"
            ) from e

    def companion_pop_message(self, companion_hash: str) -> Optional[Dict]:
        """Soft-consume and return the oldest unconsumed message in the queue.

        companion_messages is durable history (Mobile Companion API journal
        phase 1): popping no longer deletes the row, it sets ``consumed_at``
        so the frame protocol still sees a draining queue (subsequent pops
        return the next unconsumed row) while the row itself — and its
        history for companion_get_messages / future sync — survives until
        retention pruning (companion_prune_consumed_messages) removes it.
        """
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                # A frame server may have more than one connected session.
                # Serialize claim + consume so two callers cannot both return
                # the same oldest row from separate thread-local connections.
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """
                    SELECT id, sender_key, txt_type, timestamp, text, is_channel, channel_idx,
                           path_len, sender_prefix, snr, rssi, channel_data_type,
                           channel_data_payload
                    FROM companion_messages WHERE companion_hash = ? AND consumed_at IS NULL
                    ORDER BY id ASC LIMIT 1
                """,
                    (companion_hash,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                msg = dict(row)
                msg["sender_prefix"] = bytes.fromhex(msg.get("sender_prefix") or "")
                msg["snr"] = float(msg.get("snr") or 0.0)
                msg["rssi"] = int(msg.get("rssi") or 0)
                msg["channel_data_type"] = int(msg.get("channel_data_type") or 0)
                msg["channel_data_payload"] = bytes(msg.get("channel_data_payload") or b"")
                columns = {
                    item[1] for item in conn.execute("PRAGMA table_info(companion_messages)")
                }
                if "pending_for_frame" in columns:
                    consume_query = """
                    UPDATE companion_messages
                    SET consumed_at = ?, pending_for_frame = 0
                    WHERE id = ? AND consumed_at IS NULL
                    """
                else:
                    consume_query = """
                    UPDATE companion_messages
                    SET consumed_at = ?
                    WHERE id = ? AND consumed_at IS NULL
                    """
                conn.execute(consume_query, (time.time(), msg["id"]))
                conn.commit()
                return {k: v for k, v in msg.items() if k != "id"}
        except Exception as e:
            logger.error(f"Failed to pop companion message: {e}")
            return None

    def companion_get_messages(
        self, companion_hash: str, before_id: Optional[int] = None, limit: int = 100
    ) -> List[Dict]:
        """Return a newest-first page of a companion's message history.

        Unlike companion_load_messages (queue-order, for boot restore),
        this serves the Mobile Companion API's message-history endpoint:
        newest-first, optionally paged backward with ``before_id`` (an
        exclusive upper bound on the rowid), and includes ``id`` and
        ``consumed_at`` so callers can tell delivered/history apart. Bytes
        columns are hex-encoded for JSON transport, matching the ``.hex()``
        convention used elsewhere in this file (e.g. companion_push_message).
        """
        try:
            return self._companion_get_messages_page(
                companion_hash,
                before_id=before_id,
                limit=limit,
                strict=False,
            )
        except Exception as e:
            logger.error(f"Failed to get companion messages for {companion_hash}: {e}")
            return []

    def companion_get_messages_strict(
        self, companion_hash: str, before_id: Optional[int] = None, limit: int = 100
    ) -> List[Dict]:
        """Return message history, raising when storage cannot prove the result.

        Mobile v1 uses this fail-closed variant so a database outage is a 503,
        not an authoritative-looking empty conversation.  The legacy helper
        above retains its historical ``[]`` error contract.
        """

        try:
            return self._companion_get_messages_page(
                companion_hash,
                before_id=before_id,
                limit=limit,
                strict=True,
            )
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to get companion messages for {companion_hash}"
            ) from e

    def _companion_get_messages_page(
        self,
        companion_hash: str,
        *,
        before_id: Optional[int],
        limit: int,
        strict: bool,
    ) -> List[Dict]:
        """Read one history page for either the legacy or fail-closed surface."""

        limit = max(1, min(int(limit), 200))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            query = self._companion_message_select() + " WHERE companion_hash = ?"
            params: List[Any] = [companion_hash]
            if before_id is not None:
                query += " AND id < ?"
                params.append(before_id)
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [self._companion_message_row_to_dict(row, strict=strict) for row in rows]

    # --- Mobile Companion API RF observation surface (design doc §10) ---
    #
    # Read-only queries over the existing packets/companion_messages tables;
    # no new write path. Every query here resolves through idx_packets_hash
    # (an equality lookup on packet_hash) or the companion_messages rowid,
    # with the caller-supplied time window applied as an additional SQL
    # predicate -- never a scan of packets filtered by a non-indexed column
    # alone (design doc §13).

    @staticmethod
    def _parse_json_path(raw: Optional[str]) -> List[str]:
        """Parse a packets.original_path/forwarded_path JSON array column.

        Returns [] for NULL/empty/malformed input rather than raising --
        callers treat "no path recorded" the same as "empty path".
        """
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []

    def companion_message_get_by_id(self, companion_hash: str, message_id: int) -> Optional[Dict]:
        """Return one companion message row by id, scoped to companion_hash.

        Scoping the WHERE clause to companion_hash (not just id) means a
        device token paired to one companion can't probe another
        companion's message ids and learn whether they exist (mirrors the
        404-folding choke point mobile_endpoints._resolve already applies
        to companion names).
        """
        try:
            return self._companion_message_get_by_id(
                companion_hash,
                message_id,
                strict=False,
            )
        except Exception as e:
            logger.error(f"Failed to get companion message {message_id} for {companion_hash}: {e}")
            return None

    def companion_message_get_by_id_strict(
        self, companion_hash: str, message_id: int
    ) -> Optional[Dict]:
        """Return one scoped message, raising on an uncertain database read."""

        try:
            return self._companion_message_get_by_id(
                companion_hash,
                message_id,
                strict=True,
            )
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to get companion message {message_id} for {companion_hash}"
            ) from e

    def _companion_message_get_by_id(
        self,
        companion_hash: str,
        message_id: int,
        *,
        strict: bool,
    ) -> Optional[Dict]:
        """Read one complete scoped row for a legacy or fail-closed caller."""

        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                self._companion_message_select()
                + """
                WHERE id = ? AND companion_hash = ?
                """,
                (message_id, companion_hash),
            ).fetchone()
            if row is None:
                return None
            return self._companion_message_row_to_dict(row, strict=strict)

    def companion_outbound_message_get_by_hash(
        self,
        companion_hash: str,
        packet_hash: str,
    ) -> Optional[Dict]:
        """Return one outbound message owned by a companion and packet hash.

        The global ``packets`` table is shared by every local identity.  Radio
        observation endpoints must therefore establish companion ownership in
        ``companion_messages`` before exposing rows from that global table.
        Storage failures raise instead of looking like a harmless 404.
        """

        normalized_hash = self._companion_packet_hash(packet_hash)
        if not normalized_hash:
            return None
        lookup_hash = normalized_hash
        if lookup_hash.lower().startswith("0x"):
            lookup_hash = lookup_hash[2:]
        lookup_hash = lookup_hash.upper()[:16]
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    self._companion_message_select()
                    + """
                    WHERE companion_hash = ?
                      AND UPPER(
                          SUBSTR(
                              CASE
                                  WHEN LOWER(packet_hash) LIKE '0x%'
                                  THEN SUBSTR(packet_hash, 3)
                                  ELSE packet_hash
                              END,
                              1,
                              16
                          )
                      ) = ?
                      AND direction = 'out'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (companion_hash, lookup_hash),
                ).fetchone()
                if row is None:
                    return None
                return self._companion_message_row_to_dict(row, strict=True)
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to resolve outbound packet {normalized_hash} for {companion_hash}"
            ) from e

    def companion_messages_by_sender(
        self,
        companion_hash: str,
        sender_key: bytes,
        since_ts: float,
        until_ts: float,
        limit: int = 200,
    ) -> List[Dict]:
        """Recent message rows from one sender pubkey within a time window.

        Backs the contact-paths endpoint (design doc §10, contact pubkey
        resolution): bounded LIMIT (newest first) rather than an unbounded
        scan, since a single busy contact could otherwise dominate the
        query.
        """
        try:
            rows, _truncated = self.companion_messages_by_sender_strict(
                companion_hash,
                sender_key,
                since_ts,
                until_ts,
                limit=limit,
            )
            return rows
        except CompanionStorageError as e:
            logger.error(f"Failed to get companion messages by sender for {companion_hash}: {e}")
            return []

    def companion_messages_by_sender_strict(
        self,
        companion_hash: str,
        sender_key: bytes,
        since_ts: float,
        until_ts: float,
        limit: int = 200,
    ) -> Tuple[List[Dict], bool]:
        """Return a bounded sender page and whether additional rows exist."""

        try:
            limit = max(1, min(int(limit), 200))
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT id, packet_hash, timestamp
                    FROM companion_messages
                    WHERE companion_hash = ? AND sender_key = ?
                      AND timestamp >= ? AND timestamp <= ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (companion_hash, sender_key, since_ts, until_ts, limit + 1),
                ).fetchall()
                truncated = len(rows) > limit
                messages = []
                for row in rows[:limit]:
                    message = dict(row)
                    _strict_storage_integer(
                        message["id"],
                        "companion message id",
                        minimum=1,
                        maximum=_SQLITE_MAX_ROW_ID,
                    )
                    _strict_companion_packet_hash(message["packet_hash"])
                    _strict_storage_integer(
                        message["timestamp"],
                        "companion message timestamp",
                    )
                    messages.append(message)
                return messages, truncated
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to get companion messages by sender for {companion_hash}"
            ) from e

    def packets_receptions(
        self, packet_hash_16: str, since_ts: float, until_ts: float, limit: int = 500
    ) -> List[Dict]:
        """Every OTA copy of ``packet_hash_16`` within [since_ts, until_ts].

        Ordered oldest-first. ``idx_packets_hash`` narrows to this one
        packet's copies first (small fanout even with no composite index --
        see design doc §10.1/§13); the time bound is then applied as a
        second SQL predicate rather than a separate scan.
        """
        try:
            rows, _truncated = self.packets_receptions_strict(
                packet_hash_16,
                since_ts,
                until_ts,
                limit=limit,
            )
            return rows
        except CompanionStorageError as e:
            logger.error(f"Failed to get packet receptions for {packet_hash_16}: {e}")
            return []

    def packets_receptions_strict(
        self,
        packet_hash_16: str,
        since_ts: float,
        until_ts: float,
        limit: int = 500,
    ) -> Tuple[List[Dict], bool]:
        """Return a bounded reception page and whether additional rows exist."""

        try:
            limit = max(1, min(int(limit), 500))
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT timestamp, rssi, snr, original_path, is_duplicate, transmitted
                    FROM packets
                    WHERE packet_hash = ? AND timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp ASC LIMIT ?
                    """,
                    (packet_hash_16, since_ts, until_ts, limit + 1),
                ).fetchall()
                results = []
                for row in rows[:limit]:
                    d = dict(row)
                    d["original_path"] = self._parse_json_path(d.get("original_path"))
                    d["is_duplicate"] = bool(d.get("is_duplicate"))
                    d["transmitted"] = bool(d.get("transmitted"))
                    results.append(d)
                return results, len(rows) > limit
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to get packet receptions for {packet_hash_16}"
            ) from e

    def packets_transmissions(
        self, packet_hash_16: str, since_ts: float, until_ts: float
    ) -> List[Dict]:
        """This repeater's own transmitted rows for ``packet_hash_16`` within
        the window (design doc §10.3's anchor for heard-repeat correlation).
        """
        try:
            rows, _truncated = self.packets_transmissions_strict(
                packet_hash_16,
                since_ts,
                until_ts,
                limit=None,
            )
            return rows
        except CompanionStorageError as e:
            logger.error(f"Failed to get packet transmissions for {packet_hash_16}: {e}")
            return []

    def packets_transmissions_strict(
        self,
        packet_hash_16: str,
        since_ts: float,
        until_ts: float,
        limit: Optional[int] = 500,
    ) -> Tuple[List[Dict], bool]:
        """Return transmissions and whether a requested bound was exceeded.

        ``limit=None`` preserves the legacy unbounded helper's behavior.
        Mobile v1 always supplies a small bound.
        """

        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                query = """
                    SELECT timestamp FROM packets
                    WHERE packet_hash = ? AND transmitted = 1
                      AND timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp ASC
                """
                params: List[Any] = [packet_hash_16, since_ts, until_ts]
                normalized_limit = None
                if limit is not None:
                    normalized_limit = max(1, min(int(limit), 500))
                    query += " LIMIT ?"
                    params.append(normalized_limit + 1)
                rows = conn.execute(query, params).fetchall()
                truncated = normalized_limit is not None and len(rows) > normalized_limit
                if normalized_limit is not None:
                    rows = rows[:normalized_limit]
                return [dict(row) for row in rows], truncated
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to get packet transmissions for {packet_hash_16}"
            ) from e

    def packets_heard_repeats(
        self, packet_hash_16: str, after_ts: float, until_ts: float, limit: int = 500
    ) -> List[Dict]:
        """Heard repeats of our own transmission of ``packet_hash_16``.

        Applies the design doc §10.3 local-echo-exclusion predicate exactly:
        ``is_duplicate=1 AND transmitted=0 AND timestamp > after_ts`` (the
        earliest matching transmit row's timestamp) -- a locally injected
        outbound frame or the transmission row itself never qualifies, only
        a genuine OTA reception heard strictly afterward.
        """
        try:
            rows, _truncated = self.packets_heard_repeats_strict(
                packet_hash_16,
                after_ts,
                until_ts,
                limit=limit,
            )
            return rows
        except CompanionStorageError as e:
            logger.error(f"Failed to get heard repeats for {packet_hash_16}: {e}")
            return []

    def packets_heard_repeats_strict(
        self,
        packet_hash_16: str,
        after_ts: float,
        until_ts: float,
        limit: int = 500,
    ) -> Tuple[List[Dict], bool]:
        """Return a bounded heard-repeat page and whether more rows exist."""

        try:
            limit = max(1, min(int(limit), 500))
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT timestamp, rssi, snr, original_path FROM packets
                    WHERE packet_hash = ? AND is_duplicate = 1 AND transmitted = 0
                      AND timestamp > ? AND timestamp <= ?
                    ORDER BY timestamp ASC LIMIT ?
                    """,
                    (packet_hash_16, after_ts, until_ts, limit + 1),
                ).fetchall()
                results = []
                for row in rows[:limit]:
                    d = dict(row)
                    d["original_path"] = self._parse_json_path(d.get("original_path"))
                    results.append(d)
                return results, len(rows) > limit
        except Exception as e:
            raise CompanionStorageError(f"Failed to get heard repeats for {packet_hash_16}") from e

    # --- Mobile Companion API event journal (design doc §5) ---
    #
    # companion_events is the canonical sync mechanism: every companion-scoped
    # state change appends one row, and a client's sync state collapses to a
    # single seq integer. companion_journal_meta carries small journal-wide
    # facts (the epoch, the prune floor) that aren't per-event.

    @staticmethod
    def _companion_json_safe(value: Any) -> Any:
        """Return a small JSON-safe copy of companion state.

        Companion protocol dictionaries contain byte strings.  Hex is the
        existing wire/storage convention, and keeping the conversion here
        avoids importing the bridge into the storage layer.
        """
        if isinstance(value, bytes):
            return value.hex()
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("non-finite floats are not valid companion JSON")
            return value
        if isinstance(value, dict):
            return {
                str(key): SQLiteHandler._companion_json_safe(item) for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [SQLiteHandler._companion_json_safe(item) for item in value]
        return value

    @staticmethod
    def _companion_append_event_row(
        conn: sqlite3.Connection,
        companion_hash: str,
        event_type: str,
        payload: dict,
        ref_table: Optional[str] = None,
        ref_id: Optional[int] = None,
        packet_hash: Optional[str] = None,
        created_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Insert an event using the caller's transaction and return its wire row."""
        timestamp = _finite_storage_float(
            time.time() if created_at is None else created_at,
            "companion event created_at",
        )
        safe_payload = SQLiteHandler._companion_json_safe(payload)
        cursor = conn.execute(
            """
            INSERT INTO companion_events
            (companion_hash, event_type, created_at, ref_table, ref_id, packet_hash, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                companion_hash,
                event_type,
                timestamp,
                ref_table,
                ref_id,
                packet_hash,
                json.dumps(safe_payload, separators=(",", ":"), allow_nan=False),
            ),
        )
        return {
            "seq": int(cursor.lastrowid),
            "event_type": event_type,
            "created_at": timestamp,
            "packet_hash": packet_hash,
            "payload": safe_payload,
        }

    def companion_append_event(
        self,
        companion_hash: str,
        event_type: str,
        payload: dict,
        ref_table: Optional[str] = None,
        ref_id: Optional[int] = None,
        packet_hash: Optional[str] = None,
        created_at: Optional[float] = None,
    ) -> Optional[int]:
        """Append one row to the companion event journal.

        Returns the new row's ``seq`` (the AUTOINCREMENT rowid), or None on
        failure. Callers should append in the same transaction scope as the
        state write the event describes where possible (design doc §5.4).

        ``created_at`` defaults to ``time.time()`` when omitted. Callers that
        also notify in-process listeners (``CompanionEventJournal._append``,
        for the SSE phase) pass an explicit value so the timestamp on the
        live-pushed event matches the one persisted here exactly.
        """
        try:
            with self._connect() as conn:
                event = self._companion_append_event_row(
                    conn,
                    companion_hash,
                    event_type,
                    payload,
                    ref_table=ref_table,
                    ref_id=ref_id,
                    packet_hash=packet_hash,
                    created_at=created_at,
                )
                conn.commit()
                return event["seq"]
        except Exception as e:
            logger.error(f"Failed to append companion event for {companion_hash}: {e}")
            return None

    def companion_get_event(self, companion_hash: str, seq: int) -> Optional[Dict[str, Any]]:
        """Return one committed event, scoped to its companion."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT seq, event_type, created_at, packet_hash, payload
                    FROM companion_events
                    WHERE companion_hash = ? AND seq = ?
                    """,
                    (companion_hash, int(seq)),
                ).fetchone()
                if row is None:
                    return None
                return self._companion_event_row_to_dict(row)
        except Exception as e:
            logger.error(f"Failed to get companion event seq={seq} for {companion_hash}: {e}")
            return None

    @staticmethod
    def _companion_event_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        created_at = float(row["created_at"])
        if not math.isfinite(created_at):
            raise CompanionStorageError(f"Companion event {row['seq']} has a non-finite timestamp")
        event = {
            "seq": int(row["seq"]),
            "event_type": row["event_type"],
            "created_at": created_at,
            "packet_hash": row["packet_hash"],
        }
        try:
            payload = strict_json_loads(row["payload"])
        except (TypeError, ValueError) as exc:
            raise CompanionStorageError(
                f"Companion event {row['seq']} has invalid JSON payload"
            ) from exc
        if not isinstance(payload, dict):
            raise CompanionStorageError(f"Companion event {row['seq']} payload is not an object")
        event["payload"] = payload
        return event

    def companion_get_events(
        self, companion_hash: str, after_seq: int, limit: int = 100
    ) -> List[Dict]:
        """Return journal rows for ``companion_hash`` with seq > after_seq.

        Ordered oldest-first, served entirely by idx_companion_events_sync
        (companion_hash, seq) — no other predicate is added, keeping this an
        index range scan per the performance rules (design doc §13).
        """
        try:
            limit = max(1, min(int(limit), 500))
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT seq, event_type, created_at, packet_hash, payload
                    FROM companion_events
                    WHERE companion_hash = ? AND seq > ?
                    ORDER BY seq ASC LIMIT ?
                    """,
                    (companion_hash, int(after_seq), limit),
                ).fetchall()

                return [self._companion_event_row_to_dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get companion events for {companion_hash}: {e}")
            return []

    def companion_journal_head(self, companion_hash: str) -> int:
        """Return the highest journaled seq for this companion, 0 if none."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT MAX(seq) FROM companion_events WHERE companion_hash = ?",
                    (companion_hash,),
                ).fetchone()
                return int(row[0]) if row and row[0] is not None else 0
        except Exception as e:
            logger.error(f"Failed to get companion journal head for {companion_hash}: {e}")
            return 0

    def companion_journal_floor(self, companion_hash: str) -> int:
        """Return the oldest valid cursor for one companion.

        This method raises on storage failure.  Sync callers must fail closed;
        returning zero would misdescribe a pruned journal as complete.
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT prune_floor FROM companion_journal_floors
                    WHERE companion_hash = ?
                    """,
                    (companion_hash,),
                ).fetchone()
                return int(row[0]) if row and row[0] is not None else 0
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to get journal floor for companion {companion_hash}"
            ) from e

    def companion_journal_meta_get(self, key: str) -> Optional[str]:
        """Return a companion_journal_meta value, or None if unset/on failure."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT value FROM companion_journal_meta WHERE key = ?", (key,)
                ).fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error(f"Failed to get companion journal meta '{key}': {e}")
            return None

    def companion_journal_meta_set(self, key: str, value: str) -> bool:
        """Upsert a companion_journal_meta key/value pair."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO companion_journal_meta (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to set companion journal meta '{key}': {e}")
            return False

    def companion_journal_epoch(self) -> str:
        """Return the journal epoch, generating and persisting one if unset.

        The epoch is a random ID a client compares against its stored value
        (design doc §5.3): a mismatch means the database was wiped or restored
        from backup, and the client must discard its cursor and re-snapshot
        rather than risk a smaller-but-valid-looking seq silently replaying or
        skipping history. Stable across calls and across handler instances
        (persisted in companion_journal_meta, not process state).
        """
        try:
            return self.companion_journal_epoch_strict()
        except CompanionStorageError as e:
            logger.error(f"Failed to get/generate companion journal epoch: {e}")
            return secrets.token_hex(8)

    def companion_journal_epoch_strict(self) -> str:
        """Return the durable journal epoch, raising if it cannot be verified."""

        try:
            with self._connect() as conn:
                epoch = self._companion_journal_epoch_in_transaction(conn)
                conn.commit()
                return epoch
        except Exception as e:
            raise CompanionStorageError("Failed to get/generate companion journal epoch") from e

    @staticmethod
    def _companion_journal_epoch_in_transaction(conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT value FROM companion_journal_meta WHERE key = 'journal_epoch'"
        ).fetchone()
        if row and row[0]:
            return str(row[0])
        epoch = secrets.token_hex(8)
        conn.execute(
            "INSERT OR IGNORE INTO companion_journal_meta (key, value) VALUES ('journal_epoch', ?)",
            (epoch,),
        )
        row = conn.execute(
            "SELECT value FROM companion_journal_meta WHERE key = 'journal_epoch'"
        ).fetchone()
        if not row or not row[0]:
            raise CompanionStorageError("Journal epoch could not be persisted")
        return str(row[0])

    def companion_sync_state(self, companion_hash: str) -> Dict[str, Any]:
        """Read one coherent epoch/head/floor tuple for snapshot and sync."""
        try:
            with self._connect() as conn:
                conn.execute("BEGIN")
                epoch = self._companion_journal_epoch_in_transaction(conn)
                head_row = conn.execute(
                    "SELECT MAX(seq) FROM companion_events WHERE companion_hash = ?",
                    (companion_hash,),
                ).fetchone()
                floor_row = conn.execute(
                    """
                    SELECT prune_floor FROM companion_journal_floors
                    WHERE companion_hash = ?
                    """,
                    (companion_hash,),
                ).fetchone()
                stored_head = (
                    int(head_row[0]) if head_row is not None and head_row[0] is not None else 0
                )
                floor = (
                    int(floor_row[0]) if floor_row is not None and floor_row[0] is not None else 0
                )
                # A fully pruned, quiet companion has no physical MAX(seq),
                # but its logical head remains the prune floor. Returning 0
                # would make the snapshot cursor immediately invalid.
                head = max(stored_head, floor)
                conn.commit()
                return {
                    "epoch": epoch,
                    "head": head,
                    "floor": floor,
                    "cursor": self.companion_cursor_encode(epoch, head),
                }
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to read sync state for companion {companion_hash}"
            ) from e

    def companion_cursor_status(
        self, companion_hash: str, epoch: Optional[str], seq: Any
    ) -> Dict[str, Any]:
        """Validate a cursor against one coherent journal state.

        ``reason`` is intentionally a small, stable vocabulary suitable for
        logs, HTTP control responses, and agent-authored clients.
        """
        state = self.companion_sync_state(companion_hash)
        try:
            cursor_seq = int(seq)
        except (TypeError, ValueError):
            return {**state, "valid": False, "reason": "invalid_cursor"}
        if cursor_seq < 0:
            return {**state, "valid": False, "reason": "invalid_cursor"}
        if not epoch:
            return {**state, "valid": False, "reason": "missing_epoch"}
        if not secrets.compare_digest(str(epoch), state["epoch"]):
            return {**state, "valid": False, "reason": "epoch_mismatch"}
        if cursor_seq > state["head"]:
            return {**state, "valid": False, "reason": "future_cursor"}
        if cursor_seq < state["floor"]:
            return {**state, "valid": False, "reason": "pruned_cursor"}
        return {**state, "valid": True, "reason": None, "seq": cursor_seq}

    @staticmethod
    def companion_cursor_encode(epoch: str, seq: int) -> str:
        """Encode the human-readable opaque cursor used by HTTP and SSE."""
        return f"{epoch}:{int(seq)}"

    @staticmethod
    def companion_cursor_decode(cursor: str) -> tuple[str, int]:
        """Decode ``epoch:seq`` or raise ``ValueError``."""
        try:
            epoch, raw_seq = str(cursor).rsplit(":", 1)
            seq = int(raw_seq)
        except (TypeError, ValueError) as e:
            raise ValueError("cursor must be 'epoch:seq'") from e
        if not epoch or seq < 0:
            raise ValueError("cursor must be 'epoch:seq'")
        return epoch, seq

    def companion_sync_page(
        self,
        companion_hash: str,
        epoch: Optional[str],
        after_seq: Any,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Validate a cursor and read one ordered page from the same snapshot."""
        try:
            page_size = max(1, min(int(limit), 500))
            cursor_seq = int(after_seq)
        except (TypeError, ValueError):
            return {
                **self.companion_sync_state(companion_hash),
                "valid": False,
                "reason": "invalid_cursor",
                "events": [],
            }
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN")
                current_epoch = self._companion_journal_epoch_in_transaction(conn)
                head_row = conn.execute(
                    "SELECT MAX(seq) FROM companion_events WHERE companion_hash = ?",
                    (companion_hash,),
                ).fetchone()
                floor_row = conn.execute(
                    """
                    SELECT prune_floor FROM companion_journal_floors
                    WHERE companion_hash = ?
                    """,
                    (companion_hash,),
                ).fetchone()
                stored_head = int(head_row[0]) if head_row and head_row[0] is not None else 0
                floor = int(floor_row[0]) if floor_row and floor_row[0] is not None else 0
                head = max(stored_head, floor)
                state = {
                    "epoch": current_epoch,
                    "head": head,
                    "floor": floor,
                    "cursor": self.companion_cursor_encode(current_epoch, head),
                }
                if cursor_seq < 0:
                    conn.commit()
                    return {
                        **state,
                        "valid": False,
                        "reason": "invalid_cursor",
                        "events": [],
                    }
                if not epoch:
                    conn.commit()
                    return {
                        **state,
                        "valid": False,
                        "reason": "missing_epoch",
                        "events": [],
                    }
                if not secrets.compare_digest(str(epoch), current_epoch):
                    conn.commit()
                    return {
                        **state,
                        "valid": False,
                        "reason": "epoch_mismatch",
                        "events": [],
                    }
                if cursor_seq > head:
                    conn.commit()
                    return {
                        **state,
                        "valid": False,
                        "reason": "future_cursor",
                        "events": [],
                    }
                if cursor_seq < floor:
                    conn.commit()
                    return {
                        **state,
                        "valid": False,
                        "reason": "pruned_cursor",
                        "events": [],
                    }

                rows = conn.execute(
                    """
                    SELECT seq, event_type, created_at, packet_hash, payload
                    FROM companion_events
                    WHERE companion_hash = ? AND seq > ?
                    ORDER BY seq ASC LIMIT ?
                    """,
                    (companion_hash, cursor_seq, page_size + 1),
                ).fetchall()
                conn.commit()
                has_more = len(rows) > page_size
                events = [self._companion_event_row_to_dict(row) for row in rows[:page_size]]
                next_seq = events[-1]["seq"] if events else cursor_seq
                return {
                    **state,
                    "valid": True,
                    "reason": None,
                    "events": events,
                    "next_seq": next_seq,
                    "next_cursor": self.companion_cursor_encode(current_epoch, next_seq),
                    "has_more": has_more,
                }
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to read sync page for companion {companion_hash}"
            ) from e

    def companion_journal_rotate_epoch(self) -> str:
        """Replace the journal epoch after a database restore/reset."""
        try:
            with self._connect() as conn:
                epoch = self._companion_rotate_epoch_in_transaction(conn)
                conn.commit()
            return epoch
        except Exception as e:
            raise CompanionStorageError("Failed to rotate companion journal epoch") from e

    @staticmethod
    def _companion_rotate_epoch_in_transaction(conn: sqlite3.Connection) -> str:
        """Rotate the cursor epoch inside the caller's transaction."""

        epoch = secrets.token_hex(8)
        conn.execute(
            """
            INSERT INTO companion_journal_meta (key, value)
            VALUES ('journal_epoch', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (epoch,),
        )
        return epoch

    def companion_prune_events(self, max_age_days: int) -> int:
        """Delete companion_events rows older than max_age_days.

        Before deleting, records the highest seq about to be removed and, if
        it exceeds the current 'prune_floor' meta value, advances the floor.
        prune_floor semantics: a client cursor ``c`` is only valid if
        ``c >= prune_floor``; sync requests below the floor must be answered
        with snapshot_required rather than a silently incomplete delta.
        Returns the number of rows deleted.
        """
        retention_days = validate_retention_days(
            max_age_days,
            "storage.retention.companion_events_days",
        )
        try:
            cutoff = time.time() - (retention_days * 86400)
            with self._connect() as conn:
                # Lock before identifying the rows so an old-dated event
                # cannot arrive between the floor calculation and deletion.
                conn.execute("BEGIN IMMEDIATE")
                floor_rows = conn.execute(
                    """
                    SELECT companion_hash, MAX(seq)
                    FROM companion_events
                    WHERE created_at < ?
                    GROUP BY companion_hash
                    """,
                    (cutoff,),
                ).fetchall()

                result = conn.execute(
                    "DELETE FROM companion_events WHERE created_at < ?", (cutoff,)
                )
                deleted = result.rowcount

                max_deleted_seq = 0
                for companion_hash, deleted_seq in floor_rows:
                    if deleted_seq is None:
                        continue
                    floor = int(deleted_seq)
                    max_deleted_seq = max(max_deleted_seq, floor)
                    conn.execute(
                        """
                        INSERT INTO companion_journal_floors
                            (companion_hash, prune_floor)
                        VALUES (?, ?)
                        ON CONFLICT(companion_hash) DO UPDATE SET
                            prune_floor = MAX(prune_floor, excluded.prune_floor)
                        """,
                        (companion_hash, floor),
                    )

                # Keep the old aggregate meta key current for backward
                # compatibility.  New sync code must use the scoped table.
                if max_deleted_seq:
                    floor_row = conn.execute(
                        "SELECT value FROM companion_journal_meta WHERE key = 'prune_floor'"
                    ).fetchone()
                    current_floor = (
                        int(floor_row[0]) if floor_row and floor_row[0] is not None else 0
                    )
                    if max_deleted_seq > current_floor:
                        conn.execute(
                            """
                            INSERT INTO companion_journal_meta (key, value) VALUES ('prune_floor', ?)
                            ON CONFLICT(key) DO UPDATE SET value = excluded.value
                            """,
                            (str(max_deleted_seq),),
                        )

                conn.commit()
                if deleted:
                    logger.info(
                        f"Pruned {deleted} companion journal event(s) older than {retention_days}d"
                    )
                return deleted
        except Exception as e:
            logger.error(f"Failed to prune companion events: {e}")
            raise

    def companion_prune_consumed_messages(self, max_age_days: int) -> int:
        """Delete soft-consumed companion_messages rows older than max_age_days.

        Only rows with consumed_at set are eligible — unconsumed rows are the
        live offline queue and must never be pruned by age alone. Returns the
        number of rows deleted.
        """
        retention_days = validate_retention_days(
            max_age_days,
            "storage.retention.companion_events_days",
        )
        try:
            cutoff = time.time() - (retention_days * 86400)
            with self._connect() as conn:
                result = conn.execute(
                    """
                    DELETE FROM companion_messages
                    WHERE consumed_at IS NOT NULL AND consumed_at < ?
                    """,
                    (cutoff,),
                )
                deleted = result.rowcount
                conn.commit()
                if deleted:
                    logger.info(
                        f"Pruned {deleted} consumed companion message(s) older than "
                        f"{retention_days}d"
                    )
                return deleted
        except Exception as e:
            logger.error(f"Failed to prune consumed companion messages: {e}")
            raise

    # --- Companion idempotency (design doc §5.4, §6) -----------------------
    #
    # POST …/messages reserves a typed principal/key before RF work begins.
    # Terminal responses are replayable.  Pending/transmitted rows become
    # indeterminate rather than disappearing, because deleting an ambiguous
    # outcome would make a later retry transmit again.

    @staticmethod
    def _companion_principal_key(principal_type: str, principal_id: str) -> str:
        kind = str(principal_type).strip().lower()
        identity = str(principal_id).strip()
        if kind not in {
            "device",
            "user",
            "admin",
            "frame",
            "token",
            "jwt",
            "legacy",
        }:
            raise ValueError("invalid idempotency principal type")
        if not identity:
            raise ValueError("idempotency principal id is required")
        return f"{kind}:{identity}"

    @staticmethod
    def _companion_idempotency_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        state = row["state"]
        response_json = row["response_json"]
        if state not in {
            "pending",
            "transmitted",
            "complete",
            "failed",
            "indeterminate",
        }:
            raise ValueError(f"invalid stored idempotency state: {state}")
        principal_type = row["principal_type"]
        principal_id = row["principal_id"]
        idempotency_key = row["idempotency_key"]
        request_hash = row["request_hash"]
        if not isinstance(principal_type, str) or not principal_type:
            raise ValueError("stored idempotency principal_type is invalid")
        if not isinstance(principal_id, str) or not principal_id:
            raise ValueError("stored idempotency principal_id is invalid")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("stored idempotency key is invalid")
        if not isinstance(request_hash, str) or not request_hash:
            raise ValueError("stored idempotency request_hash is invalid")
        if not isinstance(response_json, str):
            raise ValueError("stored idempotency response is not JSON text")
        if state in {"complete", "failed"}:
            if principal_type == "legacy":
                _validated_json_object(response_json)
            else:
                _validated_response_json(response_json)
        created_at = _finite_storage_float(
            row["created_at"],
            "stored idempotency created_at",
        )
        updated_at = _finite_storage_float(
            row["updated_at"],
            "stored idempotency updated_at",
        )
        return {
            "principal_type": principal_type,
            "principal_id": principal_id,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
            "state": state,
            "response_json": response_json,
            "message_id": row["message_id"],
            "packet_hash": row["packet_hash"],
            "expected_ack": row["expected_ack"],
            "created_at": created_at,
            "updated_at": updated_at,
        }

    @staticmethod
    def _companion_idempotency_select() -> str:
        return """
            SELECT principal_type, principal_id, idempotency_key, request_hash,
                   state, response_json, message_id, packet_hash, expected_ack,
                   created_at, updated_at
            FROM companion_idempotency
        """

    def _migrate_device_idempotency_principals(
        self,
        conn: sqlite3.Connection,
        *,
        token_id: Optional[int] = None,
    ) -> Dict[str, int]:
        """Rekey resolvable numeric device principals without losing collisions.

        Old releases used ``companion_devices.id`` as the principal. That row
        id changes after revoke/re-pair, so retries could bypass a prior RF
        reservation. Stable principals use the full companion identity (or
        legacy hash) plus the client-supplied stable device id.
        """
        previous_row_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            device_sql = """
                SELECT id, companion_identity, companion_hash, device_id
                FROM companion_devices
            """
            params: tuple = ()
            if token_id is not None:
                device_sql += " WHERE token_id = ?"
                params = (int(token_id),)
            devices = conn.execute(device_sql, params).fetchall()
            rekeyed = 0
            collisions = 0
            for device in devices:
                old_principal_id = str(device["id"])
                new_principal_id = companion_device_principal_id(
                    device["companion_identity"],
                    device["companion_hash"],
                    device["device_id"],
                )
                new_principal_key = self._companion_principal_key(
                    "device",
                    new_principal_id,
                )
                legacy_rows = conn.execute(
                    """
                    SELECT rowid AS storage_rowid, *
                    FROM companion_idempotency
                    WHERE principal_type = 'device' AND principal_id = ?
                    ORDER BY rowid
                    """,
                    (old_principal_id,),
                ).fetchall()
                for legacy in legacy_rows:
                    target = conn.execute(
                        """
                        SELECT rowid AS storage_rowid, *
                        FROM companion_idempotency
                        WHERE idempotency_key = ?
                          AND (
                              device_id = ?
                              OR (
                                  principal_type = 'device'
                                  AND principal_id = ?
                              )
                          )
                        ORDER BY CASE WHEN device_id = ? THEN 0 ELSE 1 END,
                                 rowid
                        LIMIT 1
                        """,
                        (
                            legacy["idempotency_key"],
                            new_principal_key,
                            new_principal_id,
                            new_principal_key,
                        ),
                    ).fetchone()
                    if target is None:
                        conn.execute(
                            """
                            UPDATE companion_idempotency
                            SET device_id = ?, principal_id = ?
                            WHERE rowid = ?
                            """,
                            (
                                new_principal_key,
                                new_principal_id,
                                legacy["storage_rowid"],
                            ),
                        )
                        rekeyed += 1
                        continue

                    # Keep both physical rows on collision. Only an exactly
                    # matching terminal record remains replayable; every other
                    # combination is conservatively indeterminate so neither
                    # request can transmit again.
                    terminal_states = {"complete", "failed"}
                    material_fields = (
                        "request_hash",
                        "state",
                        "response_json",
                        "message_id",
                        "packet_hash",
                        "expected_ack",
                    )
                    compatible = (
                        legacy["state"] in terminal_states
                        and target["state"] in terminal_states
                        and all(legacy[field] == target[field] for field in material_fields)
                    )
                    if not compatible:
                        now = time.time()
                        merged_message_id = (
                            target["message_id"]
                            if target["message_id"] is not None
                            else legacy["message_id"]
                        )
                        merged_packet_hash = (
                            target["packet_hash"]
                            if target["packet_hash"] is not None
                            else legacy["packet_hash"]
                        )
                        merged_expected_ack = (
                            target["expected_ack"]
                            if target["expected_ack"] is not None
                            else legacy["expected_ack"]
                        )
                        for row in (target, legacy):
                            conn.execute(
                                """
                                UPDATE companion_idempotency
                                SET state = 'indeterminate',
                                    response_json = '',
                                    message_id = ?,
                                    packet_hash = ?,
                                    expected_ack = ?,
                                    updated_at = ?
                                WHERE rowid = ?
                                """,
                                (
                                    merged_message_id,
                                    merged_packet_hash,
                                    merged_expected_ack,
                                    now,
                                    row["storage_rowid"],
                                ),
                            )
                    collisions += 1
            return {"rekeyed": rekeyed, "collisions": collisions}
        finally:
            conn.row_factory = previous_row_factory

    def companion_idempotency_reserve(
        self,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> Dict[str, Any]:
        """Atomically reserve a send key before touching the radio.

        Result values are ``reserved``, ``replay``, ``in_progress``,
        ``indeterminate``, or ``conflict``.  Storage errors raise
        :class:`CompanionStorageError` so the caller never transmits after an
        uncertain reservation.
        """
        principal_key = self._companion_principal_key(principal_type, principal_id)
        now = time.time()
        try:
            with self._companion_durable_transaction() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO companion_idempotency
                    (device_id, idempotency_key, request_hash, response_json,
                     created_at, principal_type, principal_id, state, updated_at)
                    VALUES (?, ?, ?, '', ?, ?, ?, 'pending', ?)
                    """,
                    (
                        principal_key,
                        idempotency_key,
                        request_hash,
                        now,
                        principal_type,
                        principal_id,
                        now,
                    ),
                )
                if cursor.rowcount > 0:
                    return {
                        "result": "reserved",
                        "principal_type": principal_type,
                        "principal_id": principal_id,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "state": "pending",
                        "response_json": "",
                        "message_id": None,
                        "packet_hash": None,
                        "expected_ack": None,
                        "created_at": now,
                        "updated_at": now,
                    }

                row = conn.execute(
                    self._companion_idempotency_select()
                    + " WHERE device_id = ? AND idempotency_key = ?",
                    (principal_key, idempotency_key),
                ).fetchone()
                if row is None:
                    raise CompanionStorageError(
                        "Idempotency reservation conflict row could not be read"
                    )
                record = self._companion_idempotency_row_to_dict(row)
                if record["request_hash"] != request_hash:
                    record["result"] = "conflict"
                elif record["state"] in {"complete", "failed"}:
                    record["result"] = "replay"
                elif record["state"] == "indeterminate":
                    record["result"] = "indeterminate"
                else:
                    record["result"] = "in_progress"
                return record
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to reserve idempotency key for {principal_type}:{principal_id}"
            ) from e

    def companion_reserve_outbound_send(
        self,
        companion_hash: str,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        outbound: Dict[str, Any],
        source: str = "rest",
    ) -> Dict[str, Any]:
        """Reserve a key, message row, and initial event in one transaction."""
        principal_key = self._companion_principal_key(principal_type, principal_id)
        now = time.time()
        try:
            with self._companion_durable_transaction() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO companion_idempotency
                    (device_id, idempotency_key, request_hash, response_json,
                     created_at, principal_type, principal_id, state, updated_at)
                    VALUES (?, ?, ?, '', ?, ?, ?, 'pending', ?)
                    """,
                    (
                        principal_key,
                        idempotency_key,
                        request_hash,
                        now,
                        principal_type,
                        principal_id,
                        now,
                    ),
                )
                if cursor.rowcount > 0:
                    stored = self._companion_insert_outbound_message_row(
                        conn,
                        companion_hash,
                        outbound,
                        source,
                        "pending",
                        now,
                    )
                    conn.execute(
                        """
                        UPDATE companion_idempotency
                        SET message_id = ?, updated_at = ?
                        WHERE device_id = ? AND idempotency_key = ?
                        """,
                        (
                            stored["message_id"],
                            now,
                            principal_key,
                            idempotency_key,
                        ),
                    )
                    return {
                        "result": "reserved",
                        "principal_type": principal_type,
                        "principal_id": principal_id,
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "state": "pending",
                        "response_json": "",
                        "message_id": stored["message_id"],
                        "packet_hash": None,
                        "expected_ack": None,
                        "created_at": now,
                        "updated_at": now,
                        "message": stored["message"],
                        "event": stored["event"],
                    }

                row = conn.execute(
                    self._companion_idempotency_select()
                    + " WHERE device_id = ? AND idempotency_key = ?",
                    (principal_key, idempotency_key),
                ).fetchone()
                if row is None:
                    raise CompanionStorageError(
                        "Idempotency reservation conflict row could not be read"
                    )
                record = self._companion_idempotency_row_to_dict(row)
                if record["request_hash"] != request_hash:
                    record["result"] = "conflict"
                elif record["state"] in {"complete", "failed"}:
                    record["result"] = "replay"
                elif record["state"] == "indeterminate":
                    record["result"] = "indeterminate"
                else:
                    record["result"] = "in_progress"
                record["event"] = None
                return record
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to reserve outbound send for {principal_type}:{principal_id}"
            ) from e

    def companion_complete_outbound_send(
        self,
        companion_hash: str,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        message_id: int,
        message_state: str,
        response_json: str,
        packet_hash: Optional[str] = None,
        expected_ack: Optional[int] = None,
        idempotency_state: str = "complete",
    ) -> Dict[str, Any]:
        """Commit final message state, event, and replay response together."""
        if idempotency_state not in {"complete", "failed"}:
            raise ValueError("idempotency_state must be complete or failed")
        response_json = _validated_response_json(response_json)
        principal_key = self._companion_principal_key(principal_type, principal_id)
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    self._companion_idempotency_select()
                    + " WHERE device_id = ? AND idempotency_key = ?",
                    (principal_key, idempotency_key),
                ).fetchone()
                if existing is None:
                    raise CompanionStorageError("Idempotency reservation does not exist")
                record = self._companion_idempotency_row_to_dict(existing)
                if record["request_hash"] != request_hash:
                    raise CompanionStorageError(
                        "Idempotency request hash does not match its reservation"
                    )
                if record["message_id"] != int(message_id):
                    raise CompanionStorageError(
                        "Idempotency reservation is linked to a different message"
                    )
                if record["state"] in {"complete", "failed"}:
                    record["result"] = "replay"
                    record["event"] = None
                    return record
                if record["state"] == "indeterminate":
                    record["result"] = "indeterminate"
                    record["event"] = None
                    return record

                now = time.time()
                message_result = self._companion_advance_outbound_state_row(
                    conn,
                    companion_hash,
                    message_id,
                    message_state,
                    packet_hash,
                    expected_ack,
                    now,
                )
                conn.execute(
                    """
                    UPDATE companion_idempotency
                    SET state = ?, response_json = ?, packet_hash = COALESCE(?, packet_hash),
                        expected_ack = COALESCE(?, expected_ack), updated_at = ?
                    WHERE device_id = ? AND idempotency_key = ?
                      AND state NOT IN ('complete', 'failed', 'indeterminate')
                    """,
                    (
                        idempotency_state,
                        response_json,
                        self._companion_packet_hash(packet_hash),
                        int(expected_ack) if expected_ack is not None else None,
                        now,
                        principal_key,
                        idempotency_key,
                    ),
                )
                row = conn.execute(
                    self._companion_idempotency_select()
                    + " WHERE device_id = ? AND idempotency_key = ?",
                    (principal_key, idempotency_key),
                ).fetchone()
                conn.commit()
                result = self._companion_idempotency_row_to_dict(row)
                result["result"] = "replay"
                result["message"] = message_result["message"]
                result["event"] = message_result.get("event")
                return result
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to complete outbound send for {principal_type}:{principal_id}"
            ) from e

    def companion_mark_outbound_send_indeterminate(
        self,
        companion_hash: str,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        message_id: int,
        packet_hash: Optional[str] = None,
        expected_ack: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Mark both linked records indeterminate in one transaction."""
        principal_key = self._companion_principal_key(principal_type, principal_id)
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    self._companion_idempotency_select()
                    + " WHERE device_id = ? AND idempotency_key = ?",
                    (principal_key, idempotency_key),
                ).fetchone()
                if existing is None:
                    raise CompanionStorageError("Idempotency reservation does not exist")
                record = self._companion_idempotency_row_to_dict(existing)
                if record["request_hash"] != request_hash:
                    raise CompanionStorageError(
                        "Idempotency request hash does not match its reservation"
                    )
                if record["message_id"] != int(message_id):
                    raise CompanionStorageError(
                        "Idempotency reservation is linked to a different message"
                    )
                if record["state"] in {"complete", "failed"}:
                    record["event"] = None
                    return record

                now = time.time()
                message_result = self._companion_advance_outbound_state_row(
                    conn,
                    companion_hash,
                    message_id,
                    "indeterminate",
                    packet_hash,
                    expected_ack,
                    now,
                )
                conn.execute(
                    """
                    UPDATE companion_idempotency
                    SET state = 'indeterminate',
                        packet_hash = COALESCE(?, packet_hash),
                        expected_ack = COALESCE(?, expected_ack),
                        updated_at = ?
                    WHERE device_id = ? AND idempotency_key = ?
                      AND state NOT IN ('complete', 'failed')
                    """,
                    (
                        self._companion_packet_hash(packet_hash),
                        int(expected_ack) if expected_ack is not None else None,
                        now,
                        principal_key,
                        idempotency_key,
                    ),
                )
                row = conn.execute(
                    self._companion_idempotency_select()
                    + " WHERE device_id = ? AND idempotency_key = ?",
                    (principal_key, idempotency_key),
                ).fetchone()
                conn.commit()
                result = self._companion_idempotency_row_to_dict(row)
                result["event"] = message_result.get("event")
                result["message"] = message_result["message"]
                return result
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to mark outbound send indeterminate for {principal_type}:{principal_id}"
            ) from e

    def companion_idempotency_lookup(
        self,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> Optional[Dict[str, Any]]:
        """Read an existing send key without reserving a new one.

        This lets harmless response replays bypass RF admission limits.  A
        subsequent atomic reservation still resolves the race between two
        first attempts.
        """

        principal_key = self._companion_principal_key(principal_type, principal_id)
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    self._companion_idempotency_select()
                    + " WHERE device_id = ? AND idempotency_key = ?",
                    (principal_key, idempotency_key),
                ).fetchone()
                if row is None:
                    return None
                record = self._companion_idempotency_row_to_dict(row)
                if record["request_hash"] != request_hash:
                    record["result"] = "conflict"
                elif record["state"] in {"complete", "failed"}:
                    record["result"] = "replay"
                elif record["state"] == "indeterminate":
                    record["result"] = "indeterminate"
                else:
                    record["result"] = "in_progress"
                return record
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to read idempotency key for {principal_type}:{principal_id}"
            ) from e

    def _companion_idempotency_transition(
        self,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        state: str,
        response_json: Optional[str] = None,
        message_id: Optional[int] = None,
        packet_hash: Optional[str] = None,
        expected_ack: Optional[int] = None,
    ) -> Dict[str, Any]:
        if state not in {"transmitted", "complete", "failed", "indeterminate"}:
            raise ValueError("invalid idempotency state transition")
        if response_json is not None:
            response_json = _validated_response_json(response_json)
        principal_key = self._companion_principal_key(principal_type, principal_id)
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    self._companion_idempotency_select()
                    + " WHERE device_id = ? AND idempotency_key = ?",
                    (principal_key, idempotency_key),
                ).fetchone()
                if existing is None:
                    raise CompanionStorageError("Idempotency reservation does not exist")
                record = self._companion_idempotency_row_to_dict(existing)
                if record["request_hash"] != request_hash:
                    raise CompanionStorageError(
                        "Idempotency request hash does not match its reservation"
                    )
                if record["state"] in {"complete", "failed"}:
                    record["result"] = "replay"
                    return record
                if record["state"] == "indeterminate":
                    record["result"] = "indeterminate"
                    return record

                now = time.time()
                conn.execute(
                    """
                    UPDATE companion_idempotency
                    SET state = ?,
                        response_json = COALESCE(?, response_json),
                        message_id = COALESCE(?, message_id),
                        packet_hash = COALESCE(?, packet_hash),
                        expected_ack = COALESCE(?, expected_ack),
                        updated_at = ?
                    WHERE device_id = ? AND idempotency_key = ?
                      AND state NOT IN ('complete', 'failed', 'indeterminate')
                    """,
                    (
                        state,
                        response_json,
                        int(message_id) if message_id is not None else None,
                        self._companion_packet_hash(packet_hash),
                        int(expected_ack) if expected_ack is not None else None,
                        now,
                        principal_key,
                        idempotency_key,
                    ),
                )
                row = conn.execute(
                    self._companion_idempotency_select()
                    + " WHERE device_id = ? AND idempotency_key = ?",
                    (principal_key, idempotency_key),
                ).fetchone()
                conn.commit()
                result = self._companion_idempotency_row_to_dict(row)
                result["result"] = (
                    "replay" if result["state"] in {"complete", "failed"} else result["state"]
                )
                return result
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to update idempotency key for {principal_type}:{principal_id}"
            ) from e

    def companion_idempotency_mark_transmitted(
        self,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        message_id: int,
        packet_hash: Optional[str],
        expected_ack: Optional[int],
    ) -> Dict[str, Any]:
        return self._companion_idempotency_transition(
            principal_type,
            principal_id,
            idempotency_key,
            request_hash,
            "transmitted",
            message_id=message_id,
            packet_hash=packet_hash,
            expected_ack=expected_ack,
        )

    def companion_idempotency_complete(
        self,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        response_json: str,
        message_id: Optional[int] = None,
        packet_hash: Optional[str] = None,
        expected_ack: Optional[int] = None,
        state: str = "complete",
    ) -> Dict[str, Any]:
        if state not in {"complete", "failed"}:
            raise ValueError("completion state must be 'complete' or 'failed'")
        return self._companion_idempotency_transition(
            principal_type,
            principal_id,
            idempotency_key,
            request_hash,
            state,
            response_json=response_json,
            message_id=message_id,
            packet_hash=packet_hash,
            expected_ack=expected_ack,
        )

    def companion_idempotency_mark_indeterminate(
        self,
        principal_type: str,
        principal_id: str,
        idempotency_key: str,
        request_hash: str,
        message_id: Optional[int] = None,
        packet_hash: Optional[str] = None,
        expected_ack: Optional[int] = None,
    ) -> Dict[str, Any]:
        return self._companion_idempotency_transition(
            principal_type,
            principal_id,
            idempotency_key,
            request_hash,
            "indeterminate",
            message_id=message_id,
            packet_hash=packet_hash,
            expected_ack=expected_ack,
        )

    def companion_idempotency_get(
        self, device_id: str, idempotency_key: str
    ) -> Optional[Dict[str, Any]]:
        """Return the stored {request_hash, response_json, created_at} for
        this (device_id, idempotency_key), or None if no such row exists."""
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    self._companion_idempotency_select()
                    + " WHERE device_id = ? AND idempotency_key = ?",
                    (device_id, idempotency_key),
                ).fetchone()
                return self._companion_idempotency_row_to_dict(row) if row else None
        except Exception as e:
            logger.error(f"Failed to get companion idempotency record for device {device_id}: {e}")
            return None

    def companion_idempotency_put(
        self, device_id: str, idempotency_key: str, request_hash: str, response_json: str
    ) -> bool:
        """Record a (device_id, idempotency_key) -> response mapping.

        Uses INSERT OR IGNORE so a concurrent duplicate write (two retries
        racing each other) can't raise an IntegrityError; returns False if a
        row for this key already existed (whether from the race or an
        earlier call), True if this call created it.
        """
        try:
            response_json = _validated_json_object(response_json)
            with self._connect() as conn:
                now = time.time()
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO companion_idempotency
                    (device_id, idempotency_key, request_hash, response_json,
                     created_at, principal_type, principal_id, state, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'legacy', ?, 'complete', ?)
                    """,
                    (
                        device_id,
                        idempotency_key,
                        request_hash,
                        response_json,
                        now,
                        device_id,
                        now,
                    ),
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to put companion idempotency record for device {device_id}: {e}")
            return False

    def companion_idempotency_prune(self, max_age_seconds: float = 48 * 3600) -> int:
        """Prune old terminal rows and preserve ambiguous send outcomes."""
        retention_seconds = validate_positive_seconds(
            max_age_seconds,
            "companion idempotency max_age_seconds",
        )
        try:
            now = time.time()
            cutoff = now - retention_seconds
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                stale_sends = conn.execute(
                    """
                    SELECT i.message_id, i.packet_hash, i.expected_ack,
                           m.companion_hash
                    FROM companion_idempotency AS i
                    LEFT JOIN companion_messages AS m
                      ON m.id = i.message_id AND m.direction = 'out'
                    WHERE i.state IN ('pending', 'transmitted')
                      AND i.created_at < ?
                    """,
                    (cutoff,),
                ).fetchall()
                for send in stale_sends:
                    if send["message_id"] is None or send["companion_hash"] is None:
                        continue
                    self._companion_advance_outbound_state_row(
                        conn,
                        send["companion_hash"],
                        int(send["message_id"]),
                        "indeterminate",
                        send["packet_hash"],
                        send["expected_ack"],
                        now,
                    )
                conn.execute(
                    """
                    UPDATE companion_idempotency
                    SET state = 'indeterminate', updated_at = ?
                    WHERE state IN ('pending', 'transmitted') AND created_at < ?
                    """,
                    (now, cutoff),
                )
                result = conn.execute(
                    """
                    DELETE FROM companion_idempotency
                    WHERE state IN ('complete', 'failed') AND created_at < ?
                    """,
                    (cutoff,),
                )
                deleted = result.rowcount
                conn.commit()
                if deleted:
                    logger.info(f"Pruned {deleted} old companion idempotency record(s)")
                return deleted
        except Exception as e:
            logger.error(f"Failed to prune companion idempotency records: {e}")
            raise

    def companion_idempotency_recover_incomplete(self) -> int:
        """Make sends interrupted by a process restart explicitly ambiguous.

        No live request survives construction of a new ``SQLiteHandler``.
        Leaving its old ``pending`` or ``transmitted`` key as ``in_progress``
        would strand a client; deleting it could double-send over RF.
        """

        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                now = time.time()
                pending_messages = conn.execute(
                    """
                    SELECT id, companion_hash, packet_hash, expected_ack
                    FROM companion_messages
                    WHERE direction = 'out' AND state = 'pending'
                    """
                ).fetchall()
                for message in pending_messages:
                    message_id = int(message["id"])
                    conn.execute(
                        """
                        UPDATE companion_messages
                        SET state = 'indeterminate'
                        WHERE id = ? AND direction = 'out' AND state = 'pending'
                        """,
                        (message_id,),
                    )
                    self._companion_append_event_row(
                        conn,
                        message["companion_hash"],
                        "message_send_state",
                        {
                            "message_id": message_id,
                            "state": "indeterminate",
                            "packet_hash": message["packet_hash"],
                            "expected_ack": message["expected_ack"],
                        },
                        ref_table="companion_messages",
                        ref_id=message_id,
                        packet_hash=message["packet_hash"],
                        created_at=now,
                    )
                result = conn.execute(
                    """
                    UPDATE companion_idempotency
                    SET state = 'indeterminate', updated_at = ?
                    WHERE state IN ('pending', 'transmitted')
                    """,
                    (now,),
                )
                conn.commit()
                if result.rowcount or pending_messages:
                    logger.warning(
                        "Recovered %d request key(s) and %d message(s) "
                        "from interrupted companion sends as indeterminate",
                        result.rowcount,
                        len(pending_messages),
                    )
                return result.rowcount
        except Exception as e:
            raise CompanionStorageError("Failed to recover interrupted companion sends") from e

    # --- Companion devices (design doc §5.4, §11.2 pairing) -----------------

    def companion_device_create(
        self,
        companion_hash: str,
        device_id: str,
        name: str,
        token_id: int,
        platform: Optional[str] = None,
        push_relay_url: Optional[str] = None,
        companion_identity: Optional[str] = None,
    ) -> Optional[int]:
        """Create a companion_devices row for a newly paired device.

        Returns the new row's id, or None on failure (e.g. device_id already
        registered — the column is UNIQUE).
        """
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT 1 FROM companion_devices WHERE token_id = ?",
                    (token_id,),
                ).fetchone()
                if existing is not None:
                    return None
                cursor = conn.execute(
                    """
                    INSERT INTO companion_devices
                    (companion_hash, companion_identity, device_id, name, token_id,
                     platform, push_relay_url, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        companion_hash,
                        companion_identity,
                        device_id,
                        name,
                        token_id,
                        platform,
                        push_relay_url,
                        time.time(),
                    ),
                )
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Failed to create companion device {device_id}: {e}")
            return None

    @staticmethod
    def _companion_device_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "companion_hash": row["companion_hash"],
            "companion_identity": (
                row["companion_identity"] if "companion_identity" in row.keys() else None
            ),
            "device_id": row["device_id"],
            "name": row["name"],
            "token_id": row["token_id"],
            "platform": row["platform"],
            "push_token": row["push_token"],
            "push_relay_url": row["push_relay_url"],
            "push_detail": (row["push_detail"] if "push_detail" in row.keys() else "none"),
            "mention_push": (bool(row["mention_push"]) if "mention_push" in row.keys() else False),
            "mention_keywords": (
                row["mention_keywords"] if "mention_keywords" in row.keys() else None
            ),
            "created_at": _finite_storage_float(
                row["created_at"],
                "companion device created_at",
            ),
            "last_seen": _optional_finite_storage_float(
                row["last_seen"],
                "companion device last_seen",
            ),
            "last_synced_seq": row["last_synced_seq"],
        }

    def companion_pair_device(
        self,
        companion_hash: str,
        companion_identity: str,
        device_id: str,
        name: str,
        token_name: str,
        token_hash: str,
        scope: str,
        platform: Optional[str] = None,
        push_relay_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create the API token and immutable device binding in one transaction."""
        if not companion_identity:
            raise ValueError("companion_identity is required")
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute(
                    "SELECT 1 FROM companion_devices WHERE device_id = ?",
                    (device_id,),
                ).fetchone():
                    raise CompanionStorageError(f"Device {device_id} is already paired")
                now = time.time()
                token_cursor = conn.execute(
                    """
                    INSERT INTO api_tokens (name, token_hash, created_at, scope)
                    VALUES (?, ?, ?, ?)
                    """,
                    (token_name, token_hash, now, scope),
                )
                token_id = int(token_cursor.lastrowid)
                device_cursor = conn.execute(
                    """
                    INSERT INTO companion_devices
                    (companion_hash, companion_identity, device_id, name, token_id,
                     platform, push_relay_url, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        companion_hash,
                        companion_identity,
                        device_id,
                        name,
                        token_id,
                        platform,
                        push_relay_url,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM companion_devices WHERE id = ?",
                    (int(device_cursor.lastrowid),),
                ).fetchone()
                conn.commit()
                return {
                    "token_id": token_id,
                    "device": self._companion_device_row_to_dict(row),
                }
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(f"Failed to pair device {device_id}") from e

    def companion_revoke_device(
        self,
        *,
        device_id: Optional[str] = None,
        token_id: Optional[int] = None,
        expected_token_id: Optional[int] = None,
    ) -> Dict[str, int]:
        """Remove a paired device and its token in one transaction.

        Either identifier is accepted.  All legacy rows sharing the resolved
        token are removed together, closing historical one-to-many bindings.
        ``expected_token_id`` makes self-revocation an atomic ownership check.
        """
        if device_id is None and token_id is None:
            raise ValueError("device_id or token_id is required")
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                resolved_token_id = token_id
                if resolved_token_id is None:
                    row = conn.execute(
                        "SELECT token_id FROM companion_devices WHERE device_id = ?",
                        (device_id,),
                    ).fetchone()
                    if row is None:
                        return {"devices_deleted": 0, "tokens_deleted": 0}
                    resolved_token_id = int(row[0])
                if expected_token_id is not None and int(resolved_token_id) != int(
                    expected_token_id
                ):
                    return {"devices_deleted": 0, "tokens_deleted": 0}

                # Preserve old numeric-principal reservations while the final
                # row-id -> stable-device-id mapping is still available.
                self._migrate_device_idempotency_principals(
                    conn,
                    token_id=int(resolved_token_id),
                )
                devices = conn.execute(
                    "DELETE FROM companion_devices WHERE token_id = ?",
                    (int(resolved_token_id),),
                ).rowcount
                tokens = conn.execute(
                    "DELETE FROM api_tokens WHERE id = ?",
                    (int(resolved_token_id),),
                ).rowcount
                conn.commit()
                return {
                    "devices_deleted": int(devices),
                    "tokens_deleted": int(tokens),
                }
        except Exception as e:
            raise CompanionStorageError("Failed to revoke companion device") from e

    def companion_device_get(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Return the companion_devices row for device_id, or None."""
        try:
            return self.companion_device_get_strict(device_id)
        except CompanionStorageError as e:
            logger.error(f"Failed to get companion device {device_id}: {e}")
            return None

    def companion_device_get_strict(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Return a paired device, raising on an uncertain database read."""

        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM companion_devices WHERE device_id = ?", (device_id,)
                ).fetchone()
                return self._companion_device_row_to_dict(row) if row else None
        except Exception as e:
            raise CompanionStorageError(f"Failed to get companion device {device_id}") from e

    def companion_device_get_by_token(self, token_id: int) -> Optional[Dict[str, Any]]:
        """Return the companion_devices row linked to token_id, or None."""
        try:
            return self.companion_device_get_by_token_strict(token_id)
        except CompanionStorageError as e:
            logger.error(f"Failed to get companion device for token {token_id}: {e}")
            return None

    def companion_device_get_by_token_strict(self, token_id: int) -> Optional[Dict[str, Any]]:
        """Return a token's paired device, raising on an uncertain DB read.

        Authorization and ownership checks must distinguish a missing binding
        from unavailable storage.  Legacy callers keep using
        :meth:`companion_device_get_by_token`, whose historical error result is
        ``None``. Historical schemas allowed one token to point at multiple
        devices; that is not a deterministic authorization binding, so this
        strict lookup refuses it instead of selecting an arbitrary row.
        """

        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT * FROM companion_devices
                    WHERE token_id = ?
                    ORDER BY id ASC
                    LIMIT 2
                    """,
                    (token_id,),
                ).fetchall()
                if len(rows) > 1:
                    raise CompanionStorageError(
                        f"API token {token_id} has multiple paired-device bindings; "
                        "revoke it and pair each device again"
                    )
                return self._companion_device_row_to_dict(rows[0]) if rows else None
        except CompanionStorageError:
            raise
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to get companion device for token {token_id}"
            ) from e

    def companion_device_list(self, companion_hash: Optional[str] = None) -> List[Dict[str, Any]]:
        """List companion_devices rows, optionally filtered to one companion_hash."""
        try:
            return self.companion_device_list_strict(companion_hash)
        except CompanionStorageError as e:
            logger.error(f"Failed to list companion devices: {e}")
            return []

    def companion_device_list_strict(
        self, companion_hash: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List paired devices, raising when storage is unavailable."""

        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                if companion_hash is not None:
                    rows = conn.execute(
                        "SELECT * FROM companion_devices WHERE companion_hash = ? "
                        "ORDER BY created_at DESC",
                        (companion_hash,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM companion_devices ORDER BY created_at DESC"
                    ).fetchall()
                return [self._companion_device_row_to_dict(row) for row in rows]
        except Exception as e:
            raise CompanionStorageError("Failed to list companion devices") from e

    def companion_device_touch(
        self,
        device_id: str,
        last_seen: Optional[float] = None,
        last_synced_seq: Optional[int] = None,
    ) -> bool:
        """Update last_seen and/or last_synced_seq for a device.

        Only the provided fields are updated; omitted ones are left as-is.
        last_seen defaults to now when neither argument is given a value by
        the caller AND last_synced_seq is also omitted -- i.e. a bare call
        with no arguments still records a "seen now" heartbeat.
        """
        try:
            updates = []
            params: List[Any] = []
            if last_seen is not None:
                updates.append("last_seen = ?")
                params.append(last_seen)
            if last_synced_seq is not None:
                updates.append("last_synced_seq = ?")
                params.append(last_synced_seq)
            if not updates:
                # Bare call: at least bump last_seen to now.
                updates.append("last_seen = ?")
                params.append(time.time())

            params.append(device_id)
            with self._connect() as conn:
                # Update fields are selected internally.
                cursor = conn.execute(
                    f"UPDATE companion_devices SET {', '.join(updates)} WHERE device_id = ?",  # nosec B608
                    params,
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to touch companion device {device_id}: {e}")
            return False

    def companion_device_delete(self, device_id: str) -> bool:
        """Delete a companion_devices row (e.g. on token revocation)."""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM companion_devices WHERE device_id = ?", (device_id,)
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete companion device {device_id}: {e}")
            return False

    def companion_device_set_push(
        self,
        device_id: str,
        push_token: str,
        push_relay_url: Optional[str] = None,
        push_detail: Optional[str] = None,
        mention_push: Optional[bool] = None,
        mention_keywords: Optional[list] = None,
    ) -> bool:
        """Register/update a device's push credentials (design doc §12.2).

        The relay URL column is retained only for schema compatibility; the
        notifier uses the operator-owned ``companion.push.relay_url``.
        Passing an empty legacy URL clears it. ``push_detail``
        (``none``/``count``/``preview``) is the per-device content level.
        ``mention_push`` toggles the content-free mention-alert class and
        ``mention_keywords`` is the per-device trigger list (stored as JSON).
        Optional preference fields are written only when provided.
        """
        try:
            return self.companion_device_set_push_strict(
                device_id,
                push_token,
                push_relay_url=push_relay_url,
                push_detail=push_detail,
                mention_push=mention_push,
                mention_keywords=mention_keywords,
            )
        except CompanionStorageError as e:
            logger.error(f"Failed to set push for companion device {device_id}: {e}")
            return False

    def companion_device_set_push_strict(
        self,
        device_id: str,
        push_token: str,
        push_relay_url: Optional[str] = None,
        push_detail: Optional[str] = None,
        mention_push: Optional[bool] = None,
        mention_keywords: Optional[list] = None,
        expected_token_id: Optional[int] = None,
    ) -> bool:
        """Update push settings, optionally only for the expected pairing."""

        try:
            updates = ["push_token = ?"]
            params: List[Any] = [push_token]
            if push_relay_url is not None:
                updates.append("push_relay_url = ?")
                params.append(push_relay_url or None)
            if push_detail is not None:
                updates.append("push_detail = ?")
                params.append(push_detail)
            if mention_push is not None:
                updates.append("mention_push = ?")
                params.append(1 if mention_push else 0)
            if mention_keywords is not None:
                updates.append("mention_keywords = ?")
                params.append(json.dumps(list(mention_keywords), allow_nan=False))
            params.append(device_id)
            where = "device_id = ?"
            if expected_token_id is not None:
                where += " AND token_id = ?"
                params.append(int(expected_token_id))
            with self._connect() as conn:
                # Update and where fields are selected internally.
                cursor = conn.execute(
                    f"UPDATE companion_devices SET {', '.join(updates)} WHERE {where}",  # nosec B608
                    params,
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to set push for companion device {device_id}"
            ) from e

    def companion_device_clear_push(self, device_id: str) -> bool:
        """Clear a device's push_token (unregister push; design doc §12.2).

        Also clears any legacy device-selected relay URL. Preferences remain,
        so a later registration needs only a new token.
        """
        try:
            return self.companion_device_clear_push_strict(device_id)
        except CompanionStorageError as e:
            logger.error(f"Failed to clear push for companion device {device_id}: {e}")
            return False

    def companion_device_clear_push_strict(
        self,
        device_id: str,
        expected_token_id: Optional[int] = None,
    ) -> bool:
        """Clear push credentials, optionally only for the expected pairing."""

        try:
            if expected_token_id is None:
                query = """
                    UPDATE companion_devices
                    SET push_token = NULL, push_relay_url = NULL
                    WHERE device_id = ?
                """
                params: List[Any] = [device_id]
            else:
                query = """
                    UPDATE companion_devices
                    SET push_token = NULL, push_relay_url = NULL
                    WHERE device_id = ? AND token_id = ?
                """
                params = [device_id, int(expected_token_id)]
            with self._connect() as conn:
                cursor = conn.execute(query, params)
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to clear push for companion device {device_id}"
            ) from e

    def companion_device_clear_push_if_token_strict(
        self,
        device_id: str,
        push_token: str,
        companion_hash: str,
        companion_identity: str,
    ) -> bool:
        """Clear credentials only while the complete device binding is current.

        A relay response can arrive after a client refreshes its token.  The
        comparisons in the UPDATE keep that stale response from unregistering
        a refreshed token or a new pairing that reused the same device ID.
        """

        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE companion_devices
                    SET push_token = NULL, push_relay_url = NULL
                    WHERE device_id = ?
                      AND push_token = ?
                      AND companion_hash = ?
                      AND companion_identity = ?
                    """,
                    (
                        device_id,
                        push_token,
                        companion_hash,
                        companion_identity,
                    ),
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            raise CompanionStorageError(
                f"Failed to conditionally clear push for companion device {device_id}"
            ) from e

    def companion_devices_with_push(
        self,
        companion_hash: str,
        companion_identity: str,
    ) -> List[Dict[str, Any]]:
        """Return push devices belonging to one active companion identity.

        This is the notifier's fan-out query on a journal event (design doc
        §12.2): only devices that actually registered for push are returned,
        so the common "nobody registered" case is a single indexed-ish scan
        of the small companion_devices table returning nothing. Production
        callers must provide ``companion_identity`` so a colliding one-byte
        hash can never select another identity's devices.
        """
        hash_value, identity_value = self._normalize_companion_namespace(
            companion_hash,
            companion_identity,
        )
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT * FROM companion_devices
                    WHERE companion_hash = ?
                      AND lower(trim(companion_identity)) = ?
                      AND push_token IS NOT NULL
                    """,
                    (hash_value, identity_value),
                ).fetchall()
                return [self._companion_device_row_to_dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to list push devices for companion {hash_value}: {e}")
            return []
