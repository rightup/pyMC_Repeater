import base64
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger("Config")


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
