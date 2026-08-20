import copy
import logging

import yaml

from repeater.modem_config import normalize_modem_config


def test_canonical_modem_config_is_unchanged_and_input_is_not_mutated():
    original = {
        "radio_type": "modem_tcp",
        "modem_tcp": {"host": "modem.local", "token": "secret"},
        "modem_usb": {"port": "/dev/ttyACM0"},
    }
    before = copy.deepcopy(original)

    normalized = normalize_modem_config(original)

    assert normalized == before
    assert original == before
    assert normalized is not original


def test_legacy_top_level_type_and_section_are_canonicalized():
    normalized = normalize_modem_config(
        {"radio_type": "pymc_tcp", "pymc_tcp": {"host": "modem.local"}}
    )

    assert normalized == {
        "radio_type": "modem_tcp",
        "modem_tcp": {"host": "modem.local"},
    }


def test_canonical_section_wins_conflict_without_logging_values(caplog):
    config = {
        "radio_type": "pymc_tcp",
        "pymc_tcp": {"host": "legacy.example", "token": "legacy-secret"},
        "modem_tcp": {"host": "canonical.example", "token": "canonical-secret"},
    }

    with caplog.at_level(logging.WARNING):
        normalized = normalize_modem_config(config)

    assert normalized["radio_type"] == "modem_tcp"
    assert normalized["modem_tcp"]["host"] == "canonical.example"
    assert "pymc_tcp" not in normalized
    assert "pymc_tcp" in caplog.text and "modem_tcp" in caplog.text
    assert "legacy.example" not in caplog.text
    assert "canonical.example" not in caplog.text
    assert "legacy-secret" not in caplog.text
    assert "canonical-secret" not in caplog.text


def test_each_multi_radio_entry_is_normalized_independently():
    normalized = normalize_modem_config(
        {
            "radios": [
                {"id": "tcp", "radio_type": "pymc_tcp", "pymc_tcp": {"host": "x"}},
                {
                    "id": "usb",
                    "radio_type": "modem_usb",
                    "pymc_usb": {"port": "old"},
                    "modem_usb": {"port": "new"},
                },
                "malformed",
            ]
        }
    )

    assert normalized["radios"][0] == {
        "id": "tcp",
        "radio_type": "modem_tcp",
        "modem_tcp": {"host": "x"},
    }
    assert normalized["radios"][1]["radio_type"] == "modem_usb"
    assert normalized["radios"][1]["modem_usb"] == {"port": "new"}
    assert "pymc_usb" not in normalized["radios"][1]
    assert normalized["radios"][2] == "malformed"


def test_unrelated_and_malformed_values_are_preserved():
    config = {
        "radio_type": "kiss",
        "pymc_tcp": "not-a-mapping",
        "modem_tcp": ["also", "not", "a", "mapping"],
        "radios": None,
    }

    normalized = normalize_modem_config(config)

    assert normalized["radio_type"] == "kiss"
    assert normalized["modem_tcp"] == ["also", "not", "a", "mapping"]
    assert "pymc_tcp" not in normalized
    assert normalized["radios"] is None


def test_load_config_normalizes_in_memory_without_rewriting_file(tmp_path):
    from repeater.config import load_config

    path = tmp_path / "config.yaml"
    persisted = {
        "radio_type": "pymc_usb",
        "pymc_usb": {"port": "/dev/ttyACM0"},
        "storage": {"storage_dir": str(tmp_path / "data")},
        "repeater": {"security": {}},
    }
    path.write_text(yaml.safe_dump(persisted), encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    loaded = load_config(str(path))

    assert loaded["radio_type"] == "modem_usb"
    assert loaded["modem_usb"]["port"] == "/dev/ttyACM0"
    assert "pymc_usb" not in loaded
    assert path.read_text(encoding="utf-8") == before


def test_config_manager_save_lazily_canonicalizes_in_memory_and_yaml(tmp_path):
    from repeater.config_manager import ConfigManager

    path = tmp_path / "config.yaml"
    config = {
        "radio_type": "pymc_tcp",
        "pymc_tcp": {"host": "legacy.local", "token": "secret"},
        "radios": [{"id": "usb", "radio_type": "pymc_usb", "pymc_usb": {"port": "/dev/x"}}],
    }

    assert ConfigManager(str(path), config).save_to_file() is True

    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert config == written
    assert written["radio_type"] == "modem_tcp"
    assert "pymc_tcp" not in written
    assert written["radios"][0]["radio_type"] == "modem_usb"
    assert "pymc_usb" not in written["radios"][0]


def test_save_config_serializes_only_canonical_modem_keys(tmp_path):
    from repeater.config import save_config

    path = tmp_path / "config.yaml"
    config = {
        "radio_type": "pymc_usb",
        "pymc_usb": {"port": "/dev/ttyACM0"},
    }

    assert save_config(config, str(path)) is True

    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert written == {
        "radio_type": "modem_usb",
        "modem_usb": {"port": "/dev/ttyACM0"},
    }


def test_sensor_and_gps_aliases_migrate_to_canonical_names():
    normalized = normalize_modem_config(
        {
            "sensors": {
                "definitions": [
                    {"name": "modem", "type": "pymc_modem", "settings": {"host": "x"}},
                    {"name": "system", "type": "hardware_stats"},
                ]
            },
            "gps": {"source": "pymc_modem"},
        }
    )

    assert normalized["sensors"]["definitions"][0]["type"] == "openhop_modem"
    assert normalized["sensors"]["definitions"][1]["type"] == "hardware_stats"
    assert normalized["gps"]["source"] == "modem_http"
