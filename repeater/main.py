import asyncio
import logging
import os
import sys
import time

from repeater.companion.utils import validate_companion_node_name, normalize_companion_identity_key
from repeater.config import get_radio_for_board, load_config, save_config
from repeater.config_manager import ConfigManager
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
from repeater.identity_manager import IdentityManager
from repeater.packet_router import PacketRouter
from repeater.web.http_server import HTTPStatsServer, _log_buffer

logger = logging.getLogger("RepeaterDaemon")


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
        self.acl = None
        self.router = None
        self.companion_bridges: dict[int, object] = {}
        self.companion_frame_servers: list = []

        log_level = config.get("logging", {}).get("level", "INFO")
        logging.basicConfig(
            level=getattr(logging, log_level),
            format=config.get("logging", {}).get("format"),
        )

        root_logger = logging.getLogger()
        _log_buffer.setLevel(getattr(logging, log_level))
        root_logger.addHandler(_log_buffer)

    async def initialize(self):

        logger.info(f"Initializing repeater: {self.config['repeater']['node_name']}")

        if self.radio is None:
            radio_type = self.config.get("radio_type", "sx1262")
            logger.info(f"Initializing radio hardware... (radio_type={radio_type})")
            try:
                self.radio = get_radio_for_board(self.config)

                # KISS modem: schedule RX callbacks on the event loop for thread safety
                if hasattr(self.radio, "set_event_loop"):
                    self.radio.set_event_loop(asyncio.get_running_loop())

                if hasattr(self.radio, "set_custom_cad_thresholds"):
                    # Load CAD settings from config, with defaults
                    cad_config = self.config.get("radio", {}).get("cad", {})
                    peak_threshold = cad_config.get("peak_threshold", 23)
                    min_threshold = cad_config.get("min_threshold", 11)

                    self.radio.set_custom_cad_thresholds(peak=peak_threshold, min_val=min_threshold)
                    logger.info(
                        f"CAD thresholds set from config: peak={peak_threshold}, min={min_threshold}"
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
                raise RuntimeError("Repeater requires real LoRa hardware") from e

        try:
            from pymc_core import LocalIdentity
            from pymc_core.node.dispatcher import Dispatcher

            self.dispatcher = Dispatcher(self.radio)
            logger.info("Dispatcher initialized")

            # Initialize Identity Manager for additional identities (e.g., room servers)
            self.identity_manager = IdentityManager(self.config)
            logger.info("Identity manager initialized")

            # Set up default repeater identity (not managed by identity manager)
            identity_key = self.config.get("mesh", {}).get("identity_key")
            if not identity_key:
                logger.error("No identity key found in configuration. Cannot init repeater.")
                raise RuntimeError("Identity key is required for repeater operation")

            local_identity = LocalIdentity(seed=identity_key)
            self.local_identity = local_identity
            self.dispatcher.local_identity = local_identity

            pubkey = local_identity.get_public_key()
            self.local_hash = pubkey[0]
            self.local_hash_bytes = bytes(pubkey[:3])

            logger.info(f"Local identity set: {local_identity.get_address_bytes().hex()}")
            local_hash_hex = f"0x{self.local_hash:02x}"
            logger.info(f"Local node hash (from identity): {local_hash_hex}")

            # Load additional identities from config (e.g., room servers)
            await self._load_additional_identities()

            self.dispatcher._is_own_packet = lambda pkt: False

            self.repeater_handler = RepeaterHandler(
                self.config, self.dispatcher, self.local_hash,
                local_hash_bytes=self.local_hash_bytes,
                send_advert_func=self.send_advert,
            )

            # Create router
            self.router = PacketRouter(self)
            await self.router.start()

            # Register router as entry point for ALL packets via fallback handler
            # All received packets flow through router → helpers → repeater engine
            self.dispatcher.register_fallback_handler(self._router_callback)
            logger.info("Packet router registered as fallback (catches all packets)")

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
                )
                logger.info("Discovery processing helper initialized")
            else:
                logger.info("Discovery response handler disabled")

            # Create login helper (will create per-identity ACLs)
            self.login_helper = LoginHelper(
                identity_manager=self.identity_manager,
                packet_injector=self.router.inject_packet,
                log_fn=logger.info,
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
                config_path=getattr(self, "config_path", "/etc/pymc_repeater/config.yaml"),
                config=self.config,
                daemon_instance=self,
            )
            logger.info("Config manager initialized")

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
            )
            # Register repeater identity for protocol requests
            self.protocol_request_helper.register_identity(
                name="repeater", identity=self.local_identity, identity_type="repeater"
            )
            logger.info("Protocol request handler initialized")

            # Load companion identities (CompanionBridge + frame server per companion)
            await self._load_companion_identities()

            # Subscribe to raw RX in pyMC_core so we can push PUSH_CODE_LOG_RX_DATA to companion clients
            self.dispatcher.add_raw_rx_subscriber(self._on_raw_rx_for_companions)
            n = len(getattr(self, "companion_frame_servers", []))
            logger.info(
                "Raw RX subscriber registered (%s companion frame server(s)). Connect a client to see rx_log (0x88).",
                n,
            )

            # When trace reaches final node, push PUSH_CODE_TRACE_DATA (0x89) to companion clients (firmware onTraceRecv)
            self.trace_helper.on_trace_complete = self._on_trace_complete_for_companions

        except Exception as e:
            logger.error(f"Failed to initialize dispatcher: {e}")
            raise

    async def _load_additional_identities(self):
        from pymc_core import LocalIdentity

        identities_config = self.config.get("identities", {})

        # Load room server identities
        room_servers = identities_config.get("room_servers") or []
        for room_config in room_servers:
            try:
                name = room_config.get("name")
                identity_key = room_config.get("identity_key")

                if not name or not identity_key:
                    logger.warning(f"Skipping room server config: missing name or identity_key")
                    continue

                # Convert identity_key to bytes if it's a hex string
                if isinstance(identity_key, bytes):
                    identity_key_bytes = identity_key
                elif isinstance(identity_key, str):
                    try:
                        identity_key_bytes = bytes.fromhex(identity_key)
                        if len(identity_key_bytes) != 32:
                            logger.error(
                                f"Identity key for '{name}' is invalid length: {len(identity_key_bytes)} bytes (expected 32)"
                            )
                            continue
                    except ValueError as e:
                        logger.error(f"Identity key for '{name}' is not valid hex: {e}")
                        continue
                else:
                    logger.error(
                        f"Identity key for '{name}' has unknown type: {type(identity_key)}"
                    )
                    continue

                # Create the identity
                room_identity = LocalIdentity(seed=identity_key_bytes)

                # Register with the manager and all helpers
                success = self._register_identity_everywhere(
                    name=name,
                    identity=room_identity,
                    config=room_config,
                    identity_type="room_server",
                )

                if success:
                    room_hash = room_identity.get_public_key()[0]
                    logger.info(
                        f"Loaded room server '{name}': hash=0x{room_hash:02x}, "
                        f"address={room_identity.get_address_bytes().hex()}"
                    )

            except Exception as e:
                logger.error(f"Failed to load room server identity '{name}': {e}")

        # Summary logging
        total_identities = len(self.identity_manager.list_identities())
        logger.info(f"Identity manager loaded {total_identities} total identities")

    async def _load_companion_identities(self) -> None:
        """Load companion identities from config and create CompanionBridge + frame server for each."""
        from pymc_core import LocalIdentity
        from pymc_core.companion.models import Channel, Contact

        from repeater.companion import CompanionFrameServer, RepeaterCompanionBridge

        companions_config = self.config.get("identities", {}).get("companions") or []
        if not companions_config:
            return

        sqlite_handler = None
        if self.repeater_handler and self.repeater_handler.storage:
            sqlite_handler = self.repeater_handler.storage.sqlite_handler
        if not sqlite_handler and companions_config:
            logger.warning(
                "Companion persistence disabled: no storage (contacts/channels will not survive restart or disconnect)"
            )

        radio_config = (
            self.repeater_handler.radio_config
            if self.repeater_handler
            else self.config.get("radio", {})
        )

        for comp_config in companions_config:
            try:
                name = comp_config.get("name")
                identity_key = comp_config.get("identity_key")
                settings = comp_config.get("settings") or {}

                if not name or not identity_key:
                    logger.warning("Skipping companion config: missing name or identity_key")
                    continue

                if isinstance(identity_key, str):
                    try:
                        identity_key_bytes = bytes.fromhex(normalize_companion_identity_key(identity_key))
                    except ValueError as e:
                        logger.error(f"Companion '{name}' identity_key invalid hex: {e}")
                        continue
                elif isinstance(identity_key, bytes):
                    identity_key_bytes = identity_key
                else:
                    logger.error(f"Companion '{name}' identity_key has unknown type")
                    continue

                if len(identity_key_bytes) not in (32, 64):
                    logger.error(
                        f"Companion '{name}' identity_key must be 32 bytes (hex) or 64 bytes (MeshCore firmware key)"
                    )
                    continue

                identity = LocalIdentity(seed=identity_key_bytes)
                pubkey = identity.get_public_key()
                companion_hash = pubkey[0]
                companion_hash_str = f"0x{companion_hash:02x}"

                node_name = settings.get("node_name", name)
                tcp_port = settings.get("tcp_port", 5000)
                bind_address = settings.get("bind_address", "0.0.0.0")
                tcp_timeout_raw = settings.get("tcp_timeout", 120)
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

                bridge = RepeaterCompanionBridge(
                    identity=identity,
                    packet_injector=self.router.inject_packet,
                    node_name=node_name,
                    radio_config=radio_config,
                    sqlite_handler=sqlite_handler,
                    companion_hash=companion_hash_str,
                    on_prefs_saved=_make_sync_node_name_to_config(name),
                )

                # Load contacts from SQLite
                if sqlite_handler:
                    contact_rows = sqlite_handler.companion_load_contacts(companion_hash_str)
                    if contact_rows:
                        records = []
                        for row in contact_rows:
                            d = dict(row)
                            d["public_key"] = d.pop("pubkey", d.get("public_key", b""))
                            records.append(d)
                        bridge.contacts.load_from_dicts(records)

                    # Load channels from SQLite (normalize secret to 32 bytes to match
                    # CompanionBase.set_channel and GroupTextHandler/PacketBuilder)
                    channel_rows = sqlite_handler.companion_load_channels(companion_hash_str)
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
                        ch = Channel(name=row.get("name", ""), secret=raw)
                        bridge.channels.set(row.get("channel_idx", 0), ch)

                    # Preload queued messages from SQLite into bridge
                    for msg_dict in sqlite_handler.companion_load_messages(companion_hash_str):
                        from pymc_core.companion.models import QueuedMessage

                        sk = msg_dict.get("sender_key", b"")
                        if isinstance(sk, str):
                            sk = bytes.fromhex(sk)
                        bridge.message_queue.push(
                            QueuedMessage(
                                sender_key=sk,
                                txt_type=msg_dict.get("txt_type", 0),
                                timestamp=msg_dict.get("timestamp", 0),
                                text=msg_dict.get("text", ""),
                                is_channel=bool(msg_dict.get("is_channel", False)),
                                channel_idx=msg_dict.get("channel_idx", 0),
                                path_len=msg_dict.get("path_len", 0),
                            )
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
                    control_handler=(
                        self.discovery_helper.control_handler if self.discovery_helper else None
                    ),
                )
                await frame_server.start()
                self.companion_frame_servers.append(frame_server)

                self.identity_manager.register_identity(
                    name=name,
                    identity=identity,
                    config=comp_config,
                    identity_type="companion",
                )

                logger.info(
                    f"Loaded companion '{name}': hash=0x{companion_hash:02x}, "
                    f"port={tcp_port}, bind={bind_address}, client_idle_timeout_sec={client_idle_timeout_sec}"
                )

            except Exception as e:
                logger.error(f"Failed to load companion '{name}': {e}", exc_info=True)

    async def add_companion_from_config(self, comp_config: dict) -> None:
        """
        Load a single companion from config and register it (hot-reload).
        Creates RepeaterCompanionBridge, CompanionFrameServer, starts the server,
        and registers with identity_manager. Raises on error.
        """
        from pymc_core import LocalIdentity
        from pymc_core.companion.models import Channel

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
        bind_address = settings.get("bind_address", "0.0.0.0")
        tcp_timeout_raw = settings.get("tcp_timeout", 120)
        client_idle_timeout_sec = None if tcp_timeout_raw == 0 else int(tcp_timeout_raw)

        bridge = RepeaterCompanionBridge(
            identity=identity,
            packet_injector=self.router.inject_packet,
            node_name=node_name,
            radio_config=radio_config,
            sqlite_handler=sqlite_handler,
            companion_hash=companion_hash_str,
        )

        if sqlite_handler:
            contact_rows = sqlite_handler.companion_load_contacts(companion_hash_str)
            if contact_rows:
                records = []
                for row in contact_rows:
                    d = dict(row)
                    d["public_key"] = d.pop("pubkey", d.get("public_key", b""))
                    records.append(d)
                bridge.contacts.load_from_dicts(records)

            channel_rows = sqlite_handler.companion_load_channels(companion_hash_str)
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
                ch = Channel(name=row.get("name", ""), secret=raw)
                bridge.channels.set(row.get("channel_idx", 0), ch)

            for msg_dict in sqlite_handler.companion_load_messages(companion_hash_str):
                from pymc_core.companion.models import QueuedMessage

                sk = msg_dict.get("sender_key", b"")
                if isinstance(sk, str):
                    sk = bytes.fromhex(sk)
                bridge.message_queue.push(
                    QueuedMessage(
                        sender_key=sk,
                        txt_type=msg_dict.get("txt_type", 0),
                        timestamp=msg_dict.get("timestamp", 0),
                        text=msg_dict.get("text", ""),
                        is_channel=bool(msg_dict.get("is_channel", False)),
                        channel_idx=msg_dict.get("channel_idx", 0),
                        path_len=msg_dict.get("path_len", 0),
                    )
                )

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
            control_handler=(
                self.discovery_helper.control_handler if self.discovery_helper else None
            ),
        )
        await frame_server.start()
        self.companion_frame_servers.append(frame_server)

        self.identity_manager.register_identity(
            name=name,
            identity=identity,
            config=comp_config,
            identity_type="companion",
        )

        logger.info(
            f"Hot-reload: Loaded companion '{name}': hash=0x{companion_hash:02x}, "
            f"port={tcp_port}, bind={bind_address}, client_idle_timeout_sec={client_idle_timeout_sec}"
        )

    async def _on_raw_rx_for_companions(self, data: bytes, rssi: int, snr: float) -> None:
        """Raw RX subscriber: push PUSH_CODE_LOG_RX_DATA (0x88) to connected companion clients."""
        servers = getattr(self, "companion_frame_servers", [])
        if not servers:
            return
        for fs in servers:
            try:
                fs.push_rx_raw(snr, rssi, data)
            except Exception as e:
                logger.debug("Push RX raw to companion: %s", e)

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
        path_len = len(parsed_data.get("trace_path", []))
        if path_len == 0:
            return
        path_hashes = bytes(parsed_data["trace_path"])
        flags = parsed_data.get("flags", 0)
        tag = parsed_data.get("tag", 0)
        auth_code = parsed_data.get("auth_code", 0)
        # path_snrs: exactly path_len bytes = (path_len-1) from forwarding hops + 1 (our receive SNR)
        snr_scaled = max(-128, min(127, int(round(packet.get_snr() * 4))))
        snr_byte = snr_scaled if snr_scaled >= 0 else (256 + snr_scaled)
        path_snrs = bytes(packet.path)[: path_len - 1] + bytes([snr_byte])
        for fs in getattr(self, "companion_frame_servers", []):
            try:
                fs.push_trace_data(
                    path_len, flags, tag, auth_code, path_hashes, path_snrs, snr_byte
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
            noise_floor = int(engine.get_noise_floor() or 0)
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

        try:
            from pymc_core.protocol import PacketBuilder
            from pymc_core.protocol.constants import ADVERT_FLAG_HAS_NAME, ADVERT_FLAG_IS_REPEATER

            # Get node name and location from config
            repeater_config = self.config.get("repeater", {})
            node_name = repeater_config.get("node_name", "Repeater")
            latitude = repeater_config.get("latitude", 0.0)
            longitude = repeater_config.get("longitude", 0.0)

            flags = ADVERT_FLAG_IS_REPEATER | ADVERT_FLAG_HAS_NAME

            packet = PacketBuilder.create_advert(
                local_identity=self.local_identity,
                name=node_name,
                lat=latitude,
                lon=longitude,
                feature1=0,
                feature2=0,
                flags=flags,
                route_type="flood",
            )

            # Send via dispatcher
            await self.dispatcher.send_packet(packet, wait_for_ack=False)

            # Mark our own advert as seen to prevent re-forwarding it
            if self.repeater_handler:
                self.repeater_handler.mark_seen(packet)
                logger.debug("Marked own advert as seen in duplicate cache")

            logger.info(f"Sent flood advert '{node_name}' at ({latitude: .6f}, {longitude: .6f})")
            return True

        except Exception as e:
            logger.error(f"Failed to send advert: {e}", exc_info=True)
            return False

    async def _shutdown(self):
        """Best-effort shutdown: stop background services and release hardware."""
        # Stop router
        if self.router:
            try:
                await self.router.stop()
            except Exception as e:
                logger.warning(f"Error stopping router: {e}")

        # Stop HTTP server
        if self.http_server:
            try:
                self.http_server.stop()
            except Exception as e:
                logger.warning(f"Error stopping HTTP server: {e}")

        # Release radio resources
        if self.radio and hasattr(self.radio, "cleanup"):
            try:
                self.radio.cleanup()
            except Exception as e:
                logger.warning(f"Error cleaning up radio: {e}")

        # Release CH341 USB device if in use
        try:
            if self.config.get("radio_type", "sx1262").lower() == "sx1262_ch341":
                from pymc_core.hardware.ch341.ch341_async import CH341Async

                CH341Async.reset_instance()
        except Exception as e:
            logger.debug(f"CH341 reset skipped/failed: {e}")

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

        # Warn if running inside a container (udev rules won't work here)
        if os.path.exists("/.dockerenv") or os.environ.get("container") or self._detect_container():
            logger.warning(
                "Container environment detected. "
                "USB device udev rules must be configured on the HOST, not inside this container."
            )

        try:
            await self.initialize()

            # Start HTTP stats server
            http_port = self.config.get("http", {}).get("port", 8000)
            http_host = self.config.get("http", {}).get("host", "0.0.0.0")

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
                config_path=getattr(self, "config_path", "/etc/pymc_repeater/config.yaml"),
            )

            try:
                self.http_server.start()
            except Exception as e:
                logger.error(f"Failed to start HTTP server: {e}")

            # Run dispatcher (handles RX/TX via pymc_core)
            try:
                await self.dispatcher.run_forever()
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

    parser = argparse.ArgumentParser(description="pyMC Repeater Daemon")
    parser.add_argument(
        "--config",
        help="Path to config file (default: /etc/pymc_repeater/config.yaml)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )

    args = parser.parse_args()

    # Load configuration
    config = load_config(args.config)
    config_path = args.config if args.config else "/etc/pymc_repeater/config.yaml"

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
