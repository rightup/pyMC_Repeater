#!/bin/bash
# openHop Repeater Management Script - Deploy, Upgrade, Uninstall

set -Eeuo pipefail

readonly SCRIPT_PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/$(basename -- "${BASH_SOURCE[0]}")"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

readonly REPOSITORY_URL="https://github.com/openhop-dev/openhop_repeater.git"
readonly DEFAULT_UPGRADE_REF="dev"
readonly PACKAGE_DIST_NAME="openhop_repeater"
readonly LOCK_FILE="/run/lock/openhop-repeater-manage.lock"

readonly INSTALL_DIR="/opt/openhop_repeater"
readonly VENV_DIR="$INSTALL_DIR/venv"
readonly VENV_PIP="$VENV_DIR/bin/pip"
readonly VENV_PYTHON="$VENV_DIR/bin/python"
readonly CONFIG_DIR="/etc/openhop_repeater"
readonly LOG_DIR="/var/log/openhop_repeater"
readonly DATA_DIR="/var/lib/openhop_repeater"
readonly SERVICE_USER="repeater"
readonly SERVICE_NAME="openhop-repeater"
readonly SILENT_MODE="${PYMC_SILENT:-${SILENT:-}}"

readonly LEGACY_PYMC_INSTALL_DIR="/opt/pymc_repeater"
readonly LEGACY_PYMC_CONFIG_DIR="/etc/pymc_repeater"
readonly LEGACY_PYMC_LOG_DIR="/var/log/pymc_repeater"
readonly LEGACY_PYMC_DATA_DIR="/var/lib/pymc_repeater"

# R2 Wheels Configuration improves install speed on ARM devices
readonly R2_BASE_URL="https://wheel.pymc.dev/pymc_build_deps"
readonly R2_ENABLED=1  # Set to 0 to disable R2 wheels and always build from source

error() {
    printf 'ERROR: %s\n' "$*" >&2
}

die() {
    error "$*"
    exit 1
}

is_safe_git_ref() {
    local ref="${1:-}"
    [[ "$ref" =~ ^[a-zA-Z0-9._/-]{1,80}$ ]]
}

is_expected_repo_checkout() {
    local source_dir=$1
    local remote_url=""

    [ -d "${source_dir}/.git" ] || return 1
    remote_url="$(git -C "$source_dir" remote get-url origin 2>/dev/null || true)"
    [ -n "$remote_url" ] || return 1
    case "$remote_url" in
        "$REPOSITORY_URL"|"${REPOSITORY_URL%.git}"|git@github.com:openhop-dev/openhop_repeater.git)
            return 0
            ;;
    esac
    return 1
}

# Refresh the checkout supplying both the package and its management logic.
# Never pull the caller's cwd or silently merge/stash administrator changes.
refresh_upgrade_checkout() {
    local silent="${1:-true}"
    local before after branch changes

    if [ ! -e "$SCRIPT_DIR/.git" ]; then
        echo "No Git checkout beside manage.sh; keeping standalone upgrade source."
        return 0
    fi
    if ! command -v git >/dev/null 2>&1; then
        error "Git is required to update the manage.sh checkout"
        return 1
    fi
    branch="$(git -C "$SCRIPT_DIR" symbolic-ref --quiet --short HEAD)" || {
        error "Checkout has detached HEAD; switch to the branch you want to upgrade first"
        return 1
    }
    changes="$(git -C "$SCRIPT_DIR" status --porcelain --untracked-files=no)" || return 1
    if [ -n "$changes" ]; then
        error "Checkout has local changes; commit or stash them before upgrading"
        return 1
    fi
    if ! git -C "$SCRIPT_DIR" rev-parse --verify '@{upstream}' >/dev/null 2>&1; then
        error "Branch $branch has no upstream; configure its tracking branch before upgrading"
        return 1
    fi
    before="$(git -C "$SCRIPT_DIR" hash-object "$SCRIPT_PATH")" || return 1
    echo ">>> Pulling checkout branch $branch in $SCRIPT_DIR ..."
    if ! git -C "$SCRIPT_DIR" pull --ff-only; then
        error "Git pull failed; upgrade aborted before changing the installation"
        return 1
    fi
    after="$(git -C "$SCRIPT_DIR" hash-object "$SCRIPT_PATH")" || return 1
    echo "Checkout revision: $(git -C "$SCRIPT_DIR" rev-parse HEAD)"
    if [ "$before" != "$after" ]; then
        echo "manage.sh changed; restarting with the updated upgrade logic..."
        # The new process acquires the lock normally. No installation changes
        # have happened yet; aborting on a competing upgrade is safe.
        exec 9>&-
        if [[ "$silent" == "true" ]]; then
            exec bash "$SCRIPT_PATH" upgrade --silent
        else
            exec bash "$SCRIPT_PATH" upgrade --interactive
        fi
    fi
    return 0
}

determine_package_source() {
    local script_dir=$1
    local requested_ref=$2

    if [ -f "${script_dir}/pyproject.toml" ]; then
        printf '%s\n' "$script_dir"
    else
        printf '%s\n' "git+${REPOSITORY_URL}@${requested_ref}"
    fi
}

build_package_requirement() {
    local package_source=$1

    if [[ "$package_source" == git+* ]]; then
        printf '%s\n' "${PACKAGE_DIST_NAME}[hardware] @ ${package_source}"
    else
        printf '%s\n' "${package_source}[hardware]"
    fi
}

log_package_source() {
    local package_source=$1

    if [[ "$package_source" == git+* ]]; then
        echo "Using package source: Git URL (${package_source})"
    else
        echo "Using package source: local checkout (${package_source})"
    fi
}

acquire_global_lock() {
    if ! command -v flock >/dev/null 2>&1; then
        error "Required command not found: flock (install util-linux)"
        return 1
    fi

    mkdir -p "$(dirname "$LOCK_FILE")" || {
        error "Unable to create lock directory for ${LOCK_FILE}"
        return 1
    }

    exec 9>"$LOCK_FILE" || {
        error "Unable to open lock file ${LOCK_FILE}"
        return 1
    }

    if ! flock -n 9; then
        error "Another OpenHOP install, upgrade, or uninstall operation is already running"
        return 1
    fi
}

run_locked_action() {
    acquire_global_lock || return 1
    "$@"
}

# ---------------------------------------------------------------------------
# Virtual-environment helpers
# ---------------------------------------------------------------------------

cleanup_stale_source_trees() {
    local removed=0
    local path

    for path in \
        "$INSTALL_DIR/repeater" \
        "$INSTALL_DIR/openhop_core" \
        "$INSTALL_DIR/openhop-repeater" \
        "$INSTALL_DIR/openhop-core" \
        "$LEGACY_PYMC_INSTALL_DIR/repeater" \
        "$LEGACY_PYMC_INSTALL_DIR/pymc_core" \
        "$LEGACY_PYMC_INSTALL_DIR/pymc-repeater" \
        "$LEGACY_PYMC_INSTALL_DIR/pymc-core"
    do
        if [ -e "$path" ]; then
            rm -rf "$path"
            removed=1
            echo "    ✓ Removed stale source tree at $path"
        fi
    done

    if [ "$removed" -eq 0 ]; then
        echo "    ✓ No stale source-tree paths found"
    fi
}

cleanup_broken_distribution_metadata() {
    local site_packages_dir
    local removed=0

    for site_packages_dir in "$VENV_DIR"/lib/python*/site-packages; do
        [ -d "$site_packages_dir" ] || continue

        while IFS= read -r path; do
            [ -n "$path" ] || continue
            rm -rf "$path"
            removed=1
            echo "    ✓ Removed broken package metadata at $path"
        done < <(
            find "$site_packages_dir" \
                \( -type d -name '~*dist-info' -o -type d -name '~*egg-info' -o -type f -name '~*.pth' \) \
                -print 2>/dev/null | sort
        )
    done

    if [ "$removed" -eq 0 ]; then
        echo "    ✓ No broken package metadata found"
    fi
}

migrate_legacy_paths() {
    local timestamp legacy current label backup_path
    timestamp="$(date +%Y%m%d_%H%M%S)"

    migrate_one_path() {
        legacy="$1"
        current="$2"
        label="$3"

        if [ ! -e "$legacy" ]; then
            return 0
        fi

        mkdir -p "$current" 2>/dev/null || true

        if [ ! -e "$current" ] || [ -z "$(ls -A "$current" 2>/dev/null)" ]; then
            rm -rf "$current" 2>/dev/null || true
            mv "$legacy" "$current"
            echo "    ✓ Migrated legacy $label path: $legacy -> $current"
            return 0
        fi

        cp -an "$legacy"/. "$current"/ 2>/dev/null || true
        backup_path="${legacy}.migrated.${timestamp}"
        mv "$legacy" "$backup_path"
        echo "    ✓ Merged legacy $label data into $current"
        echo "    ✓ Archived legacy $label path at $backup_path"
    }

    migrate_one_path "$LEGACY_PYMC_CONFIG_DIR" "$CONFIG_DIR" "config"
    migrate_one_path "$LEGACY_PYMC_LOG_DIR" "$LOG_DIR" "log"
    migrate_one_path "$LEGACY_PYMC_DATA_DIR" "$DATA_DIR" "data"
    migrate_one_path "$LEGACY_PYMC_INSTALL_DIR" "$INSTALL_DIR" "install"
}

ensure_service_user_home() {
    local current_home

    if ! id "$SERVICE_USER" &>/dev/null; then
        return 0
    fi

    current_home="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
    if [ -z "$current_home" ]; then
        return 0
    fi

    if [ "$current_home" != "$DATA_DIR" ]; then
        mkdir -p "$DATA_DIR" 2>/dev/null || true
        usermod -d "$DATA_DIR" "$SERVICE_USER" 2>/dev/null || true
        echo "    ✓ Updated $SERVICE_USER home directory: $current_home -> $DATA_DIR"
    fi
}

# Create (or re-create) the dedicated venv for openhop_repeater
ensure_venv() {
    local recreate=0

    if [ ! -x "$VENV_PYTHON" ]; then
        recreate=1
    elif ! "$VENV_PYTHON" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
        # Venv python exists but points to a missing interpreter (stale venv).
        recreate=1
    elif ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
        # Pip script/shebang can break after Python upgrades; treat as stale.
        recreate=1
    fi

    if [ "$recreate" -eq 1 ]; then
        if [ -d "$VENV_DIR" ]; then
            echo ">>> Rebuilding broken virtual environment at $VENV_DIR ..."
            rm -rf "$VENV_DIR"
        else
            echo ">>> Creating virtual environment at $VENV_DIR ..."
        fi
        python3 -m venv --system-site-packages "$VENV_DIR"
    fi

    cleanup_broken_distribution_metadata

    # Always use python -m pip so we don't rely on a potentially stale pip wrapper.
    if ! "$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel; then
        error "Failed to bootstrap pip/setuptools/wheel in venv"
        return 1
    fi
}

# Migrate an existing system-pip install into the venv.
# Idempotent: safe to call on every upgrade.
migrate_to_venv() {
    echo ">>> Checking for legacy system-pip installation..."

    # 1. Ensure the venv exists
    ensure_venv
    ensure_service_user_home

    # 2. Remove legacy PYTHONPATH from the service unit
    local svc_unit="/etc/systemd/system/openhop-repeater.service"
    if [ -f "$svc_unit" ]; then
        if grep -q 'PYTHONPATH' "$svc_unit" 2>/dev/null; then
            sed -i '/^Environment=.*PYTHONPATH/d' "$svc_unit"
            echo "    ✓ Removed legacy PYTHONPATH from service unit"
        fi
        # 3. Fix WorkingDirectory if still pointing at old source
        if grep -q 'WorkingDirectory=/opt/openhop_repeater' "$svc_unit" 2>/dev/null; then
            sed -i 's|WorkingDirectory=/opt/openhop_repeater|WorkingDirectory=/var/lib/openhop_repeater|' "$svc_unit"
            echo "    ✓ Fixed WorkingDirectory in service unit"
        fi
        if grep -q 'WorkingDirectory=/opt/pymc_repeater\|WorkingDirectory=/var/lib/pymc_repeater' "$svc_unit" 2>/dev/null; then
            sed -i 's|WorkingDirectory=/opt/pymc_repeater|WorkingDirectory=/var/lib/openhop_repeater|' "$svc_unit"
            sed -i 's|WorkingDirectory=/var/lib/pymc_repeater|WorkingDirectory=/var/lib/openhop_repeater|' "$svc_unit"
            echo "    ✓ Migrated legacy WorkingDirectory to openhop path"
        fi
        # 4. Ensure ExecStart uses the venv python
        if grep -q 'ExecStart=/usr/bin/python3' "$svc_unit" 2>/dev/null; then
            sed -i "s|ExecStart=/usr/bin/python3|ExecStart=$VENV_PYTHON|" "$svc_unit"
            echo "    ✓ Updated ExecStart to use venv python"
        fi
        if grep -q 'ExecStart=/opt/pymc_repeater/venv/bin/python' "$svc_unit" 2>/dev/null; then
            sed -i "s|ExecStart=/opt/pymc_repeater/venv/bin/python|ExecStart=$VENV_PYTHON|" "$svc_unit"
            echo "    ✓ Migrated legacy ExecStart to openhop venv"
        fi
        systemctl daemon-reload
    fi

    # 5. Remove the package from system python (best-effort)
    python3 -m pip uninstall -y openhop_repeater 2>/dev/null || true
    python3 -m pip uninstall -y openhop_core 2>/dev/null || true
    python3 -m pip uninstall -y pymc_repeater 2>/dev/null || true
    python3 -m pip uninstall -y pymc_core 2>/dev/null || true
    echo "    ✓ Cleaned up system-level packages (if any)"

    # 6. Remove stale source trees that could shadow the venv package
    cleanup_stale_source_trees
}

is_silent_flag() {
    case "${1:-}" in
        --silent|-y|silent) return 0 ;;
        *) return 1 ;;
    esac
}

is_interactive_flag() {
    case "${1:-}" in
        --interactive|-i|interactive) return 0 ;;
        *) return 1 ;;
    esac
}

# Check if we're running in an interactive terminal
if [ ! -t 0 ] || [ -z "$TERM" ]; then
    if [[ "${1:-}" =~ ^(upgrade|start|stop|restart)$ ]] && ! is_interactive_flag "${2:-}"; then
        :
    else
        echo "Error: This script requires an interactive terminal."
        echo "Please run from SSH or a local terminal, not via file manager."
        exit 1
    fi
fi

# Check if whiptail is available, fallback to dialog
if command -v whiptail &> /dev/null; then
    DIALOG="whiptail"
elif command -v dialog &> /dev/null; then
    DIALOG="dialog"
else
    echo "TUI interface requires whiptail or dialog."
    if [ "$EUID" -eq 0 ]; then
        echo "Installing whiptail..."
        apt-get update -qq && apt-get install -y whiptail
        DIALOG="whiptail"
    else
        echo ""
        echo "Please install whiptail: sudo apt-get install -y whiptail"
        echo "Then run this script again."
        exit 1
    fi
fi

# Function to show info box
show_info() {
    $DIALOG --backtitle "openHop Repeater Management" --title "$1" --msgbox "$2" 12 70
}

# Function to show error box
show_error() {
    $DIALOG --backtitle "openHop Repeater Management" --title "Error" --msgbox "$1" 8 60
}

# Function to ask yes/no question
ask_yes_no() {
    $DIALOG --backtitle "openHop Repeater Management" --title "$1" --yesno "$2" 10 70
}

# Function to show progress
show_progress() {
    echo "$2" | $DIALOG --backtitle "openHop Repeater Management" --title "$1" --gauge "$3" 8 70 0
}

print_live_logs_header() {
    local width=70
    local border
    border="$(printf '%*s' "$width" '' | tr ' ' '=')"

    printf '\033[1;36m╔%s╗\033[0m\n' "$border"
    printf '\033[1;36m║\033[0m%*s%-*s%*s\033[1;36m║\033[0m\n' 2 '' $((width - 4)) 'openHop Repeater - Live Logs' 2 ''
    printf '\033[1;36m║\033[0m%*s%-*s%*s\033[1;36m║\033[0m\n' 2 '' $((width - 4)) '(Press Ctrl+C to return)' 2 ''
    printf '\033[1;36m╚%s╝\033[0m\n' "$border"
}

# Function to check if service exists
service_exists() {
    local unit_file="/etc/systemd/system/${SERVICE_NAME}.service"

    if [ -f "$unit_file" ]; then
        return 0
    fi

    systemctl list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1}' | grep -Fxq "${SERVICE_NAME}.service"
}

# Function to check if service is installed
is_installed() {
    [ -d "$INSTALL_DIR" ] && service_exists
}

# Uninstall should be available for partial/broken installs too.
has_installation_artifacts() {
    [ -d "$INSTALL_DIR" ] \
        || [ -d "$CONFIG_DIR" ] \
        || [ -d "$LOG_DIR" ] \
        || [ -d "$DATA_DIR" ] \
        || [ -d "$LEGACY_PYMC_INSTALL_DIR" ] \
        || [ -d "$LEGACY_PYMC_CONFIG_DIR" ] \
        || [ -d "$LEGACY_PYMC_LOG_DIR" ] \
        || [ -d "$LEGACY_PYMC_DATA_DIR" ] \
        || [ -f /etc/systemd/system/openhop-repeater.service ] \
        || service_exists
}

# Function to check if service is running
is_running() {
    systemctl is-active "$SERVICE_NAME" >/dev/null 2>&1
}

# Function to check if service is enabled
is_enabled() {
    systemctl is-enabled "$SERVICE_NAME" >/dev/null 2>&1
}

# Stop/disable legacy service names that can conflict on GPIO.
disable_legacy_services() {
    local legacy_services="pymc-repeater pymc-repeater.service"
    local svc removed_unit=0

    for svc in $legacy_services; do
        systemctl stop "$svc" >/dev/null 2>&1 || true
        systemctl disable "$svc" >/dev/null 2>&1 || true
    done

    if [ -f /etc/systemd/system/pymc-repeater.service ]; then
        rm -f /etc/systemd/system/pymc-repeater.service
        removed_unit=1
    fi

    if [ "$removed_unit" -eq 1 ]; then
        systemctl daemon-reload >/dev/null 2>&1 || true
    fi
}

wait_for_service_active() {
    local timeout_seconds="${1:-15}"
    local elapsed=0

    while (( elapsed < timeout_seconds )); do
        if systemctl is-active --quiet "$SERVICE_NAME"; then
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done

    return 1
}

print_service_failure_diagnostics() {
    echo "    --- ${SERVICE_NAME} status ---"
    systemctl --no-pager --full status "$SERVICE_NAME" 2>/dev/null || true
    echo "    --- recent journal ---"
    journalctl --no-pager -u "$SERVICE_NAME" -n 40 -o cat 2>/dev/null || true
}

# Function to get current version
get_version() {
    # Read version from the pip-installed package in the venv
    if [ -x "$VENV_PYTHON" ]; then
        "$VENV_PYTHON" -c "from importlib.metadata import version; print(version('openhop_repeater'))" 2>/dev/null \
            || echo "not installed"
    else
        # Fallback: try system python for pre-migration installs
        python3 -c "from importlib.metadata import version; print(version('openhop_repeater'))" 2>/dev/null \
            || echo "not installed"
    fi
}

# Function to get service status for display
get_status_display() {
    if ! is_installed; then
        echo "Not Installed"
    elif is_running; then
        echo "Running ($(get_version))"
    else
        echo "Installed but Stopped ($(get_version))"
    fi
}

# Main menu
show_main_menu() {
    local status=$(get_status_display)

    CHOICE=$($DIALOG --backtitle "openHop Repeater Management" --title "openHop Repeater Management" --menu "\nCurrent Status: $status\n\nChoose an action:" 18 70 9 \
        "install" "Install openHop Repeater" \
        "upgrade" "Upgrade existing installation" \
        "reset" "reset existing installation to defaults" \
        "uninstall" "Remove openHop Repeater completely" \
        "config" "Configure radio settings" \
        "start" "Start the service" \
        "stop" "Stop the service" \
        "restart" "Restart the service" \
        "logs" "View live logs" \
        "status" "Show detailed status" \
        "exit" "Exit" 3>&1 1>&2 2>&3)

    case $CHOICE in
        "install")
            if is_installed; then
                show_error "openHop Repeater is already installed!\n\nUse 'upgrade' to update or 'uninstall' first."
            else
                run_locked_action install_repeater || show_error "Installation failed."
            fi
            ;;
        "upgrade")
            if is_installed; then
                run_locked_action upgrade_repeater "false" || show_error "Upgrade failed."
            else
                show_error "openHop Repeater is not installed!\n\nUse 'install' first."
            fi
            ;;
        "reset")
            if is_installed; then
                run_locked_action reset_repeater || show_error "Reset failed."
            else
                show_error "openHop Repeater is not installed!\n\nUse 'install' first."
            fi
            ;;
        "uninstall")
            if has_installation_artifacts; then
                run_locked_action uninstall_repeater || show_error "Uninstall failed."
            else
                show_error "openHop Repeater is not installed."
            fi
            ;;
        "config")
            configure_radio
            ;;
        "start")
            manage_service "start" "false"
            ;;
        "stop")
            manage_service "stop" "false"
            ;;
        "restart")
            manage_service "restart" "false"
            ;;
        "logs")
            print_live_logs_header
            echo ""
            journalctl -u "$SERVICE_NAME" -f -o cat --no-hostname | sed -e 's/.*ERROR.*/\x1b[1;31m&\x1b[0m/' -e 's/.*CRITICAL.*/\x1b[1;41;37m&\x1b[0m/' -e 's/.*WARNING.*/\x1b[1;33m&\x1b[0m/' -e 's/.*INFO.*/\x1b[0;32m&\x1b[0m/' -e 's/.*DEBUG.*/\x1b[0;36m&\x1b[0m/'
            ;;
        "status")
            show_detailed_status
            ;;
        "exit"|"")
            exit 0
            ;;
    esac
}

# Install function
install_repeater() {
    # Check root
    if [ "$EUID" -ne 0 ]; then
        show_error "Installation requires root privileges.\n\nPlease run: sudo $0"
        return 1
    fi

    # Welcome screen (Bypass if the script was passd with the "install" option, assume we want a silent install)
    if [[ "${1:-}" != "install" ]]; then
        $DIALOG --backtitle "openHop Repeater Management" --title "Welcome" --msgbox "\nWelcome to openHop Repeater Setup\n\nThis installer will configure your Linux system as a LoRa mesh network repeater.\n\nPress OK to continue..." 12 70
    fi

    # SPI Check - Universal approach that works on all boards (skip for CH341 USB-SPI adapter)
    SPI_MISSING=0
    USES_CH341=0
    if [ -f "$CONFIG_DIR/config.yaml" ]; then
        if grep -q "radio_type:.*sx1262_ch341" "$CONFIG_DIR/config.yaml" 2>/dev/null; then
            USES_CH341=1
        fi
    fi

    if [ "$USES_CH341" -eq 0 ] && ! ls /dev/spidev* >/dev/null 2>&1; then
        # SPI devices not found, check if we're on a Raspberry Pi and can enable it
        CONFIG_FILE=""
        if [ -f "/boot/firmware/config.txt" ]; then
            CONFIG_FILE="/boot/firmware/config.txt"
        elif [ -f "/boot/config.txt" ]; then
            CONFIG_FILE="/boot/config.txt"
        fi

        if [ -n "$CONFIG_FILE" ]; then
            # Raspberry Pi detected - offer to enable SPI
            if ask_yes_no "SPI Not Enabled" "\nSPI interface is required but not detected (/dev/spidev* not found)!\n\nWould you like to enable it now?\n(This will require a reboot)"; then
                echo "dtparam=spi=on" >> "$CONFIG_FILE"
                show_info "SPI Enabled" "\nSPI has been enabled in $CONFIG_FILE\n\nSystem will reboot now. Please run this script again after reboot."
                reboot
            else
                if ask_yes_no "Continue Without SPI?" "\nSPI is required for LoRa radio operation and is not enabled.\n\nYou can continue the installation, but the radio will not work until SPI is enabled.\n\nContinue anyway?"; then
                    SPI_MISSING=1
                else
                    show_error "SPI is required for LoRa radio operation.\n\nPlease enable SPI manually and run this script again."
                    return
                fi
            fi
        else
            # Not a Raspberry Pi - provide generic instructions
            if ask_yes_no "SPI Not Detected" "\nSPI interface is required but not detected (/dev/spidev* not found).\n\nPlease enable SPI in your system's configuration and ensure the SPI kernel module is loaded.\n\nFor Raspberry Pi: sudo raspi-config -> Interfacing Options -> SPI -> Enable\n\nContinue installation anyway?"; then
                SPI_MISSING=1
            else
                show_error "SPI interface is required but not detected (/dev/spidev* not found).\n\nPlease enable SPI in your system's configuration and ensure the SPI kernel module is loaded.\n\nFor Raspberry Pi: sudo raspi-config -> Interfacing Options -> SPI -> Enable"
                return
            fi
        fi
    fi

    if [ "$SPI_MISSING" -eq 1 ]; then
        show_info "Warning" "\nContinuing without SPI enabled.\n\nLoRa radio will not work until SPI is enabled and /dev/spidev* is available."
    fi

    # Installation progress
    echo ""
    echo "═══════════════════════════════════════════════════════════════"
    echo "        Installing openHop Repeater"
    echo "═══════════════════════════════════════════════════════════════"
    echo ""
    
    echo ">>> Creating service user..."
    if ! id "$SERVICE_USER" &>/dev/null; then
        useradd --system --home "$DATA_DIR" --shell /sbin/nologin "$SERVICE_USER"
    fi
    ensure_service_user_home

    disable_legacy_services

    set +e
    (
    echo "10"; echo "# Adding user to hardware groups..."
    for grp in plugdev dialout gpio i2c spi; do
        getent group "$grp" >/dev/null 2>&1 && usermod -a -G "$grp" "$SERVICE_USER" 2>/dev/null || true
    done

    echo "20"; echo "# Migrating legacy paths..."
    migrate_legacy_paths
    cleanup_stale_source_trees

    echo "23"; echo "# Creating directories..."
    mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$LOG_DIR" "$DATA_DIR"

    echo "25"; echo "# Installing system dependencies..."
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y libffi-dev libusb-1.0-0 sudo jq python3-pip python3-venv python3-rrdtool wget swig build-essential python3-dev i2c-tools
    # Install polkit (package name varies by distro version)
    DEBIAN_FRONTEND=noninteractive apt-get install -y policykit-1 2>/dev/null \
        || DEBIAN_FRONTEND=noninteractive apt-get install -y polkitd pkexec 2>/dev/null \
        || echo "    Warning: Could not install polkit (sudo fallback will be used)"
    echo "28"; echo "# Creating virtual environment..."
    ensure_venv

    # Install setuptools_scm in the dedicated venv to avoid PEP 668 system-pip errors.
    "$VENV_PYTHON" -m pip install -q setuptools_scm || true

    # Install mikefarah yq v4 if not already installed
    if ! command -v yq &> /dev/null || [[ "$(yq --version 2>&1)" != *"mikefarah/yq"* ]]; then
        echo ">>> Installing yq..."
        YQ_VERSION="v4.40.5"
        YQ_BINARY="yq_linux_arm64"
        if [[ "$(uname -m)" == "x86_64" ]]; then
            YQ_BINARY="yq_linux_amd64"
        elif [[ "$(uname -m)" == "armv7"* ]]; then
            YQ_BINARY="yq_linux_arm"
        fi
        wget -qO /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/${YQ_BINARY}" 2>/dev/null && chmod +x /usr/local/bin/yq
    fi

    echo "29"; echo "# Installing files..."
    cp "$SCRIPT_DIR/manage.sh" "$INSTALL_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/openhop-repeater.service" "$INSTALL_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/openhop-plugin-manager.service" "$INSTALL_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/radio-settings.json" "$DATA_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/radio-presets.json" "$DATA_DIR/" 2>/dev/null || true

    echo "45"; echo "# Installing configuration..."
    cp "$SCRIPT_DIR/config.yaml.example" "$CONFIG_DIR/config.yaml.example"
    if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
        cp "$SCRIPT_DIR/config.yaml.example" "$CONFIG_DIR/config.yaml"
    fi

    echo "55"; echo "# Installing systemd service..."
    cp "$SCRIPT_DIR/openhop-repeater.service" /etc/systemd/system/
    if [ -f "$SCRIPT_DIR/openhop-plugin-manager.service" ]; then
        cp "$SCRIPT_DIR/openhop-plugin-manager.service" /etc/systemd/system/openhop-plugin-manager.service
    fi
    systemctl daemon-reload

    echo "58"; echo "# Installing udev rules for CH341..."
    if [ -f "$SCRIPT_DIR/../openhop-core/99-ch341.rules" ]; then
        cp "$SCRIPT_DIR/../openhop-core/99-ch341.rules" /etc/udev/rules.d/99-ch341.rules
        udevadm control --reload-rules 2>/dev/null || true
        udevadm trigger 2>/dev/null || true
    fi

    echo "59"; echo "# Installing udev rules for USB modems..."
    if [ -f "$SCRIPT_DIR/../openhop-core/99-openhop-modem.rules" ]; then
        cp "$SCRIPT_DIR/../openhop-core/99-openhop-modem.rules" /etc/udev/rules.d/99-openhop-modem.rules
        udevadm control --reload-rules 2>/dev/null || true
        udevadm trigger --subsystem-match=tty --action=change 2>/dev/null || true
    fi

    echo "65"; echo "# Setting permissions..."
    # Venv stays root-owned (pip runs as root); service user only needs read+execute
    chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR" "$LOG_DIR" "$DATA_DIR"
    chmod 750 "$CONFIG_DIR" "$LOG_DIR" "$DATA_DIR"
    # Ensure manage.sh and support files in INSTALL_DIR are accessible
    chown root:root "$INSTALL_DIR"
    chmod 755 "$INSTALL_DIR"
    # Ensure the service user can create subdirectories in their home directory
    chmod 755 "$DATA_DIR"
    # Pre-create the .config directory that the service will need
    mkdir -p "$DATA_DIR/.config/openhop_repeater"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR/.config"

    # Configure polkit for passwordless service restart

    # Work out which version of polkit is installed

    POLKIT_VERSION=$(pkaction --version 2>/dev/null | awk '{print $NF}')
    if echo "$POLKIT_VERSION" | awk '{ exit ($1 > 0.105) ? 0 : 1 }'; then
        echo "Polkit 0.106 or greater detected, using rules file"
        echo ">>> Configuring polkit for service management..."
        mkdir -p /etc/polkit-1/rules.d
        cat > /etc/polkit-1/rules.d/10-openhop-repeater.rules <<'EOF'
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") == "openhop-repeater.service" &&
        subject.user == "repeater") {
        return polkit.Result.YES;
    }
});
EOF
        chmod 0644 /etc/polkit-1/rules.d/10-openhop-repeater.rules
    else
        echo "Polkit 0.105 or less detected, using pkla file"
        mkdir -p /etc/polkit-1/localauthority/50-local.d
        cat > /etc/polkit-1/localauthority/50-local.d/10-openhop-repeater.pkla <<'EOF'
[Allow repeater to restart openhop-repeater service]
Identity=unix-user:repeater
Action=org.freedesktop.systemd1.manage-units
ResultAny=yes
ResultInactive=yes
ResultActive=yes
EOF
        chmod 0644 /etc/polkit-1/localauthority/50-local.d/10-openhop-repeater.pkla
    fi

    # Also configure sudoers as fallback for service restart
    echo ">>> Configuring sudoers for service management..."
    mkdir -p /etc/sudoers.d
    cat > /etc/sudoers.d/openhop-repeater <<'EOF'
# Allow repeater user to manage the openhop-repeater service without password
repeater ALL=(root) NOPASSWD: /usr/bin/systemctl restart openhop-repeater, /usr/bin/systemctl stop openhop-repeater, /usr/bin/systemctl start openhop-repeater, /usr/bin/systemctl status openhop-repeater, /usr/local/bin/pymc-do-upgrade
EOF
    chmod 0440 /etc/sudoers.d/openhop-repeater

    echo ">>> Installing OTA upgrade wrapper..."
    cat > /usr/local/bin/pymc-do-upgrade <<'UPGRADEEOF'
#!/bin/bash -p
# Ignore caller shell startup hooks, then run with a clean environment.
exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LANG=C.UTF-8 PIP_CONFIG_FILE=/dev/null /bin/bash --noprofile --norc -s -- "$@" <<'CLEANENV'
set -Eeuo pipefail
umask 022
cd /
[[ "$EUID" -eq 0 ]] || { echo "Root is required" >&2; exit 1; }
[[ "$#" -le 2 ]] || { echo "Too many arguments" >&2; exit 1; }
# Never repair untrusted executable trees by chowning attacker-owned code.
require_root_owned() {
    local path="$1" unsafe
    unsafe=$(/usr/bin/find -L "$path" -maxdepth 0 \( ! -uid 0 -o -perm -022 \) -print) || exit 1
    [[ -z "$unsafe" ]] || { echo "Unsafe root execution path: $path" >&2; exit 1; }
}
for path in / /opt /opt/openhop_repeater /usr /usr/bin /usr/bin/python3; do
    require_root_owned "$path"
done
if [[ -e /opt/openhop_repeater/venv ]]; then
    unsafe=$(/usr/bin/find -L /opt/openhop_repeater/venv \( ! -uid 0 -o -perm -022 \) -print -quit) || exit 1
    [[ -z "$unsafe" ]] || { echo "Unsafe venv; administrator repair required" >&2; exit 1; }
fi
CHANNEL="${1:-main}"
PRETEND_VERSION="${2:-}"
VENV_DIR="/opt/openhop_repeater/venv"
VENV_PYTHON="$VENV_DIR/bin/python"
# Validate: only allow safe git ref characters
if ! [[ "$CHANNEL" =~ ^[a-zA-Z0-9._/-]{1,80}$ ]]; then
    echo "Invalid channel name: $CHANNEL" >&2
    exit 1
fi
if [[ -n "$PRETEND_VERSION" ]] && ! [[ "$PRETEND_VERSION" =~ ^[a-zA-Z0-9][a-zA-Z0-9.!+_-]{0,127}$ ]]; then
    echo "Invalid version" >&2
    exit 1
fi
# If caller supplied a version string, tell setuptools_scm to use it (sudo
# strips env vars so it is passed as a positional argument instead). Scoped to
# this distribution: the unscoped variable applies to every setuptools_scm
# project built in the same pip run, so a dependency built from an sdist
# instead of a wheel would be stamped with our version too.
[ -n "$PRETEND_VERSION" ] && export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OPENHOP_REPEATER="$PRETEND_VERSION"
# ---- Migration: ensure venv exists (handles upgrades from system-pip era) ----
if [ ! -x "$VENV_PYTHON" ]; then
    echo "[pymc-do-upgrade] Creating venv at $VENV_DIR ..."
    /usr/bin/python3 -I -m venv --system-site-packages "$VENV_DIR"
    "$VENV_PYTHON" -I -m pip install --upgrade pip setuptools wheel
fi
# ---- Migration: clean up legacy service unit issues ----
SVC_UNIT=/etc/systemd/system/openhop-repeater.service
if grep -q 'PYTHONPATH' "$SVC_UNIT" 2>/dev/null; then
    sed -i '/^Environment=.*PYTHONPATH/d' "$SVC_UNIT"
    systemctl daemon-reload
fi
if grep -q 'WorkingDirectory=/opt/openhop_repeater' "$SVC_UNIT" 2>/dev/null; then
    sed -i 's|WorkingDirectory=/opt/openhop_repeater|WorkingDirectory=/var/lib/openhop_repeater|' "$SVC_UNIT"
    systemctl daemon-reload
fi
if grep -q 'WorkingDirectory=/opt/pymc_repeater\|WorkingDirectory=/var/lib/pymc_repeater' "$SVC_UNIT" 2>/dev/null; then
    sed -i 's|WorkingDirectory=/opt/pymc_repeater|WorkingDirectory=/var/lib/openhop_repeater|' "$SVC_UNIT"
    sed -i 's|WorkingDirectory=/var/lib/pymc_repeater|WorkingDirectory=/var/lib/openhop_repeater|' "$SVC_UNIT"
    systemctl daemon-reload
fi
if grep -q 'ExecStart=/usr/bin/python3' "$SVC_UNIT" 2>/dev/null; then
    sed -i "s|ExecStart=/usr/bin/python3|ExecStart=$VENV_PYTHON|" "$SVC_UNIT"
    systemctl daemon-reload
fi
if grep -q 'ExecStart=/opt/pymc_repeater/venv/bin/python' "$SVC_UNIT" 2>/dev/null; then
    sed -i "s|ExecStart=/opt/pymc_repeater/venv/bin/python|ExecStart=$VENV_PYTHON|" "$SVC_UNIT"
    systemctl daemon-reload
fi
# ---- Remove stale source trees that shadow the venv package ----
[ -d /opt/openhop_repeater/repeater ] && rm -rf /opt/openhop_repeater/repeater
[ -d /opt/openhop_repeater/openhop-repeater ] && rm -rf /opt/openhop_repeater/openhop-repeater
[ -d /opt/pymc_repeater/repeater ] && rm -rf /opt/pymc_repeater/repeater
[ -d /opt/pymc_repeater/pymc-repeater ] && rm -rf /opt/pymc_repeater/pymc-repeater
# ---- Remove old system-level packages to avoid confusion ----
/usr/bin/python3 -I -m pip uninstall -y openhop_repeater 2>/dev/null || true
/usr/bin/python3 -I -m pip uninstall -y openhop_core 2>/dev/null || true
/usr/bin/python3 -I -m pip uninstall -y pymc_repeater 2>/dev/null || true
/usr/bin/python3 -I -m pip uninstall -y pymc_core 2>/dev/null || true
# ---- Try R2 wheels first for faster OTA upgrades ----
R2_BASE_URL="https://wheel.pymc.dev/pymc_build_deps"
MACHINE_ARCH=$(uname -m)
case "$MACHINE_ARCH" in
    aarch64) ARCH_TAG="arm64"; PLATFORM_TAG="aarch64" ;;
    armv7l|armv7) ARCH_TAG="armv7"; PLATFORM_TAG="armv7l" ;;
    x86_64) ARCH_TAG="x86_64"; PLATFORM_TAG="x86_64" ;;
    *) ARCH_TAG=""; PLATFORM_TAG="" ;;
esac
if [ -n "$ARCH_TAG" ]; then
    PY_TAG=$("$VENV_PYTHON" -I -c 'import sys; v=f"cp{sys.version_info.major}{sys.version_info.minor}"; print(f"{v}-{v}")' 2>/dev/null || echo "cp311-cp311")
    WHEEL_BASE="${R2_BASE_URL}/${ARCH_TAG}/${PLATFORM_TAG}/${PY_TAG}"
    echo "[pymc-do-upgrade] Trying dependencies from R2 wheels..."
    "$VENV_PYTHON" -I -m pip install --find-links "${WHEEL_BASE}/index.html" --no-cache-dir "pycryptodome>=3.23.0" "PyNaCl>=1.5.0" cffi "pyyaml>=6.0.0" 2>/dev/null || true
fi
# ---- Install openhop_repeater from git ----
if "$VENV_PYTHON" -I -m pip install \
    --upgrade \
    --no-cache-dir \
    "openhop_repeater[hardware] @ git+https://github.com/openhop-dev/openhop_repeater.git@${CHANNEL}"; then
    # Use the just-installed distribution, never a caller checkout or cwd.
    PLUGIN_UNIT=$("$VENV_PYTHON" -I -c 'from importlib.metadata import distribution; print(distribution("openhop_repeater").locate_file("repeater/plugins/openhop-plugin-manager.service"))')
    if [[ -f "$PLUGIN_UNIT" ]]; then
        require_root_owned "$PLUGIN_UNIT"
        install -o root -g root -m 0644 "$PLUGIN_UNIT" /etc/systemd/system/openhop-plugin-manager.service
        systemctl daemon-reload
        systemctl enable openhop-plugin-manager
        # ExecCondition skips startup successfully when plugins.enabled is false.
        systemctl restart openhop-plugin-manager || echo "[pymc-do-upgrade] WARNING: inspect openhop-plugin-manager service status" >&2
    else
        echo "[pymc-do-upgrade] WARNING: selected package does not ship a plugin-manager unit; older channels may not support plugins. Re-run manage.sh upgrade from a plugin-capable release if unexpected." >&2
    fi
    # Keep web/OTA updates aligned with manage.sh install/upgrade defaults.
    RADIO_BASE_URL="https://raw.githubusercontent.com/openhop-dev/openhop_repeater/${CHANNEL}"
    RADIO_STORAGE_DIR="/var/lib/openhop_repeater"
    mkdir -p "$RADIO_STORAGE_DIR"
    wget -qO "$RADIO_STORAGE_DIR/radio-settings.json" "${RADIO_BASE_URL}/radio-settings.json" 2>/dev/null || true
    wget -qO "$RADIO_STORAGE_DIR/radio-presets.json" "${RADIO_BASE_URL}/radio-presets.json" 2>/dev/null || true
else
    exit 1
fi
CLEANENV
UPGRADEEOF
    chown root:root /usr/local/bin/pymc-do-upgrade
    chmod 0755 /usr/local/bin/pymc-do-upgrade

    echo "75"; echo "# Starting service..."
    systemctl enable "$SERVICE_NAME"
    if [ -f /etc/systemd/system/openhop-plugin-manager.service ]; then
        systemctl enable openhop-plugin-manager 2>/dev/null || true
    fi

    echo "90"; echo "# Installation files complete..."
    ) | "$DIALOG" --backtitle "openHop Repeater Management" --title "Installing" --gauge "Setting up openHop Repeater..." 8 70 0
    local pipeline_status=("${PIPESTATUS[@]}")
    set -e

    if (( pipeline_status[0] != 0 )); then
        error "Installation setup failed with status ${pipeline_status[0]}"
        return "${pipeline_status[0]}"
    fi

    if (( pipeline_status[1] != 0 )); then
        error "Installation progress dialog failed with status ${pipeline_status[1]}"
        return "${pipeline_status[1]}"
    fi

    # Install Python package outside of progress gauge for better error handling
    echo "=== Installing Python Dependencies ==="
    echo ""
    echo "Installing openhop_repeater and dependencies (including openhop_core from PyPI)..."
    echo "This may take a few minutes..."
    echo ""

    local requested_ref="${OPENHOP_UPGRADE_REF:-$DEFAULT_UPGRADE_REF}"
    if ! is_safe_git_ref "$requested_ref"; then
        error "Invalid upgrade ref: $requested_ref"
        return 1
    fi

    local package_source
    package_source="$(determine_package_source "$SCRIPT_DIR" "$requested_ref")"
    log_package_source "$package_source"

    # Only inspect git metadata when this directory is a checkout of the expected repo.
    if is_expected_repo_checkout "$SCRIPT_DIR"; then
        if GIT_VERSION="$(python3 -m setuptools_scm 2>/dev/null)"; then
            export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OPENHOP_REPEATER="$GIT_VERSION"
            echo "Installing version: $GIT_VERSION"
        fi
    fi
    # We don't have any binary wheels available for these on a LuckFox, so we need to ignore them on that platform.
    if ! grep -q "Luckfox Pico" /proc/device-tree/model 2>/dev/null; then
        # Force binary wheels for slow-to-compile packages (much faster on Raspberry Pi)
        export PIP_ONLY_BINARY=pycryptodome,cffi,PyNaCl,psutil
    fi
    echo "Note: Using optimized binary wheels for faster installation"
    echo ""

    # Ensure venv exists
    ensure_venv

    echo "Installing openhop_repeater into venv ($VENV_DIR)..."
    
    # Attempt R2 wheels first for faster installation
    if [ "$R2_ENABLED" -eq 1 ]; then
        MACHINE_ARCH=$(uname -m)
        case "$MACHINE_ARCH" in
            aarch64) ARCH_TAG="arm64"; PLATFORM_TAG="aarch64" ;;
            armv7l|armv7) ARCH_TAG="armv7"; PLATFORM_TAG="armv7l" ;;
            x86_64) ARCH_TAG="x86_64"; PLATFORM_TAG="x86_64" ;;
            *) ARCH_TAG=""; PLATFORM_TAG="" ;;
        esac
        if [ -n "$ARCH_TAG" ]; then
            PY_TAG=$("$VENV_PYTHON" -c 'import sys; v=f"cp{sys.version_info.major}{sys.version_info.minor}"; print(f"{v}-{v}")' 2>/dev/null || echo "cp311-cp311")
            WHEEL_BASE="${R2_BASE_URL}/${ARCH_TAG}/${PLATFORM_TAG}/${PY_TAG}"
            echo "  Checking for R2 wheels (${ARCH_TAG}/${PLATFORM_TAG}/${PY_TAG})..."
            echo "  Trying install from R2 pre-built wheels..."
            "$VENV_PYTHON" -m pip install --find-links "${WHEEL_BASE}/index.html" --no-cache-dir "pycryptodome>=3.23.0" "PyNaCl>=1.5.0" cffi "pyyaml>=6.0.0" 2>/dev/null && R2_SUCCESS=1 || R2_SUCCESS=0
            if [ "$R2_SUCCESS" -eq 1 ]; then
                echo "  ✓ R2 wheels installed"
            else
                echo "  - R2 wheels unavailable for this platform/tag, falling back"
            fi
        fi
    fi
    
    local package_requirement
    package_requirement="$(build_package_requirement "$package_source")"
    if ! "$VENV_PYTHON" -m pip install --upgrade --no-cache-dir "$package_requirement"; then
        error "Python package installation failed"
        return 1
    fi
    echo ""
    echo "✓ Python package installation completed successfully!"

    # Reload systemd and start the service
    if ! systemctl daemon-reload; then
        error "Failed to reload systemd units"
        return 1
    fi
    if ! systemctl start "$SERVICE_NAME"; then
        error "Failed to start ${SERVICE_NAME}"
        return 1
    fi
    if [ -f /etc/systemd/system/openhop-plugin-manager.service ]; then
        systemctl enable openhop-plugin-manager >/dev/null 2>&1 || true
        systemctl start openhop-plugin-manager >/dev/null 2>&1 || true
    fi

    # Show final results
    sleep 2
    local ip_address=$(hostname -I | awk '{print $1}')
    if is_running; then
        echo "═══════════════════════════════════════════════════════════════"
        echo "        ✓ Installation Completed Successfully!"
        echo "═══════════════════════════════════════════════════════════════"
        echo ""
        echo "Service is running on:"
        echo "  → http://$ip_address:8000"
        echo ""
        echo "═══════════════════════════════════════════════════════════════"
        echo "        NEXT STEP: Complete Web Setup Wizard"
        echo "═══════════════════════════════════════════════════════════════"
        echo ""
        echo "Open the web dashboard in your browser to complete setup:"
        echo ""
        echo "  1. Navigate to: http://$ip_address:8000"
        echo "  2. Complete the 5-step setup wizard:"
        echo "     • Choose repeater name"
        echo "     • Select hardware board"
        echo "     • Configure radio settings"
        echo "     • Set admin password"
        echo "  3. Log in to your configured repeater"
        echo ""
        # Container detection: warn about host-side udev rules
        if [ -f /run/host/container-manager ] || [ -n "${container:-}" ] || grep -qsai 'container=' /proc/1/environ 2>/dev/null || [ -f /.dockerenv ]; then
            echo "═══════════════════════════════════════════════════════════════"
            echo "        ⚠  CONTAINER ENVIRONMENT DETECTED"
            echo "═══════════════════════════════════════════════════════════════"
            echo ""
            echo "  USB device udev rules do NOT work inside containers."
            echo "  You MUST install the CH341 udev rule on the HOST machine:"
            echo ""
            echo "    echo 'SUBSYSTEM==\"usb\", ATTR{idVendor}==\"1a86\", ATTR{idProduct}==\"5512\", MODE=\"0666\"' \\"
            echo "      | sudo tee /etc/udev/rules.d/99-ch341.rules"
            echo "    sudo udevadm control --reload-rules"
            echo "    sudo udevadm trigger --subsystem-match=usb --action=change"
            echo ""
            echo "  Then unplug and replug the CH341 USB adapter."
            echo ""
        fi
        echo "═══════════════════════════════════════════════════════════════"
        echo ""
        if [[ "${1:-}" != "install" ]]; then #Headless install support
            read -p "Press Enter to return to main menu..." || true
        fi
    else
        show_error "Installation completed but service failed to start!\n\nCheck logs from the main menu for details."
        return 1
    fi

    return 0
}

# Reset function
reset_repeater() {
    local config_file="$CONFIG_DIR/config.yaml"
    local updated_example="$CONFIG_DIR/config.yaml.example"

    if [ "$EUID" -ne 0 ]; then
        show_error "Upgrade requires root privileges.\n\nPlease run: sudo $0"
        return 1
    fi

    local current_version=$(get_version)

    if ask_yes_no "Confirm Reset" "Reset openHop Repeater to default configuration?\n\nContinue?"; then

        # Show info that upgrade is starting
        show_info "Reseting" "Starting reset process...\n\nProgress will be shown in the terminal."

        echo "=== Reset Progress ==="
        echo "[1/4] Stopping service..."
        systemctl stop "$SERVICE_NAME" 2>/dev/null || true

        echo "[2/4] Backing up configuration..."
        if [ -d "$CONFIG_DIR" ]; then
            cp -r "$CONFIG_DIR" "$CONFIG_DIR.backup.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
            echo "    ✓ Configuration backed up"
        fi
    echo "3/4 Restore default config.yaml from config.yaml.example"
    cp "$updated_example" "$config_file"
	sleep 5
        # Reload systemd and start the service
	echo "4/4 Restart the service"
        systemctl daemon-reload
        systemctl start "$SERVICE_NAME"
        # Show final results
        sleep 2
        local ip_address=$(hostname -I | awk '{print $1}')
        if is_running; then
            echo "═══════════════════════════════════════════════════════════════"
            echo "        ✓ Reset Completed Successfully!"
            echo "═══════════════════════════════════════════════════════════════"
            echo ""
            echo "Service is running on:"
            echo "  → http://$ip_address:8000"
            echo ""
            echo "═══════════════════════════════════════════════════════════════"
            echo "        NEXT STEP: Complete Web Setup Wizard"
            echo "═══════════════════════════════════════════════════════════════"
            echo ""
            echo "Open the web dashboard in your browser to complete setup:"
            echo ""
            echo "  1. Navigate to: http://$ip_address:8000"
            echo "  2. Complete the 5-step setup wizard:"
            echo "     • Choose repeater name"
            echo "     • Select hardware board"
            echo "     • Configure radio settings"
            echo "     • Set admin password"
            echo "  3. Log in to your configured repeater"
            echo ""
            echo "═══════════════════════════════════════════════════════════════"
            echo ""
            read -p "Press Enter to return to main menu..." || true
        else
            show_error "Installation completed but service failed to start!\n\nCheck logs from the main menu for details."
            return 1
        fi
    fi
    return 0
}

# Upgrade function
upgrade_repeater() {
    local silent="${1:-false}"
    local requested_ref="${OPENHOP_UPGRADE_REF:-$DEFAULT_UPGRADE_REF}"
    local package_source=""
    local package_requirement=""
    local service_was_running=0
    local service_was_enabled=0
    local service_stopped=0

    restart_service_after_failure() {
        if (( service_stopped == 1 )) && (( service_was_running == 1 || service_was_enabled == 1 )); then
            error "Upgrade failed after stopping service; attempting best-effort restart"
            systemctl start "$SERVICE_NAME" >/dev/null 2>&1 || true
        fi
    }

    if [ "$EUID" -ne 0 ]; then
        if [[ "$silent" == "true" ]]; then
            echo "Upgrade requires root privileges. Please run: sudo $0 upgrade"
        else
            show_error "Upgrade requires root privileges.\n\nPlease run: sudo $0"
        fi
        return 1
    fi

    local current_version=$(get_version)

    if [[ "$silent" != "true" ]]; then
        if ! ask_yes_no "Confirm Upgrade" "Current version: $current_version\n\nThis will upgrade openHop Repeater while preserving your configuration.\n\nContinue?"; then
            return 0
        fi

        # Show info that upgrade is starting
        show_info "Upgrading" "Starting upgrade process...\n\nThis may take a few minutes.\nProgress will be shown in the terminal."
    else
        echo "Starting upgrade process..."
        echo "Current version: $current_version"
    fi

    if ! is_safe_git_ref "$requested_ref"; then
        error "Invalid upgrade ref: $requested_ref"
        return 1
    fi

    refresh_upgrade_checkout "$silent" || return 1

    package_source="$(determine_package_source "$SCRIPT_DIR" "$requested_ref")"
    package_requirement="$(build_package_requirement "$package_source")"
    log_package_source "$package_source"

    if is_running; then
        service_was_running=1
    fi
    if is_enabled; then
        service_was_enabled=1
    fi

    if is_expected_repo_checkout "$SCRIPT_DIR"; then
        if GIT_VERSION="$(python3 -m setuptools_scm 2>/dev/null)"; then
            export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OPENHOP_REPEATER="$GIT_VERSION"
            echo "Upgrading to version: $GIT_VERSION"
        fi
    fi

    echo "=== Upgrade Progress ==="
    echo "[1/9] Migrating legacy paths..."
    migrate_legacy_paths
    cleanup_stale_source_trees
    disable_legacy_services

    echo "[2/9] Ensuring required directories..."
    mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$LOG_DIR" "$DATA_DIR"
    ensure_service_user_home

    echo "[3/9] Backing up configuration..."
    if [ -d "$CONFIG_DIR" ]; then
        cp -r "$CONFIG_DIR" "$CONFIG_DIR.backup.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
        echo "    ✓ Configuration backed up"
    fi

    echo "[4/9] Updating system dependencies..."
    apt-get update -qq
    apt-get install -y libffi-dev libusb-1.0-0 sudo jq python3-pip python3-venv python3-rrdtool wget swig build-essential python3-dev i2c-tools
    apt-get install -y policykit-1 2>/dev/null \
        || apt-get install -y polkitd pkexec 2>/dev/null \
        || echo "    Warning: Could not install polkit (sudo fallback will be used)"

    # Keep setuptools_scm in the venv instead of system Python (PEP 668 safe).
    ensure_venv
    if ! "$VENV_PYTHON" -m pip install -q setuptools_scm; then
        echo "    Warning: Could not install setuptools_scm in venv; continuing without git-derived version"
    fi

    if ! command -v yq >/dev/null 2>&1 || [[ "$(yq --version 2>&1)" != *"mikefarah/yq"* ]]; then
        YQ_VERSION="v4.40.5"
        YQ_BINARY="yq_linux_arm64"
        if [[ "$(uname -m)" == "x86_64" ]]; then
            YQ_BINARY="yq_linux_amd64"
        elif [[ "$(uname -m)" == "armv7"* ]]; then
            YQ_BINARY="yq_linux_arm"
        fi
        wget -qO /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/${YQ_BINARY}" && chmod +x /usr/local/bin/yq
    fi
    echo "    ✓ Dependencies updated"

    echo "[5/9] Installing files..."
    if ! cp "$SCRIPT_DIR/openhop-repeater.service" /etc/systemd/system/; then
        echo "    ⚠ Warning: Failed to update service file – old service file may remain"
    fi
    if [ -f "$SCRIPT_DIR/openhop-plugin-manager.service" ]; then
        if cp "$SCRIPT_DIR/openhop-plugin-manager.service" /etc/systemd/system/openhop-plugin-manager.service; then
            echo "    ✓ openhop-plugin-manager.service installed"
        else
            echo "    ⚠ Warning: Failed to install openhop-plugin-manager.service"
        fi
    else
        echo "    ⚠ openhop-plugin-manager.service not found in $SCRIPT_DIR (skipped)"
    fi
    cp "$SCRIPT_DIR/radio-settings.json" "$DATA_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/radio-presets.json" "$DATA_DIR/" 2>/dev/null || true
    echo "    ✓ Files updated"

    echo "[6/9] Validating and updating configuration..."
    if validate_and_update_config; then
        echo "    ✓ Configuration validated and updated"
    else
        echo "    ⚠ Configuration validation failed, keeping existing config"
    fi

    echo "[6.5/9] Ensuring user groups and udev rules..."
    for grp in plugdev dialout gpio i2c spi; do
        getent group "$grp" >/dev/null 2>&1 && usermod -a -G "$grp" "$SERVICE_USER" 2>/dev/null || true
    done
    if [ -f "$SCRIPT_DIR/../openhop-core/99-ch341.rules" ]; then
        cp "$SCRIPT_DIR/../openhop-core/99-ch341.rules" /etc/udev/rules.d/99-ch341.rules
        udevadm control --reload-rules 2>/dev/null || true
        udevadm trigger 2>/dev/null || true
        echo "    ✓ CH341 udev rules updated"
    elif [ -f /etc/udev/rules.d/99-ch341.rules ]; then
        echo "    ✓ CH341 udev rules already present"
    fi
    if [ -f "$SCRIPT_DIR/../openhop-core/99-openhop-modem.rules" ]; then
        cp "$SCRIPT_DIR/../openhop-core/99-openhop-modem.rules" /etc/udev/rules.d/99-openhop-modem.rules
        udevadm control --reload-rules 2>/dev/null || true
        udevadm trigger --subsystem-match=tty --action=change 2>/dev/null || true
        echo "    ✓ USB modem udev rules updated"
    elif [ -f /etc/udev/rules.d/99-openhop-modem.rules ]; then
        echo "    ✓ USB modem udev rules already present"
    fi
    echo "    ✓ User groups updated"

    echo "[7/9] Fixing permissions and helper files..."
    chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR" "$LOG_DIR" "$DATA_DIR" 2>/dev/null || true
    chown root:root "$INSTALL_DIR" 2>/dev/null || true
    chmod 755 "$INSTALL_DIR" 2>/dev/null || true
    chmod 750 "$CONFIG_DIR" "$LOG_DIR" 2>/dev/null || true
    chmod 755 "$DATA_DIR" 2>/dev/null || true

    mkdir -p "$DATA_DIR/.config/openhop_repeater" 2>/dev/null || true
    chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR/.config" 2>/dev/null || true

    POLKIT_VERSION=$(pkaction --version 2>/dev/null | awk '{print $NF}')
    if echo "$POLKIT_VERSION" | awk '{ exit ($1 > 0.105) ? 0 : 1 }'; then
        echo "Polkit 0.106 or greater detected, using rules file"
        echo ">>> Configuring polkit for service management..."
        mkdir -p /etc/polkit-1/rules.d
        cat > /etc/polkit-1/rules.d/10-openhop-repeater.rules <<'EOF'
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") == "openhop-repeater.service" &&
        subject.user == "repeater") {
        return polkit.Result.YES;
    }
});
EOF
        chmod 0644 /etc/polkit-1/rules.d/10-openhop-repeater.rules
    else
        echo "Polkit 0.105 or less detected, using pkla file"
        mkdir -p /etc/polkit-1/localauthority/50-local.d
        cat > /etc/polkit-1/localauthority/50-local.d/10-openhop-repeater.pkla <<'EOF'
[Allow repeater to restart openhop-repeater service]
Identity=unix-user:repeater
Action=org.freedesktop.systemd1.manage-units
ResultAny=yes
ResultInactive=yes
ResultActive=yes
EOF
    chmod 0644 /etc/polkit-1/localauthority/50-local.d/10-openhop-repeater.pkla
    fi

    mkdir -p /etc/sudoers.d
    cat > /etc/sudoers.d/openhop-repeater <<'EOF'
# Allow repeater user to manage the openhop-repeater service without password
repeater ALL=(root) NOPASSWD: /usr/bin/systemctl restart openhop-repeater, /usr/bin/systemctl stop openhop-repeater, /usr/bin/systemctl start openhop-repeater, /usr/bin/systemctl status openhop-repeater, /usr/local/bin/pymc-do-upgrade
EOF
    chmod 0440 /etc/sudoers.d/openhop-repeater

    cat > /usr/local/bin/pymc-do-upgrade <<'UPGRADEEOF'
#!/bin/bash -p
# Ignore caller shell startup hooks, then run with a clean environment.
exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LANG=C.UTF-8 PIP_CONFIG_FILE=/dev/null /bin/bash --noprofile --norc -s -- "$@" <<'CLEANENV'
set -Eeuo pipefail
umask 022
cd /
[[ "$EUID" -eq 0 ]] || { echo "Root is required" >&2; exit 1; }
[[ "$#" -le 2 ]] || { echo "Too many arguments" >&2; exit 1; }
# Never repair untrusted executable trees by chowning attacker-owned code.
require_root_owned() {
    local path="$1" unsafe
    unsafe=$(/usr/bin/find -L "$path" -maxdepth 0 \( ! -uid 0 -o -perm -022 \) -print) || exit 1
    [[ -z "$unsafe" ]] || { echo "Unsafe root execution path: $path" >&2; exit 1; }
}
for path in / /opt /opt/openhop_repeater /usr /usr/bin /usr/bin/python3; do
    require_root_owned "$path"
done
if [[ -e /opt/openhop_repeater/venv ]]; then
    unsafe=$(/usr/bin/find -L /opt/openhop_repeater/venv \( ! -uid 0 -o -perm -022 \) -print -quit) || exit 1
    [[ -z "$unsafe" ]] || { echo "Unsafe venv; administrator repair required" >&2; exit 1; }
fi
CHANNEL="${1:-main}"
PRETEND_VERSION="${2:-}"
VENV_DIR="/opt/openhop_repeater/venv"
VENV_PYTHON="$VENV_DIR/bin/python"
# Validate: only allow safe git ref characters
if ! [[ "$CHANNEL" =~ ^[a-zA-Z0-9._/-]{1,80}$ ]]; then
    echo "Invalid channel name: $CHANNEL" >&2
    exit 1
fi
if [[ -n "$PRETEND_VERSION" ]] && ! [[ "$PRETEND_VERSION" =~ ^[a-zA-Z0-9][a-zA-Z0-9.!+_-]{0,127}$ ]]; then
    echo "Invalid version" >&2
    exit 1
fi
# If caller supplied a version string, tell setuptools_scm to use it (sudo
# strips env vars so it is passed as a positional argument instead). Scoped to
# this distribution: the unscoped variable applies to every setuptools_scm
# project built in the same pip run, so a dependency built from an sdist
# instead of a wheel would be stamped with our version too.
[ -n "$PRETEND_VERSION" ] && export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OPENHOP_REPEATER="$PRETEND_VERSION"
# ---- Migration: ensure venv exists (handles upgrades from system-pip era) ----
if [ ! -x "$VENV_PYTHON" ]; then
    echo "[pymc-do-upgrade] Creating venv at $VENV_DIR ..."
    /usr/bin/python3 -I -m venv --system-site-packages "$VENV_DIR"
    "$VENV_PYTHON" -I -m pip install --upgrade pip setuptools wheel
fi
# ---- Migration: clean up legacy service unit issues ----
SVC_UNIT=/etc/systemd/system/openhop-repeater.service
if grep -q 'PYTHONPATH' "$SVC_UNIT" 2>/dev/null; then
    sed -i '/^Environment=.*PYTHONPATH/d' "$SVC_UNIT"
    systemctl daemon-reload
fi
if grep -q 'WorkingDirectory=/opt/openhop_repeater' "$SVC_UNIT" 2>/dev/null; then
    sed -i 's|WorkingDirectory=/opt/openhop_repeater|WorkingDirectory=/var/lib/openhop_repeater|' "$SVC_UNIT"
    systemctl daemon-reload
fi
if grep -q 'WorkingDirectory=/opt/pymc_repeater\|WorkingDirectory=/var/lib/pymc_repeater' "$SVC_UNIT" 2>/dev/null; then
    sed -i 's|WorkingDirectory=/opt/pymc_repeater|WorkingDirectory=/var/lib/openhop_repeater|' "$SVC_UNIT"
    sed -i 's|WorkingDirectory=/var/lib/pymc_repeater|WorkingDirectory=/var/lib/openhop_repeater|' "$SVC_UNIT"
    systemctl daemon-reload
fi
if grep -q 'ExecStart=/usr/bin/python3' "$SVC_UNIT" 2>/dev/null; then
    sed -i "s|ExecStart=/usr/bin/python3|ExecStart=$VENV_PYTHON|" "$SVC_UNIT"
    systemctl daemon-reload
fi
if grep -q 'ExecStart=/opt/pymc_repeater/venv/bin/python' "$SVC_UNIT" 2>/dev/null; then
    sed -i "s|ExecStart=/opt/pymc_repeater/venv/bin/python|ExecStart=$VENV_PYTHON|" "$SVC_UNIT"
    systemctl daemon-reload
fi
# ---- Remove stale source trees that shadow the venv package ----
[ -d /opt/openhop_repeater/repeater ] && rm -rf /opt/openhop_repeater/repeater
[ -d /opt/openhop_repeater/openhop-repeater ] && rm -rf /opt/openhop_repeater/openhop-repeater
[ -d /opt/pymc_repeater/repeater ] && rm -rf /opt/pymc_repeater/repeater
[ -d /opt/pymc_repeater/pymc-repeater ] && rm -rf /opt/pymc_repeater/pymc-repeater
# ---- Remove old system-level packages to avoid confusion ----
/usr/bin/python3 -I -m pip uninstall -y openhop_repeater 2>/dev/null || true
/usr/bin/python3 -I -m pip uninstall -y openhop_core 2>/dev/null || true
/usr/bin/python3 -I -m pip uninstall -y pymc_repeater 2>/dev/null || true
/usr/bin/python3 -I -m pip uninstall -y pymc_core 2>/dev/null || true
# ---- Try R2 wheels first for faster OTA upgrades ----
R2_BASE_URL="https://wheel.pymc.dev/pymc_build_deps"
MACHINE_ARCH=$(uname -m)
case "$MACHINE_ARCH" in
    aarch64) ARCH_TAG="arm64"; PLATFORM_TAG="aarch64" ;;
    armv7l|armv7) ARCH_TAG="armv7"; PLATFORM_TAG="armv7l" ;;
    x86_64) ARCH_TAG="x86_64"; PLATFORM_TAG="x86_64" ;;
    *) ARCH_TAG=""; PLATFORM_TAG="" ;;
esac
if [ -n "$ARCH_TAG" ]; then
    PY_TAG=$("$VENV_PYTHON" -I -c 'import sys; v=f"cp{sys.version_info.major}{sys.version_info.minor}"; print(f"{v}-{v}")' 2>/dev/null || echo "cp311-cp311")
    WHEEL_BASE="${R2_BASE_URL}/${ARCH_TAG}/${PLATFORM_TAG}/${PY_TAG}"
    echo "[pymc-do-upgrade] Trying dependencies from R2 wheels..."
    "$VENV_PYTHON" -I -m pip install --find-links "${WHEEL_BASE}/index.html" --no-cache-dir "pycryptodome>=3.23.0" "PyNaCl>=1.5.0" cffi "pyyaml>=6.0.0" 2>/dev/null || true
fi
# ---- Install openhop_repeater from git ----
if "$VENV_PYTHON" -I -m pip install \
    --upgrade \
    --no-cache-dir \
    "openhop_repeater[hardware] @ git+https://github.com/openhop-dev/openhop_repeater.git@${CHANNEL}"; then
    # Use the just-installed distribution, never a caller checkout or cwd.
    PLUGIN_UNIT=$("$VENV_PYTHON" -I -c 'from importlib.metadata import distribution; print(distribution("openhop_repeater").locate_file("repeater/plugins/openhop-plugin-manager.service"))')
    if [[ -f "$PLUGIN_UNIT" ]]; then
        require_root_owned "$PLUGIN_UNIT"
        install -o root -g root -m 0644 "$PLUGIN_UNIT" /etc/systemd/system/openhop-plugin-manager.service
        systemctl daemon-reload
        systemctl enable openhop-plugin-manager
        # ExecCondition skips startup successfully when plugins.enabled is false.
        systemctl restart openhop-plugin-manager || echo "[pymc-do-upgrade] WARNING: inspect openhop-plugin-manager service status" >&2
    else
        echo "[pymc-do-upgrade] WARNING: selected package does not ship a plugin-manager unit; older channels may not support plugins. Re-run manage.sh upgrade from a plugin-capable release if unexpected." >&2
    fi
    # Keep web/OTA updates aligned with manage.sh install/upgrade defaults.
    RADIO_BASE_URL="https://raw.githubusercontent.com/openhop-dev/openhop_repeater/${CHANNEL}"
    RADIO_STORAGE_DIR="/var/lib/openhop_repeater"
    mkdir -p "$RADIO_STORAGE_DIR"
    wget -qO "$RADIO_STORAGE_DIR/radio-settings.json" "${RADIO_BASE_URL}/radio-settings.json" 2>/dev/null || true
    wget -qO "$RADIO_STORAGE_DIR/radio-presets.json" "${RADIO_BASE_URL}/radio-presets.json" 2>/dev/null || true
else
    exit 1
fi
CLEANENV
UPGRADEEOF
    chown root:root /usr/local/bin/pymc-do-upgrade
    chmod 0755 /usr/local/bin/pymc-do-upgrade
    echo "    ✓ Permissions updated"

    echo "[8/9] Installing Python Dependencies..."

    if ! grep -q "Luckfox Pico" /proc/device-tree/model 2>/dev/null; then
        export PIP_ONLY_BINARY=pycryptodome,cffi,PyNaCl,psutil
    fi
    echo "Note: Using optimized binary wheels for faster installation"
    echo ""

    if ! migrate_to_venv; then
        error "Failed to migrate installation to venv"
        return 1
    fi

    echo "Upgrading openhop_repeater into venv ($VENV_DIR)..."
    if [ "$R2_ENABLED" -eq 1 ]; then
        MACHINE_ARCH=$(uname -m)
        case "$MACHINE_ARCH" in
            aarch64) ARCH_TAG="arm64"; PLATFORM_TAG="aarch64" ;;
            armv7l|armv7) ARCH_TAG="armv7"; PLATFORM_TAG="armv7l" ;;
            x86_64) ARCH_TAG="x86_64"; PLATFORM_TAG="x86_64" ;;
            *) ARCH_TAG=""; PLATFORM_TAG="" ;;
        esac
        if [ -n "$ARCH_TAG" ]; then
            PY_TAG=$("$VENV_PYTHON" -c 'import sys; v=f"cp{sys.version_info.major}{sys.version_info.minor}"; print(f"{v}-{v}")' 2>/dev/null || echo "cp311-cp311")
            WHEEL_BASE="${R2_BASE_URL}/${ARCH_TAG}/${PLATFORM_TAG}/${PY_TAG}"
            echo "  Checking for R2 wheels (${ARCH_TAG}/${PLATFORM_TAG}/${PY_TAG})..."
            echo "  Trying install from R2 pre-built wheels..."
            if "$VENV_PYTHON" -m pip install --find-links "${WHEEL_BASE}/index.html" --no-cache-dir "pycryptodome>=3.23.0" "PyNaCl>=1.5.0" cffi "pyyaml>=6.0.0" 2>/dev/null; then
                echo "  ✓ R2 wheels installed"
            else
                echo "  - R2 wheels unavailable for this platform/tag, falling back"
            fi
        fi
    fi

    if ! "$VENV_PYTHON" -m pip install --upgrade --no-cache-dir "$package_requirement"; then
        error "Package upgrade failed"
        return 1
    fi
    echo ""
    echo "✓ Package and dependencies upgraded successfully!"

    echo "[9/9] Reloading systemd and restarting service..."
    if ! systemctl daemon-reload; then
        error "Failed to reload systemd"
        return 1
    fi

    if (( service_was_running == 1 || service_was_enabled == 1 )); then
        if ! systemctl stop "$SERVICE_NAME" 2>/dev/null; then
            error "Failed to stop ${SERVICE_NAME} before restart"
            return 1
        fi
        service_stopped=1

        if ! systemctl restart "$SERVICE_NAME"; then
            restart_service_after_failure
            error "Failed to restart ${SERVICE_NAME}"
            print_service_failure_diagnostics
            return 1
        fi

        if ! wait_for_service_active 15; then
            restart_service_after_failure
            error "Service restart completed but ${SERVICE_NAME} is not active"
            print_service_failure_diagnostics
            return 1
        fi
        echo "    ✓ Service restarted and active"
    else
        echo "    ✓ Service was not running/enabled before upgrade; no restart required"
    fi

    # Plugin manager is optional but should be installed/enabled on upgrade when present.
    # Failures here must not fail the Repeater upgrade.
    if [ -f /etc/systemd/system/openhop-plugin-manager.service ]; then
        if systemctl enable openhop-plugin-manager >/dev/null 2>&1; then
            echo "    ✓ openhop-plugin-manager enabled"
        else
            echo "    ⚠ Could not enable openhop-plugin-manager"
        fi
        if systemctl restart openhop-plugin-manager >/dev/null 2>&1; then
            echo "    ✓ openhop-plugin-manager restarted"
        elif systemctl start openhop-plugin-manager >/dev/null 2>&1; then
            echo "    ✓ openhop-plugin-manager started"
        else
            echo "    ⚠ openhop-plugin-manager installed but failed to start (Repeater still OK)"
            systemctl status openhop-plugin-manager --no-pager -l 2>/dev/null | head -20 || true
        fi
    fi

    local new_version
    new_version=$(get_version)

    local container_note=""
    if [ -f /run/host/container-manager ] || [ -n "${container:-}" ] || grep -qsai 'container=' /proc/1/environ 2>/dev/null || [ -f /.dockerenv ]; then
        container_note="\n\n⚠ CONTAINER DETECTED:\nUSB udev rules must be set on the HOST, not here.\nCH341 host-side setup: https://docs.openhop.dev/projects/openhop-repeater/hardware-setup/#ch341-usb-spi-hosts"
    fi

    if [[ "$silent" == "true" ]]; then
        echo "Upgrade completed successfully!"
        echo "Version: $current_version -> $new_version"
        if (( service_was_running == 1 || service_was_enabled == 1 )); then
            echo "✓ Service is running"
        fi
        echo "✓ Configuration preserved"
        if [[ -n "$container_note" ]]; then
            echo "$container_note"
        fi
    else
        show_info "Upgrade Complete" "Upgrade completed successfully!\n\nVersion: $current_version → $new_version\n\n✓ Configuration preserved${container_note}"
    fi

    echo "=== Upgrade Complete ==="
    return 0
}

# Radio Configuration function
configure_radio() {
    # Check if service is running
    if ! is_running; then
        show_error "Service is not running!\n\nPlease start the service first from the main menu."
        return
    fi

    # Get IP address
    local ip_address=$(hostname -I | awk '{print $1}')

    # Show info about web-based configuration
    if ask_yes_no "Configure Radio Settings" "Radio configuration is now done through the web interface.\n\nThe web-based setup wizard provides an easy way to:\n\n• Change repeater name\n• Select hardware board\n• Configure radio frequency and settings\n• Update admin password\n\nWeb Dashboard: http://$ip_address:8000/setup\n\nWould you like to open this information?"; then
        echo "═══════════════════════════════════════════════════════════════"
        echo "        Web-Based Radio Configuration"
        echo "═══════════════════════════════════════════════════════════════"
        echo ""
        echo "To configure your radio settings:"
        echo ""
        echo "  1. Open a web browser"
        echo "  2. Navigate to: http://$ip_address:8000/setup"
        echo "  3. Complete the setup wizard:"
        echo "     • Choose repeater name"
        echo "     • Select hardware board"
        echo "     • Configure radio settings"
        echo "     • Update passwords if needed"
        echo "  4. Service will restart automatically with new settings"
        echo ""
        echo "═══════════════════════════════════════════════════════════════"
        echo ""
        echo "Note: The web interface is much easier than the old"
        echo "      terminal-based configuration!"
        echo ""
        echo "═══════════════════════════════════════════════════════════════"
        echo ""
        read -p "Press Enter to return to main menu..." || true
    fi
}

# Uninstall function
uninstall_repeater() {
    if [ "$EUID" -ne 0 ]; then
        show_error "Uninstall requires root privileges.\n\nPlease run: sudo $0"
        return 1
    fi

    if ask_yes_no "Confirm Uninstall" "This will completely remove openHop Repeater including:\n\n- Service and files\n- Configuration (backup will be created)\n- Logs and data\n\nThis action cannot be undone!\n\nContinue?"; then
        echo ""
        echo "═══════════════════════════════════════════════════════════════"
        echo "        Uninstalling openHop Repeater"
        echo "═══════════════════════════════════════════════════════════════"
        echo ""
        
        echo ">>> Stopping and disabling service..."
        systemctl stop openhop-plugin-manager 2>/dev/null || true
        systemctl disable openhop-plugin-manager 2>/dev/null || true
        systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        systemctl disable "$SERVICE_NAME" 2>/dev/null || true

        set +e
        (
        echo "20"; echo "# Backing up configuration..."
        if [ -d "$CONFIG_DIR" ]; then
            cp -r "$CONFIG_DIR" "/tmp/openhop_repeater_config_backup_$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
        fi

        echo "40"; echo "# Removing service files..."
        rm -f /etc/systemd/system/openhop-repeater.service
        rm -f /etc/systemd/system/openhop-plugin-manager.service
        systemctl daemon-reload

        echo "50"; echo "# Removing polkit and sudoers rules..."
        rm -f /etc/polkit-1/rules.d/10-openhop-repeater.rules || true
        rm -f /etc/polkit-1/localauthority/50-local.d/10-openhop-repeater.pkla || true
        rm -f /etc/sudoers.d/openhop-repeater
        rm -f /usr/local/bin/pymc-do-upgrade

        echo "60"; echo "# Removing installation..."
        rm -rf "$INSTALL_DIR"
        rm -rf "$CONFIG_DIR"
        rm -rf "$LOG_DIR"
        rm -rf "$DATA_DIR"
        rm -rf "$LEGACY_PYMC_INSTALL_DIR"
        rm -rf "$LEGACY_PYMC_CONFIG_DIR"
        rm -rf "$LEGACY_PYMC_LOG_DIR"
        rm -rf "$LEGACY_PYMC_DATA_DIR"

        echo "80"; echo "# Removing service user..."
        if id "$SERVICE_USER" &>/dev/null; then
            userdel "$SERVICE_USER" 2>/dev/null || true
        fi

        echo "100"; echo "# Uninstall complete!"
        ) | "$DIALOG" --backtitle "openHop Repeater Management" --title "Uninstalling" --gauge "Removing openHop Repeater..." 8 70 0
        local pipeline_status=("${PIPESTATUS[@]}")
        set -e

        if (( pipeline_status[0] != 0 )); then
            error "Uninstall failed with status ${pipeline_status[0]}"
            return "${pipeline_status[0]}"
        fi

        if (( pipeline_status[1] != 0 )); then
            error "Uninstall progress dialog failed with status ${pipeline_status[1]}"
            return "${pipeline_status[1]}"
        fi

        show_info "Uninstall Complete" "\nopenHop Repeater has been completely removed.\n\nConfiguration backup saved to /tmp/\n\nThank you for using openHop Repeater!"
        return 0
    fi

    return 1
}

# Service management
manage_service() {
    local action=$1
    local silent="${2:-false}"

    if [ "$EUID" -ne 0 ]; then
        if [[ "$silent" == "true" ]]; then
            echo "Service management requires root privileges. Please run: sudo $0 $action"
        else
            show_error "Service management requires root privileges.\n\nPlease run: sudo $0"
        fi
        return 1
    fi

    if ! service_exists; then
        if [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
            # Unit file may exist but systemd cache may be stale.
            systemctl daemon-reload >/dev/null 2>&1 || true
        fi
    fi

    if ! service_exists; then
        if [[ "$silent" == "true" ]]; then
            echo "Service is not installed."
        else
            show_error "Service is not installed."
        fi
        return 1
    fi

    case $action in
        "start")
            if ! is_enabled; then
                systemctl enable "$SERVICE_NAME"
            fi
            if ! systemctl start "$SERVICE_NAME"; then
                error "Failed to start ${SERVICE_NAME}"
                return 1
            fi
            if is_running; then
                if [[ "$silent" == "true" ]]; then
                    echo "✓ openHop Repeater service has been started successfully."
                else
                    show_info "Service Started" "\n✓ openHop Repeater service has been started successfully."
                fi
                return 0
            else
                if [[ "$silent" == "true" ]]; then
                    echo "Failed to start service!"
                    echo "Check logs for details."
                else
                    show_error "Failed to start service!\n\nCheck logs for details."
                fi
                return 1
            fi
            ;;
        "stop")
            if ! systemctl stop "$SERVICE_NAME"; then
                error "Failed to stop ${SERVICE_NAME}"
                return 1
            fi
            if [[ "$silent" == "true" ]]; then
                echo "✓ openHop Repeater service has been stopped."
            else
                show_info "Service Stopped" "\n✓ openHop Repeater service has been stopped."
            fi
            return 0
            ;;
        "restart")
            if ! systemctl restart "$SERVICE_NAME"; then
                error "Failed to restart ${SERVICE_NAME}"
                return 1
            fi
            if is_running; then
                if [[ "$silent" == "true" ]]; then
                    echo "✓ openHop Repeater service has been restarted successfully."
                else
                    show_info "Service Restarted" "\n✓ openHop Repeater service has been restarted successfully."
                fi
                return 0
            else
                if [[ "$silent" == "true" ]]; then
                    echo "Failed to restart service!"
                    echo "Check logs for details."
                else
                    show_error "Failed to restart service!\n\nCheck logs for details."
                fi
                return 1
            fi
            ;;
    esac

    return 1
}

# Show detailed status
show_detailed_status() {
    local status_info=""
    local version=$(get_version)
    local ip_address=$(hostname -I | awk '{print $1}')

    status_info="Installation Status: "
    if is_installed; then
        status_info="${status_info}Installed\n"
        status_info="${status_info}Version: $version\n"
        status_info="${status_info}Install Directory: $INSTALL_DIR\n"
        status_info="${status_info}Config Directory: $CONFIG_DIR\n\n"

        status_info="${status_info}Service Status: "
        if is_running; then
            status_info="${status_info}Running ✓\n"
            status_info="${status_info}Web Dashboard: http://$ip_address:8000\n\n"
        else
            status_info="${status_info}Stopped ✗\n\n"
        fi

        # Add system info
        status_info="${status_info}System Info:\n"
        status_info="${status_info}- SPI: "
        if grep -q "spi_bcm2835" /proc/modules 2>/dev/null; then
            status_info="${status_info}Enabled ✓\n"
        else
            status_info="${status_info}Disabled ✗\n"
        fi

        status_info="${status_info}- IP Address: $ip_address\n"
        status_info="${status_info}- Hostname: $(hostname)\n"

    else
        status_info="${status_info}Not Installed"
    fi

    show_info "System Status" "$status_info"
}

# Function to validate and update configuration
validate_and_update_config() {
    local config_file="$CONFIG_DIR/config.yaml"
    local example_file="$SCRIPT_DIR/config.yaml.example"
    local updated_example="$CONFIG_DIR/config.yaml.example"

    normalize_legacy_paths_in_config() {
        local target_file="$1"
        [ -f "$target_file" ] || return 0

        sed -i 's|/var/lib/pymc_repeater|/var/lib/openhop_repeater|g' "$target_file" 2>/dev/null || true
        sed -i 's|/etc/pymc_repeater|/etc/openhop_repeater|g' "$target_file" 2>/dev/null || true
        sed -i 's|/var/log/pymc_repeater|/var/log/openhop_repeater|g' "$target_file" 2>/dev/null || true
        sed -i 's|/opt/pymc_repeater|/opt/openhop_repeater|g' "$target_file" 2>/dev/null || true
    }

    # Ensure destination config directory exists before copy/merge steps.
    mkdir -p "$CONFIG_DIR"

    # Copy the new example file
    if [ -f "$example_file" ]; then
        cp "$example_file" "$updated_example"
    else
        echo "    ⚠ config.yaml.example not found in source directory"
        return 1
    fi

    # Check if user config exists
    if [ ! -f "$config_file" ]; then
        echo "    ⚠ No existing config.yaml found, copying example"
        cp "$updated_example" "$config_file"
        normalize_legacy_paths_in_config "$config_file"
        return 0
    fi

    # Check if yq is available
    local YQ_CMD="/usr/local/bin/yq"
    if ! command -v "$YQ_CMD" &> /dev/null; then
        echo "    ⚠ mikefarah yq not found at $YQ_CMD, skipping config merge"
        return 0
    fi

    # Verify it's the correct yq version
    if [[ "$($YQ_CMD --version 2>&1)" != *"mikefarah/yq"* ]]; then
        echo "    ⚠ Wrong yq version detected at $YQ_CMD, skipping config merge"
        return 0
    fi

    echo "    Merging configuration..."

    # Create backup of user config
    local backup_file="${config_file}.backup.$(date +%Y%m%d_%H%M%S)"
    cp "$config_file" "$backup_file"
    echo "    ✓ Backup created: $backup_file"

    # Merge strategy: user config takes precedence, add missing keys from example
    # This uses yq's multiply merge operator (*) which:
    # - Keeps all values from the right operand (user config)
    # - Adds missing keys from the left operand (example config)
    local temp_merged="${config_file}.merged"

    # Strip comments from user config before merge to prevent comment accumulation.
    # yq preserves comments from both files, so each upgrade cycle would duplicate
    # the header and inline comments. We keep only the example's comments.
    local stripped_user="${config_file}.stripped"
    "$YQ_CMD" eval '... comments=""' "$config_file" > "$stripped_user" 2>/dev/null || cp "$config_file" "$stripped_user"

    if "$YQ_CMD" eval-all '. as $item ireduce ({}; . * $item)' "$updated_example" "$stripped_user" > "$temp_merged" 2>/dev/null; then
        rm -f "$stripped_user"
        # Verify the merged file is valid YAML
        if "$YQ_CMD" eval '.' "$temp_merged" > /dev/null 2>&1; then
            mv "$temp_merged" "$config_file"
            normalize_legacy_paths_in_config "$config_file"
            echo "    ✓ Configuration merged successfully"
            echo "    ✓ Legacy pymc_* paths normalized"
            echo "    ✓ User settings preserved, new options added"
            return 0
        else
            echo "    ✗ Merged config is invalid, restoring backup"
            rm -f "$temp_merged"
            cp "$backup_file" "$config_file"
            normalize_legacy_paths_in_config "$config_file"
            return 1
        fi
    else
        echo "    ✗ Config merge failed, keeping original"
        rm -f "$temp_merged" "$stripped_user"
        normalize_legacy_paths_in_config "$config_file"
        return 1
    fi
}

usage() {
    echo "openHop Repeater Management Script"
    echo ""
    echo "Usage: $SCRIPT_PATH [action]"
    echo ""
    echo "Actions:"
    echo "  install   - Install openHop Repeater"
    echo "  upgrade   - Upgrade existing installation (CLI is silent by default; use --interactive to show dialogs)"
    echo "  reset     - Reset existing installation to defaults"
    echo "  uninstall - Remove openHop Repeater"
    echo "  config    - Configure radio settings"
    echo "  start     - Start the service (CLI is silent by default; use --interactive to show dialogs)"
    echo "  stop      - Stop the service (CLI is silent by default; use --interactive to show dialogs)"
    echo "  restart   - Restart the service (CLI is silent by default; use --interactive to show dialogs)"
    echo "  logs      - View live logs"
    echo "  status    - Show status"
    echo "  debug     - Show debug information"
    echo ""
    echo "Run without arguments for interactive menu."
}

main() {
    local command="${1:-}"
    local silent_mode="true"

    case "$command" in
        "" )
            while true; do
                show_main_menu
            done
            ;;
        "--help"|"-h")
            usage
            return 0
            ;;
        "debug")
            echo "=== Debug Information ==="
            echo "DIALOG: $DIALOG"
            echo "TERM: $TERM"
            echo "TTY: $(tty 2>/dev/null || echo 'not a tty')"
            echo "EUID: $EUID"
            echo "PWD: $PWD"
            echo "Script: $SCRIPT_PATH"
            echo ""
            echo "Testing dialog..."
            "$DIALOG" --backtitle "openHop Repeater Management" --title "Test" --msgbox "Dialog test successful!" 8 40
            echo "Dialog test completed."
            return 0
            ;;
        "install"|"upgrade"|"reset"|"uninstall")
            if [ "$EUID" -ne 0 ]; then
                error "${command} requires root privileges. Please run: sudo $SCRIPT_PATH ${command}"
                return 1
            fi
            acquire_global_lock || return 1
            ;;
    esac

    case "$command" in
        "install")
            install_repeater install
            ;;
        "upgrade")
            if is_interactive_flag "${2:-}" || [[ "$SILENT_MODE" == "0" || "$SILENT_MODE" == "false" ]]; then
                silent_mode="false"
            fi
            upgrade_repeater "$silent_mode"
            ;;
        "reset")
            reset_repeater
            ;;
        "uninstall")
            uninstall_repeater
            ;;
        "config")
            configure_radio
            ;;
        "start"|"stop"|"restart")
            if is_interactive_flag "${2:-}" || [[ "$SILENT_MODE" == "0" || "$SILENT_MODE" == "false" ]]; then
                silent_mode="false"
            fi
            manage_service "$command" "$silent_mode"
            ;;
        "logs")
            print_live_logs_header
            echo ""
            journalctl -u "$SERVICE_NAME" -f -o cat --no-hostname | sed -e 's/.*ERROR.*/\x1b[1;31m&\x1b[0m/' -e 's/.*CRITICAL.*/\x1b[1;41;37m&\x1b[0m/' -e 's/.*WARNING.*/\x1b[1;33m&\x1b[0m/' -e 's/.*INFO.*/\x1b[0;32m&\x1b[0m/' -e 's/.*DEBUG.*/\x1b[0;36m&\x1b[0m/'
            ;;
        "status")
            show_detailed_status
            ;;
        *)
            usage >&2
            return 2
            ;;
    esac
}

main "$@"
