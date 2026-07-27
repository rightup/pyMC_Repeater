import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from repeater.handler_helpers.room_server import (
    MAX_UNSYNCED_POSTS,
    TXT_TYPE_PLAIN,
    TXT_TYPE_SIGNED_PLAIN,
    RoomServer,
)


class _FakeIdentity:
    def __init__(self, pubkey: bytes):
        self._pubkey = pubkey

    def get_public_key(self):
        return self._pubkey


class _FakeClient:
    def __init__(self, pubkey: bytes, shared_secret=b"s" * 32, out_path=b"", out_path_len=-1):
        self.id = _FakeIdentity(pubkey)
        self.shared_secret = shared_secret
        self.out_path = bytearray(out_path)
        self.out_path_len = out_path_len
        self.sync_since = 0


class _FakeACL:
    def __init__(self, clients=None):
        self._clients = list(clients or [])
        self.remove_client = MagicMock(return_value=True)

    def get_all_clients(self):
        return list(self._clients)


class _FakeDB:
    def __init__(self):
        self.insert_room_message = MagicMock(return_value=1)
        self.upsert_client_sync = MagicMock()
        self.get_client_sync = MagicMock(return_value=None)
        self.get_unsynced_count = MagicMock(return_value=0)
        self.get_all_room_clients = MagicMock(return_value=[])
        self.get_unsynced_messages = MagicMock(return_value=[])
        self.cleanup_old_messages = MagicMock(return_value=0)


def _make_room_server(db=None, acl=None, injector=None, max_posts=8):
    return RoomServer(
        room_hash=0x34,
        room_name="room-alpha",
        local_identity=_FakeIdentity(b"R" * 32),
        sqlite_handler=db or _FakeDB(),
        packet_injector=injector or AsyncMock(return_value=True),
        acl=acl or _FakeACL(),
        max_posts=max_posts,
    )


@pytest.mark.asyncio
async def test_room_server_add_post_stores_message_and_updates_sync_state():
    db = _FakeDB()
    rs = _make_room_server(db=db, acl=_FakeACL([_FakeClient(b"C" * 32)]))

    ok = await rs.add_post(
        client_pubkey=b"A" * 32,
        message_text="hello room",
        sender_timestamp=111,
        txt_type=TXT_TYPE_PLAIN,
    )

    assert ok is True
    db.insert_room_message.assert_called_once()
    db.upsert_client_sync.assert_called_once()
    kwargs = db.upsert_client_sync.call_args.kwargs
    assert kwargs["room_hash"] == "0x34"
    assert kwargs["client_pubkey"] == (b"A" * 32).hex()


@pytest.mark.asyncio
async def test_room_server_add_post_truncates_and_rate_limits_client():
    db = _FakeDB()
    rs = _make_room_server(db=db)
    client_key = b"B" * 32

    long_msg = "x" * 500
    first_ok = await rs.add_post(client_key, long_msg, sender_timestamp=5)
    assert first_ok is True
    args = db.insert_room_message.call_args.kwargs
    assert len(args["message_text"]) == 160

    # Force client to appear at post-per-minute limit.
    rs.client_post_times[client_key.hex()] = [time.time() - 1] * 10
    second_ok = await rs.add_post(client_key, "blocked", sender_timestamp=6)
    assert second_ok is False


@pytest.mark.asyncio
async def test_room_server_add_post_returns_false_on_db_insert_failure():
    db = _FakeDB()
    db.insert_room_message.return_value = 0
    rs = _make_room_server(db=db)

    ok = await rs.add_post(b"D" * 32, "msg", sender_timestamp=9)
    assert ok is False


def test_room_server_init_caps_max_posts_to_hard_limit():
    rs = _make_room_server(max_posts=MAX_UNSYNCED_POSTS + 50)
    assert rs.max_posts == MAX_UNSYNCED_POSTS


@pytest.mark.asyncio
async def test_room_server_push_post_to_client_success_direct_route_sets_path_and_ack():
    db = _FakeDB()
    db.get_client_sync.return_value = {"push_failures": 0}
    injector = AsyncMock(return_value=True)
    rs = _make_room_server(db=db, injector=injector)
    rs.global_limiter = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())
    rs._handle_ack_received = AsyncMock()

    client = _FakeClient(pubkey=b"E" * 32, out_path=b"\xaa\xbb", out_path_len=2)
    post = {
        "author_pubkey": (b"F" * 32).hex(),
        "message_text": "payload",
        "post_timestamp": 1234.5,
    }

    packet = SimpleNamespace(path=bytearray(), path_len=0)
    with (
        patch(
            "repeater.handler_helpers.room_server.CryptoUtils.sha256",
            return_value=b"\x01\x02\x03\x04abcd",
        ),
        patch(
            "repeater.handler_helpers.room_server.PacketBuilder.create_datagram",
            return_value=packet,
        ),
    ):
        ok = await rs.push_post_to_client(client, post)

    assert ok is True
    assert bytes(packet.path) == b"\xaa\xbb"
    assert packet.path_len == 2
    injector.assert_awaited_once_with(
        packet,
        wait_for_ack=True,
        expected_crc=67305985,
        ack_timeout_s=10.0,
    )
    rs._handle_ack_received.assert_awaited_once_with(
        client.id.get_public_key(), post["post_timestamp"]
    )
    rs.global_limiter.release.assert_called_once()


@pytest.mark.asyncio
async def test_room_server_push_expected_ack_matches_firmware_signed_ack():
    """The pending ACK CRC must match what a signed-plain receiver sends back.

    Firmware clients (BaseChatMesh::onPeerDataRecv) and openhop_core's text
    handler ack TXT_TYPE_SIGNED_PLAIN with
    ``sha256(decrypted[0 : 9 + strlen(text)] || client_pubkey)[:4]``.
    Regression test for the room server expecting a plain-DM style hash
    (timestamp + attempt + text) instead: every push timed out and posts were
    re-pushed forever (issue #286).
    """
    from openhop_core import LocalIdentity
    from openhop_core.protocol import CryptoUtils, Identity

    room_id = LocalIdentity()
    client_id = LocalIdentity()
    client_pk = client_id.get_public_key()
    shared = Identity(client_pk).calc_shared_secret(room_id.get_private_key())

    db = _FakeDB()
    sent = []
    injector_kwargs = []

    async def injector(packet, wait_for_ack=False, **kwargs):
        sent.append(packet)
        injector_kwargs.append(kwargs)
        return False  # no ACK: leaves the pending upsert as the only db write

    rs = RoomServer(
        room_hash=room_id.get_public_key()[0],
        room_name="parity",
        local_identity=room_id,
        sqlite_handler=db,
        packet_injector=injector,
        acl=_FakeACL(),
    )
    rs.global_limiter = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())

    author = LocalIdentity().get_public_key()
    client = _FakeClient(pubkey=client_pk, shared_secret=shared, out_path=b"\x01", out_path_len=1)
    post = {
        "author_pubkey": author.hex(),
        "message_text": "parity check",
        "post_timestamp": 1234.5,
    }

    ok = await rs.push_post_to_client(client, post)
    assert ok is False
    assert len(sent) == 1

    upserts = [
        c.kwargs for c in db.upsert_client_sync.call_args_list if "pending_ack_crc" in c.kwargs
    ]
    assert len(upserts) == 1
    expected_ack_crc = upserts[0]["pending_ack_crc"]

    # The injector must be told the crypto CRC (and the computed timeout) so
    # dispatcher.wait_for_ack matches the client's actual ACK.
    assert injector_kwargs[0]["expected_crc"] == expected_ack_crc
    assert injector_kwargs[0]["ack_timeout_s"] > 0

    # Decrypt the pushed datagram and verify the signed-plain layout.
    pkt = sent[0]
    encrypted = bytes(pkt.payload[2 : pkt.payload_len])
    decrypted = CryptoUtils.mac_then_decrypt(shared[:16], shared, encrypted)
    assert decrypted is not None
    flags = decrypted[4]
    assert (flags >> 2) & 0x3F == TXT_TYPE_SIGNED_PLAIN
    assert bytes(decrypted[5:9]) == author[:4]
    body = bytes(decrypted[9:])
    nul = body.find(b"\x00")
    text_len = nul if nul >= 0 else len(body)
    assert body[:text_len] == b"parity check"

    # Recompute the ACK exactly as the receiving client does.
    client_ack_crc = int.from_bytes(
        CryptoUtils.sha256(bytes(decrypted[: 9 + text_len]) + client_pk)[:4], "little"
    )
    assert client_ack_crc == expected_ack_crc


@pytest.mark.asyncio
async def test_room_server_push_post_to_client_backoff_skip_and_timeout_path():
    db = _FakeDB()
    injector = AsyncMock(return_value=False)
    rs = _make_room_server(db=db, injector=injector)
    rs.global_limiter = SimpleNamespace(acquire=AsyncMock(), release=MagicMock())
    rs._handle_ack_timeout = AsyncMock()

    client = _FakeClient(pubkey=b"G" * 32)
    post = {
        "author_pubkey": (b"H" * 32).hex(),
        "message_text": "payload",
        "post_timestamp": 88.0,
    }

    # In backoff window: skip send.
    db.get_client_sync.return_value = {"push_failures": 2, "updated_at": time.time()}
    skip_ok = await rs.push_post_to_client(client, post)
    assert skip_ok is False
    injector.assert_not_awaited()

    # Out of backoff and send fails -> timeout handler called.
    db.get_client_sync.return_value = {"push_failures": 1, "updated_at": time.time() - 9999}
    with (
        patch(
            "repeater.handler_helpers.room_server.PacketBuilder._pack_timestamp_data",
            return_value=b"pk",
        ),
        patch(
            "repeater.handler_helpers.room_server.CryptoUtils.sha256",
            return_value=b"\x01\x02\x03\x04abcd",
        ),
        patch(
            "repeater.handler_helpers.room_server.PacketBuilder.create_datagram",
            return_value=SimpleNamespace(path=bytearray(), path_len=0),
        ),
    ):
        fail_ok = await rs.push_post_to_client(client, post)

    assert fail_ok is False
    rs._handle_ack_timeout.assert_awaited_once_with(client.id.get_public_key())


@pytest.mark.asyncio
async def test_room_server_ack_helpers_and_unsynced_count_fallbacks():
    db = _FakeDB()
    rs = _make_room_server(db=db)

    await rs._handle_ack_received(b"I" * 32, post_timestamp=123.0)
    db.upsert_client_sync.assert_called()

    db.get_client_sync.return_value = {"push_failures": 2}
    await rs._handle_ack_timeout(b"I" * 32)
    # last call should have incremented failures and cleared pending ack
    timeout_kwargs = db.upsert_client_sync.call_args.kwargs
    assert timeout_kwargs["push_failures"] == 3
    assert timeout_kwargs["pending_ack_crc"] == 0

    db.get_client_sync.side_effect = RuntimeError("db down")
    assert rs.get_unsynced_count(b"I" * 32) == 0


@pytest.mark.asyncio
async def test_room_server_evict_failed_clients_and_check_ack_timeouts():
    db = _FakeDB()
    acl = _FakeACL([_FakeClient(b"J" * 32)])
    rs = _make_room_server(db=db, acl=acl)

    now = time.time()
    db.get_all_room_clients.return_value = [
        {
            "client_pubkey": (b"J" * 32).hex(),
            "push_failures": 3,
            "last_activity": now,
            "pending_ack_crc": 0,
            "ack_timeout_time": 0,
        },
        {
            "client_pubkey": (b"K" * 32).hex(),
            "push_failures": 0,
            "last_activity": now - 5000,
            "pending_ack_crc": 0,
            "ack_timeout_time": 0,
        },
    ]

    await rs._evict_failed_clients()
    assert db.upsert_client_sync.call_count >= 2
    assert acl.remove_client.call_count == 2

    rs._handle_ack_timeout = AsyncMock()
    db.get_all_room_clients.return_value = [
        {
            "client_pubkey": (b"L" * 32).hex(),
            "pending_ack_crc": 123,
            "ack_timeout_time": now - 1,
        },
        {
            "client_pubkey": (b"M" * 32).hex(),
            "pending_ack_crc": 0,
            "ack_timeout_time": now - 1,
        },
    ]
    await rs._check_ack_timeouts()
    rs._handle_ack_timeout.assert_awaited_once_with(b"L" * 32)


@pytest.mark.asyncio
async def test_room_server_start_and_stop_are_idempotent():
    rs = _make_room_server()

    await rs.start()
    assert rs._running is True
    first_task = rs._sync_task

    # Second start should not replace task.
    await rs.start()
    assert rs._sync_task is first_task

    await rs.stop()
    assert rs._running is False

    # Stop again should be safe.
    await rs.stop()

    # Ensure task is cleaned up.
    if first_task:
        assert first_task.cancelled() or first_task.done()
