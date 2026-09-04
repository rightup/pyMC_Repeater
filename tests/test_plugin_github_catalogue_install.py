"""R2 catalogue lookup with approved GitHub Release install and update flows."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock
from urllib.request import Request

import pytest

from repeater.plugins.catalogue import CatalogueClient
from repeater.plugins.github_releases import GitHubReleaseClient
from repeater.plugins.manager import PluginManager, PluginManagerError
from repeater.plugins.storage import PluginStorage


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _make_wheel(path: Path, plugin_id: str, version: str) -> bytes:
    manifest = {
        "schema": 1,
        "id": plugin_id,
        "name": plugin_id,
        "version": version,
        "description": "test",
        "ui": {"type": "application", "entry": "ui/index.html"},
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"share/openhop/plugins/{plugin_id}/openhop-plugin.json",
            json.dumps(manifest),
        )
        archive.writestr("ui/index.html", "<html></html>")
        dist = f"test_plugin-{version}.dist-info"
        archive.writestr(
            f"{dist}/METADATA",
            f"Metadata-Version: 2.1\nName: test-plugin\nVersion: {version}\n",
        )
        archive.writestr(
            f"{dist}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{dist}/RECORD", "")
    return path.read_bytes()


def _entry(version: str, wheel: bytes) -> dict:
    return {
        "id": "openhop.demo",
        "name": "Demo",
        "description": "demo plugin",
        "repository": "openhop-dev/demo-plugin",
        "category": "test",
        "version": version,
        "wheel_url": (
            f"https://github.com/openhop-dev/demo-plugin/releases/download/v{version}/"
            f"demo-{version}-py3-none-any.whl"
        ),
        "sha256": hashlib.sha256(wheel).hexdigest(),
    }


@pytest.fixture
def catalogue_manager(tmp_path: Path):
    wheels = {
        "0.1.0": _make_wheel(tmp_path / "demo-0.1.0.whl", "openhop.demo", "0.1.0"),
        "0.2.0": _make_wheel(tmp_path / "demo-0.2.0.whl", "openhop.demo", "0.2.0"),
    }
    state = {"entry": _entry("0.1.0", wheels["0.1.0"]), "requests": []}

    def opener(request: Request, timeout: float = 30.0):
        state["requests"].append(request.full_url)
        if request.full_url.endswith("catalogue.json"):
            return _FakeResp(json.dumps({"schema": 2, "plugins": [state["entry"]]}).encode())
        for version, wheel in wheels.items():
            if request.full_url == _entry(version, wheel)["wheel_url"]:
                return _FakeResp(wheel)
        raise AssertionError(request.full_url)

    catalogue = CatalogueClient("https://example.test/catalogue.json", opener=opener)
    github = MagicMock(spec=GitHubReleaseClient)
    manager = PluginManager(
        PluginStorage(tmp_path / "plugins"),
        catalogue_client=catalogue,
        github_client=github,
    )
    manager._catalogue_test_state = state  # type: ignore[attr-defined]
    manager._catalogue_test_wheels = wheels  # type: ignore[attr-defined]
    return manager


def test_schema2_install_uses_only_approved_github_wheel(catalogue_manager: PluginManager):
    status = catalogue_manager.install_from_catalogue("openhop.demo")

    assert status["version"] == "0.1.0"
    assert status["enabled"] is True
    assert status["source"] == "catalogue"
    assert all(
        "api.github.com" not in url for url in catalogue_manager._catalogue_test_state["requests"]
    )
    catalogue_manager.github.latest_stable.assert_not_called()
    catalogue_manager.github.download_latest_wheel.assert_not_called()


def test_schema2_catalogue_version_controls_updates(catalogue_manager: PluginManager):
    catalogue_manager.install_from_catalogue("openhop.demo")
    wheels = catalogue_manager._catalogue_test_wheels
    catalogue_manager._catalogue_test_state["entry"] = _entry("0.2.0", wheels["0.2.0"])
    catalogue_manager.catalogue.clear_cache()

    check = catalogue_manager.check_update("openhop.demo", force_refresh=True)
    assert check["latestVersion"] == "0.2.0"
    assert check["updateAvailable"] is True

    updated = catalogue_manager.update_plugin("openhop.demo", force_refresh=True)
    assert updated["version"] == "0.2.0"
    assert updated["enabled"] is True
    catalogue_manager.github.latest_stable.assert_not_called()
    catalogue_manager.github.download_wheel.assert_not_called()


def test_schema2_rejects_unapproved_requested_version(catalogue_manager: PluginManager):
    with pytest.raises(PluginManagerError, match="not approved"):
        catalogue_manager.install_from_catalogue("openhop.demo", version="9.9.9")


def test_schema2_checksum_failure_does_not_install(catalogue_manager: PluginManager):
    catalogue_manager._catalogue_test_state["entry"]["sha256"] = "0" * 64
    catalogue_manager.catalogue.clear_cache()

    with pytest.raises(PluginManagerError, match="checksum"):
        catalogue_manager.install_from_catalogue("openhop.demo", force_refresh=True)
    assert catalogue_manager.storage.list_plugin_ids() == []
