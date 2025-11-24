import asyncio
import logging
import struct
import time
from collections import OrderedDict
from typing import Optional, Tuple

from pymc_core.node.handlers.base import BaseHandler
from pymc_core.protocol import Packet
from pymc_core.protocol.constants import (
    MAX_PATH_SIZE,
    PAYLOAD_TYPE_ADVERT,
    PH_ROUTE_MASK,
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_TRANSPORT_FLOOD,
    ROUTE_TYPE_TRANSPORT_DIRECT,

)
from pymc_core.protocol.packet_utils import PacketHeaderUtils, PacketTimingUtils

from repeater.airtime import AirtimeManager
from repeater.data_acquisition import StorageCollector

logger = logging.getLogger("RepeaterHandler")

NOISE_FLOOR_INTERVAL = 30.0  # seconds


class RepeaterHandler(BaseHandler):

    @staticmethod
    def payload_type() -> int:

        return 0xFF  # Special marker (not a real payload type)

    def __init__(self, config: dict, dispatcher, local_hash: int, send_advert_func=None):

        self.config = config
        self.dispatcher = dispatcher
        self.local_hash = local_hash
        self.send_advert_func = send_advert_func
        self.airtime_mgr = AirtimeManager(config)
        self.seen_packets = OrderedDict()
        self.cache_ttl = config.get("repeater", {}).get("cache_ttl", 60)
        self.max_cache_size = 1000
        self.tx_delay_factor = config.get("delays", {}).get("tx_delay_factor", 1.0)
        self.direct_tx_delay_factor = config.get("delays", {}).get("direct_tx_delay_factor", 0.5)
        self.use_score_for_tx = config.get("repeater", {}).get("use_score_for_tx", False)
        self.score_threshold = config.get("repeater", {}).get("score_threshold", 0.3)
        self.send_advert_interval_hours = config.get("repeater", {}).get(
            "send_advert_interval_hours", 10
        )
        self.last_advert_time = time.time()

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
        self.recent_packets = []
        self.max_recent_packets = 50
        self.start_time = time.time()

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
        self.noise_floor_interval = NOISE_FLOOR_INTERVAL  # 30 seconds
        self._background_task = None
        
        # Cache transport keys for efficient lookup
        self._transport_keys_cache = None
        self._transport_keys_cache_time = 0
        self._transport_keys_cache_ttl = 60  # Cache for 60 seconds
        
        # Track last drop reason for better logging
        self._last_drop_reason = None
        
        self._start_background_tasks()

    async def __call__(self, packet: Packet, metadata: Optional[dict] = None) -> None:

        if metadata is None:
            metadata = {}

        self.rx_count += 1

        # Reset drop reason for this packet processing
        self._last_drop_reason = None

        # Check if we're in monitor mode (receive only, no forwarding)
        mode = self.config.get("repeater", {}).get("mode", "forward")
        monitor_mode = mode == "monitor"

        logger.debug(
            f"RX packet: header=0x{packet.header:02x}, payload_len={len(packet.payload or b'')}, "
            f"path_len={len(packet.path) if packet.path else 0}, "
            f"rssi={metadata.get('rssi', 'N/A')}, snr={metadata.get('snr', 'N/A')}, mode={mode}"
        )

        snr = metadata.get("snr", 0.0)
        rssi = metadata.get("rssi", 0)
        transmitted = False
        tx_delay_ms = 0.0
        drop_reason = None

        original_path = list(packet.path) if packet.path else []

        # Process for forwarding (skip if in monitor mode)
        result = None if monitor_mode else self.process_packet(packet, snr)
        forwarded_path = None
        if result:
            fwd_pkt, delay = result
            tx_delay_ms = delay * 1000.0

            # Capture the forwarded path (after modification)
            forwarded_path = list(fwd_pkt.path) if fwd_pkt.path else []

            # Check duty-cycle before scheduling TX
            packet_bytes = (
                fwd_pkt.write_to() if hasattr(fwd_pkt, "write_to") else fwd_pkt.payload or b""
            )
            airtime_ms = PacketTimingUtils.estimate_airtime_ms(len(packet_bytes), self.radio_config)

            can_tx, wait_time = self.airtime_mgr.can_transmit(airtime_ms)

            if not can_tx:
                logger.warning(
                    f"Duty-cycle limit exceeded. Airtime={airtime_ms:.1f}ms, "
                    f"wait={wait_time:.1f}s before retry"
                )
                self.dropped_count += 1
                drop_reason = "Duty cycle limit"
            else:
                self.forwarded_count += 1
                transmitted = True
                # Schedule retransmit with delay
                await self.schedule_retransmit(fwd_pkt, delay, airtime_ms)
        else:
            self.dropped_count += 1
            # Determine drop reason from process_packet result
            if monitor_mode:
                drop_reason = "Monitor mode"
            else:
                drop_reason = self._last_drop_reason or self._get_drop_reason(packet)
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
            logger.debug(
                f"Packet header=0x{packet.header:02x}, type={payload_type}, route={route_type}"
            )

        # Check if this is a duplicate
        pkt_hash = packet.calculate_packet_hash().hex().upper()
        is_dupe = pkt_hash in self.seen_packets and not transmitted

        # Set drop reason for duplicates
        if is_dupe and drop_reason is None:
            drop_reason = "Duplicate"

        # Process adverts for neighbor tracking
        if payload_type == PAYLOAD_TYPE_ADVERT:
            self._process_advert(packet, rssi, snr)

        path_hash = None
        display_path = (
            original_path if original_path else (list(packet.path) if packet.path else [])
        )
        if display_path and len(display_path) > 0:
            # Format path as array of uppercase hex bytes
            path_bytes = [f"{b:02X}" for b in display_path[:8]]  # First 8 bytes max
            if len(display_path) > 8:
                path_bytes.append("...")
            path_hash = "[" + ", ".join(path_bytes) + "]"

        src_hash = None
        dst_hash = None

        # Payload types with dest_hash and src_hash as first 2 bytes
        if payload_type in [0x00, 0x01, 0x02, 0x08]:
            if hasattr(packet, "payload") and packet.payload and len(packet.payload) >= 2:
                dst_hash = f"{packet.payload[0]:02X}"
                src_hash = f"{packet.payload[1]:02X}"

        # ADVERT packets have source identifier as first byte
        elif payload_type == PAYLOAD_TYPE_ADVERT:
            if hasattr(packet, "payload") and packet.payload and len(packet.payload) >= 1:
                src_hash = f"{packet.payload[0]:02X}"

        # Record packet for charts
        packet_record = {
            "timestamp": time.time(),
            "header": (
                f"0x{packet.header:02X}"
                if hasattr(packet, "header") and packet.header is not None
                else None
            ),
            "payload": (
                packet.payload.hex() if hasattr(packet, "payload") and packet.payload else None
            ),
            "payload_length": (
                len(packet.payload) if hasattr(packet, "payload") and packet.payload else 0
            ),
            "type": payload_type,
            "route": route_type,
            "length": len(packet.payload or b""),
            "rssi": rssi,
            "snr": snr,
            "score": self.calculate_packet_score(
                snr, len(packet.payload or b""), self.radio_config["spreading_factor"]
            ),
            "tx_delay_ms": tx_delay_ms,
            "transmitted": transmitted,
            "is_duplicate": is_dupe,
            "packet_hash": pkt_hash[:16],
            "drop_reason": drop_reason,
            "path_hash": path_hash,
            "src_hash": src_hash,
            "dst_hash": dst_hash,
            "original_path": ([f"{b:02X}" for b in original_path] if original_path else None),
            "forwarded_path": (
                [f"{b:02X}" for b in forwarded_path] if forwarded_path is not None else None
            ),
            "raw_packet": packet.write_to().hex() if hasattr(packet, "write_to") else None,
        }

        # Store packet record to persistent storage
        if self.storage:
            try:
                self.storage.record_packet(packet_record)
            except Exception as e:
                logger.error(f"Failed to store packet record: {e}")

        # If this is a duplicate, try to attach it to the original packet
        if is_dupe and len(self.recent_packets) > 0:
            # Find the original packet with same hash
            for idx in range(len(self.recent_packets) - 1, -1, -1):
                prev_pkt = self.recent_packets[idx]
                if prev_pkt.get("packet_hash") == packet_record["packet_hash"]:
                    # Add duplicate to original packet's duplicate list
                    if "duplicates" not in prev_pkt:
                        prev_pkt["duplicates"] = []
                    prev_pkt["duplicates"].append(packet_record)
                    # Don't add duplicate to main list, just track in original
                    break
            else:
                # Original not found, add as regular packet
                self.recent_packets.append(packet_record)
        else:
            # Not a duplicate or first occurrence
            self.recent_packets.append(packet_record)

        if len(self.recent_packets) > self.max_recent_packets:
            self.recent_packets.pop(0)

    def log_trace_record(self, packet_record: dict) -> None:
        """Manually log a packet trace record (used by external callers)"""
        self.recent_packets.append(packet_record)

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

        if len(self.recent_packets) > self.max_recent_packets:
            self.recent_packets.pop(0)

    def cleanup_cache(self):

        now = time.time()
        expired = [k for k, ts in self.seen_packets.items() if now - ts > self.cache_ttl]
        for k in expired:
            del self.seen_packets[k]

    def _get_drop_reason(self, packet: Packet) -> str:

        if self.is_duplicate(packet):
            return "Duplicate"

        if not packet or not packet.payload:
            return "Empty payload"

        if len(packet.path or []) >= MAX_PATH_SIZE:
            return "Path too long"

        route_type = packet.header & PH_ROUTE_MASK

        if route_type == ROUTE_TYPE_FLOOD:
            # Check if global flood policy blocked it
            global_flood_allow = self.config.get("mesh", {}).get("global_flood_allow", True)
            if not global_flood_allow:
                return "Global flood policy disabled"

        if route_type == ROUTE_TYPE_DIRECT:
            if not packet.path or len(packet.path) == 0:
                return "Direct: no path"
            next_hop = packet.path[0]
            if next_hop != self.local_hash:
                return "Direct: not for us"

        # Default reason
        return "Unknown"

    def _process_advert(self, packet: Packet, rssi: int, snr: float):

        try:
            from pymc_core.protocol.constants import ADVERT_FLAG_IS_REPEATER
            from pymc_core.protocol.utils import (
                decode_appdata,
                get_contact_type_name,
                parse_advert_payload,
                determine_contact_type_from_flags,
            )

            # Parse advert payload
            if not packet.payload or len(packet.payload) < 40:
                return

            advert_data = parse_advert_payload(packet.payload)
            pubkey = advert_data.get("pubkey", "")

            # Skip our own adverts
            if self.dispatcher and hasattr(self.dispatcher, "local_identity"):
                local_pubkey = self.dispatcher.local_identity.get_public_key().hex()
                if pubkey == local_pubkey:
                    logger.debug("Ignoring own advert in neighbor tracking")
                    return

            appdata = advert_data.get("appdata", b"")
            if not appdata:
                return

            appdata_decoded = decode_appdata(appdata)
            flags = appdata_decoded.get("flags", 0)
            is_repeater = bool(flags & ADVERT_FLAG_IS_REPEATER)
            route_type = packet.header & PH_ROUTE_MASK
            contact_type_id = determine_contact_type_from_flags(flags)
            contact_type = get_contact_type_name(contact_type_id)

            # Extract neighbor info
            node_name = appdata_decoded.get("node_name", "Unknown")
            latitude = appdata_decoded.get("latitude")
            longitude = appdata_decoded.get("longitude")

            current_time = time.time()

            # Check if this is a new neighbor
            current_neighbors = self.storage.get_neighbors() if self.storage else {}
            is_new_neighbor = pubkey not in current_neighbors

            # Create advert record for storage
            advert_record = {
                "timestamp": current_time,
                "pubkey": pubkey,
                "node_name": node_name,
                "is_repeater": is_repeater,
                "route_type": route_type,
                "contact_type": contact_type,
                "latitude": latitude,
                "longitude": longitude,
                "rssi": rssi,
                "snr": snr,
                "is_new_neighbor": is_new_neighbor,
            }

            # Store to database
            if self.storage:
                try:
                    self.storage.record_advert(advert_record)
                    if is_new_neighbor:
                        logger.info(f"Discovered new neighbor: {node_name} ({pubkey[:16]}...)")
                except Exception as e:
                    logger.error(f"Failed to store advert record: {e}")

        except Exception as e:
            logger.debug(f"Error processing advert for neighbor tracking: {e}")

    def is_duplicate(self, packet: Packet) -> bool:

        pkt_hash = packet.calculate_packet_hash().hex().upper()
        if pkt_hash in self.seen_packets:
            return True
        return False

    def mark_seen(self, packet: Packet):

        pkt_hash = packet.calculate_packet_hash().hex().upper()
        self.seen_packets[pkt_hash] = time.time()

        if len(self.seen_packets) > self.max_cache_size:
            self.seen_packets.popitem(last=False)

    def validate_packet(self, packet: Packet) -> Tuple[bool, str]:

        if not packet or not packet.payload:
            return False, "Empty payload"

        if len(packet.path or []) >= MAX_PATH_SIZE:
            return False, "Path at max size"

        return True, ""

    def _check_transport_codes(self, packet: Packet) -> Tuple[bool, str]:

        if not self.storage:
            logger.warning("Transport code check failed: no storage available")
            return False, "No storage available for transport key validation"
        
        try:
            from pymc_core.protocol.transport_keys import calc_transport_code
            
            # Check cache validity
            current_time = time.time()
            if (self._transport_keys_cache is None or 
                current_time - self._transport_keys_cache_time > self._transport_keys_cache_ttl):
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
            payload_type = packet.get_payload_type() if hasattr(packet, 'get_payload_type') else ((packet.header & 0x3C) >> 2)
            
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
                        logger.debug(f"Transport code validated for key '{key_name}' with policy '{flood_policy}'")
                        
                        # Update last_used timestamp for this key
                        try:
                            key_id = key_record.get("id")
                            if key_id:
                                self.storage.update_transport_key(
                                    key_id=key_id,
                                    last_used=time.time()
                                )
                                logger.debug(f"Updated last_used timestamp for transport key '{key_name}'")
                        except Exception as e:
                            logger.warning(f"Failed to update last_used for transport key '{key_name}': {e}")
                        
                        # Check flood policy for this key
                        if flood_policy == "allow":
                            return True, ""
                        else:
                            return False, f"Transport key '{key_name}' flood policy denied"

                    
                except Exception as e:
                    logger.warning(f"Error checking transport key '{key_name}': {e}")
                    continue
            
            # No matching transport code found
            logger.debug(f"Transport code 0x{transport_code_0:04X} denied (checked {len(transport_keys)} keys)")
            return False, "No matching transport code"
            
        except Exception as e:
            logger.error(f"Transport code validation error: {e}")
            return False, f"Transport code validation error: {e}"

    def flood_forward(self, packet: Packet) -> Optional[Packet]:

        # Validate
        valid, reason = self.validate_packet(packet)
        if not valid:
            self._last_drop_reason = reason
            return None

        # Check global flood policy
        global_flood_allow = self.config.get("mesh", {}).get("global_flood_allow", True)
        if not global_flood_allow:
            route_type = packet.header & PH_ROUTE_MASK
            if route_type == ROUTE_TYPE_FLOOD or route_type == ROUTE_TYPE_TRANSPORT_FLOOD:
             
                allowed, check_reason = self._check_transport_codes(packet)
                if not allowed:
                    self._last_drop_reason = check_reason
                    return None
            else:
                self._last_drop_reason = "Global flood policy disabled"
                return None

        # Suppress duplicates
        if self.is_duplicate(packet):
            self._last_drop_reason = "Duplicate"
            return None

        if packet.path is None:
            packet.path = bytearray()
        elif not isinstance(packet.path, bytearray):
            packet.path = bytearray(packet.path)

        packet.path.append(self.local_hash)
        packet.path_len = len(packet.path)

        self.mark_seen(packet)

        return packet

    def direct_forward(self, packet: Packet) -> Optional[Packet]:

        # Check if we're the next hop
        if not packet.path or len(packet.path) == 0:
            self._last_drop_reason = "Direct: no path"
            return None

        next_hop = packet.path[0]
        if next_hop != self.local_hash:
            self._last_drop_reason = "Direct: not for us"
            return None

        original_path = list(packet.path)
        packet.path = bytearray(packet.path[1:])
        packet.path_len = len(packet.path)

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

        import random

        packet_len = len(packet.payload) if packet.payload else 0
        airtime_ms = PacketTimingUtils.estimate_airtime_ms(packet_len, self.radio_config)

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
            logger.debug(
                f"Congestion detected (delay >= 50ms), score={score:.2f}, "
                f"delay multiplier={score_multiplier:.2f}"
            )

        # Cap at 5 seconds maximum
        delay_s = min(delay_s, 5.0)

        logger.debug(
            f"Route={'FLOOD' if route_type == ROUTE_TYPE_FLOOD else 'DIRECT'}, "
            f"len={packet_len}B, airtime={airtime_ms:.1f}ms, delay={delay_s:.3f}s"
        )

        return delay_s

    def process_packet(self, packet: Packet, snr: float = 0.0) -> Optional[Tuple[Packet, float]]:

        route_type = packet.header & PH_ROUTE_MASK

        if route_type == ROUTE_TYPE_FLOOD or route_type == ROUTE_TYPE_TRANSPORT_FLOOD:
            fwd_pkt = self.flood_forward(packet)
            if fwd_pkt is None:
                return None
            delay = self._calculate_tx_delay(fwd_pkt, snr)
            return fwd_pkt, delay

        elif route_type == ROUTE_TYPE_DIRECT or route_type == ROUTE_TYPE_TRANSPORT_DIRECT:
            fwd_pkt = self.direct_forward(packet)
            if fwd_pkt is None:
                return None
            delay = self._calculate_tx_delay(fwd_pkt, snr)
            return fwd_pkt, delay

        else:
            self._last_drop_reason = f"Unknown route type: {route_type}"
            return None

    async def schedule_retransmit(self, fwd_pkt: Packet, delay: float, airtime_ms: float = 0.0):

        async def delayed_send():
            await asyncio.sleep(delay)
            try:
                await self.dispatcher.send_packet(fwd_pkt, wait_for_ack=False)
                # Record airtime after successful TX
                if airtime_ms > 0:
                    self.airtime_mgr.record_tx(airtime_ms)
                packet_size = len(fwd_pkt.payload)
                logger.info(
                    f"Retransmitted packet ({packet_size} bytes, {airtime_ms:.1f}ms airtime)"
                )
            except Exception as e:
                logger.error(f"Retransmit failed: {e}")

        asyncio.create_task(delayed_send())

    def get_noise_floor(self) -> Optional[float]:
        try:
            radio = self.dispatcher.radio if self.dispatcher else None
            if radio and hasattr(radio, "get_noise_floor"):
                return radio.get_noise_floor()
            return None
        except Exception as e:
            logger.debug(f"Failed to get noise floor: {e}")
            return None

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

        # Get current noise floor from radio
        noise_floor_dbm = self.get_noise_floor()

        # Get neighbors from database
        neighbors = self.storage.get_neighbors() if self.storage else {}

        stats = {
            "local_hash": f"0x{self.local_hash:02x}",
            "duplicate_cache_size": len(self.seen_packets),
            "cache_ttl": self.cache_ttl,
            "rx_count": self.rx_count,
            "forwarded_count": self.forwarded_count,
            "dropped_count": self.dropped_count,
            "rx_per_hour": rx_per_hour,
            "forwarded_per_hour": forwarded_per_hour,
            "recent_packets": self.recent_packets,
            "neighbors": neighbors,
            "uptime_seconds": uptime_seconds,
            "noise_floor_dbm": noise_floor_dbm,
            # Add configuration data
            "config": {
                "node_name": repeater_config.get("node_name", "Unknown"),
                "repeater": {
                    "mode": repeater_config.get("mode", "forward"),
                    "use_score_for_tx": self.use_score_for_tx,
                    "score_threshold": self.score_threshold,
                    "send_advert_interval_hours": self.send_advert_interval_hours,
                    "latitude": repeater_config.get("latitude", 0.0),
                    "longitude": repeater_config.get("longitude", 0.0),
                },
                "radio": {
                    "frequency": self.radio_config.get("frequency", 0),
                    "tx_power": self.radio_config.get("tx_power", 0),
                    "bandwidth": self.radio_config.get("bandwidth", 0),
                    "spreading_factor": self.radio_config.get("spreading_factor", 0),
                    "coding_rate": self.radio_config.get("coding_rate", 0),
                    "preamble_length": self.radio_config.get("preamble_length", 0),
                },
                "duty_cycle": {
                    "max_airtime_percent": max_duty_cycle_percent,
                    "enforcement_enabled": duty_cycle_config.get("enforcement_enabled", True),
                },
                "delays": {
                    "tx_delay_factor": delays_config.get("tx_delay_factor", 1.0),
                    "direct_tx_delay_factor": delays_config.get("direct_tx_delay_factor", 0.5),
                },
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
                    self.last_noise_measurement = current_time

                # Check advert sending (every N hours)
                if self.send_advert_interval_hours > 0 and self.send_advert_func:
                    interval_seconds = self.send_advert_interval_hours * 3600
                    if current_time - self.last_advert_time >= interval_seconds:
                        await self._send_periodic_advert_async()
                        self.last_advert_time = current_time

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
            noise_floor = self.get_noise_floor()
            if noise_floor is not None:
                self.storage.record_noise_floor(noise_floor)
                logger.debug(f"Recorded noise floor: {noise_floor} dBm")
            else:
                logger.debug("Unable to read noise floor from radio")
        except Exception as e:
            logger.error(f"Error recording noise floor: {e}")

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
