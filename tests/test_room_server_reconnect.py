import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)

# ---------------------------------------------------------------------------
# Stub pymc_core before loading room_server directly via importlib so we
# never trigger repeater.handler_helpers.__init__ (which pulls in all other
# helpers and their deep pymc_core dependencies).
# ---------------------------------------------------------------------------

def _pkg(name: str, **attrs) -> types.ModuleType:
    """Register a stub package (with __path__) in sys.modules."""
    m = types.ModuleType(name)
    m.__path__ = []
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return sys.modules[name]

_pkg("pymc_core")
_pkg("pymc_core.protocol", CryptoUtils=MagicMock(), PacketBuilder=MagicMock())
_pkg("pymc_core.protocol.constants", PAYLOAD_TYPE_TXT_MSG=0x04)

# Load room_server.py directly — bypasses handler_helpers/__init__.py entirely.
_rs_path = Path(__file__).parent.parent / "repeater" / "handler_helpers" / "room_server.py"
_spec = importlib.util.spec_from_file_location("repeater.handler_helpers.room_server", _rs_path)
_rs_mod = importlib.util.module_from_spec(_spec)
sys.modules["repeater.handler_helpers.room_server"] = _rs_mod
_spec.loader.exec_module(_rs_mod)

RoomServer = _rs_mod.RoomServer
MAX_PUSH_FAILURES = _rs_mod.MAX_PUSH_FAILURES
POST_SYNC_DELAY_SECS = _rs_mod.POST_SYNC_DELAY_SECS
DEDUP_WINDOW_SECS = _rs_mod.DEDUP_WINDOW_SECS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(pubkey_hex: str = "aa" * 32, sync_since: float = 0.0):
    client = MagicMock()
    pubkey_bytes = bytes.fromhex(pubkey_hex)
    client.id.get_public_key.return_value = pubkey_bytes
    client.sync_since = sync_since
    return client


def _make_room_server(clients=None, db_sync_state=None, unsynced_messages=None):
    """Build a RoomServer with fully mocked infrastructure."""
    local_identity = MagicMock()
    local_identity.get_public_key.return_value = b"\x01" * 32

    db = MagicMock()
    acl = MagicMock()
    packet_injector = AsyncMock()

    clients = clients or []
    acl.get_all_clients.return_value = clients

    db.get_client_sync.return_value = db_sync_state
    db.upsert_client_sync.return_value = None
    db.get_unsynced_messages.return_value = unsynced_messages or []
    db.get_all_room_clients.return_value = []

    with patch.object(_rs_mod, "_global_push_limiter", None):
        server = RoomServer(
            room_hash=0x42,
            room_name="TestRoom",
            local_identity=local_identity,
            sqlite_handler=db,
            packet_injector=packet_injector,
            acl=acl,
            config={},
        )

    server.db = db
    server.acl = acl
    # Start with _running=True; the helper below will stop it after one pass.
    server._running = True
    server.consecutive_sync_errors = 0
    server.next_push_time = 0.0   # Always ready to push
    server.last_eviction_check = 9_999_999_999.0   # Skip eviction
    server.last_cleanup_time = 9_999_999_999.0     # Skip cleanup

    return server, db, acl


async def _run_one_iteration(server, now=9999.0):
    """Run _sync_loop for exactly one while-loop iteration.

    The loop body starts with ``await asyncio.sleep(...)``.  We use a
    side_effect counter so that:
      - The startup sleep (before the while) is a no-op.
      - The first sleep *inside* the loop is also a no-op, but sets
        _running=False so the loop exits after processing.
    """
    call_count = 0

    async def _one_shot_sleep(_delay):
        nonlocal call_count
        call_count += 1
        if call_count == 2:          # 1=startup, 2=first body sleep → stop after
            server._running = False

    with (
        patch("asyncio.sleep", side_effect=_one_shot_sleep),
        patch("time.time", return_value=now),
        patch("random.uniform", return_value=0.0),
    ):
        await server._sync_loop()


# ===========================================================================
# Evicted client (last_activity == 0) — reconnect restore path
# ===========================================================================

class TestSyncLoopReconnect:

    def test_evicted_client_upsert_called_to_restore(self):
        pubkey_hex = "bb" * 32
        client = _make_client(pubkey_hex)
        sync_state = {"pending_ack_crc": 0, "last_activity": 0, "push_failures": 0, "sync_since": 1000.0, "updated_at": 0}
        server, db, _ = _make_room_server(clients=[client], db_sync_state=sync_state, unsynced_messages=[])
        _run(_run_one_iteration(server))
        db.upsert_client_sync.assert_called_once()
        kwargs = db.upsert_client_sync.call_args[1]
        assert kwargs.get("last_activity", 0) > 0
        assert kwargs.get("push_failures") == 0
        assert kwargs.get("pending_ack_crc") == 0

    def test_evicted_client_preserves_sync_since(self):
        pubkey_hex = "cc" * 32
        client = _make_client(pubkey_hex)
        sync_state = {"pending_ack_crc": 0, "last_activity": 0, "push_failures": 2, "sync_since": 5000.0, "updated_at": 0}
        server, db, _ = _make_room_server(clients=[client], db_sync_state=sync_state, unsynced_messages=[])
        _run(_run_one_iteration(server))
        kwargs = db.upsert_client_sync.call_args[1]
        assert kwargs.get("sync_since") == 5000.0

    def test_evicted_client_proceeds_to_fetch_messages(self):
        pubkey_hex = "dd" * 32
        client = _make_client(pubkey_hex)
        sync_state = {"pending_ack_crc": 0, "last_activity": 0, "push_failures": 0, "sync_since": 0.0, "updated_at": 0}
        server, db, _ = _make_room_server(clients=[client], db_sync_state=sync_state, unsynced_messages=[])
        _run(_run_one_iteration(server))
        db.get_unsynced_messages.assert_called_once()


class TestSyncLoopMaxFailures:

    def test_max_failure_client_is_skipped(self):
        pubkey_hex = "ee" * 32
        client = _make_client(pubkey_hex)
        sync_state = {"pending_ack_crc": 0, "last_activity": 9000.0, "push_failures": MAX_PUSH_FAILURES, "sync_since": 0.0, "updated_at": 8000.0}
        server, db, _ = _make_room_server(clients=[client], db_sync_state=sync_state, unsynced_messages=[])
        _run(_run_one_iteration(server))
        db.get_unsynced_messages.assert_not_called()

    def test_failures_one_below_max_not_skipped(self):
        pubkey_hex = "ff" * 32
        client = _make_client(pubkey_hex)
        sync_state = {"pending_ack_crc": 0, "last_activity": 9000.0, "push_failures": MAX_PUSH_FAILURES - 1, "sync_since": 0.0, "updated_at": 8000.0}
        server, db, _ = _make_room_server(clients=[client], db_sync_state=sync_state, unsynced_messages=[])
        _run(_run_one_iteration(server))
        db.get_unsynced_messages.assert_called_once()


class TestSyncLoopNormalClient:

    def test_normal_client_messages_fetched(self):
        pubkey_hex = "11" * 32
        client = _make_client(pubkey_hex)
        sync_state = {"pending_ack_crc": 0, "last_activity": 9000.0, "push_failures": 0, "sync_since": 100.0, "updated_at": 8000.0}
        server, db, _ = _make_room_server(clients=[client], db_sync_state=sync_state, unsynced_messages=[])
        _run(_run_one_iteration(server))
        db.get_unsynced_messages.assert_called_once()

    def test_message_ready_triggers_push(self):
        pubkey_hex = "22" * 32
        client = _make_client(pubkey_hex)
        now = 9999.0
        post = {"id": 1, "post_timestamp": now - POST_SYNC_DELAY_SECS - 1, "message_text": "hello", "author_pubkey": pubkey_hex, "txt_type": 0}
        sync_state = {"pending_ack_crc": 0, "last_activity": 9000.0, "push_failures": 0, "sync_since": 0.0, "updated_at": 8000.0}
        server, db, _ = _make_room_server(clients=[client], db_sync_state=sync_state, unsynced_messages=[post])
        with patch.object(server, "push_post_to_client", new_callable=AsyncMock) as mock_push:
            _run(_run_one_iteration(server, now=now))
        mock_push.assert_called_once_with(client, post)

    def test_no_clients_skips_message_fetch(self):
        server, db, _ = _make_room_server(clients=[], unsynced_messages=[])
        _run(_run_one_iteration(server))
        db.get_unsynced_messages.assert_not_called()

    def test_pending_ack_client_skipped(self):
        pubkey_hex = "33" * 32
        client = _make_client(pubkey_hex)
        sync_state = {
            "pending_ack_crc": 0xDEAD,
            "last_activity": 9000.0,
            "push_failures": 0,
            "sync_since": 0.0,
            "updated_at": 8000.0,
        }
        server, db, _ = _make_room_server(
            clients=[client], db_sync_state=sync_state, unsynced_messages=[]
        )
        _run(_run_one_iteration(server))
        db.get_unsynced_messages.assert_not_called()


# ===========================================================================
# Deduplication of client retransmissions
# ===========================================================================

class TestAddPostDedup:
    """
    Clients retry a message when they don't receive an ack fast enough.
    The MeshCore client sends the SAME sender_timestamp on every retry of the
    same message.  add_post() uses (author, sender_timestamp) as the primary
    dedup key so ALL retransmissions are caught even when the decoded text
    differs slightly (e.g. a trailing U+FFFD replacement char on one copy).

    When sender_timestamp is 0 (web-API path) the fallback is
    (author, normalised_text) + a time-window.
    """

    def _make_server_for_add_post(self):
        server, db, _ = _make_room_server()
        db.insert_room_message.return_value = 1
        return server, db

    # ------------------------------------------------------------------
    # Timestamp-based dedup (primary path — radio retransmissions)
    # ------------------------------------------------------------------

    def test_first_post_is_stored(self):
        server, db = self._make_server_for_add_post()
        pubkey = b"\xaa" * 32
        with patch("time.time", return_value=1000.0):
            result = _run(server.add_post(pubkey, "hello mesh", sender_timestamp=1000))
        assert result is True
        db.insert_room_message.assert_called_once()

    def test_retry_same_timestamp_is_dropped(self):
        """Retransmission carries identical sender_timestamp → duplicate."""
        server, db = self._make_server_for_add_post()
        pubkey = b"\xbb" * 32
        with patch("time.time", return_value=1000.0):
            _run(server.add_post(pubkey, "hello mesh", sender_timestamp=42000))
        db.insert_room_message.reset_mock()
        # Retry 15 s later — same sender_timestamp, text may differ (trailing U+FFFD)
        with patch("time.time", return_value=1015.0):
            result = _run(server.add_post(pubkey, "hello mesh�", sender_timestamp=42000))
        assert result is False
        db.insert_room_message.assert_not_called()

    def test_retry_different_timestamp_is_stored(self):
        """Different sender_timestamp means a genuinely different message."""
        server, db = self._make_server_for_add_post()
        pubkey = b"\xcc" * 32
        with patch("time.time", return_value=1000.0):
            _run(server.add_post(pubkey, "hello again", sender_timestamp=42000))
        db.insert_room_message.reset_mock()
        with patch("time.time", return_value=1060.0):
            result = _run(server.add_post(pubkey, "hello again", sender_timestamp=42060))
        assert result is True
        db.insert_room_message.assert_called_once()

    def test_different_text_from_same_author_is_accepted(self):
        server, db = self._make_server_for_add_post()
        pubkey = b"\xdd" * 32
        with patch("time.time", return_value=1000.0):
            _run(server.add_post(pubkey, "message one", sender_timestamp=42001))
        db.insert_room_message.reset_mock()
        with patch("time.time", return_value=1001.0):
            result = _run(server.add_post(pubkey, "message two", sender_timestamp=42002))
        assert result is True
        db.insert_room_message.assert_called_once()

    def test_same_timestamp_different_authors_both_stored(self):
        """Same timestamp from two different senders are independent messages."""
        server, db = self._make_server_for_add_post()
        pubkey_a = b"\xee" * 32
        pubkey_b = b"\xff" * 32
        with patch("time.time", return_value=1000.0):
            r1 = _run(server.add_post(pubkey_a, "same text", sender_timestamp=42000))
            r2 = _run(server.add_post(pubkey_b, "same text", sender_timestamp=42000))
        assert r1 is True
        assert r2 is True
        assert db.insert_room_message.call_count == 2

    # ------------------------------------------------------------------
    # Text + window fallback (sender_timestamp == 0, e.g. web-API posts)
    # ------------------------------------------------------------------

    def test_fallback_duplicate_within_window_is_dropped(self):
        server, db = self._make_server_for_add_post()
        pubkey = b"\xa1" * 32
        with patch("time.time", return_value=1000.0):
            _run(server.add_post(pubkey, "hello mesh", sender_timestamp=0))
        db.insert_room_message.reset_mock()
        with patch("time.time", return_value=1005.0):
            result = _run(server.add_post(pubkey, "hello mesh", sender_timestamp=0))
        assert result is False
        db.insert_room_message.assert_not_called()

    def test_fallback_same_text_after_window_is_accepted(self):
        server, db = self._make_server_for_add_post()
        pubkey = b"\xa2" * 32
        with patch("time.time", return_value=1000.0):
            _run(server.add_post(pubkey, "hello again", sender_timestamp=0))
        db.insert_room_message.reset_mock()
        with patch("time.time", return_value=1000.0 + DEDUP_WINDOW_SECS + 1):
            result = _run(server.add_post(pubkey, "hello again", sender_timestamp=0))
        assert result is True
        db.insert_room_message.assert_called_once()
