import base64
import sys
import types
from pathlib import Path

import pytest

from repeater.data_acquisition.sqlite_handler import SQLiteHandler


def _make_handler(tmp_path: Path) -> SQLiteHandler:
    return SQLiteHandler(tmp_path)


def test_api_token_crud_cycle(tmp_path):
    h = _make_handler(tmp_path)

    token_id = h.create_api_token("svc-a", "hash-a")
    assert isinstance(token_id, int)

    verified = h.verify_api_token("hash-a")
    assert verified is not None
    assert verified["id"] == token_id
    assert verified["name"] == "svc-a"

    listed = h.list_api_tokens()
    assert any(t["id"] == token_id for t in listed)

    assert h.revoke_api_token(token_id) is True
    assert h.verify_api_token("hash-a") is None
    assert h.revoke_api_token(token_id) is False


def test_transport_key_crud_cycle(tmp_path):
    h = _make_handler(tmp_path)

    key_id = h.create_transport_key(
        name="root",
        flood_policy="allow",
        transport_key="dGVzdC1rZXk=",  # base64('test-key')
    )
    assert isinstance(key_id, int)

    row = h.get_transport_key_by_id(key_id)
    assert row is not None
    assert row["name"] == "root"
    assert row["flood_policy"] == "allow"

    # No fields to update returns False by design.
    assert h.update_transport_key(key_id) is False

    assert h.update_transport_key(key_id, name="child", flood_policy="deny") is True
    row2 = h.get_transport_key_by_id(key_id)
    assert row2 is not None
    assert row2["name"] == "child"
    assert row2["flood_policy"] == "deny"

    all_rows = h.get_transport_keys()
    assert len(all_rows) == 1
    assert all_rows[0]["id"] == key_id

    assert h.delete_transport_key(key_id) is True
    assert h.get_transport_key_by_id(key_id) is None
    assert h.delete_transport_key(key_id) is False


def test_generate_transport_key_uses_implicit_hashtag_region(tmp_path, monkeypatch):
    h = _make_handler(tmp_path)

    captured = {}
    fake_transport_keys = types.ModuleType("openhop_core.protocol.transport_keys")

    def _fake_get_auto_key_for(name: str) -> bytes:
        captured["name"] = name
        return b"0123456789abcdef"

    fake_transport_keys.get_auto_key_for = _fake_get_auto_key_for

    fake_protocol = types.ModuleType("openhop_core.protocol")
    fake_protocol.transport_keys = fake_transport_keys

    fake_core = types.ModuleType("openhop_core")
    fake_core.protocol = fake_protocol

    monkeypatch.setitem(sys.modules, "openhop_core", fake_core)
    monkeypatch.setitem(sys.modules, "openhop_core.protocol", fake_protocol)
    monkeypatch.setitem(sys.modules, "openhop_core.protocol.transport_keys", fake_transport_keys)

    generated = h.generate_transport_key("eu")
    generated_bytes = base64.b64decode(generated)

    assert captured["name"] == "eu"
    assert generated_bytes == b"0123456789abcdef"
    assert len(generated_bytes) == 16


def test_room_messages_and_sync_flow(tmp_path):
    h = _make_handler(tmp_path)

    room_hash = "0x42"
    a_pub = "a" * 64
    b_pub = "b" * 64

    m1 = h.insert_room_message(room_hash, a_pub, "hello", post_timestamp=100.0)
    m2 = h.insert_room_message(room_hash, b_pub, "world", post_timestamp=200.0)
    assert isinstance(m1, int)
    assert isinstance(m2, int)

    assert h.get_room_message_count(room_hash) == 2

    # get_room_messages sorts by post_timestamp DESC
    msgs = h.get_room_messages(room_hash, limit=10, offset=0)
    assert len(msgs) == 2
    assert msgs[0]["message_text"] == "world"

    since = h.get_messages_since(room_hash, since_timestamp=150.0, limit=10)
    assert len(since) == 1
    assert since[0]["message_text"] == "world"

    unsynced_for_a = h.get_unsynced_messages(room_hash, client_pubkey=a_pub, sync_since=0.0)
    assert len(unsynced_for_a) == 1
    assert unsynced_for_a[0]["author_pubkey"] == b_pub

    assert h.get_unsynced_count(room_hash, client_pubkey=a_pub, sync_since=0.0) == 1

    # Client sync upsert/get/list
    assert h.upsert_client_sync(room_hash, a_pub, sync_since=50.0, last_activity=123.0) is True
    sync = h.get_client_sync(room_hash, a_pub)
    assert sync is not None
    assert sync["sync_since"] == 50.0

    clients = h.get_all_room_clients(room_hash)
    assert len(clients) == 1
    assert clients[0]["client_pubkey"] == a_pub

    assert h.delete_room_message(room_hash, int(m1)) is True
    assert h.delete_room_message(room_hash, int(m1)) is False

    deleted = h.clear_room_messages(room_hash)
    assert deleted == 1
    assert h.get_room_message_count(room_hash) == 0


def test_store_and_delete_advert(tmp_path):
    h = _make_handler(tmp_path)

    h.store_advert(
        {
            "timestamp": 123.0,
            "pubkey": "pk1",
            "node_name": "node-1",
            "is_repeater": True,
            "route_type": 1,
            "contact_type": "neighbor",
            "latitude": 1.0,
            "longitude": 2.0,
            "rssi": -88,
            "snr": 7.5,
            "is_new_neighbor": True,
            "zero_hop": True,
        }
    )

    with h._connect() as conn:
        row = conn.execute("SELECT id FROM adverts WHERE pubkey = ?", ("pk1",)).fetchone()
    assert row is not None
    advert_id = int(row[0])

    assert h.delete_advert(advert_id) is True
    assert h.delete_advert(advert_id) is False


def test_store_packet_returns_inserted_row_id(tmp_path):
    h = _make_handler(tmp_path)

    packet_id = h.store_packet(
        {
            "timestamp": 123.0,
            "type": 1,
            "route": 2,
            "length": 3,
            "transmitted": True,
            "packet_hash": "pkt-1",
        }
    )

    assert isinstance(packet_id, int)
    assert packet_id > 0

    with h._connect() as conn:
        row = conn.execute(
            "SELECT id, type, route, length FROM packets WHERE id = ?",
            (packet_id,),
        ).fetchone()

    assert row is not None
    assert row[0] == packet_id
    assert row[1] == 1
    assert row[2] == 2
    assert row[3] == 3


def test_packets_table_has_upstream_columns_and_index(tmp_path):
    h = _make_handler(tmp_path)

    with h._connect() as conn:
        cols = conn.execute("PRAGMA table_info(packets)").fetchall()
        col_names = {col[1] for col in cols}
        idx_rows = conn.execute("PRAGMA index_list(packets)").fetchall()
        idx_names = {row[1] for row in idx_rows}

    assert "upstream_hash" in col_names
    assert "upstream_hash_size" in col_names
    assert "idx_packets_upstream_time" in idx_names


def test_store_packet_persists_upstream_fields(tmp_path):
    h = _make_handler(tmp_path)

    packet_id = h.store_packet(
        {
            "timestamp": 200.0,
            "type": 1,
            "route": 1,
            "length": 9,
            "transmitted": False,
            "packet_hash": "pkt-upstream",
            "upstream_hash": "AB",
            "upstream_hash_size": 2,
            "original_path": ["CD", "AB"],
        }
    )

    row = h.get_packet_by_id(int(packet_id))
    assert row is not None
    assert row["upstream_hash"] == "AB"
    assert row["upstream_hash_size"] == 2


def test_neighbor_link_history_uses_packets_table_and_filters_hash_and_size(tmp_path):
    h = _make_handler(tmp_path)
    base_ts = 1_700_000_000.0

    h.store_packet(
        {
            "timestamp": base_ts,
            "type": 4,
            "route": 1,
            "length": 10,
            "rssi": -80,
            "snr": 3.5,
            "score": 0.4,
            "is_duplicate": False,
            "packet_hash": "match-1",
            "upstream_hash": "AA",
            "upstream_hash_size": 1,
            "original_path": ["10", "AA"],
        }
    )
    h.store_packet(
        {
            "timestamp": base_ts + 1.0,
            "type": 5,
            "route": 1,
            "length": 11,
            "rssi": -82,
            "snr": 2.5,
            "score": 0.35,
            "is_duplicate": True,
            "packet_hash": "match-2",
            "upstream_hash": "AA",
            "upstream_hash_size": 1,
            "original_path": ["20", "30", "AA"],
        }
    )
    # Same hash but different width: must not be merged.
    h.store_packet(
        {
            "timestamp": base_ts + 2.0,
            "type": 6,
            "route": 1,
            "length": 12,
            "packet_hash": "wrong-width",
            "upstream_hash": "AA",
            "upstream_hash_size": 2,
            "original_path": ["00AA"],
        }
    )
    # Different hash: must be filtered out.
    h.store_packet(
        {
            "timestamp": base_ts + 3.0,
            "type": 7,
            "route": 1,
            "length": 13,
            "packet_hash": "wrong-hash",
            "upstream_hash": "BB",
            "upstream_hash_size": 1,
            "original_path": ["BB"],
        }
    )

    rows = h.get_neighbor_link_history(peer_hash="aa", path_hash_size=1, hours=50000, limit=100)

    assert [row["packet_hash"] for row in rows] == ["match-1", "match-2"]
    assert rows[0]["path_hop_count"] == 2
    assert rows[1]["path_hop_count"] == 3
    assert rows[1]["is_duplicate"] is True


def _store_link_observation(h, ts, score, rssi, snr, duplicate=False, hash_="AA"):
    h.store_packet(
        {
            "timestamp": ts,
            "type": 4,
            "route": 1,
            "length": 10,
            "rssi": rssi,
            "snr": snr,
            "score": score,
            "is_duplicate": duplicate,
            "packet_hash": f"pkt-{ts}",
            "upstream_hash": hash_,
            "upstream_hash_size": 1,
            "original_path": [hash_],
        }
    )


def test_neighbor_link_history_buckets_summarise_the_rows(tmp_path):
    h = _make_handler(tmp_path)
    t0 = 1_700_000_400.0  # a 600 s boundary
    _store_link_observation(h, t0 + 5, 0.2, -100, -2.0)
    _store_link_observation(h, t0 + 300, 0.6, -80, 4.0, duplicate=True)
    _store_link_observation(h, t0 + 599, 0.4, -90, 1.0)
    _store_link_observation(h, t0 + 1300, 0.9, -70, 8.0)
    _store_link_observation(h, t0 + 10, 0.0, -120, -9.0, hash_="BB")

    def history(**kwargs):
        return h.get_neighbor_link_history(peer_hash="AA", path_hash_size=1, hours=50000, **kwargs)

    first, second = history(bucket_seconds=600)
    assert (first["timestamp"], first["last_ts"], first["n"], first["dup"]) == (t0, t0 + 599, 3, 1)
    assert (first["score"], first["rssi"], first["snr"]) == (0.4, -90.0, 1.0)
    assert (second["timestamp"], second["n"], second["score"]) == (t0 + 1200, 1, 0.9)
    assert [b["timestamp"] for b in history(bucket_seconds=600, limit=1)] == [t0 + 1200]
    assert len(history()) == 4


def test_recent_packet_queries_include_ids_and_preserve_duplicate_hash_rows(tmp_path):
    h = _make_handler(tmp_path)

    first_id = h.store_packet(
        {
            "timestamp": 100.0,
            "type": 1,
            "route": 1,
            "length": 8,
            "transmitted": False,
            "is_duplicate": False,
            "packet_hash": "same-hash",
        }
    )
    second_id = h.store_packet(
        {
            "timestamp": 101.0,
            "type": 1,
            "route": 1,
            "length": 8,
            "transmitted": True,
            "is_duplicate": True,
            "packet_hash": "same-hash",
        }
    )

    recent = h.get_recent_packets(limit=10)
    assert len(recent) == 2
    assert {packet["id"] for packet in recent} == {first_id, second_id}
    assert [packet["packet_hash"] for packet in recent] == ["same-hash", "same-hash"]

    filtered = h.get_filtered_packets(limit=10)
    assert len(filtered) == 2
    assert {packet["id"] for packet in filtered} == {first_id, second_id}

    by_id = h.get_packet_by_id(int(second_id))
    assert by_id is not None
    assert by_id["id"] == second_id
    assert by_id["packet_hash"] == "same-hash"


def test_verify_api_token_last_used_throttle(tmp_path, monkeypatch):
    h = _make_handler(tmp_path)
    h._api_token_last_used_interval_sec = 300

    now = {"v": 1000.0}

    monkeypatch.setattr("repeater.data_acquisition.sqlite_handler.time.time", lambda: now["v"])

    token_id = h.create_api_token("svc-throttle", "hash-throttle")
    assert token_id > 0

    assert h.verify_api_token("hash-throttle") is not None
    with h._connect() as conn:
        first = conn.execute(
            "SELECT last_used FROM api_tokens WHERE id = ?", (token_id,)
        ).fetchone()[0]
    assert first == 1000.0

    now["v"] = 1010.0
    assert h.verify_api_token("hash-throttle") is not None
    with h._connect() as conn:
        second = conn.execute(
            "SELECT last_used FROM api_tokens WHERE id = ?", (token_id,)
        ).fetchone()[0]
    assert second == 1000.0

    now["v"] = 1401.0
    assert h.verify_api_token("hash-throttle") is not None
    with h._connect() as conn:
        third = conn.execute(
            "SELECT last_used FROM api_tokens WHERE id = ?", (token_id,)
        ).fetchone()[0]
    assert third == 1401.0


def test_store_advert_zero_hop_signal_handling(tmp_path):
    h = _make_handler(tmp_path)

    h.store_advert(
        {
            "timestamp": 10.0,
            "pubkey": "pk-z",
            "node_name": "node-z",
            "is_repeater": False,
            "route_type": 1,
            "contact_type": "neighbor",
            "rssi": -80,
            "snr": 5.0,
            "is_new_neighbor": True,
            "zero_hop": True,
        }
    )

    # Multi-hop update must preserve previous zero-hop signal quality.
    h.store_advert(
        {
            "timestamp": 20.0,
            "pubkey": "pk-z",
            "node_name": "node-z-2",
            "is_repeater": False,
            "route_type": 2,
            "contact_type": "neighbor",
            "rssi": -50,
            "snr": 9.0,
            "is_new_neighbor": False,
            "zero_hop": False,
        }
    )
    with h._connect() as conn:
        row = conn.execute(
            "SELECT rssi, snr, zero_hop, advert_count FROM adverts WHERE pubkey = ?",
            ("pk-z",),
        ).fetchone()
    assert row is not None
    assert row[0] == -80
    assert row[1] == 5.0
    assert bool(row[2]) is True
    assert row[3] == 2

    # New zero-hop update should refresh signal quality.
    h.store_advert(
        {
            "timestamp": 30.0,
            "pubkey": "pk-z",
            "node_name": "node-z-3",
            "is_repeater": False,
            "route_type": 1,
            "contact_type": "neighbor",
            "rssi": -60,
            "snr": 6.5,
            "is_new_neighbor": False,
            "zero_hop": True,
        }
    )
    with h._connect() as conn:
        row2 = conn.execute(
            "SELECT rssi, snr, zero_hop, advert_count FROM adverts WHERE pubkey = ?",
            ("pk-z",),
        ).fetchone()
    assert row2 is not None
    assert row2[0] == -60
    assert row2[1] == 6.5
    assert bool(row2[2]) is True
    assert row2[3] == 3


def test_sync_transport_keys_validation_and_tree_apply(tmp_path, monkeypatch):
    h = _make_handler(tmp_path)

    with pytest.raises(ValueError, match="must be a list"):
        h.sync_transport_keys({"bad": True})

    with pytest.raises(ValueError, match="Duplicate node_id"):
        h.sync_transport_keys(
            [
                {"node_id": "1", "name": "a", "flood_policy": "allow"},
                {"node_id": "1", "name": "b", "flood_policy": "deny"},
            ]
        )


def test_sync_transport_keys_parent_and_tree_apply(tmp_path, monkeypatch):
    h = _make_handler(tmp_path)

    with pytest.raises(ValueError, match="Parent node 'missing'"):
        h.sync_transport_keys(
            [
                {
                    "node_id": "c1",
                    "name": "child",
                    "flood_policy": "allow",
                    "parent_node_id": "missing",
                }
            ]
        )

    monkeypatch.setattr(h, "generate_transport_key", lambda _name: "GEN-KEY")
    applied = h.sync_transport_keys(
        [
            {
                "node_id": "root",
                "name": "root-name",
                "flood_policy": "allow",
                "transport_key": "ROOT-KEY",
            },
            {
                "node_id": "child",
                "name": "child-name",
                "flood_policy": "deny",
                "parent_node_id": "root",
                "transport_key": None,
            },
        ]
    )

    assert applied == {"applied_nodes": 2, "generated_keys": 1}

    with h._connect() as conn:
        rows = conn.execute(
            "SELECT id, name, flood_policy, transport_key, parent_id FROM transport_keys ORDER BY id"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][1] == "root-name"
    assert rows[0][3] == "ROOT-KEY"
    assert rows[1][1] == "child-name"
    assert rows[1][2] == "deny"
    assert rows[1][3] == "GEN-KEY"
    assert rows[1][4] == rows[0][0]


def test_get_lbt_diagnostics_aggregates_retry_distribution_and_summary(tmp_path):
    h = _make_handler(tmp_path)

    packets = [
        {
            "timestamp": 10.0,
            "type": 1,
            "route": 1,
            "length": 8,
            "transmitted": True,
            "packet_hash": "lbt-1",
            "lbt_attempts": 0,
            "lbt_channel_busy": False,
        },
        {
            "timestamp": 20.0,
            "type": 1,
            "route": 1,
            "length": 8,
            "transmitted": True,
            "packet_hash": "lbt-2",
            "lbt_attempts": 1,
            "lbt_channel_busy": True,
        },
        {
            "timestamp": 30.0,
            "type": 1,
            "route": 1,
            "length": 8,
            "transmitted": True,
            "packet_hash": "lbt-3",
            "lbt_attempts": 2,
            "lbt_channel_busy": True,
        },
        {
            "timestamp": 40.0,
            "type": 1,
            "route": 1,
            "length": 8,
            "transmitted": False,
            "drop_reason": "TX failed",
            "packet_hash": "lbt-4",
            "lbt_attempts": 4,
            "lbt_channel_busy": True,
        },
        {
            # Excluded from TX-path diagnostics by filter.
            "timestamp": 50.0,
            "type": 1,
            "route": 1,
            "length": 8,
            "transmitted": False,
            "drop_reason": "Duplicate",
            "packet_hash": "lbt-excluded",
            "lbt_attempts": 0,
            "lbt_channel_busy": False,
        },
        {
            "timestamp": 70.0,
            "type": 1,
            "route": 1,
            "length": 8,
            "transmitted": True,
            "packet_hash": "lbt-5",
            "lbt_attempts": 0,
            "lbt_channel_busy": False,
        },
    ]

    for record in packets:
        h.store_packet(record)

    out = h.get_lbt_diagnostics(
        start_timestamp=0,
        end_timestamp=180,
        bucket_seconds=60,
        severe_attempt_threshold=4,
    )

    summary = out["summary"]
    assert summary["total_transmissions"] == 5
    assert summary["total_attempts"] == 12
    assert summary["first_attempt_success"] == 2
    assert summary["retry_packets"] == 3
    assert summary["retry_rate_pct"] == pytest.approx(60.0)
    assert summary["avg_attempts"] == pytest.approx(2.4)
    assert summary["max_attempts"] == 5
    assert summary["median_attempts"] == pytest.approx(2.0)
    assert summary["p95_attempts"] == pytest.approx(5.0)
    assert summary["attempts_1"] == 2
    assert summary["attempts_2"] == 1
    assert summary["attempts_3"] == 1
    assert summary["attempts_4_plus"] == 1
    assert summary["attempts_3_plus"] == 2
    assert summary["failed_transmissions"] == 1
    assert summary["busy_channel_events"] == 3
    assert summary["severe_contention_count"] == 1
    assert summary["has_lbt_data"] is True
    assert summary["worst_bucket"] is not None
    assert summary["worst_bucket"]["timestamp"] == 0

    buckets = {int(b["timestamp"]): b for b in out["buckets"]}
    first = buckets[0]
    assert first["transmissions"] == 4
    assert first["retry_packets"] == 3
    assert first["retry_rate_pct"] == pytest.approx(75.0)
    assert first["first_attempt_success_rate_pct"] == pytest.approx(25.0)
    assert first["attempts_4_plus"] == 1
    assert first["severe_contention_count"] == 1
    assert first["failed_transmissions"] == 1

    second = buckets[60]
    assert second["transmissions"] == 1
    assert second["retry_packets"] == 0
    assert second["retry_rate_pct"] == pytest.approx(0.0)
    assert second["avg_attempts"] == pytest.approx(1.0)

    packet_types = out["packet_types"]
    assert len(packet_types) == 1
    assert packet_types[0]["packet_type"] == 1
    assert packet_types[0]["transmissions"] == 5
    assert packet_types[0]["retry_packets"] == 3

    packet_type_buckets = out["packet_type_buckets"]
    assert len(packet_type_buckets) == 2
    first_type_bucket = packet_type_buckets[0]
    assert first_type_bucket["packet_type"] == 1
    assert first_type_bucket["timestamp"] == 0
    assert first_type_bucket["retry_rate_pct"] == pytest.approx(75.0)
    assert first_type_bucket["attempts_3_plus_pct"] == pytest.approx(50.0)


def test_get_lbt_diagnostics_empty_range_preserves_no_data_distinction(tmp_path):
    h = _make_handler(tmp_path)

    out = h.get_lbt_diagnostics(
        start_timestamp=0,
        end_timestamp=180,
        bucket_seconds=60,
        severe_attempt_threshold=4,
    )

    summary = out["summary"]
    assert summary["total_transmissions"] == 0
    assert summary["retry_rate_pct"] is None
    assert summary["first_attempt_success_rate_pct"] is None
    assert summary["avg_attempts"] is None
    assert summary["has_lbt_data"] is False

    assert len(out["buckets"]) >= 3
    for bucket in out["buckets"]:
        assert bucket["transmissions"] == 0
        assert bucket["retry_rate_pct"] is None
        assert bucket["first_attempt_success_rate_pct"] is None
        assert bucket["avg_attempts"] is None
    assert out["packet_types"] == []
    assert out["packet_type_buckets"] == []


def test_cleanup_old_data_accepts_companion_events_days(tmp_path):
    """engine.py forwards companion_events_days; accepting it must not break
    packet retention cleanup (the pre-fix TypeError was swallowed upstream).
    """
    import time

    h = _make_handler(tmp_path)
    old_ts = time.time() - (40 * 86400)
    h.store_packet(
        {
            "timestamp": old_ts,
            "type": 1,
            "route": 2,
            "length": 3,
            "transmitted": False,
            "packet_hash": "pkt-old",
        }
    )

    h.cleanup_old_data(days=31, companion_events_days=14)

    with h._connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
    assert count == 0


def test_get_metrics_data_returns_aligned_buckets_and_equal_length_arrays(tmp_path):
    h = _make_handler(tmp_path)

    packets = [
        {
            "timestamp": 61,
            "type": 0,
            "route": 1,
            "length": 10,
            "rssi": -80,
            "snr": 5.0,
            "score": 0.2,
            "transmitted": False,
            "packet_hash": "metrics-1",
        },
        {
            "timestamp": 62,
            "type": 19,
            "route": 1,
            "length": 20,
            "rssi": -90,
            "snr": 3.0,
            "score": 0.4,
            "transmitted": True,
            "packet_hash": "metrics-2",
        },
        {
            "timestamp": 121,
            "type": 5,
            "route": 1,
            "length": 30,
            "rssi": -70,
            "snr": 10.0,
            "score": 0.8,
            "transmitted": True,
            "packet_hash": "metrics-3",
        },
    ]
    for record in packets:
        h.store_packet(record)

    out = h.get_metrics_data(start_time=61, end_time=239, resolution="average")

    assert out["data_source"] == "sqlite"
    assert out["counter_mode"] == "bucket_count"
    assert out["start_time"] == 60
    assert out["end_time"] == 180
    assert out["step"] == 60
    assert out["timestamps"] == [60, 120, 180]

    expected_length = len(out["timestamps"])
    for values in out["metrics"].values():
        assert len(values) == expected_length
    for values in out["packet_types"].values():
        assert len(values) == expected_length

    assert out["metrics"]["rx_count"] == [2, 1, 0]
    assert out["metrics"]["tx_count"] == [1, 1, 0]
    assert out["metrics"]["drop_count"] == [1, 0, 0]
    assert out["metrics"]["avg_length"] == [15.0, 30.0, None]
    assert out["metrics"]["neighbor_count"] == [None, None, None]

    assert out["packet_types"]["type_0"] == [1, 0, 0]
    assert out["packet_types"]["type_5"] == [0, 1, 0]
    assert out["packet_types"]["type_other"] == [1, 0, 0]
    assert out["packet_types"]["type_15"] == [0, 0, 0]


def test_get_metrics_data_empty_buckets_use_zero_counters_and_null_gauges(tmp_path):
    h = _make_handler(tmp_path)

    out = h.get_metrics_data(start_time=0, end_time=120, resolution="average")

    assert out["timestamps"] == [0, 60, 120]
    assert out["metrics"]["rx_count"] == [0, 0, 0]
    assert out["metrics"]["tx_count"] == [0, 0, 0]
    assert out["metrics"]["drop_count"] == [0, 0, 0]
    assert out["metrics"]["avg_rssi"] == [None, None, None]
    assert out["metrics"]["avg_snr"] == [None, None, None]
    assert out["metrics"]["avg_length"] == [None, None, None]
    assert out["metrics"]["avg_score"] == [None, None, None]
    assert out["packet_types"]["type_0"] == [0, 0, 0]
    assert out["packet_types"]["type_other"] == [0, 0, 0]


@pytest.mark.parametrize(
    ("resolution", "expected_rssi", "expected_snr", "expected_length", "expected_score"),
    [
        ("average", -75.0, 4.0, 15.0, 0.5),
        ("max", -70, 6.0, 20, 0.8),
        ("min", -80, 2.0, 10, 0.2),
    ],
)
def test_get_metrics_data_applies_requested_gauge_aggregation(
    tmp_path,
    resolution,
    expected_rssi,
    expected_snr,
    expected_length,
    expected_score,
):
    h = _make_handler(tmp_path)

    h.store_packet(
        {
            "timestamp": 61,
            "type": 1,
            "route": 1,
            "length": 10,
            "rssi": -80,
            "snr": 2.0,
            "score": 0.2,
            "transmitted": False,
            "packet_hash": f"agg-{resolution}-1",
        }
    )
    h.store_packet(
        {
            "timestamp": 62,
            "type": 1,
            "route": 1,
            "length": 20,
            "rssi": -70,
            "snr": 6.0,
            "score": 0.8,
            "transmitted": True,
            "packet_hash": f"agg-{resolution}-2",
        }
    )

    out = h.get_metrics_data(start_time=60, end_time=119, resolution=resolution)

    assert out["metrics"]["avg_rssi"] == [expected_rssi]
    assert out["metrics"]["avg_snr"] == [expected_snr]
    assert out["metrics"]["avg_length"] == [expected_length]
    assert out["metrics"]["avg_score"] == [expected_score]
