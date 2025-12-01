"""
Trace packet handling helper for pyMC Repeater.

This module handles the processing and forwarding of trace packets,
which are used for network diagnostics to track the path and SNR
of packets through the mesh network.
"""

import logging
import time
from typing import Optional, Dict, Any

from pymc_core.node.handlers.trace import TraceHandler
from pymc_core.protocol.constants import MAX_PATH_SIZE, ROUTE_TYPE_DIRECT

logger = logging.getLogger("TraceHelper")


class TraceHelper:
    """Helper class for processing trace packets in the repeater."""

    def __init__(self, local_hash: int, repeater_handler, dispatcher, log_fn=None):
        """
        Initialize the trace helper.

        Args:
            local_hash: The local node's hash identifier
            repeater_handler: The RepeaterHandler instance
            dispatcher: The Dispatcher instance for sending packets
            log_fn: Optional logging function for TraceHandler
        """
        self.local_hash = local_hash
        self.repeater_handler = repeater_handler
        self.dispatcher = dispatcher
        
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
                logger.warning(
                    f"Invalid trace packet: {parsed_data.get('error', 'Unknown error')}"
                )
                return

            trace_path = parsed_data["trace_path"]
            trace_path_len = len(trace_path)

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
                self._log_no_forward_reason(packet, trace_path, trace_path_len)

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
                # Convert unsigned byte to signed SNR
                snr_signed = snr_val if snr_val < 128 else snr_val - 256
                snr_db = snr_signed / 4.0
                path_snrs.append(f"{snr_val}({snr_db:.1f}dB)")

                # Add detailed SNR info if we have the corresponding hash
                if i < len(trace_path):
                    path_snr_details.append({
                        "hash": f"{trace_path[i]:02X}",
                        "snr_raw": snr_val,
                        "snr_db": snr_db
                    })

        return {
            "timestamp": time.time(),
            "header": f"0x{packet.header:02X}" if hasattr(packet, "header") and packet.header is not None else None,
            "payload": packet.payload.hex() if hasattr(packet, "payload") and packet.payload else None,
            "payload_length": len(packet.payload) if hasattr(packet, "payload") and packet.payload else 0,
            "type": packet.get_payload_type(),  # 0x09 for trace
            "route": packet.get_route_type(),   # Should be direct (1)
            "length": len(packet.payload or b""),
            "rssi": getattr(packet, "rssi", 0),
            "snr": getattr(packet, "snr", 0.0),
            "score": self.repeater_handler.calculate_packet_score(
                getattr(packet, "snr", 0.0),
                len(packet.payload or b""),
                self.repeater_handler.radio_config.get("spreading_factor", 8)
            ) if self.repeater_handler else 0.0,
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
                snr_signed = snr_val if snr_val < 128 else snr_val - 256
                snr_db = snr_signed / 4.0
                path_snrs.append(f"{snr_val}({snr_db:.1f}dB)")

            if i < len(trace_path):
                path_hashes.append(f"0x{trace_path[i]:02x}")

        return path_snrs, path_hashes

    def _should_forward_trace(self, packet, trace_path: list, trace_path_len: int) -> bool:
        """
        Determine if this node should forward the trace packet.

        Args:
            packet: The trace packet
            trace_path: The parsed trace path from the payload
            trace_path_len: The length of the trace path

        Returns:
            True if the packet should be forwarded, False otherwise
        """
        # Check if we've reached the end of the trace path
        if packet.path_len >= trace_path_len:
            return False

        # Check if path index is valid
        if len(trace_path) <= packet.path_len:
            return False

        # Check if this node is the next hop
        if trace_path[packet.path_len] != self.local_hash:
            return False

        # Check for duplicates
        if self.repeater_handler and self.repeater_handler.is_duplicate(packet):
            return False

        return True

    async def _forward_trace_packet(self, packet, trace_path_len: int) -> None:
        """
        Forward a trace packet by appending SNR and setting up correct routing.

        Args:
            packet: The trace packet to forward
            trace_path_len: The length of the trace path
        """
        # Update the packet record to show it was transmitted
        if self.repeater_handler and hasattr(self.repeater_handler, 'recent_packets'):
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

        # For direct trace packets, we need to update the routing path to point to next hop
        # Parse the trace payload to get the trace route
        parsed_data = self.trace_handler._parse_trace_payload(packet.payload)
        if parsed_data.get("valid", False):
            trace_path = parsed_data["trace_path"]
            
            # Check if there's a next hop after current position
            if packet.path_len < len(trace_path):
                next_hop = trace_path[packet.path_len]
                
                # Set up direct routing to next hop by putting it at front of path
                # The SNR data stays in the path, but we prepend the next hop for routing
                packet.path = bytearray([next_hop] + list(packet.path))
                packet.path_len = len(packet.path)
                
                logger.debug(f"Set next trace hop to 0x{next_hop:02X}")
            else:
                logger.info("Trace reached end of route")

        # Don't mark as seen - let the packet flow to repeater handler for normal processing
        # The repeater handler will handle duplicate detection and forwarding logic

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
            expected_hash = trace_path[packet.path_len] if packet.path_len < len(trace_path) else None
            logger.info(f"Not our turn (next hop: 0x{expected_hash:02x})")
        elif self.repeater_handler and self.repeater_handler.is_duplicate(packet):
            logger.info("Duplicate packet, ignoring")
