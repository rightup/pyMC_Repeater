"""Tests for repeater.airtime using radio preset configurations.

This complements duration-focused tests by validating AirtimeManager behavior
across real-world SF/BW/CR combinations from radio-presets.json.
"""

import json
import math
from pathlib import Path

import pytest

from repeater.airtime import AirtimeManager


def _semtech_airtime_ms(payload_len: int, sf: int, bw_hz: int, cr: int, preamble: int) -> float:
    """Independent Semtech reference formula used as oracle in tests."""
    crc = 1
    h = 0  # explicit header
    # LDRO follows the radio driver's auto rule: on when symbol time >= 16 ms
    de = 1 if (2**sf) / (bw_hz / 1000) >= 16.0 else 0
    t_sym = (2**sf) / (bw_hz / 1000)
    t_preamble = (preamble + 4.25) * t_sym
    numerator = max(8 * payload_len - 4 * sf + 28 + 16 * crc - 20 * h, 0)
    denominator = 4 * (sf - 2 * de)
    n_payload = 8 + math.ceil(numerator / denominator) * cr
    return t_preamble + n_payload * t_sym


def _load_all_presets():
    """Load preset tuples (title, sf, bw_hz, cr) from JSON."""
    preset_file = Path(__file__).resolve().parents[1] / "radio-presets.json"
    data = json.loads(preset_file.read_text(encoding="utf-8"))
    entries = data["config"]["suggested_radio_settings"]["entries"]

    selected = []
    for e in entries:
        selected.append(
            (
                e["title"],
                int(e["spreading_factor"]),
                int(float(e["bandwidth"]) * 1000),
                int(e["coding_rate"]),
            )
        )
    return selected


ALL_PRESETS = _load_all_presets()
ALL_PRESET_IDS = [p[0] for p in ALL_PRESETS]


def _make_mgr(sf: int, bw_hz: int, cr: int, preamble: int = 8, max_airtime_per_minute: int = 3600):
    cfg = {
        "radio": {
            "spreading_factor": sf,
            "bandwidth": bw_hz,
            "coding_rate": cr,
            "preamble_length": preamble,
        },
        "duty_cycle": {
            "max_airtime_per_minute": max_airtime_per_minute,
            "enforcement_enabled": True,
        },
    }
    return AirtimeManager(cfg)


def test_all_presets_loaded():
    assert ALL_PRESETS


@pytest.mark.parametrize("_title,sf,bw_hz,cr", ALL_PRESETS, ids=ALL_PRESET_IDS)
def test_all_presets_match_semtech_formula(_title, sf, bw_hz, cr):
    mgr = _make_mgr(sf, bw_hz, cr, preamble=8)
    for payload_len in (16, 32, 64, 128):
        actual = mgr.calculate_airtime(payload_len)
        expected = _semtech_airtime_ms(payload_len, sf=sf, bw_hz=bw_hz, cr=cr, preamble=8)
        assert math.isclose(actual, expected, rel_tol=1e-9), (
            f"{_title} mismatch for {payload_len}B: got {actual}, expected {expected}"
        )


@pytest.mark.parametrize("_title,sf,bw_hz,cr", ALL_PRESETS, ids=ALL_PRESET_IDS)
def test_all_presets_airtime_increases_with_payload(_title, sf, bw_hz, cr):
    mgr = _make_mgr(sf, bw_hz, cr, preamble=8)
    short = mgr.calculate_airtime(16)
    medium = mgr.calculate_airtime(64)
    long_ = mgr.calculate_airtime(128)
    assert short < medium < long_


def test_long_range_preset_has_higher_airtime_than_fast_preset_for_same_payload():
    # EU/UK long-range profile vs US recommended profile from presets.
    long_mgr = _make_mgr(sf=11, bw_hz=250000, cr=5, preamble=8)
    fast_mgr = _make_mgr(sf=7, bw_hz=62500, cr=5, preamble=8)
    payload_len = 64
    assert long_mgr.calculate_airtime(payload_len) > fast_mgr.calculate_airtime(payload_len)


def test_ldro_follows_symbol_time_rule_like_radiolib():
    """Settings with 16.384 ms symbols use LDRO on real hardware (RadioLib's
    auto rule) even though they fall outside the old "SF >= 11 and
    BW <= 125 kHz" shorthand. Expected values generated with RadioLib 7.6.0's
    SX126x::calculateTimeOnAir integer arithmetic (microseconds / 1000)."""
    sf12_wide = _make_mgr(sf=12, bw_hz=250000, cr=5, preamble=8)
    assert math.isclose(sf12_wide.calculate_airtime(50), 1150.976, rel_tol=1e-9)
    sf10_narrow = _make_mgr(sf=10, bw_hz=62500, cr=6, preamble=8)
    assert math.isclose(sf10_narrow.calculate_airtime(32), 1216.512, rel_tol=1e-9)


def test_legacy_coding_rate_index_matches_denominator_form():
    """A config using the legacy index form (1..4) must compute the same
    airtime as the equivalent denominator form (5..8), not undercount
    payload symbols by multiplying with the raw index."""
    for index, denom in ((1, 5), (2, 6), (3, 7), (4, 8)):
        by_index = _make_mgr(sf=10, bw_hz=250000, cr=index, preamble=8)
        by_denom = _make_mgr(sf=10, bw_hz=250000, cr=denom, preamble=8)
        for payload_len in (16, 64, 128):
            assert math.isclose(
                by_index.calculate_airtime(payload_len),
                by_denom.calculate_airtime(payload_len),
                rel_tol=1e-9,
            )


@pytest.mark.parametrize("_title,sf,bw_hz,cr", ALL_PRESETS, ids=ALL_PRESET_IDS)
def test_can_transmit_blocks_after_budget_exhausted_for_each_preset(_title, sf, bw_hz, cr):
    mgr = _make_mgr(sf, bw_hz, cr, preamble=8, max_airtime_per_minute=600)
    airtime = mgr.calculate_airtime(64)

    # Feed TX history until just before the limit.
    sent = 0
    while True:
        can_tx, _ = mgr.can_transmit(airtime)
        if not can_tx:
            break
        mgr.record_tx(airtime)
        sent += 1
        # Safety guard against accidental infinite loops.
        assert sent < 1000

    can_tx_after, wait = mgr.can_transmit(airtime)
    assert can_tx_after is False
    assert wait >= 0.0


def test_stats_report_tx_rx_airtime_totals():
    mgr = _make_mgr(sf=8, bw_hz=62500, cr=8, preamble=17)
    tx_airtime = mgr.calculate_airtime(50)
    rx_airtime = mgr.calculate_airtime(20)

    mgr.record_tx(tx_airtime)
    mgr.record_rx(rx_airtime)

    stats = mgr.get_stats()
    assert stats["total_airtime_ms"] == pytest.approx(tx_airtime)
    assert stats["total_rx_airtime_ms"] == pytest.approx(rx_airtime)
    assert stats["current_airtime_ms"] == pytest.approx(tx_airtime)


def test_refresh_radio_params_updates_cached_modulation():
    """Live SF/BW changes must refresh estimation scalars without clearing history."""
    from openhop_core.protocol.packet_utils import calculate_lora_airtime_ms

    mgr = _make_mgr(sf=7, bw_hz=125000, cr=5, preamble=8)
    before = mgr.calculate_airtime(50)
    assert math.isclose(before, 97.536, rel_tol=1e-9)

    mgr.record_tx(before)
    mgr.refresh_radio_params(
        {
            "spreading_factor": 12,
            "bandwidth": 125000,
            "coding_rate": 5,
            "preamble_length": 8,
        }
    )

    after = mgr.calculate_airtime(50)
    expected = calculate_lora_airtime_ms(50, 12, 125000, 5, 8)
    assert math.isclose(after, expected, rel_tol=1e-9)
    assert math.isclose(after, 2301.952, rel_tol=1e-9)
    assert after > before
    assert mgr.spreading_factor == 12
    assert mgr.total_airtime_ms == pytest.approx(before)
    assert len(mgr.tx_history) == 1
