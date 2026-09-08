"""Tests for optional multi-radio fabric stack in the repeater."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from repeater.config import (
    NullRadio,
    _merge_radio_entry,
    build_radio_stack,
)

try:
    from openhop_core.rf_fabric import RFFabric

    _CORE_HAS_ORIGIN_TX = hasattr(RFFabric, "set_origin_tx")
except ImportError:  # pragma: no cover - core is a hard dependency
    _CORE_HAS_ORIGIN_TX = False


class _FakeRadio:
    def __init__(self, name="r"):
        self.name = name
        self.rx_callback = None
        self.sent = []
        self.frequency = 869618000
        self.spreading_factor = 8
        self.bandwidth = 62500
        self.coding_rate = 8
        self.tx_power = 14

    def set_rx_callback(self, cb):
        self.rx_callback = cb

    async def send(self, data: bytes):
        self.sent.append(data)
        return {"ok": True, "name": self.name}

    def get_frequency(self):
        return self.frequency / 1e6

    def get_spreading_factor(self):
        return self.spreading_factor

    def get_bandwidth(self):
        return self.bandwidth / 1e3

    def get_coding_rate(self):
        return self.coding_rate

    def get_tx_power(self):
        return self.tx_power

    def get_last_rssi(self):
        return -80

    def get_last_snr(self):
        return 5.0


def test_merge_radio_entry_inherits_and_overrides():
    global_cfg = {
        "radio_type": "sx1262",
        "radio": {"frequency": 1, "tx_power": 10},
        "sx1262": {"bus_id": 0},
    }
    entry = {
        "id": "link",
        "radio_type": "modem_usb",
        "radio": {"frequency": 2, "tx_power": 22},
        "modem_usb": {"port": "/dev/ttyACM0"},
    }
    merged = _merge_radio_entry(global_cfg, entry)
    assert merged["_radio_id"] == "link"
    assert merged["radio_type"] == "modem_usb"
    assert merged["radio"]["frequency"] == 2
    assert merged["modem_usb"]["port"] == "/dev/ttyACM0"
    assert "sx1262" in merged  # inherited leftover ok; factory uses radio_type


def test_build_radio_stack_legacy_single():
    cfg = {"radio_type": "none"}
    radio, meta = build_radio_stack(cfg)
    assert isinstance(radio, NullRadio)
    assert meta["mode"] == "single"
    assert meta["fabric"] is False


def test_build_radio_stack_multi_wraps_fabric():
    a = _FakeRadio("a")
    b = _FakeRadio("b")

    def fake_get(board):
        # build_radio_stack pops _radio_id before calling get_radio_for_board
        # so distinguish by radio air settings
        freq = (board.get("radio") or {}).get("frequency")
        return a if freq == 111 else b

    cfg = {
        "fabric": {"default_radio": "local", "tx_mode": "sticky"},
        "radios": [
            {
                "id": "local",
                "radio_type": "sx1262",
                "radio": {"frequency": 111},
                "sx1262": {"bus_id": 0},
            },
            {
                "id": "link",
                "radio_type": "sx1262",
                "radio": {"frequency": 222},
                "sx1262": {"bus_id": 0},
            },
        ],
    }

    with patch("repeater.config.get_radio_for_board", side_effect=fake_get):
        radio, meta = build_radio_stack(cfg)

    assert meta["fabric"] is True
    assert meta["mode"] == "multi"
    assert meta["radio_ids"] == ["local", "link"]
    assert meta["default_radio"] == "local"
    # FabricRadio surface
    assert hasattr(radio, "fabric")
    assert list(radio.fabric.radios.keys()) == ["local", "link"]
    assert radio.fabric.default_radio_id == "local"
    # sticky selector installed
    assert radio.fabric._tx_selector is not None


@pytest.mark.asyncio
async def test_sticky_tx_uses_last_rx_radio():
    a = _FakeRadio("a")
    b = _FakeRadio("b")

    def fake_get(board):
        freq = (board.get("radio") or {}).get("frequency")
        return a if freq == 111 else b

    cfg = {
        "fabric": {"default_radio": "local", "tx_mode": "sticky"},
        "radios": [
            {"id": "local", "radio_type": "sx1262", "radio": {"frequency": 111}, "sx1262": {}},
            {"id": "link", "radio_type": "sx1262", "radio": {"frequency": 222}, "sx1262": {}},
        ],
    }
    with patch("repeater.config.get_radio_for_board", side_effect=fake_get):
        radio, meta = build_radio_stack(cfg)

    # Simulate RX on link
    b.rx_callback(b"hello", -70, 3.0)
    await radio.send(b"reply")
    assert b.sent == [b"reply"]
    assert a.sent == []


def test_use_fabric_single_radio():
    fake = _FakeRadio("solo")
    cfg = {"radio_type": "sx1262", "fabric": {"use_fabric": True, "tx_mode": "default"}}
    with patch("repeater.config.get_radio_for_board", return_value=fake):
        radio, meta = build_radio_stack(cfg)
    assert meta["fabric"] is True
    assert meta["mode"] == "single_fabric"
    assert hasattr(radio, "fabric")


def test_tx_mode_all_rejected():
    a = _FakeRadio("a")
    b = _FakeRadio("b")

    def fake_get(board):
        freq = (board.get("radio") or {}).get("frequency")
        return a if freq == 111 else b

    cfg = {
        "fabric": {"default_radio": "local", "tx_mode": "all"},
        "radios": [
            {"id": "local", "radio_type": "sx1262", "radio": {"frequency": 111}, "sx1262": {}},
            {"id": "link", "radio_type": "sx1262", "radio": {"frequency": 222}, "sx1262": {}},
        ],
    }
    with patch("repeater.config.get_radio_for_board", side_effect=fake_get):
        with pytest.raises(ValueError, match="Unknown fabric.tx_mode"):
            build_radio_stack(cfg)


@pytest.mark.asyncio
async def test_bridge_tx_crosses_to_other_radio():
    """RX on local -> TX on link; RX on link -> TX on local."""
    a = _FakeRadio("a")
    b = _FakeRadio("b")

    def fake_get(board):
        freq = (board.get("radio") or {}).get("frequency")
        return a if freq == 111 else b

    cfg = {
        "fabric": {"default_radio": "local", "tx_mode": "bridge"},
        "radios": [
            {"id": "local", "radio_type": "sx1262", "radio": {"frequency": 111}, "sx1262": {}},
            {"id": "link", "radio_type": "sx1262", "radio": {"frequency": 222}, "sx1262": {}},
        ],
    }
    with patch("repeater.config.get_radio_for_board", side_effect=fake_get):
        radio, meta = build_radio_stack(cfg)

    assert meta["tx_mode"] == "bridge"

    # Heard on local neighborhood -> forward out link backhaul
    a.rx_callback(b"from-local", -70, 3.0)
    await radio.send(b"fwd-1")
    assert a.sent == []
    assert b.sent == [b"fwd-1"]

    # Heard on link backhaul -> forward out local
    b.rx_callback(b"from-link", -80, 2.0)
    await radio.send(b"fwd-2")
    assert a.sent == [b"fwd-2"]
    assert b.sent == [b"fwd-1"]


def test_merge_radio_entry_preserves_per_radio_ch341():
    global_cfg = {
        "radio_type": "sx1262_ch341",
        "ch341": {"vid": 0x1A86, "pid": 0x5512, "bus": 1, "address": 5},
        "radio": {"frequency": 1},
        "sx1262": {"bus_id": 0},
    }
    entry = {
        "id": "link",
        "ch341": {"vid": 0x1A86, "pid": 0x5512, "bus": 1, "address": 8},
        "radio": {"frequency": 2},
    }
    merged = _merge_radio_entry(global_cfg, entry)
    assert merged["ch341"]["address"] == 8
    assert merged["_ch341_per_instance"] is True


def _two_radio_cfg(fabric_cfg):
    return {
        "fabric": fabric_cfg,
        "radios": [
            {
                "id": "local",
                "radio_type": "sx1262",
                "radio": {"frequency": 111},
                "sx1262": {"bus_id": 0},
            },
            {
                "id": "link",
                "radio_type": "sx1262",
                "radio": {"frequency": 222},
                "sx1262": {"bus_id": 0},
            },
        ],
    }


@pytest.mark.skipif(
    not _CORE_HAS_ORIGIN_TX,
    reason="needs openhop-core with fabric origin_tx support",
)
def test_origin_tx_all_applied_to_fabric():
    a = _FakeRadio("a")
    b = _FakeRadio("b")

    def fake_get(board):
        freq = (board.get("radio") or {}).get("frequency")
        return a if freq == 111 else b

    cfg = _two_radio_cfg({"default_radio": "local", "tx_mode": "bridge", "origin_tx": "all"})
    with patch("repeater.config.get_radio_for_board", side_effect=fake_get):
        radio, meta = build_radio_stack(cfg)

    assert meta["origin_tx"] == "all"
    assert radio.fabric.origin_tx == "all"


def test_origin_tx_defaults_to_default():
    a = _FakeRadio("a")
    b = _FakeRadio("b")

    def fake_get(board):
        freq = (board.get("radio") or {}).get("frequency")
        return a if freq == 111 else b

    cfg = _two_radio_cfg({"default_radio": "local"})
    with patch("repeater.config.get_radio_for_board", side_effect=fake_get):
        radio, meta = build_radio_stack(cfg)

    assert meta["origin_tx"] == "default"
    assert getattr(radio.fabric, "origin_tx", "default") == "default"


def test_origin_tx_invalid_rejected():
    a = _FakeRadio("a")
    b = _FakeRadio("b")

    def fake_get(board):
        freq = (board.get("radio") or {}).get("frequency")
        return a if freq == 111 else b

    cfg = _two_radio_cfg({"origin_tx": "broadcast"})
    with patch("repeater.config.get_radio_for_board", side_effect=fake_get):
        with pytest.raises(ValueError):
            build_radio_stack(cfg)
