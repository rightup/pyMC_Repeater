"""
Protocol request (REQ) handling helper for pyMC Repeater.

Provides repeater-specific callbacks for status and telemetry requests.
"""

import asyncio
import logging
import struct
import time

from pymc_core.node.handlers.protocol_request import (
    REQ_TYPE_GET_ACCESS_LIST,
    REQ_TYPE_GET_NEIGHBOURS,
    REQ_TYPE_GET_OWNER_INFO,
    REQ_TYPE_GET_STATUS,
    REQ_TYPE_GET_TELEMETRY_DATA,
    SERVER_RESPONSE_DELAY_MS,
    ProtocolRequestHandler,
)

logger = logging.getLogger("ProtocolRequestHelper")


class ProtocolRequestHelper:
    """Provides repeater-specific protocol request handlers."""

    def __init__(
        self,
        identity_manager,
        packet_injector=None,
        acl_dict=None,
        radio=None,
        engine=None,
        neighbor_tracker=None,
    ):

        self.identity_manager = identity_manager
        self.packet_injector = packet_injector
        self.acl_dict = acl_dict or {}
        self.radio = radio
        self.engine = engine
        self.neighbor_tracker = neighbor_tracker
        
        # Dictionary of core handlers keyed by dest_hash
        self.handlers = {}
        
    def register_identity(self, name: str, identity, identity_type: str = "repeater"):

        hash_byte = identity.get_public_key()[0]
        
        # Get ACL for this identity
        identity_acl = self.acl_dict.get(hash_byte)
        if not identity_acl:
            logger.warning(f"Cannot register identity '{name}': no ACL for hash 0x{hash_byte:02X}")
            return
        
        # Create ACL contacts wrapper
        acl_contacts = self._create_acl_contacts_wrapper(identity_acl)
        
        # Build request handlers dict
        request_handlers = {
            REQ_TYPE_GET_STATUS: self._handle_get_status,
        }
        
        # Create core handler
        handler = ProtocolRequestHandler(
            local_identity=identity,
            contacts=acl_contacts,
            get_client_fn=lambda src_hash: self._get_client_from_acl(identity_acl, src_hash),
            request_handlers=request_handlers,
            log_fn=logger.info,
        )
        
        self.handlers[hash_byte] = {
            "handler": handler,
            "identity": identity,
            "name": name,
            "type": identity_type,
        }
        
        logger.info(f"Registered protocol request handler for '{name}': hash=0x{hash_byte:02X}")

    def _create_acl_contacts_wrapper(self, acl):
        """Create contacts wrapper from ACL."""

        class ACLContactsWrapper:
            def __init__(self, identity_acl):
                self._acl = identity_acl
            
            @property
            def contacts(self):
                return self._acl.get_all_clients()
        
        return ACLContactsWrapper(acl)
    
    def _get_client_from_acl(self, acl, src_hash: int):
        """Get client from ACL by source hash."""
        for client_info in acl.get_all_clients():
            if client_info.id.get_public_key()[0] == src_hash:
                return client_info
        return None
    
    async def process_request_packet(self, packet):

        try:
            if len(packet.payload) < 2:
                return False
            
            dest_hash = packet.payload[0]
            
            handler_info = self.handlers.get(dest_hash)
            if not handler_info:
                return False
            
            # Let core handler build response
            response_packet = await handler_info["handler"](packet)
            
            # Send response after delay
            if response_packet and self.packet_injector:
                await asyncio.sleep(SERVER_RESPONSE_DELAY_MS / 1000.0)
                await self.packet_injector(response_packet, wait_for_ack=False)
            
            packet.mark_do_not_retransmit()
            return True
            
        except Exception as e:
            logger.error(f"Error processing protocol request: {e}", exc_info=True)
            return False
    
    def _handle_get_status(self, client, timestamp: int, req_data: bytes):
        """Build 56-byte RepeaterStats (firmware layout from MeshCore simple_repeater/MyMesh.h)."""
        # RepeaterStats: uint16 batt, uint16 curr_tx_queue_len, int16 noise_floor, int16 last_rssi,
        # uint32 n_packets_recv, n_packets_sent, total_air_time_secs, total_up_time_secs,
        # n_sent_flood, n_sent_direct, n_recv_flood, n_recv_direct,
        # uint16 err_events, int16 last_snr (×4), uint16 n_direct_dups, n_flood_dups,
        # uint32 total_rx_air_time_secs, n_recv_errors  → 56 bytes

        # Uptime: use engine start_time when available (fixes wrong "20521 days" from time.time())
        if self.engine and hasattr(self.engine, "start_time"):
            total_up_time_secs = int(time.time() - self.engine.start_time)
        else:
            total_up_time_secs = 0

        # Radio: noise floor, last RSSI, last SNR (firmware stores SNR × 4)
        if self.radio:
            noise_floor = int(getattr(self.radio, "get_noise_floor", lambda: 0)() or 0)
            if callable(getattr(self.radio, "get_last_rssi", None)):
                last_rssi = int(self.radio.get_last_rssi() or -120)
            else:
                last_rssi = int(getattr(self.radio, "last_rssi", -120) or -120)
            if callable(getattr(self.radio, "get_last_snr", None)):
                last_snr = int((self.radio.get_last_snr() or 0) * 4)
            else:
                last_snr = int((getattr(self.radio, "last_snr", 0) or 0) * 4)
        else:
            noise_floor = 0
            last_rssi = -120
            last_snr = 0

        # Packet counts: prefer engine (rx_count, forwarded_count); fall back to radio if present
        if self.engine:
            n_packets_recv = getattr(self.engine, "rx_count", 0)
            n_packets_sent = getattr(self.engine, "forwarded_count", 0)
        elif self.radio:
            n_packets_recv = getattr(self.radio, "packets_received", 0) or 0
            n_packets_sent = getattr(self.radio, "packets_sent", 0) or 0
        else:
            n_packets_recv = 0
            n_packets_sent = 0

        # Airtime (AirtimeManager uses total_airtime_ms for TX; total_rx_airtime_ms if we track RX)
        total_air_time_secs = 0
        total_rx_air_time_secs = 0
        if self.engine:
            am = getattr(self.engine, "airtime_mgr", None) or getattr(
                self.engine, "airtime_manager", None
            )
            if am is not None:
                total_air_time_secs = int(getattr(am, "total_airtime_ms", 0) or 0) // 1000
                total_rx_air_time_secs = int(getattr(am, "total_rx_airtime_ms", 0) or 0) // 1000

        # Routing stats (flood/direct and dups - from engine when available)
        n_sent_flood = getattr(self.engine, "sent_flood_count", 0) if self.engine else 0
        n_sent_direct = getattr(self.engine, "sent_direct_count", 0) if self.engine else 0
        n_recv_flood = getattr(self.engine, "recv_flood_count", 0) if self.engine else 0
        n_recv_direct = getattr(self.engine, "recv_direct_count", 0) if self.engine else 0
        n_direct_dups = getattr(self.engine, "direct_dup_count", 0) if self.engine else 0
        n_flood_dups = getattr(self.engine, "flood_dup_count", 0) if self.engine else 0
        n_recv_errors = (
            int(getattr(self.radio, "crc_error_count", 0) or 0)
            if self.radio
            else 0
        )

        # Pack 56-byte RepeaterStats (layout matches firmware)
        stats = struct.pack(
            "<HHhhIIIIIIIIHhHHII",
            0,  # batt_milli_volts (not available on Pi)
            0,  # curr_tx_queue_len (TODO)
            noise_floor,
            last_rssi,
            n_packets_recv,
            n_packets_sent,
            total_air_time_secs,
            total_up_time_secs,
            n_sent_flood,
            n_sent_direct,
            n_recv_flood,
            n_recv_direct,
            0,  # err_events
            last_snr,
            n_direct_dups,
            n_flood_dups,
            total_rx_air_time_secs,
            n_recv_errors,
        )

        logger.debug(
            "GET_STATUS: uptime=%ds, noise=%ddBm, rssi=%ddBm, snr=%.1fdB, rx=%s, tx=%s",
            total_up_time_secs,
            noise_floor,
            last_rssi,
            last_snr / 4.0,
            n_packets_recv,
            n_packets_sent,
        )

        return stats
