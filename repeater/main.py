import asyncio
import functools
import logging
import os
import signal
import socket
import sys
import threading
import time

from openhop_core.companion.radio_capabilities import resolve_max_tx_power_dbm
from openhop_core.protocol.constants import PAYLOAD_TYPE_RAW_CUSTOM

from repeater.companion.utils import (
    CompanionContactCapacityError,
    CompanionStateLoadError,
    effective_max_contacts,
    enforce_companion_contact_capacity,
    format_companion_bridge_limits,
    normalize_companion_identity_key,
    parse_companion_bridge_kwargs,
    validate_companion_node_name,
)
from repeater.config import (
    NullRadio,
    build_radio_stack,
    load_config,
    save_config,
)
from repeater.config_manager import ConfigManager
from repeater.data_acquisition.glass_handler import GlassHandler
from repeater.data_acquisition.gps_service import GPSService
from repeater.engine import RepeaterHandler
from repeater.exceptions import ConfigurationError
from repeater.handler_helpers import (
    AdvertHelper,
    DiscoveryHelper,
    LoginHelper,
    NeighborScopeHelper,
    PathHelper,
    ProtocolRequestHelper,
    TextHelper,
    TraceHelper,
)
from repeater.identity_manager import IdentityConfigurationError, IdentityManager, IdentitySpec
from repeater.logging_utils import normalize_log_level
from repeater.neighbors_publisher import NeighborsPublisher
from repeater.packet_router import PacketRouter
from repeater.region_map_builder import build_region_map
from repeater.sensors import SensorManager
from repeater.utils_packet import create_scoped_advert_packet
from repeater.web.http_server import HTTPStatsServer, _log_buffer

logger = logging.getLogger("RepeaterDaemon")

_COMPANION_LOAD_RETRY_DELAY_SEC = 0.5
_PERIODIC_ADVERT_STAGGER_SECONDS = 10.0


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
        self.neighbor_scope_helper = None
        self.neighbors_publisher = None
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
        # Shared RegionMap describing the named regions this repeater serves.
        # Wired into the dispatcher and every companion bridge so core can
        # re-scope flood replies to the region their request arrived under
        # (firmware sendFloodReply parity). Rebuilt on any transport_keys change.
        self._region_map = None
        # Parsed once during the startup preflight; the identity loaders reuse
        # them so config parsing (and its warnings) does not run twice.
        self._room_server_specs: list[IdentitySpec] | None = None
        self._companion_specs: list[IdentitySpec] | None = None
        self._shutdown_started = False
        self._main_task = None
        # Set by the first shutdown signal so a second one is ignored while
        # run() is still unwinding.
        self._stop_requested = False
        self.radio_status = "unknown"
        self.radio_error = None
        self._periodic_advert_last_sent: dict[str, float] = {}

        log_level = normalize_log_level(config.get("logging", {}).get("level", "INFO"))
        logging.basicConfig(
            level=log_level,
            format=config.get("logging", {}).get("format"),
        )

        root_logger = logging.getLogger()
        _log_buffer.setLevel(log_level)
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
        specs = [
            IdentitySpec("repeater", local_identity, self.config, "repeater"),
            *self._room_server_specs,
            *self._companion_specs,
        ]
        manager = self.identity_manager or IdentityManager(self.config)
        manager.validate_specs(specs)

    def _get_sqlite_handler(self):
        """Return the shared SQLiteHandler, or None if storage is unavailable."""
        handler = self.repeater_handler
        storage = getattr(handler, "storage", None) if handler else None
        return getattr(storage, "sqlite_handler", None) if storage else None

    def _init_region_map(self) -> None:
        """Build the shared RegionMap, wire it into the dispatcher, and hook rebuilds.

        Called once storage is available (right after ``repeater_handler`` is
        created) and before any companion bridge is built, so the dispatcher and
        every bridge share the same instance. Runtime region edits (transport_keys
        CRUD from the CLI, web API, or Glass sync) fire the storage change hook,
        which reruns ``refresh_region_map``.
        """
        sqlite_handler = self._get_sqlite_handler()
        self._region_map = build_region_map(self.config, sqlite_handler)
        if self.dispatcher is not None:
            self.dispatcher.region_map = self._region_map
        if sqlite_handler is not None and hasattr(
            sqlite_handler, "set_transport_keys_changed_callback"
        ):
            sqlite_handler.set_transport_keys_changed_callback(self.refresh_region_map)
        logger.info(
            "Region map initialized with %d served region(s)",
            len(self._region_map.regions),
        )

    def refresh_region_map(self) -> None:
        """Rebuild the RegionMap and reassign it to the dispatcher and all bridges.

        Fires from the storage transport_keys change hook whenever a named region
        is added, removed, or has its flood policy changed. A fresh instance is
        reassigned (rather than mutated in place) because this may run in a
        cherrypy worker thread while ``find_match`` iterates the map on the RX
        hot path in the event-loop thread: an attribute rebind is atomic under
        the GIL, so an in-flight match keeps using the old, fully-built map.
        New bridges pick up the current instance at creation time.
        """
        new_map = build_region_map(self.config, self._get_sqlite_handler())
        self._region_map = new_map
        if self.dispatcher is not None:
            self.dispatcher.region_map = new_map
        for bridge in list(self.companion_bridges.values()):
            try:
                bridge.region_map = new_map
            except Exception:
                logger.debug("Failed to update region map on a companion bridge", exc_info=True)
        logger.info("Region map refreshed with %d served region(s)", len(new_map.regions))

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
                self.radio, self.radio_stack_meta = build_radio_stack(self.config)
                meta = getattr(self, "radio_stack_meta", {}) or {}
                if meta.get("fabric"):
                    logger.info(
                        "RF fabric active: mode=%s radios=%s default=%s tx_mode=%s",
                        meta.get("mode"),
                        meta.get("radio_ids"),
                        meta.get("default_radio"),
                        meta.get("tx_mode"),
                    )

                # Physical radios for per-device setup (CAD, event loop).
                root = getattr(self.radio, "_radio", self.radio)
                fabric = getattr(root, "fabric", None)
                if fabric is not None and getattr(fabric, "radios", None):
                    physicals = list(fabric.radios.values())
                else:
                    physicals = [root]

                if all(isinstance(r, NullRadio) for r in physicals):
                    self.radio = physicals[0] if physicals else NullRadio()
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

                # KISS modem / multi-radio: schedule RX on the event loop.
                loop = asyncio.get_running_loop()
                for r in physicals:
                    if hasattr(r, "set_event_loop"):
                        r.set_event_loop(loop)

                # CAD from global radio.cad defaults applied to each physical radio.
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

                cad_applied = False
                for r in physicals:
                    if hasattr(r, "set_custom_cad_thresholds"):
                        r.set_custom_cad_thresholds(peak=peak_threshold, min_val=min_threshold)
                        if hasattr(r, "set_custom_cad_symbol_num"):
                            r.set_custom_cad_symbol_num(symbol_num)
                        cad_applied = True
                if cad_applied:
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
                raise ConfigurationError("Identity key is required for repeater operation")

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

            self.repeater_handler = RepeaterHandler(
                self.config,
                self.dispatcher,
                self.local_hash,
                local_hash_bytes=self.local_hash_bytes,
                send_advert_func=self.send_advert,
                periodic_advert_tick_func=self.run_periodic_advert_scheduler_tick,
            )

            # Storage now exists: build the served-region map and wire it into the
            # dispatcher so flood replies are re-scoped to their request's region.
            # Runs before any companion bridge is created so all share one instance.
            self._init_region_map()

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
            if self.repeater_handler and self.repeater_handler.storage:
                self.repeater_handler.storage.advert_stats_getter = (
                    self.advert_helper.get_rate_limit_stats
                )

            # Set up discovery handler if enabled
            allow_discovery = self.config.get("repeater", {}).get("allow_discovery", True)
            if allow_discovery:
                self.discovery_helper = DiscoveryHelper(
                    local_identity=self.local_identity,
                    packet_injector=self.router.inject_packet,
                    node_type=2,
                    log_fn=logger.info,
                    debug_log_fn=logger.debug,
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

            # Neighbour scope discovery + periodic MQTT neighbors publication.
            # Created unconditionally and self-gating: the publisher idles unless
            # the master switch is on and some broker opted in with neighbors:true.
            self.neighbor_scope_helper = NeighborScopeHelper(
                local_identity=self.local_identity,
                packet_injector=self.router.inject_packet,
                airtime_manager=(
                    getattr(self.repeater_handler, "airtime_mgr", None)
                    if self.repeater_handler
                    else None
                ),
                config=self.config,
            )
            self.neighbors_publisher = NeighborsPublisher(
                config=self.config,
                local_identity=self.local_identity,
                discovery_helper=self.discovery_helper,
                scope_helper=self.neighbor_scope_helper,
                mqtt_handler_provider=lambda: getattr(
                    getattr(self.repeater_handler, "storage", None), "mqtt_handler", None
                ),
                storage_provider=lambda: getattr(self.repeater_handler, "storage", None),
                self_scopes_fn=(
                    self.login_helper._format_region_names if self.login_helper else None
                ),
            )
            self.neighbors_publisher.start()
            logger.info("Neighbors publisher initialized")

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

    async def _load_companion_identities(self) -> None:
        """Load companion identities from config and create CompanionBridge + frame server for each."""
        from repeater.companion import CompanionFrameServer, RepeaterCompanionBridge

        companion_specs = self._companion_specs
        if companion_specs is None:
            companion_specs = self._configured_identity_specs("companion")
        if not companion_specs:
            return

        # Validate the complete companion set before any bridge can restore or
        # mutate a hash-keyed SQLite namespace, or any TCP server can bind.
        self.identity_manager.validate_specs(companion_specs)

        sqlite_handler = None
        if self.repeater_handler and self.repeater_handler.storage:
            sqlite_handler = self.repeater_handler.storage.sqlite_handler
        if not sqlite_handler:
            logger.warning(
                "Companion persistence disabled: no storage (contacts/channels will not survive restart or disconnect)"
            )

        radio_config = (
            self.repeater_handler.radio_config
            if self.repeater_handler
            else self.config.get("radio", {})
        )

        for spec in companion_specs:
            name, identity, comp_config = spec.name, spec.identity, spec.config
            try:
                settings = comp_config.get("settings") or {}
                pubkey = identity.get_public_key()
                companion_hash = pubkey[0]
                companion_hash_str = f"0x{companion_hash:02x}"

                node_name = settings.get("node_name", name)
                tcp_port = settings.get("tcp_port", 5000)
                bind_address = settings.get("bind_address", "0.0.0.0")  # nosec B104
                tcp_timeout_raw = settings.get("tcp_timeout", 8 * 60 * 60)  # 8 hours
                client_idle_timeout_sec = None if tcp_timeout_raw == 0 else int(tcp_timeout_raw)

                def _make_sync_node_name_to_config(companion_name: str):
                    """Return a callback that syncs node_name to config for this companion (binds name at creation)."""

                    def _sync(new_node_name: str) -> None:
                        try:
                            validated = validate_companion_node_name(new_node_name)
                        except ValueError:
                            return
                        companions = (self.config.get("identities") or {}).get("companions") or []
                        for entry in companions:
                            if entry.get("name") == companion_name:
                                if "settings" not in entry:
                                    entry["settings"] = {}
                                entry["settings"]["node_name"] = validated
                                config_path = getattr(self, "config_path", None)
                                if config_path:
                                    save_config(self.config, config_path)
                                break

                    return _sync

                bridge_kwargs = parse_companion_bridge_kwargs(settings)
                max_contacts = effective_max_contacts(bridge_kwargs)
                if sqlite_handler:
                    trimmed = enforce_companion_contact_capacity(
                        companion_hash_str,
                        max_contacts,
                        sqlite_handler,
                        trim=bool(settings.get("trim_contacts_on_overflow")),
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
                    on_prefs_saved=_make_sync_node_name_to_config(name),
                    **bridge_kwargs,
                )

                # Share the dispatcher's served-region map so this bridge re-scopes
                # its own flood replies to the region the request arrived under.
                bridge.region_map = self._region_map

                # Feed this bridge every pre-dedup copy of a flood reply so its
                # return-path teacher can pick the best-received route rather than
                # the first-arrived one. The router hands a bridge only the first
                # copy (later ones are dropped by the engine's seen-table) and the
                # pre-dedup firehose lives on the dispatcher, which the bridge does
                # not own -- so the host has to wire it.
                if self.dispatcher:
                    self.dispatcher.add_raw_packet_subscriber(bridge.note_flood_copy)

                # Restore persisted state (contacts/channels/messages) from SQLite.
                # Raises CompanionStateLoadError instead of continuing with an
                # empty store when persisted rows exist but cannot be loaded.
                if sqlite_handler:
                    await self._restore_companion_state(
                        sqlite_handler, bridge, companion_hash_str, name
                    )

                # Ensure public channel (0) exists with default key for new companions
                from repeater.companion.constants import DEFAULT_PUBLIC_CHANNEL_SECRET

                if bridge.get_channel(0) is None:
                    bridge.set_channel(0, "Public", DEFAULT_PUBLIC_CHANNEL_SECRET)

                self.companion_bridges[companion_hash] = bridge

                frame_server = CompanionFrameServer(
                    bridge=bridge,
                    companion_hash=companion_hash_str,
                    port=tcp_port,
                    bind_address=bind_address,
                    client_idle_timeout_sec=client_idle_timeout_sec,
                    sqlite_handler=sqlite_handler,
                    local_hash=self.local_hash,
                    stats_getter=self._get_companion_stats,
                    batt_getter=self._companion_battery_mv,
                    storage_dir=self._companion_storage_dir(),
                    control_handler=(
                        self.discovery_helper.control_handler if self.discovery_helper else None
                    ),
                )
                await frame_server.start()
                self.companion_frame_servers.append(frame_server)

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

                limits = format_companion_bridge_limits(bridge_kwargs)
                logger.info(
                    f"Loaded companion '{name}': hash=0x{companion_hash:02x}, "
                    f"port={tcp_port}, bind={bind_address}, "
                    f"client_idle_timeout_sec={client_idle_timeout_sec}{limits}"
                )

            except CompanionContactCapacityError as e:
                logger.error("%s", e)
            except CompanionStateLoadError as e:
                logger.error("Companion init aborted: %s", e)
            except IdentityConfigurationError:
                raise
            except Exception as e:
                logger.error(f"Failed to load companion '{name}': {e}", exc_info=True)

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

    async def add_companion_from_config(self, comp_config: dict) -> None:
        """
        Load a single companion from config and register it (hot-reload).
        Creates RepeaterCompanionBridge, CompanionFrameServer, starts the server,
        and registers with identity_manager. Raises on error.
        """
        from openhop_core import LocalIdentity

        from repeater.companion import CompanionFrameServer, RepeaterCompanionBridge
        from repeater.companion.constants import DEFAULT_PUBLIC_CHANNEL_SECRET

        name = comp_config.get("name")
        identity_key = comp_config.get("identity_key")
        settings = comp_config.get("settings") or {}

        if not name or not identity_key:
            raise ValueError("Companion config missing name or identity_key")

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

        # Already registered?
        if name in self.identity_manager.named_identities:
            raise ValueError(f"Companion '{name}' is already registered")

        identity = LocalIdentity(seed=identity_key_bytes)
        pubkey = identity.get_public_key()
        companion_hash = pubkey[0]
        companion_hash_str = f"0x{companion_hash:02x}"

        if self.identity_manager is None:
            raise RuntimeError("Identity manager must be initialized before adding a companion")
        registration_error = self.identity_manager.registration_error(name, identity, "companion")
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

        node_name = settings.get("node_name", name)
        tcp_port = settings.get("tcp_port", 5000)
        bind_address = settings.get("bind_address", "0.0.0.0")  # nosec B104
        tcp_timeout_raw = settings.get("tcp_timeout", 120)
        client_idle_timeout_sec = None if tcp_timeout_raw == 0 else int(tcp_timeout_raw)

        bridge_kwargs = parse_companion_bridge_kwargs(settings)
        max_contacts = effective_max_contacts(bridge_kwargs)
        if sqlite_handler:
            trimmed = enforce_companion_contact_capacity(
                companion_hash_str,
                max_contacts,
                sqlite_handler,
                trim=bool(settings.get("trim_contacts_on_overflow")),
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
            **bridge_kwargs,
        )

        # Share the current served-region map (hot-reload path) so this bridge
        # re-scopes its flood replies to the region the request arrived under.
        bridge.region_map = self._region_map

        # Feed this bridge every pre-dedup copy of a flood reply so its
        # return-path teacher can pick the best-received route rather than
        # the first-arrived one. The router hands a bridge only the first
        # copy (later ones are dropped by the engine's seen-table) and the
        # pre-dedup firehose lives on the dispatcher, which the bridge does
        # not own -- so the host has to wire it.
        if self.dispatcher:
            self.dispatcher.add_raw_packet_subscriber(bridge.note_flood_copy)

        # Restore persisted state; raises CompanionStateLoadError when persisted
        # rows exist but cannot be loaded (hot-reload callers surface the error).
        if sqlite_handler:
            await self._restore_companion_state(sqlite_handler, bridge, companion_hash_str, name)

        if bridge.get_channel(0) is None:
            bridge.set_channel(0, "Public", DEFAULT_PUBLIC_CHANNEL_SECRET)

        self.companion_bridges[companion_hash] = bridge

        frame_server = CompanionFrameServer(
            bridge=bridge,
            companion_hash=companion_hash_str,
            port=tcp_port,
            bind_address=bind_address,
            client_idle_timeout_sec=client_idle_timeout_sec,
            sqlite_handler=sqlite_handler,
            local_hash=self.local_hash,
            stats_getter=self._get_companion_stats,
            batt_getter=self._companion_battery_mv,
            storage_dir=self._companion_storage_dir(),
            control_handler=(
                self.discovery_helper.control_handler if self.discovery_helper else None
            ),
        )
        await frame_server.start()
        self.companion_frame_servers.append(frame_server)

        if not self.identity_manager.register_identity(
            name=name,
            identity=identity,
            config=comp_config,
            identity_type="companion",
        ):
            raise IdentityConfigurationError(f"Failed to register companion identity '{name}'")

        limits = format_companion_bridge_limits(bridge_kwargs)
        logger.info(
            f"Hot-reload: Loaded companion '{name}': hash=0x{companion_hash:02x}, "
            f"port={tcp_port}, bind={bind_address}, "
            f"client_idle_timeout_sec={client_idle_timeout_sec}{limits}"
        )

    async def _on_raw_rx_for_companions(
        self, data: bytes, rssi: int, snr: float, exclude_hash: str | None = None
    ) -> None:
        """Raw RX subscriber: push PUSH_CODE_LOG_RX_DATA (0x88) to connected companion clients.

        ``exclude_hash`` skips the frame server for that companion hash; used when
        echoing a companion's own injected TX so it never hears its own transmission.
        OTA RX subscribers leave it unset, so received packets reach every companion.
        """
        servers = getattr(self, "companion_frame_servers", [])
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

        for bridge in self.companion_bridges.values():
            try:
                await bridge.process_received_packet(packet)
            except Exception as e:
                logger.debug("Companion bridge RAW_CUSTOM error: %s", e)

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
        # Push every discovery response to the client, including our own (snr=0, rssi=0 = local node's response)
        servers = getattr(self, "companion_frame_servers", [])
        if not servers:
            return
        tag = int.from_bytes(payload_bytes[2:6], "little") if len(payload_bytes) >= 6 else 0
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
        snr_scaled = max(-128, min(127, int(round(packet.get_snr() * 4))))
        snr_byte = snr_scaled if snr_scaled >= 0 else (256 + snr_scaled)
        # Firmware: memcpy path_snrs from pkt->path (length hash_len >> path_sz), then final SNR byte
        raw = bytes(packet.path)[:expected_snr_len]
        if len(raw) < expected_snr_len:
            raw = raw + b"\x00" * (expected_snr_len - len(raw))
        path_snrs = raw
        for fs in getattr(self, "companion_frame_servers", []):
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

    def _companion_storage_dir(self) -> "str | None":
        """Filesystem path companion clients should see storage figures for."""
        try:
            path = (self.config.get("storage", {}) or {}).get("storage_dir")
            return path if path and os.path.isdir(path) else None
        except Exception:  # pragma: no cover - defensive
            return None

    def _companion_battery_mv(self) -> int:
        """Battery voltage in mV for the companion core-stats frame.

        MeshCore companion clients show the battery of the device they are
        connected to. A Python repeater has no battery of its own, but a sensor
        plug-in often reports one -- an attached UPS HAT, or the battery of an
        openHop modem the repeater is driving -- so surface the first sensor
        publishing a usable voltage instead of always reporting 0.

        Returns 0 when nothing is available, which is what clients already
        expect for a mains-powered node.
        """
        if not self.sensor_manager:
            return 0
        try:
            readings = (self.sensor_manager.get_summary() or {}).get("readings") or []
        except Exception:  # pragma: no cover - defensive; stats must never raise
            logger.debug("companion battery lookup failed", exc_info=True)
            return 0

        for reading in readings:
            if not isinstance(reading, dict) or not reading.get("ok"):
                continue
            data = reading.get("data")
            if not isinstance(data, dict):
                continue

            millivolts = data.get("battery_voltage_mv")
            if millivolts is None:
                volts = data.get("battery_voltage_v")
                if volts is None:
                    continue
                try:
                    millivolts = float(volts) * 1000.0
                except (TypeError, ValueError):
                    continue
            try:
                millivolts = round(float(millivolts))
            except (TypeError, ValueError):
                continue

            # The core-stats frame packs battery_mv as an unsigned 16-bit int.
            if 0 < millivolts <= 0xFFFF:
                return millivolts
        return 0

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
                "battery_mv": self._companion_battery_mv(),
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

    @staticmethod
    def _coerce_interval_hours(raw_value, *, min_enabled: int = 1, max_enabled: int = 168) -> int:
        """Parse interval hours from config, returning 0 when disabled/invalid."""
        try:
            hours = int(raw_value)
        except (TypeError, ValueError):
            return 0
        if hours == 0:
            return 0
        if hours < min_enabled or hours > max_enabled:
            return 0
        return hours

    def _periodic_advert_due(self, stream_key: str, interval_hours: int, now: float) -> bool:
        """Return True when this stream should send now.

        Streams start their cadence at daemon start, matching the historical
        repeater timer behavior (first periodic advert after one full interval).
        """
        if interval_hours <= 0:
            return False
        interval_seconds = float(interval_hours) * 3600.0
        last_sent = self._periodic_advert_last_sent.setdefault(stream_key, now)
        return (now - last_sent) >= interval_seconds

    async def _send_room_server_advert(
        self,
        *,
        room_name: str,
        identity,
        identity_config: dict,
        advert_kind: str,
    ) -> bool:
        """Send an advert packet for a room-server identity."""
        if not self.dispatcher:
            logger.error("Cannot send room advert: dispatcher not initialized")
            return False

        try:
            from openhop_core.protocol.constants import (
                ADVERT_FLAG_HAS_NAME,
                ADVERT_FLAG_IS_ROOM_SERVER,
            )

            settings = (
                identity_config.get("settings", {}) if isinstance(identity_config, dict) else {}
            )
            node_name = settings.get("node_name", settings.get("room_name", room_name))
            latitude = settings.get("latitude", 0.0)
            longitude = settings.get("longitude", 0.0)
            flags = ADVERT_FLAG_IS_ROOM_SERVER | ADVERT_FLAG_HAS_NAME

            mesh_config = self.config.get("mesh", {})
            default_region = mesh_config.get("default_region")
            advert_route_type = "direct" if advert_kind == "direct" else "flood"
            packet, scoped_region_name = create_scoped_advert_packet(
                local_identity=identity,
                node_name=node_name,
                latitude=latitude,
                longitude=longitude,
                flags=flags,
                default_region=default_region,
                scope_label="room server advert",
                route_type=advert_route_type,
            )

            injector = getattr(getattr(self, "router", None), "inject_packet", None)
            if callable(injector):
                sent = await injector(packet, wait_for_ack=False)
            else:
                sent = await self.dispatcher.send_packet(packet, wait_for_ack=False)

            if not sent:
                logger.error("Failed to send room server advert: packet transmission was rejected")
                return False

            if not callable(injector) and self.repeater_handler:
                self.repeater_handler.mark_seen(packet)
                logger.debug("Marked room server advert '%s' as seen in duplicate cache", node_name)

            logger.info(
                "Sent %s room advert (%s packet) '%s' at (%.6f, %.6f)",
                advert_kind,
                advert_route_type,
                node_name,
                latitude,
                longitude,
            )
            if scoped_region_name:
                logger.info("Room server advert scoped to default region '%s'", scoped_region_name)
            return True
        except Exception as e:
            logger.error("Failed to send room server advert: %s", e, exc_info=True)
            return False

    async def run_periodic_advert_scheduler_tick(self) -> None:
        """Run one scheduler tick for repeater + room-server advert intervals.

        Called from RepeaterHandler's existing background loop every 5 seconds.
        """
        mode = self.config.get("repeater", {}).get("mode", "forward")
        if mode == "no_tx":
            return

        now = time.time()
        scheduled = []

        repeater_cfg = self.config.get("repeater", {}) if isinstance(self.config, dict) else {}
        repeater_flood_hours = self._coerce_interval_hours(
            repeater_cfg.get("send_advert_interval_hours", 10),
            min_enabled=3,
        )
        if self._periodic_advert_due("repeater:flood", repeater_flood_hours, now):
            scheduled.append(
                {
                    "key": "repeater:flood",
                    "label": "repeater flood advert",
                    "sender": lambda: self.send_advert(advert_kind="flood"),
                }
            )

        repeater_direct_hours = self._coerce_interval_hours(
            repeater_cfg.get("direct_advert_interval_hours", 0),
            min_enabled=1,
        )
        if self._periodic_advert_due("repeater:direct", repeater_direct_hours, now):
            scheduled.append(
                {
                    "key": "repeater:direct",
                    "label": "repeater direct advert",
                    "sender": lambda: self.send_advert(advert_kind="direct"),
                }
            )

        if self.identity_manager is not None:
            room_identities = sorted(
                self.identity_manager.get_identities_by_type("room_server"),
                key=lambda item: str(item[0]).lower(),
            )
            for room_name, room_identity, room_cfg in room_identities:
                settings = room_cfg.get("settings", {}) if isinstance(room_cfg, dict) else {}
                room_flood_hours = self._coerce_interval_hours(
                    settings.get("flood_advert_interval_hours", 0),
                    min_enabled=1,
                )
                room_flood_key = f"room:{room_name}:flood"
                if self._periodic_advert_due(room_flood_key, room_flood_hours, now):
                    scheduled.append(
                        {
                            "key": room_flood_key,
                            "label": f"room '{room_name}' flood advert",
                            "sender": (
                                lambda n=room_name, i=room_identity, c=room_cfg: (
                                    self._send_room_server_advert(
                                        room_name=n,
                                        identity=i,
                                        identity_config=c,
                                        advert_kind="flood",
                                    )
                                )
                            ),
                        }
                    )

                room_direct_hours = self._coerce_interval_hours(
                    settings.get("direct_advert_interval_hours", 0),
                    min_enabled=1,
                )
                room_direct_key = f"room:{room_name}:direct"
                if self._periodic_advert_due(room_direct_key, room_direct_hours, now):
                    scheduled.append(
                        {
                            "key": room_direct_key,
                            "label": f"room '{room_name}' direct advert",
                            "sender": (
                                lambda n=room_name, i=room_identity, c=room_cfg: (
                                    self._send_room_server_advert(
                                        room_name=n,
                                        identity=i,
                                        identity_config=c,
                                        advert_kind="direct",
                                    )
                                )
                            ),
                        }
                    )

        if not scheduled:
            return

        logger.info("Periodic advert scheduler: %d advert(s) due", len(scheduled))

        for idx, item in enumerate(scheduled):
            if idx > 0:
                await asyncio.sleep(_PERIODIC_ADVERT_STAGGER_SECONDS)

            stream_key = item["key"]
            self._periodic_advert_last_sent[stream_key] = time.time()
            try:
                ok = await item["sender"]()
                if ok:
                    logger.info("Periodic advert sent: %s", item["label"])
                else:
                    logger.warning("Periodic advert failed: %s", item["label"])
            except Exception as exc:
                logger.error(
                    "Periodic advert raised exception for %s: %s",
                    item["label"],
                    exc,
                    exc_info=True,
                )

    async def send_advert(self, advert_kind: str = "flood") -> bool:

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
            advert_route_type = "direct" if advert_kind == "direct" else "flood"
            packet, scoped_region_name = create_scoped_advert_packet(
                local_identity=self.local_identity,
                node_name=node_name,
                latitude=latitude,
                longitude=longitude,
                flags=flags,
                default_region=default_region,
                scope_label="advert",
                route_type=advert_route_type,
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
                "Sent %s advert (%s packet) '%s' at (%.6f, %.6f) source=%s",
                advert_kind,
                advert_route_type,
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
        if self._shutdown_started or self._stop_requested:
            logger.info(f"Received signal {sig.name}, shutdown already in progress")
            return
        logger.info(f"Received signal {sig.name}, shutting down...")
        self._stop_requested = True
        # Unwind run() *cooperatively* rather than cancelling it: stopping the
        # dispatcher makes run_forever() return, and run()'s finally then does
        # the cleanup inside a task that is not being cancelled.
        #
        # Cancelling run() instead — the previous behaviour — meant its finally
        # awaited _shutdown() from inside an already-cancelled task, so the first
        # await raised CancelledError, run() returned, and asyncio.run() tore the
        # loop down before any cleanup happened. Observed on SIGTERM: not one
        # shutdown step logged, all three companion listen sockets still bound
        # and the serial port still held, and the process then hung
        # indefinitely in interpreter finalization. Running cleanup in a sibling
        # task does not fix it either: run() returns as soon as the dispatcher
        # stops, and asyncio.run() cancels every leftover task on the way out.
        if self.dispatcher is not None and hasattr(self.dispatcher, "stop"):
            loop.create_task(self.dispatcher.stop())
        elif self._main_task and not self._main_task.done():
            # No dispatcher to stop (a failure before startup finished): fall
            # back to cancelling, and accept the reduced cleanup.
            self._main_task.cancel()

    # Per-step ceiling for shutdown. A best-effort shutdown must never be able to
    # hang: one stuck step used to strand the whole sequence, leaving sockets
    # bound and the serial port held.
    SHUTDOWN_STEP_TIMEOUT_S = 5.0
    # Grace period after cleanup before the process is forced down. Interpreter
    # finalization joins non-daemon threads with no timeout of its own, so a
    # single library thread that never returns hangs SIGTERM forever.
    SHUTDOWN_EXIT_GRACE_S = 5.0

    async def _shutdown_step(self, name: str, awaitable, timeout: float = None) -> None:
        """Await one shutdown step, bounded and logged; never raise."""
        try:
            await asyncio.wait_for(
                awaitable, timeout=self.SHUTDOWN_STEP_TIMEOUT_S if timeout is None else timeout
            )
        except asyncio.TimeoutError:
            logger.warning("Shutdown step '%s' timed out; continuing", name)
        except asyncio.CancelledError:
            logger.warning("Shutdown step '%s' was cancelled; continuing", name)
        except Exception as e:
            logger.warning("Shutdown step '%s' failed: %s", name, e)

    async def _shutdown(self):
        """Best-effort shutdown: stop background services and release hardware."""
        if self._shutdown_started:
            return
        self._shutdown_started = True
        logger.info("Shutdown: stopping services and releasing hardware")

        # Stop the dispatcher first so RX stops before its radio is taken away.
        if self.dispatcher is not None and hasattr(self.dispatcher, "stop"):
            await self._shutdown_step("dispatcher", self.dispatcher.stop())

        # Stop companion frame servers first to close client sockets and child workers.
        for frame_server in getattr(self, "companion_frame_servers", []):
            await self._shutdown_step(
                f"frame server :{getattr(frame_server, 'port', '?')}", frame_server.stop()
            )

        # Stop companion bridges to flush/persist state.
        if hasattr(self, "companion_bridges"):
            for companion_hash, bridge in self.companion_bridges.items():
                if hasattr(bridge, "stop"):
                    await self._shutdown_step(f"bridge 0x{companion_hash:02X}", bridge.stop())

        # Stop router
        if self.router:
            await self._shutdown_step("router", self.router.stop())

        # Stop HTTP server. Sync stop() runs off-loop so a wedged handler thread
        # cannot block the sequence.
        if self.http_server:
            await self._shutdown_step(
                "http server", asyncio.to_thread(self.http_server.stop), timeout=3
            )

        # Stop the neighbours publication loop.
        if self.neighbors_publisher:
            try:
                await self.neighbors_publisher.stop()
            except Exception as e:
                logger.warning(f"Error stopping neighbors publisher: {e}")

        # Stop Glass inform loop
        if self.glass_handler:
            await self._shutdown_step("glass handler", self.glass_handler.stop())

        # Stop sensor manager.
        if self.sensor_manager:
            await self._shutdown_step("sensor manager", asyncio.to_thread(self.sensor_manager.stop))

        # Stop GPS diagnostics.
        if self.gps_service:
            await self._shutdown_step("gps service", asyncio.to_thread(self.gps_service.stop))

        # Close storage publishers (MQTT/LetsMesh) to stop their worker threads.
        if self.repeater_handler and self.repeater_handler.storage:
            await self._shutdown_step(
                "storage publishers",
                asyncio.to_thread(self.repeater_handler.storage.close),
                timeout=5,
            )

        # Release radio resources. Off-loop for the same reason as the HTTP
        # server: closing a serial port can block on a stuck driver.
        if self.radio and hasattr(self.radio, "cleanup"):
            await self._shutdown_step("radio cleanup", asyncio.to_thread(self.radio.cleanup))

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
        logger.info("Shutdown: services stopped")
        self._report_lingering_threads()
        self._arm_exit_watchdog()

    @staticmethod
    def _report_lingering_threads() -> None:
        """Name any non-daemon threads that will block interpreter exit.

        Python joins non-daemon threads at finalization with no timeout, so one
        library thread that never returns hangs SIGTERM indefinitely — observed
        here for 18 minutes, the main thread parked in
        ``Py_FinalizeEx -> wait_for_thread_shutdown``. The watchdog below stops
        that from holding up a restart; this names the culprit so it can be
        fixed at the source rather than papered over every time.
        """
        current = threading.current_thread()
        expected, unexpected = [], []
        for t in threading.enumerate():
            if t is current or t is threading.main_thread() or t.daemon or not t.is_alive():
                continue
            # asyncio names its default-executor workers "asyncio_N". Those are
            # joined by asyncio.run() under its own timeout, so they are not the
            # unbounded kind; warning about them every shutdown would bury the
            # thread that actually matters.
            (expected if t.name.startswith("asyncio_") else unexpected).append(t.name)
        if unexpected:
            logger.warning(
                "Non-daemon threads still alive; these block interpreter exit: %s",
                ", ".join(sorted(unexpected)),
            )
        if expected:
            logger.debug("Executor threads still winding down: %s", ", ".join(sorted(expected)))

    def _arm_exit_watchdog(self) -> None:
        """Force the process down if finalization does not complete promptly.

        Cleanup is finished by the time this is armed, so exiting hard costs
        nothing and guarantees SIGTERM is honoured. A daemon timer, so it never
        becomes the thing holding the process open.
        """
        grace = self.SHUTDOWN_EXIT_GRACE_S

        def _force_exit() -> None:
            logger.warning(
                "Shutdown did not complete within %.0fs of cleanup finishing; exiting now",
                grace,
            )
            logging.shutdown()
            os._exit(0)

        watchdog = threading.Timer(grace, _force_exit)
        watchdog.name = "shutdown-watchdog"
        watchdog.daemon = True
        watchdog.start()

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

            node_name = self.config.get("repeater", {}).get("node_name", "Repeater")

            # Format public key for display
            pub_key_formatted = ""
            if self.local_identity:
                pub_key_hex = self.local_identity.get_public_key().hex()
                # Format as <first8...last8>
                if len(pub_key_hex) >= 16:
                    pub_key_formatted = f"{pub_key_hex[:8]}...{pub_key_hex[-8:]}"
                else:
                    pub_key_formatted = pub_key_hex

            current_loop = asyncio.get_event_loop()

            self.http_server = HTTPStatsServer(
                host=http_host,
                port=http_port,
                stats_getter=self.get_stats,
                node_name=node_name,
                pub_key=pub_key_formatted,
                send_advert_func=self.send_advert,
                config=self.config,
                event_loop=current_loop,
                daemon_instance=self,
                config_path=getattr(self, "config_path", "/etc/openhop_repeater/config.yaml"),
            )

            if http_enabled:
                try:
                    self.http_server.start()
                except Exception as e:
                    logger.error(f"Failed to start HTTP server: {e}")
            else:
                logger.info("HTTP server startup skipped (http.enabled=false)")

            # Run dispatcher (handles RX/TX via openhop_core)
            try:
                await self.dispatcher.run_forever()
            except asyncio.CancelledError:
                logger.info("Dispatcher loop cancelled for shutdown")
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                for frame_server in getattr(self, "companion_frame_servers", []):
                    try:
                        await frame_server.stop()
                    except Exception as e:
                        logger.debug(f"Companion frame server stop: {e}")
                if hasattr(self, "companion_bridges"):
                    for bridge in self.companion_bridges.values():
                        if hasattr(bridge, "stop"):
                            try:
                                await bridge.stop()
                            except Exception as e:
                                logger.debug(f"Companion bridge stop: {e}")
                if self.router:
                    await self.router.stop()
                if self.http_server:
                    self.http_server.stop()
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

    # Load configuration, build the daemon, and run it. Config mistakes (a
    # missing or invalid config file, a missing required key, colliding local
    # identities) surface as ConfigurationError and exit cleanly with just the
    # message; only unexpected failures get the full traceback.
    try:
        config = load_config(args.config)
        config_path = args.config if args.config else "/etc/openhop_repeater/config.yaml"

        if args.log_level:
            if "logging" not in config:
                config["logging"] = {}
            config["logging"]["level"] = args.log_level

        # Don't initialize radio here - it will be done inside the async event loop
        daemon = RepeaterDaemon(config, radio=None)
        daemon.config_path = config_path

        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        logger.info("Repeater stopped")
    except ConfigurationError as e:
        # An actionable config problem, not a crash: report just the message.
        logger.error("Configuration error: %s", e)
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
