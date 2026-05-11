import base64
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger("Config")


def resolve_storage_dir(
    config: Dict[str, Any],
    *,
    config_path: Optional[str] = None,
    default: str = "/var/lib/pymc_repeater",
) -> Path:

    storage_dir_cfg = (
        config.get("storage", {}).get("storage_dir")
        or config.get("storage_dir")
        or default
    )

    storage_dir = Path(str(storage_dir_cfg)).expanduser()
    if not storage_dir.is_absolute():
        if config_path:
            base_dir = Path(config_path).expanduser().resolve().parent
            storage_dir = (base_dir / storage_dir).resolve()
        else:
            storage_dir = storage_dir.resolve()

    return storage_dir


def get_node_info(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract node name, radio configuration, and MQTT settings from config.
    
    Args:
        config: Configuration dictionary
        
    Returns:
        Dictionary with node_name, radio_config, and MQTT configuration
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
    
    # Handle getting the config from mqtt brokers, falling back to letsmesh if it doesn't exist
    mqtt_config = config.get("mqtt_brokers", config.get("letsmesh", {}))
    
    return {
        "node_name": node_name,
        "radio_config": radio_config_str,
        "iata_code": mqtt_config.get("iata_code", "TEST"),
        "status_interval": mqtt_config.get("status_interval", 60),
        "model": mqtt_config.get("model", "PyMC-Repeater"),
        "email": mqtt_config.get("email", ""),
        "owner": mqtt_config.get("owner", ""),
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

    storage_dir = resolve_storage_dir(config, config_path=config_path)
    if "storage" not in config or not isinstance(config.get("storage"), dict):
        config["storage"] = {}
    config["storage"]["storage_dir"] = str(storage_dir)

    if config.get("storage_dir"):
        logger.warning(
            "Deprecated config key 'storage_dir' detected; prefer 'storage.storage_dir'."
        )

    if "mesh" not in config:
        config["mesh"] = {}

    if "glass" not in config:
        config["glass"] = {
            "enabled": False,
            "base_url": "http://localhost:8080",
            "inform_interval_seconds": 30,
            "request_timeout_seconds": 10,
            "verify_tls": True,
            "api_token": "",
            "cert_store_dir": "/etc/pymc_repeater/glass",
        }

    if "gps" not in config:
        config["gps"] = {
            "enabled": False,
            "api_fallback_to_config_location": True,
            "advertise_gps_location": False,
            "location_precision_digits": None,
            "source": "serial",
            "device": "/dev/serial0",
            "baud_rate": 9600,
            "read_timeout_seconds": 1.0,
            "reconnect_interval_seconds": 5.0,
            "stale_after_seconds": 10.0,
            "retain_sentences": 25,
            "validate_checksum": True,
            "require_checksum": False,
            "time_sync_enabled": True,
            "time_sync_interval_seconds": 3600.0,
            "time_sync_min_offset_seconds": 1.0,
            "time_sync_min_valid_year": 2020,
            "persist_gps_fix_to_config": False,
            "persist_gps_fix_interval_seconds": 600.0,
        }

    # Ensure repeater.security exists with defaults for upgrades from older configs
    if "repeater" not in config:
        config["repeater"] = {}
    if "security" not in config["repeater"]:
        logger.warning(
            "No 'security' section found under 'repeater' in config. "
            "Adding defaults — please review and update passwords."
        )
        config["repeater"]["security"] = {
            "max_clients": 1,
            "admin_password": "admin123",
            "guest_password": "guest123",
            "allow_read_only": False,
            "jwt_secret": "",
            "jwt_expiry_minutes": 60,
        }

    # Only auto-generate identity_key if not provided under repeater section
    if "identity_key" not in config["repeater"]:
        # Check if identity_file is specified
        identity_file = config["repeater"].get("identity_file")
        if identity_file:
            config["repeater"]["identity_key"] = _load_or_create_identity_key(path=identity_file)
        else:
            config["repeater"]["identity_key"] = _load_or_create_identity_key()

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
            backup_path = config_file.with_suffix(".yaml.backup")
            config_file.rename(backup_path)
            logger.info(f"Created backup at {backup_path}")

        # Save new config (allow_unicode=True so emojis etc. are not escaped as \U0001F47E)
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                config_data,
                f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
                width=1000000,
            )

        logger.info(f"Saved configuration to {config_path}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to save configuration: {e}")
        return False


def update_unscoped_flood_policy(allow: bool, config_path: Optional[str] = None) -> bool:
    """
    Update the unscoped flood policy in the configuration.
    
    Args:
        allow: True to allow unscoped flooding, False to deny
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
        config["mesh"]["unscoped_flood_allow"] = allow
        
        # Save updated config
        return save_config(config, config_path)
        
    except Exception as e:
        logger.error(f"Failed to update unscoped flood policy: {e}")
        return False


def _load_or_create_identity_key(path: Optional[str] = None) -> bytes:

    if path is None:
        # Check system-wide location first (matches config.yaml location)
        system_key_path = Path("/etc/pymc_repeater/identity.key")
        if system_key_path.exists():
            key_path = system_key_path
        else:
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
                if len(key) not in (32, 64):
                    raise ValueError(f"Invalid key length: {len(key)}, expected 32 or 64")
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

    def _parse_int(value, *, default=None) -> int:
        if value is None:
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            return int(value.strip().rstrip(','), 0)
        raise ValueError(f"Invalid int value type: {type(value)}")

    def _parse_int_list(value):
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            return [_parse_int(item) for item in value]
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped[0] == "[" and stripped[-1] == "]":
                stripped = stripped[1:-1]
            return [_parse_int(item) for item in stripped.split(",") if item.strip()]
        raise ValueError(f"Invalid int list value type: {type(value)}")

    radio_type = board_config.get("radio_type", "sx1262").lower().strip()
    if radio_type == "kiss-modem":
        radio_type = "kiss"

    if radio_type in ("sx1262", "sx1262_ch341"):
        from pymc_core.hardware.sx1262_wrapper import SX1262Radio

        # Get radio and SPI configuration - all settings must be in config file
        spi_config = board_config.get("sx1262")
        if not spi_config:
            raise ValueError("Missing 'sx1262' section in configuration file")

        radio_config = board_config.get("radio")
        if not radio_config:
            raise ValueError("Missing 'radio' section in configuration file")

        # CH341 integration: swap SPI transport + GPIO backend to CH341
        if radio_type == "sx1262_ch341":
            ch341_cfg = board_config.get("ch341")
            if not ch341_cfg:
                raise ValueError("Missing 'ch341' section in configuration file")

            from pymc_core.hardware.lora.LoRaRF.SX126x import set_spi_transport
            from pymc_core.hardware.transports.ch341_spi_transport import CH341SPITransport

            vid = _parse_int(ch341_cfg.get("vid"), default=0x1A86)
            pid = _parse_int(ch341_cfg.get("pid"), default=0x5512)

            # Create CH341 transport (also configures CH341 GPIO manager globally)
            ch341_spi = CH341SPITransport(vid=vid, pid=pid, auto_setup_gpio=True)
            set_spi_transport(ch341_spi)

        combined_config = {
            "bus_id": _parse_int(spi_config["bus_id"]),
            "cs_id": _parse_int(spi_config["cs_id"]),
            "cs_pin": _parse_int(spi_config["cs_pin"]),
            "reset_pin": _parse_int(spi_config["reset_pin"]),
            "busy_pin": _parse_int(spi_config["busy_pin"]),
            "irq_pin": _parse_int(spi_config["irq_pin"]),
            "txen_pin": _parse_int(spi_config["txen_pin"]),
            "rxen_pin": _parse_int(spi_config["rxen_pin"]),
            "txled_pin": _parse_int(spi_config.get("txled_pin", -1), default=-1),
            "rxled_pin": _parse_int(spi_config.get("rxled_pin", -1), default=-1),
            "use_dio3_tcxo": spi_config.get("use_dio3_tcxo", False),
            "dio3_tcxo_voltage": float(spi_config.get("dio3_tcxo_voltage", 1.8)),
            "use_dio2_rf": spi_config.get("use_dio2_rf", False),
            "is_waveshare": spi_config.get("is_waveshare", False),
            "frequency": int(radio_config["frequency"]),
            "tx_power": radio_config["tx_power"],
            "spreading_factor": radio_config["spreading_factor"],
            "bandwidth": int(radio_config["bandwidth"]),
            "coding_rate": radio_config["coding_rate"],
            "preamble_length": radio_config["preamble_length"],
            "sync_word": radio_config["sync_word"],
        }

        en_pin = _parse_int(spi_config.get("en_pin"), default=None)
        en_pins = _parse_int_list(spi_config.get("en_pins"))
        if en_pin is not None:
            combined_config["en_pin"] = en_pin
        if en_pins is not None:
            combined_config["en_pins"] = en_pins

        # Add optional GPIO parameters if specified in config
        # These wont be supported by older versions of pymc_core
        if "gpio_chip" in spi_config:
            combined_config["gpio_chip"] = _parse_int(spi_config["gpio_chip"], default=0)
        if "use_gpiod_backend" in spi_config:
            combined_config["use_gpiod_backend"] = spi_config["use_gpiod_backend"]
        if "radio_timing_delay" in spi_config:
            combined_config["radio_timing_delay"] = float(spi_config["radio_timing_delay"])

        radio = SX1262Radio.get_instance(**combined_config)

        if hasattr(radio, "_initialized") and not radio._initialized:
            try:
                radio.begin()
            except RuntimeError as e:
                raise RuntimeError(f"Failed to initialize SX1262 radio: {e}") from e

        return radio

    elif radio_type == "kiss":
        try:
            from pymc_core.hardware.kiss_modem_wrapper import KissModemWrapper
        except ImportError:
            try:
                from pymc_core.hardware.kiss_serial_wrapper import (
                    KissSerialWrapper as KissModemWrapper,
                )
            except ImportError:
                raise RuntimeError(
                    "KISS modem support requires pyMC_core with KISS support. "
                    "Install your fork with: pip install -e /path/to/pyMC_core"
                ) from None

        kiss_config = board_config.get("kiss")
        if not kiss_config:
            raise ValueError("Missing 'kiss' section in configuration file for radio_type: kiss")

        port = kiss_config.get("port")
        if not port:
            raise ValueError("Missing 'port' in 'kiss' section (e.g. /dev/ttyUSB0)")

        baudrate = int(kiss_config.get("baud_rate", 115200))
        radio_cfg = board_config.get("radio") or {}
        radio_config = {
            "frequency": int(radio_cfg.get("frequency", 869618000)),
            "bandwidth": int(radio_cfg.get("bandwidth", 62500)),
            "spreading_factor": int(radio_cfg.get("spreading_factor", 8)),
            "coding_rate": int(radio_cfg.get("coding_rate", 8)),
            "tx_power": int(radio_cfg.get("tx_power", 14)),
        }
        radio = KissModemWrapper(
            port=port,
            baudrate=baudrate,
            radio_config=radio_config,
            auto_configure=True,
        )

        if hasattr(radio, "begin"):
            try:
                radio.begin()
            except Exception as e:
                raise RuntimeError(f"Failed to initialize KISS modem: {e}") from e

        return radio

    raise RuntimeError(
        f"Unknown radio type: {radio_type}. Supported: sx1262, sx1262_ch341, kiss (or kiss-modem)"
    )
