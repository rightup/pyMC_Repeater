import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from repeater.handler_helpers.acl import ACL, PERM_ACL_ADMIN, PERM_ACL_GUEST
from repeater.handler_helpers.advert import AdvertHelper, MeshActivityTier


class _FakeIdentity:
    def __init__(self, pubkey: bytes):
        self._pubkey = pubkey

    def get_public_key(self):
        return self._pubkey


class _FakePacket:
    def __init__(self, *, header=0x00, path=None, pkt_hash=b"\xaa" * 16):
        self.header = header
        self.path = path if path is not None else bytearray()
        self._pkt_hash = pkt_hash
        self.mark_do_not_retransmit = MagicMock()
        self.drop_reason = None

    def calculate_packet_hash(self):
        return self._pkt_hash


def test_acl_blank_password_guest_rules_and_room_server_password_requirements():
    identity = _FakeIdentity(b"A" * 32)

    acl = ACL(allow_read_only=True)
    ok, perms = acl.authenticate_client(
        client_identity=identity,
        shared_secret=b"secret",
        password="",
        timestamp=10,
        sync_since=42,
    )
    assert ok is True
    assert perms == PERM_ACL_GUEST
    guest_client = acl.get_client(identity.get_public_key())
    assert guest_client is not None
    assert guest_client.permissions == PERM_ACL_GUEST
    assert guest_client.shared_secret == b"secret"
    assert guest_client.last_timestamp == 10

    # Blank-password logins now track replay/timestamp just like password logins.
    replay_ok, replay_perms = acl.authenticate_client(
        client_identity=identity,
        shared_secret=b"secret",
        password="",
        timestamp=10,
    )
    assert replay_ok is False
    assert replay_perms == 0

    # The read-only guest must land in the ACL with its shared secret: the
    # room server's text handler and sync loop only see ACL members, so an
    # absent entry means posts are dropped un-ACKed and pushes never happen.
    guest = acl.get_client(b"A" * 32)
    assert guest is not None
    assert guest.is_guest() is True
    assert guest.shared_secret == b"secret"
    assert guest.sync_since == 42

    # A repeat blank-password login reuses the existing entry.
    ok_again, perms_again = acl.authenticate_client(
        client_identity=identity,
        shared_secret=b"secret",
        password="",
        timestamp=11,
    )
    assert ok_again is True
    assert perms_again == PERM_ACL_GUEST
    assert acl.get_num_clients() == 1

    # A full ACL rejects new blank-password guests.
    acl_full = ACL(max_clients=0, allow_read_only=True)
    full_ok, full_perms = acl_full.authenticate_client(
        client_identity=identity,
        shared_secret=b"secret",
        password="",
        timestamp=10,
    )
    assert full_ok is False
    assert full_perms == 0

    acl_ro_disabled = ACL(allow_read_only=False)
    ok2, perms2 = acl_ro_disabled.authenticate_client(
        client_identity=identity,
        shared_secret=b"secret",
        password="",
        timestamp=10,
    )
    assert ok2 is False
    assert perms2 == 0

    room_cfg = {"type": "room_server", "settings": {}}
    ok3, perms3 = acl.authenticate_client(
        client_identity=identity,
        shared_secret=b"secret",
        password="admin",
        timestamp=11,
        target_identity_name="room-a",
        target_identity_config=room_cfg,
    )
    assert ok3 is False
    assert perms3 == 0


def test_acl_admin_login_sets_client_state_and_replay_protection():
    identity = _FakeIdentity(b"B" * 32)
    acl = ACL(max_clients=5, admin_password="top-secret", guest_password="guest")

    ok, perms = acl.authenticate_client(
        client_identity=identity,
        shared_secret=b"k" * 32,
        password="top-secret",
        timestamp=100,
        sync_since=77,
    )
    assert ok is True
    assert perms == PERM_ACL_ADMIN

    client = acl.get_client(b"B" * 40)
    assert client is not None
    assert client.shared_secret == b"k" * 32
    assert client.last_timestamp == 100
    assert client.sync_since == 77
    assert client.is_admin() is True

    replay_ok, replay_perms = acl.authenticate_client(
        client_identity=identity,
        shared_secret=b"k" * 32,
        password="top-secret",
        timestamp=100,
    )
    assert replay_ok is False
    assert replay_perms == 0


def test_acl_max_clients_invalid_password_and_remove_client_paths():
    acl = ACL(max_clients=1, admin_password="a", guest_password="g")
    id_a = _FakeIdentity(b"C" * 32)
    id_b = _FakeIdentity(b"D" * 32)

    ok_a, _ = acl.authenticate_client(id_a, b"s", "a", timestamp=1)
    assert ok_a is True
    assert acl.get_num_clients() == 1

    full_ok, full_perms = acl.authenticate_client(id_b, b"s", "a", timestamp=2)
    assert full_ok is False
    assert full_perms == 0

    bad_ok, bad_perms = acl.authenticate_client(id_a, b"s", "bad", timestamp=3)
    assert bad_ok is False
    assert bad_perms == 0

    assert acl.remove_client(b"C" * 32) is True
    assert acl.remove_client(b"C" * 32) is False


@pytest.mark.asyncio
async def test_advert_process_invalid_packet_marks_drop_and_no_storage():
    storage = SimpleNamespace(get_neighbors=lambda: {}, record_advert=MagicMock())
    helper = AdvertHelper(local_identity=None, storage=storage, config={"repeater": {}})
    helper.advert_handler = AsyncMock(return_value={"valid": False})

    packet = _FakePacket()
    await helper.process_advert_packet(packet, rssi=-80, snr=6.5)

    packet.mark_do_not_retransmit.assert_called_once()
    assert packet.drop_reason == "Invalid advert packet"
    storage.record_advert.assert_not_called()


@pytest.mark.asyncio
async def test_advert_duplicate_reheard_skips_storage_and_tracks_duplicate_stat():
    storage = SimpleNamespace(get_neighbors=lambda: {}, record_advert=MagicMock())
    helper = AdvertHelper(local_identity=None, storage=storage, config={"repeater": {}})
    helper.advert_handler = AsyncMock(
        return_value={
            "valid": True,
            "public_key": "11" * 32,
            "name": "node-1",
            "contact_type": "REPEATER",
            "latitude": 1.0,
            "longitude": 2.0,
        }
    )

    packet = _FakePacket(pkt_hash=b"\x10" * 16)
    await helper.process_advert_packet(packet, rssi=-70, snr=5.0)
    await helper.process_advert_packet(packet, rssi=-70, snr=5.0)

    assert storage.record_advert.call_count == 1
    stats = helper.get_rate_limit_stats()
    assert stats["stats"]["adverts_duplicate_reheard"] == 1


@pytest.mark.asyncio
async def test_advert_own_advert_is_ignored_after_validation():
    local = _FakeIdentity(bytes.fromhex("22" * 32))
    storage = SimpleNamespace(get_neighbors=lambda: {}, record_advert=MagicMock())
    helper = AdvertHelper(local_identity=local, storage=storage, config={"repeater": {}})
    helper.advert_handler = AsyncMock(
        return_value={
            "valid": True,
            "public_key": ("22" * 32),
            "name": "self-node",
            "contact_type": "REPEATER",
            "latitude": 1.0,
            "longitude": 2.0,
        }
    )

    await helper.process_advert_packet(_FakePacket(), rssi=-60, snr=8.0)

    storage.record_advert.assert_not_called()


@pytest.mark.asyncio
async def test_advert_new_neighbor_persists_record_and_flags_new_neighbor():
    stored_records = []

    def _record_advert(data):
        stored_records.append(data)

    storage = SimpleNamespace(get_neighbors=lambda: {}, record_advert=_record_advert)
    helper = AdvertHelper(local_identity=None, storage=storage, config={"repeater": {}})
    helper.advert_handler = AsyncMock(
        return_value={
            "valid": True,
            "public_key": "33" * 32,
            "name": "neighbor-a",
            "contact_type": "REPEATER",
            "latitude": 10.0,
            "longitude": 20.0,
        }
    )

    packet = _FakePacket(header=0x01, path=bytearray())
    await helper.process_advert_packet(packet, rssi=-75, snr=4.2)

    assert len(stored_records) == 1
    record = stored_records[0]
    assert record["pubkey"] == "33" * 32
    assert record["node_name"] == "neighbor-a"
    assert record["is_new_neighbor"] is True
    assert record["zero_hop"] is True


def test_advert_allow_advert_rate_limit_penalty_and_quiet_bypass():
    cfg = {
        "repeater": {
            "advert_adaptive": {"enabled": False},
            "advert_rate_limit": {
                "enabled": True,
                "bucket_capacity": 1,
                "refill_tokens": 1,
                "refill_interval_seconds": 9999,
                "min_interval_seconds": 100,
            },
            "advert_penalty_box": {
                "enabled": True,
                "violation_threshold": 1,
                "violation_decay_seconds": 1000,
                "base_penalty_seconds": 10,
                "penalty_multiplier": 2,
                "max_penalty_seconds": 60,
            },
        }
    }
    helper = AdvertHelper(local_identity=None, storage=None, config=cfg)

    t0 = time.time()
    ok1, reason1 = helper._allow_advert("AA" * 16, t0)
    assert ok1 is True
    assert reason1 == ""

    ok2, reason2 = helper._allow_advert("AA" * 16, t0 + 1)
    assert ok2 is False
    assert "min-interval" in reason2

    ok3, reason3 = helper._allow_advert("AA" * 16, t0 + 2)
    assert ok3 is False
    assert "penalty box active" in reason3

    # QUIET tier bypass when adaptive mode is on
    helper._adaptive_enabled = True
    helper._current_tier = MeshActivityTier.QUIET
    ok4, _ = helper._allow_advert("AA" * 16, t0 + 3)
    assert ok4 is True


def test_advert_reload_config_and_cleanup_old_state_bounds_memory():
    helper = AdvertHelper(local_identity=None, storage=None, config={"repeater": {}})
    helper.config = {
        "repeater": {
            "advert_adaptive": {
                "enabled": True,
                "ewma_alpha": 0.2,
                "hysteresis_seconds": 10,
                "thresholds": {"normal": 2, "busy": 7, "congested": 12},
            },
            "advert_rate_limit": {
                "enabled": True,
                "bucket_capacity": 3,
                "refill_tokens": 2,
                "refill_interval_seconds": 30,
                "min_interval_seconds": 5,
            },
            "advert_penalty_box": {
                "enabled": True,
                "violation_threshold": 2,
                "violation_decay_seconds": 20,
                "base_penalty_seconds": 15,
                "penalty_multiplier": 2,
                "max_penalty_seconds": 120,
            },
            "advert_dedupe": {"ttl_seconds": 30, "max_hashes": 100},
        }
    }

    helper.reload_config()
    assert helper._ewma_alpha == 0.2
    assert helper._base_bucket_capacity == 3.0
    assert helper._advert_dedupe_ttl_seconds == 30.0

    now = time.time()
    helper._recent_advert_hashes["old"] = now - 1
    helper._penalty_until["pk"] = now - 1
    helper._bucket_state["oldpk"] = {
        "last_seen": now - (helper._bucket_state_retention_seconds + 1)
    }
    helper._violation_state["oldpk"] = {"count": 3, "last_violation": now - 9999}

    helper._cleanup_old_state(now)

    assert "old" not in helper._recent_advert_hashes
    assert "pk" not in helper._penalty_until
    assert "oldpk" not in helper._bucket_state
