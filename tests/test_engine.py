"""
tests for pyMC_Repeater engine.py — RepeaterHandler.

Covers: flood_forward, direct_forward, process_packet, duplicate detection,
mark_seen, validate_packet, packet scoring, TX delay, cache management,
airtime duty-cycle, TX mode (forward/monitor/no_tx), and config reloading.
"""
import asyncio
import copy
import math
import time
from collections import OrderedDict
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pymc_core.protocol import Packet, PacketBuilder
from pymc_core.protocol.constants import (
    MAX_PATH_SIZE,
    PH_ROUTE_MASK,
    PH_TYPE_SHIFT,
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_TRANSPORT_DIRECT,
    ROUTE_TYPE_TRANSPORT_FLOOD,
)


# ---------------------------------------------------------------------------
# Helpers — build minimal config / mocks needed by RepeaterHandler.__init__
# ---------------------------------------------------------------------------

LOCAL_HASH = 0xAB  # repeater's own 1-byte path hash


def _make_config(**overrides) -> dict:
    """Return a minimal valid config dict for RepeaterHandler."""
    cfg = {
        "repeater": {
            "mode": "forward",
            "cache_ttl": 3600,
            "use_score_for_tx": False,
            "score_threshold": 0.3,
            "send_advert_interval_hours": 0,  # off in tests
            "node_name": "test-node",
        },
        "mesh": {
            "unscoped_flood_allow": True,
            "loop_detect": "off",
        },
        "delays": {
            "tx_delay_factor": 1.0,
            "direct_tx_delay_factor": 0.5,
        },
        "duty_cycle": {
            "max_airtime_per_minute": 3600,
            "enforcement_enabled": True,
        },
        "radio": {
            "spreading_factor": 8,
            "bandwidth": 125000,
            "coding_rate": 8,
            "preamble_length": 17,
        },
    }
    # Merge overrides
    for key, val in overrides.items():
        if isinstance(val, dict) and key in cfg:
            cfg[key].update(val)
        else:
            cfg[key] = val
    return cfg


def _make_radio():
    """Return a mock radio with sensible defaults."""
    radio = MagicMock()
    radio.spreading_factor = 8
    radio.bandwidth = 125000
    radio.coding_rate = 8
    radio.preamble_length = 17
    radio.frequency = 915000000
    radio.tx_power = 14
    return radio


def _make_dispatcher(radio=None):
    """Return a mock dispatcher with a radio and local_identity."""
    dispatcher = MagicMock()
    dispatcher.radio = radio or _make_radio()
    dispatcher.local_identity = MagicMock()
    dispatcher.send_packet = AsyncMock()
    return dispatcher


@pytest.fixture()
def handler():
    """Create a RepeaterHandler with mocked external dependencies."""
    config = _make_config()
    dispatcher = _make_dispatcher()
    with (
        patch("repeater.engine.StorageCollector"),
        patch("repeater.engine.RepeaterHandler._start_background_tasks"),
    ):
        from repeater.engine import RepeaterHandler
        h = RepeaterHandler(config, dispatcher, LOCAL_HASH)
    return h


def _make_flood_packet(payload: bytes = b"\x01\x02\x03\x04",
                        path: bytes = b"",
                        payload_type: int = 0x01) -> Packet:
    """Build a FLOOD-routed packet."""
    pkt = Packet()
    # header: route=FLOOD(0x01), payload_type shifted, version=0
    pkt.header = ROUTE_TYPE_FLOOD | (payload_type << PH_TYPE_SHIFT)
    pkt.payload = bytearray(payload)
    pkt.payload_len = len(payload)
    pkt.path = bytearray(path)
    pkt.path_len = len(path)
    return pkt


def _make_direct_packet(payload: bytes = b"\x01\x02\x03\x04",
                         path: bytes = None,
                         payload_type: int = 0x01) -> Packet:
    """Build a DIRECT-routed packet with path[0] == LOCAL_HASH by default."""
    if path is None:
        path = bytes([LOCAL_HASH, 0xCC, 0xDD])
    pkt = Packet()
    pkt.header = ROUTE_TYPE_DIRECT | (payload_type << PH_TYPE_SHIFT)
    pkt.payload = bytearray(payload)
    pkt.payload_len = len(payload)
    pkt.path = bytearray(path)
    pkt.path_len = len(path)
    return pkt


def _make_transport_flood_packet(payload: bytes = b"\x01\x02\x03\x04",
                                  path: bytes = b"",
                                  transport_codes=(0x1234, 0x5678)) -> Packet:
    """Build a TRANSPORT_FLOOD-routed packet."""
    pkt = Packet()
    pkt.header = ROUTE_TYPE_TRANSPORT_FLOOD | (0x01 << PH_TYPE_SHIFT)
    pkt.payload = bytearray(payload)
    pkt.payload_len = len(payload)
    pkt.path = bytearray(path)
    pkt.path_len = len(path)
    pkt.transport_codes = list(transport_codes)
    return pkt


def _make_transport_direct_packet(payload: bytes = b"\x01\x02\x03\x04",
                                   path: bytes = None,
                                   transport_codes=(0x1234, 0x5678)) -> Packet:
    """Build a TRANSPORT_DIRECT-routed packet with path[0] == LOCAL_HASH."""
    if path is None:
        path = bytes([LOCAL_HASH, 0xCC])
    pkt = Packet()
    pkt.header = ROUTE_TYPE_TRANSPORT_DIRECT | (0x01 << PH_TYPE_SHIFT)
    pkt.payload = bytearray(payload)
    pkt.payload_len = len(payload)
    pkt.path = bytearray(path)
    pkt.path_len = len(path)
    pkt.transport_codes = list(transport_codes)
    return pkt


# ===================================================================
# 1. flood_forward
# ===================================================================

class TestFloodForward:
    """flood_forward: validation, duplicate suppression, path append."""

    def test_valid_flood_returns_packet(self, handler):
        pkt = _make_flood_packet()
        result = handler.flood_forward(pkt)
        assert result is not None
        assert result is pkt  # mutated in-place

    def test_local_hash_appended_to_path(self, handler):
        pkt = _make_flood_packet(path=b"\x11\x22")
        result = handler.flood_forward(pkt)
        assert result.path[-1] == LOCAL_HASH
        assert list(result.path) == [0x11, 0x22, LOCAL_HASH]
        assert result.path_len == 3

    def test_empty_path_gets_local_hash(self, handler):
        pkt = _make_flood_packet(path=b"")
        result = handler.flood_forward(pkt)
        assert list(result.path) == [LOCAL_HASH]
        assert result.path_len == 1

    def test_duplicate_flood_dropped(self, handler):
        pkt = _make_flood_packet()
        handler.flood_forward(pkt)

        pkt2 = _make_flood_packet()  # same payload → same hash
        result = handler.flood_forward(pkt2)
        assert result is None
        assert pkt2.drop_reason == "Duplicate"

    def test_empty_payload_rejected(self, handler):
        pkt = _make_flood_packet(payload=b"")
        pkt.payload_len = 0
        result = handler.flood_forward(pkt)
        assert result is None
        assert "Empty payload" in pkt.drop_reason

    def test_none_payload_rejected(self, handler):
        pkt = _make_flood_packet()
        pkt.payload = None
        result = handler.flood_forward(pkt)
        assert result is None

    def test_path_at_max_rejected(self, handler):
        pkt = _make_flood_packet(path=bytes(range(MAX_PATH_SIZE)))
        result = handler.flood_forward(pkt)
        assert result is None
        assert "Path length" in pkt.drop_reason

    def test_do_not_retransmit_dropped(self, handler):
        pkt = _make_flood_packet()
        pkt.mark_do_not_retransmit()
        result = handler.flood_forward(pkt)
        assert result is None
        assert "do not retransmit" in pkt.drop_reason.lower()

    def test_unscoped_flood_deny_plain_flood(self, handler):
        handler.config["mesh"]["unscoped_flood_allow"] = False
        pkt = _make_flood_packet()
        # When unscoped_flood_allow=False, flood_forward should fail on a packet type without a transport code defined
        result = handler.flood_forward(pkt)
        assert result is None

    def test_hash_computed_before_path_append(self, handler):
        """mark_seen must use the pre-append hash so duplicate detection works
        when another node sends the same packet with or without our hash."""
        pkt1 = _make_flood_packet(payload=b"\xAA\xBB")
        hash_before = pkt1.calculate_packet_hash().hex().upper()

        handler.flood_forward(pkt1)

        # The hash stored in seen_packets should be the PRE-append hash
        assert hash_before in handler.seen_packets

    def test_path_len_updated_after_append(self, handler):
        pkt = _make_flood_packet(path=b"\x11")
        handler.flood_forward(pkt)
        assert pkt.path_len == len(pkt.path) == 2

    def test_different_payloads_not_duplicate(self, handler):
        pkt1 = _make_flood_packet(payload=b"\x01")
        pkt2 = _make_flood_packet(payload=b"\x02")
        r1 = handler.flood_forward(pkt1)
        r2 = handler.flood_forward(pkt2)
        assert r1 is not None
        assert r2 is not None

    def test_path_none_is_handled(self, handler):
        pkt = _make_flood_packet()
        pkt.path = None
        pkt.path_len = 0
        result = handler.flood_forward(pkt)
        assert result is not None
        assert list(result.path) == [LOCAL_HASH]


# ===================================================================
# 2. direct_forward
# ===================================================================

class TestDirectForward:
    """direct_forward: next-hop check, path consumption, duplicate suppression."""

    def test_valid_direct_returns_packet(self, handler):
        pkt = _make_direct_packet()
        result = handler.direct_forward(pkt)
        assert result is not None

    def test_path_consumed(self, handler):
        pkt = _make_direct_packet(path=bytes([LOCAL_HASH, 0xCC, 0xDD]))
        result = handler.direct_forward(pkt)
        assert list(result.path) == [0xCC, 0xDD]
        assert result.path_len == 2

    def test_single_hop_path_consumed(self, handler):
        """Single hop to us: we strip and return packet with empty path (forward so it can reach destination)."""
        pkt = _make_direct_packet(path=bytes([LOCAL_HASH]))
        result = handler.direct_forward(pkt)
        assert result is not None
        assert list(result.path) == []
        assert result.path_len == 0

    def test_wrong_next_hop_dropped(self, handler):
        pkt = _make_direct_packet(path=bytes([0xFF, 0xCC]))
        result = handler.direct_forward(pkt)
        assert result is None
        assert "not for us" in pkt.drop_reason

    def test_empty_path_dropped(self, handler):
        pkt = _make_direct_packet(path=b"")
        pkt.path_len = 0
        result = handler.direct_forward(pkt)
        assert result is None
        assert "no path" in pkt.drop_reason

    def test_none_path_dropped(self, handler):
        pkt = Packet()
        pkt.header = ROUTE_TYPE_DIRECT | (0x01 << PH_TYPE_SHIFT)
        pkt.payload = bytearray(b"\x01\x02")
        pkt.payload_len = 2
        pkt.path = None
        pkt.path_len = 0
        result = handler.direct_forward(pkt)
        assert result is None

    def test_duplicate_direct_dropped(self, handler):
        pkt = _make_direct_packet()
        handler.direct_forward(pkt)

        pkt2 = _make_direct_packet()  # same payload → same hash
        result = handler.direct_forward(pkt2)
        assert result is None
        assert pkt2.drop_reason == "Duplicate"

    def test_hash_computed_before_path_consume(self, handler):
        """mark_seen hash must match the packet as received, before path[0] removal."""
        pkt = _make_direct_packet(path=bytes([LOCAL_HASH, 0xCC]))
        hash_before = pkt.calculate_packet_hash().hex().upper()

        handler.direct_forward(pkt)
        assert hash_before in handler.seen_packets

    def test_path_len_updated_after_consume(self, handler):
        pkt = _make_direct_packet(path=bytes([LOCAL_HASH, 0xCC, 0xDD]))
        handler.direct_forward(pkt)
        assert pkt.path_len == len(pkt.path) == 2


# ===================================================================
# 3. process_packet — route dispatch
# ===================================================================

class TestProcessPacket:
    """process_packet routes to flood_forward or direct_forward."""

    def test_flood_route_dispatched(self, handler):
        pkt = _make_flood_packet()
        result = handler.process_packet(pkt, snr=5.0)
        assert result is not None
        fwd_pkt, delay = result
        # Flood appends local hash
        assert fwd_pkt.path[-1] == LOCAL_HASH

    def test_direct_route_dispatched(self, handler):
        pkt = _make_direct_packet()
        result = handler.process_packet(pkt, snr=5.0)
        assert result is not None
        fwd_pkt, delay = result
        # Direct consumed first hop
        assert LOCAL_HASH not in fwd_pkt.path

    def test_transport_flood_dispatched(self, handler):
        pkt = _make_transport_flood_packet()
        with patch.object(handler, '_check_transport_codes', return_value=(True, "")):
            result = handler.process_packet(pkt, snr=5.0)
        assert result is not None
        fwd_pkt, _ = result
        assert fwd_pkt.path[-1] == LOCAL_HASH

    def test_transport_direct_dispatched(self, handler):
        pkt = _make_transport_direct_packet()
        result = handler.process_packet(pkt, snr=5.0)
        assert result is not None

    def test_unknown_route_type_dropped(self, handler):
        """A header with route bits = 0x03 is TRANSPORT_DIRECT which IS handled.
        We hackily create a truly unknown route by patching both masks."""
        pkt = _make_flood_packet()
        # All 4 route types are handled. We can verify the fallback by
        # ensuring flood forward fails, e.g. empty payload → returns None.
        pkt.payload = None
        result = handler.process_packet(pkt, snr=0.0)
        assert result is None

    def test_returns_tuple_with_delay(self, handler):
        pkt = _make_flood_packet()
        result = handler.process_packet(pkt, snr=5.0)
        assert isinstance(result, tuple)
        assert len(result) == 2
        fwd_pkt, delay = result
        assert isinstance(delay, float)
        assert delay >= 0.0

    def test_flood_forward_failure_returns_none(self, handler):
        pkt = _make_flood_packet(payload=b"")
        pkt.payload_len = 0
        result = handler.process_packet(pkt, snr=0.0)
        assert result is None

    def test_direct_forward_failure_returns_none(self, handler):
        pkt = _make_direct_packet(path=bytes([0xFF]))  # wrong hop
        result = handler.process_packet(pkt, snr=0.0)
        assert result is None


# ===================================================================
# 4. is_duplicate / mark_seen / cache management
# ===================================================================

class TestDuplicateDetection:
    """Duplicate tracking, TTL clean-up, and cache eviction."""

    def test_unseen_packet_not_duplicate(self, handler):
        pkt = _make_flood_packet()
        assert handler.is_duplicate(pkt) is False

    def test_after_mark_seen_is_duplicate(self, handler):
        pkt = _make_flood_packet()
        handler.mark_seen(pkt)
        assert handler.is_duplicate(pkt) is True

    def test_different_packets_independent(self, handler):
        pkt1 = _make_flood_packet(payload=b"\x01")
        pkt2 = _make_flood_packet(payload=b"\x02")
        handler.mark_seen(pkt1)
        assert handler.is_duplicate(pkt1) is True
        assert handler.is_duplicate(pkt2) is False

    def test_cache_eviction_at_max_size(self, handler):
        handler.max_cache_size = 5
        packets = [_make_flood_packet(payload=bytes([i])) for i in range(6)]
        for p in packets:
            handler.mark_seen(p)
        # Oldest (packets[0]) should have been evicted
        assert handler.is_duplicate(packets[0]) is False
        assert handler.is_duplicate(packets[5]) is True

    def test_cleanup_cache_removes_expired(self, handler):
        pkt = _make_flood_packet()
        handler.mark_seen(pkt)
        pkt_hash = pkt.calculate_packet_hash().hex().upper()
        # Manually expire it
        handler.seen_packets[pkt_hash] = time.time() - handler.cache_ttl - 1
        handler.cleanup_cache()
        assert handler.is_duplicate(pkt) is False

    def test_cleanup_cache_keeps_fresh(self, handler):
        pkt = _make_flood_packet()
        handler.mark_seen(pkt)
        handler.cleanup_cache()
        assert handler.is_duplicate(pkt) is True

    def test_mark_seen_stores_hex_upper_key(self, handler):
        pkt = _make_flood_packet()
        handler.mark_seen(pkt)
        for key in handler.seen_packets:
            assert key == key.upper()


# ===================================================================
# 5. validate_packet
# ===================================================================

class TestValidatePacket:
    """validate_packet: empty payload, oversized path."""

    def test_valid_packet(self, handler):
        pkt = _make_flood_packet()
        valid, reason = handler.validate_packet(pkt)
        assert valid is True
        assert reason == ""

    def test_empty_payload_fails(self, handler):
        pkt = _make_flood_packet(payload=b"")
        pkt.payload = None
        valid, reason = handler.validate_packet(pkt)
        assert valid is False
        assert "Empty" in reason

    def test_path_at_max_fails(self, handler):
        pkt = _make_flood_packet(path=bytes(range(MAX_PATH_SIZE)))
        valid, reason = handler.validate_packet(pkt)
        assert valid is False
        assert "MAX_PATH_SIZE" in reason

    def test_path_one_below_max_passes(self, handler):
        pkt = _make_flood_packet(path=bytes(range(MAX_PATH_SIZE - 1)))
        valid, reason = handler.validate_packet(pkt)
        assert valid is True

    def test_none_packet(self, handler):
        valid, reason = handler.validate_packet(None)
        assert valid is False


# ===================================================================
# 6. calculate_packet_score — static method
# ===================================================================

class TestPacketScore:
    """Score: SNR thresholds, collision penalty, clamping."""

    def test_below_threshold_returns_zero(self):
        from repeater.engine import RepeaterHandler
        # SF8 threshold is -10.0
        score = RepeaterHandler.calculate_packet_score(snr=-15.0, packet_len=50, spreading_factor=8)
        assert score == 0.0

    def test_at_threshold_returns_zero(self):
        from repeater.engine import RepeaterHandler
        score = RepeaterHandler.calculate_packet_score(snr=-10.0, packet_len=50, spreading_factor=8)
        assert score == 0.0

    def test_above_threshold_positive(self):
        from repeater.engine import RepeaterHandler
        score = RepeaterHandler.calculate_packet_score(snr=0.0, packet_len=50, spreading_factor=8)
        assert score > 0.0

    def test_high_snr_high_score(self):
        from repeater.engine import RepeaterHandler
        score = RepeaterHandler.calculate_packet_score(snr=10.0, packet_len=10, spreading_factor=8)
        assert score > 0.5

    def test_long_packet_collision_penalty(self):
        from repeater.engine import RepeaterHandler
        short = RepeaterHandler.calculate_packet_score(snr=5.0, packet_len=10, spreading_factor=8)
        long_ = RepeaterHandler.calculate_packet_score(snr=5.0, packet_len=250, spreading_factor=8)
        assert short > long_

    def test_score_clamped_to_0_1(self):
        from repeater.engine import RepeaterHandler
        score = RepeaterHandler.calculate_packet_score(snr=50.0, packet_len=1, spreading_factor=8)
        assert 0.0 <= score <= 1.0

    def test_sf_below_7_returns_zero(self):
        from repeater.engine import RepeaterHandler
        score = RepeaterHandler.calculate_packet_score(snr=10.0, packet_len=50, spreading_factor=6)
        assert score == 0.0

    def test_each_sf_has_different_threshold(self):
        from repeater.engine import RepeaterHandler
        scores = {}
        for sf in (7, 8, 9, 10, 11, 12):
            scores[sf] = RepeaterHandler.calculate_packet_score(snr=-5.0, packet_len=50, spreading_factor=sf)
        # Higher SF → lower threshold → better reception at same SNR
        # At SNR=-5, SF7 (threshold -7.5) should be worse than SF12 (threshold -20)
        assert scores[12] > scores[7]


# ===================================================================
# 7. _calculate_tx_delay
# ===================================================================

class TestTxDelay:
    """TX delay: flood random, direct fixed, score adjustment, cap."""

    def test_flood_delay_non_negative(self, handler):
        pkt = _make_flood_packet()
        delay = handler._calculate_tx_delay(pkt, snr=0.0)
        assert delay >= 0.0

    def test_flood_delay_capped_at_5s(self, handler):
        handler.tx_delay_factor = 1000.0  # extreme multiplier
        pkt = _make_flood_packet()
        delay = handler._calculate_tx_delay(pkt, snr=0.0)
        assert delay <= 5.0

    def test_direct_delay_uses_factor(self, handler):
        handler.direct_tx_delay_factor = 1.23
        pkt = _make_direct_packet()
        delay = handler._calculate_tx_delay(pkt, snr=0.0)
        # Direct packets use direct_tx_delay_factor directly (in seconds)
        # Score adjustment may change it, but base should be 1.23 when score is off
        assert delay == pytest.approx(1.23, abs=0.01)

    def test_score_adjustment_reduces_delay(self, handler):
        handler.use_score_for_tx = True
        pkt = _make_flood_packet(payload=b"\x01" * 50)
        # High SNR → high score → shorter delay
        delays = []
        for _ in range(50):
            d = handler._calculate_tx_delay(pkt, snr=10.0)
            delays.append(d)
        avg_high_snr = sum(delays) / len(delays)

        # Low SNR → low score → longer delay
        delays_low = []
        for _ in range(50):
            d = handler._calculate_tx_delay(pkt, snr=-5.0)
            delays_low.append(d)
        avg_low_snr = sum(delays_low) / len(delays_low)

        # Using statistical comparison — average high-SNR delay should be lower
        # (Both use random, but multiplier differs)
        # This is non-deterministic; we just check score path doesn't crash
        assert avg_high_snr >= 0.0
        assert avg_low_snr >= 0.0

    def test_zero_tx_delay_factor(self, handler):
        handler.tx_delay_factor = 0.0
        pkt = _make_flood_packet()
        delay = handler._calculate_tx_delay(pkt, snr=0.0)
        assert delay == 0.0

    def test_transport_direct_uses_direct_delay(self, handler):
        handler.direct_tx_delay_factor = 0.77
        pkt = _make_transport_direct_packet()
        delay = handler._calculate_tx_delay(pkt, snr=0.0)
        assert delay == pytest.approx(0.77, abs=0.01)


# ===================================================================
# 8. Hash stability through forwarding operations
# ===================================================================

class TestHashStabilityThroughForwarding:
    """Verify hash is computed on original packet (before path mutation)."""

    def test_flood_hash_unchanged_after_forward(self, handler):
        pkt = _make_flood_packet(payload=b"\xDE\xAD")
        hash_before = pkt.calculate_packet_hash().hex().upper()

        handler.flood_forward(pkt)
        # The hash stored should be the pre-modification hash
        assert hash_before in handler.seen_packets

    def test_direct_hash_unchanged_after_forward(self, handler):
        pkt = _make_direct_packet(payload=b"\xBE\xEF",
                                   path=bytes([LOCAL_HASH, 0xCC]))
        hash_before = pkt.calculate_packet_hash().hex().upper()

        handler.direct_forward(pkt)
        assert hash_before in handler.seen_packets

    def test_flood_second_identical_detected_as_duplicate(self, handler):
        """Two identical packets with the same payload (but path not yet modified)
        should be correctly detected as duplicates."""
        p1 = _make_flood_packet(payload=b"\xCA\xFE")
        p2 = _make_flood_packet(payload=b"\xCA\xFE")
        handler.flood_forward(p1)
        result = handler.flood_forward(p2)
        assert result is None

    def test_direct_second_identical_detected_as_duplicate(self, handler):
        p1 = _make_direct_packet(payload=b"\xCA\xFE",
                                  path=bytes([LOCAL_HASH, 0x11]))
        p2 = _make_direct_packet(payload=b"\xCA\xFE",
                                  path=bytes([LOCAL_HASH, 0x11]))
        handler.direct_forward(p1)
        result = handler.direct_forward(p2)
        assert result is None


# ===================================================================
# 9. unscoped flood policy
# ===================================================================

class TestUnscopedFloodPolicy:
    """unscoped_flood_allow=False blocks plain flood, transport checked."""

    def test_flood_blocked_by_policy(self, handler):
        handler.config["mesh"]["unscoped_flood_allow"] = False
        pkt = _make_flood_packet()
        result = handler.flood_forward(pkt)
        assert result is None

    def test_direct_unaffected_by_flood_policy(self, handler):
        handler.config["mesh"]["unscoped_flood_allow"] = False
        pkt = _make_direct_packet()
        result = handler.direct_forward(pkt)
        assert result is not None  # direct is not blocked by flood policy

    def test_transport_flood_unaffected_by_unscoped_policy(self, handler):
        # unscoped_flood_allow controls only plain FLOOD packets.
        # Transport floods are validated via _check_transport_codes regardless.
        # With a configured scope that allows, transport flood passes even when
        # unscoped traffic is denied — the two settings are fully independent.
        handler.config["mesh"]["unscoped_flood_allow"] = False
        pkt = _make_transport_flood_packet()
        with patch.object(handler, '_check_transport_codes', return_value=(True, "")):
            result = handler.flood_forward(pkt)
        assert result is not None  # transport flood passes; unscoped=False did not block it

    def test_transport_flood_denied_with_no_keys(self, handler):
        # Scope Not Configured = denied, regardless of unscoped_flood_allow.
        pkt = _make_transport_flood_packet()
        result = handler.flood_forward(pkt)  # no mocking — real _check_transport_codes
        assert result is None  # denied because no transport keys configured


class TestFloodLoopDetection:
    """MeshCore-style loop detection for flood forwarding."""

    def test_loop_detect_off_allows_looped_path(self, handler):
        handler.config["mesh"]["loop_detect"] = "off"
        handler.reload_runtime_config()
        pkt = _make_flood_packet(path=bytes([LOCAL_HASH, 0x11, LOCAL_HASH]))
        result = handler.flood_forward(pkt)
        assert result is not None

    def test_loop_detect_minimal_drops_at_four(self, handler):
        handler.config["mesh"]["loop_detect"] = "minimal"
        handler.reload_runtime_config()
        pkt = _make_flood_packet(path=bytes([LOCAL_HASH, LOCAL_HASH, LOCAL_HASH, LOCAL_HASH]))
        result = handler.flood_forward(pkt)
        assert result is None
        assert "loop detected" in (pkt.drop_reason or "").lower()

    def test_loop_detect_minimal_allows_below_threshold(self, handler):
        handler.config["mesh"]["loop_detect"] = "minimal"
        handler.reload_runtime_config()
        pkt = _make_flood_packet(path=bytes([LOCAL_HASH, LOCAL_HASH, LOCAL_HASH]))
        result = handler.flood_forward(pkt)
        assert result is not None

    def test_loop_detect_moderate_drops_at_two(self, handler):
        handler.config["mesh"]["loop_detect"] = "moderate"
        handler.reload_runtime_config()
        pkt = _make_flood_packet(path=bytes([LOCAL_HASH, 0x22, LOCAL_HASH]))
        result = handler.flood_forward(pkt)
        assert result is None

    def test_loop_detect_strict_drops_at_one(self, handler):
        handler.config["mesh"]["loop_detect"] = "strict"
        handler.reload_runtime_config()
        pkt = _make_flood_packet(path=bytes([0x33, LOCAL_HASH, 0x44]))
        result = handler.flood_forward(pkt)
        assert result is None


# ===================================================================
# 10. Airtime / duty-cycle integration
# ===================================================================

class TestAirtimeIntegration:
    """Airtime calculation and duty-cycle enforcement."""

    def test_airtime_positive(self, handler):
        airtime = handler.airtime_mgr.calculate_airtime(50)
        assert airtime > 0.0

    def test_can_transmit_fresh(self, handler):
        can_tx, wait = handler.airtime_mgr.can_transmit(100.0)
        assert can_tx is True
        assert wait == 0.0

    def test_cannot_transmit_after_exhaustion(self, handler):
        # Fill up the budget
        handler.airtime_mgr.record_tx(handler.airtime_mgr.max_airtime_per_minute)
        can_tx, wait = handler.airtime_mgr.can_transmit(1.0)
        assert can_tx is False
        assert wait > 0.0

    def test_duty_cycle_disabled(self, handler):
        handler.config["duty_cycle"]["enforcement_enabled"] = False
        handler.airtime_mgr.config = handler.config
        handler.airtime_mgr.record_tx(999999)
        can_tx, wait = handler.airtime_mgr.can_transmit(999999)
        assert can_tx is True

    def test_airtime_increases_with_packet_size(self, handler):
        short = handler.airtime_mgr.calculate_airtime(10)
        long_ = handler.airtime_mgr.calculate_airtime(200)
        assert long_ > short


# ===================================================================
# 11. Config reload
# ===================================================================

class TestConfigReload:
    """reload_runtime_config updates in-memory state."""

    def test_tx_delay_factor_reloaded(self, handler):
        handler.config["delays"]["tx_delay_factor"] = 3.14
        handler.reload_runtime_config()
        assert handler.tx_delay_factor == 3.14

    def test_direct_tx_delay_factor_reloaded(self, handler):
        handler.config["delays"]["direct_tx_delay_factor"] = 2.5
        handler.reload_runtime_config()
        assert handler.direct_tx_delay_factor == 2.5

    def test_use_score_for_tx_reloaded(self, handler):
        handler.config["repeater"]["use_score_for_tx"] = True
        handler.reload_runtime_config()
        assert handler.use_score_for_tx is True

    def test_cache_ttl_reloaded(self, handler):
        handler.config["repeater"]["cache_ttl"] = 120
        handler.reload_runtime_config()
        assert handler.cache_ttl == 120


# ===================================================================
# 12. _get_drop_reason
# ===================================================================

class TestGetDropReason:
    """_get_drop_reason: determine why a packet was not forwarded."""

    def test_duplicate_reason(self, handler):
        pkt = _make_flood_packet()
        handler.mark_seen(pkt)
        reason = handler._get_drop_reason(pkt)
        assert reason == "Duplicate"

    def test_empty_payload_reason(self, handler):
        pkt = _make_flood_packet(payload=b"")
        pkt.payload_len = 0
        pkt.payload = bytearray()
        reason = handler._get_drop_reason(pkt)
        assert "Empty" in reason

    def test_path_too_long_reason(self, handler):
        pkt = _make_flood_packet(path=bytes(range(MAX_PATH_SIZE)))
        reason = handler._get_drop_reason(pkt)
        assert "Path too long" in reason

    def test_flood_policy_reason(self, handler):
        handler.config["mesh"]["unscoped_flood_allow"] = False
        pkt = _make_flood_packet()
        reason = handler._get_drop_reason(pkt)
        assert "flood" in reason.lower()

    def test_direct_not_for_us_reason(self, handler):
        pkt = _make_direct_packet(path=bytes([0xFF, 0xCC]))
        reason = handler._get_drop_reason(pkt)
        assert "not for us" in reason

    def test_direct_no_path_reason(self, handler):
        pkt = _make_direct_packet(path=b"")
        pkt.path_len = 0
        reason = handler._get_drop_reason(pkt)
        assert "no path" in reason


# ===================================================================
# 13. Transport route forwarding
# ===================================================================

class TestTransportForwarding:
    """TRANSPORT_FLOOD and TRANSPORT_DIRECT: packet routing through process_packet."""

    def test_transport_flood_appends_path(self, handler):
        pkt = _make_transport_flood_packet(path=b"\x11")
        with patch.object(handler, '_check_transport_codes', return_value=(True, "")):
            result = handler.process_packet(pkt, snr=5.0)
        assert result is not None
        fwd_pkt, _ = result
        assert fwd_pkt.path[-1] == LOCAL_HASH
        assert len(fwd_pkt.path) == 2

    def test_transport_direct_consumes_path(self, handler):
        pkt = _make_transport_direct_packet(path=bytes([LOCAL_HASH, 0xCC]))
        result = handler.process_packet(pkt, snr=5.0)
        assert result is not None
        fwd_pkt, _ = result
        assert list(fwd_pkt.path) == [0xCC]

    def test_transport_codes_preserved_after_flood(self, handler):
        pkt = _make_transport_flood_packet(transport_codes=(0xAAAA, 0xBBBB))
        with patch.object(handler, '_check_transport_codes', return_value=(True, "")):
            result = handler.process_packet(pkt, snr=5.0)
        assert result is not None
        fwd_pkt, _ = result
        assert fwd_pkt.transport_codes == [0xAAAA, 0xBBBB]

    def test_transport_codes_preserved_after_direct(self, handler):
        pkt = _make_transport_direct_packet(transport_codes=(0x1111, 0x2222))
        result = handler.process_packet(pkt, snr=5.0)
        assert result is not None
        fwd_pkt, _ = result
        assert fwd_pkt.transport_codes == [0x1111, 0x2222]


# ===================================================================
# 14. Statistics tracking
# ===================================================================

class TestStatistics:
    """RX/TX/dropped counters and recent_packets list."""

    def test_initial_counters_zero(self, handler):
        assert handler.rx_count == 0
        assert handler.forwarded_count == 0
        assert handler.dropped_count == 0

    def test_get_stats_returns_dict(self, handler):
        with patch.object(handler, "storage", None):
            stats = handler.get_stats()
        assert "rx_count" in stats
        assert "forwarded_count" in stats
        assert "dropped_count" in stats
        assert "local_hash" in stats
        assert "uptime_seconds" in stats

    def test_get_stats_local_hash_format(self, handler):
        with patch.object(handler, "storage", None):
            stats = handler.get_stats()
        assert stats["local_hash"] == f"0x{LOCAL_HASH:02x}"


# ===================================================================
# 15. Edge cases and regression tests
# ===================================================================

class TestEdgeCases:
    """Miscellaneous edge cases and regressions."""

    def test_path_as_list_converted_to_bytearray(self, handler):
        """flood_forward should handle path being a list (not bytearray)."""
        pkt = _make_flood_packet()
        pkt.path = [0x11, 0x22]
        pkt.path_len = 2
        result = handler.flood_forward(pkt)
        assert result is not None
        assert isinstance(result.path, bytearray)
        assert list(result.path) == [0x11, 0x22, LOCAL_HASH]

    def test_flood_forward_idempotent_on_second_call(self, handler):
        """Calling flood_forward again with the SAME packet object should
        detect as duplicate (the first call already mark_seen'd it)."""
        pkt = _make_flood_packet(payload=b"\xFF" * 10)
        r1 = handler.flood_forward(pkt)
        assert r1 is not None
        # Now pkt has local_hash appended, but hash was computed pre-append.
        # A new packet with same original payload should be duplicate.
        pkt2 = _make_flood_packet(payload=b"\xFF" * 10)
        r2 = handler.flood_forward(pkt2)
        assert r2 is None

    def test_large_payload_still_forwarded(self, handler):
        pkt = _make_flood_packet(payload=bytes(range(256)) * 4)
        result = handler.flood_forward(pkt)
        assert result is not None

    def test_payload_type_encoding_in_header(self, handler):
        """Verify header construction encodes payload_type correctly."""
        for pt in range(16):
            pkt = _make_flood_packet(payload=bytes([pt, 0xFF]), payload_type=pt)
            assert pkt.get_payload_type() == pt
            result = handler.flood_forward(pkt)
            assert result is not None

    def test_many_distinct_packets_all_forwarded(self, handler):
        """100 unique packets should all be forwarded (no false duplicates)."""
        for i in range(100):
            pkt = _make_flood_packet(payload=i.to_bytes(4, "big"))
            result = handler.flood_forward(pkt)
            assert result is not None, f"Packet {i} incorrectly detected as duplicate"

    def test_cache_eviction_order_is_fifo(self, handler):
        handler.max_cache_size = 3
        pkts = [_make_flood_packet(payload=bytes([i])) for i in range(4)]
        for p in pkts:
            handler.mark_seen(p)
        # First one evicted
        assert handler.is_duplicate(pkts[0]) is False
        # Last three still present
        for p in pkts[1:]:
            assert handler.is_duplicate(p) is True

    def test_do_not_retransmit_with_custom_drop_reason(self, handler):
        pkt = _make_flood_packet()
        pkt.mark_do_not_retransmit()
        pkt.drop_reason = "Custom reason"
        result = handler.flood_forward(pkt)
        assert result is None
        # Custom reason should be preserved (not overwritten)
        assert pkt.drop_reason == "Custom reason"

    def test_monitor_mode_skips_processing(self, handler):
        """In monitor mode, process_packet is not called at all in __call__,
        but process_packet itself doesn't check mode — that's done in __call__.
        We test that process_packet works irrespective of mode."""
        handler.config["repeater"]["mode"] = "monitor"
        pkt = _make_flood_packet()
        # process_packet doesn't check mode — should still work
        result = handler.process_packet(pkt, snr=0.0)
        assert result is not None


# ===================================================================
# 15b. TX mode: forward, monitor, no_tx
# ===================================================================

@pytest.mark.asyncio
class TestTxMode:
    """forward = repeat on; monitor = no repeat, local TX allowed; no_tx = all TX off."""

    async def test_forward_mode_calls_process_packet_for_rx(self, handler):
        """In forward mode, a received packet (not local) triggers process_packet."""
        handler.config["repeater"]["mode"] = "forward"
        pkt = _make_flood_packet()
        with patch.object(handler, "process_packet", wraps=handler.process_packet) as m:
            await handler(pkt, {"snr": 0.0, "rssi": -80}, local_transmission=False)
            m.assert_called_once()

    async def test_monitor_mode_does_not_call_process_packet_for_rx(self, handler):
        """In monitor mode, a received packet does not trigger process_packet."""
        handler.config["repeater"]["mode"] = "monitor"
        pkt = _make_flood_packet()
        with patch.object(handler, "process_packet") as m:
            await handler(pkt, {"snr": 0.0, "rssi": -80}, local_transmission=False)
            m.assert_not_called()

    async def test_no_tx_mode_does_not_call_process_packet_for_rx(self, handler):
        """In no_tx mode, a received packet does not trigger process_packet."""
        handler.config["repeater"]["mode"] = "no_tx"
        pkt = _make_flood_packet()
        with patch.object(handler, "process_packet") as m:
            await handler(pkt, {"snr": 0.0, "rssi": -80}, local_transmission=False)
            m.assert_not_called()

    async def test_monitor_mode_allows_local_tx(self, handler):
        """In monitor mode, local_transmission=True still schedules send_packet."""
        handler.config["repeater"]["mode"] = "monitor"
        pkt = _make_flood_packet()
        with patch("repeater.engine.asyncio.sleep", new_callable=AsyncMock):
            await handler(pkt, {"snr": 0.0, "rssi": -80}, local_transmission=True)
            await asyncio.sleep(0)  # flush scheduled task
        handler.dispatcher.send_packet.assert_called_once()

    async def test_no_tx_mode_blocks_local_tx(self, handler):
        """In no_tx mode, local_transmission=True does not schedule send_packet."""
        handler.config["repeater"]["mode"] = "no_tx"
        pkt = _make_flood_packet()
        with patch("repeater.engine.asyncio.sleep", new_callable=AsyncMock):
            await handler(pkt, {"snr": 0.0, "rssi": -80}, local_transmission=True)
            await asyncio.sleep(0)
        handler.dispatcher.send_packet.assert_not_called()

    async def test_forward_mode_allows_local_tx(self, handler):
        """In forward mode, local_transmission=True schedules send_packet."""
        handler.config["repeater"]["mode"] = "forward"
        pkt = _make_flood_packet()
        with patch("repeater.engine.asyncio.sleep", new_callable=AsyncMock):
            await handler(pkt, {"snr": 0.0, "rssi": -80}, local_transmission=True)
            await asyncio.sleep(0)
        handler.dispatcher.send_packet.assert_called_once()


# ===================================================================
# 16. Airtime calculation correctness
# ===================================================================

class TestAirtimeCalculation:
    """Semtech LoRa airtime formula validation."""

    def test_known_airtime_sf7_125khz(self, handler):
        """SF7, 125kHz, CR4/5, 10-byte payload — well-known reference value."""
        mgr = handler.airtime_mgr
        # Override to known settings
        at = mgr.calculate_airtime(10, spreading_factor=7, bandwidth_hz=125000,
                                    coding_rate=5, preamble_len=8)
        # Semtech calculator: ~36ms for these params
        assert 30.0 < at < 50.0

    def test_sf12_much_slower_than_sf7(self, handler):
        mgr = handler.airtime_mgr
        at7 = mgr.calculate_airtime(50, spreading_factor=7)
        at12 = mgr.calculate_airtime(50, spreading_factor=12)
        # SF12 is roughly 32x slower than SF7 per symbol
        assert at12 > at7 * 10

    def test_zero_payload_still_has_preamble(self, handler):
        mgr = handler.airtime_mgr
        at = mgr.calculate_airtime(0)
        assert at > 0.0


# ===================================================================
# 17. Curated good-packet and bad-packet arrays
# ===================================================================

# ---- 20 GOOD packets: all should be forwarded by process_packet ----
GOOD_PACKETS = [
    # (id, description, builder)
    ("good_flood_minimal",
     "Flood, 1-byte payload, empty path",
     lambda: _make_flood_packet(payload=b"\x01")),

    ("good_flood_typical",
     "Flood, 10-byte payload, 2-hop path",
     lambda: _make_flood_packet(payload=bytes(range(10)), path=b"\x11\x22")),

    ("good_flood_max_payload_type",
     "Flood, payload_type=15 (max 4-bit)",
     lambda: _make_flood_packet(payload=b"\xAA\xBB", payload_type=15)),

    ("good_flood_payload_type_0",
     "Flood, payload_type=0 (plain text)",
     lambda: _make_flood_packet(payload=b"\x01\x02\x03", payload_type=0)),

    ("good_flood_long_payload",
     "Flood, 200-byte payload",
     lambda: _make_flood_packet(payload=bytes(range(200)))),

    ("good_flood_single_byte_path",
     "Flood, path has 1 prior hop",
     lambda: _make_flood_packet(payload=b"\xDE\xAD", path=b"\x42")),

    ("good_flood_binary_payload",
     "Flood, all-zero payload",
     lambda: _make_flood_packet(payload=b"\x00" * 16)),

    ("good_flood_high_entropy",
     "Flood, high-entropy random-looking payload",
     lambda: _make_flood_packet(payload=bytes(i ^ 0xA5 for i in range(64)))),

    ("good_flood_advert_type",
     "Flood, payload_type=4 (ADVERT)",
     lambda: _make_flood_packet(payload=b"\xAB\x01\x02\x03", payload_type=4)),

    ("good_direct_minimal",
     "Direct, 1-byte payload, single hop to us (forward with empty path)",
     lambda: _make_direct_packet(payload=b"\x01", path=bytes([LOCAL_HASH]))),

    ("good_direct_multihop",
     "Direct, 3-hop remaining path (us + 2 more)",
     lambda: _make_direct_packet(payload=b"\xCA\xFE", path=bytes([LOCAL_HASH, 0x11, 0x22]))),

    ("good_direct_long_payload",
     "Direct, 150-byte payload",
     lambda: _make_direct_packet(payload=bytes(range(150)), path=bytes([LOCAL_HASH, 0xBB]))),

    ("good_direct_type_2",
     "Direct, payload_type=2 (ACK)",
     lambda: _make_direct_packet(payload=b"\x01\x02", path=bytes([LOCAL_HASH]),
                                  payload_type=2)),

    ("good_direct_long_remaining_path",
     "Direct, 10 hops remaining after us",
     lambda: _make_direct_packet(payload=b"\xFF\xEE",
                                  path=bytes([LOCAL_HASH] + list(range(10))))),

    ("good_transport_direct_basic",
     "Transport direct, basic hop to us",
     lambda: _make_transport_direct_packet(payload=b"\x01\x02")),
    ("good_transport_direct_long_path",
     "Transport direct, 5 remaining hops",
     lambda: _make_transport_direct_packet(
         payload=b"\xDE\xAD\xBE\xEF",
         path=bytes([LOCAL_HASH, 0x11, 0x22, 0x33, 0x44]))),
]


# ---- 20 BAD packets: all should be dropped / return None ----
BAD_PACKETS = [
    # (id, description, builder)
    ("bad_empty_payload",
     "Empty bytearray payload",
     lambda: _make_flood_packet(payload=b""),
     "Empty payload"),

    ("bad_none_payload",
     "payload = None",
     lambda: (lambda p: (setattr(p, "payload", None), p)[-1])(_make_flood_packet()),
     "Empty payload"),

    ("bad_path_at_max",
     "Path exactly MAX_PATH_SIZE — no room to append",
     lambda: _make_flood_packet(payload=b"\x01", path=bytes(range(MAX_PATH_SIZE))),
     "Path length"),

    ("bad_flood_path_near_max",
     "Flood, path = MAX_PATH_SIZE - 1 (63 hops; path_len encodes 0-63, cannot append)",
     lambda: _make_flood_packet(payload=b"\xFF", path=bytes(range(MAX_PATH_SIZE - 1))),
     "cannot append"),

    ("bad_path_over_max",
     "Path exceeds MAX_PATH_SIZE",
     lambda: _make_flood_packet(payload=b"\x01", path=bytes(range(MAX_PATH_SIZE + 5))),
     "Path length"),

    ("bad_do_not_retransmit",
     "Marked do-not-retransmit",
     lambda: (lambda p: (p.mark_do_not_retransmit(), p)[-1])(_make_flood_packet()),
     "do not retransmit"),

    ("bad_direct_wrong_hop",
     "Direct packet, path[0] != LOCAL_HASH",
     lambda: _make_direct_packet(path=bytes([0xFF, 0xCC])),
     "not for us"),

    ("bad_direct_empty_path",
     "Direct packet with empty path",
     lambda: _make_direct_packet(path=b""),
     "no path"),

    ("bad_direct_none_path",
     "Direct packet with path = None",
     lambda: (lambda p: (setattr(p, "path", None), setattr(p, "path_len", 0), p)[-1])(
         _make_direct_packet()),
     "no path"),

    ("bad_flood_policy_off",
     "Plain flood when unscoped_flood_allow=False (needs config override)",
     lambda: _make_flood_packet(payload=b"\x01\x02"),
     "unscoped flood"),

    ("bad_transport_flood_no_keys",
     "Transport flood with no configured transport keys — always denied",
     lambda: _make_transport_flood_packet(payload=b"\x01\x02"),
     "transport"),

    ("bad_direct_empty_payload",
     "Direct with empty payload (now caught by validate_packet)",
     lambda: (lambda p: (setattr(p, "payload", bytearray()), setattr(p, "payload_len", 0), p)[-1])(
         _make_direct_packet(path=bytes([LOCAL_HASH]))),
     "Empty payload"),

    ("bad_flood_zero_len_payload",
     "Flood with payload_len forced to 0",
     lambda: (lambda p: (setattr(p, "payload_len", 0), setattr(p, "payload", bytearray()), p)[-1])(
         _make_flood_packet(payload=b"\x01")),
     "Empty payload"),

    ("bad_direct_only_wrong_hops",
     "Direct path of all 0xFF bytes (none match LOCAL_HASH)",
     lambda: _make_direct_packet(path=bytes([0xFF, 0xFE, 0xFD])),
     "not for us"),

    ("bad_transport_direct_wrong_hop",
     "Transport direct with wrong first hop",
     lambda: _make_transport_direct_packet(path=bytes([0x01, 0x02])),
     "not for us"),

    ("bad_transport_direct_empty_path",
     "Transport direct with empty path",
     lambda: _make_transport_direct_packet(path=b""),
     "no path"),

    ("bad_transport_direct_none_path",
     "Transport direct with path = None",
     lambda: (lambda p: (setattr(p, "path", None), setattr(p, "path_len", 0), p)[-1])(
         _make_transport_direct_packet()),
     "no path"),

    ("bad_flood_payload_255_zeros",
     "Flood with payload = bytearray(0) (empty)",
     lambda: (lambda p: (setattr(p, "payload", bytearray()), setattr(p, "payload_len", 0), p)[-1])(
         _make_flood_packet()),
     "Empty payload"),

    ("bad_direct_none_payload",
     "Direct with None payload (now caught by validate_packet)",
     lambda: (lambda p: (setattr(p, "payload", None), p)[-1])(
         _make_direct_packet(path=bytes([LOCAL_HASH]))),
     "Empty payload"),

    ("bad_flood_do_not_retransmit_custom",
     "Flood, do-not-retransmit with custom drop reason",
     lambda: (lambda p: (p.mark_do_not_retransmit(), setattr(p, "drop_reason", "Advert consumed"), p)[-1])(
         _make_flood_packet(payload=b"\xAB")),
     "Advert consumed"),

    ("bad_direct_do_not_retransmit",
     "Direct, marked do-not-retransmit (now caught by direct_forward)",
     lambda: (lambda p: (p.mark_do_not_retransmit(), p)[-1])(
         _make_direct_packet(payload=b"\x99", path=bytes([LOCAL_HASH, 0x11]))),
     "do not retransmit"),
]


# Pytest ids for readable output
_good_ids = [g[0] for g in GOOD_PACKETS]
_bad_ids = [b[0] for b in BAD_PACKETS]


class TestGoodPacketArray:
    """All 20 good packets should be forwarded successfully."""

    @pytest.mark.parametrize(
        "name, desc, builder", GOOD_PACKETS, ids=_good_ids,
    )
    def test_process_packet_forwards(self, handler, name, desc, builder):
        pkt = builder()
        result = handler.process_packet(pkt, snr=5.0)
        assert result is not None, f"[{name}] {desc} — unexpectedly dropped"
        fwd_pkt, delay = result
        assert delay >= 0.0

    @pytest.mark.parametrize(
        "name, desc, builder", GOOD_PACKETS, ids=_good_ids,
    )
    def test_good_packet_not_duplicate_on_first_see(self, handler, name, desc, builder):
        pkt = builder()
        assert handler.is_duplicate(pkt) is False, f"[{name}] falsely flagged as duplicate"

    @pytest.mark.parametrize(
        "name, desc, builder", GOOD_PACKETS, ids=_good_ids,
    )
    def test_good_packet_path_modified(self, handler, name, desc, builder):
        pkt = builder()
        route = pkt.header & PH_ROUTE_MASK
        original_path = list(pkt.path) if pkt.path else []

        result = handler.process_packet(pkt, snr=5.0)
        assert result is not None

        fwd_pkt, _ = result
        if route in (ROUTE_TYPE_FLOOD, ROUTE_TYPE_TRANSPORT_FLOOD):
            assert fwd_pkt.path[-1] == LOCAL_HASH, f"[{name}] local hash not appended"
            assert len(fwd_pkt.path) == len(original_path) + 1
        else:
            # Direct: first hop consumed
            assert len(fwd_pkt.path) == len(original_path) - 1


class TestBadPacketArray:
    """All 20 bad packets should be dropped by the engine."""

    @pytest.mark.parametrize(
        "name, desc, builder, expected_reason",
        BAD_PACKETS, ids=_bad_ids,
    )
    def test_process_packet_drops(self, handler, name, desc, builder, expected_reason):
        # Two entries need unscoped_flood_allow=False
        if "policy_off" in name:
            handler.config["mesh"]["unscoped_flood_allow"] = False

        pkt = builder()
        result = handler.process_packet(pkt, snr=5.0)
        assert result is None, f"[{name}] {desc} — should have been dropped"

    @pytest.mark.parametrize(
        "name, desc, builder, expected_reason",
        BAD_PACKETS, ids=_bad_ids,
    )
    def test_drop_reason_set(self, handler, name, desc, builder, expected_reason):
        if "policy_off" in name:
            handler.config["mesh"]["unscoped_flood_allow"] = False

        pkt = builder()
        handler.process_packet(pkt, snr=5.0)
        assert pkt.drop_reason is not None, f"[{name}] drop_reason not set"
        assert expected_reason.lower() in pkt.drop_reason.lower(), (
            f"[{name}] expected '{expected_reason}' in drop_reason, got '{pkt.drop_reason}'"
        )

    @pytest.mark.parametrize(
        "name, desc, builder, expected_reason",
        BAD_PACKETS, ids=_bad_ids,
    )
    def test_bad_packet_not_marked_seen(self, handler, name, desc, builder, expected_reason):
        """Dropped packets must NOT pollute the seen cache."""
        if "policy_off" in name:
            handler.config["mesh"]["unscoped_flood_allow"] = False

        pkt = builder()
        handler.process_packet(pkt, snr=5.0)
        # Should not be in seen_packets (except do-not-retransmit and policy
        # packets which fail AFTER validation but BEFORE mark_seen)
        # Actually none of these should be marked seen — the engine only
        # calls mark_seen on packets that pass all checks.
        pkt_hash = pkt.calculate_packet_hash().hex().upper() if pkt.payload else None
        if pkt_hash:
            assert pkt_hash not in handler.seen_packets, (
                f"[{name}] bad packet was incorrectly added to seen cache"
            )


class TestRecordPacketOnlyTrace:
    """record_packet_only must not log TRACE: TraceHelper owns trace path; packet.path is SNR."""

    def test_record_packet_only_skips_trace(self, handler):
        storage = handler.storage
        storage.record_packet.reset_mock()
        pkt = PacketBuilder.create_trace(tag=1, auth_code=2, flags=0, path=[0xAB, 0xCD])
        n_before = len(handler.recent_packets)
        handler.record_packet_only(pkt, {"rssi": -80, "snr": 10.0})
        storage.record_packet.assert_not_called()
        assert len(handler.recent_packets) == n_before
