"""Catalogue install / update flows against a real PluginManager + mocked HTTP."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from urllib.request import Request

import pytest

from repeater.plugins.catalogue import CatalogueClient
from repeater.plugins.github_releases import GitHubReleaseClient
from repeater.plugins.manager import PluginManager, PluginManagerError
from repeater.plugins.storage import PluginStorage


def _make_wheel(path: Path, plugin_id: str, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": 1,
        "id": plugin_id,
        "name": plugin_id,
        "version": version,
        "description": "test",
        "ui": {"type": "application", "entry": "ui/index.html"},
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            f"share/openhop/plugins/{plugin_id}/openhop-plugin.json",
            json.dumps(manifest),
        )
        zf.writestr("ui/index.html", "<html></html>")
        dist = f"test_plugin-{version}.dist-info"
        zf.writestr(
            f"{dist}/METADATA",
            f"Metadata-Version: 2.1\nName: test-plugin\nVersion: {version}\n",
        )
        zf.writestr(
            f"{dist}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )
        zf.writestr(f"{dist}/RECORD", "")
    return path


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


CATALOGUE = {
    "schema": 1,
    "plugins": [
        {
            "id": "openhop.demo",
            "name": "Demo",
            "description": "demo plugin",
            "repository": "openhop-dev/demo-plugin",
            "category": "test",
        }
    ],
}


def _release(tag: str, wheel_name: str, url: str):
    return {
        "tag_name": tag,
        "name": tag,
        "body": f"notes {tag}",
        "draft": False,
        "prerelease": False,
        "html_url": f"https://github.com/openhop-dev/demo-plugin/releases/tag/{tag}",
        "published_at": "2026-01-01T00:00:00Z",
        "assets": [
            {
                "name": wheel_name,
                "browser_download_url": url,
                "size": 100,
                "content_type": "application/octet-stream",
            }
        ],
    }


@pytest.fixture
def manager(tmp_path: Path):
    wheels = {
        "https://example.test/demo-0.1.0.whl": _make_wheel(
            tmp_path / "w0.1.0.whl", "openhop.demo", "0.1.0"
        ),
        "https://example.test/demo-0.2.0.whl": _make_wheel(
            tmp_path / "w0.2.0.whl", "openhop.demo", "0.2.0"
        ),
        "https://example.test/wrong-id.whl": _make_wheel(
            tmp_path / "wrong.whl", "other.id", "0.1.0"
        ),
    }
    releases_v1 = [
        _release("v0.1.0", "demo-0.1.0-py3-none-any.whl", "https://example.test/demo-0.1.0.whl")
    ]
    state = {"releases": releases_v1}

    def opener(request: Request, timeout: float = 30.0):
        url = request.full_url
        if url.endswith("catalogue.json"):
            return _FakeResp(json.dumps(CATALOGUE).encode())
        if "api.github.com" in url and "/releases" in url:
            return _FakeResp(json.dumps(state["releases"]).encode())
        if url in wheels:
            return _FakeResp(wheels[url].read_bytes())
        raise AssertionError(url)

    storage = PluginStorage(tmp_path / "plugins")
    cat = CatalogueClient("https://example.test/catalogue.json", opener=opener)
    gh = GitHubReleaseClient(opener=opener)
    mgr = PluginManager(storage, catalogue_client=cat, github_client=gh)
    mgr._test_state = state  # type: ignore[attr-defined]
    return mgr


def test_catalogue_install_enables_and_sets_source(manager: PluginManager, tmp_path: Path):
    status = manager.install_from_catalogue("openhop.demo")
    assert status["id"] == "openhop.demo"
    assert status["enabled"] is True
    assert status["version"] == "0.1.0"
    assert status["source"] == "catalogue"
    assert status["repository"] == "openhop-dev/demo-plugin"
    # data dir exists and survives
    data = Path(status["data_dir"])
    assert data.is_dir()
    (data / "config.json").write_text('{"kept": true}', encoding="utf-8")

    # update available after bumping releases
    manager._test_state["releases"] = [  # type: ignore[attr-defined]
        {
            "tag_name": "v0.2.0",
            "name": "v0.2.0",
            "body": "newer",
            "draft": False,
            "prerelease": False,
            "html_url": "https://github.com/x/y/releases/tag/v0.2.0",
            "published_at": "2026-02-01T00:00:00Z",
            "assets": [
                {
                    "name": "demo-0.2.0-py3-none-any.whl",
                    "browser_download_url": "https://example.test/demo-0.2.0.whl",
                    "size": 1,
                    "content_type": "application/octet-stream",
                }
            ],
        },
        {
            "tag_name": "v0.1.0",
            "name": "v0.1.0",
            "body": "",
            "draft": False,
            "prerelease": False,
            "html_url": "",
            "published_at": "",
            "assets": [
                {
                    "name": "demo-0.1.0-py3-none-any.whl",
                    "browser_download_url": "https://example.test/demo-0.1.0.whl",
                    "size": 1,
                    "content_type": "application/octet-stream",
                }
            ],
        },
    ]
    manager.github.clear_cache()
    check = manager.check_update("openhop.demo", force_refresh=True)
    assert check["updateAvailable"] is True
    assert check["latestVersion"] == "0.2.0"

    updated = manager.update_plugin("openhop.demo", force_refresh=True)
    assert updated["version"] == "0.2.0"
    assert updated["enabled"] is True
    assert json.loads((data / "config.json").read_text(encoding="utf-8"))["kept"] is True
    # temp download cleaned
    download = manager.storage.root / ".download"
    if download.exists():
        assert list(download.iterdir()) == [] or all(
            not p.name.startswith("openhop-plugin-download-") or not p.exists()
            for p in download.iterdir()
        )


def test_manifest_id_mismatch_rejected(manager: PluginManager):
    # Point catalogue download at wrong-id wheel by swapping releases
    manager._test_state["releases"] = [  # type: ignore[attr-defined]
        {
            "tag_name": "v0.1.0",
            "name": "v0.1.0",
            "body": "",
            "draft": False,
            "prerelease": False,
            "html_url": "",
            "published_at": "",
            "assets": [
                {
                    "name": "wrong-0.1.0-py3-none-any.whl",
                    "browser_download_url": "https://example.test/wrong-id.whl",
                    "size": 1,
                    "content_type": "application/octet-stream",
                }
            ],
        }
    ]
    manager.github.clear_cache()
    with pytest.raises(PluginManagerError, match="does not match catalogue id"):
        manager.install_from_catalogue("openhop.demo", force_refresh=True)


def test_same_version_no_update(manager: PluginManager):
    manager.install_from_catalogue("openhop.demo")
    check = manager.check_update("openhop.demo")
    assert check["updateAvailable"] is False
    status = manager.update_plugin("openhop.demo")
    assert status.get("updated") is False
    assert status["version"] == "0.1.0"


def test_list_catalogue_annotates_installed(manager: PluginManager):
    listed = manager.list_catalogue()
    assert listed["plugins"][0]["installed"] is False
    manager.install_from_catalogue("openhop.demo")
    manager.github.clear_cache()
    listed2 = manager.list_catalogue(force_refresh=True)
    row = listed2["plugins"][0]
    assert row["installed"] is True
    assert row["installedVersion"] == "0.1.0"
    assert row["latestVersion"] == "0.1.0"


def test_local_install_update_unavailable(manager: PluginManager, tmp_path: Path):
    wheel = _make_wheel(tmp_path / "local.whl", "openhop.local", "1.0.0")
    manager.install(wheel)
    check = manager.check_update("openhop.local")
    assert check["updateAvailable"] is False
    assert "unavailable" in (check.get("reason") or "").lower()
