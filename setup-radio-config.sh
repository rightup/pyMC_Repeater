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

# Step 0.5: Radio type (SX1262 hardware vs KISS modem)
echo "=== Step 0.5: Select Radio Type ==="
echo ""
echo "  1) SX1262 hardware (SPI LoRa module - Raspberry Pi HAT, etc.)"
echo "  2) KISS modem (serial TNC - requires pyMC_core with KISS support)"
echo ""
read -p "Select radio type (1 or 2): " radio_type_sel

if [ "$radio_type_sel" = "2" ]; then
    RADIO_TYPE="kiss"
    hw_key="kiss"
    hw_name="KISS modem"
    echo "Selected: $hw_name"
    echo ""
else
    RADIO_TYPE="sx1262"
    echo "Selected: SX1262 hardware"
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
fi

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
freq_hz=$(awk "BEGIN {printf \"%.0f\", $freq * 1000000}")
bw_hz=$(awk "BEGIN {printf \"%.0f\", $bw * 1000}")


echo ""
echo "Selected: $title"
echo "Frequency: ${freq}MHz, SF: $sf, BW: $bw, CR: $cr"
echo ""

# KISS modem: prompt for serial port and baud rate
if [ "$RADIO_TYPE" = "kiss" ]; then
    echo "=== KISS Modem Settings ==="
    echo ""
    default_port="/dev/ttyUSB0"
    read -p "Serial port [$default_port]: " kiss_port
    kiss_port=${kiss_port:-$default_port}
    default_baud="9600"
    read -p "Baud rate [$default_baud]: " kiss_baud
    kiss_baud=${kiss_baud:-$default_baud}
    echo "KISS: port=$kiss_port, baud_rate=$kiss_baud"
    echo ""
fi

# Ensure config file exists (create from example if missing)
if [ ! -f "$CONFIG_FILE" ]; then
    if [ -f "$CONFIG_DIR/config.yaml.example" ]; then
        cp "$CONFIG_DIR/config.yaml.example" "$CONFIG_FILE"
        echo "Created $CONFIG_FILE from config.yaml.example"
    elif [ -f "$SCRIPT_DIR/config.yaml.example" ]; then
        cp "$SCRIPT_DIR/config.yaml.example" "$CONFIG_FILE"
        echo "Created $CONFIG_FILE from $SCRIPT_DIR/config.yaml.example"
    else
        echo "Error: Config file not found at $CONFIG_FILE"
        echo "Copy config.yaml.example to config.yaml or run from a directory that has it."
        exit 1
    fi
fi

echo "Updating configuration..."

# Radio type (sx1262 or kiss)
if grep -q "^radio_type:" "$CONFIG_FILE"; then
    sed "${SED_OPTS[@]}" "s/^radio_type:.*/radio_type: $RADIO_TYPE/" "$CONFIG_FILE"
else
    { echo "radio_type: $RADIO_TYPE"; cat "$CONFIG_FILE"; } > "$CONFIG_FILE.tmp" && mv "$CONFIG_FILE.tmp" "$CONFIG_FILE"
fi

# Repeater name
sed "${SED_OPTS[@]}" "s/^  node_name:.*/  node_name: \"$repeater_name\"/" "$CONFIG_FILE"

# Radio settings - using converted Hz values
sed "${SED_OPTS[@]}" "s/^  frequency:.*/  frequency: $freq_hz/" "$CONFIG_FILE"
sed "${SED_OPTS[@]}" "s/^  spreading_factor:.*/  spreading_factor: $sf/" "$CONFIG_FILE"
sed "${SED_OPTS[@]}" "s/^  bandwidth:.*/  bandwidth: $bw_hz/" "$CONFIG_FILE"
sed "${SED_OPTS[@]}" "s/^  coding_rate:.*/  coding_rate: $cr/" "$CONFIG_FILE"

# KISS modem: update kiss section
if [ "$RADIO_TYPE" = "kiss" ]; then
    if grep -q "^kiss:" "$CONFIG_FILE"; then
        sed "${SED_OPTS[@]}" "s/^  port:.*/  port: \"$kiss_port\"/" "$CONFIG_FILE"
        sed "${SED_OPTS[@]}" "s/^  baud_rate:.*/  baud_rate: $kiss_baud/" "$CONFIG_FILE"
    else
        printf '\nkiss:\n  port: "%s"\n  baud_rate: %s\n' "$kiss_port" "$kiss_baud" >> "$CONFIG_FILE"
    fi
fi

# Extract hardware-specific settings from radio-settings.json (SX1262 only)
if [ "$RADIO_TYPE" = "sx1262" ]; then
echo "Extracting hardware configuration from $HARDWARE_CONFIG..."

# Use jq to extract all fields from the selected hardware
hw_config=$(jq ".hardware.\"$hw_key\"" "$HARDWARE_CONFIG" 2>/dev/null)

if [ -z "$hw_config" ] || [ "$hw_config" == "null" ]; then
    echo "Warning: Could not extract hardware config from JSON, using defaults"
else
    # Extract each field and update config.yaml
    radio_type=$(echo "$hw_config" | jq -r '.radio_type // empty')
    vid=$(echo "$hw_config" | jq -r '.vid // empty')
    pid=$(echo "$hw_config" | jq -r '.pid // empty')

    bus_id=$(echo "$hw_config" | jq -r '.bus_id // empty')
    cs_id=$(echo "$hw_config" | jq -r '.cs_id // empty')
    cs_pin=$(echo "$hw_config" | jq -r '.cs_pin // empty')
    reset_pin=$(echo "$hw_config" | jq -r '.reset_pin // empty')
    busy_pin=$(echo "$hw_config" | jq -r '.busy_pin // empty')
    irq_pin=$(echo "$hw_config" | jq -r '.irq_pin // empty')
    txen_pin=$(echo "$hw_config" | jq -r '.txen_pin // empty')
    rxen_pin=$(echo "$hw_config" | jq -r '.rxen_pin // empty')
    txled_pin=$(echo "$hw_config" | jq -r '.txled_pin // empty')
    rxled_pin=$(echo "$hw_config" | jq -r '.rxled_pin // empty')
    tx_power=$(echo "$hw_config" | jq -r '.tx_power // empty')
    preamble_length=$(echo "$hw_config" | jq -r '.preamble_length // empty')
    is_waveshare=$(echo "$hw_config" | jq -r '.is_waveshare // empty')
    use_dio3_tcxo=$(echo "$hw_config" | jq -r '.use_dio3_tcxo // empty')
    dio3_tcxo_voltage=$(echo "$hw_config" | jq -r '.dio3_tcxo_voltage // empty')
    use_dio2_rf=$(echo "$hw_config" | jq -r '.use_dio2_rf // empty')

    # Update radio_type + optional CH341 section
    if [ -n "$radio_type" ]; then
        if grep -q "^radio_type:" "$CONFIG_FILE"; then
            sed "${SED_OPTS[@]}" "s/^radio_type:.*/radio_type: $radio_type/" "$CONFIG_FILE"
        else
            # Append if missing (config.yaml.example includes it, so this is a fallback)
            printf "\nradio_type: %s\n" "$radio_type" >> "$CONFIG_FILE"
        fi
    fi

    if [ -n "$vid" ] || [ -n "$pid" ]; then
        # Ensure ch341 section exists
        if ! grep -q "^ch341:" "$CONFIG_FILE"; then
            # Append if missing (fallback)
            printf "\nch341:\n  vid: %s\n  pid: %s\n" "${vid:-6790}" "${pid:-21778}" >> "$CONFIG_FILE"
        fi

        [ -n "$vid" ] && sed "${SED_OPTS[@]}" "s/^  vid:.*/  vid: $vid/" "$CONFIG_FILE"
        [ -n "$pid" ] && sed "${SED_OPTS[@]}" "s/^  pid:.*/  pid: $pid/" "$CONFIG_FILE"
    fi

    # Update sx1262 section in config.yaml (2-space indentation)
    [ -n "$bus_id" ] && sed "${SED_OPTS[@]}" "s/^  bus_id:.*/  bus_id: $bus_id/" "$CONFIG_FILE"
    [ -n "$cs_id" ] && sed "${SED_OPTS[@]}" "s/^  cs_id:.*/  cs_id: $cs_id/" "$CONFIG_FILE"
    [ -n "$cs_pin" ] && sed "${SED_OPTS[@]}" "s/^  cs_pin:.*/  cs_pin: $cs_pin/" "$CONFIG_FILE"
    [ -n "$reset_pin" ] && sed "${SED_OPTS[@]}" "s/^  reset_pin:.*/  reset_pin: $reset_pin/" "$CONFIG_FILE"
    [ -n "$busy_pin" ] && sed "${SED_OPTS[@]}" "s/^  busy_pin:.*/  busy_pin: $busy_pin/" "$CONFIG_FILE"
    [ -n "$irq_pin" ] && sed "${SED_OPTS[@]}" "s/^  irq_pin:.*/  irq_pin: $irq_pin/" "$CONFIG_FILE"
    [ -n "$txen_pin" ] && sed "${SED_OPTS[@]}" "s/^  txen_pin:.*/  txen_pin: $txen_pin/" "$CONFIG_FILE"
    [ -n "$rxen_pin" ] && sed "${SED_OPTS[@]}" "s/^  rxen_pin:.*/  rxen_pin: $rxen_pin/" "$CONFIG_FILE"

    # Handle LED pins - add if missing, update if present
    if [ -n "$txled_pin" ]; then
        if grep -q "^  txled_pin:" "$CONFIG_FILE"; then
            sed "${SED_OPTS[@]}" "s/^  txled_pin:.*/  txled_pin: $txled_pin/" "$CONFIG_FILE"
        else
            # Add txled_pin after rxen_pin
            sed "${SED_OPTS[@]}" "/^  rxen_pin:.*/a\\  txled_pin: $txled_pin" "$CONFIG_FILE"
        fi
    fi

    if [ -n "$rxled_pin" ]; then
        if grep -q "^  rxled_pin:" "$CONFIG_FILE"; then
            sed "${SED_OPTS[@]}" "s/^  rxled_pin:.*/  rxled_pin: $rxled_pin/" "$CONFIG_FILE"
        else
            # Add rxled_pin after txled_pin
            sed "${SED_OPTS[@]}" "/^  txled_pin:.*/a\\  rxled_pin: $rxled_pin" "$CONFIG_FILE"
        fi
    fi

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
        if grep -q "^  use_dio3_tcxo:" "$CONFIG_FILE"; then
            sed "${SED_OPTS[@]}" "s/^  use_dio3_tcxo:.*/  use_dio3_tcxo: true/" "$CONFIG_FILE"
        else
            # Add use_dio3_tcxo after rxled_pin (or rxen_pin if no LED pins)
            if grep -q "^  rxled_pin:" "$CONFIG_FILE"; then
                sed "${SED_OPTS[@]}" "/^  rxled_pin:.*/a\\  use_dio3_tcxo: true" "$CONFIG_FILE"
            else
                sed "${SED_OPTS[@]}" "/^  rxen_pin:.*/a\\  use_dio3_tcxo: true" "$CONFIG_FILE"
            fi
        fi
    elif [ "$use_dio3_tcxo" == "false" ]; then
        if grep -q "^  use_dio3_tcxo:" "$CONFIG_FILE"; then
            sed "${SED_OPTS[@]}" "s/^  use_dio3_tcxo:.*/  use_dio3_tcxo: false/" "$CONFIG_FILE"
        else
            # Add use_dio3_tcxo after rxled_pin (or rxen_pin if no LED pins)
            if grep -q "^  rxled_pin:" "$CONFIG_FILE"; then
                sed "${SED_OPTS[@]}" "/^  rxled_pin:.*/a\\  use_dio3_tcxo: false" "$CONFIG_FILE"
            else
                sed "${SED_OPTS[@]}" "/^  rxen_pin:.*/a\\  use_dio3_tcxo: false" "$CONFIG_FILE"
            fi
        fi
    fi

    # Update dio3_tcxo_voltage (only meaningful if use_dio3_tcxo is true)
    if [ -n "$dio3_tcxo_voltage" ]; then
        if grep -q "^  dio3_tcxo_voltage:" "$CONFIG_FILE"; then
            sed "${SED_OPTS[@]}" "s/^  dio3_tcxo_voltage:.*/  dio3_tcxo_voltage: $dio3_tcxo_voltage/" "$CONFIG_FILE"
        else
            # Add after use_dio3_tcxo
            sed "${SED_OPTS[@]}" "/^  use_dio3_tcxo:.*/a\\  dio3_tcxo_voltage: $dio3_tcxo_voltage" "$CONFIG_FILE"
        fi
    fi

    # Update use_dio2_rf flag
    if [ "$use_dio2_rf" == "true" ] || [ "$use_dio2_rf" == "false" ]; then
        if grep -q "^  use_dio2_rf:" "$CONFIG_FILE"; then
            sed "${SED_OPTS[@]}" "s/^  use_dio2_rf:.*/  use_dio2_rf: $use_dio2_rf/" "$CONFIG_FILE"
        else
            # Add after dio3_tcxo_voltage if present, otherwise after use_dio3_tcxo
            if grep -q "^  dio3_tcxo_voltage:" "$CONFIG_FILE"; then
                sed "${SED_OPTS[@]}" "/^  dio3_tcxo_voltage:.*/a\\  use_dio2_rf: $use_dio2_rf" "$CONFIG_FILE"
            else
                sed "${SED_OPTS[@]}" "/^  use_dio3_tcxo:.*/a\\  use_dio2_rf: $use_dio2_rf" "$CONFIG_FILE"
            fi
        fi
    fi
fi
fi

# Cleanup
rm -f /tmp/radio_*_* "$CONFIG_FILE.bak"

echo "Configuration updated successfully!"
echo ""
echo "Applied Configuration:"
echo "  Repeater Name: $repeater_name"
echo "  Radio Type: $RADIO_TYPE"
echo "  Hardware: $hw_name ($hw_key)"
echo "  Frequency: ${freq}MHz (${freq_hz}Hz)"
echo "  Spreading Factor: $sf"
echo "  Bandwidth: ${bw}kHz (${bw_hz}Hz)"
echo "  Coding Rate: $cr"
if [ "$RADIO_TYPE" = "kiss" ]; then
    echo "  KISS Port: $kiss_port"
    echo "  KISS Baud Rate: $kiss_baud"
fi
echo ""
echo "Hardware GPIO Configuration:"
if [ "$RADIO_TYPE" = "sx1262" ] && [ -n "$bus_id" ]; then
    echo "  Bus ID: $bus_id"
    echo "  Chip Select: $cs_id (pin $cs_pin)"
    echo "  Reset Pin: $reset_pin"
    echo "  Busy Pin: $busy_pin"
    echo "  IRQ Pin: $irq_pin"
    [ "$txen_pin" != "-1" ] && echo "  TX Enable Pin: $txen_pin"
    [ "$rxen_pin" != "-1" ] && echo "  RX Enable Pin: $rxen_pin"
    [ "$txled_pin" != "-1" ] && echo "  TX LED Pin: $txled_pin"
    [ "$rxled_pin" != "-1" ] && echo "  RX LED Pin: $rxled_pin"
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
