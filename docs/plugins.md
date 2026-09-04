# openHop Plugins

The plugin system installs and supervises **external** applications around the
Repeater. Plugins are not imported into the Repeater process. They talk to
existing interfaces (REST, WebSocket/SSE, Companion frame server, static web).

## Layout

Default root (under Repeater storage):

```text
/var/lib/openhop_repeater/plugins/
└── openhop.nomad/
    ├── releases/0.1.0/
    │   ├── openhop-plugin.json
    │   ├── ui/                 # optional application UI assets
    │   └── venv/               # isolated Python environment
    ├── current -> releases/0.1.0
    ├── data/                   # persistent plugin-owned data
    ├── logs/plugin.log
    └── state.json              # manager metadata only (id, version, enabled)
```

- Code and virtualenv are **version-specific**.
- The installed wheel is retained with each release so its virtualenv can be
  rebuilt automatically if a future container image changes Python versions.
- `data/` is **not** version-specific and is never interpreted by the manager.
- IPC socket default: `/var/lib/openhop_repeater/plugin-manager.sock`.

Override with config:

```yaml
plugins:
  enabled: true
  # root: "/var/lib/openhop_repeater/plugins"
  # socket: "/var/lib/openhop_repeater/plugin-manager.sock"
```

## Manifest (`openhop-plugin.json`)

Schema version **1**:

```json
{
  "schema": 1,
  "id": "openhop.nomad",
  "name": "NOMAD Bridge",
  "version": "0.1.0",
  "runtime": {
    "type": "python",
    "entrypoint": "meshcore-nomad-bridge"
  }
}
```

UI-only application plugin:

```json
{
  "schema": 1,
  "id": "openhop.console",
  "name": "openHop Console",
  "version": "1.0.0",
  "ui": {
    "type": "application",
    "entry": "ui/index.html"
  }
}
```

A plugin may declare both `runtime` and `ui`.

## Plugin manager service

Native installs:

```bash
sudo cp openhop-plugin-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openhop-plugin-manager
```

The manager starts enabled service plugins on boot and supervises crashes
(restarts with a simple crash-loop limit: 5 unexpected exits in 60s → `FAILED`).

Repeater does **not** require the manager. If the socket is missing, plugin API
calls return HTTP 503 and normal Repeater operation continues.

The Docker image runs Repeater and the manager under a small process supervisor,
with `tini` as PID 1 to reap orphaned children. It forwards shutdown signals to
both services and their plugin processes, and restarts the manager if it exits
unexpectedly while Repeater remains running. Set `OPENHOP_PLUGIN_MANAGER=0` to
run Repeater without the manager.

## Install a local wheel

Via API (authenticated):

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -F "wheel=@openhop_nomad_plugin-0.1.0-py3-none-any.whl" \
  http://127.0.0.1:8000/api/plugins/install
```

Or JSON with a host path:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"wheel_path":"/tmp/plugin.whl"}' \
  http://127.0.0.1:8000/api/plugins/install
```

Installation creates an isolated venv under the plugin release directory and
**never** mutates the Repeater virtualenv.

## Enable / disable / lifecycle

```bash
# Enable (also starts service plugins)
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id":"openhop.nomad"}' \
  http://127.0.0.1:8000/api/plugins/enable

curl -X POST .../api/plugins/disable   -d '{"id":"openhop.nomad"}'
curl -X POST .../api/plugins/start     -d '{"id":"openhop.nomad"}'
curl -X POST .../api/plugins/stop      -d '{"id":"openhop.nomad"}'
curl -X POST .../api/plugins/restart   -d '{"id":"openhop.nomad"}'
```

List / status / logs:

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/plugins/
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/plugins/openhop.nomad
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/plugins/logs?id=openhop.nomad&tail=100"
```

## Plugin configuration

Each plugin owns `$OPENHOP_PLUGIN_DATA/config.json`. The manager treats it as an
opaque JSON object — it does not validate plugin-specific keys.

Plugins can declare editor defaults in the manifest:

```json
{
  "config": {
    "defaults": {
      "meshcore_host": "127.0.0.1",
      "nomad_url": "http://127.0.0.1:8080"
    }
  }
}
```

Optionally ship `config.default.json` next to `openhop-plugin.json` in the wheel
(`share/openhop/plugins/<id>/`). On install, if `data/config.json` is missing,
the manager seeds it from those defaults. The settings UI shows defaults when no
saved config exists, and offers **Reset to defaults**.

API:

```bash
# Read
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/plugins/settings?id=openhop.nomad"

# Write (optionally restart service plugins)
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "openhop.nomad",
    "restart": true,
    "config": {
      "nomad_url": "http://192.168.0.170:8080",
      "nomad_model": "qwen2.5:3b-instruct",
      "meshcore_host": "127.0.0.1",
      "meshcore_port": 5001
    }
  }' \
  http://127.0.0.1:8000/api/plugins/settings
```

The Plugins page in the web UI provides a Settings dialog for the same file.

## Service plugin environment

Started processes receive:

| Variable | Example |
|----------|---------|
| `OPENHOP_PLUGIN_ID` | `openhop.nomad` |
| `OPENHOP_PLUGIN_DATA` | `/var/lib/openhop_repeater/plugins/openhop.nomad/data` |

Plugin-specific configuration (for example NOMAD `config.json`) lives under
`$OPENHOP_PLUGIN_DATA` and is owned by the plugin.

## Application UI plugins

Enabled UI plugins are served by Repeater at:

```text
/plugins/{id}/
/plugins/{id}/assets/...
```

Path traversal is rejected. Missing paths fall back to the manifest `ui.entry`
document (SPA-friendly). Disabled plugins are not exposed.

## Uninstall

Default **keeps** `data/`:

```bash
curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/plugins/openhop.nomad
```

Delete data as well:

```bash
curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/plugins/openhop.nomad?delete_data=true"
```

## NOMAD acceptance

1. Build the `openhop-nomad-plugin` wheel.
2. Install via `/api/plugins/install`.
3. Enable a Repeater Companion frame server reachable from the plugin. In the
   standard Docker image, `127.0.0.1:5001` reaches the same container.
4. Write `$OPENHOP_PLUGIN_DATA/config.json` with `nomad_url` / `nomad_model`.
5. Enable the plugin — `meshcore-nomad-bridge` should report `RUNNING`.
6. Confirm its logs show a successful Companion connection, not only a running
   retry loop. A `RUNNING` process can still be reconnecting to a disabled frame
   server.

## Future work (not in this version)

- GitHub release installation and catalogue
- Automatic updates / rollback between versions
- Web Component dashboard widgets and settings panels
- Plugin management UI page
- Permission scopes, signing, non-Python runtimes
- Plugin SDK

## Plugin catalogue

Repeater can browse a curated static catalogue and install plugins from GitHub Releases.

Default catalogue URL:

```text
https://repeater-plugins.openhop.dev/catalogue.json
```

Override with:

```yaml
plugins:
  catalogue_url: "https://repeater-plugins.openhop.dev/catalogue.json"
```

The catalogue lists plugin **identity and repository only**. Versions and wheel assets come from GitHub Releases on each plugin repo (draft and prerelease tags are ignored by default).

GitHub permits anonymous lookups, but the anonymous API limit is low and may be
shared by multiple containers behind the same public address. Docker operators
can set `OPENHOP_PLUGIN_GITHUB_TOKEN` in `.env` to a fine-grained token with
read-only access to public repository metadata. The token is used only for
`api.github.com` release lookups and is not sent to release asset URLs.

```bash
# List catalogue (annotated with installed / latest when available)
curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/plugins/catalogue

# Install latest stable release of a catalogue plugin (enabled by default)
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id":"openhop.nomad"}' \
  http://127.0.0.1:8000/api/plugins/catalogue_install

# Check for updates
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/plugins/updates?id=openhop.nomad"

# Apply update
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id":"openhop.nomad"}' \
  http://127.0.0.1:8000/api/plugins/update
```

Catalogue or GitHub outages do not affect already-installed plugins or Repeater startup. Local `.whl` install continues to work unchanged.

