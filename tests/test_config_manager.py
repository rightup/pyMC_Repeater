import math
import os
import stat
import threading

import yaml

import repeater.config_manager as config_manager_module
from repeater.airtime import AirtimeManager
from repeater.config_manager import ConfigManager


class _DummyRepeaterHandler:
    def __init__(self, config=None):
        self.radio_config = {}
        self.airtime_mgr = AirtimeManager(
            config
            or {
                "radio": {
                    "frequency": 868000000,
                    "bandwidth": 125000,
                    "spreading_factor": 7,
                    "coding_rate": 5,
                    "tx_power": 14,
                    "preamble_length": 8,
                }
            }
        )


class _DummySX1262Radio:
    def __init__(self, apply_ok=True):
        self.frequency = 868000000
        self.bandwidth = 125000
        self.spreading_factor = 7
        self.coding_rate = 5
        self.tx_power = 14
        self.calls = []
        self.apply_ok = apply_ok

    def set_frequency(self, frequency):
        self.calls.append(("set_frequency", frequency))
        if not self.apply_ok:
            return False
        self.frequency = frequency
        return True

    def set_tx_power(self, power):
        self.calls.append(("set_tx_power", power))
        if not self.apply_ok:
            return False
        self.tx_power = power
        return True

    def set_spreading_factor(self, spreading_factor):
        self.calls.append(("set_spreading_factor", spreading_factor))
        if not self.apply_ok:
            return False
        self.spreading_factor = spreading_factor
        return True

    def set_bandwidth(self, bandwidth):
        self.calls.append(("set_bandwidth", bandwidth))
        if not self.apply_ok:
            return False
        self.bandwidth = bandwidth
        return True


class _DummyKissRadio:
    def __init__(self):
        self.radio_config = {
            "frequency": 869618000,
            "bandwidth": 62500,
            "spreading_factor": 8,
            "coding_rate": 8,
            "tx_power": 20,
        }
        self.calls = []

    def configure_radio(self, **kwargs):
        self.calls.append(("configure_radio", kwargs))
        self.frequency = kwargs["frequency"]
        self.bandwidth = kwargs["bandwidth"]
        self.spreading_factor = kwargs["spreading_factor"]
        self.coding_rate = kwargs["coding_rate"]
        self.tx_power = self.radio_config["tx_power"]
        return True


class _DummyDaemon:
    def __init__(self, config, radio):
        self.config = {
            "radio": dict(config.get("radio", {})),
            "kiss": dict(config.get("kiss", {})),
        }
        self.radio = radio
        self.repeater_handler = _DummyRepeaterHandler(config)
        self.advert_helper = None
        self.dispatcher = None


def test_live_update_daemon_applies_sx1262_radio_config():
    config = {
        "radio": {
            "frequency": 915000000,
            "bandwidth": 250000,
            "spreading_factor": 10,
            "coding_rate": 6,
            "tx_power": 20,
        }
    }
    radio = _DummySX1262Radio()
    daemon = _DummyDaemon(config, radio)
    manager = ConfigManager("/tmp/config.yaml", config, daemon)

    assert manager.live_update_daemon(["radio"])

    assert radio.calls == [
        ("set_frequency", 915000000),
        ("set_tx_power", 20),
        ("set_spreading_factor", 10),
        ("set_bandwidth", 250000),
    ]
    assert radio.coding_rate == 6
    assert daemon.repeater_handler.radio_config == config["radio"]


def test_live_update_daemon_applies_kiss_radio_config():
    config = {
        "radio": {
            "frequency": 915500000,
            "bandwidth": 125000,
            "spreading_factor": 9,
            "coding_rate": 7,
            "tx_power": 22,
        },
        "kiss": {
            "port": "/dev/ttyUSB0",
            "baud_rate": 115200,
        },
    }
    radio = _DummyKissRadio()
    daemon = _DummyDaemon(config, radio)
    manager = ConfigManager("/tmp/config.yaml", config, daemon)

    assert manager.live_update_daemon(["radio"])

    assert radio.calls == [
        (
            "configure_radio",
            {
                "frequency": 915500000,
                "bandwidth": 125000,
                "spreading_factor": 9,
                "coding_rate": 7,
            },
        )
    ]
    assert radio.radio_config == config["radio"]
    assert daemon.repeater_handler.radio_config == config["radio"]


def test_live_update_daemon_refreshes_airtime_manager_modulation():
    startup_radio = {
        "frequency": 868000000,
        "bandwidth": 125000,
        "spreading_factor": 7,
        "coding_rate": 5,
        "tx_power": 14,
        "preamble_length": 8,
    }
    updated_radio = {
        "frequency": 915000000,
        "bandwidth": 125000,
        "spreading_factor": 12,
        "coding_rate": 5,
        "tx_power": 14,
        "preamble_length": 8,
    }
    config = {"radio": dict(startup_radio)}
    radio = _DummySX1262Radio()
    daemon = _DummyDaemon(config, radio)
    airtime_mgr = daemon.repeater_handler.airtime_mgr
    before = airtime_mgr.calculate_airtime(50)
    assert math.isclose(before, 97.536, rel_tol=1e-9)

    config["radio"] = dict(updated_radio)
    manager = ConfigManager("/tmp/config.yaml", config, daemon)
    assert manager.live_update_daemon(["radio"])

    assert airtime_mgr.spreading_factor == 12
    assert airtime_mgr.bandwidth == 125000
    assert airtime_mgr.preamble_length == 8
    assert math.isclose(airtime_mgr.calculate_airtime(50), 2301.952, rel_tol=1e-9)


def test_failed_live_radio_apply_leaves_airtime_manager_unchanged():
    startup_radio = {
        "frequency": 868000000,
        "bandwidth": 125000,
        "spreading_factor": 7,
        "coding_rate": 5,
        "tx_power": 14,
        "preamble_length": 8,
    }
    config = {"radio": dict(startup_radio)}
    radio = _DummySX1262Radio(apply_ok=False)
    daemon = _DummyDaemon(config, radio)
    airtime_mgr = daemon.repeater_handler.airtime_mgr

    config["radio"] = {
        "frequency": 915000000,
        "bandwidth": 125000,
        "spreading_factor": 12,
        "coding_rate": 5,
        "tx_power": 14,
        "preamble_length": 8,
    }
    manager = ConfigManager("/tmp/config.yaml", config, daemon)
    assert manager.live_update_daemon(["radio"]) is False

    assert airtime_mgr.spreading_factor == 7
    assert math.isclose(airtime_mgr.calculate_airtime(50), 97.536, rel_tol=1e-9)


def test_failed_config_serialization_preserves_existing_file(tmp_path):
    config_path = tmp_path / "config.yaml"
    original = b"repeater:\n  node_name: before\n"
    config_path.write_bytes(original)
    manager = ConfigManager(
        str(config_path),
        {"repeater": {"unsupported": object()}},
    )

    assert manager.save_to_file() is False
    assert config_path.read_bytes() == original
    assert list(tmp_path.glob(".config.yaml.*.tmp")) == []


def test_directory_sync_failure_does_not_misreport_completed_replace(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("repeater:\n  node_name: before\n")
    manager = ConfigManager(
        str(config_path),
        {"repeater": {"node_name": "after"}},
    )
    real_fsync = os.fsync
    fsync_calls = 0

    def fail_directory_fsync(descriptor):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("directory fsync unsupported")
        return real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)

    assert manager.save_to_file() is True
    assert "node_name: after" in config_path.read_text()


def test_config_save_tightens_existing_permissions_to_owner_only(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("repeater:\n  node_name: before\n")
    config_path.chmod(0o644)
    manager = ConfigManager(
        str(config_path),
        {"repeater": {"node_name": "after"}},
    )

    assert manager.save_to_file() is True
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_companion_node_name_save_failure_rolls_back_memory(tmp_path, monkeypatch):
    config = {
        "identities": {
            "companions": [
                {
                    "name": "field-radio",
                    "settings": {"node_name": "before"},
                }
            ]
        }
    }
    manager = ConfigManager(str(tmp_path / "config.yaml"), config)
    monkeypatch.setattr(manager, "_save_to_file_locked", lambda: False)

    assert manager.save_companion_node_name("field-radio", "after") is False
    assert (
        config["identities"]["companions"][0]["settings"]["node_name"]
        == "before"
    )


def test_companion_node_name_save_failure_restores_absent_settings(
    tmp_path,
    monkeypatch,
):
    companion = {"name": "field-radio"}
    config = {"identities": {"companions": [companion]}}
    manager = ConfigManager(str(tmp_path / "config.yaml"), config)
    monkeypatch.setattr(manager, "_save_to_file_locked", lambda: False)

    assert manager.save_companion_node_name("field-radio", "after") is False
    assert companion == {"name": "field-radio"}


def test_update_and_save_failure_rolls_back_shared_config(tmp_path, monkeypatch):
    config = {
        "repeater": {"node_name": "before"},
        "web": {"site_name": "before"},
    }
    manager = ConfigManager(str(tmp_path / "config.yaml"), config)
    monkeypatch.setattr(manager, "_save_to_file_locked", lambda: False)

    result = manager.update_and_save(
        {
            "repeater": {"node_name": "after"},
            "web": {"site_name": "after"},
        },
        live_update=False,
    )

    assert result == {
        "success": False,
        "saved": False,
        "live_updated": False,
        "error": "Failed to save config to file",
    }
    assert config == {
        "repeater": {"node_name": "before"},
        "web": {"site_name": "before"},
    }


def test_frame_name_and_http_update_share_one_serialized_save(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "config.yaml"
    config = {
        "repeater": {"node_name": "Repeater"},
        "identities": {
            "companions": [
                {
                    "name": "field-radio",
                    "settings": {"node_name": "before"},
                }
            ]
        },
    }
    manager = ConfigManager(str(config_path), config)
    frame_dump_started = threading.Event()
    release_frame_dump = threading.Event()
    http_update_done = threading.Event()
    real_safe_dump = yaml.safe_dump

    def blocking_safe_dump(*args, **kwargs):
        if threading.current_thread().name == "frame-name-writer":
            frame_dump_started.set()
            assert release_frame_dump.wait(timeout=5)
        return real_safe_dump(*args, **kwargs)

    monkeypatch.setattr(
        config_manager_module.yaml,
        "safe_dump",
        blocking_safe_dump,
    )
    results = {}

    def save_frame_name():
        results["frame"] = manager.save_companion_node_name(
            "field-radio",
            "after",
        )

    def save_http_update():
        results["http"] = manager.update_and_save(
            {"web": {"site_name": "mesh"}},
            live_update=False,
        )
        http_update_done.set()

    frame_thread = threading.Thread(
        target=save_frame_name,
        name="frame-name-writer",
    )
    http_thread = threading.Thread(
        target=save_http_update,
        name="http-config-writer",
    )
    frame_thread.start()
    assert frame_dump_started.wait(timeout=5)
    http_thread.start()

    # The second transaction must not mutate the shared dictionary while the
    # first transaction is serializing it.
    assert not http_update_done.wait(timeout=0.1)
    assert "web" not in config

    release_frame_dump.set()
    frame_thread.join(timeout=5)
    http_thread.join(timeout=5)
    assert not frame_thread.is_alive()
    assert not http_thread.is_alive()
    assert results["frame"] is True
    assert results["http"]["success"] is True

    persisted = yaml.safe_load(config_path.read_text())
    assert (
        persisted["identities"]["companions"][0]["settings"]["node_name"]
        == "after"
    )
    assert persisted["web"]["site_name"] == "mesh"
