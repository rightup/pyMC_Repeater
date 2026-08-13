import pytest

from repeater.config import get_radio_for_board


class _DummyRadio:
    _initialized = True

    def begin(self):
        return True


def test_get_radio_for_board_passes_en_pins(monkeypatch):
    captured_kwargs = {}

    class _DummySX1262Radio(_DummyRadio):
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

    monkeypatch.setattr(
        "openhop_core.hardware.sx1262_wrapper.SX1262Radio",
        _DummySX1262Radio,
    )

    board_config = {
        "radio_type": "sx1262",
        "sx1262": {
            "bus_id": 0,
            "cs_id": 0,
            "cs_pin": -1,
            "reset_pin": 18,
            "busy_pin": 5,
            "irq_pin": 6,
            "txen_pin": -1,
            "rxen_pin": -1,
            "en_pins": [26, 23],
        },
        "radio": {
            "frequency": 915000000,
            "tx_power": 22,
            "spreading_factor": 9,
            "bandwidth": 125000,
            "coding_rate": 5,
            "preamble_length": 17,
            "sync_word": 0x3444,
        },
    }

    get_radio_for_board(board_config)

    assert captured_kwargs["en_pins"] == [26, 23]
    assert "en_pin" not in captured_kwargs


def test_get_radio_for_board_null_radio_type_returns_null_radio():
    radio = get_radio_for_board({"radio_type": None})
    assert type(radio).__name__ == "NullRadio"


def test_get_radio_for_board_missing_radio_type_returns_null_radio():
    radio = get_radio_for_board({})
    assert type(radio).__name__ == "NullRadio"


# ─── pymc_tcp / pymc_usb branches ────────────────────────────────────


def _pymc_radio_cfg():
    """Common radio params for the pymc_* tests."""
    return {
        "frequency": 869618000,
        "tx_power": 22,
        "spreading_factor": 8,
        "bandwidth": 62500,
        "coding_rate": 8,
        "preamble_length": 16,
        "sync_word": 0x12,
    }


def test_get_radio_for_board_pymc_tcp(monkeypatch):
    pytest.importorskip("openhop_core.hardware.tcp_radio")
    captured = {}

    class _DummyTCPLoRaRadio(_DummyRadio):
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "openhop_core.hardware.tcp_radio.TCPLoRaRadio",
        _DummyTCPLoRaRadio,
    )

    board_config = {
        "radio_type": "pymc_tcp",
        "pymc_tcp": {
            "host": "pymc-3e2834.local",
            "port": 5055,
            "token": "shared-secret",
            "connect_timeout": 7.5,
            "lbt_enabled": False,
            "lbt_max_attempts": 3,
        },
        "radio": _pymc_radio_cfg(),
    }

    get_radio_for_board(board_config)

    assert captured["host"] == "pymc-3e2834.local"
    assert captured["port"] == 5055
    assert captured["token"] == "shared-secret"
    assert captured["connect_timeout"] == 7.5
    assert captured["frequency"] == 869618000
    assert captured["sync_word"] == 0x12
    assert captured["lbt_enabled"] is False
    assert captured["lbt_max_attempts"] == 3


def test_get_radio_for_board_pymc_tcp_requires_host(monkeypatch):
    pytest.importorskip("openhop_core.hardware.tcp_radio")

    monkeypatch.setattr(
        "openhop_core.hardware.tcp_radio.TCPLoRaRadio",
        lambda **kwargs: _DummyRadio(),
    )

    board_config = {
        "radio_type": "pymc_tcp",
        "pymc_tcp": {"port": 5055},
        "radio": _pymc_radio_cfg(),
    }

    with pytest.raises(ValueError, match="Missing 'host'"):
        get_radio_for_board(board_config)


def test_get_radio_for_board_pymc_usb(monkeypatch):
    pytest.importorskip("openhop_core.hardware.usb_radio")
    captured = {}

    class _DummyUSBLoRaRadio(_DummyRadio):
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "openhop_core.hardware.usb_radio.USBLoRaRadio",
        _DummyUSBLoRaRadio,
    )

    board_config = {
        "radio_type": "pymc_usb",
        "pymc_usb": {
            "port": "/dev/ttyACM0",
            "baudrate": 921600,
        },
        "radio": _pymc_radio_cfg(),
    }

    get_radio_for_board(board_config)

    assert captured["port"] == "/dev/ttyACM0"
    assert captured["baudrate"] == 921600
    assert captured["frequency"] == 869618000
    assert captured["sync_word"] == 0x12
    # LBT defaults preserved when omitted from pymc_usb section.
    assert captured["lbt_enabled"] is True
    assert captured["lbt_max_attempts"] == 5


def test_get_radio_for_board_pymc_usb_requires_port(monkeypatch):
    pytest.importorskip("openhop_core.hardware.usb_radio")

    monkeypatch.setattr(
        "openhop_core.hardware.usb_radio.USBLoRaRadio",
        lambda **kwargs: _DummyRadio(),
    )

    board_config = {
        "radio_type": "pymc_usb",
        # Section present (baudrate set) but `port` deliberately omitted to
        # exercise the inner "Missing 'port'" guard rather than the outer
        # "Missing 'pymc_usb' section" one.
        "pymc_usb": {"baudrate": 921600},
        "radio": _pymc_radio_cfg(),
    }

    with pytest.raises(ValueError, match="Missing 'port'"):
        get_radio_for_board(board_config)


# ─── kiss branch: optional CSMA / key-up tuning forwarding ────────────


def _kiss_capture_radio_config(monkeypatch):
    """Patch KissModemWrapper to capture the radio_config it is built with."""
    pytest.importorskip("openhop_core.hardware.kiss_modem_wrapper")
    captured = {}

    class _DummyKissWrapper(_DummyRadio):
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

    monkeypatch.setattr(
        "openhop_core.hardware.kiss_modem_wrapper.KissModemWrapper",
        _DummyKissWrapper,
    )
    return captured


def test_get_radio_for_board_kiss_forwards_csma_tuning(monkeypatch):
    captured = _kiss_capture_radio_config(monkeypatch)

    board_config = {
        "radio_type": "kiss",
        "kiss": {
            "port": "/dev/ttyACM0",
            "baud_rate": 115200,
            "kiss_persistence": 255,
            "kiss_slottime_ms": 20,
            "tx_delay_ms": 50,
            "kiss_full_duplex": True,
        },
        "radio": _pymc_radio_cfg(),
    }

    get_radio_for_board(board_config)

    rc = captured["kwargs"]["radio_config"]
    assert rc["kiss_persistence"] == 255
    assert rc["kiss_slottime_ms"] == 20
    assert rc["tx_delay_ms"] == 50
    assert rc["kiss_full_duplex"] is True


def test_get_radio_for_board_kiss_omits_unset_tuning(monkeypatch):
    captured = _kiss_capture_radio_config(monkeypatch)

    board_config = {
        "radio_type": "kiss",
        "kiss": {"port": "/dev/ttyACM0", "baud_rate": 115200},
        "radio": _pymc_radio_cfg(),
    }

    get_radio_for_board(board_config)

    rc = captured["kwargs"]["radio_config"]
    # Unset keys must not be forwarded, so the wrapper keeps its own defaults.
    for key in ("kiss_persistence", "kiss_slottime_ms", "tx_delay_ms", "kiss_full_duplex"):
        assert key not in rc
