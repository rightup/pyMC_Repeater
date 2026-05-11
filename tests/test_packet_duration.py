"""Tests for the MQTT packet `duration` field.

The duration value published to MQTT must match the LoRa Semtech time-on-air
formula computed from the active radio settings, just like the firmware MQTT
output. This file locks that contract and the backward-compatible default.
"""

import math

from repeater.airtime import AirtimeManager
from repeater.data_acquisition.storage_utils import PacketRecord


def _semtech_airtime_ms(payload_len: int, sf: int, bw_hz: int, cr: int, preamble: int) -> float:
    """Reference implementation copied verbatim from the Semtech LoRa
    calculator so we can compare against the production code without trusting
    its own implementation as the oracle.
    """
    crc = 1
    h = 0  # explicit header
    de = 1 if (sf >= 11 and bw_hz <= 125000) else 0
    t_sym = (2 ** sf) / (bw_hz / 1000)
    t_preamble = (preamble + 4.25) * t_sym
    numerator = max(8 * payload_len - 4 * sf + 28 + 16 * crc - 20 * h, 0)
    denominator = 4 * (sf - 2 * de)
    n_payload = 8 + math.ceil(numerator / denominator) * cr
    return t_preamble + n_payload * t_sym


def _make_packet_record(raw_packet_len_bytes: int = 32) -> dict:
    """Minimal packet_record dict with a hex raw_packet of the desired length."""
    return {
        "timestamp": 1700000000,
        "type": 4,
        "route": 1,
        "rssi": -90,
        "snr": 7.5,
        "score": 0.5,
        "payload_length": raw_packet_len_bytes - 6,  # arbitrary
        "packet_hash": "deadbeef",
        "raw_packet": "AB" * raw_packet_len_bytes,  # hex string -> N bytes
    }


# --------------------------------------------------------------------
# Backward compatibility: legacy packet_records without airtime_ms
# --------------------------------------------------------------------
def test_packet_record_defaults_duration_to_zero_when_airtime_ms_missing():
    """packet_records produced before this change have no 'airtime_ms' key;
    the serializer must default to '0' rather than raising.
    """
    pkt = _make_packet_record()
    pkt.pop("airtime_ms", None)  # ensure not present
    record = PacketRecord.from_packet_record(pkt, origin="node", origin_id="ABCD")
    assert record is not None
    assert record.duration == "0"


# --------------------------------------------------------------------
# Forward path: airtime_ms field flows through to duration
# --------------------------------------------------------------------
def test_packet_record_serializes_airtime_ms_as_rounded_integer_string():
    """airtime_ms = 123.7 must serialize as duration='124'."""
    pkt = _make_packet_record()
    pkt["airtime_ms"] = 123.7
    record = PacketRecord.from_packet_record(pkt, origin="node", origin_id="ABCD")
    assert record is not None
    assert record.duration == "124"


def test_packet_record_serializes_zero_airtime_as_zero_duration():
    pkt = _make_packet_record()
    pkt["airtime_ms"] = 0.0
    record = PacketRecord.from_packet_record(pkt, origin="node", origin_id="ABCD")
    assert record is not None
    assert record.duration == "0"


# --------------------------------------------------------------------
# End-to-end: AirtimeManager output matches the Semtech reference
# --------------------------------------------------------------------
def test_airtime_manager_matches_semtech_reference_for_typical_meshcore_settings():
    """The calculator wired into _publish_packet_to_mqtt must produce the same
    number as the Semtech reference formula for typical MeshCore EU settings.
    """
    cfg = {
        "radio": {
            "spreading_factor": 8,
            "bandwidth": 62500,
            "coding_rate": 8,
            "preamble_length": 17,
        }
    }
    mgr = AirtimeManager(cfg)
    for payload_len in (16, 32, 64, 128, 200):
        actual = mgr.calculate_airtime(payload_len)
        expected = _semtech_airtime_ms(payload_len, sf=8, bw_hz=62500, cr=8, preamble=17)
        assert math.isclose(actual, expected, rel_tol=1e-9), (
            f"airtime mismatch for {payload_len}B: got {actual}, expected {expected}"
        )


def test_airtime_manager_matches_semtech_reference_for_low_data_rate_optimization():
    """SF11/SF12 at <=125kHz triggers low-data-rate optimization (DE=1).
    This test ensures both the reference and production path agree there.
    """
    cfg = {
        "radio": {
            "spreading_factor": 12,
            "bandwidth": 125000,
            "coding_rate": 5,
            "preamble_length": 8,
        }
    }
    mgr = AirtimeManager(cfg)
    actual = mgr.calculate_airtime(50)
    expected = _semtech_airtime_ms(50, sf=12, bw_hz=125000, cr=5, preamble=8)
    assert math.isclose(actual, expected, rel_tol=1e-9)
