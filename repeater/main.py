import asyncio
import functools
import logging
import os
import signal
import socket
import sys
import time
from collections import OrderedDict
from typing import Optional

from repeater.companion.correlation import (
    CompanionCorrelationTracker,
    await_to_thread_outcome,
)
from repeater.companion.push_notifier import CompanionPushNotifier
from repeater.companion.utils import (
    CompanionContactCapacityError,
    CompanionStateLoadError,
    DEFAULT_COMPANION_TCP_PORT,
    DEFAULT_COMPANION_TCP_TIMEOUT_SEC,
    MAX_COMPANION_PUSH_MIN_INTERVAL_SEC,
    MAX_COMPANION_PUSH_REQUEST_TIMEOUT_SEC,
    effective_max_contacts,
    enforce_companion_contact_capacity,
    format_companion_bridge_limits,
    normalize_companion_identity_key,
    parse_companion_bridge_kwargs,
    validate_companion_bind_address,
    validate_companion_boolean_setting,
    validate_companion_legacy_adoption,
    validate_companion_listener_config,
    validate_companion_registration_name,
    validate_companion_seconds_setting,
    validate_companion_node_name,
    validate_companion_tcp_port,
    validate_companion_tcp_timeout,
)
from repeater.config import NullRadio, get_radio_for_board, load_config
from repeater.config_manager import ConfigManager
from repeater.data_acquisition.glass_handler import GlassHandler
from repeater.data_acquisition.gps_service import GPSService
from repeater.data_acquisition.sqlite_handler import (
    CompanionNamespaceCollisionError,
    CompanionStorageError,
)
from repeater.engine import RepeaterHandler
from repeater.handler_helpers import (
    AdvertHelper,
    DiscoveryHelper,
    LoginHelper,
    PathHelper,
    ProtocolRequestHelper,
    TextHelper,
    TraceHelper,
)
from repeater.identity_manager import IdentityConfigurationError, IdentityManager, IdentitySpec
from repeater.packet_router import PacketRouter
from repeater.sensors import SensorManager
from repeater.utils_packet import create_scoped_advert_packet
from repeater.web.http_server import HTTPStatsServer, _log_buffer

from openhop_core.companion.constants import MAX_PENDING_ACK_CRCS
from openhop_core.companion.radio_capabilities import resolve_max_tx_power_dbm
from openhop_core.protocol.constants import PAYLOAD_TYPE_RAW_CUSTOM

logger = logging.getLogger("RepeaterDaemon")

_COMPANION_LOAD_RETRY_DELAY_SEC = 0.5


async def _load_companion_rows_verified(
    loader, counter, kind: str, companion_hash_str: str, name: str, **loader_kwargs
):
    """Load persisted companion rows, cross-checking empty results against the table.

    A transient SQLite error at boot must not present as "no data" — the
    companion would start with an empty store and later saves would overwrite
    the persisted state. Retries once after a short delay when the load failed
    (loader returned None) or returned empty while the table has rows for this
    companion; raises CompanionStateLoadError if it still cannot load.

    Returns (rows, stored_count).
    """
    stored = 0
    for attempt in (1, 2):
        rows = loader(companion_hash_str, **loader_kwargs)
        stored = counter(companion_hash_str)
        if rows is not None and (rows or stored == 0):
            return rows, stored
        if attempt == 1:
            logger.warning(
                "Companion %s ('%s'): %s load %s but table has %d row(s); retrying once",
                companion_hash_str,
                name,
                kind,
                "failed" if rows is None else "returned empty",
                stored,
            )
            await asyncio.sleep(_COMPANION_LOAD_RETRY_DELAY_SEC)
    raise CompanionStateLoadError(
        f"Companion {companion_hash_str} ('{name}'): could not load persisted {kind} "
        f"(table has {stored} row(s)); refusing to start with an empty store"
    )


class RepeaterDaemon:
    def __init__(self, config: dict, radio=None):

        self.config = config
        self.radio = radio
        self.dispatcher = None
        self.repeater_handler = None
        self.local_hash = None
        self.local_identity = None
        self.identity_manager = None
        self.config_manager = None
        self.http_server = None
        self.trace_helper = None
        self.advert_helper = None
        self.discovery_helper = None
        self.login_helper = None
        self.text_helper = None
        self.path_helper = None
        self.protocol_request_helper = None
        self.glass_handler = None
        self.gps_service = None
        self.sensor_manager = None
        self.acl = None
        self.router = None
        self.companion_bridges: dict[int, object] = {}
        self.companion_frame_servers: list = []
        # Mobile Companion API live RF correlation (design doc §10.4): one
        # process-wide tracker (built once RepeaterHandler's cache_ttl is
        # known) and a companion_hash-string -> journal registry so the
        # duplicate_observer hook can route a correlation hit to the right
        # companion's journal without scanning companion_frame_servers.
        self.correlation_tracker = None
        # Mobile Companion API push notifier (design doc §12.2): one
        # process-wide notifier, built lazily once companion storage is known,
        # registered as a listener on every companion journal.
        self.push_notifier = None
        self.companion_journals: dict[str, object] = {}
        # Exact listener handles are retained so hot removal can unregister
        # callbacks and invalidate queued push work before components drain.
        self._companion_push_listeners: dict[str, tuple[object, object, str]] = {}
        # Opt-in RF-reception firehose (design doc §9 "Correlated vs.
        # uncorrelated receptions"): only companions with
        # settings.rf_reception_events=true get an entry here, so the common
        # case (nobody opted in) costs one empty-dict lookup per duplicate.
        self._rf_reception_journals: dict[str, object] = {}
        # Parsed once during the startup preflight; the identity loaders reuse
        # them so config parsing (and its warnings) does not run twice.
        self._room_server_specs: list[IdentitySpec] | None = None
        self._companion_specs: list[IdentitySpec] | None = None
        # Hot add/remove operations share listener ports, identity maps, and
        # radio fan-out registries.  Serialize the lifecycle so two admin
        # requests cannot both pass preflight and race to bind or unregister.
        self._companion_lifecycle_lock = asyncio.Lock()
        # Detached components whose stop failed stay unreachable but are
        # retained for a shutdown retry. Their names/hashes cannot be reused
        # in-process.
        self._retiring_companions: dict[str, dict[str, object]] = {}
        self._shutdown_started = False
        # The signal handler starts teardown in its own task while run() is
        # being cancelled. Retain that owner so run()'s finally block joins the
        # same teardown instead of returning early and letting asyncio.run()
        # cancel it.
        self._shutdown_task: asyncio.Task | None = None
        self._main_task = None
        self.radio_status = "unknown"
        self.radio_error = None

        log_level = config.get("logging", {}).get("level", "INFO")
        logging.basicConfig(
            level=getattr(logging, log_level),
            format=config.get("logging", {}).get("format"),
        )

        root_logger = logging.getLogger()
        _log_buffer.setLevel(getattr(logging, log_level))
        root_logger.addHandler(_log_buffer)

    def _configured_identity_specs(self, identity_type: str) -> list[IdentitySpec]:
        """Build valid configured local identities without registering them.

        Invalid optional room-server or companion entries retain the existing
        skip-and-log behavior.  Valid entries are returned for collision
        validation before they can create helper, database, or TCP state.
        """
        from openhop_core import LocalIdentity

        config_key = {
            "room_server": "room_servers",
            "companion": "companions",
        }[identity_type]
        configs = self.config.get("identities", {}).get(config_key) or []
        specs = []

        for identity_config in configs:
            name = identity_config.get("name")
            identity_key = identity_config.get("identity_key")
            label = "Companion" if identity_type == "companion" else "Room server"

            if not name or not identity_key:
                logger.warning("Skipping %s config: missing name or identity_key", label.lower())
                continue

            if identity_type == "companion":
                try:
                    name = validate_companion_registration_name(name)
                    raw_settings = identity_config.get("settings")
                    settings = {} if raw_settings is None else raw_settings
                    if not isinstance(settings, dict):
                        raise ValueError("companion settings must be an object")
                    frame_enabled = validate_companion_boolean_setting(
                        settings.get("frame_enabled", True),
                        "frame_enabled",
                    )
                    if frame_enabled:
                        validate_companion_tcp_port(
                            settings.get("tcp_port", DEFAULT_COMPANION_TCP_PORT)
                        )
                        validate_companion_bind_address(
                            settings.get("bind_address", "127.0.0.1")
                        )
                        validate_companion_tcp_timeout(
                            settings.get(
                                "tcp_timeout",
                                DEFAULT_COMPANION_TCP_TIMEOUT_SEC,
                            )
                        )
                    validate_companion_legacy_adoption(
                        settings.get("adopt_legacy_namespace", False)
                    )
                    validate_companion_boolean_setting(
                        settings.get("trim_contacts_on_overflow", False),
                        "trim_contacts_on_overflow",
                    )
                    validate_companion_boolean_setting(
                        settings.get("rf_reception_events", False),
                        "rf_reception_events",
                    )
                except ValueError as error:
                    logger.error("Skipping companion config %r: %s", name, error)
                    continue

            try:
                if isinstance(identity_key, str):
                    key_hex = (
                        normalize_companion_identity_key(identity_key)
                        if identity_type == "companion"
                        else identity_key
                    )
                    identity_key_bytes = bytes.fromhex(key_hex)
                elif isinstance(identity_key, bytes):
                    identity_key_bytes = identity_key
                else:
                    logger.error("%s '%s' identity_key has unknown type", label, name)
                    continue
            except ValueError as error:
                logger.error("%s '%s' identity_key invalid hex: %s", label, name, error)
                continue

            if len(identity_key_bytes) not in (32, 64):
                logger.error(
                    "%s '%s' identity_key must be 32 bytes (hex) or 64 bytes "
                    "(MeshCore firmware key)",
                    label,
                    name,
                )
                continue

            try:
                identity = LocalIdentity(seed=identity_key_bytes)
            except Exception as error:
                logger.error("Failed to create %s identity '%s': %s", label.lower(), name, error)
                continue

            specs.append(
                IdentitySpec(
                    name=name,
                    identity=identity,
                    config=identity_config,
                    identity_type=identity_type,
                )
            )

        return specs

    def _preflight_configured_local_identities(self, local_identity) -> None:
        """Validate every configured local identity before stateful setup begins.

        The parsed room-server and companion specs are cached so the identity
        loaders reuse them instead of re-parsing the config (and re-logging
        every invalid entry). Collision rules live in
        ``IdentityManager.validate_specs``; at this point the manager holds no
        registered identities, so this is a pure batch check.
        """
        self._room_server_specs = self._configured_identity_specs("room_server")
        self._companion_specs = self._configured_identity_specs("companion")
        try:
            validate_companion_listener_config(
                (spec.config for spec in self._companion_specs),
                self.config.get("http", {}),
            )
        except ValueError as exc:
            raise IdentityConfigurationError(str(exc)) from exc
        specs = [
            IdentitySpec("repeater", local_identity, self.config, "repeater"),
            *self._room_server_specs,
            *self._companion_specs,
        ]
        manager = self.identity_manager or IdentityManager(self.config)
        manager.validate_specs(specs)

    async def initialize(self):

        logger.info(f"Initializing repeater: {self.config['repeater']['node_name']}")

        # -----------------------------------------------
        # Get the actual Network IP Address
        try:
            # This looks for the IP assigned to the default hostname
            host_name = socket.gethostname()
            # We try to get the IP associated with the hostname
            self.network_ip = socket.gethostbyname(host_name)

            # If that still gives 127.0.x.x, let's try a different internal method
            if self.network_ip.startswith("127."):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                # We use a non-routable IP that doesn't require an actual connection
                s.connect(("10.255.255.255", 1))
                self.network_ip = s.getsockname()[0]
                s.close()
        except Exception as e:
            logger.warning(f"Could not determine network IP: {e}")
            self.network_ip = "Unknown"

        logger.info(f"System Network IP: {self.network_ip}")
        # -----------------------------------------------

        if self.radio is None:
            radio_type_raw = self.config.get("radio_type")
            radio_type = "none" if radio_type_raw is None else str(radio_type_raw)
            radio_type_lower = radio_type.lower().strip()
            radio_explicitly_disabled = radio_type_lower in (
                "",
                "none",
                "null",
                "disabled",
                "off",
                "no_radio",
            )
            logger.info(f"Initializing radio hardware... (radio_type={radio_type})")
            try:
                self.radio = get_radio_for_board(self.config)

                if isinstance(self.radio, NullRadio):
                    self.radio_status = "disabled" if radio_explicitly_disabled else "degraded"
                    if self.radio_status == "disabled":
                        self.radio_error = None
                    else:
                        self.radio_error = (
                            self.radio_error
                            or f"Radio type '{radio_type}' unavailable; running in no-radio mode"
                        )
                else:
                    self.radio_status = "ok"
                    self.radio_error = None

                # KISS modem: schedule RX callbacks on the event loop for thread safety
                if hasattr(self.radio, "set_event_loop"):
                    self.radio.set_event_loop(asyncio.get_running_loop())

                if hasattr(self.radio, "set_custom_cad_thresholds"):
                    # Load CAD settings from config, with defaults
                    cad_config = self.config.get("radio", {}).get("cad", {})
                    peak_threshold = cad_config.get("peak_threshold", 23)
                    min_threshold = cad_config.get("min_threshold", 11)
                    symbol_num = cad_config.get("symbol_num", 2)
                    try:
                        symbol_num = int(symbol_num)
                    except (TypeError, ValueError):
                        symbol_num = 2
                    if symbol_num not in {1, 2, 4, 8, 16}:
                        logger.warning(
                            "Invalid CAD symbol_num in config (%s); defaulting to 2",
                            symbol_num,
                        )
                        symbol_num = 2

                    self.radio.set_custom_cad_thresholds(peak=peak_threshold, min_val=min_threshold)
                    if hasattr(self.radio, "set_custom_cad_symbol_num"):
                        self.radio.set_custom_cad_symbol_num(symbol_num)
                    logger.info(
                        "CAD settings set from config: peak=%s, min=%s, symbols=%s",
                        peak_threshold,
                        min_threshold,
                        symbol_num,
                    )
                else:
                    logger.warning("Radio does not support CAD configuration")

                if hasattr(self.radio, "get_frequency"):
                    logger.info(f"Radio config - Freq: {self.radio.get_frequency():.1f}MHz")
                if hasattr(self.radio, "get_spreading_factor"):
                    logger.info(f"Radio config - SF: {self.radio.get_spreading_factor()}")
                if hasattr(self.radio, "get_bandwidth"):
                    logger.info(f"Radio config - BW: {self.radio.get_bandwidth()}kHz")
                if hasattr(self.radio, "get_coding_rate"):
                    logger.info(f"Radio config - CR: {self.radio.get_coding_rate()}")
                if hasattr(self.radio, "get_tx_power"):
                    logger.info(f"Radio config - TX Power: {self.radio.get_tx_power()}dBm")

                logger.info("Radio hardware initialized")
            except Exception as e:
                logger.error(f"Failed to initialize radio hardware: {e}")
                self.radio_status = "degraded"
                self.radio_error = str(e)
                logger.warning(
                    "Radio type '%s' unavailable; starting in no-radio mode to keep service alive. "
                    "Check radio configuration and hardware mapping.",
                    radio_type,
                )
                self.radio = NullRadio()

        try:
            from openhop_core import LocalIdentity
            from openhop_core.node.dispatcher import Dispatcher

            dedupe_enabled = bool(
                self.config.get("repeater", {}).get("dispatcher_dedupe_enabled", False)
            )
            self.dispatcher = Dispatcher(self.radio, dedupe_enabled=dedupe_enabled)
            # Flood reception-quality delay base (MeshCore "set rxdelay");
            # 0 keeps flood processing immediate, the firmware default.
            self.dispatcher.rx_delay_base = float(
                self.config.get("delays", {}).get("rx_delay_base", 0.0)
            )
            logger.info("Dispatcher initialized")
            logger.info("Dispatcher dedupe enabled: %s", dedupe_enabled)

            # Track every local identity, including the default repeater.
            self.identity_manager = IdentityManager(self.config)
            logger.info("Identity manager initialized")

            # Set up the default repeater identity.
            identity_key = self.config.get("repeater", {}).get("identity_key")
            if not identity_key:
                logger.error("No identity key found in configuration. Cannot init repeater.")
                raise RuntimeError("Identity key is required for repeater operation")

            local_identity = LocalIdentity(seed=identity_key)
            self.local_identity = local_identity
            self.dispatcher.local_identity = local_identity

            # A one-byte public-key prefix selects local routing, companion
            # bridges, and companion SQLite namespaces.  Reject all configured
            # collisions before helpers, databases, or companion TCP servers
            # have any state to overwrite.
            self._preflight_configured_local_identities(local_identity)
            if not self.identity_manager.register_identity(
                name="repeater",
                identity=local_identity,
                config=self.config,
                identity_type="repeater",
            ):
                raise IdentityConfigurationError("Failed to register repeater identity")

            pubkey = local_identity.get_public_key()
            self.local_hash = pubkey[0]
            self.local_hash_bytes = bytes(pubkey[:3])

            logger.info(f"Local identity set: {local_identity.get_address_bytes().hex()}")
            local_hash_hex = f"0x{self.local_hash:02x}"
            logger.info(f"Local node hash (from identity): {local_hash_hex}")

            # Load additional identities from config (e.g., room servers)
            await self._load_additional_identities()

            # Same cache_ttl computation as RepeaterHandler.__init__ (min 5
            # minutes, default 1 hour): the correlation window must never
            # outlive the dedup window it rides on (design doc §10.4).
            correlation_ttl = max(300, self.config.get("repeater", {}).get("cache_ttl", 3600))
            self.correlation_tracker = CompanionCorrelationTracker(ttl_seconds=correlation_ttl)

            self.repeater_handler = RepeaterHandler(
                self.config,
                self.dispatcher,
                self.local_hash,
                local_hash_bytes=self.local_hash_bytes,
                send_advert_func=self.send_advert,
                duplicate_observer=self._companion_duplicate_observer,
            )

            # Create router
            self.router = PacketRouter(self)
            await self.router.start()

            # Register router as entry point for ALL packets via fallback handler
            # All received packets flow through router → helpers → repeater engine
            self.dispatcher.register_fallback_handler(self._router_callback)
            logger.info("Packet router registered as fallback (catches all packets)")

            # Final-hop RAW_CUSTOM is local-only. Direct packets with remaining
            # hops are handed to the router; flood RAW_CUSTOM is discarded.
            self._register_raw_custom_handler()

            # Set default path hash mode for flood 0-hop packets (adverts, etc.)
            path_hash_mode = self.config.get("mesh", {}).get("path_hash_mode", 0)
            if path_hash_mode not in (0, 1, 2):
                logger.warning(
                    f"Invalid mesh.path_hash_mode={path_hash_mode}, must be 0/1/2; using 0"
                )
                path_hash_mode = 0
            self.dispatcher.set_default_path_hash_mode(path_hash_mode)
            mode_names = {0: "1-byte", 1: "2-byte", 2: "3-byte"}
            logger.info(
                f"Path hash mode set to {mode_names[path_hash_mode]} (mesh.path_hash_mode={path_hash_mode})"
            )

            # Create processing helpers (handlers created internally)
            self.trace_helper = TraceHelper(
                local_hash=self.local_hash,
                repeater_handler=self.repeater_handler,
                packet_injector=self.router.inject_packet,
                log_fn=logger.info,
                local_identity=self.local_identity,
            )
            logger.info("Trace processing helper initialized")

            # Create advert helper for neighbor tracking
            self.advert_helper = AdvertHelper(
                local_identity=self.local_identity,
                storage=self.repeater_handler.storage if self.repeater_handler else None,
                config=self.config,
                log_fn=logger.info,
            )
            logger.info("Advert processing helper initialized")

            # Set up discovery handler if enabled
            allow_discovery = self.config.get("repeater", {}).get("allow_discovery", True)
            if allow_discovery:
                self.discovery_helper = DiscoveryHelper(
                    local_identity=self.local_identity,
                    packet_injector=self.router.inject_packet,
                    node_type=2,
                    log_fn=logger.info,
                    debug_log_fn=logger.debug,
                    tag_conflict=functools.partial(
                        self._frame_has_response_owner,
                        "control",
                    ),
                )
                logger.info("Discovery processing helper initialized")
            else:
                logger.info("Discovery response handler disabled")

            # Create login helper (will create per-identity ACLs)
            self.login_helper = LoginHelper(
                identity_manager=self.identity_manager,
                packet_injector=self.router.inject_packet,
                log_fn=logger.info,
                sqlite_handler=(
                    self.repeater_handler.storage.sqlite_handler
                    if self.repeater_handler and self.repeater_handler.storage
                    else None
                ),  # For anon regions-discovery replies
                config=self.config,  # For owner-info / feature-flags replies
            )

            # Register default repeater identity
            self.login_helper.register_identity(
                name="repeater",
                identity=self.local_identity,
                identity_type="repeater",
                config=self.config,  # Pass full config so repeater can access top-level security section
            )

            # Register room server identities with their configs
            for name, identity, config in self.identity_manager.get_identities_by_type(
                "room_server"
            ):
                self.login_helper.register_identity(
                    name=name,
                    identity=identity,
                    identity_type="room_server",
                    config=config,  # Pass room-specific config
                )

            logger.info("Login processing helper initialized")

            # Initialize ConfigManager for centralized config management
            self.config_manager = ConfigManager(
                config_path=getattr(self, "config_path", "/etc/openhop_repeater/config.yaml"),
                config=self.config,
                daemon_instance=self,
            )
            logger.info("Config manager initialized")

            self.sensor_manager = SensorManager(self.config)
            self.sensor_manager.start()
            if self.sensor_manager.get_summary().get("loaded", 0):
                logger.info("Sensor manager initialized")
            else:
                logger.info("No configured sensors loaded")

            self.gps_service = GPSService(
                self.config,
                location_update_callback=self._update_repeater_location_from_gps,
            )
            self.gps_service.start()
            if self.config.get("gps", {}).get("enabled", False):
                logger.info("GPS diagnostics initialized")
            else:
                logger.info("GPS diagnostics disabled")

            # Initialize text message helper with per-identity ACLs
            self.text_helper = TextHelper(
                identity_manager=self.identity_manager,
                packet_injector=self.router.inject_packet,
                acl_dict=self.login_helper.get_acl_dict(),  # Per-identity ACLs
                log_fn=logger.info,
                config_path=getattr(self, "config_path", None),  # For CLI to save changes
                config=self.config,  # For CLI to read/modify settings
                config_manager=self.config_manager,  # New centralized config manager
                sqlite_handler=(
                    self.repeater_handler.storage.sqlite_handler
                    if self.repeater_handler and self.repeater_handler.storage
                    else None
                ),  # For room server database
                send_advert_callback=self.send_advert,  # For CLI advert command
            )
            self.text_helper._loop = asyncio.get_running_loop()

            # Register default repeater identity for text messages
            self.text_helper.register_identity(
                name="repeater",
                identity=self.local_identity,
                identity_type="repeater",
                radio_config=self.config.get("radio", {}),
            )

            # Register room server identities for text messages
            for name, identity, config in self.identity_manager.get_identities_by_type(
                "room_server"
            ):
                self.text_helper.register_identity(
                    name=name,
                    identity=identity,
                    identity_type="room_server",
                    radio_config=config,  # Pass room-specific config (includes max_posts, etc.)
                )

            logger.info("Text message processing helper initialized")

            # Initialize PATH packet helper for updating client out_path
            self.path_helper = PathHelper(
                acl_dict=self.login_helper.get_acl_dict(),  # Per-identity ACLs
                log_fn=logger.info,
                ack_received_callback=(
                    self.dispatcher._register_ack_received
                    if self.dispatcher and hasattr(self.dispatcher, "_register_ack_received")
                    else None
                ),
            )
            logger.info("PATH packet processing helper initialized")

            # Initialize protocol request handler for status/telemetry requests
            self.protocol_request_helper = ProtocolRequestHelper(
                identity_manager=self.identity_manager,
                packet_injector=self.router.inject_packet,
                acl_dict=self.login_helper.get_acl_dict(),
                radio=self.radio,
                engine=self.repeater_handler,
                neighbor_tracker=self.advert_helper,
                config=self.config,
                sensor_manager=self.sensor_manager,
            )
            # Register repeater identity for protocol requests
            self.protocol_request_helper.register_identity(
                name="repeater", identity=self.local_identity, identity_type="repeater"
            )
            logger.info("Protocol request handler initialized")

            # Load companion identities (CompanionBridge + frame server per companion)
            await self._load_companion_identities()

            # Subscribe to raw RX in openhop-core so we can push PUSH_CODE_LOG_RX_DATA to companion clients
            self.dispatcher.add_raw_rx_subscriber(self._on_raw_rx_for_companions)
            n = len(getattr(self, "companion_frame_servers", []))
            logger.info(
                "Raw RX subscriber registered (%s companion frame server(s)). Connect a client to see rx_log (0x88).",
                n,
            )

            self._register_duplicate_logging_hook(dedupe_enabled)

            # When trace reaches final node, push PUSH_CODE_TRACE_DATA (0x89) to companion clients (firmware onTraceRecv)
            self.trace_helper.on_trace_complete = self._on_trace_complete_for_companions

            # Optional pyMC_Glass integration loop (inform/control plane)
            self.glass_handler = GlassHandler(
                config=self.config,
                daemon_instance=self,
                config_manager=self.config_manager,
            )
            await self.glass_handler.start()
            if (
                self.repeater_handler
                and self.repeater_handler.storage
                and hasattr(self.repeater_handler.storage, "set_glass_publisher")
            ):
                self.repeater_handler.storage.set_glass_publisher(
                    self.glass_handler.publish_telemetry
                )

        except Exception as e:
            logger.error(f"Failed to initialize dispatcher: {e}")
            raise

    async def _load_additional_identities(self):
        room_specs = self._room_server_specs
        if room_specs is None:
            room_specs = self._configured_identity_specs("room_server")
        self.identity_manager.validate_specs(room_specs)

        for spec in room_specs:
            name, room_identity = spec.name, spec.identity
            try:
                # Register with the manager and all helpers
                success = self._register_identity_everywhere(
                    name=name,
                    identity=room_identity,
                    config=spec.config,
                    identity_type="room_server",
                )

                if success:
                    room_hash = room_identity.get_public_key()[0]
                    logger.info(
                        f"Loaded room server '{name}': hash=0x{room_hash:02x}, "
                        f"address={room_identity.get_address_bytes().hex()}"
                    )
                else:
                    raise IdentityConfigurationError(
                        f"Failed to register room server identity '{name}'"
                    )

            except IdentityConfigurationError:
                raise
            except Exception as e:
                logger.error(f"Failed to load room server identity '{name}': {e}")

        # Summary logging
        total_identities = len(self.identity_manager.list_identities())
        logger.info(f"Identity manager loaded {total_identities} total identities")

    def _get_companion_radio_settings(self) -> dict:
        """Return the current repeater radio settings for virtual companions.

        The values are read-only to companion sessions.  Prefer attributes of
        the active backend, then retain the configured value when a backend
        cannot expose that field.
        """
        config = (
            self.repeater_handler.radio_config
            if self.repeater_handler
            else self.config.get("radio", {})
        )
        settings = dict(config) if isinstance(config, dict) else {}
        radio = self.radio
        if radio is None:
            return settings

        for config_key, attr in (
            ("frequency", "frequency"),
            ("bandwidth", "bandwidth"),
            ("spreading_factor", "spreading_factor"),
            ("coding_rate", "coding_rate"),
            ("tx_power", "tx_power"),
        ):
            value = getattr(radio, attr, None)
            if value is not None:
                settings[config_key] = value
        return settings

    def _get_companion_max_tx_power_dbm(self):
        """Return the active backend's TX limit when it declares one.

        The backend can expose a ``get_max_tx_power_dbm`` method, a
        ``max_tx_power_dbm`` attribute (SX1262 backends declare their 22 dBm
        driver limit this way), or a validated deployment setting.
        Returning ``None`` lets Core use its generic protocol fallback.
        """
        return resolve_max_tx_power_dbm(self.radio, self._get_companion_radio_settings())

    def _build_push_notifier(self, sqlite_handler) -> CompanionPushNotifier:
        """Construct and start the process-wide push notifier from config
        (design doc §12.2). Config lives under ``companion.push``:
        ``enabled`` (default true), ``min_interval_sec`` (0..86400, default
        30), ``request_timeout_sec`` (0.1..300, default 10),
        operator-owned ``relay_url``, ``allow_insecure_http`` (default false),
        and ``worker_count`` (1..4, default 2)."""
        companion_cfg = self.config.get("companion", {})
        if companion_cfg is not None and not isinstance(companion_cfg, dict):
            raise ValueError("companion must be an object")
        configured_push = (companion_cfg or {}).get("push", {})
        if configured_push is None:
            configured_push = {}
        if not isinstance(configured_push, dict):
            raise ValueError("companion.push must be an object")
        push_cfg = configured_push
        enabled = validate_companion_boolean_setting(
            push_cfg.get("enabled", True),
            "companion.push.enabled",
        )
        allow_insecure_http = validate_companion_boolean_setting(
            push_cfg.get("allow_insecure_http", False),
            "companion.push.allow_insecure_http",
        )
        min_interval = validate_companion_seconds_setting(
            push_cfg.get("min_interval_sec", 30.0),
            "companion.push.min_interval_sec",
            minimum=0.0,
            maximum=MAX_COMPANION_PUSH_MIN_INTERVAL_SEC,
        )
        request_timeout = validate_companion_seconds_setting(
            push_cfg.get("request_timeout_sec", 10.0),
            "companion.push.request_timeout_sec",
            minimum=0.1,
            maximum=MAX_COMPANION_PUSH_REQUEST_TIMEOUT_SEC,
        )
        worker_count = push_cfg.get("worker_count", 2)
        if type(worker_count) is not int:
            raise ValueError("companion.push.worker_count must be an integer")
        worker_count = max(1, min(worker_count, 4))
        notifier = CompanionPushNotifier(
            sqlite_handler,
            enabled=enabled,
            min_interval=min_interval,
            request_timeout=request_timeout,
            relay_url=push_cfg.get("relay_url"),
            allow_insecure_http=allow_insecure_http,
            worker_count=worker_count,
        )
        notifier.start()
        return notifier

    async def _load_companion_identities(self) -> None:
        """Load companion identities from config and create CompanionBridge + frame server for each."""
        from repeater.companion import (
            CompanionEventJournal,
            CompanionFrameServer,
            RepeaterCompanionBridge,
        )

        companion_specs = self._companion_specs
        if companion_specs is None:
            companion_specs = self._configured_identity_specs("companion")
        if not companion_specs:
            return

        # Validate the complete companion set before any bridge can restore or
        # mutate a hash-keyed SQLite namespace, or any TCP server can bind.
        try:
            validate_companion_listener_config(
                (spec.config for spec in companion_specs),
                self.config.get("http", {}),
            )
        except ValueError as exc:
            raise IdentityConfigurationError(str(exc)) from exc
        self.identity_manager.validate_specs(companion_specs)

        sqlite_handler = None
        if self.repeater_handler and self.repeater_handler.storage:
            sqlite_handler = self.repeater_handler.storage.sqlite_handler
        if not sqlite_handler:
            logger.warning(
                "Companion persistence disabled: no storage (contacts/channels will not survive restart or disconnect)"
            )

        # Build the process-wide push notifier once storage is known (design
        # doc §12.2). Journals register a listener on it below.
        if self.push_notifier is None and sqlite_handler is not None:
            self.push_notifier = self._build_push_notifier(sqlite_handler)

        radio_config = (
            self.repeater_handler.radio_config
            if self.repeater_handler
            else self.config.get("radio", {})
        )

        for spec in companion_specs:
            name, identity, comp_config = spec.name, spec.identity, spec.config
            companion_hash = None
            companion_hash_str = None
            companion_identity = None
            journal = None
            push_listener = None
            bridge = None
            frame_server = None
            loaded = False
            try:
                settings = comp_config.get("settings") or {}
                pubkey = identity.get_public_key()
                companion_hash = pubkey[0]
                companion_hash_str = f"0x{companion_hash:02x}"
                companion_identity = pubkey.hex()

                node_name = validate_companion_node_name(
                    settings.get("node_name", name[:31])
                )
                frame_enabled = validate_companion_boolean_setting(
                    settings.get("frame_enabled", True),
                    "frame_enabled",
                )
                tcp_port = None
                bind_address = None
                client_idle_timeout_sec = None
                if frame_enabled:
                    tcp_port = validate_companion_tcp_port(
                        settings.get("tcp_port", DEFAULT_COMPANION_TCP_PORT)
                    )
                    bind_address = validate_companion_bind_address(
                        settings.get("bind_address", "127.0.0.1")
                    )
                    tcp_timeout_raw = validate_companion_tcp_timeout(
                        settings.get(
                            "tcp_timeout",
                            DEFAULT_COMPANION_TCP_TIMEOUT_SEC,
                        )
                    )
                    client_idle_timeout_sec = (
                        None if tcp_timeout_raw == 0 else int(tcp_timeout_raw)
                    )
                adopt_legacy_namespace = validate_companion_legacy_adoption(
                    settings.get("adopt_legacy_namespace", False)
                )
                trim_contacts_on_overflow = validate_companion_boolean_setting(
                    settings.get("trim_contacts_on_overflow", False),
                    "trim_contacts_on_overflow",
                )
                rf_reception_events = validate_companion_boolean_setting(
                    settings.get("rf_reception_events", False),
                    "rf_reception_events",
                )

                bridge_kwargs = parse_companion_bridge_kwargs(settings)
                max_contacts = effective_max_contacts(bridge_kwargs)
                if sqlite_handler:
                    # All read-only configuration preflight has passed.
                    # Establish ownership before any trim, journal/listener,
                    # restore, bridge, or socket touches this namespace.
                    sqlite_handler.companion_bind_namespace(
                        companion_hash_str,
                        companion_identity,
                        adopt_legacy_namespace=adopt_legacy_namespace,
                    )
                    trimmed = enforce_companion_contact_capacity(
                        companion_hash_str,
                        max_contacts,
                        sqlite_handler,
                        trim=trim_contacts_on_overflow,
                        companion_name=name,
                    )
                    if trimmed:
                        logger.warning(
                            "Companion '%s': trimmed %d contact(s) to fit "
                            "max_contacts=%d (trim_contacts_on_overflow)",
                            name,
                            trimmed,
                            max_contacts,
                        )

                journal = (
                    CompanionEventJournal(sqlite_handler, companion_hash_str)
                    if sqlite_handler
                    else None
                )
                if journal is not None:
                    self.companion_journals[companion_hash_str] = journal
                    if rf_reception_events:
                        self._rf_reception_journals[companion_hash_str] = journal
                    if self.push_notifier is not None:
                        push_listener = self.push_notifier.make_listener(
                            companion_hash_str,
                            companion_identity,
                        )
                        journal.register_listener(push_listener)

                def _on_companion_prefs_saved(
                    new_node_name: str,
                    _name=name,
                ) -> None:
                    """Keep the configured display name aligned after commit."""
                    self._sync_companion_node_name_to_config(
                        _name,
                        new_node_name,
                    )

                bridge = RepeaterCompanionBridge(
                    identity=identity,
                    # Tag the injector with this companion's hash so inject_packet can
                    # skip its own frame server when echoing TX as raw RX (a node never
                    # hears its own transmission).
                    packet_injector=functools.partial(
                        self.router.inject_packet, origin_hash=companion_hash_str
                    ),
                    node_name=node_name,
                    radio_config=radio_config,
                    radio_settings_getter=self._get_companion_radio_settings,
                    max_tx_power_getter=self._get_companion_max_tx_power_dbm,
                    sqlite_handler=sqlite_handler,
                    companion_hash=companion_hash_str,
                    on_prefs_saved=_on_companion_prefs_saved,
                    journal=journal,
                    tracker=self.correlation_tracker,
                    **bridge_kwargs,
                )
                self._wire_companion_history_observers(bridge, journal)

                # Restore persisted state (contacts/channels/messages) from SQLite.
                # Raises CompanionStateLoadError instead of continuing with an
                # empty store when persisted rows exist but cannot be loaded.
                if sqlite_handler:
                    await self._restore_companion_state(
                        sqlite_handler, bridge, companion_hash_str, name
                    )

                await self._reconcile_companion_node_name(
                    bridge,
                    settings.get("node_name") if "node_name" in settings else None,
                    name,
                )
                await self._ensure_default_companion_channel(bridge, journal)

                # A bridge owns protocol-handler lifecycle even though the
                # repeater owns the physical radio. Start it before exposing
                # either the frame or REST surface.
                await bridge.start()

                if frame_enabled:
                    frame_server = CompanionFrameServer(
                        bridge=bridge,
                        companion_hash=companion_hash_str,
                        port=tcp_port,
                        bind_address=bind_address,
                        client_idle_timeout_sec=client_idle_timeout_sec,
                        sqlite_handler=sqlite_handler,
                        local_hash=self.local_hash,
                        stats_getter=self._get_companion_stats,
                        control_handler=(
                            self.discovery_helper.control_handler
                            if self.discovery_helper
                            else None
                        ),
                        journal=journal,
                        tracker=self.correlation_tracker,
                        response_owner_resolver=self._is_unique_frame_response_owner,
                        response_tag_conflict=self._frame_response_tag_conflict,
                    )
                    try:
                        await frame_server.start()
                    except Exception as exc:
                        raise IdentityConfigurationError(
                            f"Companion '{name}' Frame listener failed to start "
                            f"on {bind_address}:{tcp_port}: {exc}"
                        ) from exc

                if not self.identity_manager.register_identity(
                    name=name,
                    identity=identity,
                    config=comp_config,
                    identity_type="companion",
                ):
                    # The complete set was prevalidated above.  A failure here
                    # signals a concurrent/configuration error and must not be
                    # silently treated as a running companion.
                    raise IdentityConfigurationError(
                        f"Failed to register companion identity '{name}'"
                    )

                # Publish the fully started runtime without another await.
                # HTTP routing therefore cannot observe a bridge whose
                # required frame listener failed to bind.
                self.companion_bridges[companion_hash] = bridge
                if frame_server is not None:
                    self.companion_frame_servers.append(frame_server)
                if push_listener is not None:
                    self._companion_push_listeners[companion_hash_str] = (
                        journal,
                        push_listener,
                        companion_identity,
                    )
                loaded = True
                limits = format_companion_bridge_limits(bridge_kwargs)
                frame_status = (
                    f"port={tcp_port}, bind={bind_address}, "
                    f"client_idle_timeout_sec={client_idle_timeout_sec}"
                    if frame_enabled
                    else "frame=disabled"
                )
                logger.info(
                    f"Loaded companion '{name}': hash=0x{companion_hash:02x}, "
                    f"{frame_status}{limits}"
                )

            except CompanionContactCapacityError as e:
                logger.error("%s", e)
            except CompanionStateLoadError as e:
                logger.error("Companion init aborted: %s", e)
            except CompanionNamespaceCollisionError as e:
                logger.error("Companion activation refused: %s", e)
            except CompanionStorageError as e:
                logger.error("Companion init aborted: %s", e)
            except IdentityConfigurationError:
                raise
            except Exception as e:
                logger.error(f"Failed to load companion '{name}': {e}", exc_info=True)
            finally:
                if not loaded:
                    self._detach_companion_push_listener(
                        journal,
                        push_listener,
                        companion_hash_str,
                        companion_identity,
                    )
                    if companion_hash_str is not None:
                        self._companion_push_listeners.pop(companion_hash_str, None)
                    if frame_server in self.companion_frame_servers:
                        self.companion_frame_servers.remove(frame_server)
                    if companion_hash is not None:
                        self.companion_bridges.pop(companion_hash, None)
                    if companion_hash_str is not None:
                        self.companion_journals.pop(companion_hash_str, None)
                        self._rf_reception_journals.pop(companion_hash_str, None)
                    await self._stop_partial_companion(frame_server, bridge)

    async def _restore_companion_state(
        self, sqlite_handler, bridge, companion_hash_str: str, name: str
    ) -> None:
        """Restore persisted contacts and channels from SQLite into a bridge.

        Each load is cross-checked against the table's row count for this
        companion and retried once on mismatch; raises CompanionStateLoadError
        when persisted rows exist but cannot be loaded, so the companion fails
        init loudly instead of starting with an empty store.
        """
        from openhop_core.companion.models import Channel

        contact_rows, contact_count = await _load_companion_rows_verified(
            sqlite_handler.companion_load_contacts,
            sqlite_handler.companion_count_contacts,
            "contacts",
            companion_hash_str,
            name,
        )
        if contact_rows:
            records = []
            for row in contact_rows:
                d = dict(row)
                d["public_key"] = d.pop("pubkey", d.get("public_key", b""))
                records.append(d)
            bridge.contacts.load_from_dicts(records)

        # Load channels (normalize secret to 32 bytes to match
        # CompanionBase.set_channel and GroupTextHandler/PacketBuilder)
        channel_rows, channel_count = await _load_companion_rows_verified(
            sqlite_handler.companion_load_channels,
            sqlite_handler.companion_count_channels,
            "channels",
            companion_hash_str,
            name,
        )
        for row in channel_rows:
            s = row.get("secret", b"")
            if isinstance(s, bytes):
                raw = s
            elif isinstance(s, (bytearray, memoryview)):
                raw = bytes(s)
            elif s:
                raw = bytes.fromhex(s if isinstance(s, str) else str(s))
            else:
                raw = b""
            if len(raw) < 32:
                raw = raw + b"\x00" * (32 - len(raw))
            elif len(raw) > 32:
                raw = raw[:32]
            idx = row.get("channel_idx", 0)
            ch = Channel(name=row.get("name", ""), secret=raw)
            if not bridge.channels.set(idx, ch):
                logger.error(
                    "Companion %s ('%s'): channel store rejected persisted channel "
                    "idx=%r name=%r (index out of range?)",
                    companion_hash_str,
                    name,
                    idx,
                    row.get("name", ""),
                )

        logger.info(
            "Companion %s ('%s'): restored %d/%d contact(s), %d/%d channel(s); "
            "queued messages remain in SQLite",
            companion_hash_str,
            name,
            len(contact_rows),
            contact_count,
            len(channel_rows),
            channel_count,
        )

    @staticmethod
    async def _ensure_default_companion_channel(bridge, journal) -> None:
        """Provision channel 0 durably before exposing a companion.

        A previously cleared Public channel must reappear through the same
        journal clients resume from; an in-memory-only backfill would leave
        valid cursors and ETags pointing at stale state.
        """
        if bridge.get_channel(0) is not None:
            return
        from repeater.companion.constants import DEFAULT_PUBLIC_CHANNEL_SECRET

        if not bridge.set_channel(0, "Public", DEFAULT_PUBLIC_CHANNEL_SECRET):
            raise CompanionStateLoadError("Default Public channel was rejected")
        if journal is None:
            return
        try:
            await asyncio.to_thread(
                journal.store_channel,
                0,
                "Public",
                DEFAULT_PUBLIC_CHANNEL_SECRET,
            )
        except BaseException:
            bridge.channels.remove(0)
            raise

    def _sync_companion_node_name_to_config(
        self,
        companion_name: str,
        new_node_name: str,
    ) -> None:
        """Persist a committed Frame name change back to its YAML setting."""
        validated = validate_companion_node_name(new_node_name)
        config_manager = self.config_manager
        if config_manager is None:
            config_path = getattr(self, "config_path", None)
            if config_path:
                config_manager = ConfigManager(
                    config_path=config_path,
                    config=self.config,
                    daemon_instance=self,
                )
                self.config_manager = config_manager

        if config_manager is not None:
            try:
                saved = config_manager.save_companion_node_name(
                    companion_name,
                    validated,
                )
            except (KeyError, ValueError) as exc:
                raise RuntimeError(str(exc)) from exc
            if not saved:
                raise RuntimeError("Failed to persist companion node_name to config")
            return

        # A daemon assembled without a config path can still be used in tests
        # and embedded callers; keep its in-memory state coherent.
        companions = (self.config.get("identities") or {}).get("companions") or []
        for entry in companions:
            if entry.get("name") != companion_name:
                continue
            settings = entry.setdefault("settings", {})
            if settings.get("node_name") == validated:
                return
            settings["node_name"] = validated
            return
        raise RuntimeError(f"Companion '{companion_name}' is missing from config")

    @staticmethod
    async def _reconcile_companion_node_name(
        bridge,
        desired_name: Optional[str],
        companion_name: str,
    ) -> None:
        """Make an explicit YAML name durable before exposing the bridge."""
        if desired_name is None or bridge.prefs.node_name == desired_name:
            return
        bridge.clear_prefs_save_error()
        await asyncio.to_thread(bridge.set_advert_name, desired_name)
        error = bridge.consume_prefs_save_error()
        if error is not None or bridge.prefs.node_name != desired_name:
            detail = f": {error}" if error is not None else ""
            raise CompanionStateLoadError(
                f"Companion '{companion_name}' could not persist configured "
                f"node_name{detail}"
            )

    @staticmethod
    def _wire_companion_history_observers(bridge, journal) -> None:
        """Persist non-v1 sends and all accepted ACK transitions.

        REST reserves and stores its own outbound row before touching RF, then
        marks its bridge call with ``source='rest'``. These observers therefore
        ignore REST send events and own the Frame and legacy operator sides of
        the shared conversation history.
        """
        if journal is None or not callable(getattr(bridge, "add_observer", None)):
            return

        # A CRC is only a protocol hint and can be reused.  The bridge supplies
        # an opaque per-send token whenever it owns the send, so history
        # correlation stays exact even while Frame and operator clients share
        # one radio.  The ACK fallback keeps manually constructed/older bridge
        # events compatible without inventing a second protocol.
        transport_message_ids: OrderedDict[tuple[str, int], int] = OrderedDict()
        transport_confirmations_before_store: OrderedDict[
            tuple[str, int], object
        ] = OrderedDict()

        def _send_key(event) -> tuple[str, int]:
            token = getattr(event, "correlation_token", None)
            if token is not None:
                return ("token", int(token))
            return ("ack", int(event.expected_ack))

        def _remember_cancellation(current, candidate):
            return current if current is not None else candidate

        async def _on_message_sent(event) -> None:
            source = getattr(event, "source", None)
            if source not in {"frame", "operator"}:
                return
            cancellation = None
            timestamp = getattr(event, "timestamp", None)
            message = {
                "packet_hash": event.packet_hash,
                "recipient_key": event.recipient_key,
                "text": event.text,
                "timestamp": int(timestamp) if timestamp is not None else int(time.time()),
                "is_channel": bool(event.is_channel),
                "channel_idx": event.channel_idx,
                "txt_type": int(event.txt_type),
                "expected_ack": event.expected_ack,
            }
            initial_state = getattr(event, "initial_state", "transmitted")
            if initial_state not in {"transmitted", "indeterminate"}:
                initial_state = "transmitted"
            try:
                stored, worker_cancellation = await await_to_thread_outcome(
                    journal.store_outbound_message,
                    message,
                    source,
                    initial_state,
                )
            except BaseException:
                tracker = getattr(bridge, "_tracker", None)
                if tracker is not None:
                    tracker.discard_registration(
                        getattr(event, "correlation_token", None)
                    )
                raise
            cancellation = _remember_cancellation(
                cancellation,
                worker_cancellation,
            )
            message_id = int(stored["message_id"])
            tracker = getattr(bridge, "_tracker", None)
            if tracker is not None:
                # The bridge registers immediately (before this storage await)
                # so a fast RF repeat cannot be missed. Atomically attach the
                # durable row id without resetting its TTL or repeat counters.
                correlation_token = getattr(
                    event,
                    "correlation_token",
                    None,
                )
                if correlation_token is None:
                    buffered_hits = tracker.promote_outbound(
                        event.packet_hash,
                        event.companion_hash,
                        message_id,
                    )
                else:
                    buffered_hits = tracker.promote_outbound(
                        event.packet_hash,
                        event.companion_hash,
                        message_id,
                        registration_token=correlation_token,
                    )
                buffered_hits = buffered_hits or ()
                for hit in buffered_hits:
                    _, worker_cancellation = await await_to_thread_outcome(
                        journal.record_outbound_heard_repeat,
                        hit,
                    )
                    tracker.acknowledge(hit)
                    cancellation = _remember_cancellation(
                        cancellation,
                        worker_cancellation,
                    )
            if event.expected_ack is not None:
                key = _send_key(event)
                early_confirmation = transport_confirmations_before_store.pop(
                    key,
                    None,
                )
                if early_confirmation is None:
                    transport_message_ids.pop(key, None)
                    transport_message_ids[key] = message_id
                    while len(transport_message_ids) > MAX_PENDING_ACK_CRCS:
                        transport_message_ids.popitem(last=False)
                else:
                    _, worker_cancellation = await await_to_thread_outcome(
                        journal.update_outbound_state,
                        message_id,
                        "confirmed",
                        early_confirmation.packet_hash,
                        early_confirmation.expected_ack,
                    )
                    cancellation = _remember_cancellation(
                        cancellation,
                        worker_cancellation,
                    )
            if cancellation is not None:
                raise cancellation

        async def _on_message_confirmed(event) -> None:
            source = getattr(event, "source", None)
            if source == "rest":
                message_id = getattr(event, "message_id", None)
                if message_id is None:
                    logger.debug(
                        "Companion %s: REST ACK %s has no reserved history row",
                        event.companion_hash,
                        event.expected_ack,
                    )
                    return
                _, cancellation = await await_to_thread_outcome(
                    journal.update_outbound_state,
                    int(message_id),
                    "confirmed",
                    event.packet_hash,
                    event.expected_ack,
                )
                if cancellation is not None:
                    raise cancellation
                return
            if source not in {"frame", "operator"}:
                return
            key = _send_key(event)
            message_id = transport_message_ids.pop(key, None)
            if message_id is None:
                transport_confirmations_before_store.pop(key, None)
                transport_confirmations_before_store[key] = event
                while len(transport_confirmations_before_store) > MAX_PENDING_ACK_CRCS:
                    transport_confirmations_before_store.popitem(last=False)
                logger.debug(
                    "Companion %s: ACK %s arrived before frame-history storage",
                    event.companion_hash,
                    event.expected_ack,
                )
                return
            _, cancellation = await await_to_thread_outcome(
                journal.update_outbound_state,
                message_id,
                "confirmed",
                event.packet_hash,
                event.expected_ack,
            )
            if cancellation is not None:
                raise cancellation

        bridge.add_observer("message_sent", _on_message_sent)
        bridge.add_observer("message_confirmed", _on_message_confirmed)

    @staticmethod
    async def _stop_partial_companion(frame_server=None, bridge=None) -> None:
        """Best-effort cleanup for a companion that failed during startup."""
        for label, component in (("frame server", frame_server), ("bridge", bridge)):
            stop = getattr(component, "stop", None)
            if not callable(stop):
                continue
            try:
                await stop()
            except Exception as e:
                logger.warning("Partial companion %s cleanup failed: %s", label, e)

    def _detach_companion_push_listener(
        self,
        journal,
        listener,
        companion_hash: Optional[str],
        companion_identity: Optional[str],
    ) -> None:
        """Unregister one exact push listener and discard its queued wakes."""

        if listener is None:
            return
        unregister = getattr(journal, "unregister_listener", None)
        if callable(unregister):
            try:
                unregister(listener)
            except Exception as exc:
                logger.warning("Companion push listener cleanup failed: %s", exc)
        deactivate = getattr(self.push_notifier, "deactivate", None)
        if (
            callable(deactivate)
            and companion_hash is not None
            and companion_identity is not None
        ):
            try:
                deactivate(companion_hash, companion_identity)
            except Exception as exc:
                logger.warning("Companion push queue cleanup failed: %s", exc)

    async def add_companion_from_config(
        self,
        comp_config: dict,
        *,
        require_current_config: bool = False,
    ) -> None:
        """Serialize and activate one hot-added companion.

        ``require_current_config`` is used by the HTTP create path.  It keeps
        the final runtime publication conditional on the exact configuration
        that request committed still being current.  Direct embedders and
        tests retain the historical ability to activate an explicitly supplied
        configuration without first inserting it into ``self.config``.
        """
        async with self._companion_lifecycle_lock:
            await self._add_companion_from_config_locked(
                comp_config,
                require_current_config=require_current_config,
            )

    async def _add_companion_from_config_locked(
        self,
        comp_config: dict,
        *,
        require_current_config: bool = False,
    ) -> None:
        """
        Load a single companion from config and register it (hot-reload).
        Creates RepeaterCompanionBridge, CompanionFrameServer, starts the server,
        and registers with identity_manager. Raises on error.
        """
        from openhop_core import LocalIdentity

        from repeater.companion import (
            CompanionEventJournal,
            CompanionFrameServer,
            RepeaterCompanionBridge,
        )
        name = comp_config.get("name")
        identity_key = comp_config.get("identity_key")
        raw_settings = comp_config.get("settings")
        settings = {} if raw_settings is None else raw_settings

        if not name or not identity_key:
            raise ValueError("Companion config missing name or identity_key")
        name = validate_companion_registration_name(name)
        if not isinstance(settings, dict):
            raise ValueError("companion settings must be an object")

        if isinstance(identity_key, str):
            try:
                identity_key_bytes = bytes.fromhex(normalize_companion_identity_key(identity_key))
            except ValueError as e:
                raise ValueError(f"Companion '{name}' identity_key invalid hex: {e}") from e
        elif isinstance(identity_key, bytes):
            identity_key_bytes = identity_key
        else:
            raise ValueError(f"Companion '{name}' identity_key has unknown type")

        if len(identity_key_bytes) not in (32, 64):
            raise ValueError(
                f"Companion '{name}' identity_key must be 32 bytes (hex) or 64 bytes (MeshCore firmware key)"
            )

        if self.identity_manager is None:
            raise RuntimeError("Identity manager must be initialized before adding a companion")
        # Already registered?
        if self.identity_manager.get_identity_by_name(name) is not None:
            raise ValueError(f"Companion '{name}' is already registered")

        identity = LocalIdentity(seed=identity_key_bytes)
        pubkey = identity.get_public_key()
        companion_hash = pubkey[0]
        companion_hash_str = f"0x{companion_hash:02x}"
        companion_identity = pubkey.hex()
        if name in self._retiring_companions or any(
            item.get("companion_hash") == companion_hash
            for item in self._retiring_companions.values()
        ):
            raise RuntimeError(
                f"Companion '{name}' is still retiring; restart before reusing "
                "its name or routing hash"
            )
        registration_error = self.identity_manager.registration_error(name, identity)
        if registration_error:
            raise ValueError(f"Cannot add companion: {registration_error}")

        if companion_hash in self.companion_bridges:
            raise ValueError(f"Companion with hash 0x{companion_hash:02x} already loaded")

        sqlite_handler = None
        if self.repeater_handler and self.repeater_handler.storage:
            sqlite_handler = self.repeater_handler.storage.sqlite_handler

        radio_config = (
            self.repeater_handler.radio_config
            if self.repeater_handler
            else self.config.get("radio", {})
        )

        node_name = validate_companion_node_name(
            settings.get("node_name", name[:31])
        )
        frame_enabled = validate_companion_boolean_setting(
            settings.get("frame_enabled", True),
            "frame_enabled",
        )
        tcp_port = None
        bind_address = None
        client_idle_timeout_sec = None
        if frame_enabled:
            tcp_port = validate_companion_tcp_port(
                settings.get("tcp_port", DEFAULT_COMPANION_TCP_PORT)
            )
            bind_address = validate_companion_bind_address(
                settings.get("bind_address", "127.0.0.1")
            )
            tcp_timeout_raw = validate_companion_tcp_timeout(
                settings.get(
                    "tcp_timeout",
                    DEFAULT_COMPANION_TCP_TIMEOUT_SEC,
                )
            )
            client_idle_timeout_sec = (
                None if tcp_timeout_raw == 0 else int(tcp_timeout_raw)
            )
        adopt_legacy_namespace = validate_companion_legacy_adoption(
            settings.get("adopt_legacy_namespace", False)
        )
        trim_contacts_on_overflow = validate_companion_boolean_setting(
            settings.get("trim_contacts_on_overflow", False),
            "trim_contacts_on_overflow",
        )
        rf_reception_events = validate_companion_boolean_setting(
            settings.get("rf_reception_events", False),
            "rf_reception_events",
        )

        configured = (self.config.get("identities") or {}).get("companions") or []
        prospective = [
            entry
            for entry in configured
            if str(entry.get("name") or "").strip() != name
        ]
        prospective.append(comp_config)
        validate_companion_listener_config(
            prospective,
            self.config.get("http", {}),
        )

        bridge_kwargs = parse_companion_bridge_kwargs(settings)
        max_contacts = effective_max_contacts(bridge_kwargs)
        if sqlite_handler:
            # This is the first stateful hot-add operation, after all read-only
            # validation. A collision fails before trimming or construction of
            # a journal, bridge, listener, or frame server.
            sqlite_handler.companion_bind_namespace(
                companion_hash_str,
                companion_identity,
                adopt_legacy_namespace=adopt_legacy_namespace,
            )
            trimmed = enforce_companion_contact_capacity(
                companion_hash_str,
                max_contacts,
                sqlite_handler,
                trim=trim_contacts_on_overflow,
                companion_name=name,
            )
            if trimmed:
                logger.warning(
                    "Hot-reload companion '%s': trimmed %d contact(s) to fit "
                    "max_contacts=%d (trim_contacts_on_overflow)",
                    name,
                    trimmed,
                    max_contacts,
                )

        if self.push_notifier is None and sqlite_handler is not None:
            self.push_notifier = self._build_push_notifier(sqlite_handler)

        journal = (
            CompanionEventJournal(sqlite_handler, companion_hash_str) if sqlite_handler else None
        )
        push_listener = None
        if journal is not None:
            if self.push_notifier is not None:
                push_listener = self.push_notifier.make_listener(
                    companion_hash_str,
                    companion_identity,
                )
                try:
                    journal.register_listener(push_listener)
                except BaseException:
                    self._detach_companion_push_listener(
                        journal,
                        push_listener,
                        companion_hash_str,
                        companion_identity,
                    )
                    raise

        def _on_companion_prefs_saved(
            new_node_name: str,
            _name=name,
        ) -> None:
            self._sync_companion_node_name_to_config(_name, new_node_name)

        try:
            bridge = RepeaterCompanionBridge(
                identity=identity,
                packet_injector=functools.partial(
                    self.router.inject_packet, origin_hash=companion_hash_str
                ),
                node_name=node_name,
                radio_config=radio_config,
                radio_settings_getter=self._get_companion_radio_settings,
                max_tx_power_getter=self._get_companion_max_tx_power_dbm,
                sqlite_handler=sqlite_handler,
                companion_hash=companion_hash_str,
                on_prefs_saved=_on_companion_prefs_saved,
                journal=journal,
                tracker=self.correlation_tracker,
                **bridge_kwargs,
            )
            self._wire_companion_history_observers(bridge, journal)
        except BaseException:
            self._detach_companion_push_listener(
                journal,
                push_listener,
                companion_hash_str,
                companion_identity,
            )
            raise

        try:
            # Restore persisted state; raises CompanionStateLoadError when
            # persisted rows exist but cannot be loaded.
            if sqlite_handler:
                await self._restore_companion_state(
                    sqlite_handler,
                    bridge,
                    companion_hash_str,
                    name,
                )

            await self._reconcile_companion_node_name(
                bridge,
                settings.get("node_name") if "node_name" in settings else None,
                name,
            )
            await self._ensure_default_companion_channel(bridge, journal)

            # Match the boot path: protocol handlers must be running before
            # this bridge is made available to frame or REST clients.
            await bridge.start()
        except BaseException:
            self._detach_companion_push_listener(
                journal,
                push_listener,
                companion_hash_str,
                companion_identity,
            )
            await self._stop_partial_companion(None, bridge)
            raise

        frame_server = None
        try:
            if frame_enabled:
                frame_server = CompanionFrameServer(
                    bridge=bridge,
                    companion_hash=companion_hash_str,
                    port=tcp_port,
                    bind_address=bind_address,
                    client_idle_timeout_sec=client_idle_timeout_sec,
                    sqlite_handler=sqlite_handler,
                    local_hash=self.local_hash,
                    stats_getter=self._get_companion_stats,
                    control_handler=(
                        self.discovery_helper.control_handler
                        if self.discovery_helper
                        else None
                    ),
                    journal=journal,
                    tracker=self.correlation_tracker,
                    response_owner_resolver=self._is_unique_frame_response_owner,
                    response_tag_conflict=self._frame_response_tag_conflict,
                )
                try:
                    await frame_server.start()
                except Exception as exc:
                    raise IdentityConfigurationError(
                        f"Companion '{name}' Frame listener failed to start "
                        f"on {bind_address}:{tcp_port}: {exc}"
                    ) from exc
        except BaseException:
            self._detach_companion_push_listener(
                journal,
                push_listener,
                companion_hash_str,
                companion_identity,
            )
            await self._stop_partial_companion(frame_server, bridge)
            raise

        def publish_started_runtime() -> str | None:
            """Publish synchronously, optionally guarded by the config CAS."""
            if require_current_config:
                current_companions = (
                    (self.config.get("identities") or {}).get("companions")
                    or []
                )
                if not any(
                    current == comp_config for current in current_companions
                ):
                    return (
                        f"Companion '{name}' configuration changed before "
                        "activation completed"
                    )

            self.companion_bridges[companion_hash] = bridge
            if journal is not None:
                self.companion_journals[companion_hash_str] = journal
                if rf_reception_events:
                    self._rf_reception_journals[companion_hash_str] = journal
            if frame_server is not None:
                self.companion_frame_servers.append(frame_server)
            if not self.identity_manager.register_identity(
                name=name,
                identity=identity,
                config=comp_config,
                identity_type="companion",
            ):
                if frame_server in self.companion_frame_servers:
                    self.companion_frame_servers.remove(frame_server)
                self.companion_bridges.pop(companion_hash, None)
                self.companion_journals.pop(companion_hash_str, None)
                self._rf_reception_journals.pop(companion_hash_str, None)
                return f"Failed to register companion identity '{name}'"
            if push_listener is not None:
                self._companion_push_listeners[companion_hash_str] = (
                    journal,
                    push_listener,
                    companion_identity,
                )
            return None

        # Publish only fully started components. The optional configuration
        # lock is held only across this synchronous compare-and-publish block:
        # never across bridge, socket, storage, or cleanup awaits. A concurrent
        # create/update/delete therefore lands wholly before or after runtime
        # publication instead of leaving an unconfigured live bridge.
        try:
            config_mutation = getattr(self.config_manager, "mutation", None)
            if require_current_config and callable(config_mutation):
                with config_mutation():
                    publication_error = publish_started_runtime()
            else:
                publication_error = publish_started_runtime()
        except BaseException:
            if frame_server in self.companion_frame_servers:
                self.companion_frame_servers.remove(frame_server)
            self.companion_bridges.pop(companion_hash, None)
            self.companion_journals.pop(companion_hash_str, None)
            self._rf_reception_journals.pop(companion_hash_str, None)
            self._detach_companion_push_listener(
                journal,
                push_listener,
                companion_hash_str,
                companion_identity,
            )
            await self._stop_partial_companion(frame_server, bridge)
            raise
        if publication_error is not None:
            self._detach_companion_push_listener(
                journal,
                push_listener,
                companion_hash_str,
                companion_identity,
            )
            await self._stop_partial_companion(frame_server, bridge)
            raise IdentityConfigurationError(publication_error)

        limits = format_companion_bridge_limits(bridge_kwargs)
        frame_status = (
            f"port={tcp_port}, bind={bind_address}, "
            f"client_idle_timeout_sec={client_idle_timeout_sec}"
            if frame_enabled
            else "frame=disabled"
        )
        logger.info(
            f"Hot-reload: Loaded companion '{name}': hash=0x{companion_hash:02x}, "
            f"{frame_status}{limits}"
        )

    async def remove_companion(
        self,
        name: str,
        *,
        identity_key=None,
    ) -> bool:
        """Detach one companion atomically, then drain its private components.

        ``identity_key`` lets a delete resolve the same immutable companion
        after a restart-required configuration rename.  The full public key is
        verified before removal, so a stale request cannot detach a different
        runtime that later reused the configured name.
        """
        async with self._companion_lifecycle_lock:
            if self.identity_manager is None:
                return False

            expected_public_key = None
            if identity_key is not None:
                from openhop_core import LocalIdentity

                if isinstance(identity_key, str):
                    key_bytes = bytes.fromhex(
                        normalize_companion_identity_key(identity_key)
                    )
                elif isinstance(identity_key, bytes):
                    key_bytes = identity_key
                else:
                    raise ValueError("Companion identity_key has unknown type")
                if len(key_bytes) not in (32, 64):
                    raise ValueError(
                        "Companion identity_key must be 32 or 64 bytes"
                    )
                expected_public_key = bytes(
                    LocalIdentity(seed=key_bytes).get_public_key()
                )

            retiring_name = name
            retiring = self._retiring_companions.get(retiring_name)
            if (
                retiring is not None
                and expected_public_key is not None
                and retiring.get("companion_public_key") != expected_public_key
            ):
                retiring = None
            if retiring is None and expected_public_key is not None:
                for candidate_name, candidate in self._retiring_companions.items():
                    if (
                        candidate.get("companion_public_key")
                        == expected_public_key
                    ):
                        retiring_name = candidate_name
                        retiring = candidate
                        break
            if retiring is not None:
                return await self._drain_retiring_companion(
                    retiring_name,
                    retiring,
                )

            registered = self.identity_manager.get_identity_by_name(name)
            if (
                registered is not None
                and registered[2] == "companion"
                and expected_public_key is not None
                and bytes(registered[0].get_public_key()) != expected_public_key
            ):
                registered = None
            if registered is None and expected_public_key is not None:
                for registered_name, identity, config in (
                    self.identity_manager.get_identities_by_type("companion")
                ):
                    if bytes(identity.get_public_key()) == expected_public_key:
                        name = registered_name
                        registered = (identity, config, "companion")
                        break
            if registered is None or registered[2] != "companion":
                return False

            identity = registered[0]
            companion_hash = identity.get_public_key()[0]
            companion_hash_str = f"0x{companion_hash:02x}"
            bridge = self.companion_bridges.get(companion_hash)
            frame_server = next(
                (
                    server
                    for server in self.companion_frame_servers
                    if getattr(server, "companion_hash", None) == companion_hash_str
                ),
                None,
            )
            push_registration = self._companion_push_listeners.pop(
                companion_hash_str,
                None,
            )

            # Detach every discovery/routing index before the first await.
            # In-flight work may finish on the retained handles, but no new
            # REST lookup or radio fan-out can enter a retiring bridge.
            if frame_server in self.companion_frame_servers:
                self.companion_frame_servers.remove(frame_server)
            self.companion_bridges.pop(companion_hash, None)
            self.companion_journals.pop(companion_hash_str, None)
            self._rf_reception_journals.pop(companion_hash_str, None)
            self.identity_manager.unregister_identity(name)
            if push_registration is not None:
                push_journal, push_listener, push_identity = push_registration
                self._detach_companion_push_listener(
                    push_journal,
                    push_listener,
                    companion_hash_str,
                    push_identity,
                )
            retiring = {
                "companion_hash": companion_hash,
                "companion_public_key": bytes(identity.get_public_key()),
                "frame_server": frame_server,
                "bridge": bridge,
            }
            self._retiring_companions[name] = retiring

            if not await self._drain_retiring_companion(name, retiring):
                return False

            logger.info(
                "Hot-reload: Removed companion '%s': hash=%s",
                name,
                companion_hash_str,
            )
            return True

    async def _drain_retiring_companion(
        self,
        name: str,
        retiring: dict[str, object],
    ) -> bool:
        """Independently stop and retain only failed companion components."""
        failed = False
        for key, label in (
            ("frame_server", "Frame server"),
            ("bridge", "bridge"),
        ):
            component = retiring.get(key)
            if component is None:
                continue
            try:
                await component.stop()
            except Exception as exc:
                failed = True
                logger.warning(
                    "Hot-reload: Companion '%s' %s stop failed: %s",
                    name,
                    label,
                    exc,
                    exc_info=True,
                )
            else:
                # Identity comparison prevents a stale retry from erasing a
                # replacement handle, even in minimal test embeddings.
                if retiring.get(key) is component:
                    retiring[key] = None

        if failed or any(
            retiring.get(key) is not None for key in ("frame_server", "bridge")
        ):
            return False
        if self._retiring_companions.get(name) is retiring:
            self._retiring_companions.pop(name, None)
        return True

    async def _on_raw_rx_for_companions(
        self, data: bytes, rssi: int, snr: float, exclude_hash: str | None = None
    ) -> None:
        """Raw RX subscriber: push PUSH_CODE_LOG_RX_DATA (0x88) to connected companion clients.

        ``exclude_hash`` skips the frame server for that companion hash; used when
        echoing a companion's own injected TX so it never hears its own transmission.
        OTA RX subscribers leave it unset, so received packets reach every companion.
        """
        servers = tuple(getattr(self, "companion_frame_servers", ()))
        if not servers:
            return
        for fs in servers:
            if exclude_hash is not None and getattr(fs, "companion_hash", None) == exclude_hash:
                continue
            try:
                fs.push_rx_raw(snr, rssi, data)
            except Exception as e:
                logger.debug("Push RX raw to companion: %s", e)

    def _register_raw_custom_handler(self) -> None:
        """Register firmware-compatible RAW_CUSTOM handling ahead of fallback routing."""
        if self.dispatcher:
            self.dispatcher.register_handler(
                PAYLOAD_TYPE_RAW_CUSTOM, self._on_raw_data_for_companions
            )

    async def _on_raw_data_for_companions(self, packet) -> None:
        """Deliver final direct RAW_CUSTOM packets and route direct intermediate hops."""
        if not packet.is_route_direct():
            return

        if getattr(packet, "path", None):
            await self._router_callback(packet)
            return

        handler = self.repeater_handler
        if handler:
            if handler.is_duplicate(packet):
                return
            handler.mark_seen(packet)

        for bridge in tuple(self.companion_bridges.values()):
            try:
                await bridge.process_received_packet(packet)
            except Exception as e:
                logger.debug("Companion bridge RAW_CUSTOM error: %s", e)

    def _companion_duplicate_observer(self, packet_record: dict) -> None:
        """RepeaterHandler's ``duplicate_observer`` hook (design doc §10.4).

        Consults the process-wide correlation tracker for every genuine OTA
        duplicate; on a hit, journals a ``message_reception`` (inbound) or
        ``message_send_state`` (outbound heard-repeat) event via the
        matching companion's journal, and — for inbound hits — write-throughs
        the running counters onto the message row in the same transaction as
        the event (§10.6), so they survive ``packets`` retention pruning.
        RepeaterHandler already wraps this call in try/except, but errors are
        caught here too so one bad hit (e.g. a journal write failure) never
        drops the others in the list.

        After the correlation-hit handling above, also journals an opt-in
        ``rf_reception`` event (design doc §9 "Correlated vs. uncorrelated
        receptions") to every companion that has enabled
        ``rf_reception_events`` in its settings, for this same genuine OTA
        duplicate — regardless of whether it correlated to anything. The
        common case (no companion opted in) costs one falsy dict check.
        """
        if self.correlation_tracker is not None:
            hits = self.correlation_tracker.observe_duplicate(packet_record)
            if hits:
                for hit in hits:
                    journal = self.companion_journals.get(hit["companion_hash"])
                    if journal is None:
                        continue
                    try:
                        if hit.get("message_id") is None:
                            logger.error(
                                "Ignoring non-durable companion correlation "
                                "for companion=%s packet_hash=%s",
                                hit.get("companion_hash"),
                                hit.get("packet_hash"),
                            )
                            continue
                        if hit["direction"] == "in":
                            journal.record_inbound_reception(hit)
                        else:
                            journal.record_outbound_heard_repeat(hit)
                        # Promotion and observation are deliberately two
                        # phase: clear the bounded in-memory aggregate only
                        # after the event/counter transaction commits.  A
                        # transient storage failure is retried by the next
                        # genuine duplicate instead of silently losing RF
                        # evidence.
                        self.correlation_tracker.acknowledge(hit)
                    except Exception:
                        logger.exception(
                            "Companion correlation hit failed for companion=%s packet_hash=%s",
                            hit.get("companion_hash"),
                            hit.get("packet_hash"),
                        )

        if not self._rf_reception_journals:
            return
        for journal in self._rf_reception_journals.values():
            try:
                journal.record_rf_reception(packet_record)
            except Exception:
                logger.exception(
                    "rf_reception journal write failed for packet_hash=%s",
                    packet_record.get("packet_hash"),
                )

    def _register_duplicate_logging_hook(self, dedupe_enabled: bool) -> None:
        """Register pre-dedup duplicate logging only when dispatcher dedupe is active."""
        if not self.dispatcher or not dedupe_enabled:
            return
        # When dispatcher dedupe is disabled, duplicates still flow through
        # router -> repeater_handler and are already recorded there.
        self.dispatcher.add_raw_packet_subscriber(self._on_raw_packet_for_dedup_logging)

    def _on_raw_packet_for_dedup_logging(self, pkt, data: bytes, analysis: dict) -> None:
        """Record duplicate packets for UI visibility.

        Called by Dispatcher's raw_packet_subscriber (pre-dedup) so we see
        all path variants.  Only records packets the engine has already seen;
        novel packets are left for the normal handler path.
        """
        if not self.repeater_handler:
            return
        if not self.repeater_handler.is_duplicate(pkt):
            return  # First variant — will reach engine via normal handler path
        rssi = getattr(pkt, "_rssi", 0) or 0
        snr = getattr(pkt, "_snr", 0.0) or 0.0
        self.repeater_handler.record_duplicate(pkt, rssi=rssi, snr=snr)

    def _frame_response_owners(self, kind: str, tag: int) -> tuple:
        """Resolve one globally unique Frame owner for a shared-radio response."""
        owners = []
        for frame_server in tuple(
            getattr(self, "companion_frame_servers", ())
        ):
            owns = getattr(frame_server, "owns_response_tag", None)
            if callable(owns) and owns(kind, tag):
                owners.append(frame_server)
        if len(owners) <= 1:
            return tuple(owners)
        logger.warning(
            "Dropping ambiguous %s response tag 0x%08X claimed by %s "
            "companion Frame servers",
            kind,
            tag,
            len(owners),
        )
        for frame_server in owners:
            discard = getattr(frame_server, "discard_response_tag", None)
            if callable(discard):
                discard(kind, tag)
        return ()

    def _frame_has_response_owner(self, kind: str, tag: int) -> bool:
        """Return whether any Frame request already reserves this radio tag."""

        for frame_server in tuple(
            getattr(self, "companion_frame_servers", ())
        ):
            owns = getattr(frame_server, "owns_response_tag", None)
            if callable(owns) and owns(kind, tag):
                return True
        return False

    def _is_unique_frame_response_owner(
        self,
        frame_server,
        kind: str,
        tag: int,
    ) -> bool:
        """Return whether one server is the sole claimant across this radio."""
        owners = self._frame_response_owners(kind, tag)
        return len(owners) == 1 and owners[0] is frame_server

    def _frame_response_tag_conflict(
        self,
        requesting_frame_server,
        kind: str,
        tag: int,
    ) -> bool:
        """Return whether another shared-radio client already owns this tag.

        Frame commands call this synchronously after making their local claim
        and before their first radio await. Because all Frame commands run on
        the daemon loop, that claim-and-check sequence is atomic with respect
        to the other Frame servers: the first claimant remains intact and a
        later claimant is rejected before RF transmission.
        """
        key = int(tag) & 0xFFFFFFFF
        if self._repeater_owns_response_tag(kind, key):
            return True
        for frame_server in tuple(
            getattr(self, "companion_frame_servers", ())
        ):
            if frame_server is requesting_frame_server:
                continue
            owns = getattr(frame_server, "owns_response_tag", None)
            if callable(owns) and owns(kind, key):
                return True
        return False

    def _repeater_owns_response_tag(self, kind: str, tag: int) -> bool:
        """Return whether the parallel Repeater API already owns this tag."""
        key = int(tag) & 0xFFFFFFFF
        if kind == "trace":
            pending = getattr(getattr(self, "trace_helper", None), "pending_pings", {})
            return key in pending
        if kind == "control":
            helper = getattr(self, "discovery_helper", None)
            owns = getattr(helper, "owns_response_tag", None)
            if callable(owns) and owns(key):
                return True
            handler = getattr(
                helper,
                "control_handler",
                None,
            )
            callbacks = getattr(handler, "_response_callbacks", {})
            return key in callbacks
        return False

    async def deliver_control_data(
        self,
        snr: float,
        rssi: int,
        path_len: int,
        path_bytes: bytes,
        payload_bytes: bytes,
    ) -> None:
        """Deliver CONTROL payload (e.g. discovery response) to companion clients (PUSH_CODE_CONTROL_DATA 0x8E)."""
        # Only push discovery responses (0x90); client expects these, not the request (0x80)
        if len(payload_bytes) < 6 or (payload_bytes[0] & 0xF0) != 0x90:
            return
        # Discovery is a multi-response request, but each tag still belongs to
        # exactly one Frame server. Repeater/API discovery tags have no Frame
        # owner and must not enter a parallel chat client's protocol stream.
        tag = int.from_bytes(payload_bytes[2:6], "little")
        servers = self._frame_response_owners("control", tag)
        if not servers:
            return
        logger.debug(
            "Delivering discovery response to %s companion(s): tag=0x%08X, len=%s",
            len(servers),
            tag,
            len(payload_bytes),
        )
        for fs in servers:
            try:
                await fs.push_control_data(snr, rssi, path_len, path_bytes, payload_bytes)
            except Exception as e:
                logger.warning("Companion push_control_data error: %s", e)

    async def _on_trace_complete_for_companions(self, packet, parsed_data) -> None:
        """Trace completed at this node: push PUSH_CODE_TRACE_DATA (0x89) to companion clients (firmware onTraceRecv)."""
        path_hashes = parsed_data.get("trace_path_bytes") or b""
        if not path_hashes:
            return
        flags = parsed_data.get("flags", 0)
        path_sz = flags & 0x03
        hash_len = len(path_hashes)
        expected_snr_len = hash_len >> path_sz
        if expected_snr_len <= 0:
            return
        tag = parsed_data.get("tag", 0)
        auth_code = parsed_data.get("auth_code", 0)
        servers = self._frame_response_owners("trace", tag)
        if not servers:
            return
        snr_scaled = max(-128, min(127, int(round(packet.get_snr() * 4))))
        snr_byte = snr_scaled if snr_scaled >= 0 else (256 + snr_scaled)
        # Firmware: memcpy path_snrs from pkt->path (length hash_len >> path_sz), then final SNR byte
        raw = bytes(packet.path)[:expected_snr_len]
        if len(raw) < expected_snr_len:
            raw = raw + b"\x00" * (expected_snr_len - len(raw))
        path_snrs = raw
        for fs in servers:
            try:
                await fs.push_trace_data_async(
                    hash_len, flags, tag, auth_code, path_hashes, path_snrs, snr_byte
                )
            except Exception as e:
                logger.debug("Push trace data to companion: %s", e)

    def _register_identity_everywhere(
        self, name: str, identity, config: dict, identity_type: str
    ) -> bool:
        """
        Register an identity with the manager and all helpers in one place.
        This is the single source of truth for identity registration.
        """
        # Register with identity manager
        success = self.identity_manager.register_identity(
            name=name, identity=identity, config=config, identity_type=identity_type
        )

        if not success:
            return False

        # Register with all helpers
        if self.login_helper:
            self.login_helper.register_identity(
                name=name, identity=identity, identity_type=identity_type, config=config
            )

        if self.text_helper:
            self.text_helper.register_identity(
                name=name,
                identity=identity,
                identity_type=identity_type,
                radio_config=self.config.get("radio", {}),
            )

        if self.protocol_request_helper:
            self.protocol_request_helper.register_identity(
                name=name, identity=identity, identity_type=identity_type
            )

        return True

    async def _router_callback(self, packet):
        """
        Single entry point for ALL packets.
        Enqueues packets for router processing.
        """
        if self.router:
            try:
                await self.router.enqueue(packet)
            except Exception as e:
                logger.error(f"Error enqueuing packet in router: {e}", exc_info=True)

    def register_text_handler_for_identity(
        self, name: str, identity, identity_type: str = "room_server", radio_config: dict = None
    ):

        if not self.text_helper:
            logger.warning("Text helper not initialized, cannot register identity")
            return False

        try:
            self.text_helper.register_identity(
                name=name,
                identity=identity,
                identity_type=identity_type,
                radio_config=radio_config or self.config.get("radio", {}),
            )
            logger.info(f"Registered text handler for {identity_type} '{name}'")
            return True
        except Exception as e:
            logger.error(f"Failed to register text handler for '{name}': {e}")
            return False

    def get_stats(self) -> dict:
        stats = {}

        if self.repeater_handler:
            stats = self.repeater_handler.get_stats()
            # Add public key if available
            if self.local_identity:
                try:
                    pubkey = self.local_identity.get_public_key()
                    stats["public_key"] = pubkey.hex()
                except Exception:
                    stats["public_key"] = None

        if self.gps_service:
            stats["gps"] = self.gps_service.get_summary()

        if self.sensor_manager:
            stats["sensors"] = self.sensor_manager.get_summary()

        stats["radio_status"] = self.radio_status
        if self.radio_error:
            stats["radio_error"] = self.radio_error

        return stats

    async def _get_companion_stats(self, stats_type: int) -> dict:
        """Return stats dict for companion CMD_GET_STATS (format expected by frame_server + meshcore_py)."""
        from repeater.companion.constants import (
            STATS_TYPE_CORE,
            STATS_TYPE_PACKETS,
            STATS_TYPE_RADIO,
        )

        if not self.repeater_handler:
            return {}
        engine = self.repeater_handler
        airtime = engine.airtime_mgr.get_stats()
        uptime_secs = int(time.time() - engine.start_time)
        queue_len = 0
        for bridge in getattr(self, "companion_bridges", {}).values():
            queue_len += getattr(getattr(bridge, "message_queue", None), "count", 0) or 0
        if stats_type == STATS_TYPE_CORE:
            return {
                "battery_mv": 0,
                "uptime_secs": uptime_secs,
                "errors": 0,
                "queue_len": min(255, queue_len),
            }
        if stats_type == STATS_TYPE_RADIO:
            noise_floor = int(engine.get_cached_noise_floor() or 0)
            radio = getattr(self, "dispatcher", None) and getattr(self.dispatcher, "radio", None)
            if radio:
                _r = getattr(radio, "get_last_rssi", lambda: 0)
                _s = getattr(radio, "get_last_snr", lambda: 0.0)
                last_rssi = _r() if callable(_r) else _r
                last_snr = _s() if callable(_s) else _s
            else:
                last_rssi, last_snr = 0, 0.0
            tx_air_secs = int(airtime.get("total_airtime_ms", 0) / 1000)
            return {
                "noise_floor": noise_floor,
                "last_rssi": int(last_rssi) if last_rssi is not None else 0,
                "last_snr": float(last_snr) if last_snr is not None else 0.0,
                "tx_air_secs": tx_air_secs,
                "rx_air_secs": 0,
            }
        if stats_type == STATS_TYPE_PACKETS:
            return {
                "recv": getattr(engine, "rx_count", 0),
                "sent": getattr(engine, "forwarded_count", 0),
                "flood_tx": getattr(engine, "forwarded_count", 0),
                "direct_tx": 0,
                "flood_rx": getattr(engine, "rx_count", 0),
                "direct_rx": 0,
                "recv_errors": getattr(engine, "dropped_count", 0),
            }
        return {}

    async def send_advert(self) -> bool:

        if not self.dispatcher or not self.local_identity:
            logger.error("Cannot send advert: dispatcher or identity not initialized")
            return False

        mode = self.config.get("repeater", {}).get("mode", "forward")
        if mode == "no_tx":
            logger.debug("Adverts disabled in no_tx mode")
            return False

        try:
            from openhop_core.protocol.constants import (
                ADVERT_FLAG_HAS_NAME,
                ADVERT_FLAG_IS_REPEATER,
            )

            # Get node name and location from config
            repeater_config = self.config.get("repeater", {})
            node_name = repeater_config.get("node_name", "Repeater")
            latitude = repeater_config.get("latitude", 0.0)
            longitude = repeater_config.get("longitude", 0.0)
            location_source = "config"

            if self.gps_service:
                location = self.gps_service.get_repeater_location()
                latitude = location.get("latitude", latitude)
                longitude = location.get("longitude", longitude)
                location_source = str(location.get("source", location_source))

            flags = ADVERT_FLAG_IS_REPEATER | ADVERT_FLAG_HAS_NAME

            mesh_config = self.config.get("mesh", {})
            default_region = mesh_config.get("default_region")
            packet, scoped_region_name = create_scoped_advert_packet(
                local_identity=self.local_identity,
                node_name=node_name,
                latitude=latitude,
                longitude=longitude,
                flags=flags,
                default_region=default_region,
                scope_label="advert",
            )

            injector = getattr(getattr(self, "router", None), "inject_packet", None)
            if callable(injector):
                sent = await injector(packet, wait_for_ack=False)
            else:
                sent = await self.dispatcher.send_packet(packet, wait_for_ack=False)

            if not sent:
                logger.error("Failed to send advert: packet transmission was rejected")
                return False

            if not callable(injector) and self.repeater_handler:
                self.repeater_handler.mark_seen(packet)
                pkt_hash = packet.calculate_packet_hash().hex()[:16]
                self.dispatcher.packet_filter.track_packet(pkt_hash)
                logger.debug("Marked own advert as seen in duplicate cache")

            logger.info(
                "Sent flood advert '%s' at (%.6f, %.6f) source=%s",
                node_name,
                latitude,
                longitude,
                location_source,
            )
            if scoped_region_name:
                logger.info("Advert scoped to default region '%s'", scoped_region_name)
            return True

        except Exception as e:
            logger.error(f"Failed to send advert: {e}", exc_info=True)
            return False

    def _update_repeater_location_from_gps(self, location: dict) -> bool:
        """Persist the latest valid GPS fix as the repeater's advertised location."""
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if latitude is None or longitude is None:
            return False

        repeater_config = self.config.setdefault("repeater", {})
        current_latitude = repeater_config.get("latitude")
        current_longitude = repeater_config.get("longitude")
        try:
            if (
                current_latitude is not None
                and current_longitude is not None
                and abs(float(current_latitude) - float(latitude)) < 0.000001
                and abs(float(current_longitude) - float(longitude)) < 0.000001
            ):
                return False
        except (TypeError, ValueError):
            pass

        updates = {
            "repeater": {
                "latitude": float(latitude),
                "longitude": float(longitude),
            }
        }
        if self.config_manager:
            result = self.config_manager.update_and_save(
                updates=updates,
                live_update=True,
                live_update_sections=["repeater"],
            )
            if not result.get("success"):
                logger.warning(
                    "GPS location fix could not update repeater config: %s",
                    result.get("error", "unknown error"),
                )
                return False
        else:
            repeater_config.update(updates["repeater"])

        logger.info(
            "Updated repeater location from GPS fix: latitude=%.6f longitude=%.6f",
            latitude,
            longitude,
        )
        return True

    def _signal_shutdown(self, sig, loop):
        """Handle SIGTERM/SIGINT by scheduling async shutdown."""
        if self._shutdown_started or self._shutdown_task is not None:
            logger.info(f"Received signal {sig.name}, shutdown already in progress")
            return
        logger.info(f"Received signal {sig.name}, shutting down...")
        self._shutdown_task = loop.create_task(self._shutdown())
        # Cancel run() so dispatcher.run_forever() unwinds cleanly.
        if self._main_task and not self._main_task.done():
            self._main_task.cancel()

    async def _shutdown(self):
        """Best-effort shutdown: stop background services and release hardware."""
        current_task = asyncio.current_task()
        owner_task = self._shutdown_task
        if owner_task is not None and owner_task is not current_task:
            await asyncio.shield(owner_task)
            return
        if self._shutdown_started:
            return
        if owner_task is None:
            self._shutdown_task = current_task
        self._shutdown_started = True

        # Quiesce every HTTP API before stopping the shared companion bridges
        # or radio router. Otherwise a request accepted during teardown can
        # resolve a still-published bridge and enqueue RF work after its
        # workers have stopped.
        if self.http_server:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self.http_server.stop),
                    timeout=3,
                )
            except asyncio.TimeoutError:
                logger.warning("Timeout stopping HTTP server")
            except Exception as e:
                logger.warning(f"Error stopping HTTP server: {e}")

        # Stop the push notifier's worker thread before tearing down journals.
        if self.push_notifier is not None:
            try:
                self.push_notifier.stop()
            except Exception as e:
                logger.warning(f"Push notifier stop error: {e}")
        self._companion_push_listeners.clear()

        retiring = tuple(
            getattr(self, "_retiring_companions", {}).values()
        )
        frame_servers = list(getattr(self, "companion_frame_servers", ()))
        frame_servers.extend(
            item.get("frame_server")
            for item in retiring
            if item.get("frame_server") is not None
        )
        # Stop companion frame servers first to close client sockets and child workers.
        seen_components = set()
        for frame_server in frame_servers:
            if id(frame_server) in seen_components:
                continue
            seen_components.add(id(frame_server))
            try:
                await frame_server.stop()
            except Exception as e:
                logger.warning(f"Companion frame server stop error: {e}")

        # Stop companion bridges to flush/persist state.
        bridges = list(getattr(self, "companion_bridges", {}).values())
        bridges.extend(
            item.get("bridge")
            for item in retiring
            if item.get("bridge") is not None
        )
        seen_components.clear()
        for bridge in bridges:
            if id(bridge) in seen_components:
                continue
            seen_components.add(id(bridge))
            if hasattr(bridge, "stop"):
                try:
                    await bridge.stop()
                except Exception as e:
                    logger.warning(f"Companion bridge stop error: {e}")

        # Stop router
        if self.router:
            try:
                await self.router.stop()
            except Exception as e:
                logger.warning(f"Error stopping router: {e}")

        # Stop Glass inform loop
        if self.glass_handler:
            try:
                await self.glass_handler.stop()
            except Exception as e:
                logger.warning(f"Error stopping Glass handler: {e}")

        # Stop sensor manager.
        if self.sensor_manager:
            try:
                self.sensor_manager.stop()
            except Exception as e:
                logger.warning(f"Error stopping sensor manager: {e}")

        # Stop GPS diagnostics.
        if self.gps_service:
            try:
                self.gps_service.stop()
            except Exception as e:
                logger.warning(f"Error stopping GPS diagnostics: {e}")

        # Close storage publishers (MQTT/LetsMesh) to stop their worker threads.
        try:
            if self.repeater_handler and self.repeater_handler.storage:
                await asyncio.wait_for(
                    asyncio.to_thread(self.repeater_handler.storage.close), timeout=5
                )
        except asyncio.TimeoutError:
            logger.warning("Timeout closing storage publishers")
        except Exception as e:
            logger.warning(f"Error closing storage: {e}")

        # Release radio resources
        if self.radio and hasattr(self.radio, "cleanup"):
            try:
                self.radio.cleanup()
            except Exception as e:
                logger.warning(f"Error cleaning up radio: {e}")

        # Release CH341 USB device if in use
        try:
            radio_type_raw = self.config.get("radio_type")
            radio_type = "" if radio_type_raw is None else str(radio_type_raw).lower()
            if radio_type == "sx1262_ch341":
                from openhop_core.hardware.ch341.ch341_async import CH341Async

                CH341Async.reset_instance()
        except Exception as e:
            logger.debug(f"CH341 reset skipped/failed: {e}")

        # Do not force-stop the event loop here; asyncio.run() owns loop lifecycle.

    @staticmethod
    def _detect_container() -> bool:
        """Detect if running inside an LXC/Docker/systemd-nspawn container."""
        try:
            with open("/proc/1/environ", "rb") as f:
                if b"container=" in f.read():
                    return True
        except (OSError, PermissionError):
            pass
        return os.path.exists("/run/host/container-manager")

    async def run(self):

        logger.info("Repeater daemon started")
        self._main_task = asyncio.current_task()

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                functools.partial(self._signal_shutdown, sig, loop),
            )

        # Warn if running inside a container (udev rules won't work here)
        if os.path.exists("/.dockerenv") or os.environ.get("container") or self._detect_container():
            logger.warning(
                "Container environment detected. "
                "USB device udev rules must be configured on the HOST, not inside this container."
            )

        try:
            await self.initialize()

            # Start HTTP stats server
            http_config = self.config.get("http", {})
            http_port = http_config.get("port", 8000)
            http_host = http_config.get("host", "0.0.0.0")  # nosec B104
            http_enabled_raw = http_config.get("enabled", True)
            if isinstance(http_enabled_raw, str):
                http_enabled = http_enabled_raw.strip().lower() in (
                    "1",
                    "true",
                    "yes",
                    "on",
                )
            else:
                http_enabled = bool(http_enabled_raw)

            if http_enabled:
                node_name = self.config.get("repeater", {}).get(
                    "node_name",
                    "Repeater",
                )

                # Format public key for display.
                pub_key_formatted = ""
                if self.local_identity:
                    pub_key_hex = self.local_identity.get_public_key().hex()
                    if len(pub_key_hex) >= 16:
                        pub_key_formatted = f"{pub_key_hex[:8]}...{pub_key_hex[-8:]}"
                    else:
                        pub_key_formatted = pub_key_hex

                self.http_server = HTTPStatsServer(
                    host=http_host,
                    port=http_port,
                    stats_getter=self.get_stats,
                    node_name=node_name,
                    pub_key=pub_key_formatted,
                    send_advert_func=self.send_advert,
                    config=self.config,
                    event_loop=asyncio.get_event_loop(),
                    daemon_instance=self,
                    config_path=getattr(
                        self,
                        "config_path",
                        "/etc/openhop_repeater/config.yaml",
                    ),
                )
                try:
                    self.http_server.start()
                except Exception as e:
                    logger.error(f"Failed to start HTTP server: {e}")
                    raise RuntimeError(
                        "Enabled HTTP API failed to start"
                    ) from e
            else:
                logger.info("HTTP server startup skipped (http.enabled=false)")

            # Run dispatcher (handles RX/TX via openhop_core)
            try:
                await self.dispatcher.run_forever()
            except asyncio.CancelledError:
                logger.info("Dispatcher loop cancelled for shutdown")
            except KeyboardInterrupt:
                logger.info("Shutting down...")
        except asyncio.CancelledError:
            # Signals are registered before initialize(), so cancellation can
            # arrive before the dispatcher-specific handler above exists.
            # A requested shutdown is a normal exit; unrelated task
            # cancellation retains asyncio's ordinary propagation semantics.
            if self._shutdown_task is None and not self._shutdown_started:
                raise
            logger.info("Daemon task cancelled for requested shutdown")
        finally:
            await self._shutdown()


def main():

    import argparse

    parser = argparse.ArgumentParser(description="openHop Repeater Daemon")
    parser.add_argument(
        "--config",
        help="Path to config file (default: /etc/openhop_repeater/config.yaml)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )

    args = parser.parse_args()

    # Load configuration
    config = load_config(args.config)
    config_path = args.config if args.config else "/etc/openhop_repeater/config.yaml"

    if args.log_level:
        if "logging" not in config:
            config["logging"] = {}
        config["logging"]["level"] = args.log_level

    # Don't initialize radio here - it will be done inside the async event loop
    daemon = RepeaterDaemon(config, radio=None)
    daemon.config_path = config_path

    # Run
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        logger.info("Repeater stopped")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
