"""Tests for plugin manifest parsing."""

import json
import zipfile
from pathlib import Path

import pytest

from repeater.plugins.manifest import ManifestError, load_manifest_from_wheel, parse_manifest


def test_valid_service_plugin():
    m = parse_manifest(
        {
            "schema": 1,
            "id": "openhop.nomad",
            "name": "NOMAD Bridge",
            "version": "0.1.0",
            "runtime": {"type": "python", "entrypoint": "meshcore-nomad-bridge"},
        }
    )
    assert m.id == "openhop.nomad"
    assert m.runtime.entrypoint == "meshcore-nomad-bridge"
    assert m.ui is None


def test_valid_ui_application_plugin():
    m = parse_manifest(
        {
            "schema": 1,
            "id": "openhop.console",
            "name": "Console",
            "version": "1.0.0",
            "ui": {"type": "application", "entry": "ui/index.html"},
        }
    )
    assert m.ui.entry == "ui/index.html"
    assert m.runtime is None


def test_valid_hybrid_plugin():
    m = parse_manifest(
        {
            "schema": 1,
            "id": "openhop.hybrid",
            "name": "Hybrid",
            "version": "2.3.4",
            "runtime": {"type": "python", "entrypoint": "hybrid-svc"},
            "ui": {"type": "application", "entry": "web/index.html"},
        }
    )
    assert m.runtime is not None and m.ui is not None


def test_unsupported_schema():
    with pytest.raises(ManifestError, match="schema"):
        parse_manifest(
            {
                "schema": 99,
                "id": "x.y",
                "name": "X",
                "version": "1.0.0",
                "runtime": {"type": "python", "entrypoint": "x"},
            }
        )


def test_unsafe_plugin_id():
    with pytest.raises(ManifestError):
        parse_manifest(
            {
                "schema": 1,
                "id": "../etc",
                "name": "X",
                "version": "1.0.0",
                "runtime": {"type": "python", "entrypoint": "x"},
            }
        )
    with pytest.raises(ManifestError):
        parse_manifest(
            {
                "schema": 1,
                "id": "Bad_ID",
                "name": "X",
                "version": "1.0.0",
                "runtime": {"type": "python", "entrypoint": "x"},
            }
        )


def test_unsafe_ui_path():
    with pytest.raises(ManifestError, match="parent-directory"):
        parse_manifest(
            {
                "schema": 1,
                "id": "openhop.x",
                "name": "X",
                "version": "1.0.0",
                "ui": {"type": "application", "entry": "../../etc/passwd"},
            }
        )


def test_unsupported_runtime():
    with pytest.raises(ManifestError, match="runtime.type"):
        parse_manifest(
            {
                "schema": 1,
                "id": "openhop.x",
                "name": "X",
                "version": "1.0.0",
                "runtime": {"type": "docker", "entrypoint": "x"},
            }
        )


def test_requires_runtime_or_ui():
    with pytest.raises(ManifestError, match="runtime and/or ui"):
        parse_manifest({"schema": 1, "id": "openhop.x", "name": "X", "version": "1.0.0"})


def test_manifest_config_defaults():
    m = parse_manifest(
        {
            "schema": 1,
            "id": "openhop.demo",
            "name": "Demo",
            "version": "0.1.0",
            "runtime": {"type": "python", "entrypoint": "demo"},
            "config": {"defaults": {"host": "127.0.0.1", "port": 1}},
        }
    )
    assert m.config_defaults == {"host": "127.0.0.1", "port": 1}
    assert m.to_dict()["config"]["defaults"]["port"] == 1


def test_load_manifest_from_wheel(tmp_path: Path):
    wheel = tmp_path / "demo-0.1.0-py3-none-any.whl"
    manifest = {
        "schema": 1,
        "id": "openhop.demo",
        "name": "Demo",
        "version": "0.1.0",
        "runtime": {"type": "python", "entrypoint": "demo-cli"},
    }
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr(
            "share/openhop/plugins/openhop.demo/openhop-plugin.json",
            json.dumps(manifest),
        )
    loaded = load_manifest_from_wheel(wheel)
    assert loaded.id == "openhop.demo"
