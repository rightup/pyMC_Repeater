#!/bin/bash
# Simple upgrade script for pyMC Repeater

set -e

INSTALL_DIR="/opt/pymc_repeater"
SERVICE_USER="repeater"

echo "=== pyMC Repeater Upgrade ==="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Error: This script must be run as root"
    exit 1
fi

# Check if pyMC Repeater is installed
if [ ! -d "$INSTALL_DIR" ]; then
    echo "Error: pyMC Repeater is not installed in $INSTALL_DIR"
    echo "Please run deploy.sh to install first."
    exit 1
fi

# Check if we're in a git repository
if [ ! -d ".git" ]; then
    echo "Error: This script must be run from the pyMC Repeater git repository root"
    exit 1
fi

# Check service status
SERVICE_WAS_RUNNING=false
if systemctl is-active --quiet pymc-repeater; then
    SERVICE_WAS_RUNNING=true
    echo "Stopping pyMC Repeater service..."
    systemctl stop pymc-repeater
fi

# Pull latest changes
echo "Pulling latest code from main branch..."
git fetch origin
git checkout main
git pull origin main

# Copy updated files
echo "Installing updated files..."
cp -r repeater "$INSTALL_DIR/"
cp pyproject.toml "$INSTALL_DIR/"
cp README.md "$INSTALL_DIR/"
cp setup-radio-config.sh "$INSTALL_DIR/"
cp radio-settings.json "$INSTALL_DIR/"

# Update systemd service if changed
if [ -f "pymc-repeater.service" ]; then
    cp pymc-repeater.service /etc/systemd/system/
    systemctl daemon-reload
fi

# Update Python package and dependencies
echo "Updating Python package and dependencies..."
cd "$INSTALL_DIR"
pip install --break-system-packages --upgrade --force-reinstall -e .

# Set permissions
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# Restart service if it was running
if [ "$SERVICE_WAS_RUNNING" = true ]; then
    echo "Starting pyMC Repeater service..."
    systemctl start pymc-repeater
    sleep 2
    
    if systemctl is-active --quiet pymc-repeater; then
        echo "✓ Service restarted successfully"
    else
        echo "✗ Service failed to start - check logs:"
        journalctl -u pymc-repeater --no-pager -n 10
        exit 1
    fi
fi

echo ""
echo "=== Upgrade Complete ==="
echo "Updated to: $(git rev-parse --short HEAD)"
echo ""
echo "Check status: systemctl status pymc-repeater"
echo "View logs: journalctl -u pymc-repeater -f"
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):8000"
echo "----------------------------------"