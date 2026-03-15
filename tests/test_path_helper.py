from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pymc_core.protocol.packet_utils import PathUtils

from repeater.handler_helpers.path import PathHelper


def _make_client(src_hash: int):
    client = SimpleNamespace()
    client.id = MagicMock()
    client.id.get_public_key.return_value = bytes([src_hash]) + (b"\x00" * 31)
    client.shared_secret = b"\x11" * 32
    client.out_path = bytearray()
    client.out_path_len = 0
    client.last_activity = 0
    return client


def _make_packet(dest_hash: int, src_hash: int):
    pkt = SimpleNamespace()
    # dest + src + placeholder mac/data bytes; decrypt is mocked in tests.
    pkt.payload = bytearray([dest_hash, src_hash, 0x00, 0x00, 0x00, 0x00])
    return pkt


@pytest.mark.asyncio
async def test_path_helper_decodes_encoded_path_len_and_updates_client_out_path():
    dest_hash = 0x42
    src_hash = 0x99
    client = _make_client(src_hash)
    acl = MagicMock()
    acl.get_all_clients.return_value = [client]

    helper = PathHelper(acl_dict={dest_hash: acl})
    packet = _make_packet(dest_hash, src_hash)

    encoded_path_len = PathUtils.encode_path_len(2, 2)
    path_bytes = b"\x11\x22\x33\x44"
    decrypted = bytes([encoded_path_len]) + path_bytes + b"\x01\xAA"

    with patch(
        "pymc_core.protocol.crypto.CryptoUtils.mac_then_decrypt",
        return_value=decrypted,
    ):
        handled = await helper.process_path_packet(packet)

    assert handled is False
    assert client.out_path == bytearray(path_bytes)
    assert client.out_path_len == encoded_path_len
    assert client.last_activity > 0


@pytest.mark.asyncio
async def test_path_helper_rejects_invalid_encoded_path_len():
    dest_hash = 0x42
    src_hash = 0x99
    client = _make_client(src_hash)
    acl = MagicMock()
    acl.get_all_clients.return_value = [client]

    helper = PathHelper(acl_dict={dest_hash: acl})
    packet = _make_packet(dest_hash, src_hash)

    # 0xC1 encodes hash_size=4 (reserved/invalid).
    decrypted = bytes([0xC1]) + b"\x11\x22\x33"

    with patch(
        "pymc_core.protocol.crypto.CryptoUtils.mac_then_decrypt",
        return_value=decrypted,
    ):
        handled = await helper.process_path_packet(packet)

    assert handled is False
    assert client.out_path == bytearray()
    assert client.out_path_len == 0

