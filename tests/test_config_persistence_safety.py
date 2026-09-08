import copy
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest
import yaml

from repeater.config_manager import ConfigManager


@pytest.mark.parametrize("failure", ["dump", "fsync", "replace"])
def test_failed_update_preserves_file_and_shared_config(tmp_path, monkeypatch, failure):
    path = tmp_path / "config.yaml"
    config = {"repeater": {"node_name": "before"}, "radio_type": "pymc_tcp"}
    before = copy.deepcopy(config)
    path.write_text(yaml.safe_dump(config))
    original = path.read_bytes()
    section = config["repeater"]
    manager = ConfigManager(str(path), config)
    live = Mock()
    monkeypatch.setattr(manager, "live_update_daemon", live)

    def fail(*args, **kwargs):
        if failure == "dump":
            args[1].write("partial: ")
        assert config == before
        raise OSError("synthetic write failure")

    target = "yaml.safe_dump" if failure == "dump" else f"os.{failure}"
    monkeypatch.setattr(f"repeater.config_manager.{target}", fail)
    result = manager.update_and_save({"repeater": {"node_name": "after"}})
    assert result["success"] is False
    assert result["saved"] is False
    assert result["live_updated"] is False
    assert config == before
    assert config["repeater"] is section
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    live.assert_not_called()


def test_failed_direct_save_does_not_normalize_shared_config(tmp_path, monkeypatch):
    config = {"radio_type": "pymc_tcp", "pymc_tcp": {"host": "synthetic"}}
    before = copy.deepcopy(config)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    original = path.read_bytes()
    monkeypatch.setattr("repeater.config_manager.yaml.safe_dump", Mock(side_effect=OSError()))
    assert not ConfigManager(str(path), config).save_to_file()
    assert config == before
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("mode", [None, 0o600, 0o640])
def test_atomic_save_permissions_and_publication(tmp_path, monkeypatch, mode):
    path = tmp_path / "config.yaml"
    config = {"repeater": {"node_name": "before", "other": 1}}
    section = config["repeater"]
    if mode is not None:
        path.write_text(yaml.safe_dump(config))
        path.chmod(mode)
    replace = os.replace
    observed = []

    def checked_replace(source, destination):
        assert config["repeater"]["node_name"] == "before"
        assert stat.S_IMODE(os.stat(source).st_mode) == (mode or 0o600)
        assert os.stat(source).st_uid == os.getuid()
        observed.append(True)
        replace(source, destination)

    monkeypatch.setattr("repeater.config_manager.os.replace", checked_replace)
    assert ConfigManager(str(path), config).update_and_save(
        {"repeater": {"node_name": "after"}}, live_update=False
    )["saved"]
    assert observed == [True]
    assert config["repeater"] is section
    assert yaml.safe_load(path.read_text()) == config
    assert stat.S_IMODE(path.stat().st_mode) == (mode or 0o600)
    assert list(tmp_path.iterdir()) == [path]


def test_concurrent_managers_serialize_staging_and_saving(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    config = {"repeater": {"node_name": "before"}}
    first = ConfigManager(str(path), config)
    second = ConfigManager(str(path), config)
    entered, release, second_started, second_dump = Event(), Event(), Event(), Event()
    dump = yaml.safe_dump

    def paused_dump(data, *args, **kwargs):
        if data["repeater"].get("first") and not data["repeater"].get("second"):
            entered.set()
            assert release.wait(5)
        else:
            second_dump.set()
        return dump(data, *args, **kwargs)

    def second_update():
        second_started.set()
        return second.update_and_save({"repeater": {"second": True}}, live_update=False)

    monkeypatch.setattr("repeater.config_manager.yaml.safe_dump", paused_dump)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first.update_and_save, {"repeater": {"first": True}}, False)
        try:
            assert entered.wait(5)
            b = pool.submit(second_update)
            assert second_started.wait(5)
            assert not second_dump.wait(0.1)
            assert config == {"repeater": {"node_name": "before"}}
        finally:
            release.set()
        assert a.result()["saved"]
        assert b.result()["saved"]
    assert config["repeater"] == {"node_name": "before", "first": True, "second": True}
    assert yaml.safe_load(path.read_text()) == config
