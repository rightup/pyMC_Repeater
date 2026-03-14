import asyncio
import logging
import time

from pymc_core.node.handlers.ack import AckHandler
from pymc_core.node.handlers.advert import AdvertHandler
from pymc_core.node.handlers.control import ControlHandler
from pymc_core.node.handlers.group_text import GroupTextHandler
from pymc_core.node.handlers.login_response import LoginResponseHandler
from pymc_core.node.handlers.login_server import LoginServerHandler
from pymc_core.node.handlers.path import PathHandler
from pymc_core.node.handlers.protocol_request import ProtocolRequestHandler
from pymc_core.node.handlers.protocol_response import ProtocolResponseHandler
from pymc_core.node.handlers.text import TextMessageHandler
from pymc_core.node.handlers.trace import TraceHandler
from pymc_core.protocol.constants import (
    PH_ROUTE_MASK,
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_TRANSPORT_DIRECT,
)

logger = logging.getLogger("PacketRouter")

# Deliver PATH and protocol-response (PATH) to companion at most once per logical packet
# so the client is not spammed with duplicate telemetry when the mesh delivers multiple copies.
_COMPANION_DEDUPE_TTL_SEC = 60.0


def _companion_dedup_key(packet) -> str | None:
    """Return a stable key for companion delivery deduplication, or None if not available."""
    try:
        return packet.calculate_packet_hash().hex().upper()
    except Exception:
        return None


def _is_direct_final_hop(packet) -> bool:
    """True if packet is DIRECT (or TRANSPORT_DIRECT) with empty path — we're the final destination."""
    route = getattr(packet, "header", 0) & PH_ROUTE_MASK
    if route != ROUTE_TYPE_DIRECT and route != ROUTE_TYPE_TRANSPORT_DIRECT:
        return False
    path = getattr(packet, "path", None)
    return not path or len(path) == 0


class PacketRouter:

    def __init__(self, daemon_instance):
        self.daemon = daemon_instance
        self.queue = asyncio.Queue()
        self.running = False
        self.router_task = None
        # Serialize injects so one local TX completes before the next is processed
        self._inject_lock = asyncio.Lock()
        # Hash -> expiry time; skip delivering same PATH/protocol-response to companions more than once
        self._companion_delivered = {}

    async def start(self):
        self.running = True
        self.router_task = asyncio.create_task(self._process_queue())
        logger.info("Packet router started")
    
    async def stop(self):
        self.running = False
        if self.router_task:
            self.router_task.cancel()
            try:
                await self.router_task
            except asyncio.CancelledError:
                pass
        logger.info("Packet router stopped")
    
    def _should_deliver_path_to_companions(self, packet) -> bool:
        """Return True if this PATH/protocol-response should be delivered to companions (first of duplicates)."""
        key = _companion_dedup_key(packet)
        if not key:
            return True
        now = time.time()
        # Prune expired
        self._companion_delivered = {k: v for k, v in self._companion_delivered.items() if v > now}
        if key in self._companion_delivered:
            return False
        self._companion_delivered[key] = now + _COMPANION_DEDUPE_TTL_SEC
        return True

    def _record_for_ui(self, packet, metadata: dict) -> None:
        """Record an injection-only packet for the web UI (storage + recent_packets)."""
        handler = getattr(self.daemon, "repeater_handler", None)
        if handler and getattr(handler, "storage", None):
            try:
                handler.record_packet_only(packet, metadata)
            except Exception as e:
                logger.debug("Record for UI failed: %s", e)

    async def enqueue(self, packet):
        """Add packet to router queue."""
        await self.queue.put(packet)

    async def inject_packet(self, packet, wait_for_ack: bool = False):
        try:
            metadata = {
                "rssi": getattr(packet, "rssi", 0),
                "snr": getattr(packet, "snr", 0.0),
                "timestamp": getattr(packet, "timestamp", 0),
            }

            # Serialize injects so one local TX completes before the next runs
            # (avoids duty-cycle or dispatcher races where a later packet goes out first)
            async with self._inject_lock:
                # Use local_transmission=True to bypass forwarding logic
                await self.daemon.repeater_handler(
                    packet, metadata, local_transmission=True
                )

            # Mark so when this packet is dequeued we don't pass to engine again (avoid double-send / double-count)
            packet._injected_for_tx = True

            # Enqueue so router can deliver to companion(s): TXT_MSG -> dest bridge, ACK -> all bridges (sender sees ACK)
            await self.enqueue(packet)

            packet_len = len(packet.payload) if packet.payload else 0
            logger.debug(
                f"Injected packet processed by engine as local transmission ({packet_len} bytes)"
            )
            # Log protocol REQ (e.g. status/telemetry) so we can confirm target node
            ptype = getattr(packet, "get_payload_type", lambda: None)()
            if ptype == ProtocolRequestHandler.payload_type() and packet.payload and packet_len >= 1:
                logger.info(
                    "Injected protocol REQ: dest=0x%02x, payload=%d bytes",
                    packet.payload[0],
                    packet_len,
                )
            return True

        except Exception as e:
            logger.error(f"Error injecting packet through engine: {e}")
            return False
    
    async def _process_queue(self):
        while self.running:
            try:
                packet = await asyncio.wait_for(self.queue.get(), timeout=0.1)
                await self._route_packet(packet)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Router error: {e}", exc_info=True)

    async def _route_packet(self, packet):

        payload_type = packet.get_payload_type()
        processed_by_injection = False
        metadata = {
            "rssi": getattr(packet, "rssi", 0),
            "snr": getattr(packet, "snr", 0.0),
            "timestamp": getattr(packet, "timestamp", 0),
        }

        # Route to specific handlers for parsing only
        if payload_type == TraceHandler.payload_type():
            # Process trace packet
            if self.daemon.trace_helper:
                await self.daemon.trace_helper.process_trace_packet(packet)
                # Skip engine processing for trace packets - they're handled by trace helper
                processed_by_injection = True
                self._record_for_ui(packet, metadata)

        elif payload_type == ControlHandler.payload_type():
            # Process control/discovery packet
            if self.daemon.discovery_helper:
                await self.daemon.discovery_helper.control_handler(packet)
                packet.mark_do_not_retransmit()
            # Deliver to companions via daemon (frame servers push PUSH_CODE_CONTROL_DATA 0x8E)
            deliver = getattr(self.daemon, "deliver_control_data", None)
            if deliver:
                snr = getattr(packet, "_snr", None) or getattr(packet, "snr", 0.0)
                rssi = getattr(packet, "_rssi", None) or getattr(packet, "rssi", 0)
                path_len = getattr(packet, "path_len", 0) or 0
                path_bytes = (
                    bytes(getattr(packet, "path", []))
                    if getattr(packet, "path", None) is not None
                    else b""
                )[:path_len]
                payload_bytes = bytes(packet.payload) if packet.payload else b""
                await deliver(snr, rssi, path_len, path_bytes, payload_bytes)

        elif payload_type == AdvertHandler.payload_type():
            # Process advertisement packet for neighbor tracking
            if self.daemon.advert_helper:
                rssi = getattr(packet, "rssi", 0)
                snr = getattr(packet, "snr", 0.0)
                await self.daemon.advert_helper.process_advert_packet(packet, rssi, snr)
            # Also feed adverts to companion bridges (for contact/path updates)
            for bridge in getattr(self.daemon, "companion_bridges", {}).values():
                try:
                    await bridge.process_received_packet(packet)
                except Exception as e:
                    logger.debug(f"Companion bridge advert error: {e}")

        elif payload_type == LoginServerHandler.payload_type():
            # Route to companion if dest is a companion; else to login_helper (for logging into this repeater).
            # When dest is remote (not handled), pass to engine so DIRECT/FLOOD ANON_REQ can be forwarded.
            # Our own injected ANON_REQ is suppressed by the engine's duplicate (mark_seen) check.
            dest_hash = packet.payload[0] if packet.payload else None
            companion_bridges = getattr(self.daemon, "companion_bridges", {})
            if dest_hash is not None and dest_hash in companion_bridges:
                await companion_bridges[dest_hash].process_received_packet(packet)
                processed_by_injection = True
            elif self.daemon.login_helper:
                handled = await self.daemon.login_helper.process_login_packet(packet)
                if handled:
                    processed_by_injection = True
            if processed_by_injection:
                self._record_for_ui(packet, metadata)

        elif payload_type == AckHandler.payload_type():
            # ACK has no dest in payload (4-byte CRC only); deliver to all bridges so sender sees send_confirmed.
            # Do not set processed_by_injection so packet also reaches engine for DIRECT forwarding when we're a middle hop.
            companion_bridges = getattr(self.daemon, "companion_bridges", {})
            for bridge in companion_bridges.values():
                try:
                    await bridge.process_received_packet(packet)
                except Exception as e:
                    logger.debug(f"Companion bridge ACK error: {e}")

        elif payload_type == TextMessageHandler.payload_type():
            dest_hash = packet.payload[0] if packet.payload else None
            companion_bridges = getattr(self.daemon, "companion_bridges", {})
            if dest_hash is not None and dest_hash in companion_bridges:
                await companion_bridges[dest_hash].process_received_packet(packet)
                processed_by_injection = True
                self._record_for_ui(packet, metadata)
            elif self.daemon.text_helper:
                handled = await self.daemon.text_helper.process_text_packet(packet)
                if handled:
                    processed_by_injection = True
                    self._record_for_ui(packet, metadata)

        elif payload_type == PathHandler.payload_type():
            dest_hash = packet.payload[0] if packet.payload else None
            companion_bridges = getattr(self.daemon, "companion_bridges", {})
            if dest_hash is not None and dest_hash in companion_bridges:
                if self._should_deliver_path_to_companions(packet):
                    await companion_bridges[dest_hash].process_received_packet(packet)
                # Do not set processed_by_injection so packet also reaches engine for DIRECT forwarding when we're a middle hop.
            elif companion_bridges and self._should_deliver_path_to_companions(packet):
                # Dest not in bridges: path-return with ephemeral dest (e.g. multi-hop login).
                # Deliver to all bridges; each will try to decrypt and ignore if not relevant.
                for bridge in companion_bridges.values():
                    try:
                        await bridge.process_received_packet(packet)
                    except Exception as e:
                        logger.debug(f"Companion bridge PATH error: {e}")
                logger.debug(
                    "PATH dest=0x%02x (anon) delivered to %d bridge(s) for matching",
                    dest_hash or 0,
                    len(companion_bridges),
                )
                # Do not set processed_by_injection so packet also reaches engine for DIRECT forwarding when we're a middle hop.
            elif self.daemon.path_helper:
                await self.daemon.path_helper.process_path_packet(packet)

        elif payload_type == LoginResponseHandler.payload_type():
            # PAYLOAD_TYPE_RESPONSE (0x01): payload is dest_hash(1)+src_hash(1)+encrypted.
            # Deliver to the bridge that is the destination, or to all bridges when the
            # response is addressed to this repeater (path-based reply: firmware sends
            # to first hop instead of original requester).
            # Do not set processed_by_injection so packet also reaches engine for DIRECT forwarding when we're a middle hop.
            dest_hash = packet.payload[0] if packet.payload and len(packet.payload) >= 1 else None
            companion_bridges = getattr(self.daemon, "companion_bridges", {})
            local_hash = getattr(self.daemon, "local_hash", None)
            if dest_hash is not None and dest_hash in companion_bridges:
                try:
                    await companion_bridges[dest_hash].process_received_packet(packet)
                    logger.info(
                        "RESPONSE dest=0x%02x delivered to companion bridge",
                        dest_hash,
                    )
                except Exception as e:
                    logger.debug(f"Companion bridge RESPONSE error: {e}")
            elif dest_hash == local_hash and companion_bridges:
                # Response addressed to this repeater (e.g. path-based reply to first hop)
                for bridge in companion_bridges.values():
                    try:
                        await bridge.process_received_packet(packet)
                    except Exception as e:
                        logger.debug(f"Companion bridge RESPONSE error: {e}")
                logger.info(
                    "RESPONSE dest=0x%02x (local) delivered to %d companion bridge(s)",
                    dest_hash,
                    len(companion_bridges),
                )
            elif companion_bridges:
                # Dest not in bridges and not local: likely ANON_REQ response (dest = ephemeral
                # sender hash). Deliver to all bridges; each will try to decrypt and ignore if
                # not relevant (firmware-like behavior, works with multiple companion bridges).
                for bridge in companion_bridges.values():
                    try:
                        await bridge.process_received_packet(packet)
                    except Exception as e:
                        logger.debug(f"Companion bridge RESPONSE error: {e}")
                logger.debug(
                    "RESPONSE dest=0x%02x (anon) delivered to %d bridge(s) for matching",
                    dest_hash or 0,
                    len(companion_bridges),
                )
            if companion_bridges and _is_direct_final_hop(packet):
                # DIRECT with empty path: we're the final hop; don't pass to engine (it would drop with "Direct: no path")
                processed_by_injection = True
                self._record_for_ui(packet, metadata)

        elif payload_type == ProtocolResponseHandler.payload_type():
            # PAYLOAD_TYPE_PATH (0x08): protocol responses (telemetry, binary, etc.).
            # Deliver at most once per logical packet so the client is not spammed with duplicates.
            # Do not set processed_by_injection so packet also reaches engine for DIRECT forwarding when we're a middle hop.
            companion_bridges = getattr(self.daemon, "companion_bridges", {})
            if companion_bridges and self._should_deliver_path_to_companions(packet):
                for bridge in companion_bridges.values():
                    try:
                        await bridge.process_received_packet(packet)
                    except Exception as e:
                        logger.debug(f"Companion bridge RESPONSE error: {e}")
            if companion_bridges and _is_direct_final_hop(packet):
                # DIRECT with empty path: we're the final hop; ensure delivery to all bridges (anon)
                if not self._should_deliver_path_to_companions(packet):
                    for bridge in companion_bridges.values():
                        try:
                            await bridge.process_received_packet(packet)
                        except Exception as e:
                            logger.debug(f"Companion bridge RESPONSE (final hop) error: {e}")
                processed_by_injection = True
                self._record_for_ui(packet, metadata)

        elif payload_type == ProtocolRequestHandler.payload_type():
            dest_hash = packet.payload[0] if packet.payload else None
            companion_bridges = getattr(self.daemon, "companion_bridges", {})
            if dest_hash is not None and dest_hash in companion_bridges:
                await companion_bridges[dest_hash].process_received_packet(packet)
                processed_by_injection = True
                self._record_for_ui(packet, metadata)
            elif self.daemon.protocol_request_helper:
                handled = await self.daemon.protocol_request_helper.process_request_packet(packet)
                if handled:
                    processed_by_injection = True
                    self._record_for_ui(packet, metadata)
            elif companion_bridges and _is_direct_final_hop(packet):
                # DIRECT with empty path: we're the final hop; deliver to all bridges for anon matching
                for bridge in companion_bridges.values():
                    try:
                        await bridge.process_received_packet(packet)
                    except Exception as e:
                        logger.debug(f"Companion bridge REQ (final hop) error: {e}")
                processed_by_injection = True
                self._record_for_ui(packet, metadata)

        elif payload_type == GroupTextHandler.payload_type():
            # GRP_TXT: pass to all companions (they filter by channel); still forward
            companion_bridges = getattr(self.daemon, "companion_bridges", {})
            for bridge in companion_bridges.values():
                try:
                    await bridge.process_received_packet(packet)
                except Exception as e:
                    logger.debug(f"Companion bridge GRP_TXT error: {e}")

        # Only pass to repeater engine if not already processed by injection
        # Skip engine for packets we injected for TX (already sent; avoid double-send/double-count)
        if getattr(packet, "_injected_for_tx", False):
            processed_by_injection = True
        if self.daemon.repeater_handler and not processed_by_injection:
            metadata = {
                "rssi": getattr(packet, "rssi", 0),
                "snr": getattr(packet, "snr", 0.0),
                "timestamp": getattr(packet, "timestamp", 0),
            }
            await self.daemon.repeater_handler(packet, metadata)
