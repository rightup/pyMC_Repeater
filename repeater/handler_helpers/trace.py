"""
Trace packet handling helper for pyMC Repeater.

This module handles the processing and forwarding of trace packets,
which are used for network diagnostics to track the path and SNR
of packets through the mesh network.
"""

import asyncio
import logging
import time
from typing import Any, Dict

from pymc_core.hardware.signal_utils import snr_register_to_db
from pymc_core.node.handlers.trace import TraceHandler
from pymc_core.protocol.constants import MAX_PATH_SIZE, ROUTE_TYPE_DIRECT

logger = logging.getLogger("TraceHelper")


class TraceHelper:
    """Helper class for processing trace packets in the repeater."""

    def __init__(self, local_hash: int, repeater_handler, packet_injector=None, log_fn=None):
        """
        Initialize the trace helper.

        Args:
            local_hash: The local node's hash identifier
            repeater_handler: The RepeaterHandler instance
            packet_injector: Callable to inject new packets into the router for sending
            log_fn: Optional logging function for TraceHandler
        """
        self.local_hash = local_hash
        self.repeater_handler = repeater_handler
        self.packet_injector = packet_injector  # Function to inject packets into router

        # Ping callback system - track pending ping requests by tag
        self.pending_pings = (
            {}
        )  # {tag: {'event': asyncio.Event(), 'result': dict, 'target': int, 'sent_at': float}}

        # Optional: when trace reaches final node, call this (packet, parsed_data) to push 0x89 to companions
        self.on_trace_complete = None  # async (packet, parsed_data) -> None

        # Create TraceHandler internally as a parsing utility
        self.trace_handler = TraceHandler(log_fn=log_fn or logger.info)

    async def process_trace_packet(self, packet) -> None:
        """
        Process an incoming trace packet.

        This method handles trace packet validation, logging, recording,
        and forwarding if this node is the next hop in the trace path.

        Args:
            packet: The trace packet to process
        """
        try:
            # Only process direct route trace packets
            if packet.get_route_type() != ROUTE_TYPE_DIRECT or packet.path_len >= MAX_PATH_SIZE:
                return

            # Parse the trace payload
            parsed_data = self.trace_handler._parse_trace_payload(packet.payload)

            if not parsed_data.get("valid", False):
                logger.warning(f"Invalid trace packet: {parsed_data.get('error', 'Unknown error')}")
                return

            trace_path = parsed_data["trace_path"]
            trace_path_len = len(trace_path)

            # Check if this is a response to one of our pings
            trace_tag = parsed_data.get("tag")
            if trace_tag in self.pending_pings:
                rssi_val = getattr(packet, "rssi", 0)
                if rssi_val == 0:
                    logger.warning(
                        f"Ignoring trace response for tag {trace_tag} "
                        "with RSSI=0 (no signal data)"
                    )
                    return  # wait for a valid response or let timeout handle it
                ping_info = self.pending_pings[trace_tag]
                # Store response data
                ping_info["result"] = {
                    "path": trace_path,
                    "snr": packet.get_snr(),
                    "rssi": rssi_val,
                    "received_at": time.time(),
                }
                # Signal the waiting coroutine
                ping_info["event"].set()
                logger.info(f"Ping response received for tag {trace_tag}")

            # Record the trace packet for dashboard/statistics
            if self.repeater_handler:
                packet_record = self._create_trace_record(packet, trace_path, parsed_data)
                self.repeater_handler.log_trace_record(packet_record)

            # Extract and log path SNRs and hashes
            path_snrs, path_hashes = self._extract_path_info(packet, trace_path)

            # Add packet metadata for logging
            parsed_data["snr"] = packet.get_snr()
            parsed_data["rssi"] = getattr(packet, "rssi", 0)
            formatted_response = self.trace_handler._format_trace_response(parsed_data)

            logger.info(f"{formatted_response}")
            logger.info(f"Path SNRs: [{', '.join(path_snrs)}], Hashes: [{', '.join(path_hashes)}]")

            # Check if we should forward this trace packet
            should_forward = self._should_forward_trace(packet, trace_path, trace_path_len)

            if should_forward:
                await self._forward_trace_packet(packet, trace_path_len)
            else:
                # This is the final destination or can't forward - just log and record
                self._log_no_forward_reason(packet, trace_path, trace_path_len)
                # When trace completed (reached end of path), push PUSH_CODE_TRACE_DATA (0x89) to companions (firmware onTraceRecv)
                if packet.path_len >= trace_path_len and self.on_trace_complete:
                    try:
                        await self.on_trace_complete(packet, parsed_data)
                    except Exception as e:
                        logger.debug("on_trace_complete error: %s", e)

        except Exception as e:
            logger.error(f"Error processing trace packet: {e}")

    def _create_trace_record(self, packet, trace_path: list, parsed_data: dict) -> Dict[str, Any]:
        """
        Create a packet record for trace packets to log to statistics.

        Args:
            packet: The trace packet
            trace_path: The parsed trace path from the payload
            parsed_data: The parsed trace data

        Returns:
            A dictionary containing the packet record
        """
        # Format trace path for display
        trace_path_bytes = [f"{h:02X}" for h in trace_path[:8]]
        if len(trace_path) > 8:
            trace_path_bytes.append("...")
        path_hash = "[" + ", ".join(trace_path_bytes) + "]"

        # Extract SNR information from the path
        path_snrs = []
        path_snr_details = []
        for i in range(packet.path_len):
            if i < len(packet.path):
                snr_val = packet.path[i]
                snr_db = snr_register_to_db(snr_val)
                path_snrs.append(f"{snr_val}({snr_db:.1f}dB)")

                # Add detailed SNR info if we have the corresponding hash
                if i < len(trace_path):
                    path_snr_details.append(
                        {"hash": f"{trace_path[i]:02X}", "snr_raw": snr_val, "snr_db": snr_db}
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
                    len(packet.payload or b""),
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
            "original_path": [f"{h:02X}" for h in trace_path],
            "forwarded_path": None,
            # Add trace-specific SNR path information
            "path_snrs": path_snrs,  # ["58(14.5dB)", "19(4.8dB)"]
            "path_snr_details": path_snr_details,  # [{"hash": "29", "snr_raw": 58, "snr_db": 14.5}]
            "is_trace": True,
            "raw_packet": packet.write_to().hex() if hasattr(packet, "write_to") else None,
        }

    def _extract_path_info(self, packet, trace_path: list) -> tuple:
        """
        Extract SNR and hash information from the packet path.

        Args:
            packet: The trace packet
            trace_path: The parsed trace path from the payload

        Returns:
            A tuple of (path_snrs, path_hashes) lists
        """
        path_snrs = []
        path_hashes = []

        for i in range(packet.path_len):
            if i < len(packet.path):
                snr_val = packet.path[i]
                snr_db = snr_register_to_db(snr_val)
                path_snrs.append(f"{snr_val}({snr_db:.1f}dB)")

            if i < len(trace_path):
                path_hashes.append(f"0x{trace_path[i]:02x}")

        return path_snrs, path_hashes

    def _should_forward_trace(self, packet, trace_path: list, trace_path_len: int) -> bool:
        """
        Determine if this node should forward the trace packet.
        Uses the same logic as the original working implementation.

        Args:
            packet: The trace packet
            trace_path: The parsed trace path from the payload
            trace_path_len: The length of the trace path

        Returns:
            True if the packet should be forwarded, False otherwise
        """
        # Use the exact logic from the original working code
        return (
            packet.path_len < trace_path_len
            and len(trace_path) > packet.path_len
            and trace_path[packet.path_len] == self.local_hash
            and self.repeater_handler
            and not self.repeater_handler.is_duplicate(packet)
        )

    async def _forward_trace_packet(self, packet, trace_path_len: int) -> None:
        """
        Forward a trace packet by appending SNR and sending via injection.

        Args:
            packet: The trace packet to forward
            trace_path_len: The length of the trace path
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
            f"Forwarding trace, stored SNR {current_snr:.1f}dB at position {packet.path_len - 1}"
        )

        # Inject packet into router for proper routing and transmission
        if self.packet_injector:
            await self.packet_injector(packet, wait_for_ack=False)
        else:
            logger.warning("No packet injector available - trace packet not forwarded")

    def _log_no_forward_reason(self, packet, trace_path: list, trace_path_len: int) -> None:
        """
        Log the reason why a trace packet was not forwarded.

        Args:
            packet: The trace packet
            trace_path: The parsed trace path from the payload
            trace_path_len: The length of the trace path
        """
        if packet.path_len >= trace_path_len:
            logger.info("Trace completed (reached end of path)")
        elif len(trace_path) <= packet.path_len:
            logger.info("Path index out of bounds")
        elif trace_path[packet.path_len] != self.local_hash:
            expected_hash = (
                trace_path[packet.path_len] if packet.path_len < len(trace_path) else None
            )
            logger.info(f"Not our turn (next hop: 0x{expected_hash:02x})")
        elif self.repeater_handler and self.repeater_handler.is_duplicate(packet):
            logger.info("Duplicate packet, ignoring")

    def register_ping(self, tag: int, target_hash: int) -> asyncio.Event:
        """Register a ping request and return an event to wait on.

        Args:
            tag: The unique trace tag for this ping
            target_hash: The hash of the target node

        Returns:
            asyncio.Event that will be set when response is received
        """
        event = asyncio.Event()
        self.pending_pings[tag] = {
            "event": event,
            "result": None,
            "target": target_hash,
            "sent_at": time.time(),
        }
        logger.debug(f"Registered ping with tag {tag} for target 0x{target_hash:02x}")
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
