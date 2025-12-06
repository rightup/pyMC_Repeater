import base64
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger("Config")


def get_node_info(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract node name, radio configuration, and LetsMesh settings from config.
    
    Args:
        config: Configuration dictionary
        
    Returns:
        Dictionary with node_name, radio_config, and LetsMesh configuration
    """
    node_name = config.get("repeater", {}).get("node_name", "PyMC-Repeater")
    radio_config = config.get("radio", {})
    radio_freq = radio_config.get("frequency", 0.0)
    radio_bw = radio_config.get("bandwidth", 0.0)
    radio_sf = radio_config.get("spreading_factor", 7)
    radio_cr = radio_config.get("coding_rate", 5)
    # Format frequency in MHz and bandwidth in kHz
    radio_freq_mhz = radio_freq / 1_000_000
    radio_bw_khz = radio_bw / 1_000
    radio_config_str = f"{radio_freq_mhz},{radio_bw_khz},{radio_sf},{radio_cr}"
    
    letsmesh_config = config.get("letsmesh", {})
    
    from pymc_core.protocol.utils import PAYLOAD_TYPES
    
    disallowed_types = letsmesh_config.get("disallowed_packet_types", [])
    type_name_map = {name: code for code, name in PAYLOAD_TYPES.items()}
    
    disallowed_hex = [type_name_map.get(name.upper(), None) for name in disallowed_types]
    disallowed_hex = [val for val in disallowed_hex if val is not None]  # Filter out invalid names
    
    return {
        "node_name": node_name,
        "radio_config": radio_config_str,
        "iata_code": letsmesh_config.get("iata_code", "TEST"),
        "broker_index": letsmesh_config.get("broker_index", 0),
        "status_interval": letsmesh_config.get("status_interval", 60),
        "model": letsmesh_config.get("model", "PyMC-Repeater"),
        "disallowed_packet_types": disallowed_hex,
        "email": letsmesh_config.get("email", ""),
        "owner": letsmesh_config.get("owner", "")
    }


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    if config_path is None:
        config_path = os.getenv("PYMC_REPEATER_CONFIG", "/etc/pymc_repeater/config.yaml")

    # Check if config file exists
    if not Path(config_path).exists():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}\n"
            f"Please create a config file. Example: \n"
            f"  sudo cp {Path(config_path).parent}/config.yaml.example {config_path}\n"
            f"  sudo nano {config_path}"
        )

    # Load from file - no defaults, all settings must be in config file
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
            logger.info(f"Loaded config from {config_path}")
    except Exception as e:
        raise RuntimeError(f"Failed to load configuration from {config_path}: {e}") from e

    if "mesh" not in config:
        config["mesh"] = {}

    # Only auto-generate identity_key if not provided
    if "identity_key" not in config["mesh"]:
        config["mesh"]["identity_key"] = _load_or_create_identity_key()

    if os.getenv("PYMC_REPEATER_LOG_LEVEL"):
        if "logging" not in config:
            config["logging"] = {}
        config["logging"]["level"] = os.getenv("PYMC_REPEATER_LOG_LEVEL")

    return config


def save_config(config_data: Dict[str, Any], config_path: Optional[str] = None) -> bool:
    """
    Save configuration to YAML file.
    
    Args:
        config_data: Configuration dictionary to save
        config_path: Path to config file (uses default if None)
        
    Returns:
        True if successful, False otherwise
    """
    if config_path is None:
        config_path = os.getenv("PYMC_REPEATER_CONFIG", "/etc/pymc_repeater/config.yaml")
    
    try:
        # Create backup of existing config
        config_file = Path(config_path)
        if config_file.exists():
            backup_path = config_file.with_suffix('.yaml.backup')
            config_file.rename(backup_path)
            logger.info(f"Created backup at {backup_path}")
        
        # Save new config
        with open(config_path, 'w') as f:
            yaml.safe_dump(config_data, f, default_flow_style=False, sort_keys=False)
        
        logger.info(f"Saved configuration to {config_path}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to save configuration: {e}")
        return False


def update_global_flood_policy(allow: bool, config_path: Optional[str] = None) -> bool:
    """
    Update the global flood policy in the configuration.
    
    Args:
        allow: True to allow flooding globally, False to deny
        config_path: Path to config file (uses default if None)
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Load current config
        config = load_config(config_path)
        
        # Ensure mesh section exists
        if "mesh" not in config:
            config["mesh"] = {}
        
        # Set global flood policy
        config["mesh"]["global_flood_allow"] = allow
        
        # Save updated config
        return save_config(config, config_path)
        
    except Exception as e:
        logger.error(f"Failed to update global flood policy: {e}")
        return False


def _load_or_create_identity_key(path: Optional[str] = None) -> bytes:

    if path is None:
        # Follow XDG spec
        xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config_home:
            config_dir = Path(xdg_config_home) / "pymc_repeater"
        else:
            config_dir = Path.home() / ".config" / "pymc_repeater"
        key_path = config_dir / "identity.key"
    else:
        key_path = Path(path)

    key_path.parent.mkdir(parents=True, exist_ok=True)

    if key_path.exists():
        try:
            with open(key_path, "rb") as f:
                encoded = f.read()
                key = base64.b64decode(encoded)
                if len(key) != 32:
                    raise ValueError(f"Invalid key length: {len(key)}, expected 32")
                logger.info(f"Loaded existing identity key from {key_path}")
                return key
        except Exception as e:
            logger.warning(f"Failed to load identity key: {e}")

    # Generate new random key
    key = os.urandom(32)

    # Save it
    try:
        with open(key_path, "wb") as f:
            f.write(base64.b64encode(key))
        os.chmod(key_path, 0o600)  # Restrict permissions
        logger.info(f"Generated and stored new identity key at {key_path}")
    except Exception as e:
        logger.warning(f"Failed to save identity key: {e}")

    return key


def get_radio_for_board(board_config: dict):

    radio_type = board_config.get("radio_type", "sx1262").lower()

    if radio_type == "sx1262":
        from pymc_core.hardware.sx1262_wrapper import SX1262Radio

        # Get radio and SPI configuration - all settings must be in config file
        spi_config = board_config.get("sx1262")
        if not spi_config:
            raise ValueError("Missing 'sx1262' section in configuration file")

        radio_config = board_config.get("radio")
        if not radio_config:
            raise ValueError("Missing 'radio' section in configuration file")

        # Build config with required fields - no defaults
        combined_config = {
            "bus_id": spi_config["bus_id"],
            "cs_id": spi_config["cs_id"],
            "cs_pin": spi_config["cs_pin"],
            "reset_pin": spi_config["reset_pin"],
            "busy_pin": spi_config["busy_pin"],
            "irq_pin": spi_config["irq_pin"],
            "txen_pin": spi_config["txen_pin"],
            "rxen_pin": spi_config["rxen_pin"],
            "txled_pin": spi_config.get("txled_pin", -1),
            "rxled_pin": spi_config.get("rxled_pin", -1),
            "use_dio3_tcxo": spi_config.get("use_dio3_tcxo", False),
            "dio3_tcxo_voltage": spi_config.get("dio3_tcxo_voltage", 1.4),
            "is_waveshare": spi_config.get("is_waveshare", False),
            "frequency": int(radio_config["frequency"]),
            "tx_power": radio_config["tx_power"],
            "spreading_factor": radio_config["spreading_factor"],
            "bandwidth": int(radio_config["bandwidth"]),
            "coding_rate": radio_config["coding_rate"],
            "preamble_length": radio_config["preamble_length"],
            "sync_word": radio_config["sync_word"],
        }

        radio = SX1262Radio.get_instance(**combined_config)

        if hasattr(radio, "_initialized") and not radio._initialized:
            try:
                radio.begin()
            except RuntimeError as e:
                raise RuntimeError(f"Failed to initialize SX1262 radio: {e}") from e

        return radio

    else:
        raise RuntimeError(f"Unknown radio type: {radio_type}. Supported: sx1262")
