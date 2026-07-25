"""SQLite regression tests for raw MeshCore ADVERT contact exchange."""

import asyncio
import sqlite3
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openhop_core.companion.models import Contact
from openhop_core.protocol import LocalIdentity
from repeater.companion.bridge import RepeaterCompanionBridge
from repeater.companion.frame_server import CompanionFrameServer
from repeater.companion.journal import CompanionEventJournal
from repeater.data_acquisition.sqlite_handler import SQLiteHandler


# The same literal MeshCore Packet::writeTo ADVERT fixture exercised by Core.
# It deliberately includes header/path bytes and a signature, so BLOB handling
# is tested with the real protocol shape instead of a synthetic text payload.
MESHCORE_ADVERT_WIRE = bytes.fromhex(
    "1100d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    "7856341200844b4710c3083898f85b990be03edd18c9c4d05b82590915f72d3f04"
    "ac2eefc5446a5379bf25fbbbc7fcab87570e7552ed7d30def3291ea495be646cb1"
    "ef078146776d566563"
)
MESHCORE_ADVERT_PUBKEY = bytes.fromhex(
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
)
COMPANION_HASH = "0x42"


async def _drain_loopback() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


def _contact(raw_advert=MESHCORE_ADVERT_WIRE) -> dict:
    return {
        "pubkey": MESHCORE_ADVERT_PUBKEY,
        "name": "FwmVec",
        "adv_type": 1,
        "flags": 0,
        "out_path_len": -1,
        "out_path": b"",
        "last_advert_timestamp": 0x12345678,
        "last_advert_packet": raw_advert,
        "lastmod": 123,
        "gps_lat": 0.0,
        "gps_lon": 0.0,
        "sync_since": 0,
    }


def _bridge(sqlite_handler, journal=None) -> RepeaterCompanionBridge:
    return RepeaterCompanionBridge(
        LocalIdentity(),
        AsyncMock(return_value=True),
        sqlite_handler=sqlite_handler,
        companion_hash=COMPANION_HASH,
        journal=journal,
    )


@pytest.mark.asyncio
async def test_message_before_first_client_connection_survives_restart(tmp_path):
    handler = SQLiteHandler(tmp_path)
    bridge = _bridge(handler)
    server = CompanionFrameServer(bridge, COMPANION_HASH, port=0, sqlite_handler=handler)
    with patch("repeater.companion.frame_server._BaseFrameServer.start", AsyncMock()):
        await server.start()
    assert server._client_writer is None
    await bridge._handle_new_message(
        {
            "contact_pubkey": "01" * 32,
            "message_text": "before-first-connect",
            "timestamp": 123,
            "txt_type": 0,
        }
    )
    assert bridge.message_queue.is_empty()

    restarted_handler = SQLiteHandler(tmp_path)
    message = restarted_handler.companion_pop_message(COMPANION_HASH)
    assert message is not None
    assert message["text"] == "before-first-connect"
    assert message["timestamp"] == 123


@pytest.mark.asyncio
async def test_rest_only_contacts_and_evictions_are_durable(tmp_path):
    handler = SQLiteHandler(tmp_path)
    journal = CompanionEventJournal(handler, COMPANION_HASH)
    bridge = _bridge(handler, journal)

    assert bridge.import_contact(MESHCORE_ADVERT_WIRE) is True
    rows = []
    for _ in range(100):
        rows = handler.companion_load_contacts(COMPANION_HASH) or []
        if rows:
            break
        await asyncio.sleep(0.01)
    assert rows[0]["last_advert_packet"] == MESHCORE_ADVERT_WIRE

    contact = bridge.get_contact_by_key(MESHCORE_ADVERT_PUBKEY)
    await bridge._fire_callbacks("contact_deleted", contact)

    assert handler.companion_load_contacts(COMPANION_HASH) == []
    events = [
        event["payload"]["change"]
        for event in handler.companion_get_events(COMPANION_HASH, 0)
        if event["event_type"] == "contact"
    ]
    assert events == ["new", "remove"]


@pytest.mark.asyncio
async def test_contact_delete_failure_keeps_state_and_suppresses_transient_callback(
    tmp_path,
    monkeypatch,
):
    handler = SQLiteHandler(tmp_path)
    journal = CompanionEventJournal(handler, COMPANION_HASH)
    bridge = _bridge(handler, journal)
    callback = AsyncMock()
    bridge.on_contact_deleted(callback)

    assert bridge.import_contact(MESHCORE_ADVERT_WIRE) is True
    rows = []
    for _ in range(100):
        rows = handler.companion_load_contacts(COMPANION_HASH) or []
        if rows:
            break
        await asyncio.sleep(0.01)
    assert len(rows) == 1

    remove_contact = MagicMock(side_effect=RuntimeError("storage unavailable"))
    monkeypatch.setattr(journal, "remove_contact", remove_contact)
    contact = bridge.get_contact_by_key(MESHCORE_ADVERT_PUBKEY)

    await bridge._fire_callbacks("contact_deleted", contact)

    remove_contact.assert_called_once_with(MESHCORE_ADVERT_PUBKEY)
    callback.assert_not_awaited()
    assert len(handler.companion_load_contacts(COMPANION_HASH)) == 1
    assert [
        event["payload"]["change"]
        for event in handler.companion_get_events(COMPANION_HASH, 0)
        if event["event_type"] == "contact"
    ] == ["new"]


@pytest.mark.asyncio
async def test_import_rolls_back_memory_and_push_when_contact_commit_fails(
    tmp_path, monkeypatch
):
    handler = SQLiteHandler(tmp_path)
    journal = CompanionEventJournal(handler, COMPANION_HASH)
    bridge = _bridge(handler, journal)
    server = CompanionFrameServer(
        bridge,
        COMPANION_HASH,
        port=0,
        sqlite_handler=handler,
        journal=journal,
    )
    with patch("repeater.companion.frame_server._BaseFrameServer.start", AsyncMock()):
        await server.start()
    bridge.on_node_discovered(server._on_node_discovered)
    server._enqueue_frame = MagicMock()
    attempted = threading.Event()

    def fail_commit(companion_hash, changes):
        assert companion_hash == COMPANION_HASH
        assert changes
        attempted.set()
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(handler, "companion_apply_contact_changes", fail_commit)

    assert bridge.import_contact(MESHCORE_ADVERT_WIRE) is True
    for _ in range(100):
        if attempted.is_set():
            break
        await asyncio.sleep(0.01)
    assert attempted.is_set()
    async with bridge.state_mutation_lock:
        pass

    assert bridge.get_contact_by_key(MESHCORE_ADVERT_PUBKEY) is None
    assert handler.companion_load_contacts(COMPANION_HASH) in (None, [])
    assert handler.companion_get_events(COMPANION_HASH, 0) == []
    server._enqueue_frame.assert_not_called()


@pytest.mark.asyncio
async def test_non_contact_packets_do_not_wait_for_contact_commit_lock(tmp_path):
    bridge = _bridge(SQLiteHandler(tmp_path))
    packet = MagicMock()
    packet.get_payload_type.return_value = 0x02

    with patch(
        "repeater.companion.bridge.CompanionBridge.process_received_packet",
        AsyncMock(return_value="processed"),
    ):
        async with bridge.state_mutation_lock:
            result = await asyncio.wait_for(
                bridge.process_received_packet(packet),
                timeout=0.1,
            )

    assert result == "processed"


def test_bulk_save_load_preserves_raw_advert_blob_and_upsert_replaces_it(tmp_path):
    handler = SQLiteHandler(tmp_path)
    assert handler.companion_save_contacts(COMPANION_HASH, [_contact()])
    assert (
        handler.companion_load_contacts(COMPANION_HASH)[0]["last_advert_packet"]
        == MESHCORE_ADVERT_WIRE
    )

    updated = MESHCORE_ADVERT_WIRE[:-1] + b"!"
    assert handler.companion_upsert_contact(COMPANION_HASH, _contact(updated))
    assert handler.companion_load_contacts(COMPANION_HASH)[0]["last_advert_packet"] == updated


@pytest.mark.asyncio
async def test_import_persist_restart_and_export_are_byte_exact(tmp_path):
    handler = SQLiteHandler(tmp_path)
    first = _bridge(handler)

    assert first.import_contact(MESHCORE_ADVERT_WIRE) is True
    await _drain_loopback()
    contact = first.get_contact_by_key(MESHCORE_ADVERT_PUBKEY)
    assert contact is not None

    # RepeaterFrameServer is the production persistence adapter used by the
    # contact-updated callback.  Persist through it rather than writing a row
    # directly, then rebuild state exactly as the daemon restart path does.
    server = CompanionFrameServer(first, COMPANION_HASH, port=0, sqlite_handler=handler)
    await server._persist_contact(contact)

    restarted_handler = SQLiteHandler(tmp_path)
    rows = restarted_handler.companion_load_contacts(COMPANION_HASH)
    assert rows is not None and len(rows) == 1
    assert rows[0]["last_advert_packet"] == MESHCORE_ADVERT_WIRE

    restarted = _bridge(restarted_handler)
    records = []
    for row in rows:
        record = dict(row)
        record["public_key"] = record.pop("pubkey")
        records.append(record)
    restarted.contacts.load_from_dicts(records)

    assert restarted.export_contact(MESHCORE_ADVERT_PUBKEY) == MESHCORE_ADVERT_WIRE


def test_migration_adds_nullable_advert_blob_without_losing_existing_contacts(tmp_path):
    handler = SQLiteHandler(tmp_path)
    conn = sqlite3.connect(str(handler.sqlite_path))
    conn.execute(
        "DELETE FROM migrations "
        "WHERE migration_name = 'add_last_advert_packet_to_companion_contacts'"
    )
    conn.execute("ALTER TABLE companion_contacts RENAME TO companion_contacts_old")
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
        "INSERT INTO companion_contacts "
        "(companion_hash, pubkey, name, updated_at) VALUES (?, ?, ?, ?)",
        (COMPANION_HASH, MESHCORE_ADVERT_PUBKEY, "old-contact", 1.0),
    )
    conn.execute("DROP TABLE companion_contacts_old")
    conn.commit()
    conn.close()

    migrated = SQLiteHandler(tmp_path)
    columns = {
        row[1]
        for row in sqlite3.connect(str(migrated.sqlite_path))
        .execute("PRAGMA table_info(companion_contacts)")
        .fetchall()
    }
    assert "last_advert_packet" in columns
    rows = migrated.companion_load_contacts(COMPANION_HASH)
    assert rows is not None and rows[0]["name"] == "old-contact"
    assert rows[0]["last_advert_packet"] is None


def test_persistence_adapter_keeps_raw_advert_bytes():
    contact = Contact(
        public_key=MESHCORE_ADVERT_PUBKEY,
        name="FwmVec",
        last_advert_packet=MESHCORE_ADVERT_WIRE,
    )
    record = CompanionFrameServer._contact_to_dict(contact)
    assert record["last_advert_packet"] == MESHCORE_ADVERT_WIRE
