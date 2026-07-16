"""SQLite round-trip of the queued-message signal and channel-data fields.

Companion offline-queue messages are persisted as structured fields and the
response frame is rebuilt on replay (SYNC_NEXT_MESSAGE). The parity contract is
that the frame rebuilt from a SQLite round-tripped message is byte-identical to
the frame built from the original in-memory ``QueuedMessage`` — so snr, rssi,
channel_data_type and channel_data_payload must all survive persistence.
"""

from __future__ import annotations

import sqlite3

import pytest
from openhop_core.companion.models import QueuedMessage
from openhop_core.protocol.constants import TXT_TYPE_PLAIN, TXT_TYPE_SIGNED_PLAIN

from repeater.companion.frame_server import CompanionFrameServer
from repeater.data_acquisition.sqlite_handler import SQLiteHandler

_HASH = "0x01"


def _handler(tmp_path):
    return SQLiteHandler(tmp_path)


def _frame_server(handler, app_target_ver=3):
    """A CompanionFrameServer wired to ``handler`` for frame build + replay.

    ``_build_message_frame`` only reads ``_app_target_ver``; V3 is used so the
    SNR byte is emitted in the direct and channel-text frames (the pre-V3
    variants omit it).
    """
    fs = CompanionFrameServer.__new__(CompanionFrameServer)
    fs._app_target_ver = app_target_ver
    fs.sqlite_handler = handler
    fs.companion_hash = _HASH
    return fs


def _qm_from_dict(d: dict) -> QueuedMessage:
    """Build the original in-memory QueuedMessage the bridge would enqueue."""
    prefix = d.get("sender_prefix") or b""
    return QueuedMessage(
        sender_key=d.get("sender_key", b""),
        txt_type=d.get("txt_type", 0),
        timestamp=d.get("timestamp", 0),
        text=d.get("text", ""),
        is_channel=bool(d.get("is_channel", False)),
        channel_idx=d.get("channel_idx", 0),
        path_len=d.get("path_len", 0),
        snr=float(d.get("snr") or 0.0),
        rssi=int(d.get("rssi") or 0),
        channel_data_type=int(d.get("channel_data_type") or 0),
        channel_data_payload=bytes(d.get("channel_data_payload") or b""),
        sender_prefix=bytes(prefix),
    )


# Nonzero snr (negative fractional exercises the int8 wrap) and nonzero rssi.
_DIRECT_PLAIN = {
    "sender_key": b"\x11" * 32,
    "text": "hello world",
    "timestamp": 1000,
    "txt_type": TXT_TYPE_PLAIN,
    "is_channel": False,
    "channel_idx": 0,
    "path_len": 0xFF,
    "packet_hash": "direct-plain",
    "snr": -6.25,
    "rssi": -80,
    "sender_prefix": b"",
}
_DIRECT_SIGNED = {
    "sender_key": b"\x22" * 32,
    "text": "signed room post",
    "timestamp": 2000,
    "txt_type": TXT_TYPE_SIGNED_PLAIN,
    "is_channel": False,
    "channel_idx": 0,
    "path_len": 3,
    "packet_hash": "direct-signed",
    "snr": 7.5,
    "rssi": -60,
    "sender_prefix": b"\xaa\xbb\xcc\xdd",
}
_CHANNEL_TEXT = {
    "sender_key": b"",
    "text": "channel chatter",
    "timestamp": 3000,
    "txt_type": 0,
    "is_channel": True,
    "channel_idx": 2,
    "path_len": 5,
    "packet_hash": "channel-text",
    "snr": -3.75,
    "rssi": -90,
}
_CHANNEL_DATA = {
    "sender_key": b"",
    "text": "",
    "timestamp": 0,
    "txt_type": 0,
    "is_channel": True,
    "channel_idx": 1,
    "path_len": 4,
    "packet_hash": "channel-data",
    "snr": -6.25,
    "rssi": -70,
    "channel_data_type": 0x0102,
    "channel_data_payload": b"\x01\x02\x03\xff\x10",
}


@pytest.mark.parametrize(
    "msg_dict",
    [_DIRECT_PLAIN, _DIRECT_SIGNED, _CHANNEL_TEXT, _CHANNEL_DATA],
    ids=["direct_plain", "direct_signed", "channel_text", "channel_data"],
)
def test_frame_round_trip_is_byte_identical(tmp_path, msg_dict):
    h = _handler(tmp_path)
    fs = _frame_server(h)

    original_frame = fs._build_message_frame(_qm_from_dict(msg_dict))

    assert h.companion_push_message(_HASH, dict(msg_dict))
    rebuilt = fs._sync_next_from_persistence()
    assert rebuilt is not None
    rebuilt_frame = fs._build_message_frame(rebuilt)

    assert rebuilt_frame == original_frame


def test_channel_data_survives_as_binary_frame_not_empty_text(tmp_path):
    # Regression: a binary channel-data message must not collapse into an empty
    # channel-text frame after the SQLite round-trip.
    h = _handler(tmp_path)
    fs = _frame_server(h)

    assert h.companion_push_message(_HASH, dict(_CHANNEL_DATA))
    rebuilt = fs._sync_next_from_persistence()

    assert rebuilt.channel_data_type == 0x0102
    assert rebuilt.channel_data_payload == b"\x01\x02\x03\xff\x10"
    assert rebuilt.snr == pytest.approx(-6.25)
    assert rebuilt.rssi == -70


def test_restart_durability_reconstructs_all_fields(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_push_message(_HASH, dict(_DIRECT_SIGNED))
    assert h.companion_push_message(_HASH, dict(_CHANNEL_DATA))

    # A daemon restart: drop the handler and reopen the same database file.
    del h
    h2 = _handler(tmp_path)

    signed = h2.companion_pop_message(_HASH)
    assert signed["sender_prefix"] == b"\xaa\xbb\xcc\xdd"
    assert signed["snr"] == pytest.approx(7.5)
    assert signed["rssi"] == -60
    assert signed["channel_data_type"] == 0
    assert signed["channel_data_payload"] == b""

    data = h2.companion_pop_message(_HASH)
    assert data["channel_data_type"] == 0x0102
    assert data["channel_data_payload"] == b"\x01\x02\x03\xff\x10"
    assert data["snr"] == pytest.approx(-6.25)
    assert data["rssi"] == -70


def test_load_messages_returns_new_fields_with_types(tmp_path):
    h = _handler(tmp_path)
    assert h.companion_push_message(_HASH, dict(_CHANNEL_DATA))

    msgs = h.companion_load_messages(_HASH)
    assert len(msgs) == 1
    msg = msgs[0]
    assert isinstance(msg["snr"], float)
    assert isinstance(msg["rssi"], int)
    assert isinstance(msg["channel_data_type"], int)
    assert isinstance(msg["channel_data_payload"], bytes)
    assert msg["snr"] == pytest.approx(-6.25)
    assert msg["rssi"] == -70
    assert msg["channel_data_type"] == 0x0102
    assert msg["channel_data_payload"] == b"\x01\x02\x03\xff\x10"


def test_legacy_schema_migrates_and_defaults(tmp_path):
    # Rewind companion_messages to the pre-change schema (sender_prefix present,
    # but no snr/rssi/channel_data_type/channel_data_payload) and drop the
    # migration marker, then re-run migrations against a pre-existing row.
    h = _handler(tmp_path)
    conn = sqlite3.connect(str(h.sqlite_path))
    conn.execute(
        "DELETE FROM migrations "
        "WHERE migration_name = 'add_signal_and_channel_data_to_companion_messages'"
    )
    conn.execute("ALTER TABLE companion_messages RENAME TO companion_messages_old")
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
            packet_hash TEXT,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute("DROP TABLE companion_messages_old")
    conn.execute(
        "INSERT INTO companion_messages "
        "(companion_hash, sender_key, text, created_at) VALUES ('0x01', X'01', 'old', 1.0)"
    )
    conn.commit()
    conn.close()

    h2 = _handler(tmp_path)  # re-runs migrations

    # (a) the four columns are added.
    conn = sqlite3.connect(str(h2.sqlite_path))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(companion_messages)")}
    conn.close()
    assert {"snr", "rssi", "channel_data_type", "channel_data_payload"} <= columns

    # (b) the old row pops with defaults and no exception.
    old = h2.companion_pop_message(_HASH)
    assert old["text"] == "old"
    assert old["snr"] == 0.0
    assert old["rssi"] == 0
    assert old["channel_data_type"] == 0
    assert old["channel_data_payload"] == b""

    # (c) new pushes round-trip fully through the migrated columns.
    assert h2.companion_push_message(_HASH, dict(_CHANNEL_DATA))
    data = h2.companion_pop_message(_HASH)
    assert data["channel_data_type"] == 0x0102
    assert data["channel_data_payload"] == b"\x01\x02\x03\xff\x10"
    assert data["snr"] == pytest.approx(-6.25)
    assert data["rssi"] == -70
