#!/bin/bash
# Convert MeshCore firmware 64-byte private key to pyMC_Repeater format
#
# Usage: sudo ./convert_firmware_key.sh <64-byte-hex-key> [--output-format=<yaml|identity>] [config-path]
# Example: sudo ./convert_firmware_key.sh 987BDA619630197351F2B3040FD19B2EE0DEE357DD69BBEEE295786FA78A4D5F298B0BF1B7DE73CBC23257CDB2C562F5033DF58C232916432948B0F6BA4448F2

set -e

if [ $# -eq 0 ]; then
    echo "Error: No key provided"
    echo ""
    echo "Usage: sudo $0 <64-byte-hex-key> [--output-format=<yaml|identity>] [config-path]"
    echo ""
    echo "This script imports a 64-byte MeshCore firmware private key into"
    echo "pyMC_Repeater for full identity compatibility."
    echo ""
    echo "The 64-byte key format: [32-byte scalar][32-byte nonce]"
    echo "  - Enables same node address as firmware device"
    echo "  - Supports signing using MeshCore/orlp ed25519 algorithm"
    echo "  - Fully compatible with pyMC_core LocalIdentity"
    echo ""
    echo "Arguments:"
    echo "  --output-format: Optional output format (yaml|identity, default: yaml)"
    echo "    yaml     - Store in config.yaml (embedded binary)"
    echo "    identity - Save to identity.key file (base64 encoded)"
    echo "  config-path: Optional path to config.yaml (default: /etc/pymc_repeater/config.yaml)"
    echo ""
    echo "Examples:"
    echo "  # Save to config.yaml (default)"
    echo "  sudo $0 987BDA619630197351F2B3040FD19B2EE0DEE357DD69BBEEE295786FA78A4D5F298B0BF1B7DE73CBC23257CDB2C562F5033DF58C232916432948B0F6BA4448F2"
    echo ""
    echo "  # Save to identity.key file"
    echo "  sudo $0 987BDA619630197351F2B3040FD19B2EE0DEE357DD69BBEEE295786FA78A4D5F298B0BF1B7DE73CBC23257CDB2C562F5033DF58C232916432948B0F6BA4448F2 --output-format=identity"
    exit 1
fi

# Check if running with sudo/root
if [ "$EUID" -ne 0 ]; then
    echo "Error: This script must be run with sudo to update config.yaml"
    echo "Usage: sudo $0 <64-byte-hex-key>"
    exit 1
fi

FULL_KEY="$1"
OUTPUT_FORMAT="yaml"  # Default format
CONFIG_PATH=""

# Parse arguments
shift  # Remove the key argument
while [ $# -gt 0 ]; do
    case "$1" in
        --output-format=*)
            OUTPUT_FORMAT="${1#*=}"
            ;;
        *)
            CONFIG_PATH="$1"
            ;;
    esac
    shift
done

# Validate output format
if [ "$OUTPUT_FORMAT" != "yaml" ] && [ "$OUTPUT_FORMAT" != "identity" ]; then
    echo "Error: Invalid output format '$OUTPUT_FORMAT'. Must be 'yaml' or 'identity'"
    exit 1
fi

# Set default config path if not provided
if [ -z "$CONFIG_PATH" ]; then
    CONFIG_PATH="/etc/pymc_repeater/config.yaml"
fi

# Validate hex string
if ! [[ "$FULL_KEY" =~ ^[0-9a-fA-F]+$ ]]; then
    echo "Error: Key must be a hexadecimal string"
    exit 1
fi

KEY_LEN=${#FULL_KEY}

if [ "$KEY_LEN" -ne 128 ]; then
    echo "Error: Key must be 64 bytes (128 hex characters), got $KEY_LEN characters"
    exit 1
fi

# Check if config/identity file location exists (only for yaml format or if saving identity.key)
if [ "$OUTPUT_FORMAT" = "yaml" ]; then
    # Check if config exists
    if [ ! -f "$CONFIG_PATH" ]; then
        echo "Error: Config file not found: $CONFIG_PATH"
        exit 1
    fi
else
    # For identity format, use system-wide location matching config.yaml
    IDENTITY_DIR="/etc/pymc_repeater"
    IDENTITY_PATH="$IDENTITY_DIR/identity.key"
fi

echo "=== MeshCore Firmware Key Import ==="
echo ""
echo "Output format: $OUTPUT_FORMAT"
if [ "$OUTPUT_FORMAT" = "yaml" ]; then
    echo "Target file: $CONFIG_PATH"
else
    echo "Target file: $IDENTITY_PATH"
fi
echo ""
echo "Input (64-byte firmware key):"
echo "  $FULL_KEY"
echo ""

# Verify public key derivation and import key using Python with safe YAML handling
python3 <<EOF
import sys
import yaml
import base64
import hashlib
import os
from pathlib import Path

# Import the key
key_hex = "$FULL_KEY"
key_bytes = bytes.fromhex(key_hex)
output_format = "$OUTPUT_FORMAT"

# Verify with pyMC if available
try:
    from nacl.bindings import crypto_scalarmult_ed25519_base_noclamp

    scalar = key_bytes[:32]
    pubkey = crypto_scalarmult_ed25519_base_noclamp(scalar)

    print(f"Derived public key: {pubkey.hex()}")

    # Calculate address (MeshCore uses first byte of pubkey directly, not SHA256)
    address = pubkey[0]
    print(f"Node address: 0x{address:02x}")
    print()

except ImportError:
    print("Warning: PyNaCl not available, skipping verification")
    print()

if output_format == "yaml":
    # Save to config.yaml
    config_path = Path("$CONFIG_PATH")
    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"Error loading config: {e}")
        sys.exit(1)

    # Check for existing key
    if 'mesh' in config and 'identity_key' in config['mesh']:
        existing = config['mesh']['identity_key']
        if isinstance(existing, bytes):
            print(f"WARNING: Existing identity_key found ({len(existing)} bytes)")
        else:
            print(f"WARNING: Existing identity_key found")
        print()

    # Ensure mesh section exists
    if 'mesh' not in config:
        config['mesh'] = {}

    # Store the full 64-byte key
    config['mesh']['identity_key'] = key_bytes

    # Save config atomically
    backup_path = f"{config_path}.backup.{Path(config_path).stat().st_mtime_ns}"
    import shutil
    shutil.copy2(config_path, backup_path)
    print(f"Created backup: {backup_path}")

    try:
        with open(config_path, 'w') as f:
            yaml.safe_dump(config, f, default_flow_style=False, allow_unicode=True)
        print(f"✓ Successfully updated {config_path}")
        print()
    except Exception as e:
        print(f"Error writing config: {e}")
        shutil.copy2(backup_path, config_path)
        print(f"Restored from backup")
        sys.exit(1)

else:
    # Save to identity.key file
    identity_path = Path("$IDENTITY_PATH")
    
    # Create directory if it doesn't exist
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Check for existing identity.key
    if identity_path.exists():
        print(f"WARNING: Existing identity.key found at {identity_path}")
        backup_path = identity_path.with_suffix('.key.backup')
        import shutil
        shutil.copy2(identity_path, backup_path)
        print(f"Created backup: {backup_path}")
        print()
    
    # Save as base64-encoded
    try:
        with open(identity_path, 'wb') as f:
            f.write(base64.b64encode(key_bytes))
        os.chmod(identity_path, 0o600)  # Restrict permissions
        print(f"✓ Successfully saved to {identity_path}")
        print(f"✓ File permissions set to 0600 (owner read/write only)")
        print()
    except Exception as e:
        print(f"Error writing identity.key: {e}")
        sys.exit(1)
    
    # Update config.yaml to remove embedded identity_key so it uses the file
    config_path = Path("$CONFIG_PATH")
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f) or {}
            
            # Check if identity_key exists in config
            if 'mesh' in config and 'identity_key' in config['mesh']:
                print(f"Updating {config_path} to use identity.key file...")
                
                # Create backup
                backup_path = f"{config_path}.backup.{Path(config_path).stat().st_mtime_ns}"
                import shutil
                shutil.copy2(config_path, backup_path)
                print(f"Created backup: {backup_path}")
                
                # Remove identity_key from config
                del config['mesh']['identity_key']
                
                # Save updated config
                with open(config_path, 'w') as f:
                    yaml.safe_dump(config, f, default_flow_style=False, allow_unicode=True)
                
                print(f"✓ Removed embedded identity_key from {config_path}")
                print(f"✓ Config will now use {identity_path}")
                print()
            else:
                print(f"✓ Config file already configured to use identity.key file")
                print()
                
        except Exception as e:
            print(f"Warning: Could not update config.yaml: {e}")
            print(f"You may need to manually remove 'identity_key' from {config_path}")
            print()
    else:
        print(f"Note: Config file not found at {config_path}")
        print(f"      Identity will be loaded from {identity_path}")
        print()

EOF

if [ $? -ne 0 ]; then
    echo "Error: Python script failed"
    exit 1
fi

# Offer to restart service (only relevant for yaml format)
if [ "$OUTPUT_FORMAT" = "yaml" ]; then
    if systemctl is-active --quiet pymc-repeater 2>/dev/null; then
        read -p "Restart pymc-repeater service now? (yes/no): " RESTART
        if [ "$RESTART" = "yes" ]; then
            systemctl restart pymc-repeater
            echo "✓ Service restarted"
            echo ""
            echo "Check logs for new identity:"
            echo "  sudo journalctl -u pymc-repeater -f | grep -i 'identity\|hash'"
        else
            echo "Remember to restart the service:"
            echo "  sudo systemctl restart pymc-repeater"
        fi
    else
        echo "Note: pymc-repeater service is not running"
        echo "Start it with: sudo systemctl start pymc-repeater"
    fi
else
    echo "Identity key saved to file."
    echo ""
    if systemctl is-active --quiet pymc-repeater 2>/dev/null; then
        read -p "Restart pymc-repeater service now? (yes/no): " RESTART
        if [ "$RESTART" = "yes" ]; then
            systemctl restart pymc-repeater
            echo "✓ Service restarted"
            echo ""
            echo "Check logs for new identity:"
            echo "  sudo journalctl -u pymc-repeater -f | grep -i 'identity\|hash'"
        else
            echo "Remember to restart the service:"
            echo "  sudo systemctl restart pymc-repeater"
        fi
    else
        echo "Note: pymc-repeater service is not running"
        echo "Start it with: sudo systemctl start pymc-repeater"
    fi
fi
