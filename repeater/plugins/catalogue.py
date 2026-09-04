"""Curated openHop plugin catalogue client (static JSON over HTTPS)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import ssl
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("PluginCatalogue")

DEFAULT_CATALOGUE_URL = "https://repeater-plugins.openhop.dev/catalogue.json"
DEFAULT_CACHE_TTL_SECONDS = 600  # 10 minutes
SUPPORTED_SCHEMAS = (1, 2)
USER_AGENT = "openhop-repeater-plugin-manager/1.0"
APPROVED_WHEEL_ORIGIN = "https://repeater-plugins.openhop.dev"
MAX_WHEEL_BYTES = 100 * 1024 * 1024

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_ssl_ctx: Optional[ssl.SSLContext] = None


class CatalogueError(Exception):
    def __init__(self, message: str, *, code: int = 502):
        super().__init__(message)
        self.code = code


def _ssl_context() -> ssl.SSLContext:
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def _validate_approved_wheel_url(plugin_id: str, version: str, wheel_url: str) -> None:
    parsed = urlsplit(wheel_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    expected_prefix = f"/plugins/{plugin_id}/{version}/"
    if (
        origin != APPROVED_WHEEL_ORIGIN
        or not parsed.path.startswith(expected_prefix)
        or not parsed.path.endswith(".whl")
        or parsed.query
        or parsed.fragment
    ):
        raise CatalogueError(
            f"wheel_url must be an approved R2 wheel under "
            f"{APPROVED_WHEEL_ORIGIN}{expected_prefix}",
            code=400,
        )


@dataclass(frozen=True)
class CataloguePlugin:
    id: str
    name: str
    description: str
    repository: str
    category: str = ""
    logo: str = ""
    version: Optional[str] = None
    wheel_url: Optional[str] = None
    sha256: Optional[str] = None

    @property
    def has_approved_wheel(self) -> bool:
        return bool(self.version and self.wheel_url and self.sha256)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "repository": self.repository,
        }
        if self.category:
            out["category"] = self.category
        if self.logo:
            out["logo"] = self.logo
        if self.version:
            out["version"] = self.version
        if self.wheel_url:
            out["wheel_url"] = self.wheel_url
        if self.sha256:
            out["sha256"] = self.sha256
        return out


@dataclass(frozen=True)
class Catalogue:
    schema: int
    plugins: tuple[CataloguePlugin, ...]

    def get(self, plugin_id: str) -> Optional[CataloguePlugin]:
        for plugin in self.plugins:
            if plugin.id == plugin_id:
                return plugin
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "plugins": [p.to_dict() for p in self.plugins],
        }


def parse_catalogue(data: Any) -> Catalogue:
    if not isinstance(data, dict):
        raise CatalogueError("catalogue must be a JSON object", code=400)
    schema = data.get("schema")
    if schema not in SUPPORTED_SCHEMAS:
        raise CatalogueError(
            f"unsupported catalogue schema {schema!r}; expected one of {SUPPORTED_SCHEMAS}",
            code=400,
        )
    raw_plugins = data.get("plugins")
    if not isinstance(raw_plugins, list):
        raise CatalogueError("catalogue.plugins must be an array", code=400)

    plugins: list[CataloguePlugin] = []
    seen_ids: set[str] = set()
    seen_repos: set[str] = set()
    seen_wheel_urls: set[str] = set()
    for idx, item in enumerate(raw_plugins):
        if not isinstance(item, dict):
            raise CatalogueError(f"plugins[{idx}] must be an object", code=400)
        plugin_id = item.get("id")
        name = item.get("name")
        description = item.get("description", "")
        repository = item.get("repository")
        category = item.get("category", "") or ""

        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise CatalogueError(f"plugins[{idx}].id is required", code=400)
        plugin_id = plugin_id.strip()
        if not _ID_RE.match(plugin_id):
            raise CatalogueError(f"plugins[{idx}].id is invalid: {plugin_id!r}", code=400)
        if plugin_id in seen_ids:
            raise CatalogueError(f"duplicate plugin id: {plugin_id}", code=400)

        if not isinstance(name, str) or not name.strip():
            raise CatalogueError(f"plugins[{idx}].name is required", code=400)
        if description is None:
            description = ""
        if not isinstance(description, str):
            raise CatalogueError(f"plugins[{idx}].description must be a string", code=400)
        if not isinstance(repository, str) or not repository.strip():
            raise CatalogueError(f"plugins[{idx}].repository is required", code=400)
        repository = repository.strip()
        if not _REPO_RE.match(repository):
            raise CatalogueError(
                f"plugins[{idx}].repository must be owner/repo: {repository!r}",
                code=400,
            )
        if repository in seen_repos:
            raise CatalogueError(f"duplicate repository: {repository}", code=400)
        if category is None:
            category = ""
        if not isinstance(category, str):
            raise CatalogueError(f"plugins[{idx}].category must be a string", code=400)

        logo = item.get("logo", "") or ""
        if logo is None:
            logo = ""
        if not isinstance(logo, str):
            raise CatalogueError(f"plugins[{idx}].logo must be a string", code=400)
        logo = logo.strip()
        if logo and not logo.startswith("https://"):
            raise CatalogueError(
                f"plugins[{idx}].logo must be an https:// URL",
                code=400,
            )

        version = None
        wheel_url = None
        sha256 = None
        if schema == 2:
            raw_version = item.get("version")
            raw_wheel_url = item.get("wheel_url")
            raw_sha256 = item.get("sha256")
            if not isinstance(raw_version, str) or not raw_version.strip():
                raise CatalogueError(f"plugins[{idx}].version is required", code=400)
            if not isinstance(raw_wheel_url, str) or not raw_wheel_url.strip():
                raise CatalogueError(f"plugins[{idx}].wheel_url is required", code=400)
            if not isinstance(raw_sha256, str) or not _SHA256_RE.fullmatch(raw_sha256):
                raise CatalogueError(
                    f"plugins[{idx}].sha256 must be a lowercase SHA-256 digest",
                    code=400,
                )
            version = raw_version.strip()
            wheel_url = raw_wheel_url.strip()
            sha256 = raw_sha256
            try:
                _validate_approved_wheel_url(plugin_id, version, wheel_url)
            except CatalogueError as exc:
                raise CatalogueError(f"plugins[{idx}].{exc}", code=exc.code) from exc
            if wheel_url in seen_wheel_urls:
                raise CatalogueError(f"duplicate wheel URL: {wheel_url}", code=400)
            seen_wheel_urls.add(wheel_url)

        seen_ids.add(plugin_id)
        seen_repos.add(repository)
        plugins.append(
            CataloguePlugin(
                id=plugin_id,
                name=name.strip(),
                description=description.strip(),
                repository=repository,
                category=category.strip(),
                logo=logo,
                version=version,
                wheel_url=wheel_url,
                sha256=sha256,
            )
        )
    return Catalogue(schema=int(schema), plugins=tuple(plugins))


class CatalogueClient:
    """Fetch, cache, and download from the curated plugin catalogue."""

    def __init__(
        self,
        url: str = DEFAULT_CATALOGUE_URL,
        *,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        opener: Optional[Callable[..., Any]] = None,
        user_agent: str = USER_AGENT,
    ):
        self.url = str(url or DEFAULT_CATALOGUE_URL).strip() or DEFAULT_CATALOGUE_URL
        self.cache_ttl = cache_ttl
        self._opener = opener or self._default_open
        self.user_agent = user_agent
        self._cached_at: float = 0.0
        self._cached: Optional[Catalogue] = None

    def _default_open(self, request: urllib.request.Request, timeout: float = 30.0):
        return urllib.request.urlopen(  # nosec B310 - validated HTTPS URLs
            request, timeout=timeout, context=_ssl_context()
        )

    def clear_cache(self) -> None:
        self._cached = None
        self._cached_at = 0.0

    def fetch(self, *, force_refresh: bool = False) -> Catalogue:
        now = time.monotonic()
        if (
            not force_refresh
            and self._cached is not None
            and (now - self._cached_at) < self.cache_ttl
        ):
            return self._cached

        request = urllib.request.Request(
            self.url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=30.0) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raise CatalogueError(
                f"Plugin catalogue is currently unavailable (HTTP {exc.code})",
                code=502,
            ) from exc
        except urllib.error.URLError as exc:
            raise CatalogueError(
                "Plugin catalogue is currently unavailable",
                code=502,
            ) from exc
        except Exception as exc:
            raise CatalogueError(
                "Plugin catalogue is currently unavailable",
                code=502,
            ) from exc

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CatalogueError(f"invalid catalogue JSON: {exc}", code=502) from exc

        catalogue = parse_catalogue(data)
        self._cached = catalogue
        self._cached_at = now
        return catalogue

    def get_plugin(self, plugin_id: str, *, force_refresh: bool = False) -> CataloguePlugin:
        catalogue = self.fetch(force_refresh=force_refresh)
        plugin = catalogue.get(plugin_id)
        if plugin is None:
            raise CatalogueError(f"plugin not in catalogue: {plugin_id}", code=404)
        return plugin

    def download_wheel(self, plugin: CataloguePlugin, dest_dir: Path | str) -> Path:
        """Download and verify one schema-2 approved wheel from R2."""
        if not plugin.version or not plugin.wheel_url or not plugin.sha256:
            raise CatalogueError(
                f"catalogue entry {plugin.id} does not contain approved wheel metadata",
                code=400,
            )
        _validate_approved_wheel_url(plugin.id, plugin.version, plugin.wheel_url)
        if not _SHA256_RE.fullmatch(plugin.sha256):
            raise CatalogueError(f"invalid approved checksum for {plugin.id}", code=400)

        destination_dir = Path(dest_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / Path(urlsplit(plugin.wheel_url).path).name
        fd, temporary_name = tempfile.mkstemp(prefix=".wheel-", dir=str(destination_dir))
        digest = hashlib.sha256()
        total = 0
        request = urllib.request.Request(
            plugin.wheel_url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/octet-stream",
            },
            method="GET",
        )
        try:
            with os.fdopen(fd, "wb") as output:
                try:
                    with self._opener(request, timeout=120.0) as response:
                        final_url = getattr(response, "geturl", lambda: plugin.wheel_url)()
                        _validate_approved_wheel_url(
                            plugin.id,
                            plugin.version,
                            final_url or plugin.wheel_url,
                        )
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > MAX_WHEEL_BYTES:
                                raise CatalogueError(
                                    "approved plugin wheel exceeds 100 MiB", code=502
                                )
                            digest.update(chunk)
                            output.write(chunk)
                except CatalogueError:
                    raise
                except urllib.error.HTTPError as exc:
                    raise CatalogueError(
                        f"approved plugin wheel is unavailable (HTTP {exc.code})", code=502
                    ) from exc
                except urllib.error.URLError as exc:
                    raise CatalogueError("approved plugin wheel is unavailable", code=502) from exc
                except Exception as exc:
                    raise CatalogueError("approved plugin wheel is unavailable", code=502) from exc

            if total == 0:
                raise CatalogueError("approved plugin wheel is empty", code=502)
            if digest.hexdigest() != plugin.sha256:
                raise CatalogueError(
                    f"approved plugin wheel checksum mismatch for {plugin.id}", code=502
                )
            os.replace(temporary_name, destination)
            return destination
        finally:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("Could not remove temporary wheel %s: %s", temporary_name, exc)
