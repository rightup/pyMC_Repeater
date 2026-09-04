"""Minimal GitHub Releases client for plugin wheel discovery and download."""

from __future__ import annotations

import json
import logging
import re
import ssl
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("PluginGitHubReleases")

GITHUB_API_BASE = "https://api.github.com"
DEFAULT_CACHE_TTL_SECONDS = 600  # 10 minutes
USER_AGENT = "openhop-repeater-plugin-manager/1.0"

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ssl_ctx: Optional[ssl.SSLContext] = None


class GitHubReleasesError(Exception):
    """User-facing GitHub Releases failure."""

    def __init__(self, message: str, *, code: int = 502):
        super().__init__(message)
        self.code = code


class GitHubRateLimitError(GitHubReleasesError):
    def __init__(self, message: str = "GitHub API rate limit exceeded"):
        super().__init__(message, code=429)


def _ssl_context() -> ssl.SSLContext:
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def normalize_repository(repository: str) -> str:
    repo = str(repository or "").strip()
    if repo.startswith("https://github.com/"):
        repo = repo[len("https://github.com/") :]
    if repo.startswith("http://github.com/"):
        repo = repo[len("http://github.com/") :]
    repo = repo.strip("/")
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not _REPO_RE.match(repo):
        raise GitHubReleasesError(f"invalid repository format: {repository!r}", code=400)
    return repo


def normalize_version(tag_or_version: str) -> str:
    """Strip a leading ``v`` from release tags (``v1.2.3`` → ``1.2.3``)."""
    value = str(tag_or_version or "").strip()
    if value.lower().startswith("v") and len(value) > 1 and value[1].isdigit():
        return value[1:]
    return value


def _parse_version_key(version: str):
    try:
        from packaging.version import Version

        return Version(normalize_version(version))
    except Exception:
        return normalize_version(version)


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    browser_download_url: str
    size: int = 0
    content_type: str = ""


@dataclass(frozen=True)
class PluginRelease:
    tag: str
    version: str
    name: str
    body: str
    prerelease: bool
    draft: bool
    html_url: str
    published_at: str
    assets: tuple[ReleaseAsset, ...]

    def wheel_assets(self) -> list[ReleaseAsset]:
        return [a for a in self.assets if a.name.endswith(".whl")]


def select_wheel_asset(release: PluginRelease) -> ReleaseAsset:
    wheels = release.wheel_assets()
    if not wheels:
        raise GitHubReleasesError(
            f"release {release.tag} has no .whl asset",
            code=400,
        )
    if len(wheels) > 1:
        names = ", ".join(a.name for a in wheels)
        raise GitHubReleasesError(
            f"release {release.tag} has multiple .whl assets ({names}); "
            "attach exactly one plugin wheel",
            code=400,
        )
    return wheels[0]


class GitHubReleaseClient:
    """Fetch and cache GitHub Releases for ``owner/repo`` plugin projects."""

    def __init__(
        self,
        *,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        opener: Optional[Callable[..., Any]] = None,
        user_agent: str = USER_AGENT,
    ):
        self.cache_ttl = cache_ttl
        self._opener = opener or self._default_open
        self.user_agent = user_agent
        # repo -> (monotonic_ts, list[PluginRelease])
        self._cache: dict[str, tuple[float, list[PluginRelease]]] = {}

    def _default_open(self, request: urllib.request.Request, timeout: float = 30.0):
        return urllib.request.urlopen(  # nosec B310 - https only to api.github.com / github
            request, timeout=timeout, context=_ssl_context()
        )

    def clear_cache(self, repository: Optional[str] = None) -> None:
        if repository is None:
            self._cache.clear()
            return
        self._cache.pop(normalize_repository(repository), None)

    def list_releases(
        self,
        repository: str,
        *,
        include_prereleases: bool = False,
        force_refresh: bool = False,
    ) -> list[PluginRelease]:
        repo = normalize_repository(repository)
        now = time.monotonic()
        if not force_refresh and repo in self._cache:
            ts, cached = self._cache[repo]
            if (now - ts) < self.cache_ttl:
                return self._filter(cached, include_prereleases=include_prereleases)

        raw = self._fetch_releases_json(repo)
        releases = [self._parse_release(item) for item in raw if isinstance(item, dict)]
        # Keep drafts out of cache entirely
        releases = [r for r in releases if not r.draft]
        releases.sort(key=lambda r: _parse_version_key(r.version), reverse=True)
        self._cache[repo] = (now, releases)
        return self._filter(releases, include_prereleases=include_prereleases)

    @staticmethod
    def _filter(releases: list[PluginRelease], *, include_prereleases: bool) -> list[PluginRelease]:
        if include_prereleases:
            return list(releases)
        return [r for r in releases if not r.prerelease]

    def latest_stable(
        self, repository: str, *, force_refresh: bool = False
    ) -> Optional[PluginRelease]:
        releases = self.list_releases(
            repository, include_prereleases=False, force_refresh=force_refresh
        )
        return releases[0] if releases else None

    def find_release(
        self,
        repository: str,
        version: str,
        *,
        include_prereleases: bool = False,
        force_refresh: bool = False,
    ) -> PluginRelease:
        target = normalize_version(version)
        releases = self.list_releases(
            repository,
            include_prereleases=include_prereleases,
            force_refresh=force_refresh,
        )
        for rel in releases:
            if normalize_version(rel.version) == target or rel.tag == version:
                return rel
        raise GitHubReleasesError(
            f"release version {version!r} not found for {repository}",
            code=404,
        )

    def download_wheel(
        self,
        release: PluginRelease,
        dest_dir: Path | str,
        *,
        asset: Optional[ReleaseAsset] = None,
    ) -> Path:
        """Download the release wheel into dest_dir; return the local path."""
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        chosen = asset or select_wheel_asset(release)
        dest = dest_dir / Path(chosen.name).name
        if not dest.name.endswith(".whl"):
            raise GitHubReleasesError(f"refusing non-wheel asset: {chosen.name}", code=400)

        request = urllib.request.Request(
            chosen.browser_download_url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/octet-stream",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=120.0) as resp:
                data = resp.read()
        except GitHubReleasesError:
            raise
        except urllib.error.HTTPError as exc:
            raise self._map_http_error(exc) from exc
        except urllib.error.URLError as exc:
            raise GitHubReleasesError(f"failed to download wheel: {exc.reason}", code=502) from exc
        except Exception as exc:
            raise GitHubReleasesError(f"failed to download wheel: {exc}", code=502) from exc

        if not data:
            raise GitHubReleasesError("downloaded wheel is empty", code=502)
        dest.write_bytes(data)
        return dest

    def download_latest_wheel(
        self,
        repository: str,
        dest_dir: Path | str,
        *,
        version: Optional[str] = None,
        force_refresh: bool = False,
    ) -> tuple[PluginRelease, Path]:
        if version:
            release = self.find_release(repository, version, force_refresh=force_refresh)
        else:
            release = self.latest_stable(repository, force_refresh=force_refresh)
            if release is None:
                raise GitHubReleasesError(f"no stable releases found for {repository}", code=404)
        path = self.download_wheel(release, dest_dir)
        return release, path

    def _fetch_releases_json(self, repo: str) -> list[Any]:
        url = f"{GITHUB_API_BASE}/repos/{repo}/releases?per_page=30"
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/vnd.github+json",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=30.0) as resp:
                body = resp.read()
        except GitHubReleasesError:
            raise
        except urllib.error.HTTPError as exc:
            raise self._map_http_error(exc) from exc
        except urllib.error.URLError as exc:
            raise GitHubReleasesError(
                f"GitHub Releases unavailable: {exc.reason}", code=502
            ) from exc
        except Exception as exc:
            raise GitHubReleasesError(f"GitHub Releases unavailable: {exc}", code=502) from exc

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubReleasesError(f"invalid GitHub Releases response: {exc}", code=502) from exc
        if not isinstance(data, list):
            raise GitHubReleasesError("unexpected GitHub Releases response shape", code=502)
        return data

    @staticmethod
    def _map_http_error(exc: urllib.error.HTTPError) -> GitHubReleasesError:
        if exc.code == 403:
            # Rate limit or auth — treat 403 with rate-limit headers as 429
            remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
            if remaining == "0" or "rate limit" in str(exc.reason).lower():
                return GitHubRateLimitError("GitHub API rate limit exceeded; try again later")
            return GitHubReleasesError(f"GitHub API forbidden ({exc.reason})", code=403)
        if exc.code == 404:
            return GitHubReleasesError("GitHub repository or releases not found", code=404)
        return GitHubReleasesError(f"GitHub API error HTTP {exc.code}: {exc.reason}", code=502)

    @staticmethod
    def _parse_release(item: dict[str, Any]) -> PluginRelease:
        tag = str(item.get("tag_name") or "").strip()
        assets_raw = item.get("assets") or []
        assets: list[ReleaseAsset] = []
        if isinstance(assets_raw, list):
            for raw in assets_raw:
                if not isinstance(raw, dict):
                    continue
                name = str(raw.get("name") or "").strip()
                url = str(raw.get("browser_download_url") or "").strip()
                if not name or not url:
                    continue
                assets.append(
                    ReleaseAsset(
                        name=name,
                        browser_download_url=url,
                        size=int(raw.get("size") or 0),
                        content_type=str(raw.get("content_type") or ""),
                    )
                )
        return PluginRelease(
            tag=tag,
            version=normalize_version(tag),
            name=str(item.get("name") or tag),
            body=str(item.get("body") or ""),
            prerelease=bool(item.get("prerelease")),
            draft=bool(item.get("draft")),
            html_url=str(item.get("html_url") or ""),
            published_at=str(item.get("published_at") or ""),
            assets=tuple(assets),
        )


def make_download_temp_dir(parent: Path | str) -> Path:
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="openhop-plugin-download-", dir=str(parent)))
