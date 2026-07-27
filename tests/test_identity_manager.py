from repeater.identity_manager import IdentityManager


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
    mgr.identities[0x44] = (None, {}, "repeater")
    mgr.registered_hashes[0x44] = "repeater:ghost"

    listed = mgr.list_identities()
    assert listed[0]["address"] == "N/A"
    assert listed[0]["public_key"] is None
