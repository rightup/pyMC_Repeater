"""
Tests for flood packet loop detection and duplicate suppression.

Exercises the real RepeaterHandler engine with real pymc_core Packet/PathUtils
objects to verify:
  - Duplicate packet suppression via calculate_packet_hash (SHA256-based)
  - Loop detection modes (off, minimal, moderate, strict) with real path bytes
  - Flood re-forwarding prevention (own hash already in path)
  - Multi-byte hash mode interaction with loop/dedup
  - Unscoped flood policy enforcement
  - mark_seen / is_duplicate cache behaviour
  - do_not_retransmit flag handling
"""
from unittest.mock import MagicMock, patch

import pytest

from pymc_core.protocol import Packet, PathUtils
from pymc_core.protocol.constants import (
    MAX_PATH_SIZE,
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_TRANSPORT_FLOOD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LOCAL_HASH_BYTES = bytes([0xAB, 0xCD, 0xEF])


def _make_flood_packet(
    path_bytes: bytes = b"",
    hash_size: int = 1,
    hash_count: int = 0,
    payload: bytes = b"\x01\x02\x03\x04",
) -> Packet:
    """Create a real flood Packet with the given path encoding."""
    pkt = Packet()
    pkt.header = ROUTE_TYPE_FLOOD
    pkt.path = bytearray(path_bytes)
    pkt.path_len = PathUtils.encode_path_len(hash_size, hash_count)
    pkt.payload = bytearray(payload)
    pkt.payload_len = len(payload)
    return pkt


def _make_handler(
    loop_detect="off",
    path_hash_mode=0,
    local_hash_bytes=None,
    unscoped_flood_allow=True,
):
    """Create a RepeaterHandler with real engine logic, mocking only hardware."""
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
            "unscoped_flood_allow": unscoped_flood_allow,
            "loop_detect": loop_detect,
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
        spreading_factor=8,
        bandwidth=125000,
        coding_rate=8,
        preamble_length=17,
        frequency=915000000,
        tx_power=14,
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
# 1. Duplicate suppression — real packet hash (SHA256)
# ===================================================================


class TestDuplicateSuppression:
    """Verify duplicate packets are detected via real calculate_packet_hash."""

    def test_same_packet_forwarded_twice_is_duplicate(self):
        """Forwarding the same packet a second time must be rejected as duplicate."""
        h = _make_handler()
        pkt1 = _make_flood_packet(payload=b"\xDE\xAD")
        result1 = h.flood_forward(pkt1)
        assert result1 is not None

        # Same content in a fresh Packet object
        pkt2 = _make_flood_packet(payload=b"\xDE\xAD")
        result2 = h.flood_forward(pkt2)
        assert result2 is None
        assert pkt2.drop_reason == "Duplicate"

    def test_different_payload_not_duplicate(self):
        """Packets with different payloads have different hashes."""
        h = _make_handler()
        pkt1 = _make_flood_packet(payload=b"\x01\x02")
        assert h.flood_forward(pkt1) is not None

        pkt2 = _make_flood_packet(payload=b"\x03\x04")
        assert h.flood_forward(pkt2) is not None

    def test_mark_seen_makes_is_duplicate_true(self):
        """mark_seen records the hash; is_duplicate finds it."""
        h = _make_handler()
        pkt = _make_flood_packet(payload=b"\xAA\xBB")
        assert not h.is_duplicate(pkt)
        h.mark_seen(pkt)
        assert h.is_duplicate(pkt)

    def test_packet_hash_uses_real_sha256(self):
        """Verify the hash comes from real Packet.calculate_packet_hash."""
        pkt = _make_flood_packet(payload=b"\x01\x02\x03")
        hash_bytes = pkt.calculate_packet_hash()
        assert isinstance(hash_bytes, bytes)
        assert len(hash_bytes) > 0
        # Same content → same hash
        pkt2 = _make_flood_packet(payload=b"\x01\x02\x03")
        assert pkt2.calculate_packet_hash() == hash_bytes

    def test_different_path_same_payload_same_hash(self):
        """
        Packet hash is based on payload_type + payload (not path),
        except for TRACE packets. Two flood packets with different paths
        but same payload have the same hash.
        """
        pkt_a = _make_flood_packet(path_bytes=b"\x11", hash_size=1, hash_count=1,
                                    payload=b"\xFF")
        pkt_b = _make_flood_packet(path_bytes=b"\x22", hash_size=1, hash_count=1,
                                    payload=b"\xFF")
        assert pkt_a.calculate_packet_hash() == pkt_b.calculate_packet_hash()

    def test_seen_cache_eviction(self):
        """When cache exceeds max_cache_size, oldest entries are evicted."""
        h = _make_handler()
        h.max_cache_size = 3

        packets = [_make_flood_packet(payload=bytes([i, i + 1])) for i in range(5)]
        for p in packets:
            h.mark_seen(p)

        # Oldest entries (0, 1) should have been evicted
        assert not h.is_duplicate(packets[0])
        assert not h.is_duplicate(packets[1])
        # Recent entries still present
        assert h.is_duplicate(packets[2])
        assert h.is_duplicate(packets[3])
        assert h.is_duplicate(packets[4])


# ===================================================================
# 2. Loop detection modes — 1-byte hash
# ===================================================================


class TestLoopDetection1Byte:
    """Loop detection with 1-byte hash paths (mode 0)."""

    def test_loop_detect_off_allows_own_hash(self):
        """With loop_detect=off, packet with our hash in path is forwarded."""
        h = _make_handler(loop_detect="off",
                          local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        # Path contains our 1-byte hash (0xAB) once
        pkt = _make_flood_packet(b"\xAB", hash_size=1, hash_count=1)
        result = h.flood_forward(pkt)
        assert result is not None

    def test_loop_detect_strict_blocks_single_occurrence(self):
        """strict mode (threshold=1): one occurrence of our hash → loop."""
        h = _make_handler(loop_detect="strict",
                          local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_flood_packet(b"\xAB", hash_size=1, hash_count=1)
        result = h.flood_forward(pkt)
        assert result is None
        assert "loop" in pkt.drop_reason.lower()

    def test_loop_detect_moderate_allows_one_occurrence(self):
        """moderate mode (threshold=2): one occurrence is fine."""
        h = _make_handler(loop_detect="moderate",
                          local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_flood_packet(b"\x11\xAB", hash_size=1, hash_count=2)
        result = h.flood_forward(pkt)
        assert result is not None

    def test_loop_detect_moderate_blocks_two_occurrences(self):
        """moderate mode (threshold=2): two occurrences → loop."""
        h = _make_handler(loop_detect="moderate",
                          local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_flood_packet(b"\xAB\x11\xAB", hash_size=1, hash_count=3)
        result = h.flood_forward(pkt)
        assert result is None
        assert "loop" in pkt.drop_reason.lower()

    def test_loop_detect_minimal_allows_three_occurrences(self):
        """minimal mode (threshold=4): three occurrences still OK."""
        h = _make_handler(loop_detect="minimal",
                          local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_flood_packet(b"\xAB\x11\xAB\x22\xAB", hash_size=1, hash_count=5)
        result = h.flood_forward(pkt)
        assert result is not None

    def test_loop_detect_minimal_blocks_four_occurrences(self):
        """minimal mode (threshold=4): four occurrences → loop."""
        h = _make_handler(loop_detect="minimal",
                          local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_flood_packet(
            b"\xAB\x11\xAB\x22\xAB\x33\xAB", hash_size=1, hash_count=7
        )
        result = h.flood_forward(pkt)
        assert result is None
        assert "loop" in pkt.drop_reason.lower()

    def test_loop_detect_no_match_passes(self):
        """Strict mode still passes if our hash is not in the path."""
        h = _make_handler(loop_detect="strict",
                          local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_flood_packet(b"\x11\x22\x33", hash_size=1, hash_count=3)
        result = h.flood_forward(pkt)
        assert result is not None


# ===================================================================
# 3. Own-hash re-forwarding prevention
# ===================================================================


class TestOwnHashReForwarding:
    """
    After a repeater flood_forwards a packet (appending its own hash),
    receiving that same packet back should be handled correctly.
    """

    def test_forward_then_receive_again_is_duplicate(self):
        """
        After forwarding, the packet hash is in seen_packets.
        Receiving an identical packet is a duplicate.
        """
        h = _make_handler(loop_detect="off")
        pkt = _make_flood_packet(b"\x11", hash_size=1, hash_count=1,
                                  payload=b"\xAA\xBB")
        result = h.flood_forward(pkt)
        assert result is not None
        # The original packet's payload hash was marked seen

        # Receiving same original packet again (before our hop was appended)
        pkt2 = _make_flood_packet(b"\x11", hash_size=1, hash_count=1,
                                   payload=b"\xAA\xBB")
        result2 = h.flood_forward(pkt2)
        assert result2 is None
        assert pkt2.drop_reason == "Duplicate"

    def test_strict_detects_own_hash_after_flood_chain(self):
        """
        If the forwarded packet (with our hash appended) loops back to us,
        strict mode detects our hash in the path.
        """
        our_hash = bytes([0xAB, 0xCD, 0xEF])
        h = _make_handler(loop_detect="strict", local_hash_bytes=our_hash)

        # Original packet arrives, we forward (appending 0xAB)
        pkt = _make_flood_packet(b"\x11", hash_size=1, hash_count=1,
                                  payload=b"\xDD\xEE")
        result = h.flood_forward(pkt)
        assert result is not None
        # Now path is [0x11, 0xAB], and this exact payload is in seen_packets

        # Suppose another node re-forwards it with an additional hop,
        # so it's a new payload in the packet hash sense (different path iteration)
        # but path contains our hash 0xAB
        looped_pkt = _make_flood_packet(
            b"\x11\xAB\x22", hash_size=1, hash_count=3,
            payload=b"\xDD\xEE\xFF"  # different payload → not a duplicate
        )
        result2 = h.flood_forward(looped_pkt)
        assert result2 is None
        assert "loop" in looped_pkt.drop_reason.lower()

    def test_off_mode_still_catches_duplicate_after_own_forward(self):
        """Even with loop_detect=off, duplicate suppression still works."""
        h = _make_handler(loop_detect="off")
        pkt = _make_flood_packet(payload=b"\x42\x43")
        assert h.flood_forward(pkt) is not None

        pkt2 = _make_flood_packet(payload=b"\x42\x43")
        result = h.flood_forward(pkt2)
        assert result is None
        assert pkt2.drop_reason == "Duplicate"


# ===================================================================
# 4. Multi-byte hash + loop detection
# ===================================================================


class TestLoopDetectionMultiByte:
    """
    Loop detection currently counts byte-level matches against local_hash
    (single int). In multi-byte mode the per-hop hash is >1 byte, so
    individual bytes in the path may coincidentally match.
    These tests verify the actual engine behaviour.
    """

    def test_2_byte_mode_strict_byte_level_match(self):
        """
        In 2-byte mode with strict, _is_flood_looped scans individual bytes.
        If local_hash (0xAB) appears as a byte anywhere in the 2-byte path
        entries, it counts as a match.
        """
        h = _make_handler(loop_detect="strict",
                          local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        # Path: 2-byte hop [0xAB, 0x11] — byte 0xAB appears once
        pkt = _make_flood_packet(b"\xAB\x11", hash_size=2, hash_count=1)
        result = h.flood_forward(pkt)
        # strict threshold=1, 0xAB appears once in raw bytes → loop detected
        assert result is None
        assert "loop" in pkt.drop_reason.lower()

    def test_2_byte_mode_off_ignores_byte_match(self):
        """With loop_detect=off, even byte-level 0xAB matches are ignored."""
        h = _make_handler(loop_detect="off",
                          local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_flood_packet(b"\xAB\x11", hash_size=2, hash_count=1)
        result = h.flood_forward(pkt)
        assert result is not None

    def test_2_byte_no_local_hash_byte_passes_strict(self):
        """If local_hash byte doesn't appear anywhere in the 2-byte path, strict passes."""
        h = _make_handler(loop_detect="strict",
                          local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        # Path: [0x11, 0x22] — no 0xAB byte
        pkt = _make_flood_packet(b"\x11\x22", hash_size=2, hash_count=1)
        result = h.flood_forward(pkt)
        assert result is not None

    def test_3_byte_mode_local_hash_byte_in_path(self):
        """In 3-byte mode, the 0xAB byte anywhere triggers strict loop detection."""
        h = _make_handler(loop_detect="strict",
                          local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        # 3-byte hop: [0x11, 0xAB, 0x33] — 0xAB in the middle
        pkt = _make_flood_packet(b"\x11\xAB\x33", hash_size=3, hash_count=1)
        result = h.flood_forward(pkt)
        assert result is None

    def test_moderate_multi_byte_counts_all_byte_occurrences(self):
        """
        moderate threshold=2. With 2-byte hops, each byte is counted
        independently, so two occurrences of 0xAB across different hops
        triggers the loop.
        """
        h = _make_handler(loop_detect="moderate",
                          local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        # Two 2-byte hops: [0xAB, 0x11, 0xAB, 0x22] — 0xAB appears twice
        pkt = _make_flood_packet(b"\xAB\x11\xAB\x22", hash_size=2, hash_count=2)
        result = h.flood_forward(pkt)
        assert result is None
        assert "loop" in pkt.drop_reason.lower()

    def test_2_byte_flood_forward_appends_correctly(self):
        """
        After flood_forward in 2-byte mode, verify the path contains
        only the expected bytes (no extra, no corruption).
        """
        h = _make_handler(loop_detect="off",
                          local_hash_bytes=bytes([0xAB, 0xCD, 0xEF]))
        pkt = _make_flood_packet(b"\x11\x22", hash_size=2, hash_count=1)
        result = h.flood_forward(pkt)
        assert result is not None
        assert result.get_path_hash_count() == 2
        hashes = result.get_path_hashes()
        assert hashes == [b"\x11\x22", b"\xAB\xCD"]


# ===================================================================
# 5. Unscoped flood policy
# ===================================================================


class TestUnscopedFloodPolicy:
    """Test unscoped=False blocks flood packets."""

    def test_unscoped_flood_disabled_drops_flood(self):
        h = _make_handler(unscoped_flood_allow=False)
        pkt = _make_flood_packet(payload=b"\x01\x02")
        result = h.flood_forward(pkt)
        assert result is None
        assert pkt.drop_reason is not None

    def test_unscoped_flood_enabled_allows_flood(self):
        h = _make_handler(unscoped_flood_allow=True)
        pkt = _make_flood_packet(payload=b"\x01\x02")
        result = h.flood_forward(pkt)
        assert result is not None

    def test_transport_flood_without_codes_drops(self):
        """ROUTE_TYPE_TRANSPORT_FLOOD with unscoped_flood_allow=False and no valid codes."""
        h = _make_handler(unscoped_flood_allow=False)
        # Nullify the storage to ensure transport code check fails
        h.storage = None
        pkt = Packet()
        pkt.header = ROUTE_TYPE_TRANSPORT_FLOOD
        pkt.path = bytearray()
        pkt.path_len = PathUtils.encode_path_len(1, 0)
        pkt.payload = bytearray(b"\x01\x02")
        pkt.payload_len = 2
        pkt.transport_codes = [0x1234, 0x5678]
        result = h.flood_forward(pkt)
        assert result is None


# ===================================================================
# 6. do_not_retransmit flag
# ===================================================================


class TestDoNotRetransmit:
    """Verify packets flagged do_not_retransmit are dropped."""

    def test_flagged_packet_dropped(self):
        h = _make_handler()
        pkt = _make_flood_packet(payload=b"\x01\x02")
        pkt.mark_do_not_retransmit()
        result = h.flood_forward(pkt)
        assert result is None
        assert "retransmit" in pkt.drop_reason.lower()

    def test_unflagged_packet_passes(self):
        h = _make_handler()
        pkt = _make_flood_packet(payload=b"\x01\x02")
        assert not pkt.is_marked_do_not_retransmit()
        result = h.flood_forward(pkt)
        assert result is not None


# ===================================================================
# 7. validate_packet edge cases
# ===================================================================


class TestValidatePacket:
    """Test packet validation rules used by flood_forward."""

    def test_empty_payload_rejected(self):
        h = _make_handler()
        pkt = _make_flood_packet(payload=b"")
        pkt.payload = bytearray()
        result = h.flood_forward(pkt)
        assert result is None
        assert "Empty payload" in (pkt.drop_reason or "")

    def test_max_path_size_rejected(self):
        """Path at MAX_PATH_SIZE (64 bytes) is rejected before forwarding."""
        h = _make_handler()
        pkt = Packet()
        pkt.header = ROUTE_TYPE_FLOOD
        pkt.path = bytearray(range(64))
        # Manually set path_len to bypass encode_path_len validation
        pkt.path_len = PathUtils.encode_path_len(1, 63)
        pkt.payload = bytearray(b"\x01\x02")
        pkt.payload_len = 2
        # validate_packet checks len(path) >= MAX_PATH_SIZE
        result = h.flood_forward(pkt)
        assert result is None

    def test_none_payload_rejected(self):
        h = _make_handler()
        pkt = Packet()
        pkt.header = ROUTE_TYPE_FLOOD
        pkt.payload = None
        result = h.flood_forward(pkt)
        assert result is None

    def test_hop_count_63_rejected(self):
        """At 63 hops (1-byte mode), appending would overflow 6-bit counter."""
        h = _make_handler()
        # 63 bytes of path with hash_count=63
        pkt = _make_flood_packet(bytes(63), hash_size=1, hash_count=63)
        result = h.flood_forward(pkt)
        assert result is None
        assert "maximum" in (pkt.drop_reason or "").lower() or \
               "exceed" in (pkt.drop_reason or "").lower()


# ===================================================================
# 8. Serialization round-trip after dedup/loop decisions
# ===================================================================


class TestSerializationAfterForward:
    """
    Verify packets that survive loop/dedup checks can serialize
    and deserialize correctly, preserving the appended path.
    """

    def test_forwarded_1_byte_round_trips(self):
        h = _make_handler(loop_detect="moderate",
                          local_hash_bytes=bytes([0x42, 0x00, 0x00]))
        pkt = _make_flood_packet(b"\x11\x22", hash_size=1, hash_count=2,
                                  payload=b"\xAA\xBB")
        result = h.flood_forward(pkt)
        assert result is not None
        wire = result.write_to()
        pkt2 = Packet()
        pkt2.read_from(wire)
        assert pkt2.get_path_hash_count() == 3
        assert pkt2.get_path_hashes() == [b"\x11", b"\x22", b"\x42"]
        assert pkt2.get_payload() == b"\xAA\xBB"

    def test_forwarded_2_byte_round_trips(self):
        h = _make_handler(loop_detect="off",
                          local_hash_bytes=bytes([0xAA, 0xBB, 0xCC]))
        pkt = _make_flood_packet(b"\x11\x22", hash_size=2, hash_count=1,
                                  payload=b"\xDE\xAD")
        result = h.flood_forward(pkt)
        assert result is not None
        wire = result.write_to()
        pkt2 = Packet()
        pkt2.read_from(wire)
        assert pkt2.get_path_hash_size() == 2
        assert pkt2.get_path_hash_count() == 2
        assert pkt2.get_path_hashes() == [b"\x11\x22", b"\xAA\xBB"]

    def test_forwarded_3_byte_round_trips(self):
        h = _make_handler(loop_detect="off",
                          local_hash_bytes=bytes([0xAA, 0xBB, 0xCC]))
        pkt = _make_flood_packet(b"\x11\x22\x33", hash_size=3, hash_count=1,
                                  payload=b"\xBE\xEF")
        result = h.flood_forward(pkt)
        assert result is not None
        wire = result.write_to()
        pkt2 = Packet()
        pkt2.read_from(wire)
        assert pkt2.get_path_hash_size() == 3
        assert pkt2.get_path_hash_count() == 2
        assert pkt2.get_path_hashes() == [b"\x11\x22\x33", b"\xAA\xBB\xCC"]


# ===================================================================
# 9. Multi-repeater flood chain with loop detection
# ===================================================================


class TestFloodChainLoopDetection:
    """
    Simulate a flood packet traversing multiple repeaters and verify
    loop detection works across the chain.
    """

    def test_three_repeater_chain_no_loop(self):
        """A→B→C with distinct hashes: no loop at any step (strict mode)."""
        hashes = [
            bytes([0x11, 0x00, 0x00]),
            bytes([0x22, 0x00, 0x00]),
            bytes([0x33, 0x00, 0x00]),
        ]
        handlers = [_make_handler(loop_detect="strict", local_hash_bytes=h) for h in hashes]

        pkt = _make_flood_packet(payload=b"\xFE\xED")
        for i, h in enumerate(handlers):
            result = h.flood_forward(pkt)
            assert result is not None, f"repeater {i} unexpectedly dropped packet"
            # Use different payload to avoid dedup between handlers
            # (In real life they'd be on different nodes)
            pkt = _make_flood_packet(
                path_bytes=bytes(result.path),
                hash_size=1,
                hash_count=result.get_path_hash_count(),
                payload=b"\xFE\xED" + bytes([i + 1]),
            )

        assert pkt.get_path_hash_count() == 3

    def test_circular_topology_strict_blocks_loop(self):
        """
        A→B→C→A: when the packet returns to A, strict mode detects
        A's hash (0x11) already in the path.
        """
        hash_a = bytes([0x11, 0x00, 0x00])
        hash_b = bytes([0x22, 0x00, 0x00])
        hash_c = bytes([0x33, 0x00, 0x00])

        h_a = _make_handler(loop_detect="strict", local_hash_bytes=hash_a)
        h_b = _make_handler(loop_detect="strict", local_hash_bytes=hash_b)
        h_c = _make_handler(loop_detect="strict", local_hash_bytes=hash_c)

        # A originates
        pkt = _make_flood_packet(payload=b"\x01\x02\x03")
        pkt = h_a.flood_forward(pkt)
        assert pkt is not None  # path: [0x11]

        # B forwards (new payload to avoid dedup)
        pkt_b = _make_flood_packet(
            bytes(pkt.path), hash_size=1,
            hash_count=pkt.get_path_hash_count(),
            payload=b"\x01\x02\x03\x04",
        )
        pkt_b = h_b.flood_forward(pkt_b)
        assert pkt_b is not None  # path: [0x11, 0x22]

        # C forwards
        pkt_c = _make_flood_packet(
            bytes(pkt_b.path), hash_size=1,
            hash_count=pkt_b.get_path_hash_count(),
            payload=b"\x01\x02\x03\x04\x05",
        )
        pkt_c = h_c.flood_forward(pkt_c)
        assert pkt_c is not None  # path: [0x11, 0x22, 0x33]

        # Back to A — 0x11 is already in path → strict blocks it
        pkt_a2 = _make_flood_packet(
            bytes(pkt_c.path), hash_size=1,
            hash_count=pkt_c.get_path_hash_count(),
            payload=b"\x01\x02\x03\x04\x05\x06",
        )
        result = h_a.flood_forward(pkt_a2)
        assert result is None
        assert "loop" in pkt_a2.drop_reason.lower()

    def test_circular_topology_off_allows_loop_but_dedup_catches(self):
        """
        With loop_detect=off and the exact same payload, duplicate
        suppression catches the re-visit even without loop detection.
        """
        h = _make_handler(loop_detect="off", local_hash_bytes=bytes([0x11, 0x00, 0x00]))

        pkt = _make_flood_packet(payload=b"\xAA\xBB")
        assert h.flood_forward(pkt) is not None

        # Same payload comes back
        pkt2 = _make_flood_packet(payload=b"\xAA\xBB")
        result = h.flood_forward(pkt2)
        assert result is None
        assert pkt2.drop_reason == "Duplicate"

    def test_two_byte_chain_loop_detected(self):
        """2-byte mode: circular path A→B→A detected via byte-level scan."""
        hash_a = bytes([0xAA, 0xBB, 0x00])
        hash_b = bytes([0xCC, 0xDD, 0x00])

        h_a = _make_handler(loop_detect="strict", local_hash_bytes=hash_a)
        h_b = _make_handler(loop_detect="strict", local_hash_bytes=hash_b)

        # A forwards (2-byte mode)
        pkt = _make_flood_packet(b"", hash_size=2, hash_count=0, payload=b"\x01\x02")
        pkt = h_a.flood_forward(pkt)
        assert pkt is not None  # path: [0xAA, 0xBB]

        # B forwards
        pkt_b = _make_flood_packet(
            bytes(pkt.path), hash_size=2,
            hash_count=pkt.get_path_hash_count(),
            payload=b"\x01\x02\x03",
        )
        pkt_b = h_b.flood_forward(pkt_b)
        assert pkt_b is not None  # path: [0xAA, 0xBB, 0xCC, 0xDD]

        # Back to A — byte 0xAA is in path → strict detects it
        pkt_a2 = _make_flood_packet(
            bytes(pkt_b.path), hash_size=2,
            hash_count=pkt_b.get_path_hash_count(),
            payload=b"\x01\x02\x03\x04",
        )
        result = h_a.flood_forward(pkt_a2)
        assert result is None
        assert "loop" in pkt_a2.drop_reason.lower()


# ===================================================================
# 10. Normalize loop_detect_mode edge cases
# ===================================================================


class TestNormalizeLoopDetectMode:
    """Verify _normalize_loop_detect_mode handles edge cases."""

    def test_uppercase_normalized(self):
        h = _make_handler(loop_detect="STRICT")
        assert h.loop_detect_mode == "strict"

    def test_whitespace_stripped(self):
        h = _make_handler(loop_detect="  moderate  ")
        assert h.loop_detect_mode == "moderate"

    def test_invalid_defaults_to_off(self):
        h = _make_handler(loop_detect="invalid_value")
        assert h.loop_detect_mode == "off"

    def test_numeric_defaults_to_off(self):
        h = _make_handler(loop_detect=42)
        assert h.loop_detect_mode == "off"

    def test_none_defaults_to_off(self):
        h = _make_handler(loop_detect=None)
        assert h.loop_detect_mode == "off"
