import pytest

from repeater.identity_manager import IdentityConfigurationError, IdentityManager, IdentitySpec


class _FakeIdentity:
    def __init__(self, pubkey: bytes, addr: bytes = b"\xaa\xbb"):
        self._pubkey = pubkey
        self._addr = addr

    def get_public_key(self):
        return self._pubkey

    def get_address_bytes(self):
        return self._addr


def test_identity_manager_register_lookup_and_collision_paths():
    mgr = IdentityManager(config={})
    id_a = _FakeIdentity(bytes([0x11]) + b"A" * 31, addr=b"\x01\x02")
    id_b_collision = _FakeIdentity(bytes([0x11]) + b"B" * 31, addr=b"\x03\x04")

    assert mgr.register_identity("alpha", id_a, {"k": 1}, "repeater") is True
    assert mgr.has_identity(0x11) is True
    assert mgr.get_identity_by_hash(0x11)[0] is id_a
    assert mgr.get_identity_by_name("alpha")[0] is id_a

    assert mgr.register_identity("beta", id_b_collision, {"k": 2}, "room_server") is False


def test_identity_manager_rejects_duplicate_names_without_mutating_state():
    mgr = IdentityManager(config={})
    id_a = _FakeIdentity(bytes([0x11]) + b"A" * 31)
    id_b = _FakeIdentity(bytes([0x22]) + b"B" * 31)

    assert mgr.register_identity("alpha", id_a, {}, "repeater") is True
    assert "already registered" in mgr.registration_error("alpha", id_b, "companion")
    assert mgr.validate_identity("alpha", id_b, "companion") is False
    assert mgr.get_identity_by_hash(0x22) is None


def test_identity_manager_list_and_type_filtering():
    mgr = IdentityManager(config={})
    id_a = _FakeIdentity(bytes([0x22]) + b"A" * 31)
    id_b = _FakeIdentity(bytes([0x33]) + b"B" * 31)

    mgr.register_identity("rep-main", id_a, {"x": 1}, "repeater")
    mgr.register_identity("room-a", id_b, {"y": 2}, "room_server")

    listed = mgr.list_identities()
    assert len(listed) == 2
    assert any(item["hash"] == "0x22" and item["name"] == "repeater:rep-main" for item in listed)
    assert any(item["hash"] == "0x33" and item["type"] == "room_server" for item in listed)

    assert mgr.has_identity_type("repeater") is True
    assert mgr.has_identity_type("room_server") is True
    assert mgr.has_identity_type("unknown") is False

    by_type = mgr.get_identities_by_type("room_server")
    assert len(by_type) == 1
    assert by_type[0][0] == "room-a"


def test_identity_manager_list_handles_none_identity_fields():
    mgr = IdentityManager(config={})
    mgr.identities[(0x44, "server")] = (None, {}, "repeater")
    mgr.registered_hashes[(0x44, "server")] = "repeater:ghost"

    listed = mgr.list_identities()
    assert listed[0]["address"] == "N/A"
    assert listed[0]["public_key"] is None


def test_validate_specs_rejects_intra_batch_same_namespace_hash_collision():
    """Two server-side identities (repeater + room server) share the login/
    text helper handlers[hash] slot, so a prefix collision is unrepresentable."""
    mgr = IdentityManager(config={})
    id_a = _FakeIdentity(bytes([0x11]) + b"A" * 31)
    id_b = _FakeIdentity(bytes([0x11]) + b"B" * 31)

    with pytest.raises(IdentityConfigurationError, match="one-byte public-key prefixes"):
        mgr.validate_specs(
            [
                IdentitySpec("alpha", id_a, {}, "repeater"),
                IdentitySpec("beta", id_b, {}, "room_server"),
            ]
        )


def test_validate_specs_rejects_two_companions_same_prefix():
    """Two companions share companion_bridges[hash] and the companion_* DB
    keying, so their prefix must be unique."""
    mgr = IdentityManager(config={})
    id_a = _FakeIdentity(bytes([0x11]) + b"A" * 31)
    id_b = _FakeIdentity(bytes([0x11]) + b"B" * 31)

    with pytest.raises(IdentityConfigurationError, match="one-byte public-key prefixes"):
        mgr.validate_specs(
            [
                IdentitySpec("alpha", id_a, {}, "companion"),
                IdentitySpec("beta", id_b, {}, "companion"),
            ]
        )


def test_validate_specs_allows_companion_sharing_prefix_with_server_identity():
    """A companion and a server-side identity live in separate runtime stores
    and DB tables (the router disambiguates by HMAC), so they may share a
    one-byte prefix. Verified for both the repeater and a room server."""
    mgr = IdentityManager(config={})
    repeater = _FakeIdentity(bytes([0x11]) + b"R" * 31)
    room = _FakeIdentity(bytes([0x22]) + b"S" * 31)
    comp_vs_repeater = _FakeIdentity(bytes([0x11]) + b"C" * 31)
    comp_vs_room = _FakeIdentity(bytes([0x22]) + b"D" * 31)

    mgr.validate_specs(
        [
            IdentitySpec("rep", repeater, {}, "repeater"),
            IdentitySpec("room", room, {}, "room_server"),
            IdentitySpec("comp-a", comp_vs_repeater, {}, "companion"),
            IdentitySpec("comp-b", comp_vs_room, {}, "companion"),
        ]
    )


def test_register_companion_sharing_repeater_prefix_keeps_both():
    """Cross-namespace registration keeps both entries addressable."""
    mgr = IdentityManager(config={})
    repeater = _FakeIdentity(bytes([0x11]) + b"R" * 31)
    companion = _FakeIdentity(bytes([0x11]) + b"C" * 31)

    assert mgr.register_identity("rep", repeater, {}, "repeater") is True
    assert mgr.register_identity("comp", companion, {}, "companion") is True

    assert mgr.get_identity_by_hash(0x11, "server")[0] is repeater
    assert mgr.get_identity_by_hash(0x11, "companion")[0] is companion
    assert mgr.has_identity(0x11, "server") is True
    assert mgr.has_identity(0x11, "companion") is True
    assert len(mgr.list_identities()) == 2


def test_validate_specs_rejects_intra_batch_duplicate_name():
    mgr = IdentityManager(config={})
    id_a = _FakeIdentity(bytes([0x11]) + b"A" * 31)
    id_b = _FakeIdentity(bytes([0x22]) + b"B" * 31)

    with pytest.raises(IdentityConfigurationError, match="repeater:alpha"):
        mgr.validate_specs(
            [
                IdentitySpec("alpha", id_a, {}, "repeater"),
                IdentitySpec("alpha", id_b, {}, "companion"),
            ]
        )


def test_validate_specs_rejects_registered_collisions_without_mutation():
    mgr = IdentityManager(config={})
    id_a = _FakeIdentity(bytes([0x11]) + b"A" * 31)
    id_hash_collision = _FakeIdentity(bytes([0x11]) + b"B" * 31)
    id_name_collision = _FakeIdentity(bytes([0x22]) + b"C" * 31)

    assert mgr.register_identity("alpha", id_a, {}, "repeater") is True

    # A room server (server namespace) collides with the registered repeater.
    with pytest.raises(IdentityConfigurationError, match="conflicts"):
        mgr.validate_specs([IdentitySpec("beta", id_hash_collision, {}, "room_server")])
    with pytest.raises(IdentityConfigurationError, match="already registered"):
        mgr.validate_specs([IdentitySpec("alpha", id_name_collision, {}, "companion")])

    # Validation never registers anything.
    assert mgr.get_identity_by_hash(0x22) is None
    assert mgr.get_identity_by_name("beta") is None


def test_validate_specs_accepts_distinct_batch():
    mgr = IdentityManager(config={})
    mgr.validate_specs(
        [
            IdentitySpec("alpha", _FakeIdentity(bytes([0x11]) + b"A" * 31), {}, "repeater"),
            IdentitySpec("beta", _FakeIdentity(bytes([0x22]) + b"B" * 31), {}, "room_server"),
            IdentitySpec("gamma", _FakeIdentity(bytes([0x33]) + b"C" * 31), {}, "companion"),
        ]
    )
