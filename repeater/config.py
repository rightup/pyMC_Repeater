import base64
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, overload

import yaml

from repeater.exceptions import ConfigurationError
from repeater.modem_config import normalize_modem_config
from repeater.policy_engine import default_policy_engine_config

logger = logging.getLogger("Config")


def _resolve_policy_config_path(config: Dict[str, Any], config_path: str) -> Path:
    policy_section = config.get("policy", {}) if isinstance(config.get("policy"), dict) else {}
    configured = policy_section.get("policy_file") or "policy.yaml"

    base_dir = Path(config_path).expanduser().resolve().parent
    p = Path(str(configured)).expanduser()
    if not p.is_absolute():
        p = (base_dir / p).resolve()
    return p


def _load_policy_engine_config(config: Dict[str, Any], config_path: str) -> Dict[str, Any]:
    policy_path = _resolve_policy_config_path(config, config_path)
    defaults = default_policy_engine_config()

    if not policy_path.exists():
        logger.info("Policy file not found at %s, policy engine disabled", policy_path)
        config["policy_engine"] = defaults
        config["policy_file_path"] = str(policy_path)
        return config

    try:
        with open(policy_path) as f:
            loaded = yaml.safe_load(f) or {}

        if isinstance(loaded, dict) and isinstance(loaded.get("policy_engine"), dict):
            policy_cfg = loaded.get("policy_engine")
        elif isinstance(loaded, dict):
            policy_cfg = loaded
        else:
            policy_cfg = {}

        merged = dict(defaults)
        if isinstance(policy_cfg, dict):
            merged.update(policy_cfg)

        if not isinstance(merged.get("rules"), list):
            merged["rules"] = []
        if not isinstance(merged.get("objects"), dict):
            merged["objects"] = {}

        config["policy_engine"] = merged
        config["policy_file_path"] = str(policy_path)
        logger.info("Loaded policy config from %s", policy_path)
        return config
    except Exception as e:
        logger.warning(
            "Failed to load policy config from %s: %s. Policy engine disabled.",
            policy_path,
            e,
        )
        config["policy_engine"] = defaults
        config["policy_file_path"] = str(policy_path)
        return config


class NullRadio:
    """No-op radio used when radio_type disables hardware initialization."""

    def __init__(self):
        self._rx_callback = None

    def begin(self):
        return True

    async def send(self, data: bytes):
        raise RuntimeError("Radio is disabled (radio_type is null/none)")

    async def wait_for_rx(self) -> bytes:
        import asyncio

        while True:
            await asyncio.sleep(3600)

    def sleep(self):
        return None

    def get_last_rssi(self) -> int:
        return 0

    def get_last_snr(self) -> float:
        return 0.0

    def set_rx_callback(self, callback):
        self._rx_callback = callback

    def check_radio_health(self):
        return True


class BaselineCrcCounterRadio:
    """Radio proxy that exposes CRC errors relative to repeater startup.

    openHop Modem transports report the modem firmware's cumulative CRC counter.
    The SX1262 wrapper's counter starts at process startup, which lets the engine
    persist deltas without knowing the radio backend. Mirror that wrapper flow
    here by normalizing the modem's raw counter at the transport boundary.
    """

    def __init__(self, radio):
        self._radio = radio
        self._crc_baseline = self._read_raw_crc_count()

    def __getattr__(self, name: str):
        return getattr(self._radio, name)

    @property
    def crc_error_count(self) -> int:
        current = self._read_raw_crc_count()
        if self._crc_baseline <= 0 and current > 0:
            self._crc_baseline = current
            return 0
        return max(0, current - self._crc_baseline)

    @crc_error_count.setter
    def crc_error_count(self, value: Any) -> None:
        setattr(self._radio, "crc_error_count", value)

    def _read_raw_crc_count(self) -> int:
        try:
            return int(getattr(self._radio, "crc_error_count", 0) or 0)
        except (TypeError, ValueError):
            return 0


def resolve_storage_dir(
    config: Dict[str, Any],
    *,
    config_path: Optional[str] = None,
    default: str = "/var/lib/openhop_repeater",
) -> Path:

    storage_dir_cfg = (
        config.get("storage", {}).get("storage_dir") or config.get("storage_dir") or default
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
        config_path = os.getenv(
            "OPENHOP_REPEATER_CONFIG",
            os.getenv("PYMC_REPEATER_CONFIG", "/etc/openhop_repeater/config.yaml"),
        )

    # Check if config file exists
    if not Path(config_path).exists():
        raise ConfigurationError(
            f"Configuration file not found: {config_path}\n"
            f"Please create a config file. Example: \n"
            f"  sudo cp {Path(config_path).parent}/config.yaml.example {config_path}\n"
            f"  sudo nano {config_path}"
        )

    # Load from file - no defaults, all settings must be in config file
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
            config = normalize_modem_config(config)
            logger.info(f"Loaded config from {config_path}")
    except Exception as e:
        raise ConfigurationError(f"Failed to load configuration from {config_path}: {e}") from e

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

    if "http" not in config:
        config["http"] = {
            "enabled": True,
            "host": "0.0.0.0",  # nosec B104 - intentional LAN bind default
            "port": 8000,
        }

    if "glass" not in config:
        config["glass"] = {
            "enabled": False,
            "base_url": "http://localhost:8080",
            "inform_interval_seconds": 30,
            "request_timeout_seconds": 10,
            "verify_tls": True,
            "api_token": None,
            "cert_store_dir": "/etc/openhop_repeater/glass",
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
            "host": "",
            "port": 80,
            "endpoint": "/api/stats",
            "scheme": "http",
            "username": "admin",
            "password": None,
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

    if "sensors" not in config:
        config["sensors"] = {
            "enabled": False,
            "poll_interval_seconds": 30.0,
            "auto_install_packages": False,
            "definitions": [],
        }

    # Ensure repeater.security exists with defaults for upgrades from older configs
    if "repeater" not in config:
        config["repeater"] = {}
    if "security" not in config["repeater"]:
        logger.warning(
            "No 'security' section found under 'repeater' in config. "
            "Adding secure placeholders — complete setup wizard before login."
        )
        config["repeater"]["security"] = {
            "max_clients": 1,
            "admin_password": None,
            "guest_password": None,
            "allow_read_only": False,
            "jwt_secret": None,
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

    env_log_level = os.getenv("OPENHOP_REPEATER_LOG_LEVEL", os.getenv("PYMC_REPEATER_LOG_LEVEL"))
    if env_log_level:
        if "logging" not in config:
            config["logging"] = {}
        config["logging"]["level"] = env_log_level

    config = _load_policy_engine_config(config, config_path)

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
        config_path = os.getenv(
            "OPENHOP_REPEATER_CONFIG",
            os.getenv("PYMC_REPEATER_CONFIG", "/etc/openhop_repeater/config.yaml"),
        )

    try:
        config_data = normalize_modem_config(config_data)
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
        system_key_path = Path("/etc/openhop_repeater/identity.key")
        if system_key_path.exists():
            key_path = system_key_path
        else:
            # Follow XDG spec
            xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
            if xdg_config_home:
                config_dir = Path(xdg_config_home) / "openhop_repeater"
            else:
                config_dir = Path.home() / ".config" / "openhop_repeater"
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

    # Generate new key via MeshCore-compatible keygen; store 32-byte private scalar.
    from repeater.keygen import generate_meshcore_keypair

    _, private_key = generate_meshcore_keypair()
    key = private_key[:32]

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
    board_config = normalize_modem_config(board_config)

    @overload
    def _parse_int(value, *, default: None = None) -> Optional[int]: ...

    @overload
    def _parse_int(value, *, default: int) -> int: ...

    def _parse_int(value, *, default=None):
        if value is None:
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            return int(value.strip().rstrip(","), 0)
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

    radio_type_raw = board_config.get("radio_type")
    if radio_type_raw is None:
        radio_type = "none"
    else:
        radio_type = str(radio_type_raw).lower().strip()

    if radio_type in ("", "none", "null", "disabled", "off", "no_radio"):
        logger.warning("Radio disabled by configuration (radio_type=%r)", radio_type_raw)
        return NullRadio()

    if radio_type == "kiss-modem":
        radio_type = "kiss"

    if radio_type in ("sx1262", "sx1262_ch341"):
        from openhop_core.hardware.sx1262_wrapper import SX1262Radio

        # Get radio and SPI configuration - all settings must be in config file
        spi_config = board_config.get("sx1262")
        if not spi_config:
            raise ValueError("Missing 'sx1262' section in configuration file")

        radio_config = board_config.get("radio")
        if not radio_config:
            raise ValueError("Missing 'radio' section in configuration file")

        # CH341 integration: per-radio USB adapter selection.
        # For multi-radio, pass distinct bus/address/serial_number so each
        # SX1262Radio owns its own CH341 SPI+GPIO pair (no process singleton).
        ch341_spi = None
        ch341_gpio = None
        if radio_type == "sx1262_ch341":
            ch341_cfg = board_config.get("ch341")
            if not ch341_cfg:
                raise ValueError("Missing 'ch341' section in configuration file")

            from openhop_core.hardware.lora.LoRaRF.SX126x import set_spi_transport
            from openhop_core.hardware.transports.ch341_spi_transport import CH341SPITransport

            vid = _parse_int(ch341_cfg.get("vid"), default=0x1A86)
            pid = _parse_int(ch341_cfg.get("pid"), default=0x5512)
            bus = _parse_int(ch341_cfg.get("bus"), default=None)
            address = _parse_int(
                ch341_cfg.get("address", ch341_cfg.get("device_address")),
                default=None,
            )
            serial_number = ch341_cfg.get("serial_number") or ch341_cfg.get("serial")
            if serial_number is not None:
                serial_number = str(serial_number).strip() or None

            # Multi-radio entries should not stomp the process-global defaults.
            # Legacy single-radio keeps global install for older code paths.
            multi_context = bool(board_config.get("_radio_id")) or bool(
                board_config.get("_ch341_per_instance")
            )
            ch341_spi = CH341SPITransport(
                vid=vid,
                pid=pid,
                bus=bus,
                address=address,
                serial_number=serial_number,
                auto_setup_gpio=True,
                set_as_global_gpio=not multi_context,
            )
            ch341_gpio = ch341_spi.gpio_manager
            if not multi_context:
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
            "sync_word": _parse_int(radio_config.get("sync_word", 0x12)),
        }

        en_pin = _parse_int(spi_config.get("en_pin"), default=None)
        en_pins = _parse_int_list(spi_config.get("en_pins"))
        if en_pin is not None:
            combined_config["en_pin"] = en_pin
        if en_pins is not None:
            combined_config["en_pins"] = en_pins

        # Add optional GPIO parameters if specified in config
        # These wont be supported by older versions of openhop_core
        if "gpio_chip" in spi_config:
            combined_config["gpio_chip"] = _parse_int(spi_config["gpio_chip"], default=0)
        if "use_gpiod_backend" in spi_config:
            combined_config["use_gpiod_backend"] = spi_config["use_gpiod_backend"]
        if "radio_timing_delay" in spi_config:
            combined_config["radio_timing_delay"] = float(spi_config["radio_timing_delay"])

        # Always construct a fresh instance so multi-radio configs do not
        # share/reuse a singleton SX1262 handle.
        if ch341_spi is not None:
            combined_config["spi_transport"] = ch341_spi
        if ch341_gpio is not None:
            combined_config["gpio_manager"] = ch341_gpio
        radio = SX1262Radio(**combined_config)

        if hasattr(radio, "_initialized") and not radio._initialized:
            try:
                radio.begin()
            except RuntimeError as e:
                raise RuntimeError(f"Failed to initialize SX1262 radio: {e}") from e

        return radio

    elif radio_type == "kiss":
        try:
            from openhop_core.hardware.kiss_modem_wrapper import KissModemWrapper
        except ImportError:
            try:
                from openhop_core.hardware.kiss_serial_wrapper import (
                    KissSerialWrapper as KissModemWrapper,
                )
            except ImportError:
                raise RuntimeError(
                    "KISS modem support requires openhop-core with KISS support. "
                    "Install your fork with: pip install -e /path/to/openhop-core"
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
            "preamble_length": int(radio_cfg.get("preamble_length", 32)),
        }

        # Optional KISS key-up / CSMA tuning, forwarded to the modem firmware (via
        # SetHardware) only when present so the wrapper keeps its own defaults otherwise.
        # For a host-managed repeater the engine already staggers retransmits, so the
        # firmware's p-persistent CSMA backoff is usually redundant; set
        # kiss_persistence: 255 to transmit as soon as the channel is clear.
        for _key in ("tx_delay_ms", "kiss_persistence", "kiss_slottime_ms", "kiss_txtail_ms"):
            if kiss_config.get(_key) is not None:
                radio_config[_key] = int(kiss_config[_key])
        if kiss_config.get("kiss_full_duplex") is not None:
            radio_config["kiss_full_duplex"] = bool(kiss_config["kiss_full_duplex"])

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

    elif radio_type == "modem_tcp":
        try:
            from openhop_core.hardware.tcp_radio import TCPLoRaRadio
        except ImportError:
            raise RuntimeError(
                "modem_tcp radio requires openhop-core >= the release that includes "
                "the openhop-core release with TCP modem support. "
                "Reinstall the [hardware] extra to pick it up."
            ) from None

        tcp_cfg = board_config.get("modem_tcp")
        if not tcp_cfg:
            raise ValueError(
                "Missing 'modem_tcp' section in configuration file for radio_type: modem_tcp"
            )

        host = tcp_cfg.get("host")
        if not host:
            raise ValueError("Missing 'host' in 'modem_tcp' section (modem hostname or LAN IP)")

        radio_cfg = board_config.get("radio") or {}
        radio = TCPLoRaRadio(
            host=host,
            port=int(tcp_cfg.get("port", 5055)),
            token=tcp_cfg.get("token", ""),
            connect_timeout=float(tcp_cfg.get("connect_timeout", 5.0)),
            frequency=int(radio_cfg.get("frequency", 869618000)),
            bandwidth=int(radio_cfg.get("bandwidth", 62500)),
            spreading_factor=int(radio_cfg.get("spreading_factor", 8)),
            coding_rate=int(radio_cfg.get("coding_rate", 8)),
            tx_power=int(radio_cfg.get("tx_power", 22)),
            sync_word=_parse_int(radio_cfg.get("sync_word", 0x12), default=0x12),
            preamble_length=int(radio_cfg.get("preamble_length", 16)),
            lbt_enabled=bool(tcp_cfg.get("lbt_enabled", True)),
            lbt_max_attempts=int(tcp_cfg.get("lbt_max_attempts", 5)),
        )

        try:
            radio.begin()
        except Exception as e:
            raise RuntimeError(f"Failed to initialize modem_tcp radio: {e}") from e

        return BaselineCrcCounterRadio(radio)

    elif radio_type == "modem_usb":
        try:
            from openhop_core.hardware.usb_radio import USBLoRaRadio
        except ImportError:
            raise RuntimeError(
                "modem_usb radio requires openhop-core >= the release that includes "
                "the openhop-core release with TCP modem support. "
                "Reinstall the [hardware] extra to pick it up."
            ) from None

        usb_cfg = board_config.get("modem_usb")
        if not usb_cfg:
            raise ValueError(
                "Missing 'modem_usb' section in configuration file for radio_type: modem_usb"
            )

        port = usb_cfg.get("port")
        if not port:
            raise ValueError("Missing 'port' in 'modem_usb' section (e.g. /dev/ttyACM0)")

        radio_cfg = board_config.get("radio") or {}
        radio = USBLoRaRadio(
            port=port,
            baudrate=int(usb_cfg.get("baudrate", 921600)),
            frequency=int(radio_cfg.get("frequency", 869618000)),
            bandwidth=int(radio_cfg.get("bandwidth", 62500)),
            spreading_factor=int(radio_cfg.get("spreading_factor", 8)),
            coding_rate=int(radio_cfg.get("coding_rate", 8)),
            tx_power=int(radio_cfg.get("tx_power", 22)),
            sync_word=_parse_int(radio_cfg.get("sync_word", 0x12), default=0x12),
            preamble_length=int(radio_cfg.get("preamble_length", 16)),
            lbt_enabled=bool(usb_cfg.get("lbt_enabled", True)),
            lbt_max_attempts=int(usb_cfg.get("lbt_max_attempts", 5)),
        )

        try:
            radio.begin()
        except Exception as e:
            raise RuntimeError(f"Failed to initialize modem_usb radio: {e}") from e

        return BaselineCrcCounterRadio(radio)

    raise RuntimeError(
        f"Unknown radio type: {radio_type}. "
        "Supported: sx1262, sx1262_ch341, kiss (or kiss-modem), modem_tcp, modem_usb"
    )


def _merge_radio_entry(global_config: dict, entry: dict) -> dict:
    """Build a board_config dict for one radios[] entry.

    Entry may override radio_type and hardware sections; shared top-level
    ``radio`` air settings are inherited unless the entry supplies its own.
    """
    if not isinstance(entry, dict):
        raise ValueError("each radios[] entry must be a mapping")

    radio_id = entry.get("id") or entry.get("radio_id")
    if not radio_id:
        raise ValueError("each radios[] entry requires an 'id'")

    merged = dict(global_config)
    for key in (
        "radio_type",
        "radio",
        "sx1262",
        "ch341",
        "kiss",
        "modem_tcp",
        "modem_usb",
    ):
        if key in entry:
            merged[key] = entry[key]

    if "radio_type" not in entry and "radio_type" not in global_config:
        raise ValueError(f"radios[] entry {radio_id!r} missing radio_type")

    merged["_radio_id"] = str(radio_id)
    # Hint for CH341 path: do not install process-global SPI/GPIO defaults.
    merged["_ch341_per_instance"] = True
    return merged


def _apply_fabric_tx_mode(fabric, mode: str) -> None:
    """Map simple tx_mode strings onto RFFabric.set_tx_selector.

    Modes:
    - default: always TX ``default_radio`` (or first registered).
    - sticky:  TX on the radio that last received (reply on same band).
    - bridge:  TX on a *different* radio than last RX (dual-freq backhaul).
               Designed for two radios (local <-> link). With 3+ radios, uses
               the first other id in registration order. With no prior RX,
               falls back to default_radio.
    """
    mode_l = (mode or "default").strip().lower()
    if mode_l in ("", "default"):
        fabric.set_tx_selector(None)
        return

    if mode_l == "sticky":

        def _sticky(_data: bytes):
            rid = getattr(fabric, "_last_rx_radio_id", None)
            if rid and rid in fabric.radios:
                return rid
            return fabric.default_radio_id

        fabric.set_tx_selector(_sticky)
        return

    if mode_l == "bridge":

        def _bridge(_data: bytes):
            ids = list(fabric.radios.keys())
            if len(ids) < 2:
                return fabric.default_radio_id
            last = getattr(fabric, "_last_rx_radio_id", None)
            if last and last in fabric.radios:
                for rid in ids:
                    if rid != last:
                        return rid
            return fabric.default_radio_id

        fabric.set_tx_selector(_bridge)
        return

    raise ValueError(f"Unknown fabric.tx_mode={mode!r}. Supported: default, sticky, bridge")


def _apply_fabric_origin_tx(fabric, mode: str) -> None:
    """Forward fabric.origin_tx onto the core RFFabric.

    Modes:
    - default: locally originated packets TX ``default_radio`` (current
               behaviour).
    - all:     fan origins out to every registered radio, sequentially in
               registration order. Bridge topologies need this: tx_mode
               only steers *forwards* (RX radio -> the other radio); an
               origin packet has no RX side, so without fan-out it leaves
               on a single radio and never crosses the link.
    """
    mode_l = (mode or "default").strip().lower()
    if mode_l in ("", "default"):
        return
    if mode_l != "all":
        raise ValueError(f"Unknown fabric.origin_tx={mode!r}. Supported: default, all")
    if not hasattr(fabric, "set_origin_tx"):
        raise RuntimeError(
            "fabric.origin_tx requires openhop-core with origin fan-out support; "
            "upgrade openhop-core."
        )
    fabric.set_origin_tx(mode_l)


def build_radio_stack(config: dict):
    """Build single- or multi-radio stack for the repeater.

    - No ``radios:`` -> legacy ``get_radio_for_board(config)``.
    - ``radios:`` list -> FabricRadio over N physical radios.
    - ``fabric:`` knobs: default_radio, tx_mode (default|sticky|bridge), use_fabric.

    Returns (radio, meta).
    """
    radios_cfg = config.get("radios")
    fabric_cfg = config.get("fabric") if isinstance(config.get("fabric"), dict) else {}
    use_fabric = bool(fabric_cfg.get("use_fabric", False))
    tx_mode = str(fabric_cfg.get("tx_mode", "default"))
    origin_tx = str(fabric_cfg.get("origin_tx", "default"))
    default_radio = fabric_cfg.get("default_radio") or fabric_cfg.get("default_radio_id")

    meta = {
        "mode": "single",
        "radio_ids": [],
        "default_radio": None,
        "tx_mode": tx_mode,
        "origin_tx": origin_tx,
        "fabric": False,
    }

    if isinstance(radios_cfg, list) and len(radios_cfg) > 0:
        try:
            from openhop_core.rf_fabric import FabricRadio
        except ImportError as exc:
            raise RuntimeError(
                "radios: requires openhop-core with RF Fabric support. "
                "Install/upgrade openhop-core (Phase 2+)."
            ) from exc

        pairs = []
        for entry in radios_cfg:
            merged = _merge_radio_entry(config, entry)
            rid = merged.pop("_radio_id")
            physical = get_radio_for_board(merged)
            pairs.append((physical, rid))

        default_id = str(default_radio) if default_radio else pairs[0][1]
        radio = FabricRadio(radios=pairs, default_radio_id=default_id)
        _apply_fabric_tx_mode(radio.fabric, tx_mode)
        _apply_fabric_origin_tx(radio.fabric, origin_tx)

        meta.update(
            {
                "mode": "multi",
                "radio_ids": [rid for _, rid in pairs],
                "default_radio": default_id,
                "fabric": True,
            }
        )
        return radio, meta

    physical = get_radio_for_board(config)
    meta["radio_ids"] = ["radio0"]
    meta["default_radio"] = "radio0"

    if use_fabric:
        try:
            from openhop_core.rf_fabric import FabricRadio
        except ImportError as exc:
            raise RuntimeError(
                "fabric.use_fabric requires openhop-core with RF Fabric support."
            ) from exc
        rid = str(default_radio or "radio0")
        radio = FabricRadio(radio=physical, radio_id=rid, default_radio_id=rid)
        _apply_fabric_tx_mode(radio.fabric, tx_mode)
        _apply_fabric_origin_tx(radio.fabric, origin_tx)
        meta.update(
            {
                "mode": "single_fabric",
                "radio_ids": [rid],
                "default_radio": rid,
                "fabric": True,
            }
        )
        return radio, meta

    return physical, meta
