#!/bin/bash
# openHop Repeater Management Script - Deploy, Upgrade, Uninstall

set -e

INSTALL_DIR="/opt/openhop_repeater"
VENV_DIR="$INSTALL_DIR/venv"
VENV_PIP="$VENV_DIR/bin/pip"
VENV_PYTHON="$VENV_DIR/bin/python"
CONFIG_DIR="/etc/openhop_repeater"
LOG_DIR="/var/log/openhop_repeater"
DATA_DIR="/var/lib/openhop_repeater"
SERVICE_USER="repeater"
SERVICE_NAME="openhop-repeater"
SILENT_MODE="${PYMC_SILENT:-${SILENT:-}}"

LEGACY_PYMC_INSTALL_DIR="/opt/pymc_repeater"
LEGACY_PYMC_CONFIG_DIR="/etc/pymc_repeater"
LEGACY_PYMC_LOG_DIR="/var/log/pymc_repeater"
LEGACY_PYMC_DATA_DIR="/var/lib/pymc_repeater"

# R2 Wheels Configuration improves install speed on ARM devices
R2_BASE_URL="https://wheel.pymc.dev/pymc_build_deps"
R2_ENABLED=1  # Set to 0 to disable R2 wheels and always build from source

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

    # Always use python -m pip so we don't rely on a potentially stale pip wrapper.
    "$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true
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
    if [[ "$1" =~ ^(upgrade|start|stop|restart)$ ]] && ! is_interactive_flag "$2"; then
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

# Function to check if service exists
service_exists() {
    systemctl list-unit-files | grep -q "^$SERVICE_NAME.service"
}

# Function to check if service is installed
is_installed() {
    [ -d "$INSTALL_DIR" ] && service_exists
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
                install_repeater
            fi
            ;;
        "upgrade")
            if is_installed; then
                upgrade_repeater "false"
            else
                show_error "openHop Repeater is not installed!\n\nUse 'install' first."
            fi
            ;;
        "reset")
            if is_installed; then
                reset_repeater
            else
                show_error "openHop Repeater is not installed!\n\nUse 'install' first."
            fi
            ;;
        "uninstall")
            if is_installed; then
                uninstall_repeater
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
            clear
            echo -e "\033[1;36m╔══════════════════════════════════════════════════════════════════════╗\033[0m"
            echo -e "\033[1;36m║\033[0m                  \033[1;37mopenHop Repeater - Live Logs\033[0m                     \033[1;36m║\033[0m"
            echo -e "\033[1;36m║\033[0m                  \033[0;90m(Press Ctrl+C to return)\033[0m                      \033[1;36m║\033[0m"
            echo -e "\033[1;36m╚══════════════════════════════════════════════════════════════════════╝\033[0m"
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
        return
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

    # Get script directory for file copying during installation
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

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
    DEBIAN_FRONTEND=noninteractive apt-get install -y libffi-dev libusb-1.0-0 sudo jq pip python3-venv python3-rrdtool wget swig build-essential python3-dev i2c-tools
    # Install polkit (package name varies by distro version)
    DEBIAN_FRONTEND=noninteractive apt-get install -y policykit-1 2>/dev/null \
        || DEBIAN_FRONTEND=noninteractive apt-get install -y polkitd pkexec 2>/dev/null \
        || echo "    Warning: Could not install polkit (sudo fallback will be used)"
    # setuptools_scm needed for git version detection during build
    pip install --break-system-packages setuptools_scm >/dev/null 2>&1 || python3 -m pip install --break-system-packages setuptools_scm >/dev/null 2>&1 || true

    echo "28"; echo "# Creating virtual environment..."
    ensure_venv

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
    cp "$SCRIPT_DIR/radio-settings.json" "$DATA_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/radio-presets.json" "$DATA_DIR/" 2>/dev/null || true

    echo "45"; echo "# Installing configuration..."
    cp "$SCRIPT_DIR/config.yaml.example" "$CONFIG_DIR/config.yaml.example"
    if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
        cp "$SCRIPT_DIR/config.yaml.example" "$CONFIG_DIR/config.yaml"
    fi

    echo "55"; echo "# Installing systemd service..."
    cp "$SCRIPT_DIR/openhop-repeater.service" /etc/systemd/system/
    systemctl daemon-reload

    echo "58"; echo "# Installing udev rules for CH341..."
    if [ -f "$SCRIPT_DIR/../openhop-core/99-ch341.rules" ]; then
        cp "$SCRIPT_DIR/../openhop-core/99-ch341.rules" /etc/udev/rules.d/99-ch341.rules
        udevadm control --reload-rules 2>/dev/null || true
        udevadm trigger 2>/dev/null || true
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
repeater ALL=(root) NOPASSWD: /usr/bin/systemctl restart openhop-repeater, /usr/bin/systemctl stop openhop-repeater, /usr/bin/systemctl start openhop-repeater, /usr/bin/systemctl status openhop-repeater, /usr/bin/systemctl enable openhop-repeater, /usr/bin/systemctl enable openhop-repeater.service, /usr/local/bin/pymc-do-upgrade
EOF
    chmod 0440 /etc/sudoers.d/openhop-repeater

    echo ">>> Installing OTA upgrade wrapper..."
    cat > /usr/local/bin/pymc-do-upgrade <<'UPGRADEEOF'
#!/bin/bash
# pymc-do-upgrade: invoked by the repeater service user via sudo for OTA upgrades.
# Usage: sudo /usr/local/bin/pymc-do-upgrade [channel] [pretend-version]
set -e
CHANNEL="${1:-main}"
PRETEND_VERSION="${2:-}"
VENV_DIR="/opt/openhop_repeater/venv"
VENV_PIP="$VENV_DIR/bin/pip"
VENV_PYTHON="$VENV_DIR/bin/python"
# Validate: only allow safe git ref characters
if ! [[ "$CHANNEL" =~ ^[a-zA-Z0-9._/-]{1,80}$ ]]; then
    echo "Invalid channel name: $CHANNEL" >&2
    exit 1
fi
# If caller supplied a version string, tell setuptools_scm to use it (sudo
# strips env vars so it is passed as a positional argument instead).
[ -n "$PRETEND_VERSION" ] && export SETUPTOOLS_SCM_PRETEND_VERSION="$PRETEND_VERSION"
# ---- Migration: ensure venv exists (handles upgrades from system-pip era) ----
if [ ! -x "$VENV_PYTHON" ]; then
    echo "[pymc-do-upgrade] Creating venv at $VENV_DIR ..."
    python3 -m venv --system-site-packages "$VENV_DIR"
    "$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true
fi
# ---- Migration: clean up legacy service unit issues ----
SVC_UNIT=/etc/systemd/system/openhop-repeater.service
# Ensure the migrated openHop service remains enabled across reboots.
systemctl enable openhop-repeater.service >/dev/null 2>&1 || true
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
python3 -m pip uninstall -y openhop_repeater 2>/dev/null || true
python3 -m pip uninstall -y openhop_core 2>/dev/null || true
python3 -m pip uninstall -y pymc_repeater 2>/dev/null || true
python3 -m pip uninstall -y pymc_core 2>/dev/null || true
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
    PY_TAG=$("$VENV_PYTHON" -c 'import sys; v=f"cp{sys.version_info.major}{sys.version_info.minor}"; print(f"{v}-{v}")' 2>/dev/null || echo "cp311-cp311")
    WHEEL_BASE="${R2_BASE_URL}/${ARCH_TAG}/${PLATFORM_TAG}/${PY_TAG}"
    echo "[pymc-do-upgrade] Trying dependencies from R2 wheels..."
    "$VENV_PYTHON" -m pip install --find-links "${WHEEL_BASE}/index.html" --no-cache-dir "pycryptodome>=3.23.0" "PyNaCl>=1.5.0" cffi "pyyaml>=6.0.0" 2>/dev/null || true
fi
# ---- Install openhop_repeater from git ----
if "$VENV_PYTHON" -m pip install \
    --upgrade \
    --no-cache-dir \
    "openhop_repeater[hardware] @ git+https://github.com/openhop-dev/openhop_repeater.git@${CHANNEL}"; then
    # Keep web/OTA updates aligned with manage.sh install/upgrade defaults.
    RADIO_BASE_URL="https://raw.githubusercontent.com/openhop-dev/openhop_repeater/${CHANNEL}"
    RADIO_STORAGE_DIR="/var/lib/openhop_repeater"
    mkdir -p "$RADIO_STORAGE_DIR"
    wget -qO "$RADIO_STORAGE_DIR/radio-settings.json" "${RADIO_BASE_URL}/radio-settings.json" 2>/dev/null || true
    wget -qO "$RADIO_STORAGE_DIR/radio-presets.json" "${RADIO_BASE_URL}/radio-presets.json" 2>/dev/null || true
else
    exit 1
fi
UPGRADEEOF
    chmod 0755 /usr/local/bin/pymc-do-upgrade

    echo "75"; echo "# Starting service..."
    systemctl enable "$SERVICE_NAME"

    echo "90"; echo "# Installation files complete..."
    ) | $DIALOG --backtitle "openHop Repeater Management" --title "Installing" --gauge "Setting up openHop Repeater..." 8 70 0

    # Install Python package outside of progress gauge for better error handling
    clear
    echo "=== Installing Python Dependencies ==="
    echo ""
    echo "Installing openhop_repeater and dependencies (including openhop_core from PyPI)..."
    echo "This may take a few minutes..."
    echo ""

    SCRIPT_DIR="$(dirname "$0")"
    cd "$SCRIPT_DIR"

    # Calculate version from git for setuptools_scm
    if [ -d .git ]; then
        git fetch --tags 2>/dev/null || true
        GIT_VERSION=$(python3 -m setuptools_scm 2>/dev/null || echo "1.0.5")
        export SETUPTOOLS_SCM_PRETEND_VERSION="$GIT_VERSION"
        echo "Installing version: $GIT_VERSION"
    else
        export SETUPTOOLS_SCM_PRETEND_VERSION="1.0.5"
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
    
    if "$VENV_PYTHON" -m pip install --upgrade --no-cache-dir .[hardware]; then
        echo ""
        echo "✓ Python package installation completed successfully!"

        # Reload systemd and start the service
        systemctl daemon-reload
        systemctl start "$SERVICE_NAME"
    else
        echo ""
        echo "✗ Python package installation failed!"
        echo "Please check the error messages above and try again."
        read -p "Press Enter to continue..." || true
    fi

    # Show final results
    sleep 2
    local ip_address=$(hostname -I | awk '{print $1}')
    if is_running; then
        clear
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
    fi
}

# Reset function
reset_repeater() {
    local config_file="$CONFIG_DIR/config.yaml"
    local updated_example="$CONFIG_DIR/config.yaml.example"

    if [ "$EUID" -ne 0 ]; then
        show_error "Upgrade requires root privileges.\n\nPlease run: sudo $0"
        return
    fi

    local current_version=$(get_version)

    if ask_yes_no "Confirm Reset of openHop Repeater restoring to default configuration.\n\nContinue?"; then

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
	cp $updated_example $config_file
	sleep 5
        # Reload systemd and start the service
	echo "4/4 Restart the service"
        systemctl daemon-reload
        systemctl start "$SERVICE_NAME"
        # Show final results
        sleep 2
        local ip_address=$(hostname -I | awk '{print $1}')
        if is_running; then
            clear
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
        fi
    fi
}

# Upgrade function
upgrade_repeater() {
    local silent="${1:-false}"
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

        echo "=== Upgrade Progress ==="
        echo "[1/9] Stopping service..."
        systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        disable_legacy_services

        echo "[1.5/9] Migrating legacy paths..."
        migrate_legacy_paths
        cleanup_stale_source_trees

        echo "[1.6/9] Ensuring required directories..."
        mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$LOG_DIR" "$DATA_DIR"
        ensure_service_user_home

        echo "[2/9] Backing up configuration..."
        if [ -d "$CONFIG_DIR" ]; then
            cp -r "$CONFIG_DIR" "$CONFIG_DIR.backup.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
            echo "    ✓ Configuration backed up"
        fi

        echo "[3/9] Updating system dependencies..."
        apt-get update -qq

        apt-get install -y libffi-dev libusb-1.0-0 sudo jq pip python3-venv python3-rrdtool wget swig build-essential python3-dev i2c-tools
        # Install polkit (package name varies by distro version)
        apt-get install -y policykit-1 2>/dev/null \
            || apt-get install -y polkitd pkexec 2>/dev/null \
            || echo "    Warning: Could not install polkit (sudo fallback will be used)"
        pip install --break-system-packages setuptools_scm >/dev/null 2>&1 || python3 -m pip install --break-system-packages setuptools_scm >/dev/null 2>&1 || true

        # Install mikefarah yq v4 if not already installed
        if ! command -v yq &> /dev/null || [[ "$(yq --version 2>&1)" != *"mikefarah/yq"* ]]; then
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

        echo "[4/9] Installing files..."
        SCRIPT_DIR="$(dirname "$0")"
        if ! cp "$SCRIPT_DIR/openhop-repeater.service" /etc/systemd/system/; then
            echo "    ⚠ Warning: Failed to update service file – old service file may remain"
        fi
        cp "$SCRIPT_DIR/radio-settings.json" "$DATA_DIR/" 2>/dev/null || true
        cp "$SCRIPT_DIR/radio-presets.json" "$DATA_DIR/" 2>/dev/null || true
        echo "    ✓ Files updated"

        echo "[5/9] Validating and updating configuration..."
        if validate_and_update_config; then
            echo "    ✓ Configuration validated and updated"
        else
            echo "    ⚠ Configuration validation failed, keeping existing config"
        fi

        echo "[5.5/9] Ensuring user groups and udev rules..."
        for grp in plugdev dialout gpio i2c spi; do
            getent group "$grp" >/dev/null 2>&1 && usermod -a -G "$grp" "$SERVICE_USER" 2>/dev/null || true
        done
        # Install/update CH341 udev rules
        SCRIPT_DIR_UPGRADE="$(cd "$(dirname "$0")" && pwd)"
        if [ -f "$SCRIPT_DIR_UPGRADE/../openhop-core/99-ch341.rules" ]; then
            cp "$SCRIPT_DIR_UPGRADE/../openhop-core/99-ch341.rules" /etc/udev/rules.d/99-ch341.rules
            udevadm control --reload-rules 2>/dev/null || true
            udevadm trigger 2>/dev/null || true
            echo "    ✓ CH341 udev rules updated"
        elif [ -f /etc/udev/rules.d/99-ch341.rules ]; then
            echo "    ✓ CH341 udev rules already present"
        fi
        echo "    ✓ User groups updated"

        echo "[6/9] Fixing permissions..."
        
        # Venv stays root-owned (pip runs as root); service user only needs read+execute
        chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR" "$LOG_DIR" "$DATA_DIR" 2>/dev/null || true
        chown root:root "$INSTALL_DIR" 2>/dev/null || true
        chmod 755 "$INSTALL_DIR" 2>/dev/null || true
        chmod 750 "$CONFIG_DIR" "$LOG_DIR" 2>/dev/null || true
        chmod 755 "$DATA_DIR" 2>/dev/null || true
        
        # Pre-create the .config directory that the service will need
        mkdir -p "$DATA_DIR/.config/openhop_repeater" 2>/dev/null || true
        chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR/.config" 2>/dev/null || true
        
        # Configure polkit for passwordless service restart
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
        mkdir -p /etc/sudoers.d
        cat > /etc/sudoers.d/openhop-repeater <<'EOF'
# Allow repeater user to manage the openhop-repeater service without password
repeater ALL=(root) NOPASSWD: /usr/bin/systemctl restart openhop-repeater, /usr/bin/systemctl stop openhop-repeater, /usr/bin/systemctl start openhop-repeater, /usr/bin/systemctl status openhop-repeater, /usr/bin/systemctl enable openhop-repeater, /usr/bin/systemctl enable openhop-repeater.service, /usr/local/bin/pymc-do-upgrade
EOF
        chmod 0440 /etc/sudoers.d/openhop-repeater
        # Install / refresh OTA upgrade wrapper
        cat > /usr/local/bin/pymc-do-upgrade <<'UPGRADEEOF'
#!/bin/bash
# pymc-do-upgrade: invoked by the repeater service user via sudo for OTA upgrades.
# Usage: sudo /usr/local/bin/pymc-do-upgrade [channel] [pretend-version]
set -e
CHANNEL="${1:-main}"
PRETEND_VERSION="${2:-}"
VENV_DIR="/opt/openhop_repeater/venv"
VENV_PIP="$VENV_DIR/bin/pip"
VENV_PYTHON="$VENV_DIR/bin/python"
# Validate: only allow safe git ref characters
if ! [[ "$CHANNEL" =~ ^[a-zA-Z0-9._/-]{1,80}$ ]]; then
    echo "Invalid channel name: $CHANNEL" >&2
    exit 1
fi
# If caller supplied a version string, tell setuptools_scm to use it (sudo
# strips env vars so it is passed as a positional argument instead).
[ -n "$PRETEND_VERSION" ] && export SETUPTOOLS_SCM_PRETEND_VERSION="$PRETEND_VERSION"
# ---- Migration: ensure venv exists (handles upgrades from system-pip era) ----
if [ ! -x "$VENV_PYTHON" ]; then
    echo "[pymc-do-upgrade] Creating venv at $VENV_DIR ..."
    python3 -m venv --system-site-packages "$VENV_DIR"
    "$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true
fi
# ---- Migration: clean up legacy service unit issues ----
SVC_UNIT=/etc/systemd/system/openhop-repeater.service
# Ensure the migrated openHop service remains enabled across reboots.
systemctl enable openhop-repeater.service >/dev/null 2>&1 || true
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
python3 -m pip uninstall -y openhop_repeater 2>/dev/null || true
python3 -m pip uninstall -y openhop_core 2>/dev/null || true
python3 -m pip uninstall -y pymc_repeater 2>/dev/null || true
python3 -m pip uninstall -y pymc_core 2>/dev/null || true
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
            PY_TAG=$("$VENV_PYTHON" -c 'import sys; v=f"cp{sys.version_info.major}{sys.version_info.minor}"; print(f"{v}-{v}")' 2>/dev/null || echo "cp311-cp311")
            WHEEL_BASE="${R2_BASE_URL}/${ARCH_TAG}/${PLATFORM_TAG}/${PY_TAG}"
            echo "[pymc-do-upgrade] Trying dependencies from R2 wheels..."
            "$VENV_PYTHON" -m pip install --find-links "${WHEEL_BASE}/index.html" --no-cache-dir "pycryptodome>=3.23.0" "PyNaCl>=1.5.0" cffi "pyyaml>=6.0.0" 2>/dev/null || true
        fi
        # ---- Install openhop_repeater from git ----
        if "$VENV_PYTHON" -m pip install \
            --upgrade \
            --no-cache-dir \
            "openhop_repeater[hardware] @ git+https://github.com/openhop-dev/openhop_repeater.git@${CHANNEL}"; then
            # Keep web/OTA updates aligned with manage.sh install/upgrade defaults.
            RADIO_BASE_URL="https://raw.githubusercontent.com/openhop-dev/openhop_repeater/${CHANNEL}"
            RADIO_STORAGE_DIR="/var/lib/openhop_repeater"
            mkdir -p "$RADIO_STORAGE_DIR"
            wget -qO "$RADIO_STORAGE_DIR/radio-settings.json" "${RADIO_BASE_URL}/radio-settings.json" 2>/dev/null || true
            wget -qO "$RADIO_STORAGE_DIR/radio-presets.json" "${RADIO_BASE_URL}/radio-presets.json" 2>/dev/null || true
        else
            exit 1
        fi
UPGRADEEOF
        chmod 0755 /usr/local/bin/pymc-do-upgrade
        echo "    ✓ Permissions updated"

        echo "[7/9] Reloading systemd..."
        systemctl daemon-reload
        echo "    ✓ Systemd reloaded"

        echo "=== Installing Python Dependencies ==="
        echo ""
        echo "Updating openhop_repeater and dependencies (including openhop_core from PyPI)..."
        echo "This may take a few minutes..."
        echo ""

        # Install from source directory to properly resolve Git dependencies
        SCRIPT_DIR="$(dirname "$0")"
        cd "$SCRIPT_DIR"

        # Calculate version from git for setuptools_scm
        if [ -d .git ]; then
            git fetch --tags 2>/dev/null || true
            GIT_VERSION=$(python3 -m setuptools_scm 2>/dev/null || echo "1.0.5")
            export SETUPTOOLS_SCM_PRETEND_VERSION="$GIT_VERSION"
            echo "Upgrading to version: $GIT_VERSION"
        else
            export SETUPTOOLS_SCM_PRETEND_VERSION="1.0.5"
        fi

    # We don't have any binary wheels available for these on a LuckFox, so we need to ignore them on that platform.
        if ! grep -q "Luckfox Pico" /proc/device-tree/model 2>/dev/null; then
            # Force binary wheels for slow-to-compile packages (much faster on Raspberry Pi)
            export PIP_ONLY_BINARY=pycryptodome,cffi,PyNaCl,psutil
        fi
        echo "Note: Using optimized binary wheels for faster installation"
        echo ""

        # Migrate from system pip to venv (idempotent)
        migrate_to_venv

        # Install into the venv (clean, no system-packages flags needed)
        echo "Upgrading openhop_repeater into venv ($VENV_DIR)..."
        
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
        
        if "$VENV_PYTHON" -m pip install --upgrade --no-cache-dir .[hardware]; then
            echo ""
            echo "✓ Package and dependencies upgraded successfully!"
        else
            echo ""
            echo "⚠ Package upgrade failed, but continuing..."
        fi


        echo "[8/9] Starting service..."
        systemctl daemon-reload
        systemctl start "$SERVICE_NAME"
        echo "    ✓ Service started"

        echo "[9/9] Verifying installation..."
        sleep 3  # Give service time to start

        local new_version=$(get_version)

        if is_running; then
            echo "    ✓ Service is running"
            # Container detection: warn about host-side udev rules
            local container_note=""
            if [ -f /run/host/container-manager ] || [ -n "${container:-}" ] || grep -qsai 'container=' /proc/1/environ 2>/dev/null || [ -f /.dockerenv ]; then
                container_note="\n\n⚠ CONTAINER DETECTED:\nUSB udev rules must be set on the HOST, not here.\nSee documentation for CH341 host-side setup."
            fi
            if [[ "$silent" == "true" ]]; then
                echo "Upgrade completed successfully!"
                echo "Version: $current_version -> $new_version"
                echo "✓ Service is running"
                echo "✓ Configuration preserved"
                if [[ -n "$container_note" ]]; then
                    echo "$container_note"
                fi
            else
                show_info "Upgrade Complete" "Upgrade completed successfully!\n\nVersion: $current_version → $new_version\n\n✓ Service is running\n✓ Configuration preserved${container_note}"
            fi
        else
            echo "    ✗ Service failed to start"
            if [[ "$silent" == "true" ]]; then
                echo "Upgrade completed but service failed to start!"
                echo "Version updated: $current_version -> $new_version"
                echo "Check logs from the main menu for details."
            else
                show_error "Upgrade completed but service failed to start!\n\nVersion updated: $current_version → $new_version\n\nCheck logs from the main menu for details."
            fi
        fi
        echo "=== Upgrade Complete ==="
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
        clear
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
        return
    fi

    if ask_yes_no "Confirm Uninstall" "This will completely remove openHop Repeater including:\n\n- Service and files\n- Configuration (backup will be created)\n- Logs and data\n\nThis action cannot be undone!\n\nContinue?"; then
        echo ""
        echo "═══════════════════════════════════════════════════════════════"
        echo "        Uninstalling openHop Repeater"
        echo "═══════════════════════════════════════════════════════════════"
        echo ""
        
        echo ">>> Stopping and disabling service..."
        systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        systemctl disable "$SERVICE_NAME" 2>/dev/null || true

        (
        echo "20"; echo "# Backing up configuration..."
        if [ -d "$CONFIG_DIR" ]; then
            cp -r "$CONFIG_DIR" "/tmp/openhop_repeater_config_backup_$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
        fi

        echo "40"; echo "# Removing service files..."
        rm -f /etc/systemd/system/openhop-repeater.service
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
        ) | $DIALOG --backtitle "openHop Repeater Management" --title "Uninstalling" --gauge "Removing openHop Repeater..." 8 70 0

        show_info "Uninstall Complete" "\nopenHop Repeater has been completely removed.\n\nConfiguration backup saved to /tmp/\n\nThank you for using openHop Repeater!"
    fi
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
            systemctl start "$SERVICE_NAME"
            if is_running; then
                if [[ "$silent" == "true" ]]; then
                    echo "✓ openHop Repeater service has been started successfully."
                else
                    show_info "Service Started" "\n✓ openHop Repeater service has been started successfully."
                fi
            else
                if [[ "$silent" == "true" ]]; then
                    echo "Failed to start service!"
                    echo "Check logs for details."
                else
                    show_error "Failed to start service!\n\nCheck logs for details."
                fi
            fi
            ;;
        "stop")
            systemctl stop "$SERVICE_NAME"
            if [[ "$silent" == "true" ]]; then
                echo "✓ openHop Repeater service has been stopped."
            else
                show_info "Service Stopped" "\n✓ openHop Repeater service has been stopped."
            fi
            ;;
        "restart")
            systemctl restart "$SERVICE_NAME"
            if is_running; then
                if [[ "$silent" == "true" ]]; then
                    echo "✓ openHop Repeater service has been restarted successfully."
                else
                    show_info "Service Restarted" "\n✓ openHop Repeater service has been restarted successfully."
                fi
            else
                if [[ "$silent" == "true" ]]; then
                    echo "Failed to restart service!"
                    echo "Check logs for details."
                else
                    show_error "Failed to restart service!\n\nCheck logs for details."
                fi
            fi
            ;;
    esac
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
    local example_file="config.yaml.example"
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
    YQ_CMD="/usr/local/bin/yq"
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

# Main script logic
if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    echo "openHop Repeater Management Script"
    echo ""
    echo "Usage: $0 [action]"
    echo ""
    echo "Actions:"
    echo "  install   - Install openHop Repeater"
    echo "  upgrade   - Upgrade existing installation (CLI is silent by default; use --interactive to show dialogs)"
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
    exit 0
fi

# Debug mode
if [ "$1" = "debug" ]; then
    echo "=== Debug Information ==="
    echo "DIALOG: $DIALOG"
    echo "TERM: $TERM"
    echo "TTY: $(tty 2>/dev/null || echo 'not a tty')"
    echo "EUID: $EUID"
    echo "PWD: $PWD"
    echo "Script: $0"
    echo ""
    echo "Testing dialog..."
    $DIALOG --backtitle "openHop Repeater Management" --title "Test" --msgbox "Dialog test successful!" 8 40
    echo "Dialog test completed."
    exit 0
fi

# Handle command line arguments
case "$1" in
    "install")
        install_repeater install
        exit 0
        ;;
    "upgrade")
        silent_mode="true"
        if is_interactive_flag "${2:-}" || [[ "$SILENT_MODE" == "0" || "$SILENT_MODE" == "false" ]]; then
            silent_mode="false"
        fi
        upgrade_repeater "$silent_mode"
        exit 0
        ;;
    "uninstall")
        uninstall_repeater
        exit 0
        ;;
    "config")
        configure_radio
        exit 0
        ;;
    "start"|"stop"|"restart")
        silent_mode="true"
        if is_interactive_flag "${2:-}" || [[ "$SILENT_MODE" == "0" || "$SILENT_MODE" == "false" ]]; then
            silent_mode="false"
        fi
        manage_service "$1" "$silent_mode"
        exit 0
        ;;
    "logs")
        clear
        echo -e "\033[1;36m╔══════════════════════════════════════════════════════════════════════╗\033[0m"
        echo -e "\033[1;36m║\033[0m                  \033[1;37mopenHop Repeater - Live Logs\033[0m                     \033[1;36m║\033[0m"
        echo -e "\033[1;36m║\033[0m                  \033[0;90m(Press Ctrl+C to return)\033[0m                      \033[1;36m║\033[0m"
        echo -e "\033[1;36m╚══════════════════════════════════════════════════════════════════════╝\033[0m"
        echo ""
        journalctl -u "$SERVICE_NAME" -f -o cat --no-hostname | sed -e 's/.*ERROR.*/\x1b[1;31m&\x1b[0m/' -e 's/.*CRITICAL.*/\x1b[1;41;37m&\x1b[0m/' -e 's/.*WARNING.*/\x1b[1;33m&\x1b[0m/' -e 's/.*INFO.*/\x1b[0;32m&\x1b[0m/' -e 's/.*DEBUG.*/\x1b[0;36m&\x1b[0m/'
        ;;
    "status")
        show_detailed_status
        exit 0
        ;;
esac

# Interactive menu loop
while true; do
    show_main_menu
done
