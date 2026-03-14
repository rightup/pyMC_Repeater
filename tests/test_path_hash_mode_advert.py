"""
Tests for path hash mode on repeater-originated adverts (multi-byte path support).

When mesh.path_hash_mode is 1 or 2, flood 0-hop packets (e.g. adverts) sent via
dispatcher.send_packet() must have path_len encoding set so get_path_hash_size()
returns 2 or 3. The dispatcher applies this in send_packet() before transmit.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from pymc_core.node.dispatcher import Dispatcher
from pymc_core.protocol import Packet
from pymc_core.protocol.constants import (
    PAYLOAD_TYPE_ADVERT,
    PH_TYPE_SHIFT,
    ROUTE_TYPE_FLOOD,
)


class MockRadio:
    """Minimal mock radio: send() stores data and returns True."""

    def __init__(self):
        self.tx_data = None

    async def send(self, data: bytes) -> bool:
        self.tx_data = data
        return True

    def set_rx_callback(self, callback):
        pass


def _make_advert_packet():
    """Build a 0-hop flood ADVERT packet (same shape as PacketBuilder.create_advert)."""
    pkt = Packet()
    # Version 1, ROUTE_TYPE_FLOOD, PAYLOAD_TYPE_ADVERT
    pkt.header = (1 << 6) | (PAYLOAD_TYPE_ADVERT << PH_TYPE_SHIFT) | ROUTE_TYPE_FLOOD
    pkt.path_len = 0
    pkt.path = bytearray()
    pkt.payload = bytearray(b"minimal_advert_payload")
    pkt.payload_len = len(pkt.payload)
    return pkt


@pytest.fixture
def dispatcher():
    radio = MockRadio()
    return Dispatcher(radio=radio)


@pytest.mark.asyncio
async def test_path_hash_mode_1_sets_2_byte_encoding(dispatcher):
    """With path_hash_mode=1, sent advert packet has get_path_hash_size() == 2."""
    dispatcher.set_default_path_hash_mode(1)
    packet = _make_advert_packet()
    await dispatcher.send_packet(packet, wait_for_ack=False)
    assert packet.get_path_hash_size() == 2
    assert packet.get_path_hash_count() == 0
    assert dispatcher.radio.tx_data is not None


@pytest.mark.asyncio
async def test_path_hash_mode_2_sets_3_byte_encoding(dispatcher):
    """With path_hash_mode=2, sent advert packet has get_path_hash_size() == 3."""
    dispatcher.set_default_path_hash_mode(2)
    packet = _make_advert_packet()
    await dispatcher.send_packet(packet, wait_for_ack=False)
    assert packet.get_path_hash_size() == 3
    assert packet.get_path_hash_count() == 0
    assert dispatcher.radio.tx_data is not None


@pytest.mark.asyncio
async def test_path_hash_mode_0_leaves_1_byte_encoding(dispatcher):
    """With path_hash_mode=0 (default), path_len stays 0 (1-byte hash size)."""
    dispatcher.set_default_path_hash_mode(0)
    packet = _make_advert_packet()
    await dispatcher.send_packet(packet, wait_for_ack=False)
    assert packet.get_path_hash_size() == 1
    assert packet.path_len == 0
