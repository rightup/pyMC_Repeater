#!/usr/bin/env python3
"""
Integration test for Phase 1: Compute-at-Ingest backend changes.

Tests the full pipeline:
  1. SQLiteHandler init creates new tables (topology_edges, hourly_stats, hourly_type_stats)
  2. Migration 4 adds airtime_ms + packet_origin to packets, backfills existing rows
  3. store_packet() accepts the new columns
  4. upsert_topology_edge() atomically increments edge counters
  5. upsert_hourly_stats() + upsert_hourly_type_stats() aggregate inline
  6. Query methods return correct data (get_hourly_stats, get_stats_summary, etc.)
  7. get_topology_edges() JOINs with adverts for node names/locations
  8. Simulates the storage_collector flow: _update_topology_edges + _update_hourly_stats

Run: python3 tests/test_compute_at_ingest.py
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

# Import SQLiteHandler directly, bypassing __init__.py which pulls in heavy deps
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "sqlite_handler",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "repeater", "data_acquisition", "sqlite_handler.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
SQLiteHandler = _mod.SQLiteHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REAL_DB = os.path.expanduser("~/dev/pyMC_RepeaterUI-DEV/repeater.db")
PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results = []


def check(name, condition, detail=""):
    results.append((name, condition))
    status = PASS if condition else FAIL
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")
    return condition


# ---------------------------------------------------------------------------
# Test 1: Fresh database — tables created
# ---------------------------------------------------------------------------

def test_fresh_database():
    print("\n=== Test 1: Fresh database init ===")
    tmp = tempfile.mkdtemp()
    try:
        handler = SQLiteHandler(Path(tmp))
        conn = sqlite3.connect(handler.sqlite_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]

        check("topology_edges table exists", "topology_edges" in tables)
        check("hourly_stats table exists", "hourly_stats" in tables)
        check("hourly_type_stats table exists", "hourly_type_stats" in tables)

        # Check packets table has the new columns (via _init + migration)
        cols = [c[1] for c in conn.execute("PRAGMA table_info(packets)").fetchall()]
        check("airtime_ms column in packets", "airtime_ms" in cols)
        check("packet_origin column in packets", "packet_origin" in cols)

        conn.close()
    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Test 2: Migration on real DB (copy)
# ---------------------------------------------------------------------------

def test_migration_on_real_db():
    print("\n=== Test 2: Migration on real DB copy ===")
    if not os.path.exists(REAL_DB):
        print(f"  [SKIP] Real DB not found at {REAL_DB}")
        return

    tmp = tempfile.mkdtemp()
    try:
        shutil.copy2(REAL_DB, os.path.join(tmp, "repeater.db"))
        conn_before = sqlite3.connect(os.path.join(tmp, "repeater.db"))
        count_before = conn_before.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
        conn_before.close()

        handler = SQLiteHandler(Path(tmp))
        conn = sqlite3.connect(handler.sqlite_path)

        count_after = conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
        check("packet count preserved", count_before == count_after,
              f"{count_before} → {count_after}")

        cols = [c[1] for c in conn.execute("PRAGMA table_info(packets)").fetchall()]
        check("airtime_ms column added", "airtime_ms" in cols)
        check("packet_origin column added", "packet_origin" in cols)

        # Check backfill
        null_origins = conn.execute(
            "SELECT COUNT(*) FROM packets WHERE packet_origin IS NULL"
        ).fetchone()[0]
        check("packet_origin backfilled (no NULLs)", null_origins == 0,
              f"{null_origins} NULLs remaining")

        # Check migration recorded
        migs = [m[0] for m in conn.execute("SELECT migration_name FROM migrations").fetchall()]
        check("migration recorded", "add_airtime_and_origin_to_packets" in migs)

        conn.close()
    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Test 3: store_packet with new columns
# ---------------------------------------------------------------------------

def test_store_packet_with_new_fields():
    print("\n=== Test 3: store_packet() with airtime_ms + packet_origin ===")
    tmp = tempfile.mkdtemp()
    try:
        handler = SQLiteHandler(Path(tmp))

        record = {
            "timestamp": time.time(),
            "type": 4, "route": 0, "length": 64,
            "rssi": -72, "snr": 8.5, "score": 0.85,
            "transmitted": True, "is_duplicate": False,
            "drop_reason": None,
            "src_hash": "AA", "dst_hash": "BB",
            "path_hash": "[AA, BB]",
            "header": "0x10", "transport_codes": None,
            "payload": "deadbeef", "payload_length": 64,
            "tx_delay_ms": 150.0,
            "packet_hash": "abc123def456",
            "original_path": json.dumps(["AA", "BB"]),
            "forwarded_path": json.dumps(["AA", "BB", "CC"]),
            "raw_packet": "cafebabe",
            "lbt_attempts": 0,
            "lbt_backoff_delays_ms": None,
            "lbt_channel_busy": False,
            "airtime_ms": 156.3,
            "packet_origin": "tx_forward",
        }
        handler.store_packet(record)

        conn = sqlite3.connect(handler.sqlite_path)
        row = conn.execute(
            "SELECT airtime_ms, packet_origin FROM packets ORDER BY id DESC LIMIT 1"
        ).fetchone()
        check("airtime_ms stored", row[0] == 156.3, f"got {row[0]}")
        check("packet_origin stored", row[1] == "tx_forward", f"got {row[1]}")
        conn.close()
    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Test 4: Topology edge UPSERT
# ---------------------------------------------------------------------------

def test_topology_edge_upsert():
    print("\n=== Test 4: upsert_topology_edge() ===")
    tmp = tempfile.mkdtemp()
    try:
        handler = SQLiteHandler(Path(tmp))

        # First packet: AA→BB, flood, forward direction
        handler.upsert_topology_edge(
            edge_key="AA-BB", from_prefix="AA", to_prefix="BB",
            rssi=-70.0, snr=8.0, is_forward=True, is_flood=True
        )

        # Second packet: BB→AA (reverse), direct
        handler.upsert_topology_edge(
            edge_key="AA-BB", from_prefix="AA", to_prefix="BB",
            rssi=-65.0, snr=9.0, is_forward=False, is_flood=False
        )

        # Third packet: same edge, with hash info
        handler.upsert_topology_edge(
            edge_key="AA-BB", from_prefix="AA", to_prefix="BB",
            rssi=-60.0, snr=10.0, is_forward=True, is_flood=True,
            from_hash="aabb1122", to_hash="ccdd3344"
        )

        conn = sqlite3.connect(handler.sqlite_path)
        row = conn.execute("SELECT * FROM topology_edges WHERE edge_key = 'AA-BB'").fetchone()
        conn.close()

        # row indices: 0=edge_key, 1=from, 2=to, 3=from_hash, 4=to_hash,
        # 5=packet_count, 6=fwd, 7=rev, 8=flood, 9=direct,
        # 10=avg_rssi, 11=avg_snr, 12=first, 13=last, 14=zero_hop
        check("packet_count=3", row[5] == 3, f"got {row[5]}")
        check("forward_count=2", row[6] == 2, f"got {row[6]}")
        check("reverse_count=1", row[7] == 1, f"got {row[7]}")
        check("flood_count=2", row[8] == 2, f"got {row[8]}")
        check("direct_count=1", row[9] == 1, f"got {row[9]}")
        check("from_hash set", row[3] == "aabb1122", f"got {row[3]}")
        check("to_hash set", row[4] == "ccdd3344", f"got {row[4]}")
        check("avg_rssi is numeric", row[10] is not None and isinstance(row[10], float))

    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Test 5: Hourly stats UPSERT
# ---------------------------------------------------------------------------

def test_hourly_stats_upsert():
    print("\n=== Test 5: upsert_hourly_stats() + upsert_hourly_type_stats() ===")
    tmp = tempfile.mkdtemp()
    try:
        handler = SQLiteHandler(Path(tmp))
        hour_ts = int(time.time()) // 3600 * 3600

        # Simulate 3 packets in same hour
        handler.upsert_hourly_stats(
            hour_ts, "rx", 150.0, rssi=-70, snr=8.0,
            packet_hash="hash1", payload_length=64
        )
        handler.upsert_hourly_stats(
            hour_ts, "tx_forward", 200.0, rssi=-65, snr=9.0,
            packet_hash="hash2", payload_length=128
        )
        handler.upsert_hourly_stats(
            hour_ts, "rx", 100.0, rssi=-75, snr=7.0,
            packet_hash="hash3", payload_length=32, is_drop=True
        )

        # Type stats
        handler.upsert_hourly_type_stats(hour_ts, 4, 150.0, 64)
        handler.upsert_hourly_type_stats(hour_ts, 4, 200.0, 128)
        handler.upsert_hourly_type_stats(hour_ts, 8, 100.0, 32)

        conn = sqlite3.connect(handler.sqlite_path)
        row = conn.execute(
            "SELECT * FROM hourly_stats WHERE hour_timestamp = ?", (hour_ts,)
        ).fetchone()

        # row: 0=hour, 1=rx, 2=tx, 3=fwd, 4=drop,
        # 5=rx_air, 6=tx_air, 7=fwd_air, 8=rssi, 9=snr, 10=hashes, 11=bytes
        check("rx_count=2", row[1] == 2, f"got {row[1]}")
        check("tx_count=0", row[2] == 0, f"got {row[2]}")
        check("fwd_count=1", row[3] == 1, f"got {row[3]}")
        check("drop_count=1", row[4] == 1, f"got {row[4]}")
        check("rx_airtime_ms=250", row[5] == 250.0, f"got {row[5]}")
        check("fwd_airtime_ms=200", row[7] == 200.0, f"got {row[7]}")
        check("unique_hashes=3", row[10] == 3, f"got {row[10]}")
        check("bytes_total=224", row[11] == 224, f"got {row[11]}")

        type_rows = conn.execute(
            "SELECT * FROM hourly_type_stats WHERE hour_timestamp = ? ORDER BY packet_type",
            (hour_ts,)
        ).fetchall()
        check("2 type rows", len(type_rows) == 2, f"got {len(type_rows)}")
        check("type 4 count=2", type_rows[0][2] == 2, f"got {type_rows[0][2]}")
        check("type 8 count=1", type_rows[1][2] == 1, f"got {type_rows[1][2]}")

        conn.close()
    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Test 6: Query methods
# ---------------------------------------------------------------------------

def test_query_methods():
    print("\n=== Test 6: Query methods ===")
    tmp = tempfile.mkdtemp()
    try:
        handler = SQLiteHandler(Path(tmp))
        hour_ts = int(time.time()) // 3600 * 3600

        # Seed data
        handler.upsert_hourly_stats(hour_ts, "rx", 150.0, -70, 8.0, "h1", 64)
        handler.upsert_hourly_stats(hour_ts, "tx_forward", 200.0, -65, 9.0, "h2", 128)
        handler.upsert_hourly_type_stats(hour_ts, 4, 150.0, 64)
        handler.upsert_topology_edge("AA-BB", "AA", "BB", -70, 8.0, True, True)

        # Test get_hourly_stats
        rows = handler.get_hourly_stats(hours=1)
        check("get_hourly_stats returns data", len(rows) == 1, f"got {len(rows)} rows")
        check("hourly row has rx_count", rows[0]["rx_count"] == 1)

        # Test get_stats_summary
        summary = handler.get_stats_summary(hours=1)
        check("summary has rx_count", summary["rx_count"] == 1)
        check("summary has fwd_count", summary["fwd_count"] == 1)
        check("summary bytes_total=192", summary["bytes_total"] == 192)

        # Test get_topology_edges (min_packets=1 since we only have 1)
        edges = handler.get_topology_edges(hours=1, min_packets=1)
        check("get_topology_edges returns data", len(edges) == 1, f"got {len(edges)} edges")
        check("edge has from_prefix=AA", edges[0]["from_prefix"] == "AA")
        # from_name/to_name will be None since no adverts
        check("from_name is None (no adverts)", edges[0]["from_name"] is None)

        # Test get_hourly_type_stats
        type_stats = handler.get_hourly_type_stats(hours=1)
        check("type stats returns data", len(type_stats) == 1)
        check("type 4 count=1", type_stats[0]["count"] == 1)

    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Test 7: Topology edges JOIN with adverts
# ---------------------------------------------------------------------------

def test_topology_edge_join_adverts():
    print("\n=== Test 7: Topology edges JOIN with adverts ===")
    tmp = tempfile.mkdtemp()
    try:
        handler = SQLiteHandler(Path(tmp))

        # Create an advert for one node
        handler.store_advert({
            "pubkey": "aabb1122334455",
            "node_name": "Node-Alpha",
            "is_repeater": True,
            "route_type": 0,
            "contact_type": "repeater",
            "latitude": 33.45,
            "longitude": -112.07,
            "zero_hop": True,
        })

        # Create an edge referencing that advert by pubkey
        handler.upsert_topology_edge(
            "AA-BB", "AA", "BB",
            rssi=-70, snr=8.0, is_forward=True, is_flood=True,
            from_hash="aabb1122334455",
        )

        edges = handler.get_topology_edges(hours=1, min_packets=1)
        check("edge returned", len(edges) == 1)
        check("from_name resolved", edges[0]["from_name"] == "Node-Alpha",
              f"got {edges[0]['from_name']}")
        check("from_lat resolved", edges[0]["from_lat"] == 33.45,
              f"got {edges[0]['from_lat']}")
        check("to_name is None", edges[0]["to_name"] is None)

    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Test 8: Simulated storage_collector flow
# ---------------------------------------------------------------------------

def test_storage_collector_flow():
    """Simulate _update_topology_edges + _update_hourly_stats logic."""
    print("\n=== Test 8: Simulated storage_collector flow ===")
    tmp = tempfile.mkdtemp()
    try:
        handler = SQLiteHandler(Path(tmp))

        # Simulate a packet_record as engine.py would build it
        packet_record = {
            "timestamp": time.time(),
            "type": 4,
            "route": 0,  # flood
            "length": 64,
            "rssi": -72,
            "snr": 8.5,
            "transmitted": True,
            "is_duplicate": False,
            "drop_reason": None,
            "src_hash": "AA",
            "packet_hash": "abc123",
            "payload_length": 64,
            "original_path": ["AA", "BB", "CC"],
            "airtime_ms": 156.3,
            "packet_origin": "tx_forward",
        }

        # --- Simulate _update_topology_edges ---
        original_path = packet_record.get("original_path")
        route = packet_record.get("route", 0)
        is_flood = route in (0, 1)
        rssi = packet_record.get("rssi")
        snr = packet_record.get("snr")

        for i in range(len(original_path) - 1):
            a, b = original_path[i], original_path[i + 1]
            if a <= b:
                edge_key = f"{a}-{b}"
                from_p, to_p = a, b
                is_fwd = True
            else:
                edge_key = f"{b}-{a}"
                from_p, to_p = b, a
                is_fwd = False
            hop_rssi = rssi if i == 0 else None
            hop_snr = snr if i == 0 else None
            handler.upsert_topology_edge(
                edge_key, from_p, to_p,
                rssi=hop_rssi, snr=hop_snr,
                is_forward=is_fwd, is_flood=is_flood,
            )

        # --- Simulate _update_hourly_stats ---
        ts = packet_record["timestamp"]
        hour_ts = int(ts) // 3600 * 3600
        handler.upsert_hourly_stats(
            hour_ts,
            packet_record["packet_origin"],
            packet_record["airtime_ms"],
            rssi, snr,
            packet_record["packet_hash"],
            packet_record["payload_length"],
            is_drop=bool(packet_record.get("drop_reason")),
        )
        handler.upsert_hourly_type_stats(
            hour_ts,
            packet_record["type"],
            packet_record["airtime_ms"],
            packet_record["payload_length"],
        )

        # Verify edges
        edges = handler.get_topology_edges(hours=1, min_packets=1)
        check("2 edges from path [AA,BB,CC]", len(edges) == 2,
              f"got {len(edges)}")
        edge_keys = {e["edge_key"] for e in edges}
        check("edge AA-BB exists", "AA-BB" in edge_keys)
        check("edge BB-CC exists", "BB-CC" in edge_keys)

        # Find AA-BB edge and check RSSI was set (first hop)
        aa_bb = [e for e in edges if e["edge_key"] == "AA-BB"][0]
        check("AA-BB has rssi", aa_bb["avg_rssi"] is not None and aa_bb["avg_rssi"] == -72.0,
              f"got {aa_bb['avg_rssi']}")

        # BB-CC should NOT have RSSI (second hop)
        bb_cc = [e for e in edges if e["edge_key"] == "BB-CC"][0]
        check("BB-CC has no rssi (2nd hop)", bb_cc["avg_rssi"] is None,
              f"got {bb_cc['avg_rssi']}")

        # Verify hourly stats
        summary = handler.get_stats_summary(hours=1)
        check("summary fwd_count=1", summary["fwd_count"] == 1)
        check("summary fwd_airtime=156.3", summary["fwd_airtime_ms"] == 156.3,
              f"got {summary['fwd_airtime_ms']}")

    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Test 9: Zero-hop edge from advert
# ---------------------------------------------------------------------------

def test_zero_hop_edge():
    print("\n=== Test 9: Zero-hop edge from advert ===")
    tmp = tempfile.mkdtemp()
    try:
        handler = SQLiteHandler(Path(tmp))

        handler.upsert_topology_edge(
            "0A-FF", "0A", "FF",
            rssi=-50, snr=12.0,
            is_forward=True, is_flood=True,
            is_zero_hop=True,
            from_hash="pubkey_0a",
        )

        conn = sqlite3.connect(handler.sqlite_path)
        row = conn.execute(
            "SELECT is_zero_hop FROM topology_edges WHERE edge_key = '0A-FF'"
        ).fetchone()
        check("is_zero_hop=True", bool(row[0]) is True, f"got {row[0]}")
        conn.close()

    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("Phase 1 Compute-at-Ingest Integration Tests")
    print("=" * 60)

    test_fresh_database()
    test_migration_on_real_db()
    test_store_packet_with_new_fields()
    test_topology_edge_upsert()
    test_hourly_stats_upsert()
    test_query_methods()
    test_topology_edge_join_adverts()
    test_storage_collector_flow()
    test_zero_hop_edge()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    total = len(results)
    print(f"Results: {passed}/{total} passed, {failed} failed")

    if failed > 0:
        print("\nFailed tests:")
        for name, ok in results:
            if not ok:
                print(f"  ✗ {name}")
        sys.exit(1)
    else:
        print("\nAll tests passed! ✓")
        sys.exit(0)
