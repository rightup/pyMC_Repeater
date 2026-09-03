"""Unit tests for GitHub Releases plugin client."""

from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from repeater.plugins.github_releases import (
    GitHubRateLimitError,
    GitHubReleaseClient,
    GitHubReleasesError,
    PluginRelease,
    ReleaseAsset,
    normalize_version,
    select_wheel_asset,
)


def _release(
    tag: str,
    *,
    draft: bool = False,
    prerelease: bool = False,
    assets: list[dict] | None = None,
):
    return {
        "tag_name": tag,
        "name": tag,
        "body": f"notes for {tag}",
        "draft": draft,
        "prerelease": prerelease,
        "html_url": f"https://github.com/org/repo/releases/tag/{tag}",
        "published_at": "2026-01-01T00:00:00Z",
        "assets": assets
        or [
            {
                "name": f"pkg-{tag.lstrip('v')}-py3-none-any.whl",
                "browser_download_url": f"https://example.test/{tag}.whl",
                "size": 12,
                "content_type": "application/octet-stream",
            }
        ],
    }


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_normalize_version():
    assert normalize_version("v1.2.3") == "1.2.3"
    assert normalize_version("1.2.3") == "1.2.3"
    assert normalize_version("V0.1.0") == "0.1.0"


def test_stable_release_discovery_and_ordering():
    payload = [
        _release("v0.1.0"),
        _release("v0.3.0"),
        _release("v0.2.0", prerelease=True),
        _release("v0.4.0", draft=True),
    ]

    def opener(request: Request, timeout: float = 30.0):
        if "api.github.com" in request.full_url:
            return _FakeResp(json.dumps(payload).encode())
        raise AssertionError(request.full_url)

    client = GitHubReleaseClient(opener=opener, cache_ttl=60)
    releases = client.list_releases("openhop-dev/openhop-nomad-plugin")
    assert [r.version for r in releases] == ["0.3.0", "0.1.0"]
    latest = client.latest_stable("openhop-dev/openhop-nomad-plugin")
    assert latest.version == "0.3.0"


def test_wheel_asset_selection():
    rel = PluginRelease(
        tag="v1.0.0",
        version="1.0.0",
        name="1.0.0",
        body="",
        prerelease=False,
        draft=False,
        html_url="",
        published_at="",
        assets=(
            ReleaseAsset("notes.txt", "https://x/notes.txt"),
            ReleaseAsset("pkg-1.0.0-py3-none-any.whl", "https://x/pkg.whl"),
        ),
    )
    assert select_wheel_asset(rel).name.endswith(".whl")

    none = PluginRelease(
        tag="v1",
        version="1",
        name="",
        body="",
        prerelease=False,
        draft=False,
        html_url="",
        published_at="",
        assets=(ReleaseAsset("x.zip", "https://x/x.zip"),),
    )
    with pytest.raises(GitHubReleasesError, match="no .whl"):
        select_wheel_asset(none)

    many = PluginRelease(
        tag="v1",
        version="1",
        name="",
        body="",
        prerelease=False,
        draft=False,
        html_url="",
        published_at="",
        assets=(
            ReleaseAsset("a.whl", "https://x/a.whl"),
            ReleaseAsset("b.whl", "https://x/b.whl"),
        ),
    )
    with pytest.raises(GitHubReleasesError, match="multiple"):
        select_wheel_asset(many)


def test_download_wheel(tmp_path: Path):
    payload = [_release("v0.1.0")]

    def opener(request: Request, timeout: float = 30.0):
        if "api.github.com" in request.full_url:
            return _FakeResp(json.dumps(payload).encode())
        if request.full_url.endswith(".whl"):
            return _FakeResp(b"wheel-bytes")
        raise AssertionError(request.full_url)

    client = GitHubReleaseClient(opener=opener)
    release, path = client.download_latest_wheel("org/repo", tmp_path)
    assert release.version == "0.1.0"
    assert path.read_bytes() == b"wheel-bytes"
    assert path.name.endswith(".whl")


def test_rate_limit():
    def opener(request: Request, timeout: float = 30.0):
        headers = {"X-RateLimit-Remaining": "0"}
        raise HTTPError(request.full_url, 403, "rate limit", hdrs=headers, fp=None)

    client = GitHubReleaseClient(opener=opener)
    with pytest.raises(GitHubRateLimitError):
        client.list_releases("org/repo")


def test_cache_behaviour():
    calls = {"n": 0}
    payload = [_release("v1.0.0")]

    def opener(request: Request, timeout: float = 30.0):
        calls["n"] += 1
        return _FakeResp(json.dumps(payload).encode())

    client = GitHubReleaseClient(opener=opener, cache_ttl=60)
    client.list_releases("org/repo")
    client.list_releases("org/repo")
    assert calls["n"] == 1
    client.list_releases("org/repo", force_refresh=True)
    assert calls["n"] == 2
