"""
Packet router for pyMC Repeater.

This module provides a simple router that routes packets to appropriate handlers
based on payload type. All statistics, queuing, and processing logic is handled
by the repeater engine for better separation of concerns.
"""

import asyncio
import logging

from pymc_core.node.handlers.trace import TraceHandler
from pymc_core.node.handlers.control import ControlHandler
from pymc_core.node.handlers.advert import AdvertHandler

logger = logging.getLogger("PacketRouter")


class PacketRouter:
    """
    Simple router that processes packets through handlers sequentially.
    All statistics and processing decisions are handled by the engine.
    """
    
    def __init__(self, daemon_instance):
        self.daemon = daemon_instance
        self.queue = asyncio.Queue()
        self.running = False
        self.router_task = None
        
    async def start(self):
        """Start the router processing task."""
        self.running = True
        self.router_task = asyncio.create_task(self._process_queue())
        logger.info("Packet router started")
    
    async def stop(self):
        """Stop the router processing task."""
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
        """
        Inject a new packet into the system for transmission through the engine.
        
        This method uses the engine's main packet handler with the local_transmission
        flag to bypass forwarding logic while maintaining proper statistics and airtime.
        
        Args:
            packet: The packet to send
            wait_for_ack: Whether to wait for acknowledgment
            
        Returns:
            True if packet was sent successfully, False otherwise
        """
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
        """Process packets through the router queue."""
        while self.running:
            try:
                packet = await asyncio.wait_for(self.queue.get(), timeout=0.1)
                await self._route_packet(packet)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Router error: {e}", exc_info=True)
    
    
    async def _route_packet(self, packet):
        """
        Route a packet to appropriate handlers based on payload type.
        
        Simple routing logic:
        1. Route to specific handlers for parsing
        2. Pass to repeater engine for all processing decisions
        """
        payload_type = packet.get_payload_type()
        
        # Route to specific handlers for parsing only
        if payload_type == TraceHandler.payload_type():
            # Process trace packet
            if self.daemon.trace_helper:
                await self.daemon.trace_helper.process_trace_packet(packet)

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
        
        # Always pass to repeater engine for processing decisions and statistics
        if self.daemon.repeater_handler:
            metadata = {
                "rssi": getattr(packet, "rssi", 0),
                "snr": getattr(packet, "snr", 0.0),
                "timestamp": getattr(packet, "timestamp", 0),
            }
            await self.daemon.repeater_handler(packet, metadata)
