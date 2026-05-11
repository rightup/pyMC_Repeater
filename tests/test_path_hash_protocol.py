"""
Integration tests for multi-byte path hash support using real pymc_core protocol objects.

Exercises actual Packet, PathUtils, PacketBuilder, and engine forwarding
rather than mocking the protocol layer. Covers:
  - PathUtils encode/decode round-trips for all hash sizes
  - Packet serialization/deserialization with multi-byte paths
  - Packet.apply_path_hash_mode and get_path_hashes
  - Engine flood_forward with real multi-byte encoded packets
  - Engine direct_forward with real multi-byte encoded packets
  - PacketBuilder.create_trace payload structure + TraceHandler parsing
  - Max-hop boundary enforcement per hash size
"""
import struct
from collections import OrderedDict
from unittest.mock import MagicMock, patch

import pytest

from pymc_core.protocol import Packet, PacketBuilder, PathUtils
from pymc_core.protocol.constants import (
    MAX_PATH_SIZE,
    PATH_HASH_COUNT_MASK,
    PATH_HASH_SIZE_SHIFT,
    PAYLOAD_TYPE_TRACE,
    PH_TYPE_SHIFT,
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_FLOOD,
)
from pymc_core.node.handlers.trace import TraceHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LOCAL_HASH_BYTES = bytes([0xAB, 0xCD, 0xEF])


def _make_flood_packet(path_bytes: bytes, hash_size: int, hash_count: int,
                        payload: bytes = b"\x01\x02\x03\x04") -> Packet:
    """Create a real flood Packet with the given multi-byte path encoding."""
    pkt = Packet()
    pkt.header = ROUTE_TYPE_FLOOD
    pkt.path = bytearray(path_bytes)
    pkt.path_len = PathUtils.encode_path_len(hash_size, hash_count)
    pkt.payload = bytearray(payload)
    pkt.payload_len = len(payload)
    return pkt


def _make_direct_packet(path_bytes: bytes, hash_size: int, hash_count: int,
                         payload: bytes = b"\x01\x02\x03\x04") -> Packet:
    """Create a real direct-routed Packet."""
    pkt = Packet()
    pkt.header = ROUTE_TYPE_DIRECT
    pkt.path = bytearray(path_bytes)
    pkt.path_len = PathUtils.encode_path_len(hash_size, hash_count)
    pkt.payload = bytearray(payload)
    pkt.payload_len = len(payload)
    return pkt


def _make_handler(path_hash_mode=0, local_hash_bytes=None):
    """Create a real RepeaterHandler with minimal mocking (only radio/storage)."""
    lhb = local_hash_bytes or LOCAL_HASH_BYTES
    config = {
        "repeater": {
            "mode": "forward",
            "cache_ttl": 3600,
            "use_score_for_tx": False,
            "score_threshold": 0.3,
            "send_advert_interval_hours": 0,
            "node_name": "test-node",
        },
        "mesh": {
            "unscoped_flood_allow": True,
            "loop_detect": "off",
            "path_hash_mode": path_hash_mode,
        },
        "delays": {"tx_delay_factor": 1.0, "direct_tx_delay_factor": 0.5},
        "duty_cycle": {"max_airtime_per_minute": 3600, "enforcement_enabled": True},
        "radio": {
            "spreading_factor": 8,
            "bandwidth": 125000,
            "coding_rate": 8,
            "preamble_length": 17,
        },
    }
    dispatcher = MagicMock()
    dispatcher.radio = MagicMock(
        spreading_factor=8, bandwidth=125000, coding_rate=8,
        preamble_length=17, frequency=915000000, tx_power=14,
    )
    dispatcher.local_identity = MagicMock()
    with (
        patch("repeater.engine.StorageCollector"),
        patch("repeater.engine.RepeaterHandler._start_background_tasks"),
    ):
        from repeater.engine import RepeaterHandler
        h = RepeaterHandler(config, dispatcher, lhb[0], local_hash_bytes=lhb)
    return h


# ===================================================================
# 1. PathUtils — encode/decode round-trips
# ===================================================================


class TestPathUtilsRoundTrip:
    """Verify PathUtils encode/decode for all valid hash sizes and hop counts."""

    @pytest.mark.parametrize("hash_size", [1, 2, 3])
    def test_encode_decode_hash_size(self, hash_size):
        encoded = PathUtils.encode_path_len(hash_size, 0)
        assert PathUtils.get_path_hash_size(encoded) == hash_size
        assert PathUtils.get_path_hash_count(encoded) == 0

    @pytest.mark.parametrize("hash_size,count", [
        (1, 1), (1, 10), (1, 63),
        (2, 1), (2, 15), (2, 32),
        (3, 1), (3, 10), (3, 21),
    ])
    def test_encode_decode_round_trip(self, hash_size, count):
        encoded = PathUtils.encode_path_len(hash_size, count)
        assert PathUtils.get_path_hash_size(encoded) == hash_size
        assert PathUtils.get_path_hash_count(encoded) == count
        assert PathUtils.get_path_byte_len(encoded) == hash_size * count

    @pytest.mark.parametrize("hash_size", [1, 2, 3])
    def test_encode_zero_hops(self, hash_size):
        encoded = PathUtils.encode_path_len(hash_size, 0)
        assert PathUtils.get_path_byte_len(encoded) == 0
        assert PathUtils.is_valid_path_len(encoded)

    def test_encode_preserves_bit_layout(self):
        """Verify the actual bit layout: bits 6-7 = (hash_size-1), bits 0-5 = count."""
        for hs in (1, 2, 3):
            for count in (0, 1, 30, 63):
                if count * hs > MAX_PATH_SIZE:
                    continue
                encoded = PathUtils.encode_path_len(hs, count)
                assert (encoded >> PATH_HASH_SIZE_SHIFT) == hs - 1
                assert (encoded & PATH_HASH_COUNT_MASK) == count

    def test_1_byte_backward_compatible(self):
        """For hash_size=1, encoded byte == raw hop count (legacy compat)."""
        for count in range(64):
            encoded = PathUtils.encode_path_len(1, count)
            assert encoded == count

    def test_encode_hop_count_overflow_raises(self):
        with pytest.raises(ValueError, match="hop count must be 0-63"):
            PathUtils.encode_path_len(1, 64)

    @pytest.mark.parametrize("hash_size,max_hops", [(1, 63), (2, 32), (3, 21)])
    def test_max_hops_boundary(self, hash_size, max_hops):
        at_max = PathUtils.encode_path_len(hash_size, max_hops)
        assert PathUtils.is_path_at_max_hops(at_max)
        assert PathUtils.is_valid_path_len(at_max)

    @pytest.mark.parametrize("hash_size,below_max", [(1, 62), (2, 31), (3, 20)])
    def test_below_max_hops_not_at_max(self, hash_size, below_max):
        encoded = PathUtils.encode_path_len(hash_size, below_max)
        assert not PathUtils.is_path_at_max_hops(encoded)

    def test_zero_path_len_not_at_max(self):
        assert not PathUtils.is_path_at_max_hops(0)

    def test_invalid_path_len_too_many_bytes(self):
        """33 hops of 2 bytes = 66, exceeds MAX_PATH_SIZE=64."""
        encoded = PathUtils.encode_path_len(2, 33)
        assert not PathUtils.is_valid_path_len(encoded)

    def test_hash_size_4_reserved_invalid(self):
        """hash_size=4 (bits 6-7 = 0b11) is reserved and invalid."""
        # Manually construct: (3 << 6) | 1 = 0xC1
        raw = (3 << PATH_HASH_SIZE_SHIFT) | 1
        assert not PathUtils.is_valid_path_len(raw)


# ===================================================================
# 2. Packet — multi-byte path serialization round-trip
# ===================================================================


class TestPacketMultiBytePath:
    """Verify Packet write_to/read_from preserves multi-byte path encoding."""

    def test_1_byte_path_round_trip(self):
        pkt = _make_flood_packet(b"\xAA\xBB\xCC", hash_size=1, hash_count=3)
        wire = pkt.write_to()
        pkt2 = Packet()
        pkt2.read_from(wire)
        assert pkt2.get_path_hash_size() == 1
        assert pkt2.get_path_hash_count() == 3
        assert bytes(pkt2.path) == b"\xAA\xBB\xCC"

    def test_2_byte_path_round_trip(self):
        path = b"\xAA\xBB\xCC\xDD"  # 2 hops of 2 bytes
        pkt = _make_flood_packet(path, hash_size=2, hash_count=2)
        wire = pkt.write_to()
        pkt2 = Packet()
        pkt2.read_from(wire)
        assert pkt2.get_path_hash_size() == 2
        assert pkt2.get_path_hash_count() == 2
        assert bytes(pkt2.path) == path

    def test_3_byte_path_round_trip(self):
        path = b"\xAA\xBB\xCC\xDD\xEE\xFF"  # 2 hops of 3 bytes
        pkt = _make_flood_packet(path, hash_size=3, hash_count=2)
        wire = pkt.write_to()
        pkt2 = Packet()
        pkt2.read_from(wire)
        assert pkt2.get_path_hash_size() == 3
        assert pkt2.get_path_hash_count() == 2
        assert bytes(pkt2.path) == path

    def test_empty_path_2_byte_mode(self):
        pkt = _make_flood_packet(b"", hash_size=2, hash_count=0)
        wire = pkt.write_to()
        pkt2 = Packet()
        pkt2.read_from(wire)
        assert pkt2.get_path_hash_size() == 2
        assert pkt2.get_path_hash_count() == 0
        assert bytes(pkt2.path) == b""

    def test_payload_preserved_after_multibyte_path(self):
        """Payload bytes after a multi-byte path are correctly sliced."""
        payload = b"\xDE\xAD\xBE\xEF"
        path = b"\x11\x22\x33\x44\x55\x66"
        pkt = _make_flood_packet(path, hash_size=3, hash_count=2, payload=payload)
        wire = pkt.write_to()
        pkt2 = Packet()
        pkt2.read_from(wire)
        assert pkt2.get_payload() == payload

    def test_path_len_byte_on_wire(self):
        """The encoded path_len byte on the wire has the correct bit pattern."""
        pkt = _make_flood_packet(b"\x11\x22\x33\x44", hash_size=2, hash_count=2)
        wire = pkt.write_to()
        # Wire: header(1) + path_len(1) + path(4) + payload(4)
        # For ROUTE_TYPE_FLOOD (no transport codes), path_len is at index 1
        path_len_on_wire = wire[1]
        assert PathUtils.get_path_hash_size(path_len_on_wire) == 2
        assert PathUtils.get_path_hash_count(path_len_on_wire) == 2


class TestPacketGetPathHashes:
    """Verify Packet.get_path_hashes splits path into per-hop byte entries."""

    def test_1_byte_hashes(self):
        pkt = _make_flood_packet(b"\xAA\xBB\xCC", hash_size=1, hash_count=3)
        hashes = pkt.get_path_hashes()
        assert hashes == [b"\xAA", b"\xBB", b"\xCC"]

    def test_2_byte_hashes(self):
        pkt = _make_flood_packet(b"\xAA\xBB\xCC\xDD", hash_size=2, hash_count=2)
        hashes = pkt.get_path_hashes()
        assert hashes == [b"\xAA\xBB", b"\xCC\xDD"]

    def test_3_byte_hashes(self):
        pkt = _make_flood_packet(
            b"\xAA\xBB\xCC\xDD\xEE\xFF", hash_size=3, hash_count=2
        )
        hashes = pkt.get_path_hashes()
        assert hashes == [b"\xAA\xBB\xCC", b"\xDD\xEE\xFF"]

    def test_empty_path(self):
        pkt = _make_flood_packet(b"", hash_size=2, hash_count=0)
        assert pkt.get_path_hashes() == []

    def test_hashes_hex_output(self):
        pkt = _make_flood_packet(b"\x0A\x0B\x0C\x0D", hash_size=2, hash_count=2)
        hex_hashes = pkt.get_path_hashes_hex()
        assert hex_hashes == ["0A0B", "0C0D"]


class TestPacketApplyPathHashMode:
    """Verify Packet.apply_path_hash_mode sets encoding for 0-hop packets."""

    @pytest.mark.parametrize("mode,expected_hash_size", [(0, 1), (1, 2), (2, 3)])
    def test_apply_mode_sets_hash_size(self, mode, expected_hash_size):
        pkt = Packet()
        pkt.header = ROUTE_TYPE_FLOOD
        pkt.payload = bytearray(b"\x01")
        pkt.payload_len = 1
        pkt.apply_path_hash_mode(mode)
        assert pkt.get_path_hash_size() == expected_hash_size
        assert pkt.get_path_hash_count() == 0

    def test_apply_mode_skips_nonzero_hop_count(self):
        """Mode should not be re-applied if path already has hops."""
        pkt = _make_flood_packet(b"\xAA\xBB", hash_size=2, hash_count=1)
        original_path_len = pkt.path_len
        pkt.apply_path_hash_mode(0)  # try to override to 1-byte
        assert pkt.path_len == original_path_len  # unchanged

    def test_apply_mode_skips_trace_packets(self):
        """Trace packets should never have path_hash_mode applied."""
        pkt = PacketBuilder.create_trace(tag=1, auth_code=2, flags=0)
        pkt.apply_path_hash_mode(2)
        # Trace packet path_len stays 0 (no routing path)
        assert pkt.path_len == 0

    def test_apply_invalid_mode_raises(self):
        pkt = Packet()
        pkt.header = ROUTE_TYPE_FLOOD
        with pytest.raises(ValueError, match="path_hash_mode must be 0, 1, or 2"):
            pkt.apply_path_hash_mode(3)


class TestPacketSetPath:
    """Verify Packet.set_path with explicit path_len_encoded."""

    def test_set_path_with_encoded_len(self):
        pkt = Packet()
        pkt.header = ROUTE_TYPE_FLOOD
        path = b"\xAA\xBB\xCC\xDD"
        encoded = PathUtils.encode_path_len(2, 2)
        pkt.set_path(path, path_len_encoded=encoded)
        assert pkt.get_path_hash_size() == 2
        assert pkt.get_path_hash_count() == 2
        assert bytes(pkt.path) == path

    def test_set_path_without_encoded_defaults_1_byte(self):
        """Without explicit path_len_encoded, defaults to 1-byte hash_size."""
        pkt = Packet()
        pkt.header = ROUTE_TYPE_FLOOD
        pkt.set_path(b"\xAA\xBB\xCC")
        assert pkt.get_path_hash_size() == 1
        assert pkt.get_path_hash_count() == 3


# ===================================================================
# 3. Engine flood_forward — real Packet objects
# ===================================================================


class TestFloodForwardMultiByte:
    """Test RepeaterHandler.flood_forward with real multi-byte Packet objects."""

    def test_1_byte_mode_appends_single_byte(self):
        h = _make_handler(path_hash_mode=0, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_flood_packet(b"\x11", hash_size=1, hash_count=1)
        result = h.flood_forward(pkt)
        assert result is not None
        assert result.get_path_hash_count() == 2
        assert result.get_path_hash_size() == 1
        hashes = result.get_path_hashes()
        assert hashes[0] == b"\x11"
        assert hashes[1] == b"\xAB"  # first byte of local_hash_bytes

    def test_2_byte_mode_appends_two_bytes(self):
        h = _make_handler(path_hash_mode=1, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_flood_packet(b"\x11\x22", hash_size=2, hash_count=1)
        result = h.flood_forward(pkt)
        assert result is not None
        assert result.get_path_hash_count() == 2
        assert result.get_path_hash_size() == 2
        hashes = result.get_path_hashes()
        assert hashes[0] == b"\x11\x22"
        assert hashes[1] == b"\xAB\xCD"

    def test_3_byte_mode_appends_three_bytes(self):
        h = _make_handler(path_hash_mode=2, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_flood_packet(b"\x11\x22\x33", hash_size=3, hash_count=1)
        result = h.flood_forward(pkt)
        assert result is not None
        assert result.get_path_hash_count() == 2
        assert result.get_path_hash_size() == 3
        hashes = result.get_path_hashes()
        assert hashes[0] == b"\x11\x22\x33"
        assert hashes[1] == b"\xAB\xCD\xEF"

    def test_empty_path_gets_local_hash_appended(self):
        h = _make_handler(path_hash_mode=1, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_flood_packet(b"", hash_size=2, hash_count=0)
        result = h.flood_forward(pkt)
        assert result is not None
        assert result.get_path_hash_count() == 1
        hashes = result.get_path_hashes()
        assert hashes[0] == b"\xAB\xCD"

    def test_path_len_re_encoded_after_forward(self):
        """After appending, path_len byte should encode (hash_size, count+1)."""
        h = _make_handler(path_hash_mode=1, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_flood_packet(b"\x11\x22\x33\x44", hash_size=2, hash_count=2)
        result = h.flood_forward(pkt)
        assert result is not None
        expected_path_len = PathUtils.encode_path_len(2, 3)
        assert result.path_len == expected_path_len

    def test_forwarded_packet_serializes_correctly(self):
        """The forwarded packet should serialize and deserialize cleanly."""
        h = _make_handler(path_hash_mode=1, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_flood_packet(b"\x11\x22", hash_size=2, hash_count=1)
        result = h.flood_forward(pkt)
        assert result is not None
        wire = result.write_to()
        pkt2 = Packet()
        pkt2.read_from(wire)
        assert pkt2.get_path_hash_size() == 2
        assert pkt2.get_path_hash_count() == 2
        assert pkt2.get_path_hashes() == [b"\x11\x22", b"\xAB\xCD"]

    def test_flood_rejects_at_max_hops_2_byte(self):
        """At 32 hops (2-byte mode), flood_forward should drop the packet."""
        h = _make_handler(path_hash_mode=1, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        path = bytes([0x00, 0x01] * 32)  # 32 hops × 2 bytes = 64 bytes
        pkt = _make_flood_packet(path, hash_size=2, hash_count=32)
        result = h.flood_forward(pkt)
        assert result is None
        assert pkt.drop_reason is not None

    def test_flood_rejects_at_max_hops_3_byte(self):
        """At 21 hops (3-byte mode), adding one more would exceed MAX_PATH_SIZE."""
        h = _make_handler(path_hash_mode=2, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        path = bytes([0x00, 0x01, 0x02] * 21)  # 21 hops × 3 bytes = 63 bytes
        pkt = _make_flood_packet(path, hash_size=3, hash_count=21)
        result = h.flood_forward(pkt)
        assert result is None
        assert pkt.drop_reason is not None

    def test_flood_allows_below_max_2_byte(self):
        """At 31 hops (2-byte), one more should succeed."""
        h = _make_handler(path_hash_mode=1, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        path = bytes(range(62))  # 31 hops × 2 bytes
        pkt = _make_flood_packet(path, hash_size=2, hash_count=31)
        result = h.flood_forward(pkt)
        assert result is not None
        assert result.get_path_hash_count() == 32

    def test_flood_rejects_empty_payload(self):
        h = _make_handler(path_hash_mode=0)
        pkt = _make_flood_packet(b"", hash_size=1, hash_count=0, payload=b"")
        pkt.payload = bytearray()
        result = h.flood_forward(pkt)
        assert result is None
        assert "Empty payload" in (pkt.drop_reason or "")


# ===================================================================
# 4. Engine direct_forward — real Packet objects
# ===================================================================


class TestDirectForwardMultiByte:
    """Test RepeaterHandler.direct_forward with real multi-byte Packet objects."""

    def test_1_byte_match_strips_first_hop(self):
        h = _make_handler(path_hash_mode=0, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        # Path: [0xAB, 0x11] — first hop matches local_hash_bytes[0]
        pkt = _make_direct_packet(b"\xAB\x11", hash_size=1, hash_count=2)
        result = h.direct_forward(pkt)
        assert result is not None
        assert result.get_path_hash_count() == 1
        assert result.get_path_hash_size() == 1
        assert result.get_path_hashes() == [b"\x11"]

    def test_2_byte_match_strips_first_hop(self):
        h = _make_handler(path_hash_mode=1, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        # Path: [0xAB,0xCD, 0x11,0x22] — first 2-byte hop matches local_hash_bytes[:2]
        pkt = _make_direct_packet(b"\xAB\xCD\x11\x22", hash_size=2, hash_count=2)
        result = h.direct_forward(pkt)
        assert result is not None
        assert result.get_path_hash_count() == 1
        assert result.get_path_hash_size() == 2
        assert result.get_path_hashes() == [b"\x11\x22"]

    def test_3_byte_match_strips_first_hop(self):
        h = _make_handler(path_hash_mode=2, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_direct_packet(
            b"\xAB\xCD\xEF\x11\x22\x33", hash_size=3, hash_count=2
        )
        result = h.direct_forward(pkt)
        assert result is not None
        assert result.get_path_hash_count() == 1
        assert result.get_path_hash_size() == 3
        assert result.get_path_hashes() == [b"\x11\x22\x33"]

    def test_2_byte_mismatch_rejects(self):
        h = _make_handler(path_hash_mode=1, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        # Path: [0xFF,0xEE, ...] — first 2-byte hop doesn't match
        pkt = _make_direct_packet(b"\xFF\xEE\x11\x22", hash_size=2, hash_count=2)
        result = h.direct_forward(pkt)
        assert result is None
        assert "not for us" in (pkt.drop_reason or "")

    def test_path_len_re_encoded_after_strip(self):
        h = _make_handler(path_hash_mode=1, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_direct_packet(b"\xAB\xCD\x11\x22\x33\x44", hash_size=2, hash_count=3)
        result = h.direct_forward(pkt)
        assert result is not None
        expected_path_len = PathUtils.encode_path_len(2, 2)
        assert result.path_len == expected_path_len

    def test_last_hop_strips_to_empty(self):
        """When only one hop remains and it matches, path becomes empty."""
        h = _make_handler(path_hash_mode=1, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_direct_packet(b"\xAB\xCD", hash_size=2, hash_count=1)
        result = h.direct_forward(pkt)
        assert result is not None
        assert result.get_path_hash_count() == 0
        assert bytes(result.path) == b""

    def test_forwarded_direct_serializes_correctly(self):
        """After stripping, the packet should serialize/deserialize cleanly."""
        h = _make_handler(path_hash_mode=2, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_direct_packet(
            b"\xAB\xCD\xEF\x11\x22\x33\x44\x55\x66", hash_size=3, hash_count=3
        )
        result = h.direct_forward(pkt)
        assert result is not None
        wire = result.write_to()
        pkt2 = Packet()
        pkt2.read_from(wire)
        assert pkt2.get_path_hash_size() == 3
        assert pkt2.get_path_hash_count() == 2
        assert pkt2.get_path_hashes() == [b"\x11\x22\x33", b"\x44\x55\x66"]

    def test_no_path_rejects(self):
        h = _make_handler(path_hash_mode=1, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_direct_packet(b"", hash_size=2, hash_count=0)
        result = h.direct_forward(pkt)
        assert result is None
        assert "no path" in (pkt.drop_reason or "").lower()

    def test_path_too_short_for_hash_size(self):
        """If path has fewer bytes than hash_size, reject."""
        h = _make_handler(path_hash_mode=1, local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_direct_packet(b"\xAB", hash_size=2, hash_count=1)
        # path has 1 byte but hash_size is 2
        result = h.direct_forward(pkt)
        assert result is None


# ===================================================================
# 5. Flood → Direct — multi-hop forwarding chain
# ===================================================================


class TestMultiHopForwardingChain:
    """Simulate a multi-hop path being built and then consumed."""

    def test_flood_chain_builds_path_then_direct_consumes(self):
        """
        Simulate: node_A floods → repeater_1 forwards → repeater_2 forwards
        Then the return direct packet strips hops in reverse order.
        """
        node_a_hash = bytes([0x11, 0x22, 0x33])
        rep1_hash = bytes([0xAA, 0xBB, 0xCC])
        rep2_hash = bytes([0xDD, 0xEE, 0xFF])

        # Step 1: node_A creates a flood packet with 0 hops, 2-byte mode
        pkt = _make_flood_packet(b"", hash_size=2, hash_count=0)

        # Step 2: repeater_1 flood_forward adds its 2-byte hash
        h1 = _make_handler(path_hash_mode=1, local_hash_bytes=rep1_hash)
        pkt = h1.flood_forward(pkt)
        assert pkt is not None
        assert pkt.get_path_hashes() == [rep1_hash[:2]]

        # Step 3: repeater_2 flood_forward adds its 2-byte hash
        h2 = _make_handler(path_hash_mode=1, local_hash_bytes=rep2_hash)
        pkt = h2.flood_forward(pkt)
        assert pkt is not None
        assert pkt.get_path_hashes() == [rep1_hash[:2], rep2_hash[:2]]

        # Verify the path serializes correctly
        wire = pkt.write_to()
        pkt_rx = Packet()
        pkt_rx.read_from(wire)
        assert pkt_rx.get_path_hash_size() == 2
        assert pkt_rx.get_path_hash_count() == 2

        # Step 4: Now simulate a direct reply going back through the path
        # The path should be [rep1, rep2] — direct packet addressed to rep1 first
        # (Direct packets strip from the front)
        direct_pkt = _make_direct_packet(
            bytes(pkt_rx.path), hash_size=2, hash_count=2,
            payload=b"\xFE\xED"
        )

        # repeater_1 strips its hop
        result = h1.direct_forward(direct_pkt)
        assert result is not None
        assert result.get_path_hash_count() == 1
        assert result.get_path_hashes() == [rep2_hash[:2]]

        # repeater_2 strips its hop
        result = h2.direct_forward(result)
        assert result is not None
        assert result.get_path_hash_count() == 0
        assert bytes(result.path) == b""


# ===================================================================
# 6. PacketBuilder.create_trace — payload structure
# ===================================================================


class TestTracePacketStructure:
    """Verify real trace packet creation and payload structure."""

    def test_create_trace_basic(self):
        pkt = PacketBuilder.create_trace(tag=0x12345678, auth_code=0xDEADBEEF, flags=0x01)
        assert pkt.get_payload_type() == PAYLOAD_TYPE_TRACE
        assert pkt.path_len == 0
        assert len(pkt.path) == 0
        payload = pkt.get_payload()
        assert len(payload) == 9
        tag, auth_code, flags = struct.unpack("<IIB", payload[:9])
        assert tag == 0x12345678
        assert auth_code == 0xDEADBEEF
        assert flags == 0x01

    def test_create_trace_with_path_bytes(self):
        """Trace path goes into payload, not routing path."""
        path_bytes = [0xAA, 0xBB, 0xCC, 0xDD]
        pkt = PacketBuilder.create_trace(
            tag=1, auth_code=2, flags=0, path=path_bytes
        )
        payload = pkt.get_payload()
        assert len(payload) == 9 + 4
        # Routing path stays empty
        assert pkt.path_len == 0
        assert len(pkt.path) == 0
        # Path bytes are in the payload after the 9-byte header
        assert list(payload[9:]) == path_bytes

    def test_trace_is_direct_route(self):
        pkt = PacketBuilder.create_trace(tag=0, auth_code=0, flags=0)
        assert pkt.is_route_direct()

    def test_trace_apply_path_hash_mode_is_noop(self):
        """apply_path_hash_mode should not alter trace packets."""
        pkt = PacketBuilder.create_trace(tag=0, auth_code=0, flags=0)
        pkt.apply_path_hash_mode(2)
        assert pkt.path_len == 0  # unchanged

    def test_trace_serialization_round_trip(self):
        path_bytes = [0x11, 0x22, 0x33]
        pkt = PacketBuilder.create_trace(tag=42, auth_code=99, flags=3, path=path_bytes)
        wire = pkt.write_to()
        pkt2 = Packet()
        pkt2.read_from(wire)
        assert pkt2.get_payload_type() == PAYLOAD_TYPE_TRACE
        payload = pkt2.get_payload()
        tag, auth_code, flags = struct.unpack("<IIB", payload[:9])
        assert tag == 42
        assert auth_code == 99
        assert flags == 3
        assert list(payload[9:]) == path_bytes


# ===================================================================
# 7. TraceHandler._parse_trace_payload — real parsing
# ===================================================================


class TestTracePayloadParsing:
    """Verify TraceHandler._parse_trace_payload with real trace payloads."""

    def _make_trace_handler(self):
        handler = object.__new__(TraceHandler)
        return handler

    def test_parse_basic_trace(self):
        th = self._make_trace_handler()
        payload = struct.pack("<IIB", 0x12345678, 0xAABBCCDD, 0x05)
        result = th._parse_trace_payload(payload)
        assert result["valid"]
        assert result["tag"] == 0x12345678
        assert result["auth_code"] == 0xAABBCCDD
        assert result["flags"] == 0x05
        assert result["trace_path"] == []

    def test_parse_trace_with_1_byte_path(self):
        th = self._make_trace_handler()
        payload = struct.pack("<IIB", 1, 2, 0) + bytes([0xAA, 0xBB, 0xCC])
        result = th._parse_trace_payload(payload)
        assert result["valid"]
        assert result["trace_path"] == [0xAA, 0xBB, 0xCC]
        assert result["path_length"] == 3

    def test_parse_trace_with_multibyte_path_grouped_by_flags(self):
        """flags=1 → 2-byte hashes per hop (Mesh.cpp 1<<path_sz)."""
        th = self._make_trace_handler()
        path = bytes([0xAA, 0xBB, 0xCC, 0xDD])
        payload = struct.pack("<IIB", 10, 20, 1) + path
        result = th._parse_trace_payload(payload)
        assert result["valid"]
        assert result["path_hash_width"] == 2
        assert result["trace_hops"] == [b"\xaa\xbb", b"\xcc\xdd"]
        assert result["trace_path"] == [0xAA, 0xCC]  # legacy: first byte per hop
        assert result["path_length"] == 4
        assert result["path_hop_count"] == 2

    def test_parse_from_real_packet(self):
        """Create a trace with PacketBuilder, serialize, deserialize, then parse."""
        th = self._make_trace_handler()
        trace_path = [0x11, 0x22, 0x33, 0x44, 0x55, 0x66]
        pkt = PacketBuilder.create_trace(
            tag=100, auth_code=200, flags=0, path=trace_path
        )
        wire = pkt.write_to()
        pkt2 = Packet()
        pkt2.read_from(wire)
        result = th._parse_trace_payload(pkt2.get_payload())
        assert result["valid"]
        assert result["tag"] == 100
        assert result["auth_code"] == 200
        assert result["flags"] == 0
        assert result["trace_path"] == trace_path
        assert result["path_hop_count"] == 6
        assert result["trace_path_bytes"] == bytes(trace_path)

    def test_parse_too_short_payload(self):
        th = self._make_trace_handler()
        result = th._parse_trace_payload(b"\x01\x02\x03")
        assert "error" in result
        assert "too short" in result["error"].lower()


class TestTraceHelperMultibyte:
    """TraceHelper._should_forward_trace with 2-byte TRACE payload hashes."""

    def test_should_forward_when_next_hop_matches_pubkey_prefix(self):
        from repeater.handler_helpers.trace import TraceHelper

        from pymc_core.protocol import LocalIdentity

        identity = LocalIdentity()
        pub = bytes(identity.get_public_key())
        rh = MagicMock()
        rh.is_duplicate = MagicMock(return_value=False)
        th = TraceHelper(
            local_hash=pub[0],
            repeater_handler=rh,
            local_identity=identity,
        )
        trace_bytes = pub[:2] + b"\x11\x22"
        flags = 1
        hash_width = 2
        pkt = Packet()
        pkt.path = bytearray()
        pkt.path_len = 0
        assert th._should_forward_trace(pkt, trace_bytes, flags, hash_width)

    def test_should_not_forward_when_next_hop_mismatch(self):
        from repeater.handler_helpers.trace import TraceHelper

        from pymc_core.protocol import LocalIdentity

        identity = LocalIdentity()
        pub = bytes(identity.get_public_key())
        rh = MagicMock()
        rh.is_duplicate = MagicMock(return_value=False)
        th = TraceHelper(
            local_hash=pub[0],
            repeater_handler=rh,
            local_identity=identity,
        )
        trace_bytes = bytes([pub[0] ^ 0xFF, pub[1] ^ 0xFF])
        flags = 1
        hash_width = 2
        pkt = Packet()
        pkt.path = bytearray()
        pkt.path_len = 0
        assert not th._should_forward_trace(pkt, trace_bytes, flags, hash_width)


# ===================================================================
# 8. Wire-level verification — manual byte inspection
# ===================================================================


class TestWireLevelEncoding:
    """Verify exact byte layout of serialized multi-byte path packets."""

    def test_2_byte_mode_wire_format(self):
        """
        ROUTE_TYPE_FLOOD (no transport codes):
        [header(1)] [path_len(1)] [path(N)] [payload(M)]
        """
        pkt = _make_flood_packet(
            b"\xAA\xBB\xCC\xDD", hash_size=2, hash_count=2,
            payload=b"\xFE"
        )
        wire = pkt.write_to()
        assert wire[0] == ROUTE_TYPE_FLOOD  # header
        path_len = wire[1]
        assert PathUtils.get_path_hash_size(path_len) == 2
        assert PathUtils.get_path_hash_count(path_len) == 2
        assert wire[2:6] == b"\xAA\xBB\xCC\xDD"  # path bytes
        assert wire[6:] == b"\xFE"  # payload

    def test_3_byte_mode_wire_format(self):
        pkt = _make_flood_packet(
            b"\x11\x22\x33\x44\x55\x66", hash_size=3, hash_count=2,
            payload=b"\xAA"
        )
        wire = pkt.write_to()
        assert wire[0] == ROUTE_TYPE_FLOOD
        path_len = wire[1]
        assert PathUtils.get_path_hash_size(path_len) == 3
        assert PathUtils.get_path_hash_count(path_len) == 2
        assert wire[2:8] == b"\x11\x22\x33\x44\x55\x66"
        assert wire[8:] == b"\xAA"

    def test_1_byte_mode_backward_compat_wire(self):
        """1-byte mode: path_len byte on wire == hop count (legacy format)."""
        pkt = _make_flood_packet(b"\xAA\xBB", hash_size=1, hash_count=2)
        wire = pkt.write_to()
        assert wire[1] == 2  # path_len == hop_count for 1-byte mode

    def test_read_from_2_byte_wire(self):
        """Manually construct wire bytes and verify read_from parses correctly."""
        # header=ROUTE_TYPE_FLOOD, path_len=encode(2, 2), path=4 bytes, payload=2 bytes
        path_len = PathUtils.encode_path_len(2, 2)
        wire = bytes([ROUTE_TYPE_FLOOD, path_len]) + b"\xAA\xBB\xCC\xDD" + b"\xFE\xED"
        pkt = Packet()
        pkt.read_from(wire)
        assert pkt.get_path_hash_size() == 2
        assert pkt.get_path_hash_count() == 2
        assert pkt.get_path_hashes() == [b"\xAA\xBB", b"\xCC\xDD"]
        assert pkt.get_payload() == b"\xFE\xED"
