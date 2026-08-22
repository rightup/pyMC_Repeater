"""ACL role numbering must match upstream MeshCore firmware.

Firmware ``src/helpers/ClientACL.h`` puts the role in the LOW TWO BITS of the
permissions byte (GUEST=0, READ_ONLY=1, READ_WRITE=2, ADMIN=3) and derives
admin with ``(permissions & 3) == 3``. Every stock MeshCore client decodes the
login reply's ACL byte that way, so any divergence here shows up as an OpenHop
admin being rendered read-write/guest in third-party apps (openhop_repeater
issue #388).
"""

from pathlib import Path

import pytest
import yaml

from openhop_core.protocol.acl_conformance import ROLE_MASK, ROLE_VALUES

from repeater.handler_helpers.acl import (
    ACL,
    PERM_ACL_ADMIN,
    PERM_ACL_GUEST,
    PERM_ACL_READ_ONLY,
    PERM_ACL_READ_WRITE,
    PERM_ACL_ROLE_MASK,
    ClientInfo,
    is_admin_permissions,
    role_name,
    role_of,
)

ROOM_CFG = {
    "type": "room_server",
    "settings": {"admin_password": "room-admin", "guest_password": "room-guest"},
}


class _Id:
    def __init__(self, pubkey: bytes):
        self._pubkey = pubkey

    def get_public_key(self):
        return self._pubkey


def test_role_constants_match_firmware_clientacl_h():
    """Literal pin against the firmware header, via the shared vector table."""
    assert PERM_ACL_ROLE_MASK == ROLE_MASK == 0x03
    assert PERM_ACL_GUEST == ROLE_VALUES["guest"] == 0x00
    assert PERM_ACL_READ_ONLY == ROLE_VALUES["read_only"] == 0x01
    assert PERM_ACL_READ_WRITE == ROLE_VALUES["read_write"] == 0x02
    assert PERM_ACL_ADMIN == ROLE_VALUES["admin"] == 0x03
    # Every role must be distinct; GUEST and READ_WRITE used to collide on 0x01.
    roles = {PERM_ACL_GUEST, PERM_ACL_READ_ONLY, PERM_ACL_READ_WRITE, PERM_ACL_ADMIN}
    assert len(roles) == 4


def test_roles_come_from_openhop_core_and_never_diverge():
    """We re-export core's values rather than keeping a second copy.

    This must not skip on an old core. A core without these symbols encodes
    the login reply's is_admin byte as ``permissions & 0x02``, which also
    matches READ_WRITE — pairing it with our numbering would announce a room
    server's read-write clients as admins. acl.py fails closed at import for
    exactly that reason, and this asserts the contract it depends on.
    """
    from openhop_core.protocol import constants

    assert constants.PERM_ACL_ROLE_MASK is PERM_ACL_ROLE_MASK
    assert constants.PERM_ACL_GUEST is PERM_ACL_GUEST
    assert constants.PERM_ACL_READ_ONLY is PERM_ACL_READ_ONLY
    assert constants.PERM_ACL_READ_WRITE is PERM_ACL_READ_WRITE
    assert constants.PERM_ACL_ADMIN is PERM_ACL_ADMIN
    assert is_admin_permissions is constants.acl_is_admin
    assert role_of is constants.acl_role


@pytest.mark.parametrize(
    "permissions, expect_admin",
    [
        (PERM_ACL_GUEST, False),
        (PERM_ACL_READ_ONLY, False),
        (PERM_ACL_READ_WRITE, False),  # regression: 0x02 bit test said True here
        (PERM_ACL_ADMIN, True),
    ],
)
def test_is_admin_is_an_equality_test_not_a_bit_test(permissions, expect_admin):
    assert is_admin_permissions(permissions) is expect_admin
    assert ClientInfo(_Id(b"P" * 32), permissions).is_admin() is expect_admin


def test_reserved_upper_bits_do_not_change_the_role():
    """Bits above the role mask are reserved flags, not part of the role."""
    assert is_admin_permissions(PERM_ACL_ADMIN | 0xFC) is True
    assert is_admin_permissions(PERM_ACL_READ_WRITE | 0xFC) is False
    assert role_of(PERM_ACL_READ_ONLY | 0xF0) == PERM_ACL_READ_ONLY
    assert role_name(PERM_ACL_ADMIN | 0x80) == "admin"


def test_role_names():
    assert role_name(PERM_ACL_GUEST) == "guest"
    assert role_name(PERM_ACL_READ_ONLY) == "read_only"
    assert role_name(PERM_ACL_READ_WRITE) == "read_write"
    assert role_name(PERM_ACL_ADMIN) == "admin"


def test_admin_password_grants_admin_on_repeater_and_room_server():
    repeater_acl = ACL(admin_password="rpt-admin", guest_password="rpt-guest")
    ok, perms = repeater_acl.authenticate_client(
        client_identity=_Id(b"A" * 32),
        shared_secret=b"s" * 32,
        password="rpt-admin",
        timestamp=100,
    )
    assert (ok, perms) == (True, PERM_ACL_ADMIN)

    room_acl = ACL()
    ok, perms = room_acl.authenticate_client(
        client_identity=_Id(b"B" * 32),
        shared_secret=b"s" * 32,
        password="room-admin",
        timestamp=100,
        target_identity_name="room-a",
        target_identity_config=ROOM_CFG,
    )
    assert (ok, perms) == (True, PERM_ACL_ADMIN)


def test_guest_password_is_guest_on_a_repeater():
    """Repeater guests may fetch base telemetry but must not change settings.

    Matches firmware simple_repeater handleLoginReq, which assigns
    PERM_ACL_GUEST for the guest password.
    """
    acl = ACL(admin_password="rpt-admin", guest_password="rpt-guest")
    ok, perms = acl.authenticate_client(
        client_identity=_Id(b"C" * 32),
        shared_secret=b"s" * 32,
        password="rpt-guest",
        timestamp=100,
    )
    assert ok is True
    assert perms == PERM_ACL_GUEST
    client = acl.get_client(b"C" * 32)
    assert client.is_admin() is False
    assert client.is_guest() is True


def test_guest_password_is_read_write_on_a_room_server():
    """Room-server guests may post and read messages.

    Matches firmware simple_room_server, which assigns PERM_ACL_READ_WRITE for
    the room password and reserves PERM_ACL_GUEST for allow_read_only.
    """
    acl = ACL()
    ok, perms = acl.authenticate_client(
        client_identity=_Id(b"D" * 32),
        shared_secret=b"s" * 32,
        password="room-guest",
        timestamp=100,
        target_identity_name="room-a",
        target_identity_config=ROOM_CFG,
    )
    assert ok is True
    assert perms == PERM_ACL_READ_WRITE
    client = acl.get_client(b"D" * 32)
    assert client.is_admin() is False
    assert client.is_guest() is False  # read-write, so posting is allowed


def test_blank_password_read_only_access_is_guest():
    """allow_read_only mirrors firmware's allow_read_only branch → GUEST."""
    acl = ACL(allow_read_only=True)
    ok, perms = acl.authenticate_client(
        client_identity=_Id(b"E" * 32),
        shared_secret=b"s" * 32,
        password="",
        timestamp=100,
    )
    assert (ok, perms) == (True, PERM_ACL_GUEST)
    assert acl.get_client(b"E" * 32).is_guest() is True


def test_role_change_replaces_the_role_without_touching_reserved_bits():
    acl = ACL(admin_password="rpt-admin", guest_password="rpt-guest")
    identity = _Id(b"F" * 32)

    ok, perms = acl.authenticate_client(
        client_identity=identity,
        shared_secret=b"s" * 32,
        password="rpt-admin",
        timestamp=100,
    )
    assert (ok, perms) == (True, PERM_ACL_ADMIN)

    client = acl.get_client(b"F" * 32)
    client.permissions |= 0x40  # a reserved flag set elsewhere

    ok, perms = acl.authenticate_client(
        client_identity=identity,
        shared_secret=b"s" * 32,
        password="rpt-guest",
        timestamp=101,
    )
    assert ok is True
    assert role_of(perms) == PERM_ACL_GUEST
    assert perms & 0x40  # reserved flag preserved
    assert client.is_admin() is False


def test_every_role_name_is_allowed_by_the_openapi_schema():
    """The ACL client list advertises an enum; every role we emit must be in it.

    The role split added "read_write", which the schema did not previously
    allow — generated clients and validators reject undeclared values.
    """
    spec = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "repeater/web/openapi.yaml").read_text()
    )
    enum = spec["components"]["schemas"]["ACLClient"]["properties"]["permissions"]["enum"]

    emitted = {
        role_name(PERM_ACL_GUEST),
        role_name(PERM_ACL_READ_ONLY),
        role_name(PERM_ACL_READ_WRITE),
        role_name(PERM_ACL_ADMIN),
    }
    assert emitted <= set(enum), f"roles missing from the OpenAPI enum: {emitted - set(enum)}"


def test_acl_refuses_to_import_against_a_core_without_the_role_constants(monkeypatch):
    """The fail-closed guard must actually fire, with a message that helps.

    A core lacking these symbols still builds the login reply's admin byte
    from ``permissions & 0x02``, which would announce a room server's
    READ_WRITE clients as admins. Refusing to import is the safe failure, and
    it is worth a test because nothing else exercises that path.
    """
    import importlib
    import sys
    import types

    import openhop_core.protocol.constants as real_constants

    stub = types.ModuleType("openhop_core.protocol.constants")
    for name in dir(real_constants):
        if not name.startswith("__"):
            setattr(stub, name, getattr(real_constants, name))
    for missing in (
        "PERM_ACL_ROLE_MASK",
        "PERM_ACL_GUEST",
        "PERM_ACL_READ_ONLY",
        "PERM_ACL_READ_WRITE",
        "PERM_ACL_ADMIN",
        "acl_is_admin",
        "acl_role",
    ):
        delattr(stub, missing)

    monkeypatch.setitem(sys.modules, "openhop_core.protocol.constants", stub)
    monkeypatch.delitem(sys.modules, "repeater.handler_helpers.acl")

    with pytest.raises(ImportError) as exc:
        importlib.import_module("repeater.handler_helpers.acl")

    message = str(exc.value)
    assert "openhop_core is too old" in message
    assert "fix/login-perms" in message


def test_acl_imports_normally_against_the_installed_core():
    """Companion to the guard test: the happy path must still work.

    Guards that always fire are worse than no guard, and the test above
    manipulates sys.modules.
    """
    import importlib

    module = importlib.import_module("repeater.handler_helpers.acl")
    assert module.PERM_ACL_ADMIN == 0x03
    assert module.is_admin_permissions(0x03) is True
