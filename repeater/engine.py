import asyncio
import copy
import logging
import random
import struct
import time
from collections import OrderedDict, deque
from typing import Optional, Tuple

from pymc_core.node.handlers.base import BaseHandler
from pymc_core.protocol import Packet
from pymc_core.protocol.constants import (
    MAX_PATH_SIZE,
    PAYLOAD_TYPE_ADVERT,
    PAYLOAD_TYPE_ANON_REQ,
    PAYLOAD_TYPE_TRACE,
    PH_ROUTE_MASK,
    PH_TYPE_MASK,
    PH_TYPE_SHIFT,
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_TRANSPORT_DIRECT,
    ROUTE_TYPE_TRANSPORT_FLOOD,
)
from pymc_core.protocol.packet_utils import PacketHeaderUtils, PacketTimingUtils, PathUtils

from repeater.airtime import AirtimeManager
from repeater.data_acquisition import StorageCollector

logger = logging.getLogger("RepeaterHandler")

NOISE_FLOOR_INTERVAL = 30.0  # seconds

LOOP_DETECT_OFF = "off"
LOOP_DETECT_MINIMAL = "minimal"
LOOP_DETECT_MODERATE = "moderate"
LOOP_DETECT_STRICT = "strict"

# Thresholds for 1-byte path hashes loop detection.
# Count how many times our own hash already exists in the incoming FLOOD path.
# If occurrences >= threshold, treat as loop and drop.
LOOP_DETECT_MAX_COUNTERS = {
    LOOP_DETECT_MINIMAL: 4,
    LOOP_DETECT_MODERATE: 2,
    LOOP_DETECT_STRICT: 1,
}


class RepeaterHandler(BaseHandler):

    @staticmethod
    def payload_type() -> int:

        return 0xFF  # Special marker (not a real payload type)

    def __init__(self, config: dict, dispatcher, local_hash: int, *, local_hash_bytes=None, send_advert_func=None):

        self.config = config
        self.dispatcher = dispatcher
        self.local_hash = local_hash
        self.local_hash_bytes = local_hash_bytes or bytes([local_hash])
        self.send_advert_func = send_advert_func
        self.airtime_mgr = AirtimeManager(config)
        self.seen_packets = OrderedDict()
        self.cache_ttl = max(
            300, config.get("repeater", {}).get("cache_ttl", 3600)
        )  # Min 5 min, default 1 hour
        self.max_cache_size = 1000
        self.max_duplicates_per_packet = 20
        self.tx_delay_factor = config.get("delays", {}).get("tx_delay_factor", 1.0)
        self.direct_tx_delay_factor = config.get("delays", {}).get("direct_tx_delay_factor", 0.5)
        self.use_score_for_tx = config.get("repeater", {}).get("use_score_for_tx", False)
        self.score_threshold = config.get("repeater", {}).get("score_threshold", 0.3)
        self.max_flood_hops = config.get("repeater", {}).get("max_flood_hops", 64)
        self.send_advert_interval_hours = config.get("repeater", {}).get(
            "send_advert_interval_hours", 10
        )
        self.last_advert_time = time.time()
        self.loop_detect_mode = self._normalize_loop_detect_mode(
            config.get("mesh", {}).get("loop_detect", LOOP_DETECT_OFF)
        )

        radio = dispatcher.radio if dispatcher else None
        if radio:
            self.radio_config = {
                "spreading_factor": getattr(radio, "spreading_factor", 8),
                "bandwidth": getattr(radio, "bandwidth", 125000),
                "coding_rate": getattr(radio, "coding_rate", 8),
                "preamble_length": getattr(radio, "preamble_length", 17),
                "frequency": getattr(radio, "frequency", 915000000),
                "tx_power": getattr(radio, "tx_power", 14),
            }
            logger.info(
                f"radio settings: SF={self.radio_config['spreading_factor']}, "
                f"BW={self.radio_config['bandwidth']}Hz, CR={self.radio_config['coding_rate']}"
            )
        else:
            raise RuntimeError("Radio object not available - cannot initialize repeater")

        # Statistics tracking for dashboard
        self.rx_count = 0
        self.forwarded_count = 0
        self.dropped_count = 0
        self.max_recent_packets = 50
        self.recent_packets = deque(maxlen=self.max_recent_packets)
        self._recent_hash_index = {}
        self.start_time = time.time()
        # Flood/direct and duplicate counters (for GET_STATUS / firmware RepeaterStats)
        self.recv_flood_count = 0
        self.recv_direct_count = 0
        self.sent_flood_count = 0
        self.sent_direct_count = 0
        self.flood_dup_count = 0
        self.direct_dup_count = 0

        # Storage collector for persistent packet logging
        try:

            local_identity = dispatcher.local_identity if dispatcher else None
            self.storage = StorageCollector(config, local_identity, repeater_handler=self)
            logger.info("StorageCollector initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize StorageCollector: {e}")
            self.storage = None

        # Initialize background timer tracking
        self.last_noise_measurement = time.time()
        self.last_cache_cleanup = time.time()
        self.last_db_cleanup = time.time()
        self.noise_floor_interval = NOISE_FLOOR_INTERVAL  # 30 seconds
        self._background_task = None
        self._cached_noise_floor = None
        self._last_crc_error_count = 0  # Track radio counter for delta persistence
        
        # Cache transport keys for efficient lookup
        self._transport_keys_cache = None
        self._transport_keys_cache_time = 0
        self._transport_keys_cache_ttl = 60  # Cache for 60 seconds

        # Serialise all radio TX calls.
        #
        # Background: since the queue loop dispatches each packet as an
        # asyncio.create_task, multiple _route_packet coroutines can have their
        # TX delay timers running concurrently — which is the intended behaviour
        # (firmware nodes do the same with a hardware timer).  However, the
        # LoRa radio is half-duplex: it can only transmit one packet at a time.
        # Without serialisation, two tasks whose delay timers expire near-
        # simultaneously both call dispatcher.send_packet, interleaving SPI/serial
        # commands to the radio and both passing the LBT check before either has
        # actually transmitted.
        #
        # _tx_lock is acquired after each delay sleep and held for the entire
        # send_packet call.  Delays still run concurrently; only the radio
        # access is serialised.  This also eliminates the TOCTOU gap in duty-cycle
        # enforcement — see schedule_retransmit / delayed_send for details.
        self._tx_lock = asyncio.Lock()

        self._start_background_tasks()

    async def __call__(
        self, packet: Packet, metadata: Optional[dict] = None, local_transmission: bool = False
    ) -> None:

        if metadata is None:
            metadata = {}

        # Only count as receive when packet came from the radio (not locally injected)
        if not local_transmission:
            self.rx_count += 1
            route_type = packet.header & PH_ROUTE_MASK
            if route_type in (ROUTE_TYPE_FLOOD, ROUTE_TYPE_TRANSPORT_FLOOD):
                self.recv_flood_count += 1
            elif route_type in (ROUTE_TYPE_DIRECT, ROUTE_TYPE_TRANSPORT_DIRECT):
                self.recv_direct_count += 1
            try:
                rx_airtime_ms = self.airtime_mgr.calculate_airtime(packet.get_raw_length())
                self.airtime_mgr.record_rx(rx_airtime_ms)
            except Exception:
                pass

        route_type = packet.header & PH_ROUTE_MASK
        pkt_hash_full = packet.calculate_packet_hash().hex().upper()

        # TX mode: forward (repeat on), monitor (no repeat, tenants can TX), no_tx (all TX off)
        mode = self.config.get("repeater", {}).get("mode", "forward")
        if mode not in ("forward", "monitor", "no_tx"):
            mode = "forward"
        allow_forward = mode == "forward"
        allow_local_tx = mode != "no_tx"

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"RX packet: header=0x{packet.header:02x}, payload_len={len(packet.payload or b'')}, "
                f"path_len={len(packet.path) if packet.path else 0}, "
                f"rssi={metadata.get('rssi', 'N/A')}, snr={metadata.get('snr', 'N/A')}, mode={mode}"
            )

        # clone the packet to avoid modifying the original
        processed_packet = copy.deepcopy(packet)

        snr = metadata.get("snr", 0.0)
        rssi = metadata.get("rssi", 0)
        transmitted = False
        tx_delay_ms = 0.0
        drop_reason = None
        lbt_attempts = 0
        lbt_backoff_delays_ms = None
        lbt_channel_busy = False

        original_path_hashes = packet.get_path_hashes_hex()
        path_hash_size = packet.get_path_hash_size()

        # Process for forwarding (skip if repeat disabled or if this is a local transmission).
        # Pass pkt_hash_full so flood_forward / direct_forward don't recompute SHA-256.
        result = (
            None
            if (not allow_forward or local_transmission)
            else self.process_packet(processed_packet, snr, packet_hash=pkt_hash_full)
        )
        forwarded_path_hashes = None

        # For local transmissions, create a direct transmission result (if local TX allowed)
        if local_transmission and allow_local_tx:
            # Mark local packet as seen to prevent duplicate processing when received back
            self.mark_seen(packet, packet_hash=pkt_hash_full)
            # Calculate transmission delay for local packets
            delay = self._calculate_tx_delay(packet, snr)
            result = (packet, delay)
            forwarded_path_hashes = packet.get_path_hashes_hex()
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"Local transmission: calculated delay {delay:.3f}s")

        if result:
            fwd_pkt, delay = result
            tx_delay_ms = delay * 1000.0

            # Capture the forwarded path (after modification)
            forwarded_path_hashes = fwd_pkt.get_path_hashes_hex()

            # Check duty-cycle before scheduling TX
            airtime_ms = self.airtime_mgr.calculate_airtime(fwd_pkt.get_raw_length())

            can_tx, wait_time = self.airtime_mgr.can_transmit(airtime_ms)

            # LBT metadata (set after any TX path that awaits send)
            tx_metadata = None
            lbt_attempts = 0
            lbt_backoff_delays_ms = None
            lbt_channel_busy = False

            if not can_tx:
                if local_transmission:
                    # Defer local TX until duty cycle allows instead of dropping
                    deferred_delay = delay + wait_time
                    logger.info(
                        f"Duty-cycle limit: deferring local TX by {wait_time:.1f}s "
                        f"(airtime={airtime_ms:.1f}ms)"
                    )
                    self.forwarded_count += 1
                    transmitted = True
                    tx_task = await self.schedule_retransmit(
                        fwd_pkt, deferred_delay, airtime_ms, local_transmission=True
                    )
                    try:
                        await tx_task
                    except Exception as e:
                        self.forwarded_count -= 1
                        transmitted = False
                        drop_reason = "TX failed (deferred)"
                        logger.warning(f"Deferred local TX failed: {e}")
                        raise
                    tx_metadata = getattr(fwd_pkt, "_tx_metadata", None)
                    if tx_metadata:
                        lbt_attempts = tx_metadata.get("lbt_attempts", 0)
                        lbt_backoff_delays_ms = tx_metadata.get(
                            "lbt_backoff_delays_ms", []
                        )
                        lbt_channel_busy = tx_metadata.get("lbt_channel_busy", False)
                        if lbt_attempts > 0:
                            total_lbt_delay = sum(lbt_backoff_delays_ms)
                            logger.info(
                                f"LBT: {lbt_attempts} attempts, "
                                f"{total_lbt_delay:.0f}ms delay, "
                                f"backoffs={lbt_backoff_delays_ms}"
                            )
                else:
                    logger.warning(
                        f"Duty-cycle limit exceeded. Airtime={airtime_ms:.1f}ms, "
                        f"wait={wait_time:.1f}s before retry"
                    )
                    self.dropped_count += 1
                    drop_reason = "Duty cycle limit"
            else:
                self.forwarded_count += 1
                transmitted = True
                tx_task = await self.schedule_retransmit(
                    fwd_pkt, delay, airtime_ms, local_transmission=local_transmission
                )
                try:
                    await tx_task
                except Exception as e:
                    self.forwarded_count -= 1
                    transmitted = False
                    drop_reason = "TX failed"
                    logger.warning(f"Local TX failed: {e}")
                    raise
                tx_metadata = getattr(fwd_pkt, "_tx_metadata", None)
                if tx_metadata:
                    lbt_attempts = tx_metadata.get("lbt_attempts", 0)
                    lbt_backoff_delays_ms = tx_metadata.get("lbt_backoff_delays_ms", [])
                    lbt_channel_busy = tx_metadata.get("lbt_channel_busy", False)

                    if lbt_attempts > 0:
                        total_lbt_delay = sum(lbt_backoff_delays_ms)
                        logger.info(
                            f"LBT: {lbt_attempts} attempts, {total_lbt_delay:.0f}ms delay, "
                            f"backoffs={lbt_backoff_delays_ms}"
                        )
        else:
            self.dropped_count += 1
            # Determine drop reason
            if local_transmission and not allow_local_tx:
                drop_reason = "No TX mode"
            elif not allow_forward:
                drop_reason = "Repeat disabled"
            else:
                # Check if packet has a specific drop reason set by handlers
                drop_reason = processed_packet.drop_reason or self._get_drop_reason(
                    processed_packet, packet_hash=pkt_hash_full
                )
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"Packet not forwarded: {drop_reason}")

        # Extract packet type and route from header
        if not hasattr(packet, "header") or packet.header is None:
            logger.error(f"Packet missing header attribute! Packet: {packet}")
            payload_type = 0
            route_type = 0
        else:
            header_info = PacketHeaderUtils.parse_header(packet.header)
            payload_type = header_info["payload_type"]
            route_type = header_info["route_type"]
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    f"Packet header=0x{packet.header:02x}, type={payload_type}, route={route_type}"
                )

        # Check if this is a duplicate
        is_dupe = pkt_hash_full in self.seen_packets and not transmitted

        # Set drop reason for duplicates and count flood vs direct dups
        if is_dupe and drop_reason is None:
            drop_reason = "Duplicate"
        if is_dupe:
            if route_type in (ROUTE_TYPE_FLOOD, ROUTE_TYPE_TRANSPORT_FLOOD):
                self.flood_dup_count += 1
            elif route_type in (ROUTE_TYPE_DIRECT, ROUTE_TYPE_TRANSPORT_DIRECT):
                self.direct_dup_count += 1

        display_hashes = (
            original_path_hashes if original_path_hashes else packet.get_path_hashes_hex()
        )
        path_hash = self._path_hash_display(display_hashes)
        src_hash, dst_hash = self._packet_record_src_dst(packet, payload_type)

        # Record packet for charts
        packet_record = self._build_packet_record(
            packet,
            payload_type,
            route_type,
            rssi,
            snr,
            original_path_hashes,
            path_hash_size,
            path_hash,
            src_hash,
            dst_hash,
            transmitted=transmitted,
            drop_reason=drop_reason,
            is_duplicate=is_dupe,
            forwarded_path=forwarded_path_hashes,
            tx_delay_ms=tx_delay_ms,
            lbt_attempts=lbt_attempts,
            lbt_backoff_delays_ms=lbt_backoff_delays_ms,
            lbt_channel_busy=lbt_channel_busy,
            packet_hash=pkt_hash_full,
        )

        # Store packet record to persistent storage
        # Skip mqtt only for invalid packets (not duplicates or operational drops)
        if self.storage:
            try:
                # Only skip mqtt for actual invalid/bad packets
                invalid_reasons = ["Invalid advert packet", "Empty payload", "Path too long"]
                skip_mqtt = drop_reason in invalid_reasons if drop_reason else False
                self.storage.record_packet(packet_record, skip_mqtt_if_invalid=skip_mqtt)
            except Exception as e:
                logger.error(f"Failed to store packet record: {e}")

        # If this is a duplicate, try to attach it to the original packet
        if is_dupe and len(self.recent_packets) > 0:
            prev_pkt = self._recent_hash_index.get(packet_record["packet_hash"])
            if prev_pkt is not None:
                # Add duplicate to original packet's duplicate list
                if "duplicates" not in prev_pkt:
                    prev_pkt["duplicates"] = []
                if len(prev_pkt["duplicates"]) < self.max_duplicates_per_packet:
                    prev_pkt["duplicates"].append(packet_record)
                # Don't add duplicate to main list, just track in original
            else:
                # Original not found, add as regular packet
                self._append_recent_packet(packet_record)
        else:
            # Not a duplicate or first occurrence
            self._append_recent_packet(packet_record)

    def log_trace_record(self, packet_record: dict) -> None:
        """Manually log a packet trace record (used by external callers)"""
        self._append_recent_packet(packet_record)

        self.rx_count += 1
        if packet_record.get("transmitted", False):
            self.forwarded_count += 1
        else:
            self.dropped_count += 1

        # Store to persistent storage (same as __call__ does)
        if self.storage:
            try:
                self.storage.record_packet(packet_record)
            except Exception as e:
                logger.error(f"Failed to store packet record: {e}")

    def record_packet_only(self, packet: Packet, metadata: dict) -> None:
        """Record a packet for UI/storage without running forwarding or duplicate logic.

        Used by the packet router for injection-only types (ANON_REQ, ACK, PATH, etc.)
        so they still appear in the web UI.

        TRACE packets are excluded: TraceHelper.log_trace_record stores the real trace path;
        packet.path on TRACE holds SNR bytes, not routing hashes.
        """
        if not self.storage:
            return
        rssi = metadata.get("rssi", 0)
        snr = metadata.get("snr", 0.0)
        if not hasattr(packet, "header") or packet.header is None:
            logger.debug("record_packet_only: packet missing header, skipping")
            return
        header_info = PacketHeaderUtils.parse_header(packet.header)
        payload_type = header_info["payload_type"]
        route_type = header_info["route_type"]
        if payload_type == PAYLOAD_TYPE_TRACE:
            return
        original_path_hashes = packet.get_path_hashes_hex()
        path_hash_size = packet.get_path_hash_size()
        path_hash = self._path_hash_display(original_path_hashes)
        src_hash, dst_hash = self._packet_record_src_dst(packet, payload_type)
        packet_record = self._build_packet_record(
            packet,
            payload_type,
            route_type,
            rssi,
            snr,
            original_path_hashes,
            path_hash_size,
            path_hash,
            src_hash,
            dst_hash,
            packet_hash=packet.calculate_packet_hash().hex().upper(),
        )
        try:
            self.storage.record_packet(packet_record, skip_mqtt_if_invalid=False)
        except Exception as e:
            logger.error(f"Failed to store packet record (record_packet_only): {e}")
            return
        self._append_recent_packet(packet_record)

    def record_duplicate(self, packet: Packet, rssi: int = 0, snr: float = 0.0) -> None:
        """Record a known-duplicate packet for UI/storage visibility without forwarding.

        Called by the raw_packet_subscriber path so that path variants blocked
        by the Dispatcher's payload-based dedup still appear in the UI.
        """
        self.rx_count += 1
        route_type = packet.header & PH_ROUTE_MASK
        if route_type in (ROUTE_TYPE_FLOOD, ROUTE_TYPE_TRANSPORT_FLOOD):
            self.recv_flood_count += 1
            self.flood_dup_count += 1
        elif route_type in (ROUTE_TYPE_DIRECT, ROUTE_TYPE_TRANSPORT_DIRECT):
            self.recv_direct_count += 1
            self.direct_dup_count += 1

        header_info = PacketHeaderUtils.parse_header(packet.header)
        payload_type = header_info["payload_type"]
        route_type_parsed = header_info["route_type"]

        original_path_hashes = packet.get_path_hashes_hex()
        path_hash_size = packet.get_path_hash_size()
        path_hash = self._path_hash_display(original_path_hashes)
        src_hash, dst_hash = self._packet_record_src_dst(packet, payload_type)

        packet_record = self._build_packet_record(
            packet, payload_type, route_type_parsed, rssi, snr,
            original_path_hashes, path_hash_size, path_hash,
            src_hash, dst_hash,
            transmitted=False,
            drop_reason="Duplicate",
            is_duplicate=True,
            packet_hash=packet.calculate_packet_hash().hex().upper(),
        )

        if self.storage:
            try:
                self.storage.record_packet(packet_record, skip_mqtt_if_invalid=False)
            except Exception as e:
                logger.error(f"Failed to store duplicate record: {e}")

        # Group under original in recent_packets
        if len(self.recent_packets) > 0:
            prev_pkt = self._recent_hash_index.get(packet_record["packet_hash"])
            if prev_pkt is not None:
                if "duplicates" not in prev_pkt:
                    prev_pkt["duplicates"] = []
                prev_pkt["duplicates"].append(packet_record)
            else:
                self._append_recent_packet(packet_record)
        else:
            self._append_recent_packet(packet_record)

    def cleanup_cache(self):

        now = time.time()
        expired = [k for k, ts in self.seen_packets.items() if now - ts > self.cache_ttl]
        for k in expired:
            del self.seen_packets[k]

    def _path_hash_display(self, display_hashes) -> Optional[str]:
        """Build path hash string for packet record from path hashes list."""
        if not display_hashes:
            return None
        display = display_hashes[:8]
        if len(display_hashes) > 8:
            display = list(display) + ["..."]
        return "[" + ", ".join(display) + "]"

    def _packet_record_src_dst(
        self, packet: Packet, payload_type: int
    ) -> Tuple[Optional[str], Optional[str]]:
        """Return (src_hash, dst_hash) for packet_record from packet and payload_type."""
        src_hash = None
        dst_hash = None
        payload = getattr(packet, "payload", None)
        if payload_type in [0x00, 0x01, 0x02, 0x08]:
            if payload and len(payload) >= 2:
                dst_hash = f"{payload[0]:02X}"
                src_hash = f"{payload[1]:02X}"
        elif payload_type == PAYLOAD_TYPE_ADVERT:
            if payload and len(payload) >= 1:
                src_hash = f"{payload[0]:02X}"
        elif payload_type == PAYLOAD_TYPE_ANON_REQ:
            if payload and len(payload) >= 1:
                dst_hash = f"{payload[0]:02X}"
        return (src_hash, dst_hash)

    def _build_packet_record(
        self,
        packet: Packet,
        payload_type: int,
        route_type: int,
        rssi: int,
        snr: float,
        original_path_hashes,
        path_hash_size: int,
        path_hash: Optional[str],
        src_hash: Optional[str],
        dst_hash: Optional[str],
        *,
        transmitted: bool = False,
        drop_reason: Optional[str] = None,
        is_duplicate: bool = False,
        forwarded_path=None,
        tx_delay_ms: float = 0.0,
        lbt_attempts: int = 0,
        lbt_backoff_delays_ms=None,
        lbt_channel_busy: bool = False,
        packet_hash: Optional[str] = None,
    ) -> dict:
        """Build a single packet_record dict for storage and recent_packets."""
        pkt_hash = packet_hash or packet.calculate_packet_hash().hex().upper()
        payload = getattr(packet, "payload", None)
        payload_len = len(payload or b"")

        # LoRa time-on-air for this packet (Semtech reference formula).
        # Computed once here so every downstream consumer (MQTT, SQLite, Glass,
        # websocket) reads the same value instead of recomputing or shipping
        # zeros. The same calculator is used by the RX accumulator and the
        # duty-cycle gate, keeping reporting and metering aligned.
        airtime_ms = 0.0
        try:
            raw_len = packet.get_raw_length() if hasattr(packet, "get_raw_length") else 0
            if raw_len > 0:
                airtime_ms = float(self.airtime_mgr.calculate_airtime(raw_len))
        except Exception as e:
            logger.debug(f"Could not compute airtime for packet record: {e}")

        return {
            "timestamp": time.time(),
            "header": (
                f"0x{packet.header:02X}"
                if hasattr(packet, "header") and packet.header is not None
                else None
            ),
            "payload": payload.hex() if payload else None,
            "payload_length": len(payload) if payload else 0,
            "type": payload_type,
            "route": route_type,
            "length": payload_len,
            "rssi": rssi,
            "snr": snr,
            "score": self.calculate_packet_score(
                snr, payload_len, self.radio_config["spreading_factor"]
            ),
            "tx_delay_ms": tx_delay_ms,
            "airtime_ms": airtime_ms,
            "transmitted": transmitted,
            "is_duplicate": is_duplicate,
            "packet_hash": pkt_hash[:16],
            "drop_reason": drop_reason,
            "path_hash": path_hash,
            "src_hash": src_hash,
            "dst_hash": dst_hash,
            "original_path": original_path_hashes or None,
            "forwarded_path": forwarded_path,
            "path_hash_size": path_hash_size,
            "raw_packet": packet.write_to().hex() if hasattr(packet, "write_to") else None,
            "lbt_attempts": lbt_attempts,
            "lbt_backoff_delays_ms": lbt_backoff_delays_ms,
            "lbt_channel_busy": lbt_channel_busy,
        }

    def _append_recent_packet(self, packet_record: dict) -> None:
        """Append packet to bounded recent list and keep hash index aligned."""
        if len(self.recent_packets) >= self.max_recent_packets:
            oldest = self.recent_packets.popleft()
            oldest_hash = oldest.get("packet_hash") if isinstance(oldest, dict) else None
            if oldest_hash and self._recent_hash_index.get(oldest_hash) is oldest:
                del self._recent_hash_index[oldest_hash]

        self.recent_packets.append(packet_record)
        pkt_hash = packet_record.get("packet_hash") if isinstance(packet_record, dict) else None
        if pkt_hash:
            self._recent_hash_index[pkt_hash] = packet_record

    def _get_drop_reason(self, packet: Packet, packet_hash: Optional[str] = None) -> str:

        if self.is_duplicate(packet, packet_hash=packet_hash):
            return "Duplicate"

        if not packet or not packet.payload:
            return "Empty payload"

        if len(packet.path or []) >= MAX_PATH_SIZE:
            return "Path too long"

        route_type = packet.header & PH_ROUTE_MASK

        if route_type == ROUTE_TYPE_FLOOD:
            # Check if unscoped flood policy blocked it
            unscoped_flood_allow = self.config.get("mesh", {}).get("unscoped_flood_allow", self.config.get("mesh", {}).get("global_flood_allow", True))
            if not unscoped_flood_allow:
                return "Unscoped flood policy disabled"

        if route_type == ROUTE_TYPE_DIRECT:
            hash_size = packet.get_path_hash_size()
            if not packet.path or len(packet.path) < hash_size:
                return "Direct: no path"
            next_hop = bytes(packet.path[:hash_size])
            if next_hop != self.local_hash_bytes[:hash_size]:
                return "Direct: not for us"

        # Default reason
        return "Unknown"

    def is_duplicate(self, packet: Packet, packet_hash: Optional[str] = None) -> bool:
        """Return True if this packet has already been seen.

        Accepts an optional pre-computed packet_hash to avoid a redundant SHA-256
        when the caller (e.g. __call__ → process_packet → flood/direct_forward)
        has already calculated the hash.  Falls back to computing it if not provided.

        INVARIANT: this method is synchronous with no await points.  The caller
        (process_packet / __call__) relies on is_duplicate + mark_seen being
        effectively atomic within the asyncio event loop.  Do NOT add any await
        here without revisiting that invariant.
        """
        pkt_hash = packet_hash or packet.calculate_packet_hash().hex().upper()
        return pkt_hash in self.seen_packets

    def mark_seen(self, packet: Packet, packet_hash: Optional[str] = None):

        pkt_hash = packet_hash or packet.calculate_packet_hash().hex().upper()
        self.seen_packets[pkt_hash] = time.time()

        if len(self.seen_packets) > self.max_cache_size:
            self.seen_packets.popitem(last=False)

    def validate_packet(self, packet: Packet) -> Tuple[bool, str]:

        if not packet or not packet.payload:
            return False, "Empty payload"

        if len(packet.path or []) >= MAX_PATH_SIZE:
            return (
                False,
                f"Path length {len(packet.path or [])} exceeds MAX_PATH_SIZE ({MAX_PATH_SIZE})",
            )

        return True, ""

    def _normalize_loop_detect_mode(self, mode) -> str:
        if isinstance(mode, str):
            normalized = mode.strip().lower()
            if normalized in {
                LOOP_DETECT_OFF,
                LOOP_DETECT_MINIMAL,
                LOOP_DETECT_MODERATE,
                LOOP_DETECT_STRICT,
            }:
                return normalized
        return LOOP_DETECT_OFF

    def _get_loop_detect_mode(self) -> str:
        return self.loop_detect_mode

    def _is_flood_looped(self, packet: Packet, mode: Optional[str] = None) -> bool:
        mode = mode or self._get_loop_detect_mode()
        if mode == LOOP_DETECT_OFF:
            return False

        max_counter = LOOP_DETECT_MAX_COUNTERS.get(mode)
        if max_counter is None:
            return False

        path = packet.path or bytearray()
        local_count = sum(1 for hop in path if hop == self.local_hash)
        return local_count >= max_counter

    def _check_transport_codes(self, packet: Packet) -> Tuple[bool, str]:

        if not self.storage:
            logger.warning("Transport code check failed: no storage available")
            return False, "No storage available for transport key validation"
        
        try:
            from pymc_core.protocol.transport_keys import calc_transport_code

            # Check cache validity
            current_time = time.time()
            if (
                self._transport_keys_cache is None
                or current_time - self._transport_keys_cache_time > self._transport_keys_cache_ttl
            ):
                # Refresh cache
                self._transport_keys_cache = self.storage.get_transport_keys()
                self._transport_keys_cache_time = current_time
            
            transport_keys = self._transport_keys_cache
            
            if not transport_keys:
                return False, "No transport keys configured"
            
            # Check if packet has transport codes
            if not packet.has_transport_codes():
                return False, "No transport codes present"

            transport_code_0 = packet.transport_codes[0]  # First transport code

            payload = packet.get_payload()
            payload_type = (
                packet.get_payload_type()
                if hasattr(packet, "get_payload_type")
                else ((packet.header & 0x3C) >> 2)
            )

            # Check packet against each transport key
            for key_record in transport_keys:
                transport_key_encoded = key_record.get("transport_key")
                key_name = key_record.get("name", "unknown")
                flood_policy = key_record.get("flood_policy", "deny")
                
                if not transport_key_encoded:
                    continue

                try:
                    import base64

                    transport_key = base64.b64decode(transport_key_encoded)
                    expected_code = calc_transport_code(transport_key, packet)
                    if transport_code_0 == expected_code:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug(
                                f"Transport code validated for key '{key_name}' with policy '{flood_policy}'"
                            )

                        # Update last_used timestamp for this key
                        try:
                            key_id = key_record.get("id")
                            if key_id:
                                self.storage.update_transport_key(
                                    key_id=key_id, last_used=time.time()
                                )
                                if logger.isEnabledFor(logging.DEBUG):
                                    logger.debug(
                                        f"Updated last_used timestamp for transport key '{key_name}'"
                                    )
                        except Exception as e:
                            logger.warning(
                                f"Failed to update last_used for transport key '{key_name}': {e}"
                            )

                        # Check flood policy for this key
                        if flood_policy == "allow":
                            return True, ""
                        else:
                            return False, f"Transport key '{key_name}' flood policy denied"

                except Exception as e:
                    logger.warning(f"Error checking transport key '{key_name}': {e}")
                    continue

            # No matching transport code found
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    f"Transport code 0x{transport_code_0:04X} denied (checked {len(transport_keys)} keys)"
                )
            return False, "No matching transport code"

        except Exception as e:
            logger.error(f"Transport code validation error: {e}")
            return False, f"Transport code validation error: {e}"

    def flood_forward(self, packet: Packet, packet_hash: Optional[str] = None) -> Optional[Packet]:
        """Forward a FLOOD packet, appending our hash to the path.

        INVARIANT: purely synchronous — no await points.  The is_duplicate +
        mark_seen pair is atomic within the asyncio event loop.  Do NOT add any
        await here without revisiting that invariant in __call__ / process_packet.
        """
        # Validate
        valid, reason = self.validate_packet(packet)
        if not valid:
            packet.drop_reason = reason
            return None

        # Check if packet is marked do-not-retransmit
        if packet.is_marked_do_not_retransmit():
            # Check if packet has custom drop reason
            if not packet.drop_reason:
                packet.drop_reason = "Marked do not retransmit"
            return None
        
        # Check unscoped flood policy
        unscoped_flood_allow = self.config.get("mesh", {}).get("unscoped_flood_allow", self.config.get("mesh", {}).get("global_flood_allow", True))
        route_type = packet.header & PH_ROUTE_MASK
        if route_type == ROUTE_TYPE_FLOOD:
            if not unscoped_flood_allow:
                packet.drop_reason = "Unscoped flood policy disabled"
                return None

        #Check transport scopes flood policy    
        if route_type == ROUTE_TYPE_TRANSPORT_FLOOD:             
            allowed, check_reason = self._check_transport_codes(packet)
            if not allowed:
                packet.drop_reason = "Transport code not allowed to flood"
                return None

        mode = self._get_loop_detect_mode()
        if self._is_flood_looped(packet, mode):
            packet.drop_reason = f"FLOOD loop detected ({mode})"
            return None

        # Suppress duplicates — pass pre-computed hash to avoid a second SHA-256.
        if self.is_duplicate(packet, packet_hash=packet_hash):
            packet.drop_reason = "Duplicate"
            return None

        if packet.path is None:
            packet.path = bytearray()
        elif not isinstance(packet.path, bytearray):
            packet.path = bytearray(packet.path)

        hash_size = packet.get_path_hash_size()
        hop_count = packet.get_path_hash_count()

        if self.max_flood_hops > 0 and hop_count >= self.max_flood_hops:
            packet.drop_reason = f"Max flood hops limit reached ({hop_count}/{self.max_flood_hops})"
            return None

        # path_len encodes hop count in 6 bits (0-63); adding ourselves must not exceed 63
        if hop_count >= 63:
            packet.drop_reason = "Path hop count at maximum (63), cannot append"
            return None

        # Check path won't exceed MAX_PATH_SIZE after append
        if (hop_count + 1) * hash_size > MAX_PATH_SIZE:
            packet.drop_reason = "Path would exceed MAX_PATH_SIZE"
            return None

        self.mark_seen(packet, packet_hash=packet_hash)

        # Append hash_size bytes from our public key prefix
        packet.path.extend(self.local_hash_bytes[:hash_size])
        packet.path_len = PathUtils.encode_path_len(hash_size, hop_count + 1)

        return packet

    def direct_forward(self, packet: Packet, packet_hash: Optional[str] = None) -> Optional[Packet]:
        """Forward a DIRECT packet, removing the first hop from the path.

        INVARIANT: purely synchronous — no await points.  The is_duplicate +
        mark_seen pair is atomic within the asyncio event loop.  Do NOT add any
        await here without revisiting that invariant in __call__ / process_packet.
        """
        # Validate packet (empty payload, oversized path, etc.)
        valid, reason = self.validate_packet(packet)
        if not valid:
            packet.drop_reason = reason
            return None

        # Check if packet is marked do-not-retransmit
        if packet.is_marked_do_not_retransmit():
            if not packet.drop_reason:
                packet.drop_reason = "Marked do not retransmit"
            return None

        hash_size = packet.get_path_hash_size()
        hop_count = packet.get_path_hash_count()

        # Check if we're the next hop
        if not packet.path or len(packet.path) < hash_size:
            packet.drop_reason = "Direct: no path"
            return None

        next_hop = bytes(packet.path[:hash_size])
        if next_hop != self.local_hash_bytes[:hash_size]:
            packet.drop_reason = "Direct: not for us"
            return None

        # Suppress duplicates — pass pre-computed hash to avoid a second SHA-256.
        if self.is_duplicate(packet, packet_hash=packet_hash):
            packet.drop_reason = "Duplicate"
            return None

        self.mark_seen(packet, packet_hash=packet_hash)

        # Remove first hash entry (hash_size bytes)
        packet.path = bytearray(packet.path[hash_size:])
        packet.path_len = PathUtils.encode_path_len(hash_size, hop_count - 1)

        return packet

    @staticmethod
    def calculate_packet_score(snr: float, packet_len: int, spreading_factor: int = 8) -> float:

        # SNR thresholds per SF (from MeshCore RadioLibWrappers.cpp)
        snr_thresholds = {7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}

        if spreading_factor < 7:
            return 0.0

        threshold = snr_thresholds.get(spreading_factor, -10.0)

        # Below threshold = no chance of success
        if snr < threshold:
            return 0.0

        # Success rate based on SNR above threshold
        success_rate_based_on_snr = (snr - threshold) / 10.0

        # Collision penalty: longer packets more likely to collide (max 256 bytes)
        collision_penalty = 1.0 - (packet_len / 256.0)

        # Combined score
        score = success_rate_based_on_snr * collision_penalty

        return max(0.0, min(1.0, score))

    def _calculate_tx_delay(self, packet: Packet, snr: float = 0.0) -> float:

        packet_len = packet.get_raw_length()
        airtime_ms = self.airtime_mgr.calculate_airtime(packet_len)

        route_type = packet.header & PH_ROUTE_MASK

        # Base delay calculations
        # this part took me along time to get right well i hope i got it right ;-)

        if route_type == ROUTE_TYPE_FLOOD:
            # Flood packets: random(0-5) * (airtime * 52/50 / 2) * tx_delay_factor
            # This creates collision avoidance with tunable delay
            base_delay_ms = (airtime_ms * 52 / 50) / 2.0  # From C++ implementation
            random_mult = random.uniform(0, 5)  # Random multiplier for collision avoidance
            delay_ms = base_delay_ms * random_mult * self.tx_delay_factor
            delay_s = delay_ms / 1000.0
        else:  # DIRECT
            # Direct packets: use direct_tx_delay_factor (already in seconds)
            # direct_tx_delay_factor is stored as seconds in config
            delay_s = self.direct_tx_delay_factor

        # Apply score-based delay adjustment ONLY if delay >= 50ms threshold
        # (matching C++ reactive behavior in Dispatcher::calcRxDelay)
        if delay_s >= 0.05 and self.use_score_for_tx:
            score = self.calculate_packet_score(snr, packet_len)
            # Higher score = shorter delay: max(0.2, 1.0 - score)
            # score 1.0 → multiplier 0.2 (20% of original)
            # score 0.0 → multiplier 1.0 (100% of original)
            score_multiplier = max(0.2, 1.0 - score)
            delay_s = delay_s * score_multiplier
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    f"Congestion detected (delay >= 50ms), score={score:.2f}, "
                    f"delay multiplier={score_multiplier:.2f}"
                )

        # Cap at 5 seconds maximum
        delay_s = min(delay_s, 5.0)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                f"Route={'FLOOD' if route_type == ROUTE_TYPE_FLOOD else 'DIRECT'}, "
                f"len={packet_len}B, airtime={airtime_ms:.1f}ms, delay={delay_s:.3f}s"
            )

        return delay_s

    def process_packet(
        self,
        packet: Packet,
        snr: float = 0.0,
        packet_hash: Optional[str] = None,
    ) -> Optional[Tuple[Packet, float]]:
        """Route a received packet to flood_forward or direct_forward.

        packet_hash is the pre-computed SHA-256 hex string from __call__.
        Passing it here avoids recomputing the hash in flood_forward /
        direct_forward / is_duplicate / mark_seen — reducing SHA-256 calls
        from 3 per forwarded packet to 1.
        """
        route_type = packet.header & PH_ROUTE_MASK

        if route_type == ROUTE_TYPE_FLOOD or route_type == ROUTE_TYPE_TRANSPORT_FLOOD:
            fwd_pkt = self.flood_forward(packet, packet_hash=packet_hash)
            if fwd_pkt is None:
                return None
            delay = self._calculate_tx_delay(fwd_pkt, snr)
            return fwd_pkt, delay

        elif route_type == ROUTE_TYPE_DIRECT or route_type == ROUTE_TYPE_TRANSPORT_DIRECT:
            fwd_pkt = self.direct_forward(packet, packet_hash=packet_hash)
            if fwd_pkt is None:
                return None
            delay = self._calculate_tx_delay(fwd_pkt, snr)
            return fwd_pkt, delay

        else:
            packet.drop_reason = f"Unknown route type: {route_type}"
            return None

    async def schedule_retransmit(
        self,
        fwd_pkt: Packet,
        delay: float,
        airtime_ms: float = 0.0,
        local_transmission: bool = False,
    ):
        """Schedule a packet retransmission with delay and return the task.

        If local_transmission is True and the first send fails, retry once after
        a short delay (handles transient radio/LBT failures).
        """

        async def delayed_send():
            await asyncio.sleep(delay)

            # Each attempt gets its own lock acquisition so the 1-second retry
            # backoff (local_transmission only) happens OUTSIDE the lock.
            # Holding _tx_lock across asyncio.sleep(1.0) would block every other
            # queued TX task for the full backoff period.
            #
            # Loop runs once for relayed packets, twice for local_transmission:
            #   attempt 0 — initial try (no pre-sleep)
            #   attempt 1 — retry after 1s backoff outside the lock
            for attempt in range(2 if local_transmission else 1):
                if attempt > 0:
                    # Back-off OUTSIDE the lock — other tasks can transmit here.
                    logger.info("Retrying local TX in 1s (lock released during backoff)...")
                    await asyncio.sleep(1.0)

                async with self._tx_lock:
                    # ── Authoritative duty-cycle gate ──────────────────────────
                    # The upfront can_transmit() call in __call__ is advisory: it
                    # avoids scheduling packets obviously over budget, but cannot
                    # prevent a race between tasks whose delay timers expire nearly
                    # simultaneously.  Both pass the advisory check before either
                    # records airtime, then both attempt to transmit.
                    #
                    # Inside _tx_lock only one task runs at a time.  The check and
                    # record_tx() are effectively atomic — no TOCTOU window.
                    # Re-checked every attempt because airtime state may change
                    # while we wait for the lock or sleep through backoff.
                    if airtime_ms > 0:
                        can_tx_now, _ = self.airtime_mgr.can_transmit(airtime_ms)
                        if not can_tx_now:
                            logger.warning(
                                "Packet dropped at TX time: duty-cycle exceeded "
                                "(airtime=%.1fms)", airtime_ms,
                            )
                            return

                    try:
                        await self.dispatcher.send_packet(fwd_pkt, wait_for_ack=False)
                        self._record_packet_sent(fwd_pkt)
                        if airtime_ms > 0:
                            self.airtime_mgr.record_tx(airtime_ms)
                        packet_size = fwd_pkt.get_raw_length()
                        logger.info(
                            f"Retransmitted packet ({packet_size} bytes, "
                            f"{airtime_ms:.1f}ms airtime)"
                        )
                        return
                    except Exception as e:
                        logger.error(f"Retransmit failed (attempt {attempt + 1}): {e}")
                        if local_transmission and attempt == 0:
                            pass  # release lock, outer loop sleeps, then retries
                        else:
                            raise

        return asyncio.create_task(delayed_send())

    def _record_packet_sent(self, packet: Packet) -> None:
        """Record a packet send for flood/direct stats (forwarded and originated)."""
        route = getattr(packet, "header", 0) & PH_ROUTE_MASK
        if route in (ROUTE_TYPE_FLOOD, ROUTE_TYPE_TRANSPORT_FLOOD):
            self.sent_flood_count += 1
        elif route in (ROUTE_TYPE_DIRECT, ROUTE_TYPE_TRANSPORT_DIRECT):
            self.sent_direct_count += 1

    def get_noise_floor(self) -> Optional[float]:
        try:
            radio = self.dispatcher.radio if self.dispatcher else None
            if radio and hasattr(radio, "get_noise_floor"):
                return radio.get_noise_floor()
            return None
        except Exception as e:
            logger.debug(f"Failed to get noise floor: {e}")
            return None

    def get_cached_noise_floor(self) -> Optional[float]:
        """Return the last asynchronously-sampled noise floor value."""
        return self._cached_noise_floor

    def get_stats(self) -> dict:

        uptime_seconds = time.time() - self.start_time

        # Get config sections
        repeater_config = self.config.get("repeater", {})
        duty_cycle_config = self.config.get("duty_cycle", {})
        delays_config = self.config.get("delays", {})

        max_airtime_ms = duty_cycle_config.get("max_airtime_per_minute", 3600)
        max_duty_cycle_percent = (max_airtime_ms / 60000) * 100  # 60000ms = 1 minute

        # Calculate actual hourly rates (packets in last 3600 seconds)
        now = time.time()
        packets_last_hour = [p for p in self.recent_packets if now - p["timestamp"] < 3600]
        rx_per_hour = len(packets_last_hour)
        forwarded_per_hour = sum(1 for p in packets_last_hour if p.get("transmitted", False))

        # Use cached value sampled by the background timer to avoid serial I/O on stats requests.
        noise_floor_dbm = self.get_cached_noise_floor()

        # Get CRC error count from radio hardware
        radio = self.dispatcher.radio if self.dispatcher else None
        crc_error_count = getattr(radio, "crc_error_count", 0) if radio else 0

        # Get neighbors from database
        neighbors = self.storage.get_neighbors() if self.storage else {}

        # Format local_hash respecting path_hash_mode
        phm = self.config.get("mesh", {}).get("path_hash_mode", 0)
        _bc = {0: 1, 1: 2, 2: 3}.get(phm, 1)
        _hc = _bc * 2
        _val = int.from_bytes(bytes(self.local_hash_bytes[:_bc]), "big")
        local_hash_str = f"0x{_val:0{_hc}x}"

        stats = {
            "local_hash": local_hash_str,
            "duplicate_cache_size": len(self.seen_packets),
            "cache_ttl": self.cache_ttl,
            "rx_count": self.rx_count,
            "forwarded_count": self.forwarded_count,
            "dropped_count": self.dropped_count,
            "recv_flood_count": self.recv_flood_count,
            "recv_direct_count": self.recv_direct_count,
            "sent_flood_count": self.sent_flood_count,
            "sent_direct_count": self.sent_direct_count,
            "flood_dup_count": self.flood_dup_count,
            "direct_dup_count": self.direct_dup_count,
            "rx_per_hour": rx_per_hour,
            "forwarded_per_hour": forwarded_per_hour,
            "recent_packets": list(self.recent_packets),
            "neighbors": neighbors,
            "uptime_seconds": uptime_seconds,
            "noise_floor_dbm": noise_floor_dbm,
            "crc_error_count": crc_error_count,
            # Add configuration data
            "config": {
                "node_name": repeater_config.get("node_name", "Unknown"),
                "repeater": {
                    "mode": repeater_config.get("mode", "forward"),
                    "use_score_for_tx": repeater_config.get("use_score_for_tx", False),
                    "score_threshold": repeater_config.get("score_threshold", 0.3),
                    "send_advert_interval_hours": repeater_config.get(
                        "send_advert_interval_hours", 10
                    ),
                    "latitude": repeater_config.get("latitude", 0.0),
                    "longitude": repeater_config.get("longitude", 0.0),
                    "max_flood_hops": repeater_config.get("max_flood_hops", 64),
                    "advert_interval_minutes": repeater_config.get("advert_interval_minutes", 120),
                    "advert_rate_limit": repeater_config.get("advert_rate_limit", {}),
                    "advert_penalty_box": repeater_config.get("advert_penalty_box", {}),
                    "advert_adaptive": repeater_config.get("advert_adaptive", {}),
                },
                "radio": self.config.get(
                    "radio", {}
                ),  # Read from live config, not cached radio_config
                "duty_cycle": {
                    "max_airtime_percent": max_duty_cycle_percent,
                    "enforcement_enabled": duty_cycle_config.get("enforcement_enabled", True),
                },
                "delays": {
                    "tx_delay_factor": delays_config.get("tx_delay_factor", 1.0),
                    "direct_tx_delay_factor": delays_config.get("direct_tx_delay_factor", 0.5),
                    "rx_delay_base": delays_config.get("rx_delay_base", 0.0),
                },
                "web": self.config.get("web", {}),  # Include web configuration
                "mesh": {
                    "loop_detect": self.config.get("mesh", {}).get("loop_detect", "off"),
                    "unscoped_flood_allow": self.config.get("mesh", {}).get("unscoped_flood_allow", self.config.get("mesh", {}).get("global_flood_allow", True)),
                    "path_hash_mode": self.config.get("mesh", {}).get("path_hash_mode", 0),
                },
                "mqtt_brokers": self.config.get("mqtt_brokers", {}),
            },
            "public_key": None,
        }
        # Add airtime stats
        stats.update(self.airtime_mgr.get_stats())
        return stats

    def _start_background_tasks(self):
        if self._background_task is None:
            self._background_task = asyncio.create_task(self._background_timer_loop())
            logger.info("Background timer started for noise floor and adverts")

    async def _background_timer_loop(self):
        try:
            while True:
                current_time = time.time()

                # Check noise floor recording (every 30 seconds)
                if current_time - self.last_noise_measurement >= self.noise_floor_interval:
                    await self._record_noise_floor_async()
                    await self._record_crc_errors_async()
                    self.last_noise_measurement = current_time

                # Check advert sending (every N hours)
                if self.send_advert_interval_hours > 0 and self.send_advert_func:
                    interval_seconds = self.send_advert_interval_hours * 3600
                    if current_time - self.last_advert_time >= interval_seconds:
                        await self._send_periodic_advert_async()
                        self.last_advert_time = current_time

                # Prune expired entries from duplicate detection cache (every 60s)
                if current_time - self.last_cache_cleanup >= 60.0:
                    self.cleanup_cache()
                    self.last_cache_cleanup = current_time

                # Prune old SQLite data (check every 6 hours)
                if current_time - self.last_db_cleanup >= 21600:
                    if self.storage:
                        try:
                            retention_days = (
                                self.config
                                .get("storage", {})
                                .get("retention", {})
                                .get("sqlite_cleanup_days", 31)
                            )
                            self.storage.cleanup_old_data(days=retention_days)
                            logger.info("Cleaned up SQLite data older than %d days", retention_days)
                        except Exception as e:
                            logger.warning(f"SQLite cleanup failed: {e}")
                    self.last_db_cleanup = current_time

                # Sleep for 5 seconds before next check
                await asyncio.sleep(5.0)

        except asyncio.CancelledError:
            logger.info("Background timer loop cancelled")
            raise
        except Exception as e:
            logger.error(f"Error in background timer loop: {e}")
            # Restart the timer after a delay
            await asyncio.sleep(30)
            self._background_task = asyncio.create_task(self._background_timer_loop())

    async def _record_noise_floor_async(self):
        if not self.storage:
            return

        try:
            # Run in executor so KISS modem's blocking _send_command (up to 5s timeout)
            # does not block the event loop and hang the process / delay Ctrl+C.
            loop = asyncio.get_running_loop()
            noise_floor = await loop.run_in_executor(None, self.get_noise_floor)
            if noise_floor is not None:
                self._cached_noise_floor = noise_floor
                self.storage.record_noise_floor(noise_floor)
                logger.debug(f"Recorded noise floor: {noise_floor} dBm")
            else:
                logger.debug("Unable to read noise floor from radio")
        except Exception as e:
            logger.error(f"Error recording noise floor: {e}")

    async def _record_crc_errors_async(self):
        """Persist CRC error delta from the radio hardware counter."""
        if not self.storage:
            return

        try:
            radio = self.dispatcher.radio if self.dispatcher else None
            current = getattr(radio, "crc_error_count", 0) if radio else 0
            delta = current - self._last_crc_error_count
            if delta > 0:
                self.storage.record_crc_errors(delta)
                logger.debug(f"Recorded {delta} CRC errors (total: {current})")
            self._last_crc_error_count = current
        except Exception as e:
            logger.error(f"Error recording CRC errors: {e}")

    async def _send_periodic_advert_async(self):
        logger.info(
            f"Periodic advert timer triggered (interval: {self.send_advert_interval_hours}h)"
        )
        try:
            if self.send_advert_func:
                success = await self.send_advert_func()
                if success:
                    logger.info("Periodic advert sent successfully")
                else:
                    logger.warning("Failed to send periodic advert")
            else:
                logger.debug("No send_advert_func configured")
        except Exception as e:
            logger.error(f"Error sending periodic advert: {e}")

    def reload_runtime_config(self):
        """Reload runtime configuration from self.config (called after live config updates)."""
        try:
            # Refresh delay factors
            self.tx_delay_factor = self.config.get("delays", {}).get("tx_delay_factor", 1.0)
            self.direct_tx_delay_factor = self.config.get("delays", {}).get(
                "direct_tx_delay_factor", 0.5
            )

            # Refresh repeater settings
            repeater_config = self.config.get("repeater", {})
            self.use_score_for_tx = repeater_config.get("use_score_for_tx", False)
            self.score_threshold = repeater_config.get("score_threshold", 0.3)
            self.send_advert_interval_hours = repeater_config.get("send_advert_interval_hours", 10)
            self.cache_ttl = repeater_config.get("cache_ttl", 60)
            self.max_flood_hops = repeater_config.get("max_flood_hops", 64)
            self.loop_detect_mode = self._normalize_loop_detect_mode(
                self.config.get("mesh", {}).get("loop_detect", LOOP_DETECT_OFF)
            )
            
            # Note: Radio config changes require restart as they affect hardware
            # Note: Airtime manager has its own config reference that gets updated
            
            logger.info("Runtime configuration reloaded successfully")
        except Exception as e:
            logger.error(f"Error reloading runtime config: {e}")

    def cleanup(self):
        if self._background_task and not self._background_task.done():
            self._background_task.cancel()
            logger.info("Background timer task cancelled")

        if self.storage:
            try:
                self.storage.close()
                logger.info("StorageCollector closed successfully")
            except Exception as e:
                logger.error(f"Error closing StorageCollector: {e}")

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass
