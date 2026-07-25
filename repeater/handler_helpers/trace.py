"""
Trace packet handling helper for openHop Repeater.

This module handles the processing and forwarding of trace packets,
which are used for network diagnostics to track the path and SNR
of packets through the mesh network.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List

from openhop_core.hardware.signal_utils import snr_register_to_db
from openhop_core.node.handlers.trace import TraceHandler
from openhop_core.protocol.constants import MAX_PATH_SIZE
from openhop_core.protocol.packet_utils import PathUtils

logger = logging.getLogger("TraceHelper")


class TraceHelper:
    """Helper class for processing trace packets in the repeater."""

    def __init__(
        self,
        local_hash: int,
        repeater_handler,
        packet_injector=None,
        log_fn=None,
        local_identity=None,
    ):
        """
        Initialize the trace helper.

        Args:
            local_hash: The local node's 1-byte hash (first byte of pubkey); legacy
            repeater_handler: The RepeaterHandler instance
            packet_injector: Callable to inject new packets into the router for sending
            log_fn: Optional logging function for TraceHandler
            local_identity: LocalIdentity (or any object with get_public_key()) for
                multibyte TRACE path matching (Mesh.cpp isHashMatch with 1<<path_sz bytes)
        """
        self.local_hash = local_hash
        self.local_identity = local_identity
        self._pubkey_bytes: bytes = b""
        if local_identity is not None and hasattr(local_identity, "get_public_key"):
            try:
                self._pubkey_bytes = bytes(local_identity.get_public_key())
            except Exception:
                self._pubkey_bytes = b""
        self.repeater_handler = repeater_handler
        self.packet_injector = packet_injector  # Function to inject packets into router

        # Ping callback system - track pending ping requests by tag
        self.pending_pings = {}  # {tag: {'event': asyncio.Event(), 'result': dict, 'target': int, 'sent_at': float}}

        # Optional: when trace reaches final node, call this (packet, parsed_data) to push 0x89 to companions
        self.on_trace_complete = None  # async (packet, parsed_data) -> None

        # Create TraceHandler internally as a parsing utility
        self.trace_handler = TraceHandler(log_fn=log_fn or logger.info)

    def _pubkey_prefix(self, width: int) -> bytes:
        if width <= 0 or not self._pubkey_bytes:
            return b""
        return self._pubkey_bytes[:width]

    async def process_trace_packet(self, packet) -> None:
        """
        Process an incoming trace packet.

        This method handles trace packet validation, logging, recording,
        and forwarding if this node is the next hop in the trace path.

        Args:
            packet: The trace packet to process
        """
        try:
            # Only process direct route trace packets (SNR path uses len(packet.path))
            if not packet.is_route_direct() or len(packet.path) >= MAX_PATH_SIZE:
                return

            # Parse the trace payload
            parsed_data = self.trace_handler._parse_trace_payload(packet.payload)

            if not parsed_data.get("valid", False):
                logger.warning(f"Invalid trace packet: {parsed_data.get('error', 'Unknown error')}")
                return

            trace_bytes: bytes = parsed_data.get("trace_path_bytes") or b""
            flags = parsed_data.get("flags", 0)
            hash_width = PathUtils.trace_payload_hash_width(flags)
            trace_hops: List[bytes] = parsed_data.get("trace_hops") or []
            num_hops = len(trace_hops)
            legacy_trace_path = parsed_data.get("trace_path") or []

            # Check if this is a response to one of our pings
            trace_tag = parsed_data.get("tag")
            if trace_tag in self.pending_pings:
                rssi_val = getattr(packet, "rssi", 0)
                if rssi_val == 0:
                    logger.warning(
                        f"Ignoring trace response for tag {trace_tag} with RSSI=0 (no signal data)"
                    )
                    return  # wait for a valid response or let timeout handle it
                ping_info = self.pending_pings[trace_tag]
                # Store response data (legacy path list + structured hops)
                ping_info["result"] = {
                    "path": legacy_trace_path,
                    "trace_hops": trace_hops,
                    "trace_path_bytes": trace_bytes,
                    "snr": packet.get_snr(),
                    "rssi": rssi_val,
                    "received_at": time.time(),
                }
                # Signal the waiting coroutine
                ping_info["event"].set()
                logger.info(f"Ping response received for tag {trace_tag}")

            # Record the trace packet for dashboard/statistics
            if self.repeater_handler:
                packet_record = self._create_trace_record(packet, parsed_data)
                self.repeater_handler.log_trace_record(packet_record)

            # Extract and log path SNRs and hashes
            path_snrs, path_hashes = self._extract_path_info(packet, parsed_data)

            # Add packet metadata for logging
            parsed_data["snr"] = packet.get_snr()
            parsed_data["rssi"] = getattr(packet, "rssi", 0)
            formatted_response = self.trace_handler._format_trace_response(parsed_data)

            logger.info(f"{formatted_response}")
            logger.info(f"Path SNRs: [{', '.join(path_snrs)}], Hashes: [{', '.join(path_hashes)}]")

            should_forward = self._should_forward_trace(packet, trace_bytes, flags, hash_width)
            mode_blocked = should_forward and not self._relay_allowed()
            if mode_blocked:
                should_forward = False
                logger.info("Trace relay suppressed (repeat disabled)")

            if should_forward:
                await self._forward_trace_packet(packet, num_hops)
            else:
                if not mode_blocked:
                    self._log_no_forward_reason(packet, trace_bytes, hash_width)
                if (
                    self.on_trace_complete
                    and self._is_trace_complete(packet, trace_bytes, hash_width)
                    and self.repeater_handler
                    and not self.repeater_handler.is_duplicate(packet)
                ):
                    try:
                        await self.on_trace_complete(packet, parsed_data)
                    except Exception as e:
                        logger.debug("on_trace_complete error: %s", e)

        except Exception as e:
            logger.error(f"Error processing trace packet: {e}")

    def _is_trace_complete(self, packet, trace_bytes: bytes, hash_width: int) -> bool:
        """Mirror Mesh.cpp: offset = path_len<<path_sz >= len(trace hash bytes)."""
        if not trace_bytes or hash_width <= 0:
            return False
        snr_count = len(packet.path)
        return snr_count * hash_width >= len(trace_bytes)

    def _create_trace_record(self, packet, parsed_data: dict) -> Dict[str, Any]:
        """
        Create a packet record for trace packets to log to statistics.

        Args:
            packet: The trace packet
            parsed_data: Full parse result from TraceHandler

        Returns:
            A dictionary containing the packet record
        """
        trace_hops: List[bytes] = parsed_data.get("trace_hops") or []
        legacy = parsed_data.get("trace_path") or []

        trace_path_bytes = [h.hex().upper() for h in trace_hops[:8]]
        if len(trace_hops) > 8:
            trace_path_bytes.append("...")
        path_hash = "[" + ", ".join(trace_path_bytes) + "]"

        # Extract SNR information from the path (one SNR byte per hop along trace)
        path_snrs = []
        path_snr_details = []
        for i in range(len(packet.path)):
            snr_val = packet.path[i]
            snr_db = snr_register_to_db(snr_val)
            path_snrs.append(f"{snr_val}({snr_db:.1f}dB)")

            if i < len(trace_hops):
                path_snr_details.append(
                    {
                        "hash": trace_hops[i].hex().upper(),
                        "snr_raw": snr_val,
                        "snr_db": snr_db,
                    }
                )
            elif i < len(legacy):
                path_snr_details.append(
                    {
                        "hash": f"{legacy[i]:02X}",
                        "snr_raw": snr_val,
                        "snr_db": snr_db,
                    }
                )

        return {
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
            "type": packet.get_payload_type(),  # 0x09 for trace
            "route": packet.get_route_type(),  # Should be direct (1)
            "length": len(packet.payload or b""),
            "rssi": getattr(packet, "rssi", 0),
            "snr": getattr(packet, "snr", 0.0),
            "score": (
                self.repeater_handler.calculate_packet_score(
                    getattr(packet, "snr", 0.0),
                    packet.get_raw_length() if hasattr(packet, "get_raw_length") else 0,
                    self.repeater_handler.radio_config.get("spreading_factor", 8),
                )
                if self.repeater_handler
                else 0.0
            ),
            "tx_delay_ms": 0,
            "transmitted": False,
            "is_duplicate": False,
            "packet_hash": packet.calculate_packet_hash().hex().upper()[:16],
            "drop_reason": "trace_received",
            "path_hash": path_hash,
            "src_hash": None,
            "dst_hash": None,
            "original_path": [h.hex() for h in trace_hops],
            "forwarded_path": None,
            # Add trace-specific SNR path information
            "path_snrs": path_snrs,  # ["58(14.5dB)", "19(4.8dB)"]
            "path_snr_details": path_snr_details,
            "is_trace": True,
            "raw_packet": packet.write_to().hex() if hasattr(packet, "write_to") else None,
        }

    def _extract_path_info(self, packet, parsed_data: dict) -> tuple:
        """
        Extract SNR and hash information from the packet path.

        Returns:
            A tuple of (path_snrs, path_hashes) display lists
        """
        trace_hops: List[bytes] = parsed_data.get("trace_hops") or []
        path_snrs = []
        path_hashes = []

        for i in range(len(packet.path)):
            if i < len(packet.path):
                snr_val = packet.path[i]
                snr_db = snr_register_to_db(snr_val)
                path_snrs.append(f"{snr_val}({snr_db:.1f}dB)")

            if i < len(trace_hops):
                path_hashes.append(f"0x{trace_hops[i].hex()}")

        return path_snrs, path_hashes

    def _relay_allowed(self) -> bool:
        """Firmware gates intermediate-hop TRACE relay on ``allowPacketForward``
        (Mesh.cpp), so a repeater that is not forwarding (monitor / no_tx mode)
        must not relay traces. Locally originated pings are injected directly
        and never pass through this gate.
        """
        config = getattr(self.repeater_handler, "config", None)
        mode = "forward"
        if isinstance(config, dict):
            mode = config.get("repeater", {}).get("mode", "forward")
        if mode not in ("forward", "monitor", "no_tx"):
            mode = "forward"
        return mode == "forward"

    def _should_forward_trace(
        self, packet, trace_bytes: bytes, flags: int, hash_width: int
    ) -> bool:
        """
        Mesh.cpp TRACE branch: forward if offset < len and next hash matches identity.
        offset = pkt->path_len<<path_sz uses SNR count in packet.path (len(packet.path)).
        """
        if not trace_bytes or hash_width <= 0:
            return False
        snr_count = len(packet.path)
        byte_off = snr_count * hash_width
        if byte_off >= len(trace_bytes):
            return False

        next_hop = trace_bytes[byte_off : byte_off + hash_width]
        if len(next_hop) != hash_width:
            return False

        pubkey_pfx = self._pubkey_prefix(hash_width)
        if len(pubkey_pfx) >= hash_width:
            match = next_hop == pubkey_pfx[:hash_width]
        else:
            match = hash_width == 1 and next_hop[0] == (self.local_hash & 0xFF)

        if not match:
            return False
        if not self.repeater_handler:
            return False
        return not self.repeater_handler.is_duplicate(packet)

    async def _forward_trace_packet(self, packet, num_hops: int) -> None:
        """
        Forward a trace packet by appending SNR and sending via injection.

        Args:
            packet: The trace packet to forward
            num_hops: Total hops in trace path (for logging)
        """
        # Update the packet record to show it will be transmitted
        if self.repeater_handler and hasattr(self.repeater_handler, "recent_packets"):
            packet_hash = packet.calculate_packet_hash().hex().upper()[:16]
            for record in reversed(self.repeater_handler.recent_packets):
                if record.get("packet_hash") == packet_hash:
                    record["transmitted"] = True
                    record["drop_reason"] = "trace_forwarded"
                    break

        # Get current SNR and scale it for storage (SNR * 4)
        current_snr = packet.get_snr()
        snr_scaled = int(current_snr * 4)

        # Clamp to signed byte range [-128, 127]
        if snr_scaled > 127:
            snr_scaled = 127
        elif snr_scaled < -128:
            snr_scaled = -128

        # Convert to unsigned byte representation
        snr_byte = snr_scaled if snr_scaled >= 0 else (256 + snr_scaled)

        # Ensure path array is long enough
        while len(packet.path) <= packet.path_len:
            packet.path.append(0)

        # Store SNR at current position and increment path length
        packet.path[packet.path_len] = snr_byte
        packet.path_len += 1

        logger.info(
            f"Forwarding trace ({num_hops} hop path), stored SNR {current_snr:.1f}dB "
            f"at SNR index {packet.path_len - 1}"
        )

        # Inject packet into router for proper routing and transmission
        if self.packet_injector:
            await self.packet_injector(packet, wait_for_ack=False)
        else:
            logger.warning("No packet injector available - trace packet not forwarded")

    def _log_no_forward_reason(self, packet, trace_bytes: bytes, hash_width: int) -> None:
        """Log the reason why this node did not forward the trace."""
        if self.repeater_handler and self.repeater_handler.is_duplicate(packet):
            logger.info("Duplicate packet, ignoring")
            return

        snr_count = len(packet.path)
        if not trace_bytes or hash_width <= 0:
            logger.info("Trace: empty path or invalid hash width")
            return

        if snr_count * hash_width >= len(trace_bytes):
            logger.info("Trace completed (reached end of path)")
            return

        byte_off = snr_count * hash_width
        next_hop = trace_bytes[byte_off : byte_off + hash_width]
        pubkey_pfx = self._pubkey_prefix(hash_width)
        if len(next_hop) == hash_width and len(pubkey_pfx) >= hash_width:
            if next_hop != pubkey_pfx[:hash_width]:
                logger.info(f"Not our turn (next hop: 0x{next_hop.hex()})")
                return
        elif hash_width == 1 and next_hop:
            if (next_hop[0] & 0xFF) != (self.local_hash & 0xFF):
                logger.info(f"Not our turn (next hop: 0x{next_hop.hex()})")
                return

        logger.info("Trace: not forwarded (internal)")

    def register_ping(self, tag: int, target_hash: int) -> asyncio.Event:
        """Register a ping request and return an event to wait on.

        Args:
            tag: The unique trace tag for this ping
            target_hash: The hash of the target node

        Returns:
            asyncio.Event that will be set when response is received
        """
        key = int(tag) & 0xFFFFFFFF
        if key in self.pending_pings:
            raise ValueError(f"trace tag 0x{key:08X} is already pending")
        event = asyncio.Event()
        self.pending_pings[key] = {
            "event": event,
            "result": None,
            "target": target_hash,
            "sent_at": time.time(),
        }
        logger.debug(f"Registered ping with tag {key} for target 0x{target_hash:02x}")
        return event

    def cleanup_stale_pings(self, max_age_seconds: int = 30):
        """Remove pending pings older than max_age_seconds.

        Args:
            max_age_seconds: Maximum age in seconds before a ping is considered stale
        """
        current_time = time.time()
        stale_tags = [
            tag
            for tag, info in self.pending_pings.items()
            if current_time - info["sent_at"] > max_age_seconds
        ]
        for tag in stale_tags:
            self.pending_pings.pop(tag)
            logger.debug(f"Cleaned up stale ping with tag {tag}")
        if stale_tags:
            logger.info(f"Cleaned up {len(stale_tags)} stale ping(s)")
