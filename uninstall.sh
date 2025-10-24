#!/bin/bash
# Uninstall script for pyMC Repeater

set -e

INSTALL_DIR="/opt/pymc_repeater"
CONFIG_DIR="/etc/pymc_repeater"
LOG_DIR="/var/log/pymc_repeater"
SERVICE_USER="repeater"
SERVICE_FILE="/etc/systemd/system/pymc-repeater.service"

echo "=== pyMC Repeater Uninstall ==="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Error: This script must be run as root"
    exit 1
fi

# Stop and disable service
if systemctl is-active --quiet pymc-repeater; then
    echo "Stopping service..."
    systemctl stop pymc-repeater
fi

if systemctl is-enabled --quiet pymc-repeater 2>/dev/null; then
    echo "Disabling service..."
    systemctl disable pymc-repeater
fi

# Remove systemd service file
if [ -f "$SERVICE_FILE" ]; then
    echo "Removing systemd service..."
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
fi

# Uninstall Python package
if [ -d "$INSTALL_DIR" ]; then
    echo "Uninstalling Python package..."
    cd "$INSTALL_DIR"
    pip uninstall -y pymc_repeater 2>/dev/null || true
fi

# Remove installation directory
if [ -d "$INSTALL_DIR" ]; then
    echo "Removing installation directory..."
    rm -rf "$INSTALL_DIR"
fi

# Ask before removing config and logs
echo ""
read -p "Remove configuration files in $CONFIG_DIR? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Removing configuration directory..."
    rm -rf "$CONFIG_DIR"
else
    echo "Keeping configuration files in $CONFIG_DIR"
fi

echo ""
read -p "Remove log files in $LOG_DIR? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Removing log directory..."
    rm -rf "$LOG_DIR"
else
    echo "Keeping log files in $LOG_DIR"
fi

echo ""
read -p "Remove user data in /var/lib/pymc_repeater? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Removing user data directory..."
    rm -rf /var/lib/pymc_repeater
else
    echo "Keeping user data in /var/lib/pymc_repeater"
fi

# Ask before removing service user
echo ""
read -p "Remove service user '$SERVICE_USER'? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    if id "$SERVICE_USER" &>/dev/null; then
        echo "Removing service user..."
        userdel "$SERVICE_USER" 2>/dev/null || true
    fi
else
    echo "Keeping service user '$SERVICE_USER'"
fi

echo ""
echo "=== Uninstall Complete ==="
echo ""
echo "The pyMC Repeater has been removed from your system."
echo "----------------------------------"
