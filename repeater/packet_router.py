import asyncio
import logging

from pymc_core.node.handlers.trace import TraceHandler
from pymc_core.node.handlers.control import ControlHandler
from pymc_core.node.handlers.advert import AdvertHandler
from pymc_core.node.handlers.login_server import LoginServerHandler
from pymc_core.node.handlers.text import TextMessageHandler
from pymc_core.node.handlers.path import PathHandler
from pymc_core.node.handlers.protocol_request import ProtocolRequestHandler

logger = logging.getLogger("PacketRouter")

class PacketRouter:

    def __init__(self, daemon_instance):
        self.daemon = daemon_instance
        self.queue = asyncio.Queue()
        self.running = False
        self.router_task = None
        
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
            
            # Use local_transmission=True to bypass forwarding logic
            await self.daemon.repeater_handler(packet, metadata, local_transmission=True)
            
            packet_len = len(packet.payload) if packet.payload else 0
            logger.debug(f"Injected packet processed by engine as local transmission ({packet_len} bytes)")
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
        
        # Route to specific handlers for parsing only
        if payload_type == TraceHandler.payload_type():
            # Process trace packet
            if self.daemon.trace_helper:
                await self.daemon.trace_helper.process_trace_packet(packet)
                # Skip engine processing for trace packets - they're handled by trace helper
                processed_by_injection = True

        elif payload_type == ControlHandler.payload_type():
            # Process control/discovery packet
            if self.daemon.discovery_helper:
                await self.daemon.discovery_helper.control_handler(packet)
                packet.mark_do_not_retransmit()
        
        elif payload_type == AdvertHandler.payload_type():
            # Process advertisement packet for neighbor tracking
            if self.daemon.advert_helper:
                rssi = getattr(packet, "rssi", 0)
                snr = getattr(packet, "snr", 0.0)
                await self.daemon.advert_helper.process_advert_packet(packet, rssi, snr)
        
        elif payload_type == LoginServerHandler.payload_type():
            # Process ANON_REQ login packet for all identities
            if self.daemon.login_helper:
                handled = await self.daemon.login_helper.process_login_packet(packet)
                # Only skip forwarding if we actually handled it
                if handled:
                    processed_by_injection = True
        
        elif payload_type == TextMessageHandler.payload_type():
            # Process TXT_MSG packet for all identities
            if self.daemon.text_helper:
                handled = await self.daemon.text_helper.process_text_packet(packet)
                # Only skip forwarding if we actually handled it
                if handled:
                    processed_by_injection = True
        
        elif payload_type == PathHandler.payload_type():
            # Process PATH packet to update client out_path for direct routing
            if self.daemon.path_helper:
                await self.daemon.path_helper.process_path_packet(packet)
                # Note: process_path_packet returns False to allow forwarding
        
        elif payload_type == ProtocolRequestHandler.payload_type():
            # Process protocol request packet (status, telemetry, neighbors, etc.)
            if self.daemon.protocol_request_helper:
                handled = await self.daemon.protocol_request_helper.process_request_packet(packet)
                if handled:
                    processed_by_injection = True
        
        # Only pass to repeater engine if not already processed by injection
        if self.daemon.repeater_handler and not processed_by_injection:
            metadata = {
                "rssi": getattr(packet, "rssi", 0),
                "snr": getattr(packet, "snr", 0.0),
                "timestamp": getattr(packet, "timestamp", 0),
            }
            await self.daemon.repeater_handler(packet, metadata)
