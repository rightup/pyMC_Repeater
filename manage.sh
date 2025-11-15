#!/bin/bash
# pyMC Repeater Management Script - Deploy, Upgrade, Uninstall

set -e

INSTALL_DIR="/opt/pymc_repeater"
CONFIG_DIR="/etc/pymc_repeater"
LOG_DIR="/var/log/pymc_repeater"
SERVICE_USER="repeater"
SERVICE_NAME="pymc-repeater"

# Check if we're running in an interactive terminal
if [ ! -t 0 ] || [ -z "$TERM" ]; then
    echo "Error: This script requires an interactive terminal."
    echo "Please run from SSH or a local terminal, not via file manager."
    exit 1
fi

# Check if whiptail is available, fallback to dialog
if command -v whiptail &> /dev/null; then
    DIALOG="whiptail"
    DIALOG_OPTS="--backtitle 'pyMC Repeater Management'"
elif command -v dialog &> /dev/null; then
    DIALOG="dialog"
    DIALOG_OPTS="--backtitle 'pyMC Repeater Management'"
else
    echo "TUI interface requires whiptail or dialog."
    if [ "$EUID" -eq 0 ]; then
        echo "Installing whiptail..."
        apt-get update -qq && apt-get install -y whiptail
        DIALOG="whiptail"
        DIALOG_OPTS="--backtitle 'pyMC Repeater Management'"
    else
        echo ""
        echo "Please install whiptail: sudo apt-get install -y whiptail"
        echo "Then run this script again."
        exit 1
    fi
fi

# Function to show info box
show_info() {
    $DIALOG $DIALOG_OPTS --title "$1" --msgbox "$2" 12 70
}

# Function to show error box
show_error() {
    $DIALOG $DIALOG_OPTS --title "Error" --msgbox "$1" 8 60
}

# Function to ask yes/no question
ask_yes_no() {
    $DIALOG $DIALOG_OPTS --title "$1" --yesno "$2" 10 70
}

# Function to show progress
show_progress() {
    echo "$2" | $DIALOG $DIALOG_OPTS --title "$1" --gauge "$3" 8 70 0
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

# Function to get current version
get_version() {
    if [ -f "$INSTALL_DIR/pyproject.toml" ]; then
        grep "^version" "$INSTALL_DIR/pyproject.toml" | cut -d'"' -f2 2>/dev/null || echo "unknown"
    else
        echo "not installed"
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
    
    CHOICE=$($DIALOG $DIALOG_OPTS --title "pyMC Repeater Management" --menu "\nCurrent Status: $status\n\nChoose an action:" 18 70 8 \
        "install" "Install pyMC Repeater" \
        "upgrade" "Upgrade existing installation" \
        "uninstall" "Remove pyMC Repeater completely" \
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
                upgrade_repeater
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
        "start")
            manage_service "start"
            ;;
        "stop")
            manage_service "stop"
            ;;
        "restart")
            manage_service "restart"
            ;;
        "logs")
            clear
            echo "=== Live Logs (Press Ctrl+C to return) ==="
            echo ""
            journalctl -u "$SERVICE_NAME" -f
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
    $DIALOG $DIALOG_OPTS --title "Welcome" --msgbox "\nWelcome to pyMC Repeater Setup\n\nThis installer will configure your Raspberry Pi as a LoRa mesh network repeater.\n\nPress OK to continue..." 12 70
    
    # SPI Check
    if ! grep -q "dtparam=spi=on" /boot/config.txt 2>/dev/null && ! grep -q "spi_bcm2835" /proc/modules 2>/dev/null; then
        if ask_yes_no "SPI Not Enabled" "\nSPI interface is required but not enabled!\n\nWould you like to enable it now?\n(This will require a reboot)"; then
            echo "dtparam=spi=on" >> /boot/config.txt
            show_info "SPI Enabled" "\nSPI has been enabled in /boot/config.txt\n\nSystem will reboot now. Please run this script again after reboot."
            reboot
        else
            show_error "SPI is required for LoRa radio operation.\n\nPlease enable SPI manually and run this script again."
            return
        fi
    fi
    
    # Installation type
    INSTALL_TYPE=$($DIALOG $DIALOG_OPTS --title "Installation Type" --menu "\nChoose installation type:" 15 70 3 \
        "full" "Complete installation with web dashboard" \
        "minimal" "Core repeater only (no web interface)" \
        "custom" "Custom component selection" 3>&1 1>&2 2>&3)
    
    if [ $? -ne 0 ]; then
        return
    fi
    
    # Radio configuration
    SETUP_RADIO=false
    if ask_yes_no "Radio Configuration" "\nWould you like to configure radio settings from community presets?\n\nThis will download optimized settings for your region."; then
        SETUP_RADIO=true
        
        REGION=$($DIALOG $DIALOG_OPTS --title "Select Region" --menu "\nSelect your region:" 15 70 5 \
            "EU868" "Europe (868 MHz)" \
            "US915" "United States (915 MHz)" \
            "AU915" "Australia (915 MHz)" \
            "AS923" "Asia (923 MHz)" \
            "custom" "Custom configuration" 3>&1 1>&2 2>&3)
        
        if [ $? -ne 0 ]; then
            SETUP_RADIO=false
        fi
    fi
    
    # Installation progress
    (
    echo "0"; echo "# Creating service user..."
    if ! id "$SERVICE_USER" &>/dev/null; then
        useradd --system --home /var/lib/pymc_repeater --shell /sbin/nologin "$SERVICE_USER"
    fi
    
    echo "10"; echo "# Adding user to hardware groups..."
    usermod -a -G gpio,i2c,spi "$SERVICE_USER" 2>/dev/null || true
    usermod -a -G dialout "$SERVICE_USER" 2>/dev/null || true
    
    echo "20"; echo "# Creating directories..."
    mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$LOG_DIR" /var/lib/pymc_repeater
    
    echo "30"; echo "# Installing files..."
    cp -r repeater "$INSTALL_DIR/"
    cp pyproject.toml "$INSTALL_DIR/"
    cp README.md "$INSTALL_DIR/"
    cp setup-radio-config.sh "$INSTALL_DIR/" 2>/dev/null || true
    cp radio-settings.json "$INSTALL_DIR/" 2>/dev/null || true
    
    echo "40"; echo "# Installing configuration..."
    cp config.yaml.example "$CONFIG_DIR/config.yaml.example"
    if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
        cp config.yaml.example "$CONFIG_DIR/config.yaml"
    fi
    
    echo "50"
    if [ "$SETUP_RADIO" = true ] && [ "$REGION" != "custom" ]; then
        echo "# Configuring radio settings..."
        if ! command -v jq &> /dev/null; then
            apt-get update -qq
            apt-get install -y jq
        fi
    fi
    
    echo "70"; echo "# Installing systemd service..."
    cp pymc-repeater.service /etc/systemd/system/
    systemctl daemon-reload
    
    echo "80"; echo "# Setting permissions..."
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$CONFIG_DIR" "$LOG_DIR" /var/lib/pymc_repeater
    chmod 750 "$CONFIG_DIR" "$LOG_DIR" /var/lib/pymc_repeater
    
    echo "90"; echo "# Installing Python package..."
    cd "$INSTALL_DIR"
    pip install --break-system-packages -e . >/dev/null 2>&1
    
    echo "95"; echo "# Starting service..."
    systemctl enable "$SERVICE_NAME"
    systemctl start "$SERVICE_NAME"
    
    echo "100"; echo "# Installation complete!"
    ) | $DIALOG $DIALOG_OPTS --title "Installing" --gauge "Setting up pyMC Repeater..." 8 70
    
    # Show results
    sleep 2
    local ip_address=$(hostname -I | awk '{print $1}')
    if is_running; then
        local msg="\nInstallation completed successfully!\n\n✓ Service is running\n"
        if [ "$INSTALL_TYPE" = "full" ]; then
            msg="${msg}\nWeb Dashboard: http://$ip_address:8000"
        fi
        msg="${msg}\n\nView logs: Select 'logs' from main menu"
        show_info "Installation Complete" "$msg"
    else
        show_error "Installation completed but service failed to start!\n\nCheck logs from the main menu."
    fi
}

# Upgrade function
upgrade_repeater() {
    if [ "$EUID" -ne 0 ]; then
        show_error "Upgrade requires root privileges.\n\nPlease run: sudo $0"
        return
    fi
    
    local current_version=$(get_version)
    
    if ask_yes_no "Confirm Upgrade" "\nCurrent version: $current_version\n\nThis will upgrade pyMC Repeater while preserving your configuration.\n\nContinue?"; then
        (
        echo "0"; echo "# Stopping service..."
        systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        
        echo "20"; echo "# Backing up configuration..."
        cp -r "$CONFIG_DIR" "$CONFIG_DIR.backup.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
        
        echo "40"; echo "# Installing new files..."
        cp -r repeater "$INSTALL_DIR/"
        cp pyproject.toml "$INSTALL_DIR/"
        cp README.md "$INSTALL_DIR/"
        
        echo "60"; echo "# Updating Python package..."
        cd "$INSTALL_DIR"
        pip install --break-system-packages -e . >/dev/null 2>&1
        
        echo "80"; echo "# Reloading systemd..."
        cp pymc-repeater.service /etc/systemd/system/
        systemctl daemon-reload
        
        echo "90"; echo "# Starting service..."
        systemctl start "$SERVICE_NAME"
        
        echo "100"; echo "# Upgrade complete!"
        ) | $DIALOG $DIALOG_OPTS --title "Upgrading" --gauge "Upgrading pyMC Repeater..." 8 70
        
        sleep 2
        local new_version=$(get_version)
        if is_running; then
            show_info "Upgrade Complete" "\nUpgrade completed successfully!\n\nVersion: $current_version → $new_version\n\n✓ Service is running"
        else
            show_error "Upgrade completed but service failed to start!\n\nCheck logs from the main menu."
        fi
    fi
}

# Uninstall function
uninstall_repeater() {
    if [ "$EUID" -ne 0 ]; then
        show_error "Uninstall requires root privileges.\n\nPlease run: sudo $0"
        return
    fi
    
    if ask_yes_no "Confirm Uninstall" "\nThis will completely remove pyMC Repeater including:\n\n• Service and files\n• Configuration (backup will be created)\n• Logs and data\n\nThis action cannot be undone!\n\nContinue?"; then
        (
        echo "0"; echo "# Stopping and disabling service..."
        systemctl stop "$SERVICE_NAME" 2>/dev/null || true
        systemctl disable "$SERVICE_NAME" 2>/dev/null || true
        
        echo "20"; echo "# Backing up configuration..."
        if [ -d "$CONFIG_DIR" ]; then
            cp -r "$CONFIG_DIR" "/tmp/pymc_repeater_config_backup_$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
        fi
        
        echo "40"; echo "# Removing service files..."
        rm -f /etc/systemd/system/pymc-repeater.service
        systemctl daemon-reload
        
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
        ) | $DIALOG $DIALOG_OPTS --title "Uninstalling" --gauge "Removing pyMC Repeater..." 8 70
        
        show_info "Uninstall Complete" "\npyMC Repeater has been completely removed.\n\nConfiguration backup saved to /tmp/\n\nThank you for using pyMC Repeater!"
    fi
}

# Service management
manage_service() {
    local action=$1
    
    if [ "$EUID" -ne 0 ]; then
        show_error "Service management requires root privileges.\n\nPlease run: sudo $0"
        return
    fi
    
    if ! service_exists; then
        show_error "Service is not installed."
        return
    fi
    
    case $action in
        "start")
            systemctl start "$SERVICE_NAME"
            if is_running; then
                show_info "Service Started" "\n✓ pyMC Repeater service has been started successfully."
            else
                show_error "Failed to start service!\n\nCheck logs for details."
            fi
            ;;
        "stop")
            systemctl stop "$SERVICE_NAME"
            show_info "Service Stopped" "\n✓ pyMC Repeater service has been stopped."
            ;;
        "restart")
            systemctl restart "$SERVICE_NAME"
            if is_running; then
                show_info "Service Restarted" "\n✓ pyMC Repeater service has been restarted successfully."
            else
                show_error "Failed to restart service!\n\nCheck logs for details."
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
        status_info="${status_info}• SPI: "
        if grep -q "spi_bcm2835" /proc/modules 2>/dev/null; then
            status_info="${status_info}Enabled ✓\n"
        else
            status_info="${status_info}Disabled ✗\n"
        fi
        
        status_info="${status_info}• IP Address: $ip_address\n"
        status_info="${status_info}• Hostname: $(hostname)\n"
        
    else
        status_info="${status_info}Not Installed"
    fi
    
    show_info "System Status" "$status_info"
}

# Main script logic
if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    echo "pyMC Repeater Management Script"
    echo ""
    echo "Usage: $0 [action]"
    echo ""
    echo "Actions:"
    echo "  install   - Install pyMC Repeater"
    echo "  upgrade   - Upgrade existing installation"
    echo "  uninstall - Remove pyMC Repeater"
    echo "  start     - Start the service"
    echo "  stop      - Stop the service"
    echo "  restart   - Restart the service"
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
    echo "DIALOG_OPTS: $DIALOG_OPTS"
    echo "TERM: $TERM"
    echo "TTY: $(tty 2>/dev/null || echo 'not a tty')"
    echo "EUID: $EUID"
    echo "PWD: $PWD"
    echo "Script: $0"
    echo ""
    echo "Testing dialog..."
    $DIALOG $DIALOG_OPTS --title "Test" --msgbox "Dialog test successful!" 8 40
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
        upgrade_repeater
        exit 0
        ;;
    "uninstall")
        uninstall_repeater
        exit 0
        ;;
    "start"|"stop"|"restart")
        manage_service "$1"
        exit 0
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
