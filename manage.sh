#!/bin/bash
# pyMC Repeater Management Script - Deploy, Upgrade, Uninstall

set -e

INSTALL_DIR="/opt/pymc_repeater"
CONFIG_DIR="/etc/pymc_repeater"
LOG_DIR="/var/log/pymc_repeater"
SERVICE_USER="repeater"
SERVICE_NAME="pymc-repeater"
SILENT_MODE="${PYMC_SILENT:-${SILENT:-}}"

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
    $DIALOG --backtitle "pyMC Repeater Management" --title "$1" --msgbox "$2" 12 70
}

# Function to show error box
show_error() {
    $DIALOG --backtitle "pyMC Repeater Management" --title "Error" --msgbox "$1" 8 60
}

# Function to ask yes/no question
ask_yes_no() {
    $DIALOG --backtitle "pyMC Repeater Management" --title "$1" --yesno "$2" 10 70
}

# Function to show progress
show_progress() {
    echo "$2" | $DIALOG --backtitle "pyMC Repeater Management" --title "$1" --gauge "$3" 8 70 0
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

# Function to get current version
get_version() {
    # Read version from the pip-installed package in dist-packages
    python3 -c "from importlib.metadata import version; print(version('pymc_repeater'))" 2>/dev/null \
        || echo "not installed"
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

    CHOICE=$($DIALOG --backtitle "pyMC Repeater Management" --title "pyMC Repeater Management" --menu "\nCurrent Status: $status\n\nChoose an action:" 18 70 9 \
        "install" "Install pyMC Repeater" \
        "upgrade" "Upgrade existing installation" \
        "reset" "reset existing installation to defaults" \
        "uninstall" "Remove pyMC Repeater completely" \
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
                show_error "pyMC Repeater is already installed!\n\nUse 'upgrade' to update or 'uninstall' first."
            else
                install_repeater
            fi
            ;;
        "upgrade")
            if is_installed; then
                upgrade_repeater "false"
            else
                show_error "pyMC Repeater is not installed!\n\nUse 'install' first."
            fi
            ;;
        "reset")
            if is_installed; then
                reset_repeater
            else
                show_error "pyMC Repeater is not installed!\n\nUse 'install' first."
            fi
            ;;
        "uninstall")
            if is_installed; then
                uninstall_repeater
            else
                show_error "pyMC Repeater is not installed."
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
            echo -e "\033[1;36m║\033[0m                  \033[1;37mpyMC Repeater - Live Logs\033[0m                     \033[1;36m║\033[0m"
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

    # Welcome screen
    $DIALOG --backtitle "pyMC Repeater Management" --title "Welcome" --msgbox "\nWelcome to pyMC Repeater Setup\n\nThis installer will configure your Linux system as a LoRa mesh network repeater.\n\nPress OK to continue..." 12 70

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
    echo "        Installing pyMC Repeater"
    echo "═══════════════════════════════════════════════════════════════"
    echo ""
    
    echo ">>> Creating service user..."
    if ! id "$SERVICE_USER" &>/dev/null; then
        useradd --system --home /var/lib/pymc_repeater --shell /sbin/nologin "$SERVICE_USER"
    fi

    (
    echo "10"; echo "# Adding user to hardware groups..."
    for grp in plugdev dialout gpio i2c spi; do
        getent group "$grp" >/dev/null 2>&1 && usermod -a -G "$grp" "$SERVICE_USER" 2>/dev/null || true
    done

    echo "20"; echo "# Creating directories..."
    mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$LOG_DIR" /var/lib/pymc_repeater

    echo "25"; echo "# Installing system dependencies..."
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y libffi-dev libusb-1.0-0 sudo jq pip python3-rrdtool wget swig build-essential python3-dev
    # Install polkit (package name varies by distro version)
    DEBIAN_FRONTEND=noninteractive apt-get install -y policykit-1 2>/dev/null \
        || DEBIAN_FRONTEND=noninteractive apt-get install -y polkitd pkexec 2>/dev/null \
        || echo "    Warning: Could not install polkit (sudo fallback will be used)"
    pip install --break-system-packages setuptools_scm >/dev/null 2>&1 || true

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
    cp "$SCRIPT_DIR/pymc-repeater.service" "$INSTALL_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/radio-settings.json" /var/lib/pymc_repeater/ 2>/dev/null || true
    cp "$SCRIPT_DIR/radio-presets.json" /var/lib/pymc_repeater/ 2>/dev/null || true

    echo "45"; echo "# Installing configuration..."
    cp "$SCRIPT_DIR/config.yaml.example" "$CONFIG_DIR/config.yaml.example"
    if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
        cp "$SCRIPT_DIR/config.yaml.example" "$CONFIG_DIR/config.yaml"
    fi

    echo "55"; echo "# Installing systemd service..."
    cp "$SCRIPT_DIR/pymc-repeater.service" /etc/systemd/system/
    systemctl daemon-reload

    echo "58"; echo "# Installing udev rules for CH341..."
    if [ -f "$SCRIPT_DIR/../pyMC_core/99-ch341.rules" ]; then
        cp "$SCRIPT_DIR/../pyMC_core/99-ch341.rules" /etc/udev/rules.d/99-ch341.rules
        udevadm control --reload-rules 2>/dev/null || true
        udevadm trigger 2>/dev/null || true
    fi

    echo "65"; echo "# Setting permissions..."
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$CONFIG_DIR" "$LOG_DIR" /var/lib/pymc_repeater
    chmod 750 "$CONFIG_DIR" "$LOG_DIR" /var/lib/pymc_repeater
    # Ensure the service user can create subdirectories in their home directory
    chmod 755 /var/lib/pymc_repeater
    # Pre-create the .config directory that the service will need
    mkdir -p /var/lib/pymc_repeater/.config/pymc_repeater
    chown -R "$SERVICE_USER:$SERVICE_USER" /var/lib/pymc_repeater/.config

    # Configure polkit for passwordless service restart
    echo ">>> Configuring polkit for service management..."
    mkdir -p /etc/polkit-1/rules.d
    cat > /etc/polkit-1/rules.d/10-pymc-repeater.rules <<'EOF'
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") == "pymc-repeater.service" &&
        subject.user == "repeater") {
        return polkit.Result.YES;
    }
});
EOF
    chmod 0644 /etc/polkit-1/rules.d/10-pymc-repeater.rules

    # Also configure sudoers as fallback for service restart
    echo ">>> Configuring sudoers for service management..."
    mkdir -p /etc/sudoers.d
    cat > /etc/sudoers.d/pymc-repeater <<'EOF'
# Allow repeater user to manage the pymc-repeater service without password
repeater ALL=(root) NOPASSWD: /usr/bin/systemctl restart pymc-repeater, /usr/bin/systemctl stop pymc-repeater, /usr/bin/systemctl start pymc-repeater, /usr/bin/systemctl status pymc-repeater, /usr/local/bin/pymc-do-upgrade
EOF
    chmod 0440 /etc/sudoers.d/pymc-repeater

    echo ">>> Installing OTA upgrade wrapper..."
    cat > /usr/local/bin/pymc-do-upgrade <<'UPGRADEEOF'
#!/bin/bash
# pymc-do-upgrade: invoked by the repeater service user via sudo for OTA upgrades.
# Usage: sudo /usr/local/bin/pymc-do-upgrade [channel] [pretend-version]
set -e
CHANNEL="${1:-main}"
PRETEND_VERSION="${2:-}"
# Validate: only allow safe git ref characters
if ! [[ "$CHANNEL" =~ ^[a-zA-Z0-9._/-]{1,80}$ ]]; then
    echo "Invalid channel name: $CHANNEL" >&2
    exit 1
fi
export PIP_ROOT_USER_ACTION=ignore
# If caller supplied a version string, tell setuptools_scm to use it (sudo
# strips env vars so it is passed as a positional argument instead).
[ -n "$PRETEND_VERSION" ] && export SETUPTOOLS_SCM_PRETEND_VERSION="$PRETEND_VERSION"
# Migration: remove legacy PYTHONPATH from service unit if present.
# Old installs set PYTHONPATH=/opt/pymc_repeater which caused the service to
# load from a stale source copy instead of the pip-installed dist-packages.
SVC_UNIT=/etc/systemd/system/pymc-repeater.service
if grep -q 'PYTHONPATH' "$SVC_UNIT" 2>/dev/null; then
    sed -i '/^Environment=.*PYTHONPATH/d' "$SVC_UNIT"
    systemctl daemon-reload
fi
# Migration: fix WorkingDirectory if it still points at the old source checkout.
# /opt/pymc_repeater contains a repeater/ subdirectory which shadows the
# pip-installed package, causing updates to have no effect on the running process.
if grep -q 'WorkingDirectory=/opt/pymc_repeater' "$SVC_UNIT" 2>/dev/null; then
    sed -i 's|WorkingDirectory=/opt/pymc_repeater|WorkingDirectory=/var/lib/pymc_repeater|' "$SVC_UNIT"
    systemctl daemon-reload
fi
exec python3 -m pip install \
    --break-system-packages \
    --no-cache-dir \
    --force-reinstall \
    --ignore-installed \
    "pymc_repeater[hardware] @ git+https://github.com/rightup/pyMC_Repeater.git@${CHANNEL}"
UPGRADEEOF
    chmod 0755 /usr/local/bin/pymc-do-upgrade

    echo "75"; echo "# Starting service..."
    systemctl enable "$SERVICE_NAME"

    echo "90"; echo "# Installation files complete..."
    ) | $DIALOG --backtitle "pyMC Repeater Management" --title "Installing" --gauge "Setting up pyMC Repeater..." 8 70 0

    # Install Python package outside of progress gauge for better error handling
    clear
    echo "=== Installing Python Dependencies ==="
    echo ""
    echo "Installing pymc_repeater and dependencies (including pymc_core from GitHub)..."
    echo "This may take a few minutes..."
    echo ""

    SCRIPT_DIR="$(dirname "$0")"
    cd "$SCRIPT_DIR"

    # Suppress pip root user warnings
    export PIP_ROOT_USER_ACTION=ignore

    # Calculate version from git for setuptools_scm
    if [ -d .git ]; then
        git fetch --tags 2>/dev/null || true
        GIT_VERSION=$(python3 -m setuptools_scm 2>/dev/null || echo "1.0.5")
        export SETUPTOOLS_SCM_PRETEND_VERSION="$GIT_VERSION"
        echo "Installing version: $GIT_VERSION"
    else
        export SETUPTOOLS_SCM_PRETEND_VERSION="1.0.5"
    fi

    # Force binary wheels for slow-to-compile packages (much faster on Raspberry Pi)
    export PIP_ONLY_BINARY=pycryptodome,cffi,PyNaCl,psutil
    echo "Note: Using optimized binary wheels for faster installation"
    echo ""

    # Remove old pymc_core first so no stale .py/.pyc files linger
    python3 -m pip uninstall -y pymc_core 2>/dev/null || true

    # Install with --force-reinstall to ensure fresh pymc_core from GitHub
    # --ignore-installed avoids failures on system-managed packages (e.g. PyYAML)
    echo "Installing pymc_repeater with fresh dependencies from pyproject.toml..."
    if python3 -m pip install --break-system-packages --no-cache-dir --force-reinstall --ignore-installed .[hardware]; then
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
        read -p "Press Enter to return to main menu..." || true
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

    if ask_yes_no "Confirm Reset of pyMC Repeater restoring to default configuration.\n\nContinue?"; then

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
        if ! ask_yes_no "Confirm Upgrade" "Current version: $current_version\n\nThis will upgrade pyMC Repeater while preserving your configuration.\n\nContinue?"; then
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

        echo "[2/9] Backing up configuration..."
        if [ -d "$CONFIG_DIR" ]; then
            cp -r "$CONFIG_DIR" "$CONFIG_DIR.backup.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
            echo "    ✓ Configuration backed up"
        fi

        echo "[3/9] Updating system dependencies..."
        apt-get update -qq

        apt-get install -y libffi-dev libusb-1.0-0 sudo jq pip python3-rrdtool wget swig build-essential python3-dev
        # Install polkit (package name varies by distro version)
        apt-get install -y policykit-1 2>/dev/null \
            || apt-get install -y polkitd pkexec 2>/dev/null \
            || echo "    Warning: Could not install polkit (sudo fallback will be used)"
        pip install --break-system-packages setuptools_scm >/dev/null 2>&1 || true

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
        if ! cp "$SCRIPT_DIR/pymc-repeater.service" /etc/systemd/system/; then
            echo "    ⚠ Warning: Failed to update service file – old service file may remain"
        fi
        cp "$SCRIPT_DIR/radio-settings.json" /var/lib/pymc_repeater/ 2>/dev/null || true
        cp "$SCRIPT_DIR/radio-presets.json" /var/lib/pymc_repeater/ 2>/dev/null || true
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
        if [ -f "$SCRIPT_DIR_UPGRADE/../pyMC_core/99-ch341.rules" ]; then
            cp "$SCRIPT_DIR_UPGRADE/../pyMC_core/99-ch341.rules" /etc/udev/rules.d/99-ch341.rules
            udevadm control --reload-rules 2>/dev/null || true
            udevadm trigger 2>/dev/null || true
            echo "    ✓ CH341 udev rules updated"
        elif [ -f /etc/udev/rules.d/99-ch341.rules ]; then
            echo "    ✓ CH341 udev rules already present"
        fi
        echo "    ✓ User groups updated"

        echo "[6/9] Fixing permissions..."
        chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$CONFIG_DIR" "$LOG_DIR" /var/lib/pymc_repeater 2>/dev/null || true
        chmod 750 "$CONFIG_DIR" "$LOG_DIR" 2>/dev/null || true
        chmod 755 /var/lib/pymc_repeater 2>/dev/null || true
        # Pre-create the .config directory that the service will need
        mkdir -p /var/lib/pymc_repeater/.config/pymc_repeater 2>/dev/null || true
        chown -R "$SERVICE_USER:$SERVICE_USER" /var/lib/pymc_repeater/.config 2>/dev/null || true
        # Configure polkit for passwordless service restart
        mkdir -p /etc/polkit-1/rules.d
        cat > /etc/polkit-1/rules.d/10-pymc-repeater.rules <<'EOF'
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") == "pymc-repeater.service" &&
        subject.user == "repeater") {
        return polkit.Result.YES;
    }
});
EOF
        chmod 0644 /etc/polkit-1/rules.d/10-pymc-repeater.rules
        # Also configure sudoers as fallback for service restart
        mkdir -p /etc/sudoers.d
        cat > /etc/sudoers.d/pymc-repeater <<'EOF'
# Allow repeater user to manage the pymc-repeater service without password
repeater ALL=(root) NOPASSWD: /usr/bin/systemctl restart pymc-repeater, /usr/bin/systemctl stop pymc-repeater, /usr/bin/systemctl start pymc-repeater, /usr/bin/systemctl status pymc-repeater, /usr/local/bin/pymc-do-upgrade
EOF
        chmod 0440 /etc/sudoers.d/pymc-repeater
        # Install / refresh OTA upgrade wrapper
        cat > /usr/local/bin/pymc-do-upgrade <<'UPGRADEEOF'
#!/bin/bash
# pymc-do-upgrade: invoked by the repeater service user via sudo for OTA upgrades.
# Usage: sudo /usr/local/bin/pymc-do-upgrade [channel] [pretend-version]
set -e
CHANNEL="${1:-main}"
PRETEND_VERSION="${2:-}"
# Validate: only allow safe git ref characters
if ! [[ "$CHANNEL" =~ ^[a-zA-Z0-9._/-]{1,80}$ ]]; then
    echo "Invalid channel name: $CHANNEL" >&2
    exit 1
fi
export PIP_ROOT_USER_ACTION=ignore
# If caller supplied a version string, tell setuptools_scm to use it (sudo
# strips env vars so it is passed as a positional argument instead).
[ -n "$PRETEND_VERSION" ] && export SETUPTOOLS_SCM_PRETEND_VERSION="$PRETEND_VERSION"
# Migration: remove legacy PYTHONPATH from service unit if present.
# Old installs set PYTHONPATH=/opt/pymc_repeater which caused the service to
# load from a stale source copy instead of the pip-installed dist-packages.
SVC_UNIT=/etc/systemd/system/pymc-repeater.service
if grep -q 'PYTHONPATH' "$SVC_UNIT" 2>/dev/null; then
    sed -i '/^Environment=.*PYTHONPATH/d' "$SVC_UNIT"
    systemctl daemon-reload
fi
# Migration: fix WorkingDirectory if it still points at the old source checkout.
# /opt/pymc_repeater contains a repeater/ subdirectory which shadows the
# pip-installed package, causing updates to have no effect on the running process.
if grep -q 'WorkingDirectory=/opt/pymc_repeater' "$SVC_UNIT" 2>/dev/null; then
    sed -i 's|WorkingDirectory=/opt/pymc_repeater|WorkingDirectory=/var/lib/pymc_repeater|' "$SVC_UNIT"
    systemctl daemon-reload
fi
exec python3 -m pip install \
    --break-system-packages \
    --no-cache-dir \
    --force-reinstall \
    --ignore-installed \
    "pymc_repeater[hardware] @ git+https://github.com/rightup/pyMC_Repeater.git@${CHANNEL}"
UPGRADEEOF
        chmod 0755 /usr/local/bin/pymc-do-upgrade
        echo "    ✓ Permissions updated"

        echo "[7/9] Reloading systemd..."
        systemctl daemon-reload
        echo "    ✓ Systemd reloaded"

        echo "=== Installing Python Dependencies ==="
        echo ""
        echo "Updating pymc_repeater and dependencies (including pymc_core from GitHub)..."
        echo "This may take a few minutes..."
        echo ""

        # Install from source directory to properly resolve Git dependencies
        SCRIPT_DIR="$(dirname "$0")"
        cd "$SCRIPT_DIR"

        # Suppress pip root user warnings
        export PIP_ROOT_USER_ACTION=ignore

        # Calculate version from git for setuptools_scm
        if [ -d .git ]; then
            git fetch --tags 2>/dev/null || true
            GIT_VERSION=$(python3 -m setuptools_scm 2>/dev/null || echo "1.0.5")
            export SETUPTOOLS_SCM_PRETEND_VERSION="$GIT_VERSION"
            echo "Upgrading to version: $GIT_VERSION"
        else
            export SETUPTOOLS_SCM_PRETEND_VERSION="1.0.5"
        fi

        # Force binary wheels for slow-to-compile packages (much faster on Raspberry Pi)
        export PIP_ONLY_BINARY=pycryptodome,cffi,PyNaCl,psutil
        echo "Note: Using optimized binary wheels for faster installation"
        echo ""

        # Remove old pymc_core first so no stale .py/.pyc files linger
        python3 -m pip uninstall -y pymc_core 2>/dev/null || true

        # Install with --force-reinstall to ensure fresh pymc_core from GitHub
        # --ignore-installed avoids failures on system-managed packages (e.g. PyYAML)
        echo "Upgrading pymc_repeater with fresh dependencies from pyproject.toml..."
        if python3 -m pip install --break-system-packages --no-cache-dir --force-reinstall --ignore-installed .[hardware]; then
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

    if ask_yes_no "Confirm Uninstall" "This will completely remove pyMC Repeater including:\n\n- Service and files\n- Configuration (backup will be created)\n- Logs and data\n\nThis action cannot be undone!\n\nContinue?"; then
        echo ""
        echo "═══════════════════════════════════════════════════════════════"
        echo "        Uninstalling pyMC Repeater"
        echo "═══════════════════════════════════════════════════════════════"
        echo ""
        
        echo ">>> Stopping and disabling service..."
        systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        systemctl disable "$SERVICE_NAME" 2>/dev/null || true

        (
        echo "20"; echo "# Backing up configuration..."
        if [ -d "$CONFIG_DIR" ]; then
            cp -r "$CONFIG_DIR" "/tmp/pymc_repeater_config_backup_$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
        fi

        echo "40"; echo "# Removing service files..."
        rm -f /etc/systemd/system/pymc-repeater.service
        systemctl daemon-reload

        echo "50"; echo "# Removing polkit and sudoers rules..."
        rm -f /etc/polkit-1/rules.d/10-pymc-repeater.rules
        rm -f /etc/sudoers.d/pymc-repeater
        rm -f /usr/local/bin/pymc-do-upgrade

        echo "60"; echo "# Removing installation..."
        rm -rf "$INSTALL_DIR"
        rm -rf "$CONFIG_DIR"
        rm -rf "$LOG_DIR"
        rm -rf /var/lib/pymc_repeater

        echo "80"; echo "# Removing service user..."
        if id "$SERVICE_USER" &>/dev/null; then
            userdel "$SERVICE_USER" 2>/dev/null || true
        fi

        echo "100"; echo "# Uninstall complete!"
        ) | $DIALOG --backtitle "pyMC Repeater Management" --title "Uninstalling" --gauge "Removing pyMC Repeater..." 8 70 0

        show_info "Uninstall Complete" "\npyMC Repeater has been completely removed.\n\nConfiguration backup saved to /tmp/\n\nThank you for using pyMC Repeater!"
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
                    echo "✓ pyMC Repeater service has been started successfully."
                else
                    show_info "Service Started" "\n✓ pyMC Repeater service has been started successfully."
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
                echo "✓ pyMC Repeater service has been stopped."
            else
                show_info "Service Stopped" "\n✓ pyMC Repeater service has been stopped."
            fi
            ;;
        "restart")
            systemctl restart "$SERVICE_NAME"
            if is_running; then
                if [[ "$silent" == "true" ]]; then
                    echo "✓ pyMC Repeater service has been restarted successfully."
                else
                    show_info "Service Restarted" "\n✓ pyMC Repeater service has been restarted successfully."
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
            echo "    ✓ Configuration merged successfully"
            echo "    ✓ User settings preserved, new options added"
            return 0
        else
            echo "    ✗ Merged config is invalid, restoring backup"
            rm -f "$temp_merged"
            cp "$backup_file" "$config_file"
            return 1
        fi
    else
        echo "    ✗ Config merge failed, keeping original"
        rm -f "$temp_merged" "$stripped_user"
        return 1
    fi
}

# Main script logic
if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    echo "pyMC Repeater Management Script"
    echo ""
    echo "Usage: $0 [action]"
    echo ""
    echo "Actions:"
    echo "  install   - Install pyMC Repeater"
    echo "  upgrade   - Upgrade existing installation (CLI is silent by default; use --interactive to show dialogs)"
    echo "  uninstall - Remove pyMC Repeater"
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
    $DIALOG --backtitle "pyMC Repeater Management" --title "Test" --msgbox "Dialog test successful!" 8 40
    echo "Dialog test completed."
    exit 0
fi

# Handle command line arguments
case "$1" in
    "install")
        install_repeater
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
        echo -e "\033[1;36m║\033[0m                  \033[1;37mpyMC Repeater - Live Logs\033[0m                     \033[1;36m║\033[0m"
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
