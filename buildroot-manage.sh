#!/bin/bash
# Buildroot/Luckfox management entrypoint for pyMC Repeater

set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALL_DIR="/opt/pymc_repeater"
VENV_DIR="$INSTALL_DIR/venv"
VENV_PIP="$VENV_DIR/bin/pip"
VENV_PYTHON="$VENV_DIR/bin/python"
CONFIG_DIR="/etc/pymc_repeater"
LOG_DIR="/var/log/pymc_repeater"
DATA_DIR="/var/lib/pymc_repeater"
SERVICE_USER="root"
INIT_SCRIPT="/etc/init.d/S80pymc-repeater"
PIDFILE="/var/run/pymc-repeater.pid"
LOGFILE="$LOG_DIR/repeater.log"
SERVICE_NAME="pymc-repeater"
SILENT_MODE="${PYMC_SILENT:-${SILENT:-}}"

R2_BASE_URL="https://wheel.pymc.dev/pymc_build_deps"
PIWHEELS_INDEX_URL="https://www.piwheels.org/simple"
R2_ENABLED=1
YQ_VERSION="${YQ_VERSION:-v4.44.3}"
PYMC_CORE_REPO="${PYMC_CORE_REPO:-https://github.com/rightup/pyMC_core.git}"
PYMC_CORE_REF="${PYMC_CORE_REF:-}"
PYMC_CORE_LOCAL_DIR="${PYMC_CORE_LOCAL_DIR:-}"
PYMC_SKIP_BUILDROOT_DEP_INSTALL="${PYMC_SKIP_BUILDROOT_DEP_INSTALL:-0}"
RADIO_SETTINGS_JSON="$SCRIPT_DIR/radio-settings.json"
RADIO_PRESETS_JSON="$SCRIPT_DIR/radio-presets.json"
BUILDROOT_RADIO_SETTINGS_JSON="$SCRIPT_DIR/radio-settings-buildroot.json"
set_wheel_dependencies() {
    set -- \
        "cherrypy>=18.0.0" \
        "cherrypy-cors==1.7.0" \
        "paho-mqtt>=1.6.0" \
        "pyjwt>=2.8.0" \
        "ws4py>=0.6.0" \
        "autocommand" \
        "backports.tarfile" \
        "jaraco.collections" \
        "jaraco.text" \
        "jaraco.context" \
        "tempora" \
        "zc.lockfile" \
        "httpagentparser>=1.5"
    printf '%s\n' "$@"
}

stage() {
    printf '\n==> %s\n' "$1"
}

info() {
    printf '  - %s\n' "$1"
}

warn() {
    printf '  - %s\n' "$1" >&2
}

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

run_with_spinner() {
    local message="$1"
    shift

    if [ ! -t 1 ] || [ "${SILENT_MODE:-}" = "1" ] || [ "${SILENT_MODE:-}" = "true" ]; then
        "$@"
        return $?
    fi

    local log_file pid spinner_index status
    local spinner='|/-\'

    log_file=$(mktemp)
    "$@" >"$log_file" 2>&1 &
    pid=$!
    spinner_index=0

    while kill -0 "$pid" 2>/dev/null; do
        printf '\r  - %s... %s' "$message" "${spinner:$spinner_index:1}"
        spinner_index=$(( (spinner_index + 1) % 4 ))
        sleep 0.15
    done

    wait "$pid"
    status=$?
    printf '\r\033[K'

    if [ "$status" -ne 0 ]; then
        cat "$log_file" >&2
    fi

    rm -f "$log_file"
    return "$status"
}

detect_local_git_ref() {
    local branch tag

    if ! git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        return 1
    fi

    branch=$(git -C "$SCRIPT_DIR" symbolic-ref --quiet --short HEAD 2>/dev/null || true)
    if [ -n "$branch" ]; then
        printf '%s\n' "$branch"
        return 0
    fi

    tag=$(git -C "$SCRIPT_DIR" describe --tags --exact-match 2>/dev/null || true)
    if [ -n "$tag" ]; then
        printf '%s\n' "$tag"
        return 0
    fi

    return 1
}

remote_ref_exists() {
    local repo="$1"
    local ref="$2"

    [ -n "$ref" ] || return 1
    git ls-remote --exit-code --heads --tags "$repo" "$ref" >/dev/null 2>&1
}

resolve_core_ref() {
    local candidate fallback

    if [ -n "$PYMC_CORE_REF" ]; then
        printf '%s\n' "$PYMC_CORE_REF"
        return 0
    fi

    candidate=$(detect_local_git_ref 2>/dev/null || true)
    if [ -n "$candidate" ] && remote_ref_exists "$PYMC_CORE_REPO" "$candidate"; then
        printf '%s\n' "$candidate"
        return 0
    fi

    fallback="dev"
    printf '%s\n' "$fallback"
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

normalize_interactive_tty() {
    [ -t 0 ] || return 0
    [ -r /dev/tty ] || return 0

    # Buildroot shells sometimes end up with a bad erase character, which
    # makes backspace print ^H during interactive prompts.
    stty sane < /dev/tty >/dev/null 2>&1 || true
    stty erase '^H' < /dev/tty >/dev/null 2>&1 || true
}

is_buildroot() {
    [ -f /etc/pymc-image-build-id ] && return 0
    [ -f /etc/os-release ] && grep -q '^ID=buildroot$' /etc/os-release 2>/dev/null && return 0
    return 1
}

prompt_value() {
    local prompt="$1"
    local default_value="${2:-}"
    local reply=""

    if [ ! -t 0 ]; then
        printf '%s\n' "$default_value"
        return 0
    fi

    normalize_interactive_tty

    if [ -n "$default_value" ]; then
        printf '%s [%s]: ' "$prompt" "$default_value" >&2
    else
        printf '%s: ' "$prompt" >&2
    fi
    IFS= read -r reply || true
    reply=${reply:-$default_value}
    printf '%s\n' "$reply"
}

prompt_secret() {
    local prompt="$1"

    normalize_interactive_tty

    python3 - "$prompt" <<'PY'
import os
import sys
import termios
import tty

prompt = sys.argv[1]
try:
    tty_fd = os.open("/dev/tty", os.O_RDWR)
except OSError:
    print("Interactive admin password prompt requires a TTY. Set PYMC_ADMIN_PASSWORD instead.", file=sys.stderr)
    raise SystemExit(1)

def read_secret(label: str) -> str:
    os.write(tty_fd, f"{label}: ".encode())
    original = termios.tcgetattr(tty_fd)
    chars = []
    try:
        tty.setraw(tty_fd)
        while True:
            ch = os.read(tty_fd, 1)
            if ch in (b"\r", b"\n"):
                os.write(tty_fd, b"\r\n")
                return "".join(chars)
            if ch == b"\x03":
                raise KeyboardInterrupt
            if ch in (b"\x7f", b"\x08"):
                if chars:
                    chars.pop()
                    os.write(tty_fd, b"\b \b")
                continue
            if not ch or ch[0] < 32:
                continue
            chars.append(ch.decode(errors="ignore"))
            os.write(tty_fd, b"*")
    finally:
        termios.tcsetattr(tty_fd, termios.TCSADRAIN, original)

while True:
    first = read_secret(prompt)
    second = read_secret(f"Confirm {prompt.lower()}")

    if not first:
        print("  - Password cannot be empty.", file=sys.stderr)
        continue
    if len(first) < 6:
        print("  - Password must be at least 6 characters.", file=sys.stderr)
        continue
    if first == "admin123":
        print("  - Password cannot be the default admin123.", file=sys.stderr)
        continue
    if first != second:
        print("  - Passwords do not match.", file=sys.stderr)
        continue

    print(first)
    break
os.close(tty_fd)
PY
}

validate_node_name() {
    local node_name="$1"

    python3 - "$node_name" <<'PY'
import sys

node_name = sys.argv[1].strip()
if not node_name:
    print("Repeater name cannot be empty.", file=sys.stderr)
    raise SystemExit(1)
if node_name == "mesh-repeater-01":
    print("Repeater name cannot be the default mesh-repeater-01.", file=sys.stderr)
    raise SystemExit(1)
if len(node_name.encode("utf-8")) > 31:
    print("Repeater name is too long (max 31 UTF-8 bytes).", file=sys.stderr)
    raise SystemExit(1)
PY
}

validate_admin_password() {
    local admin_password="$1"

    python3 - "$admin_password" <<'PY'
import sys

password = sys.argv[1]
if not password:
    print("Admin password cannot be empty.", file=sys.stderr)
    raise SystemExit(1)
if len(password) < 6:
    print("Admin password must be at least 6 characters.", file=sys.stderr)
    raise SystemExit(1)
if password == "admin123":
    print("Admin password cannot be the default admin123.", file=sys.stderr)
    raise SystemExit(1)
PY
}

ensure_root() {
    [ "$(id -u 2>/dev/null || echo 1)" -eq 0 ] || fail "This command must be run as root."
}

group_exists() {
    grep -q "^$1:" /etc/group 2>/dev/null
}

ensure_group_line() {
    local group_name="$1"
    local gid="$2"
    group_exists "$group_name" && return 0
    printf '%s:x:%s:\n' "$group_name" "$gid" >> /etc/group
}

ensure_service_user() {
    if [ "$SERVICE_USER" = "root" ]; then
        return 0
    fi

    if id "$SERVICE_USER" >/dev/null 2>&1; then
        return 0
    fi

    if command -v useradd >/dev/null 2>&1; then
        useradd --system --home "$DATA_DIR" --shell /sbin/nologin "$SERVICE_USER"
        return 0
    fi

    ensure_group_line "$SERVICE_USER" 990
    printf '%s:x:990:990::%s:/sbin/nologin\n' "$SERVICE_USER" "$DATA_DIR" >> /etc/passwd
    if [ -f /etc/shadow ]; then
        printf '%s:!:19000:0:99999:7:::\n' "$SERVICE_USER" >> /etc/shadow
    fi
}

add_user_to_group() {
    local user_name="$1"
    local group_name="$2"
    local current_line current_members gid escaped_line new_members

    if [ "$user_name" = "root" ]; then
        return 0
    fi

    group_exists "$group_name" || return 0
    current_line=$(grep "^${group_name}:" /etc/group 2>/dev/null || true)
    [ -n "$current_line" ] || return 0
    current_members=$(printf '%s' "$current_line" | cut -d: -f4)
    case ",${current_members}," in
        *,"${user_name}",*) return 0 ;;
    esac

    if [ -n "$current_members" ]; then
        new_members="${current_members},${user_name}"
    else
        new_members="${user_name}"
    fi
    gid=$(printf '%s' "$current_line" | cut -d: -f3)
    escaped_line=$(printf '%s\n' "$current_line" | sed 's/[].[^$\\*]/\\&/g')
    sed -i "s/^${escaped_line}\$/${group_name}:x:${gid}:${new_members}/" /etc/group
}

install_system_packages() {
    if is_buildroot; then
        info "Buildroot image detected; using preinstalled packages."
        return 0
    fi

    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        libffi-dev libusb-1.0-0 sudo jq pip python3-venv python3-rrdtool wget swig build-essential python3-dev
}

get_yq_cmd() {
    local candidate

    for candidate in /usr/local/bin/yq /usr/bin/yq; do
        if [ -x "$candidate" ] && "$candidate" --version 2>&1 | grep -q "mikefarah/yq"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    return 1
}

ensure_yq() {
    local yq_cmd yq_binary

    yq_cmd=$(get_yq_cmd 2>/dev/null || true)
    if [ -n "$yq_cmd" ]; then
        printf '%s\n' "$yq_cmd"
        return 0
    fi

    need_cmd wget

    case "$(uname -m)" in
        aarch64|arm64) yq_binary="yq_linux_arm64" ;;
        x86_64|amd64) yq_binary="yq_linux_amd64" ;;
        armv7l|armv7) yq_binary="yq_linux_arm" ;;
        *)
            warn "Unable to install yq automatically on unsupported architecture: $(uname -m)"
            return 1
            ;;
    esac

    printf '\n==> Installing yq\n' >&2
    printf '  - Fetching mikefarah/yq %s\n' "$YQ_VERSION" >&2
    wget -qO /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/${yq_binary}" || {
        warn "Failed to download yq; config updates will keep working but comments will not be preserved."
        rm -f /usr/local/bin/yq
        return 1
    }
    chmod +x /usr/local/bin/yq

    yq_cmd=$(get_yq_cmd 2>/dev/null || true)
    if [ -n "$yq_cmd" ]; then
        printf '%s\n' "$yq_cmd"
        return 0
    fi

    warn "Installed yq is not the expected mikefarah/yq binary."
    return 1
}

ensure_venv() {
    local recreate=0

    if [ -f "$VENV_DIR/pyvenv.cfg" ] && grep -Eq '^include-system-site-packages *= *true$' "$VENV_DIR/pyvenv.cfg"; then
        stage "Rebuilding virtual environment"
        info "Existing venv uses system site-packages and is not supported on Buildroot."
        recreate=1
    fi

    if [ "$recreate" -eq 1 ]; then
        rm -rf "$VENV_DIR"
    fi

    if [ ! -x "$VENV_PYTHON" ]; then
        stage "Creating virtual environment"
        info "Creating $VENV_DIR"
        info "This can take a minute on Buildroot flash storage."
        run_with_spinner "Creating virtual environment" python3 -m venv "$VENV_DIR"
        info "Bootstrapping pip, setuptools, wheel, and setuptools_scm"
        run_with_spinner "Bootstrapping Python build tools" \
            "$VENV_PIP" install --upgrade --no-cache-dir pip setuptools wheel setuptools_scm
        info "Virtual environment is ready"
    else
        info "Using existing virtual environment at $VENV_DIR"
    fi
}

ensure_venv_build_backend() {
    if "$VENV_PYTHON" - <<'PY'
import setuptools
import setuptools.build_meta
import setuptools_scm
import wheel
PY
    then
        info "venv build backend is ready"
        return 0
    fi

    stage "Rebuilding virtual environment"
    warn "Existing venv is missing required Python build packages or has incompatible leftovers; recreating it cleanly. This can take a minute on Buildroot flash storage."
    rm -rf "$VENV_DIR"
    run_with_spinner "Recreating virtual environment" python3 -m venv "$VENV_DIR"
    run_with_spinner "Reinstalling Python build tools" \
        "$VENV_PIP" install --upgrade --no-cache-dir pip setuptools wheel setuptools_scm

    if "$VENV_PYTHON" - <<'PY'
import setuptools
import setuptools.build_meta
import setuptools_scm
import wheel
PY
    then
        info "venv build backend repaired"
        return 0
    fi

    fail "Unable to prepare an isolated venv with setuptools.build_meta on this Buildroot image."
}

cleanup_legacy_install_state() {
    local removed=0
    local path

    for path in \
        "$INSTALL_DIR/repeater" \
        "$INSTALL_DIR/pymc_core" \
        "$INSTALL_DIR/pyMC_Repeater" \
        "$INSTALL_DIR/pyMC_core"
    do
        if [ -e "$path" ]; then
            rm -rf "$path"
            removed=1
            info "Removed stale source tree at $path"
        fi
    done

    if [ "$removed" -eq 0 ]; then
        info "No stale source-tree paths found under $INSTALL_DIR"
    fi
}

get_r2_wheel_base() {
    local machine_arch arch_tag platform_tag py_tag wheel_base

    [ "$R2_ENABLED" -eq 1 ] || return 1

    machine_arch=$(uname -m)
    case "$machine_arch" in
        aarch64) arch_tag="arm64"; platform_tag="aarch64" ;;
        armv7l|armv7) arch_tag="armv7"; platform_tag="armv7l" ;;
        x86_64) arch_tag="x86_64"; platform_tag="x86_64" ;;
        *) return 1 ;;
    esac

    py_tag=$("$VENV_PYTHON" -c 'import sys; v=f"cp{sys.version_info.major}{sys.version_info.minor}"; print(f"{v}-{v}")' 2>/dev/null || echo "cp311-cp311")
    wheel_base="${R2_BASE_URL}/${arch_tag}/${platform_tag}/${py_tag}"
    printf '%s\n' "$wheel_base"
}

preinstall_r2_wheels() {
    local wheel_base

    wheel_base=$(get_r2_wheel_base 2>/dev/null || true)
    [ -n "$wheel_base" ] || return 0

    stage "Checking optional wheel cache"
    info "Native Python modules come from the Buildroot image."
    info "Skipping native wheel preload from ${wheel_base}/index.html"
}

install_buildroot_dependencies() {
    local wheel_base
    local deps

    if [ "${PYMC_SKIP_BUILDROOT_DEP_INSTALL}" = "1" ]; then
        stage "Checking embedded Python dependency baseline"
        if check_buildroot_dependencies_installed >/dev/null 2>&1; then
            info "Image-provided Python dependency wheels already satisfy Buildroot requirements"
            return 0
        fi
        fail "Embedded/offline install requested, but the required Python dependency wheels are not already present in the image."
    fi

    wheel_base=$(get_r2_wheel_base 2>/dev/null || true)
    deps=$(set_wheel_dependencies)
    stage "Installing Python dependency wheels"
    if [ -n "$wheel_base" ]; then
        info "Using Rightup wheels: ${wheel_base}/index.html"
    fi
    info "Using piwheels fallback: ${PIWHEELS_INDEX_URL}"

    if [ -n "$wheel_base" ]; then
        # shellcheck disable=SC2086
        "$VENV_PIP" install --upgrade --no-cache-dir --only-binary=:all: \
            --find-links "${wheel_base}/index.html" \
            --extra-index-url "${PIWHEELS_INDEX_URL}" \
            $deps
    else
        # shellcheck disable=SC2086
        "$VENV_PIP" install --upgrade --no-cache-dir --only-binary=:all: \
            --extra-index-url "${PIWHEELS_INDEX_URL}" \
            $deps
    fi
}

check_buildroot_dependencies_installed() {
    local deps

    deps=$(set_wheel_dependencies)
    "$VENV_PYTHON" - $deps <<'PY'
import sys
from importlib import metadata
from packaging.requirements import Requirement
from packaging.version import Version

failed = []
for raw in sys.argv[1:]:
    req = Requirement(raw)
    try:
        installed = metadata.version(req.name)
    except metadata.PackageNotFoundError:
        failed.append(f"{req.name} (missing)")
        continue
    if req.specifier and Version(installed) not in req.specifier:
        failed.append(f"{req.name} {installed} (! {req.specifier})")

if failed:
    for item in failed:
        print(item)
    raise SystemExit(1)
PY
}

link_system_site_packages() {
    local venv_site_dir pth_file system_paths

    venv_site_dir=$("$VENV_PYTHON" - <<'PY'
import site
for path in site.getsitepackages():
    if path.endswith("site-packages"):
        print(path)
        break
PY
)
    [ -n "$venv_site_dir" ] || fail "Could not determine venv site-packages directory."

    system_paths=$(python3 - <<'PY'
import site
for path in site.getsitepackages():
    if path.startswith("/usr/lib/python") and path.endswith("site-packages"):
        print(path)
PY
)

    [ -n "$system_paths" ] || return 0

    pth_file="$venv_site_dir/buildroot-system-site-packages.pth"
    printf '%s\n' "$system_paths" > "$pth_file"
    info "Linked image-provided Python runtime into the venv"
}

remove_shadowing_buildroot_native_packages() {
    local removed=0

    if "$VENV_PIP" show python-periphery >/dev/null 2>&1; then
        stage "Restoring Buildroot GPIO runtime"
        info "Removing venv-installed python-periphery so the image-provided package is used"
        "$VENV_PIP" uninstall -y python-periphery >/dev/null 2>&1 || true
        removed=1
    fi

    if [ "$removed" -eq 0 ]; then
        info "No shadowing Buildroot native GPIO wheels found in the venv"
    fi
}

install_core_into_venv() {
    local core_repo core_ref core_spec

    if [ -n "$PYMC_CORE_LOCAL_DIR" ]; then
        [ -d "$PYMC_CORE_LOCAL_DIR" ] || fail "Missing local pyMC_core checkout: $PYMC_CORE_LOCAL_DIR"
        stage "Installing pyMC_core"
        info "Local dir: ${PYMC_CORE_LOCAL_DIR}"
        "$VENV_PIP" install --upgrade --no-cache-dir --no-deps --no-build-isolation "$PYMC_CORE_LOCAL_DIR"
        return 0
    fi

    core_repo="$PYMC_CORE_REPO"
    case "$core_repo" in
        *.git) ;;
        *) core_repo="${core_repo}.git" ;;
    esac
    core_ref=$(resolve_core_ref)
    core_spec="pyMC_core[hardware] @ git+${core_repo}@${core_ref}"
    stage "Installing pyMC_core"
    info "Repo: ${PYMC_CORE_REPO}"
    info "Ref: ${core_ref}"
    "$VENV_PIP" install --upgrade --no-cache-dir --no-deps --no-build-isolation "$core_spec"
}

get_installed_core_commit() {
    "$VENV_PYTHON" - <<'PY'
import glob
import json
import os

matches = glob.glob("/opt/pymc_repeater/venv/lib/python*/site-packages/pymc_core-*.dist-info/direct_url.json")
for path in matches:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        print(data.get("vcs_info", {}).get("commit_id", ""))
        raise SystemExit(0)
    except Exception:
        pass
print("")
PY
}

resolve_core_commit() {
    local core_repo core_ref

    core_repo="$PYMC_CORE_REPO"
    case "$core_repo" in
        *.git) ;;
        *) core_repo="${core_repo}.git" ;;
    esac
    core_ref=$(resolve_core_ref)
    git ls-remote "$core_repo" "$core_ref" 2>/dev/null | awk 'NR==1 {print $1}'
}

install_repeater_package() {
    stage "Installing pyMC Repeater into venv"
    info "Installing checked-out repo without re-resolving dependencies"
    "$VENV_PIP" install --upgrade --no-cache-dir --no-deps --no-build-isolation "$SCRIPT_DIR"
}

create_init_script() {
    cat > "$INIT_SCRIPT" <<'EOF'
#!/bin/sh
DAEMON="__DAEMON__"
PIDFILE="__PIDFILE__"
LOGFILE="__LOGFILE__"
WORKDIR="__WORKDIR__"
CONFIG_FILE="__CONFIG_FILE__"
RUN_AS="__RUN_AS__"

start() {
    mkdir -p "$(dirname "$PIDFILE")" "$(dirname "$LOGFILE")" "$WORKDIR"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "__SERVICE_NAME__ is already running."
        return 0
    fi
    start-stop-daemon --start --quiet --background --make-pidfile --pidfile "$PIDFILE" \
        --chuid "$RUN_AS" --exec /bin/sh -- -c "cd \"$WORKDIR\" && exec \"$DAEMON\" -m repeater.main --config \"$CONFIG_FILE\" >>\"$LOGFILE\" 2>&1"
}

stop() {
    if [ ! -f "$PIDFILE" ]; then
        echo "__SERVICE_NAME__ is not running."
        return 0
    fi
    start-stop-daemon --stop --quiet --retry 5 --pidfile "$PIDFILE" >/dev/null 2>&1 || true
    rm -f "$PIDFILE"
}

status() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "__SERVICE_NAME__ is running."
        return 0
    fi
    echo "__SERVICE_NAME__ is stopped."
    return 1
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    restart) stop; start ;;
    status) status ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
EOF
    sed -i \
        -e "s|__DAEMON__|${VENV_PYTHON}|g" \
        -e "s|__PIDFILE__|${PIDFILE}|g" \
        -e "s|__LOGFILE__|${LOGFILE}|g" \
        -e "s|__WORKDIR__|${DATA_DIR}|g" \
        -e "s|__CONFIG_FILE__|${CONFIG_DIR}/config.yaml|g" \
        -e "s|__RUN_AS__|${SERVICE_USER}|g" \
        -e "s|__SERVICE_NAME__|${SERVICE_NAME}|g" \
        "$INIT_SCRIPT"
    chmod 0755 "$INIT_SCRIPT"
}

get_primary_ip() {
    ip -o -4 addr show dev eth0 2>/dev/null | awk 'NR==1 { sub(/\/.*/, "", $4); print $4; exit }'
}

get_config_value() {
    local key_path="$1"
    local fallback="${2:-}"

    python3 - "$CONFIG_DIR/config.yaml" "$key_path" "$fallback" <<'PY'
import sys
import yaml

config_path, key_path, fallback = sys.argv[1:4]
try:
    with open(config_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
except FileNotFoundError:
    print(fallback)
    raise SystemExit(0)

current = data
for part in key_path.split("."):
    if isinstance(current, dict) and part in current:
        current = current[part]
    else:
        print(fallback)
        raise SystemExit(0)

if current in (None, ""):
    print(fallback)
elif isinstance(current, float):
    rendered = ("%f" % current).rstrip("0").rstrip(".")
    print(rendered or "0")
else:
    print(current)
PY
}

list_buildroot_boards() {
    python3 - "$BUILDROOT_RADIO_SETTINGS_JSON" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

for index, (key, entry) in enumerate(data.get("buildroot_hardware", {}).items(), start=1):
    print(f"{index}|{key}|{entry.get('name', key)}|{entry.get('description', '')}")
PY
}

resolve_buildroot_board() {
    local choice="$1"

    python3 - "$BUILDROOT_RADIO_SETTINGS_JSON" "$choice" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

choice = sys.argv[2].strip().lower()
boards = list((data.get("buildroot_hardware") or {}).items())

for index, (key, entry) in enumerate(boards, start=1):
    aliases = {str(index), key.lower(), str(entry.get("name", "")).lower()}
    aliases.update(alias.lower() for alias in entry.get("aliases", []))
    if choice in aliases:
        print(key)
        raise SystemExit(0)

raise SystemExit(1)
PY
}

get_default_buildroot_board() {
    python3 - "$BUILDROOT_RADIO_SETTINGS_JSON" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

default_board = data.get("default_board")
boards = data.get("buildroot_hardware", {})
if default_board and default_board in boards:
    print(default_board)
else:
    for key in boards:
        print(key)
        break
PY
}

get_buildroot_board_label() {
    local board_key="$1"
    python3 - "$BUILDROOT_RADIO_SETTINGS_JSON" "$board_key" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

entry = (data.get("buildroot_hardware") or {}).get(sys.argv[2], {})
print(entry.get("name", sys.argv[2]))
PY
}

get_buildroot_board_field() {
    local board_key="$1"
    local field="$2"

    python3 - "$BUILDROOT_RADIO_SETTINGS_JSON" "$board_key" "$field" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

entry = (data.get("buildroot_hardware") or {}).get(sys.argv[2], {})
value = entry.get(sys.argv[3], "")
if value is None:
    value = ""
print(value)
PY
}

list_radio_presets() {
    python3 - "$RADIO_PRESETS_JSON" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

entries = ((data.get("config") or {}).get("suggested_radio_settings") or {}).get("entries", [])
for index, entry in enumerate(entries, start=1):
    print(f"{index}|{entry.get('title', '')}|{entry.get('description', '')}")
PY
}

resolve_radio_preset() {
    local choice="$1"

    python3 - "$RADIO_PRESETS_JSON" "$choice" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

choice = sys.argv[2].strip().lower()
entries = ((data.get("config") or {}).get("suggested_radio_settings") or {}).get("entries", [])
for index, entry in enumerate(entries, start=1):
    title = entry.get("title", "")
    if choice in {str(index), title.lower()}:
        print(title)
        raise SystemExit(0)

raise SystemExit(1)
PY
}

get_default_radio_preset() {
    python3 - "$BUILDROOT_RADIO_SETTINGS_JSON" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

print(data.get("default_radio_preset", ""))
PY
}

get_radio_preset_field() {
    local preset_title="$1"
    local field="$2"

    python3 - "$RADIO_PRESETS_JSON" "$preset_title" "$field" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)

preset_title, field = sys.argv[2:4]
entries = ((data.get("config") or {}).get("suggested_radio_settings") or {}).get("entries", [])
for entry in entries:
    if entry.get("title") == preset_title:
        print(entry.get(field, ""))
        raise SystemExit(0)

raise SystemExit(1)
PY
}

get_radio_frequency_mhz() {
    python3 - "$CONFIG_DIR/config.yaml" <<'PY'
import yaml

with open(__import__("sys").argv[1], "r", encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}
freq = ((data.get("radio") or {}).get("frequency")) or 910525000
print(f"{float(freq) / 1_000_000:.3f}".rstrip("0").rstrip("."))
PY
}

get_radio_bandwidth_khz() {
    python3 - "$CONFIG_DIR/config.yaml" <<'PY'
import yaml

with open(__import__("sys").argv[1], "r", encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}
bw = ((data.get("radio") or {}).get("bandwidth")) or 62500
print(f"{float(bw) / 1000:.3f}".rstrip("0").rstrip("."))
PY
}

select_buildroot_board() {
    local choice="${LUCKFOX_RADIO_PROFILE:-${PYMC_RADIO_PROFILE:-${PYMC_BUILDROOT_BOARD:-}}}"
    local default_board

    default_board=$(get_default_buildroot_board)

    if [ -n "$choice" ]; then
        resolve_buildroot_board "$choice" || fail "Unknown Buildroot board choice: $choice"
        return 0
    fi

    printf 'Select Luckfox radio board:\n' >&2
    while IFS='|' read -r index key name description; do
        [ -n "$index" ] || continue
        printf '  %s) %s' "$index" "$name" >&2
        [ -n "$description" ] && printf ' - %s' "$description" >&2
        printf '\n' >&2
    done <<EOF
$(list_buildroot_boards)
EOF

    choice=$(prompt_value "Board" "$default_board")
    resolve_buildroot_board "$choice" || fail "Unknown Buildroot board choice: $choice"
}

select_radio_preset() {
    local choice="${PYMC_RADIO_PRESET:-}"
    local default_preset

    default_preset=$(get_default_radio_preset)

    if [ -n "$choice" ]; then
        resolve_radio_preset "$choice" || fail "Unknown radio preset choice: $choice"
        return 0
    fi

    printf 'Select radio preset:\n' >&2
    while IFS='|' read -r index title description; do
        [ -n "$index" ] || continue
        printf '  %s) %s' "$index" "$title" >&2
        [ -n "$description" ] && printf ' - %s' "$description" >&2
        printf '\n' >&2
    done <<EOF
$(list_radio_presets)
EOF

    choice=$(prompt_value "Preset" "$default_preset")
    resolve_radio_preset "$choice" || fail "Unknown radio preset choice: $choice"
}

write_repeater_config() {
    local node_name="$1"
    local admin_password="$2"
    local jwt_secret="$3"
    local freq_mhz="$4"
    local sf="$5"
    local bw_khz="$6"
    local coding_rate="$7"
    local tx_power="$8"
    local board_key="$9"

    local config_path temp_config example_config yq_cmd stripped_user temp_merged

    config_path="$CONFIG_DIR/config.yaml"
    example_config="$CONFIG_DIR/config.yaml.example"
    temp_config=$(mktemp)
    stripped_user=$(mktemp)
    temp_merged=$(mktemp)

    python3 - "$config_path" "$temp_config" "$RADIO_SETTINGS_JSON" "$BUILDROOT_RADIO_SETTINGS_JSON" "$node_name" "$admin_password" "$jwt_secret" "$freq_mhz" "$sf" "$bw_khz" "$coding_rate" "$tx_power" "$board_key" <<'PY'
import json
import sys
import yaml

(
    config_path,
    output_path,
    radio_settings_path,
    buildroot_settings_path,
    node_name,
    admin_password,
    jwt_secret,
    freq_mhz,
    sf,
    bw_khz,
    coding_rate,
    tx_power,
    board_key,
) = sys.argv[1:14]

with open(config_path, "r", encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}
with open(radio_settings_path, "r", encoding="utf-8") as fh:
    radio_settings = json.load(fh)
with open(buildroot_settings_path, "r", encoding="utf-8") as fh:
    buildroot_settings = json.load(fh)

board = (buildroot_settings.get("buildroot_hardware") or {}).get(board_key)
if not board:
    raise SystemExit(f"Unknown Buildroot board: {board_key}")

hardware = ((radio_settings.get("hardware") or {}).get(board.get("hardware_id")) or {}).copy()
sx1262 = hardware.copy()
sx1262.update(board.get("sx1262_overrides") or {})

sx1262.setdefault("bus_id", 0)
sx1262.setdefault("cs_id", 0)
sx1262.setdefault("txled_pin", -1)
sx1262.setdefault("rxled_pin", -1)
sx1262.setdefault("is_waveshare", False)

repeater = data.setdefault("repeater", {})
security = repeater.setdefault("security", {})
radio = data.setdefault("radio", {})

repeater["node_name"] = node_name
security["admin_password"] = admin_password
security["jwt_secret"] = jwt_secret

radio["frequency"] = int(round(float(freq_mhz) * 1_000_000))
radio["spreading_factor"] = int(sf)
radio["bandwidth"] = int(round(float(bw_khz) * 1000))
radio["coding_rate"] = int(coding_rate)
radio["tx_power"] = int(tx_power)

data["radio_type"] = hardware.get("radio_type", "sx1262")
data["sx1262"] = sx1262

if data["radio_type"] == "sx1262_ch341":
    ch341 = data.setdefault("ch341", {})
    if "vid" in hardware:
        ch341["vid"] = hardware["vid"]
    if "pid" in hardware:
        ch341["pid"] = hardware["pid"]

with open(output_path, "w", encoding="utf-8") as fh:
    yaml.safe_dump(data, fh, sort_keys=False)
PY

    yq_cmd=$(ensure_yq 2>/dev/null || true)
    if [ -n "$yq_cmd" ] && [ -f "$example_config" ]; then
        "$yq_cmd" eval '... comments=""' "$temp_config" > "$stripped_user" 2>/dev/null || cp "$temp_config" "$stripped_user"
        if "$yq_cmd" eval-all '. as $item ireduce ({}; . * $item)' "$example_config" "$stripped_user" > "$temp_merged" 2>/dev/null \
            && "$yq_cmd" eval '.' "$temp_merged" >/dev/null 2>&1; then
            mv "$temp_merged" "$config_path"
        else
            warn "yq merge failed; writing config without preserving comments."
            cp "$temp_config" "$config_path"
        fi
    else
        cp "$temp_config" "$config_path"
    fi

    rm -f "$temp_config" "$stripped_user" "$temp_merged"
}

seed_repeater_config() {
    local node_name admin_password jwt_secret board_key board_name preset_title freq_mhz sf bw_khz coding_rate tx_power

    stage "Configuring repeater"

    node_name="${PYMC_NODE_NAME:-}"
    if [ -n "$node_name" ]; then
        validate_node_name "$node_name" || fail "Invalid repeater name in PYMC_NODE_NAME."
    else
        while :; do
            node_name=$(prompt_value "Repeater name" "$(get_config_value repeater.node_name luckfox-repeater)")
            if validate_node_name "$node_name"; then
                break
            fi
        done
    fi

    board_key=$(select_buildroot_board)
    board_name=$(get_buildroot_board_label "$board_key")
    info "Selected board: $board_name"

    admin_password="${PYMC_ADMIN_PASSWORD:-}"
    if [ -n "$admin_password" ]; then
        validate_admin_password "$admin_password" || fail "Invalid admin password in PYMC_ADMIN_PASSWORD."
    else
        admin_password=$(prompt_secret "Admin password")
    fi

    jwt_secret="${PYMC_JWT_SECRET:-}"
    [ -n "$jwt_secret" ] || jwt_secret=$(python3 -c 'import secrets; print(secrets.token_hex(32))')

    preset_title=$(select_radio_preset)
    info "Using preset: $preset_title"

    freq_mhz="${PYMC_RADIO_FREQUENCY_MHZ:-$(get_radio_preset_field "$preset_title" frequency)}"
    sf="${PYMC_RADIO_SF:-$(get_radio_preset_field "$preset_title" spreading_factor)}"
    bw_khz="${PYMC_RADIO_BANDWIDTH_KHZ:-$(get_radio_preset_field "$preset_title" bandwidth)}"
    coding_rate="${PYMC_RADIO_CODING_RATE:-$(get_radio_preset_field "$preset_title" coding_rate)}"
    tx_power="${PYMC_RADIO_TX_POWER_DBM:-$(get_buildroot_board_field "$board_key" tx_power)}"
    [ -n "$tx_power" ] || tx_power="$(get_config_value radio.tx_power 22)"

    write_repeater_config "$node_name" "$admin_password" "$jwt_secret" "$freq_mhz" "$sf" "$bw_khz" "$coding_rate" "$tx_power" "$board_key"
    info "Saved config for ${node_name}"
}

configure_repeater() {
    ensure_root
    [ -f "$CONFIG_DIR/config.yaml" ] || fail "Config file is missing. Run install first."
    seed_repeater_config
    if service_exists; then
        "$INIT_SCRIPT" restart
    fi
}

configure_radio_profile() {
    local board_key

    ensure_root
    [ -f "$CONFIG_DIR/config.yaml" ] || fail "Config file is missing. Run install first."
    board_key=$(select_buildroot_board)
    write_repeater_config \
        "$(get_config_value repeater.node_name luckfox-repeater)" \
        "$(get_config_value repeater.security.admin_password admin123)" \
        "$(get_config_value repeater.security.jwt_secret "$(python3 -c 'import secrets; print(secrets.token_hex(32))')")" \
        "$(get_radio_frequency_mhz)" \
        "$(get_config_value radio.spreading_factor 7)" \
        "$(get_radio_bandwidth_khz)" \
        "$(get_config_value radio.coding_rate 5)" \
        "${PYMC_RADIO_TX_POWER_DBM:-$(get_buildroot_board_field "$board_key" tx_power)}" \
        "$board_key"
    info "Applied board profile: $(get_buildroot_board_label "$board_key")"
    if service_exists; then
        "$INIT_SCRIPT" restart
    fi
}

service_exists() {
    [ -x "$INIT_SCRIPT" ]
}

is_installed() {
    [ -d "$INSTALL_DIR" ] && service_exists
}

is_running() {
    [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

start_or_restart_service() {
    if is_running; then
        "$INIT_SCRIPT" restart
    else
        "$INIT_SCRIPT" start
    fi
}

get_version() {
    if [ -x "$VENV_PYTHON" ]; then
        "$VENV_PYTHON" -c "from importlib.metadata import version; print(version('pymc_repeater'))" 2>/dev/null || echo "not installed"
    else
        echo "not installed"
    fi
}

prepare_git_version() {
    local git_version

    if [ -d "$SCRIPT_DIR/.git" ]; then
        stage "Inspecting checked-out repo version"
        info "Fetching tags for setuptools_scm version detection"
        git -C "$SCRIPT_DIR" fetch --tags 2>/dev/null || true
        git_version=$(python3 -m setuptools_scm 2>/dev/null || echo "1.0.5")
        export SETUPTOOLS_SCM_PRETEND_VERSION="$git_version"
        export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYMC_REPEATER="$git_version"
        info "Using version: $git_version"
    else
        git_version="1.0.5"
        export SETUPTOOLS_SCM_PRETEND_VERSION="$git_version"
        export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYMC_REPEATER="$git_version"
        info "Using fallback version: $git_version"
    fi

    printf '%s\n' "$git_version"
}

doctor() {
    stage "Checking Buildroot image baseline"

    for cmd in bash git python3 dialog jq wget sudo sqlite3 start-stop-daemon; do
        if command -v "$cmd" >/dev/null 2>&1; then
            info "found $cmd"
        else
            warn "missing $cmd"
        fi
    done

    if python3 -m venv --help >/dev/null 2>&1; then
        info "python venv support available"
    else
        warn "python venv support missing"
    fi

    if python3 - <<'PY'
modules = [
    "sqlite3",
    "yaml",
    "cherrypy",
    "cherrypy_cors",
    "autocommand",
    "jaraco.collections",
    "jaraco.text",
    "paho.mqtt.client",
    "psutil",
    "jwt",
    "ws4py",
    "nacl",
    "periphery",
    "spidev",
    "serial",
    "usb",
    "Crypto",
]
for module in modules:
    __import__(module)
PY
    then
        info "python runtime packages are present"
    else
        warn "python runtime packages are missing"
    fi

    for path in /dev/spidev* /dev/gpiochip*; do
        [ -e "$path" ] && info "detected $path"
    done
}

check_venv_runtime() {
    "$VENV_PYTHON" - <<'PY'
checks = [
    ("import yaml", "PyYAML"),
    ("import cherrypy", "CherryPy"),
    ("import cherrypy_cors", "cherrypy-cors"),
    ("import paho.mqtt.client", "paho-mqtt"),
    ("import psutil", "psutil"),
    ("import jwt", "PyJWT"),
    ("import ws4py", "ws4py"),
    ("import nacl", "PyNaCl"),
    ("import periphery", "python-periphery"),
    ("import spidev", "spidev"),
    ("import serial", "pyserial"),
    ("import usb", "pyusb"),
    ("from Crypto.Cipher import AES", "pycryptodome AES backend"),
]
failed = []
for stmt, label in checks:
    try:
        exec(stmt, {})
    except Exception as exc:
        failed.append((label, exc))

if failed:
    print("Buildroot runtime validation failed:")
    for label, exc in failed:
        print(f" - {label}: {exc}")
    raise SystemExit(1)
PY
}

install_repeater() {
    local git_version ip_address

    ensure_root
    stage "Preparing Buildroot installation"
    install_system_packages
    ensure_yq >/dev/null 2>&1 || true
    ensure_service_user

    stage "Preparing directories and config"
    info "Install dir: $INSTALL_DIR"
    info "Config dir: $CONFIG_DIR"
    info "Data dir: $DATA_DIR"
    mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$LOG_DIR" "$DATA_DIR" "$DATA_DIR/.config/pymc_repeater"
    chown -R root:root "$CONFIG_DIR" "$LOG_DIR" "$DATA_DIR"
    chmod 755 "$INSTALL_DIR" "$DATA_DIR"
    chmod 750 "$CONFIG_DIR" "$LOG_DIR"

    cp "$SCRIPT_DIR/config.yaml.example" "$CONFIG_DIR/config.yaml.example"
    [ -f "$CONFIG_DIR/config.yaml" ] || cp "$SCRIPT_DIR/config.yaml.example" "$CONFIG_DIR/config.yaml"
    cp "$SCRIPT_DIR/radio-settings.json" "$DATA_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/radio-presets.json" "$DATA_DIR/" 2>/dev/null || true

    ensure_venv
    ensure_venv_build_backend
    stage "Cleaning legacy install state"
    cleanup_legacy_install_state

    git_version=$(prepare_git_version)

    preinstall_r2_wheels
    install_buildroot_dependencies
    install_core_into_venv
    install_repeater_package
    remove_shadowing_buildroot_native_packages
    link_system_site_packages

    stage "Validating installed runtime"
    if check_venv_runtime; then
        info "Installed Python runtime looks usable"
    else
        fail "Installed packages are present but one or more native modules are unusable on this image."
    fi

    seed_repeater_config

    stage "Writing Buildroot init service"
    create_init_script

    stage "Starting service"
    start_or_restart_service

    ip_address=$(get_primary_ip)
    if is_running; then
        printf '\nService is running on: http://%s:8000\n' "${ip_address}"
    else
        fail "Installation completed but the service failed to start. Check: sh $0 logs"
    fi
}

upgrade_repeater() {
    local current_version new_version ip_address git_version
    local target_core_commit installed_core_commit

    ensure_root
    is_installed || fail "Service is not installed."
    current_version=$(get_version)

    ensure_venv
    ensure_venv_build_backend
    stage "Cleaning legacy install state"
    cleanup_legacy_install_state
    git_version=$(prepare_git_version)
    preinstall_r2_wheels

    stage "Upgrading pyMC Repeater"
    if [ "${PYMC_FORCE_DEPS:-0}" = "1" ]; then
        info "Forcing dependency reinstall"
        install_buildroot_dependencies
    elif check_buildroot_dependencies_installed >/dev/null 2>&1; then
        info "Python dependency wheels already satisfy Buildroot requirements; skipping reinstall"
    else
        info "One or more Python dependency wheels are missing or out of range; reinstalling"
        install_buildroot_dependencies
    fi

    target_core_commit=$(resolve_core_commit)
    installed_core_commit=$(get_installed_core_commit)
    if [ -n "$target_core_commit" ] && [ "$installed_core_commit" = "$target_core_commit" ] && [ "${PYMC_FORCE_CORE:-0}" != "1" ]; then
        info "pyMC_core is already at ${target_core_commit}; skipping reinstall"
    else
        install_core_into_venv
    fi

    if [ "$current_version" = "$git_version" ] && [ "${PYMC_FORCE_REPEATER:-0}" != "1" ]; then
        info "pyMC Repeater is already at ${git_version}; skipping reinstall"
    else
        ensure_yq >/dev/null 2>&1 || true
        install_repeater_package
    fi
    remove_shadowing_buildroot_native_packages
    link_system_site_packages
    stage "Validating installed runtime"
    if check_venv_runtime; then
        info "Installed Python runtime looks usable"
    else
        fail "Installed packages are present but one or more native modules are unusable on this image."
    fi

    stage "Restarting service"
    "$INIT_SCRIPT" restart

    new_version=$(get_version)
    ip_address=$(get_primary_ip)

    if is_running; then
        printf '\nUpgrade complete.\n'
        printf 'Version: %s -> %s\n' "$current_version" "$new_version"
        printf 'Service is running on: http://%s:8000\n' "${ip_address}"
    else
        fail "Upgrade completed but the service failed to start. Check: sh $0 logs"
    fi
}

uninstall_repeater() {
    ensure_root

    stage "Removing service"
    service_exists && "$INIT_SCRIPT" stop || true
    rm -f "$INIT_SCRIPT"
    rm -rf "$INSTALL_DIR" "$CONFIG_DIR" "$LOG_DIR" "$DATA_DIR"
}

manage_service() {
    local action="$1"
    ensure_root
    service_exists || fail "Service is not installed."
    "$INIT_SCRIPT" "$action"
}

show_status() {
    local ip_address version
    version=$(get_version)
    ip_address=$(get_primary_ip)

    if ! is_installed; then
        printf 'Installation Status: Not Installed\n'
        return 0
    fi

    printf 'Installation Status: Installed\n'
    printf 'Version: %s\n' "$version"
    printf 'Install Directory: %s\n' "$INSTALL_DIR"
    printf 'Config Directory: %s\n' "$CONFIG_DIR"
    printf 'Log File: %s\n' "$LOGFILE"
    printf 'Dashboard: http://%s:8000\n' "$ip_address"
    if is_running; then
        printf 'Service Status: Running\n'
    else
        printf 'Service Status: Stopped\n'
    fi
}

show_logs() {
    mkdir -p "$LOG_DIR"
    touch "$LOGFILE"
    tail -f "$LOGFILE"
}

delegate_to_stock_manage() {
    exec bash "$SCRIPT_DIR/manage.sh" "$@"
}

usage() {
    cat <<'EOF'
Usage: bash buildroot-manage.sh <command>

Commands:
  doctor      Check Buildroot/Luckfox prerequisites
  install     Install pyMC Repeater on the Buildroot image
  upgrade     Upgrade the Buildroot installation from the checked-out repo
  config      Prompt for repeater settings and rewrite config.yaml
  configure   Same as config
  radio-profile  Reapply the Luckfox board radio config only
  start       Start the init.d service
  stop        Stop the init.d service
  restart     Restart the init.d service
  status      Show Buildroot service status
  logs        Tail the Buildroot log file
  uninstall   Remove the Buildroot installation
EOF
}

case "${1:-}" in
    doctor)
        doctor
        ;;
    install)
        install_repeater
        ;;
    upgrade)
        upgrade_repeater
        ;;
    config|configure)
        configure_repeater
        ;;
    radio-profile)
        configure_radio_profile
        ;;
    start|stop|restart)
        manage_service "$1"
        ;;
    status)
        show_status
        ;;
    logs)
        show_logs
        ;;
    uninstall)
        uninstall_repeater
        ;;
    ""|help|-h|--help)
        usage
        ;;
    *)
        fail "Unknown command: ${1}"
        ;;
esac
