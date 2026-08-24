"""Tests for /api/update_radio_hardware_config and the shared hardware
preset helper (repeater/web/hardware_presets.py).

The endpoint is the dedicated save path for the Configuration → Radio
Hardware tab. Presets are applied backend-side from radio-settings.json
(including fields the UI has no widgets for, e.g. use_dio3_tcxo), then
the request overrides merge on top — see apply_hardware_preset().
"""

import json

import cherrypy
import pytest
import yaml

from repeater.web.api_endpoints import APIEndpoints
from repeater.web.hardware_presets import apply_hardware_preset, load_hardware_presets

# Preset with "hidden" fields (no UI widgets): TCXO, DIO2 RF switch.
_STATION_G3 = {
    "name": "BQ Voyage Station G3",
    "bus_id": 0,
    "cs_id": 0,
    "cs_pin": -1,
    "reset_pin": 16,
    "busy_pin": 24,
    "irq_pin": 22,
    "txen_pin": -1,
    "rxen_pin": -1,
    "txled_pin": -1,
    "rxled_pin": -1,
    "tx_power": 19,
    "use_dio2_rf": True,
    "use_dio3_tcxo": True,
    "dio3_tcxo_voltage": 1.8,
    "preamble_length": 32,
}

_PYMC_USB_PRESET = {
    "name": "pymc_usb modem (USB-CDC)",
    "radio_type": "pymc_usb",
    "tx_power": 22,
    "preamble_length": 16,
}

_BASE_CONFIG = {
    "repeater": {"node_name": "unit-test", "security": {"admin_password": "admin123"}},
    "radio": {"frequency": 869618000, "spreading_factor": 8, "tx_power": 9},
    "radio_type": "pymc_usb",
    # lbt_enabled=False is a deliberate operator choice with no preset
    # equivalent — preset-mode saves must not wipe it (merge, not replace).
    "pymc_usb": {"port": "/dev/ttyACM0", "baudrate": 921600, "lbt_enabled": False},
    # Stale keys from a previous SPI board — a preset save must not keep them.
    "sx1262": {"irq_pin": 4, "use_gpiod_backend": True},
}


@pytest.fixture
def endpoint_env(tmp_path):
    """Tempdir with config.yaml + radio-settings.json + mocked cherrypy."""
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.safe_dump(_BASE_CONFIG, f)

    with open(tmp_path / "radio-settings.json", "w") as f:
        json.dump({"hardware": {"bq-station-g3": _STATION_G3, "pymc_usb": _PYMC_USB_PRESET}}, f)

    with open(config_path) as f:
        config = yaml.safe_load(f)
    config["storage_dir"] = str(tmp_path)
    endpoints = APIEndpoints(config=config, config_path=str(config_path))

    def _post(body):
        cherrypy.request.method = "POST"
        cherrypy.request.json = body
        return endpoints.update_radio_hardware_config()

    return config, config_path, _post


def _read_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


# ─── apply_hardware_preset (helper) ──────────────────────────────────


def test_apply_preset_maps_all_fields_including_hidden_ones():
    cfg = {}
    apply_hardware_preset(cfg, _STATION_G3)

    assert cfg["radio_type"] == "sx1262"  # SPI presets omit radio_type
    sx = cfg["sx1262"]
    assert sx["reset_pin"] == 16
    assert sx["busy_pin"] == 24
    assert sx["irq_pin"] == 22
    assert sx["cs_pin"] == -1
    # The fields the web form has no widgets for must be applied too.
    assert sx["use_dio2_rf"] is True
    assert sx["use_dio3_tcxo"] is True
    assert sx["dio3_tcxo_voltage"] == 1.8
    assert cfg["radio"]["tx_power"] == 19
    assert cfg["radio"]["preamble_length"] == 32
    # name/description are preset metadata, not config.
    assert "name" not in sx


def test_apply_preset_tx_power_override_wins():
    cfg = {}
    apply_hardware_preset(cfg, _STATION_G3, tx_power_override=14)
    assert cfg["radio"]["tx_power"] == 14


def test_apply_preset_ch341_vid_pid():
    cfg = {}
    apply_hardware_preset(
        cfg, {"radio_type": "sx1262_ch341", "vid": 6790, "pid": 21778, "cs_pin": 0}
    )
    assert cfg["radio_type"] == "sx1262_ch341"
    assert cfg["ch341"] == {"vid": 6790, "pid": 21778}
    assert cfg["sx1262"]["cs_pin"] == 0


# ─── endpoint: preset mode ───────────────────────────────────────────


def test_preset_mode_applies_full_preset(endpoint_env):
    _config, config_path, post = endpoint_env

    result = post({"hardware_key": "bq-station-g3"})

    assert result["success"] is True
    assert result["restart_required"] is True

    written = _read_yaml(config_path)
    assert written["radio_type"] == "sx1262"
    assert written["sx1262"]["reset_pin"] == 16
    assert written["sx1262"]["use_dio3_tcxo"] is True
    assert written["sx1262"]["use_dio2_rf"] is True
    # Section replaced: stale keys from the previous board are gone.
    assert "use_gpiod_backend" not in written["sx1262"]
    # Radio section merged, not replaced: tuning params survive.
    assert written["radio"]["frequency"] == 869618000
    assert written["radio"]["spreading_factor"] == 8
    assert written["radio"]["tx_power"] == 19  # from preset
    assert written["radio"]["preamble_length"] == 32


def test_preset_mode_overrides_merge_on_top(endpoint_env):
    _config, config_path, post = endpoint_env

    result = post({"hardware_key": "bq-station-g3", "overrides": {"sx1262": {"irq_pin": 23}}})

    assert result["success"] is True
    written = _read_yaml(config_path)
    assert written["sx1262"]["irq_pin"] == 23  # override
    assert written["sx1262"]["reset_pin"] == 16  # preset intact
    assert written["sx1262"]["use_dio3_tcxo"] is True  # hidden field intact


def test_preset_mode_unknown_key(endpoint_env):
    _config, config_path, post = endpoint_env
    before = _read_yaml(config_path)

    result = post({"hardware_key": "does-not-exist"})

    assert result["success"] is False
    assert "does-not-exist" in result["error"]
    assert _read_yaml(config_path) == before  # nothing written


def test_preset_mode_rejects_foreign_override_section(endpoint_env):
    _config, config_path, post = endpoint_env
    before = _read_yaml(config_path)

    result = post({"hardware_key": "bq-station-g3", "overrides": {"pymc_usb": {"port": "/dev/x"}}})

    assert result["success"] is False
    assert "pymc_usb" in result["error"]
    assert _read_yaml(config_path) == before


def test_preset_mode_invalid_override_commits_nothing(endpoint_env):
    config, config_path, post = endpoint_env
    before = _read_yaml(config_path)

    result = post({"hardware_key": "bq-station-g3", "overrides": {"sx1262": {"irq_pin": "abc"}}})

    assert result["success"] is False
    assert "irq_pin" in result["error"]
    assert _read_yaml(config_path) == before
    # In-memory config untouched too (staged application).
    assert config["radio_type"] == "pymc_usb"


def test_preset_mode_non_spi_preset_with_port_override(endpoint_env):
    _config, config_path, post = endpoint_env

    result = post(
        {
            "hardware_key": "pymc_usb",
            "overrides": {"pymc_usb": {"port": "/dev/ttyACM1", "baudrate": 115200}},
        }
    )

    assert result["success"] is True
    written = _read_yaml(config_path)
    assert written["radio_type"] == "pymc_usb"
    assert written["pymc_usb"]["port"] == "/dev/ttyACM1"
    assert written["pymc_usb"]["baudrate"] == 115200
    assert written["radio"]["preamble_length"] == 16  # from preset


def test_preset_mode_non_spi_merge_preserves_operator_keys(endpoint_env):
    """kiss/pymc_* sections are merged (not replaced) in preset mode:
    operator keys with no preset equivalent must survive."""
    _config, config_path, post = endpoint_env

    result = post({"hardware_key": "pymc_usb", "overrides": {"pymc_usb": {"port": "/dev/ttyACM1"}}})

    assert result["success"] is True
    written = _read_yaml(config_path)
    assert written["pymc_usb"]["port"] == "/dev/ttyACM1"  # override applied
    assert written["pymc_usb"]["baudrate"] == 921600  # untouched key kept
    assert written["pymc_usb"]["lbt_enabled"] is False  # operator key kept


def test_load_hardware_presets_corrupt_file_raises(tmp_path):
    """A corrupted radio-settings.json must raise (surfaced as an error by
    hardware_options), not read as an empty preset list."""
    (tmp_path / "radio-settings.json").write_text("{not json", encoding="utf-8")
    config = {"storage_dir": str(tmp_path)}

    with pytest.raises(ValueError):
        load_hardware_presets(config, str(tmp_path / "config.yaml"))


# ─── endpoint: manual mode ───────────────────────────────────────────


def test_manual_mode_pymc_usb(endpoint_env):
    _config, config_path, post = endpoint_env

    result = post(
        {"radio_type": "pymc_usb", "pymc_usb": {"port": "/dev/ttyUSB2", "baudrate": 460800}}
    )

    assert result["success"] is True
    written = _read_yaml(config_path)
    assert written["radio_type"] == "pymc_usb"
    assert written["pymc_usb"]["port"] == "/dev/ttyUSB2"
    assert written["pymc_usb"]["baudrate"] == 460800


def test_manual_mode_sx1262_merges_section(endpoint_env):
    """Manual pin tweaks must not wipe preset-managed fields that have no
    form widgets (TCXO & co survive a custom pin edit)."""
    _config, config_path, post = endpoint_env

    # First apply the preset (writes use_dio3_tcxo etc.)
    post({"hardware_key": "bq-station-g3"})
    # Then a manual save with just pins.
    result = post({"radio_type": "sx1262", "sx1262": {"irq_pin": 25}})

    assert result["success"] is True
    written = _read_yaml(config_path)
    assert written["sx1262"]["irq_pin"] == 25
    assert written["sx1262"]["use_dio3_tcxo"] is True  # survived the merge


def test_manual_mode_none_disables_radio(endpoint_env):
    _config, config_path, post = endpoint_env

    result = post({"radio_type": None})

    assert result["success"] is True
    written = _read_yaml(config_path)
    assert written["radio_type"] is None


def test_manual_mode_unsupported_radio_type(endpoint_env):
    _config, _config_path, post = endpoint_env

    result = post({"radio_type": "flux_capacitor"})

    assert result["success"] is False
    assert "flux_capacitor" in result["error"]


def test_manual_mode_rejects_foreign_section(endpoint_env):
    _config, _config_path, post = endpoint_env

    result = post({"radio_type": "pymc_usb", "sx1262": {"irq_pin": 22}})

    assert result["success"] is False
    assert "sx1262" in result["error"]


def test_manual_mode_validates_field_ranges(endpoint_env):
    config, config_path, post = endpoint_env
    before = _read_yaml(config_path)

    result = post({"radio_type": "pymc_tcp", "pymc_tcp": {"host": "modem.local", "port": 99999}})

    assert result["success"] is False
    assert "port" in result["error"]
    assert _read_yaml(config_path) == before
    # Manual mode is staged too: the in-memory config (served by
    # /api/stats) must not diverge from the file on validation failure.
    assert config["radio_type"] == "pymc_usb"
    assert "pymc_tcp" not in config


def test_missing_key_and_type(endpoint_env):
    _config, _config_path, post = endpoint_env

    result = post({})

    assert result["success"] is False
    assert "hardware_key" in result["error"]
