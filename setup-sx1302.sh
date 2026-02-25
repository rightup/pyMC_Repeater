#!/bin/bash
# setup-sx1302.sh — Build and configure WM1302/SX1302 LoRa concentrator support
#
# Run this after selecting SX1302 hardware in the web setup wizard:
#   sudo ./setup-sx1302.sh
#
# What it does:
#   1. Clones Lora-net/sx1302_hal from GitHub and builds libloragw.so
#   2. Creates the GPIO reset script (pinctrl — Bookworm/Trixie)
#   3. Writes radio_type and sx1302 section to /etc/pymc_repeater/config.yaml
#   4. Restarts the service

set -e

INSTALL_DIR="/opt/pymc_repeater"
CONFIG_FILE="/etc/pymc_repeater/config.yaml"
HAL_DIR="$INSTALL_DIR/sx1302_hal"
SO_FILE="$HAL_DIR/libloragw/libloragw.so"
RESET_SCRIPT="$HAL_DIR/libloragw/reset_lgw.sh"

if [ "$EUID" -ne 0 ]; then
    echo "Error: requires root — run: sudo $0"
    exit 1
fi

echo "=== SX1302/WM1302 Setup ==="
echo ""

# ── 1. Clone and build sx1302_hal ─────────────────────────────────────────────

if [ -f "$SO_FILE" ]; then
    echo "✓ libloragw.so already present — skipping build"
else
    if [ ! -d "$HAL_DIR" ]; then
        echo "Cloning Lora-net/sx1302_hal..."
        if ! git clone --depth=1 https://github.com/Lora-net/sx1302_hal.git "$HAL_DIR"; then
            echo "✗ Failed to clone sx1302_hal"
            exit 1
        fi
        echo "✓ Cloned"
    fi

    echo "Building for $(uname -m)..."

    # Append -fPIC to CFLAGS in both Makefiles — required for shared library
    sed -i 's/^CFLAGS\s*:=\(.*\)/CFLAGS :=\1 -fPIC/' "$HAL_DIR/libtools/Makefile" 2>/dev/null || true
    sed -i 's/^CFLAGS\s*:=\(.*\)/CFLAGS :=\1 -fPIC/' "$HAL_DIR/libloragw/Makefile" 2>/dev/null || true

    if ! make -C "$HAL_DIR/libtools" all 2>&1; then
        echo "✗ libtools build failed"
        exit 1
    fi

    if ! make -C "$HAL_DIR/libloragw" all 2>&1; then
        echo "✗ libloragw build failed"
        exit 1
    fi

    echo "Linking shared library..."

    libloragw_a="$HAL_DIR/libloragw/libloragw.a"
    if [ ! -f "$libloragw_a" ]; then
        echo "✗ libloragw.a not found — build failed"
        exit 1
    fi

    # Collect all archives from libtools (libtinymt32.a, libparson.a, etc.)
    mapfile -t libtools_archives < <(find "$HAL_DIR/libtools" -name "*.a" 2>/dev/null)
    if [ ${#libtools_archives[@]} -eq 0 ]; then
        echo "✗ No libtools archives found — libtools build failed"
        exit 1
    fi

    if ! gcc -shared -o "$SO_FILE" \
        -Wl,--whole-archive "$libloragw_a" "${libtools_archives[@]}" \
        -Wl,--no-whole-archive; then
        echo "✗ Failed to create libloragw.so"
        exit 1
    fi

    echo "✓ Built for $(uname -m)"
fi

# ── 2. GPIO reset script ───────────────────────────────────────────────────────

if [ -f "$RESET_SCRIPT" ]; then
    echo "✓ reset_lgw.sh already present"
else
    cat > "$RESET_SCRIPT" << 'RESET_EOF'
#!/bin/bash
# SX1302 GPIO reset sequence
# Pins: 18=POWER_EN, 17=SX1302_RESET, 5=SX1261_RESET, 13=ADC_RESET
set -e
if ! command -v pinctrl &>/dev/null; then
    echo "Error: pinctrl not found" >&2
    exit 1
fi
pinctrl set 18 op dh
sleep 0.01
pinctrl set 17 op dh
sleep 0.01
pinctrl set 17 dl
sleep 0.01
pinctrl set 5 op dl
sleep 0.01
pinctrl set 5 dh
sleep 0.01
pinctrl set 13 op dl
sleep 0.01
pinctrl set 13 dh
sleep 0.5
exit 0
RESET_EOF
    chmod +x "$RESET_SCRIPT"
    echo "✓ reset_lgw.sh created"
fi

# ── 3. Update config.yaml ──────────────────────────────────────────────────────

if [ ! -f "$CONFIG_FILE" ]; then
    echo "✗ Config not found at $CONFIG_FILE"
    echo "  Complete the web setup wizard first, then re-run this script."
    exit 1
fi

if grep -q "^radio_type:" "$CONFIG_FILE"; then
    sed -i 's/^radio_type:.*/radio_type: "sx1302"/' "$CONFIG_FILE"
    echo "✓ radio_type updated in config"
else
    cat >> "$CONFIG_FILE" << 'EOF'

radio_type: "sx1302"

sx1302:
  com_path: "/dev/spidev0.0"
  sx1261_spi_path: "/dev/spidev0.1"
EOF
    echo "✓ radio_type and sx1302 section added to config"
fi

# ── 4. Fix ownership and restart ──────────────────────────────────────────────

chown -R repeater:repeater "$HAL_DIR" 2>/dev/null || true

echo ""
echo "Restarting service..."
systemctl restart pymc-repeater

sleep 2
if systemctl is-active pymc-repeater >/dev/null 2>&1; then
    echo "✓ Service running"
    echo ""
    echo "=== SX1302/WM1302 setup complete ==="
    echo "Monitor logs: sudo journalctl -u pymc-repeater -f"
else
    echo "✗ Service failed to start"
    echo "  sudo journalctl -u pymc-repeater -n 50"
    exit 1
fi
