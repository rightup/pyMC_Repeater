#!/bin/sh
set -eu

IMAGE="${1:-openhop-repeater:plugin-smoke}"
SUFFIX="${GITHUB_RUN_ID:-$$}-${GITHUB_RUN_ATTEMPT:-0}"
CONTAINER="openhop-plugin-smoke-${SUFFIX}"
CONFIG_VOLUME="openhop-plugin-smoke-config-${SUFFIX}"
DATA_VOLUME="openhop-plugin-smoke-data-${SUFFIX}"

cleanup() {
    docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
    docker volume rm "${CONFIG_VOLUME}" "${DATA_VOLUME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

start_container() {
    docker run -d \
        --name "${CONTAINER}" \
        -v "${CONFIG_VOLUME}:/etc/openhop_repeater" \
        -v "${DATA_VOLUME}:/var/lib/openhop_repeater" \
        "${IMAGE}" >/dev/null
}

wait_for_manager() {
    attempts=0
    while [ "${attempts}" -lt 60 ]; do
        if docker exec "${CONTAINER}" python3 -c \
            'from repeater.plugins.ipc import PluginIPCClient; PluginIPCClient("/var/lib/openhop_repeater/plugin-manager.sock").call("ping")' \
            >/dev/null 2>&1; then
            return 0
        fi
        attempts=$((attempts + 1))
        sleep 1
    done
    docker logs "${CONTAINER}" >&2 || true
    echo "Plugin manager did not become ready" >&2
    return 1
}

wait_for_http() {
    attempts=0
    while [ "${attempts}" -lt 60 ]; do
        if docker exec "${CONTAINER}" python3 -c \
            'import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:8000/", timeout=2).status == 200' \
            >/dev/null 2>&1; then
            return 0
        fi
        attempts=$((attempts + 1))
        sleep 1
    done
    docker logs "${CONTAINER}" >&2 || true
    echo "Repeater HTTP server did not become ready" >&2
    return 1
}

docker volume create "${CONFIG_VOLUME}" >/dev/null
docker volume create "${DATA_VOLUME}" >/dev/null
start_container
wait_for_manager
wait_for_http

echo "Checking local wheel upload"
docker exec -i "${CONTAINER}" python3 - <<'PY'
import json
import urllib.request
import uuid
import zipfile
from pathlib import Path

wheel_path = Path("/tmp/openhop_smoke_plugin-0.1.0-py3-none-any.whl")
manifest = {
    "schema": 1,
    "id": "openhop.smoke",
    "name": "Container Smoke Plugin",
    "version": "0.1.0",
    "runtime": {"type": "python", "entrypoint": "openhop-smoke-plugin"},
}
module = '''import os
import signal
import time
from pathlib import Path

running = True
lifecycle = Path(os.environ["OPENHOP_PLUGIN_DATA"]) / "lifecycle.log"

def stop(signum, frame):
    global running
    with lifecycle.open("a", encoding="utf-8") as handle:
        handle.write(f"TERM {os.getpid()}\\n")
    running = False

def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with lifecycle.open("a", encoding="utf-8") as handle:
        handle.write(f"START {os.getpid()}\\n")
    while running:
        time.sleep(0.1)
'''
metadata = "Metadata-Version: 2.1\nName: openhop-smoke-plugin\nVersion: 0.1.0\n"
wheel = "Wheel-Version: 1.0\nGenerator: openhop-container-smoke\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
entries = "[console_scripts]\nopenhop-smoke-plugin = openhop_smoke_plugin:main\n"
record = "\n".join(
    [
        "openhop_smoke_plugin.py,,",
        "openhop_smoke_plugin-0.1.0.dist-info/METADATA,,",
        "openhop_smoke_plugin-0.1.0.dist-info/WHEEL,,",
        "openhop_smoke_plugin-0.1.0.dist-info/entry_points.txt,,",
        "openhop_smoke_plugin-0.1.0.dist-info/RECORD,,",
        "share/openhop/plugins/openhop.smoke/openhop-plugin.json,,",
    ]
) + "\n"
with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    archive.writestr("openhop_smoke_plugin.py", module)
    archive.writestr("openhop_smoke_plugin-0.1.0.dist-info/METADATA", metadata)
    archive.writestr("openhop_smoke_plugin-0.1.0.dist-info/WHEEL", wheel)
    archive.writestr("openhop_smoke_plugin-0.1.0.dist-info/entry_points.txt", entries)
    archive.writestr("openhop_smoke_plugin-0.1.0.dist-info/RECORD", record)
    archive.writestr(
        "share/openhop/plugins/openhop.smoke/openhop-plugin.json",
        json.dumps(manifest),
    )

def request(path, *, body=None, headers=None, method=None):
    req = urllib.request.Request(
        "http://127.0.0.1:8000" + path,
        data=body,
        headers=headers or {},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        assert response.status == 200
        return json.loads(response.read())

login = request(
    "/auth/login",
    body=json.dumps(
        {"username": "admin", "password": "admin123", "client_id": "docker-smoke"}
    ).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
assert login["success"] is True
headers = {"Authorization": "Bearer " + login["token"]}
boundary = "----openhop-smoke-" + uuid.uuid4().hex
upload = (
    f'--{boundary}\r\nContent-Disposition: form-data; name="wheel"; filename="{wheel_path.name}"\r\n'
    "Content-Type: application/octet-stream\r\n\r\n"
).encode() + wheel_path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
installed = request(
    "/api/plugins/install",
    body=upload,
    headers={**headers, "Content-Type": "multipart/form-data; boundary=" + boundary},
    method="POST",
)
assert installed["success"] is True
assert installed["plugin"]["id"] == "openhop.smoke"
release_dir = Path(
    "/var/lib/openhop_repeater/plugins/openhop.smoke/releases/0.1.0"
)
assert any(release_dir.glob("*.whl")), "installed wheel was not archived"
enabled = request(
    "/api/plugins/enable",
    body=json.dumps({"id": "openhop.smoke"}).encode(),
    headers={**headers, "Content-Type": "application/json"},
    method="POST",
)
assert enabled["success"] is True
assert enabled["plugin"]["state"] == "RUNNING"
PY

echo "Checking plugin-manager crash recovery"
docker exec "${CONTAINER}" python3 -c '
import os
import signal
from pathlib import Path
needle = b"\x00-m\x00repeater.plugins\x00"
for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
    try:
        data = cmdline.read_bytes()
    except OSError:
        continue
    if needle in data:
        os.kill(int(cmdline.parent.name), signal.SIGKILL)
        break
else:
    raise SystemExit("plugin manager process not found")
'
wait_for_manager
attempts=0
while [ "${attempts}" -lt 30 ]; do
    starts="$(docker exec "${CONTAINER}" python3 -c '
from pathlib import Path
path = Path("/var/lib/openhop_repeater/plugins/openhop.smoke/data/lifecycle.log")
print(sum(line.startswith("START ") for line in path.read_text().splitlines()))
' 2>/dev/null || printf '0')"
    if [ "${starts}" -ge 2 ]; then
        break
    fi
    attempts=$((attempts + 1))
    sleep 1
done
[ "${starts}" -ge 2 ]
docker exec "${CONTAINER}" python3 -c '
from pathlib import Path
zombies = []
for stat_path in Path("/proc").glob("[0-9]*/stat"):
    try:
        fields = stat_path.read_text().split()
    except OSError:
        continue
    if len(fields) > 2 and fields[2] == "Z":
        zombies.append(stat_path.parent.name)
assert not zombies, f"zombie processes remain after manager restart: {zombies}"
'

echo "Checking container recreation persistence"
docker stop -t 10 "${CONTAINER}" >/dev/null
docker rm "${CONTAINER}" >/dev/null
start_container
wait_for_manager
wait_for_http
docker exec "${CONTAINER}" python3 -c '
from repeater.plugins.ipc import PluginIPCClient
client = PluginIPCClient("/var/lib/openhop_repeater/plugin-manager.sock")
status = client.status("openhop.smoke")
assert status["enabled"] is True
assert status["state"] == "RUNNING"
'

echo "Checking graceful plugin shutdown"
EXPECTED_PLUGIN_PID="$(docker exec "${CONTAINER}" python3 -c '
from repeater.plugins.ipc import PluginIPCClient
client = PluginIPCClient("/var/lib/openhop_repeater/plugin-manager.sock")
print(client.status("openhop.smoke")["pid"])
')"
docker stop -t 10 "${CONTAINER}" >/dev/null
docker run --rm --entrypoint python3 -e EXPECTED_PLUGIN_PID="${EXPECTED_PLUGIN_PID}" \
    -v "${DATA_VOLUME}:/var/lib/openhop_repeater" \
    "${IMAGE}" -c '
import os
from pathlib import Path
lines = Path("/var/lib/openhop_repeater/plugins/openhop.smoke/data/lifecycle.log").read_text().splitlines()
assert sum(line.startswith("START ") for line in lines) >= 3, lines
expected = "TERM " + os.environ["EXPECTED_PLUGIN_PID"]
assert expected in lines, (expected, lines)
print("container plugin smoke test passed")
'
