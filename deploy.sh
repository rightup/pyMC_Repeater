#!/bin/bash
# Deployment script for pyMC Repeater

set -e

INSTALL_DIR="/opt/pymc_repeater"
CONFIG_DIR="/etc/pymc_repeater"
LOG_DIR="/var/log/pymc_repeater"
SERVICE_USER="repeater"

echo "=== pyMC Repeater Deployment ==="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Error: This script must be run as root"
    exit 1
fi

# Create service user
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Creating service user: $SERVICE_USER"
    useradd --system --home /var/lib/pymc_repeater --shell /sbin/nologin "$SERVICE_USER"
fi

# Add service user to required groups for hardware access
echo "Adding $SERVICE_USER to hardware groups..."
usermod -a -G gpio,i2c,spi "$SERVICE_USER" 2>/dev/null || true
usermod -a -G dialout "$SERVICE_USER" 2>/dev/null || true

# Create directories
echo "Creating directories..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$CONFIG_DIR"
mkdir -p "$LOG_DIR"
mkdir -p /var/lib/pymc_repeater

# Copy files
echo "Installing files..."
cp -r repeater "$INSTALL_DIR/"
cp pyproject.toml "$INSTALL_DIR/"
cp README.md "$INSTALL_DIR/"
cp setup-radio-config.sh "$INSTALL_DIR/"
cp radio-settings.json "$INSTALL_DIR/"

# Copy config files
echo "Installing configuration..."
cp config.yaml.example "$CONFIG_DIR/config.yaml.example"

# Create actual config if it doesn't exist (optional, will use defaults if missing)
if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    echo "Creating config file from example..."
    cp config.yaml.example "$CONFIG_DIR/config.yaml"
    echo "NOTE: Default config created. Customize $CONFIG_DIR/config.yaml as needed."
else
    echo "Existing config file found, keeping it."
fi

# Setup radio configuration from API
echo ""
echo "=== Radio Configuration Setup ==="
read -p "Would you like to configure radio settings from community presets? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    # Install jq if not already installed
    if ! command -v jq &> /dev/null; then
        echo "Installing jq..."
        apt-get update -qq
        apt-get install -y jq
    fi
    bash setup-radio-config.sh "$CONFIG_DIR"
else
    echo "Skipping radio configuration setup."
fi

# Install systemd service
echo "Installing systemd service..."
cp pymc-repeater.service /etc/systemd/system/
systemctl daemon-reload

# Set permissions
echo "Setting permissions..."
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$CONFIG_DIR" "$LOG_DIR" /var/lib/pymc_repeater
chmod 750 "$CONFIG_DIR" "$LOG_DIR"
chmod 750 /var/lib/pymc_repeater

# Install Python package
echo "Installing Python package..."
cd "$INSTALL_DIR"
# Use --break-system-packages for system-wide installation
# This is safe here since we're installing in a dedicated directory
pip install --break-system-packages -e .

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Enabling and starting service..."
systemctl enable pymc-repeater
systemctl start pymc-repeater

echo ""
echo "Service status:"
systemctl is-active pymc-repeater && echo "✓ Service is running" || echo "✗ Service failed to start"
echo ""
echo "Next steps:"
echo "1. Check live logs:"
echo "   journalctl -u pymc-repeater -f"
echo ""
echo "2. Access web dashboard:"
echo "   http://$(hostname -I | awk '{print $1}'):8000"
echo "----------------------------------"
