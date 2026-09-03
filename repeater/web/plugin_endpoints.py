"""Plugin management API endpoints (nested under /api/plugins/)."""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional

import cherrypy

from repeater.config import resolve_storage_dir
from repeater.plugins.ipc import PluginIPCClient, PluginIPCError, PluginManagerUnavailable
from repeater.plugins.storage import resolve_plugin_socket_path, resolve_plugins_root

logger = logging.getLogger("HTTPServer")


class PluginAPIEndpoints:
    """CherryPy nested app for /api/plugins/*."""

    def __init__(self, config: Optional[dict] = None, config_path: Optional[str] = None):
        self.config = config or {}
        self.config_path = config_path
        self._client: Optional[PluginIPCClient] = None

    def _socket_path(self) -> Path:
        storage_dir = resolve_storage_dir(self.config, config_path=self.config_path)
        return resolve_plugin_socket_path(self.config, storage_dir=storage_dir)

    def _client_or_raise(self) -> PluginIPCClient:
        path = self._socket_path()
        if self._client is None or self._client.socket_path != path:
            self._client = PluginIPCClient(path)
        if not self._client.available():
            raise PluginManagerUnavailable("Plugin manager unavailable")
        return self._client

    def _ok(self, data: Any = None) -> dict:
        if data is None:
            return {"success": True}
        if isinstance(data, dict):
            return {"success": True, **data}
        return {"success": True, "data": data}

    def _err(self, msg: str, status: int = 400) -> dict:
        cherrypy.response.status = status
        return {"success": False, "error": str(msg)}

    def _handle_ipc(self, fn):
        try:
            return self._ok(fn())
        except PluginManagerUnavailable as exc:
            return self._err(str(exc), 503)
        except PluginIPCError as exc:
            return self._err(str(exc), int(exc.code))
        except cherrypy.HTTPError:
            raise
        except Exception as exc:
            logger.exception("Plugin API error")
            return self._err(str(exc), 500)

    def _require_post(self) -> None:
        if cherrypy.request.method != "POST":
            raise cherrypy.HTTPError(405, "Method Not Allowed")

    def _json_body(self) -> dict:
        body = {}
        try:
            body = getattr(cherrypy.request, "json", None) or {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return {}
        return body

    def _plugin_id_from(self, kwargs: dict, body: Optional[dict] = None) -> str:
        body = body or {}
        plugin_id = (
            kwargs.get("id")
            or kwargs.get("plugin_id")
            or body.get("id")
            or body.get("plugin_id")
            or ""
        )
        plugin_id = str(plugin_id).strip()
        if not plugin_id:
            raise cherrypy.HTTPError(400, "id is required")
        return plugin_id

    # ------------------------------------------------------------------ #
    # Routes
    # ------------------------------------------------------------------ #

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def index(self, **kwargs):
        """GET /api/plugins/ — list installed plugins."""
        if cherrypy.request.method == "OPTIONS":
            return ""
        if cherrypy.request.method not in ("GET", "HEAD"):
            raise cherrypy.HTTPError(405)

        def _do():
            client = self._client_or_raise()
            return {"plugins": client.list_plugins()}

        return self._handle_ipc(_do)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def default(self, *args, **kwargs):
        """GET/DELETE /api/plugins/{id} — status or uninstall."""
        if cherrypy.request.method == "OPTIONS":
            return ""
        if not args:
            raise cherrypy.HTTPError(404)
        plugin_id = str(args[0]).strip()
        if not plugin_id or plugin_id.startswith("."):
            raise cherrypy.HTTPError(400, "invalid plugin id")

        if cherrypy.request.method in ("GET", "HEAD"):

            def _status():
                return self._client_or_raise().status(plugin_id)

            return self._handle_ipc(_status)

        if cherrypy.request.method == "DELETE":
            delete_data = False
            raw = kwargs.get("delete_data", "false")
            if isinstance(raw, str):
                delete_data = raw.strip().lower() in {"1", "true", "yes", "on"}
            else:
                delete_data = bool(raw)
            # JSON body optional
            try:
                body = self._json_body()
                if "delete_data" in body:
                    delete_data = bool(body.get("delete_data"))
            except Exception:
                pass

            def _uninstall():
                return self._client_or_raise().uninstall(plugin_id, delete_data=delete_data)

            return self._handle_ipc(_uninstall)

        raise cherrypy.HTTPError(405)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in(force=False)
    def install(self, wheel=None, **kwargs):
        """POST /api/plugins/install — multipart wheel upload or JSON wheel_path."""
        if cherrypy.request.method == "OPTIONS":
            return ""
        try:
            self._require_post()
        except cherrypy.HTTPError:
            raise

        def _do():
            client = self._client_or_raise()
            storage_dir = resolve_storage_dir(self.config, config_path=self.config_path)
            plugins_root = resolve_plugins_root(self.config, storage_dir=storage_dir)
            upload_dir = plugins_root / ".upload"
            upload_dir.mkdir(parents=True, exist_ok=True)

            wheel_path: Optional[Path] = None
            cleanup = False

            # Multipart file field "wheel"
            if wheel is not None:
                raw_name = getattr(wheel, "filename", None) or "plugin.whl"
                # Keep only the basename; pip requires a PEP 427 wheel filename.
                basename = Path(str(raw_name)).name
                if not basename.endswith(".whl"):
                    raise cherrypy.HTTPError(400, "uploaded file must be a .whl")
                # Sanitize basename only; pip needs a PEP 427 wheel name.
                # Runtime rewrites from METADATA if the name is still invalid.
                safe_name = re.sub(r"[^A-Za-z0-9._+-]", "_", basename)
                if not safe_name.endswith(".whl"):
                    safe_name = f"{safe_name}.whl"
                staging = Path(tempfile.mkdtemp(prefix="plugin-upload-", dir=str(upload_dir)))
                wheel_path = staging / safe_name
                cleanup = True
                try:
                    with open(wheel_path, "wb") as out:
                        while True:
                            chunk = wheel.file.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                except Exception:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise
            else:
                body = self._json_body()
                raw_path = body.get("wheel_path") or kwargs.get("wheel_path")
                if not raw_path:
                    raise cherrypy.HTTPError(
                        400, "multipart field 'wheel' or JSON 'wheel_path' is required"
                    )
                wheel_path = Path(str(raw_path)).expanduser()
                if not wheel_path.is_file():
                    raise cherrypy.HTTPError(400, f"wheel_path not found: {wheel_path}")
                if not wheel_path.name.endswith(".whl"):
                    raise cherrypy.HTTPError(400, "wheel_path must point to a .whl file")

            try:
                return {"plugin": client.install(str(wheel_path.resolve()))}
            finally:
                if cleanup and wheel_path is not None:
                    # Remove staging dir (preferred) or lone temp file
                    parent = wheel_path.parent
                    try:
                        if parent.name.startswith("plugin-upload-") and parent.is_dir():
                            shutil.rmtree(parent, ignore_errors=True)
                        else:
                            wheel_path.unlink(missing_ok=True)
                    except OSError:
                        pass

        return self._handle_ipc(_do)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in(force=False)
    def enable(self, **kwargs):
        if cherrypy.request.method == "OPTIONS":
            return ""
        self._require_post()
        body = self._json_body()
        plugin_id = self._plugin_id_from(kwargs, body)
        return self._handle_ipc(lambda: {"plugin": self._client_or_raise().enable(plugin_id)})

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in(force=False)
    def disable(self, **kwargs):
        if cherrypy.request.method == "OPTIONS":
            return ""
        self._require_post()
        body = self._json_body()
        plugin_id = self._plugin_id_from(kwargs, body)
        return self._handle_ipc(lambda: {"plugin": self._client_or_raise().disable(plugin_id)})

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in(force=False)
    def start(self, **kwargs):
        if cherrypy.request.method == "OPTIONS":
            return ""
        self._require_post()
        body = self._json_body()
        plugin_id = self._plugin_id_from(kwargs, body)
        return self._handle_ipc(lambda: {"plugin": self._client_or_raise().start(plugin_id)})

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in(force=False)
    def stop(self, **kwargs):
        if cherrypy.request.method == "OPTIONS":
            return ""
        self._require_post()
        body = self._json_body()
        plugin_id = self._plugin_id_from(kwargs, body)
        return self._handle_ipc(lambda: {"plugin": self._client_or_raise().stop(plugin_id)})

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in(force=False)
    def restart(self, **kwargs):
        if cherrypy.request.method == "OPTIONS":
            return ""
        self._require_post()
        body = self._json_body()
        plugin_id = self._plugin_id_from(kwargs, body)
        return self._handle_ipc(lambda: {"plugin": self._client_or_raise().restart(plugin_id)})

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def logs(self, **kwargs):
        if cherrypy.request.method == "OPTIONS":
            return ""
        if cherrypy.request.method not in ("GET", "HEAD"):
            raise cherrypy.HTTPError(405)
        plugin_id = self._plugin_id_from(kwargs)
        try:
            tail = int(kwargs.get("tail", 200))
        except (TypeError, ValueError):
            raise cherrypy.HTTPError(400, "tail must be an integer")
        return self._handle_ipc(lambda: self._client_or_raise().logs(plugin_id, tail=tail))

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in(force=False)
    def uninstall(self, **kwargs):
        """POST /api/plugins/uninstall {id, delete_data?} — alternate to DELETE."""
        if cherrypy.request.method == "OPTIONS":
            return ""
        self._require_post()
        body = self._json_body()
        plugin_id = self._plugin_id_from(kwargs, body)
        delete_data = bool(body.get("delete_data", False))
        return self._handle_ipc(
            lambda: self._client_or_raise().uninstall(plugin_id, delete_data=delete_data)
        )

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in(force=False)
    def settings(self, **kwargs):
        """GET/POST /api/plugins/settings — read or write plugin data/config.json.

        Named ``settings`` (not ``config``) because CherryPy reserves ``config``
        on controllers and would otherwise dispatch to default("config").

        GET  ?id=openhop.nomad
        POST {"id": "...", "config": {...}, "restart": false}
        """
        if cherrypy.request.method == "OPTIONS":
            return ""

        if cherrypy.request.method in ("GET", "HEAD"):
            plugin_id = self._plugin_id_from(kwargs)
            return self._handle_ipc(lambda: self._client_or_raise().get_config(plugin_id))

        if cherrypy.request.method == "POST":
            body = self._json_body()
            plugin_id = self._plugin_id_from(kwargs, body)
            config = body.get("config")
            if not isinstance(config, dict):
                return self._err("config must be a JSON object", 400)
            restart = bool(body.get("restart", False))

            def _set():
                return self._client_or_raise().set_config(plugin_id, config, restart=restart)

            return self._handle_ipc(_set)

        raise cherrypy.HTTPError(405, "Method Not Allowed")

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def catalogue(self, **kwargs):
        """GET /api/plugins/catalogue — curated catalogue + install/update hints."""
        if cherrypy.request.method == "OPTIONS":
            return ""
        if cherrypy.request.method not in ("GET", "HEAD"):
            raise cherrypy.HTTPError(405)
        raw = kwargs.get("refresh") or kwargs.get("force_refresh") or "0"
        if isinstance(raw, str):
            force = raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            force = bool(raw)

        def _do():
            return self._client_or_raise().catalogue(force_refresh=force)

        return self._handle_ipc(_do)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in(force=False)
    def catalogue_install(self, **kwargs):
        """POST /api/plugins/catalogue_install {id, version?}."""
        if cherrypy.request.method == "OPTIONS":
            return ""
        self._require_post()
        body = self._json_body()
        plugin_id = self._plugin_id_from(kwargs, body)
        version = body.get("version") or kwargs.get("version")
        if version is not None:
            version = str(version).strip() or None
        force = bool(body.get("force_refresh") or body.get("refresh") or False)

        def _do():
            return {
                "plugin": self._client_or_raise().catalogue_install(
                    plugin_id, version=version, force_refresh=force
                )
            }

        return self._handle_ipc(_do)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def updates(self, **kwargs):
        """GET /api/plugins/updates?id= — check GitHub Releases for updates."""
        if cherrypy.request.method == "OPTIONS":
            return ""
        if cherrypy.request.method not in ("GET", "HEAD"):
            raise cherrypy.HTTPError(405)
        plugin_id = self._plugin_id_from(kwargs)
        raw = kwargs.get("refresh") or kwargs.get("force_refresh") or "0"
        if isinstance(raw, str):
            force = raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            force = bool(raw)

        def _do():
            return self._client_or_raise().check_update(plugin_id, force_refresh=force)

        return self._handle_ipc(_do)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in(force=False)
    def update(self, **kwargs):
        """POST /api/plugins/update {id, version?} — update from GitHub Releases."""
        if cherrypy.request.method == "OPTIONS":
            return ""
        self._require_post()
        body = self._json_body()
        plugin_id = self._plugin_id_from(kwargs, body)
        version = body.get("version") or kwargs.get("version")
        if version is not None:
            version = str(version).strip() or None
        force = bool(body.get("force_refresh") or body.get("refresh") or False)

        def _do():
            return {
                "plugin": self._client_or_raise().update(
                    plugin_id, version=version, force_refresh=force
                )
            }

        return self._handle_ipc(_do)
