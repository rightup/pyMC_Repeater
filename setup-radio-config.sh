#!/bin/bash
# Radio configuration setup script for pyMC Repeater

CONFIG_DIR="${1:-.}"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARDWARE_CONFIG="$SCRIPT_DIR/radio-settings.json"

# Detect OS and set appropriate sed parameters
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS
    SED_OPTS=(-i '')
else
    # Linux
    SED_OPTS=(-i)
fi

echo "=== pyMC Repeater Radio Configuration ==="
echo ""

# Step 0: Repeater Name
echo "=== Step 0: Set Repeater Name ==="
echo ""

# Read existing repeater name from config if it exists
existing_name=""
if [ -f "$CONFIG_FILE" ]; then
    existing_name=$(grep "^\s*node_name:" "$CONFIG_FILE" | sed 's/.*node_name:\s*"\?\([^"]*\)"\?$/\1/' | head -1)
fi

# Generate random name with format pyRptXXXX (where X is random digit)
if [ -n "$existing_name" ]; then
    default_name="$existing_name"
    prompt_text="Enter repeater name [$default_name] (press Enter to keep)"
else
    random_num=$((RANDOM % 10000))
    default_name=$(printf "pyRpt%04d" $random_num)
    prompt_text="Enter repeater name [$default_name]"
fi

read -p "$prompt_text: " repeater_name
repeater_name=${repeater_name:-$default_name}

echo "Repeater name: $repeater_name"
echo ""
echo "=== Step 1: Select Hardware ==="
echo ""

if [ ! -f "$HARDWARE_CONFIG" ]; then
    echo "Error: Hardware configuration file not found at $HARDWARE_CONFIG"
    exit 1
fi

# Parse hardware options from radio-settings.json
hw_index=0
declare -a hw_keys
declare -a hw_names

# Extract hardware keys and names using grep and sed
hw_data=$(grep -o '"[^"]*":\s*{' "$HARDWARE_CONFIG" | grep -v hardware | sed 's/"\([^"]*\)".*/\1/' | while read hw_key; do
    hw_name=$(grep -A 1 "\"$hw_key\"" "$HARDWARE_CONFIG" | grep "\"name\"" | sed 's/.*"name":\s*"\([^"]*\)".*/\1/')
    if [ -n "$hw_name" ]; then
        echo "$hw_key|$hw_name"
    fi
done)

while IFS='|' read -r hw_key hw_name; do
    if [ -n "$hw_key" ] && [ -n "$hw_name" ]; then
        echo "  $((hw_index + 1))) $hw_name ($hw_key)"
        hw_keys[$hw_index]="$hw_key"
        hw_names[$hw_index]="$hw_name"
        ((hw_index++))
    fi
done <<< "$hw_data"

if [ "$hw_index" -eq 0 ]; then
    echo "Error: No hardware configurations found"
    exit 1
fi

echo ""
read -p "Select hardware (1-$hw_index): " hw_selection

if ! [ "$hw_selection" -ge 1 ] 2>/dev/null || [ "$hw_selection" -gt "$hw_index" ]; then
    echo "Error: Invalid selection"
    exit 1
fi

selected_hw=$((hw_selection - 1))
hw_key="${hw_keys[$selected_hw]}"
hw_name="${hw_names[$selected_hw]}"

echo "Selected: $hw_name"
echo ""

# Step 2: Radio Settings Selection
echo "=== Step 2: Select Radio Settings ==="
echo ""

# Fetch config from API with 5 second timeout, fallback to local file
echo "Fetching radio settings from API..."
API_RESPONSE=$(curl -s --max-time 5 https://api.meshcore.nz/api/v1/config 2>/dev/null)

if [ -z "$API_RESPONSE" ]; then
    echo "Warning: Failed to fetch configuration from API (timeout or error)"
    echo "Using local radio presets file..."
    
    LOCAL_PRESETS="$SCRIPT_DIR/radio-presets.json"
    if [ ! -f "$LOCAL_PRESETS" ]; then
        echo "Error: Local radio presets file not found at $LOCAL_PRESETS"
        exit 1
    fi
    
    API_RESPONSE=$(cat "$LOCAL_PRESETS")
    if [ -z "$API_RESPONSE" ]; then
        echo "Error: Failed to read local radio presets file"
        exit 1
    fi
fi

# Parse JSON entries - one per line, extracting each field
SETTINGS=$(echo "$API_RESPONSE" | grep -o '{[^{}]*"title"[^{}]*"coding_rate"[^{}]*}' | sed 's/.*"title":"\([^"]*\)".*/\1/' | while read title; do
    entry=$(echo "$API_RESPONSE" | grep -o "{[^{}]*\"title\":\"$title\"[^{}]*\"coding_rate\"[^{}]*}")
    desc=$(echo "$entry" | sed 's/.*"description":"\([^"]*\)".*/\1/')
    freq=$(echo "$entry" | sed 's/.*"frequency":"\([^"]*\)".*/\1/')
    sf=$(echo "$entry" | sed 's/.*"spreading_factor":"\([^"]*\)".*/\1/')
    bw=$(echo "$entry" | sed 's/.*"bandwidth":"\([^"]*\)".*/\1/')
    cr=$(echo "$entry" | sed 's/.*"coding_rate":"\([^"]*\)".*/\1/')
    echo "$title|$desc|$freq|$sf|$bw|$cr"
done)

if [ -z "$SETTINGS" ]; then
    echo "Error: Could not parse radio settings from API response"
    exit 1
fi

# Display menu
echo "Available Radio Settings:"
echo ""

index=0
while IFS='|' read -r title desc freq sf bw cr; do
    printf "  %2d) %-35s ----> %7.3fMHz / SF%s / BW%s / CR%s\n" $((index + 1)) "$title" "$freq" "$sf" "$bw" "$cr"

    # Store values in files to avoid subshell issues
    echo "$title" > /tmp/radio_title_$index
    echo "$freq" > /tmp/radio_freq_$index
    echo "$sf" > /tmp/radio_sf_$index
    echo "$bw" > /tmp/radio_bw_$index
    echo "$cr" > /tmp/radio_cr_$index

    ((index++))
done <<< "$SETTINGS"

echo ""
read -p "Select a radio setting (1-$index): " selection

# Validate selection
if ! [ "$selection" -ge 1 ] 2>/dev/null || [ "$selection" -gt "$index" ]; then
    echo "Error: Invalid selection"
    exit 1
fi

selected=$((selection - 1))
freq=$(cat /tmp/radio_freq_$selected 2>/dev/null)
sf=$(cat /tmp/radio_sf_$selected 2>/dev/null)
bw=$(cat /tmp/radio_bw_$selected 2>/dev/null)
cr=$(cat /tmp/radio_cr_$selected 2>/dev/null)
title=$(cat /tmp/radio_title_$selected 2>/dev/null)


# Convert frequency from MHz to Hz (handle decimal values)
freq_hz=$(echo "$freq * 1000000" | bc -l | cut -d. -f1)
bw_hz=$(echo "$bw * 1000" | bc -l | cut -d. -f1)


echo ""
echo "Selected: $title"
echo "Frequency: ${freq}MHz, SF: $sf, BW: $bw, CR: $cr"
echo ""

# Update config.yaml
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: Config file not found at $CONFIG_FILE"
    exit 1
fi

echo "Updating configuration..."

# Repeater name
sed "${SED_OPTS[@]}" "s/^  node_name:.*/  node_name: \"$repeater_name\"/" "$CONFIG_FILE"

# Radio settings - using converted Hz values
sed "${SED_OPTS[@]}" "s/^  frequency:.*/  frequency: $freq_hz/" "$CONFIG_FILE"
sed "${SED_OPTS[@]}" "s/^  spreading_factor:.*/  spreading_factor: $sf/" "$CONFIG_FILE"
sed "${SED_OPTS[@]}" "s/^  bandwidth:.*/  bandwidth: $bw_hz/" "$CONFIG_FILE"
sed "${SED_OPTS[@]}" "s/^  coding_rate:.*/  coding_rate: $cr/" "$CONFIG_FILE"

# Extract hardware-specific settings from radio-settings.json
echo "Extracting hardware configuration from $HARDWARE_CONFIG..."

# Use jq to extract all fields from the selected hardware
hw_config=$(jq ".hardware.\"$hw_key\"" "$HARDWARE_CONFIG" 2>/dev/null)

if [ -z "$hw_config" ] || [ "$hw_config" == "null" ]; then
    echo "Warning: Could not extract hardware config from JSON, using defaults"
else
    # Extract each field and update config.yaml
    bus_id=$(echo "$hw_config" | jq -r '.bus_id // empty')
    cs_id=$(echo "$hw_config" | jq -r '.cs_id // empty')
    cs_pin=$(echo "$hw_config" | jq -r '.cs_pin // empty')
    reset_pin=$(echo "$hw_config" | jq -r '.reset_pin // empty')
    busy_pin=$(echo "$hw_config" | jq -r '.busy_pin // empty')
    irq_pin=$(echo "$hw_config" | jq -r '.irq_pin // empty')
    txen_pin=$(echo "$hw_config" | jq -r '.txen_pin // empty')
    rxen_pin=$(echo "$hw_config" | jq -r '.rxen_pin // empty')
    tx_power=$(echo "$hw_config" | jq -r '.tx_power // empty')
    preamble_length=$(echo "$hw_config" | jq -r '.preamble_length // empty')
    is_waveshare=$(echo "$hw_config" | jq -r '.is_waveshare // empty')
    use_dio3_tcxo=$(echo "$hw_config" | jq -r '.use_dio3_tcxo // empty')

    # Update sx1262 section in config.yaml (2-space indentation)
    [ -n "$bus_id" ] && sed "${SED_OPTS[@]}" "s/^  bus_id:.*/  bus_id: $bus_id/" "$CONFIG_FILE"
    [ -n "$cs_id" ] && sed "${SED_OPTS[@]}" "s/^  cs_id:.*/  cs_id: $cs_id/" "$CONFIG_FILE"
    [ -n "$cs_pin" ] && sed "${SED_OPTS[@]}" "s/^  cs_pin:.*/  cs_pin: $cs_pin/" "$CONFIG_FILE"
    [ -n "$reset_pin" ] && sed "${SED_OPTS[@]}" "s/^  reset_pin:.*/  reset_pin: $reset_pin/" "$CONFIG_FILE"
    [ -n "$busy_pin" ] && sed "${SED_OPTS[@]}" "s/^  busy_pin:.*/  busy_pin: $busy_pin/" "$CONFIG_FILE"
    [ -n "$irq_pin" ] && sed "${SED_OPTS[@]}" "s/^  irq_pin:.*/  irq_pin: $irq_pin/" "$CONFIG_FILE"
    [ -n "$txen_pin" ] && sed "${SED_OPTS[@]}" "s/^  txen_pin:.*/  txen_pin: $txen_pin/" "$CONFIG_FILE"
    [ -n "$rxen_pin" ] && sed "${SED_OPTS[@]}" "s/^  rxen_pin:.*/  rxen_pin: $rxen_pin/" "$CONFIG_FILE"
    [ -n "$tx_power" ] && sed "${SED_OPTS[@]}" "s/^  tx_power:.*/  tx_power: $tx_power/" "$CONFIG_FILE"
    [ -n "$preamble_length" ] && sed "${SED_OPTS[@]}" "s/^  preamble_length:.*/  preamble_length: $preamble_length/" "$CONFIG_FILE"

    # Update is_waveshare flag
    if [ "$is_waveshare" == "true" ]; then
        sed "${SED_OPTS[@]}" "s/^  is_waveshare:.*/  is_waveshare: true/" "$CONFIG_FILE"
    else
        sed "${SED_OPTS[@]}" "s/^  is_waveshare:.*/  is_waveshare: false/" "$CONFIG_FILE"
    fi

    # Update use_dio3_tcxo flag
    if [ "$use_dio3_tcxo" == "true" ]; then
        sed "${SED_OPTS[@]}" "s/^  use_dio3_tcxo:.*/  use_dio3_tcxo: true/" "$CONFIG_FILE"
    elif [ "$use_dio3_tcxo" == "false" ]; then
        sed "${SED_OPTS[@]}" "s/^  use_dio3_tcxo:.*/  use_dio3_tcxo: false/" "$CONFIG_FILE"
    fi
fi

# Cleanup
rm -f /tmp/radio_*_* "$CONFIG_FILE.bak"

echo "Configuration updated successfully!"
echo ""
echo "Applied Configuration:"
echo "  Repeater Name: $repeater_name"
echo "  Hardware: $hw_name ($hw_key)"
echo "  Frequency: ${freq}MHz (${freq_hz}Hz)"
echo "  Spreading Factor: $sf"
echo "  Bandwidth: ${bw}kHz (${bw_hz}Hz)"
echo "  Coding Rate: $cr"
echo ""
echo "Hardware GPIO Configuration:"
if [ -n "$bus_id" ]; then
    echo "  Bus ID: $bus_id"
    echo "  Chip Select: $cs_id (pin $cs_pin)"
    echo "  Reset Pin: $reset_pin"
    echo "  Busy Pin: $busy_pin"
    echo "  IRQ Pin: $irq_pin"
    [ "$txen_pin" != "-1" ] && echo "  TX Enable Pin: $txen_pin"
    [ "$rxen_pin" != "-1" ] && echo "  RX Enable Pin: $rxen_pin"
    echo "  TX Power: $tx_power dBm"
    echo "  Preamble Length: $preamble_length"
    [ -n "$is_waveshare" ] && echo "  Waveshare: $is_waveshare"
    [ -n "$use_dio3_tcxo" ] && echo "  Use DIO3 TCXO: $use_dio3_tcxo"
fi

# Enable and start the service
SERVICE_NAME="pymc-repeater"
if systemctl list-unit-files | grep -q "^$SERVICE_NAME\.service"; then
    echo ""
    echo "Enabling and starting the $SERVICE_NAME service..."
    sudo systemctl enable "$SERVICE_NAME"
    sudo systemctl start "$SERVICE_NAME"
else
    echo ""
    echo "Service $SERVICE_NAME not found, skipping service management"
fi

echo "Setup complete. Please check the service status with 'systemctl status $SERVICE_NAME'."
