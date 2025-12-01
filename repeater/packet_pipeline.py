"""
Packet processing pipeline for pyMC Repeater.

This module provides a queue-based pipeline that processes packets through handlers
sequentially, tracks statistics, and ensures all packets flow through repeater logic.
"""

import asyncio
import logging
import time
from collections import deque

from pymc_core.node.handlers.trace import TraceHandler
from pymc_core.node.handlers.control import ControlHandler
from pymc_core.node.handlers.advert import AdvertHandler
from pymc_core.protocol.utils import get_packet_type_name

logger = logging.getLogger("PacketPipeline")


class PacketPipeline:
    """
    Pipeline that processes packets through handlers sequentially.
    Tracks queue statistics and ensures all packets flow through repeater logic.
    """
    
    def __init__(self, daemon_instance):
        self.daemon = daemon_instance
        self.queue = asyncio.Queue()
        self.running = False
        self.pipeline_task = None
        
        # Statistics tracking
        self.stats = {
            "total_enqueued": 0,
            "total_processed": 0,
            "total_errors": 0,
            "current_queue_size": 0,
            "max_queue_size": 0,
            "processing_times": deque(maxlen=100),  # Last 100 processing times
            "packets_by_type": {},
            "packets_marked_no_retransmit": 0,
            "packets_forwarded": 0,
        }
        self.last_stats_log = time.time()
        
    async def start(self):
        """Start the pipeline processing task."""
        self.running = True
        self.pipeline_task = asyncio.create_task(self._process_pipeline())
        logger.info("Packet pipeline started")
    
    async def stop(self):
        """Stop the pipeline processing task."""
        self.running = False
        if self.pipeline_task:
            self.pipeline_task.cancel()
            try:
                await self.pipeline_task
            except asyncio.CancelledError:
                pass
        logger.info("Packet pipeline stopped")
        self._log_final_stats()
    
    async def enqueue(self, packet):
        """Add packet to pipeline queue and track statistics."""
        await self.queue.put(packet)
        self.stats["total_enqueued"] += 1
        self.stats["current_queue_size"] = self.queue.qsize()
        
        # Track max queue size
        if self.stats["current_queue_size"] > self.stats["max_queue_size"]:
            self.stats["max_queue_size"] = self.stats["current_queue_size"]
        
        # Log stats periodically (every 30 seconds)
        now = time.time()
        if now - self.last_stats_log > 30:
            self._log_stats()
            self.last_stats_log = now
    
    async def _process_pipeline(self):
        """Process packets through the pipeline."""
        while self.running:
            try:
                packet = await asyncio.wait_for(self.queue.get(), timeout=0.1)
                
                start_time = time.time()
                await self._process_packet(packet)
                processing_time = (time.time() - start_time) * 1000  # ms
                
                self.stats["total_processed"] += 1
                self.stats["current_queue_size"] = self.queue.qsize()
                self.stats["processing_times"].append(processing_time)
                
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                self.stats["total_errors"] += 1
                logger.error(f"Pipeline error: {e}", exc_info=True)
    
    async def _process_packet(self, packet):
        """
        Process a single packet through the handler pipeline.
        
        Flow:
        1. Route to specific handler based on payload type
        2. Handler processes and may mark do_not_retransmit
        3. If not marked, pass to repeater for forwarding
        """
        payload_type = packet.get_payload_type()
        
        # Track packet type
        type_name = get_packet_type_name(payload_type)
        self.stats["packets_by_type"][type_name] = self.stats["packets_by_type"].get(type_name, 0) + 1
        
        # Stage 1: Route to specific handlers
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
                # Extract metadata for advert processing
                rssi = getattr(packet, "rssi", 0)
                snr = getattr(packet, "snr", 0.0)
                await self.daemon.advert_helper.process_advert_packet(packet, rssi, snr)
        
        if self.daemon.repeater_handler:
            metadata = {
                "rssi": getattr(packet, "rssi", 0),
                "snr": getattr(packet, "snr", 0.0),
                "timestamp": getattr(packet, "timestamp", 0),
            }
            
            # Call process_packet to get validation result and delay
            snr = metadata.get("snr", 0.0)
            result = self.daemon.repeater_handler.process_packet(packet, snr)
            
            if result:
                fwd_pkt, delay = result
                
                # Calculate airtime for duty cycle tracking
                from pymc_core.protocol.packet_utils import PacketTimingUtils
                packet_bytes = fwd_pkt.write_to() if hasattr(fwd_pkt, "write_to") else fwd_pkt.payload or b""
                airtime_ms = PacketTimingUtils.estimate_airtime_ms(
                    len(packet_bytes),
                    self.daemon.repeater_handler.radio_config
                )
                
                # Check duty cycle
                can_tx, wait_time = self.daemon.repeater_handler.airtime_mgr.can_transmit(airtime_ms)
                
                if can_tx:
                    # Schedule transmission with calculated delay
                    await self.daemon.repeater_handler.schedule_retransmit(fwd_pkt, delay, airtime_ms)
                    self.stats["packets_forwarded"] += 1
                    logger.debug(f"Packet scheduled for forwarding with {delay:.3f}s delay")
                else:
                    logger.warning(
                        f"Duty cycle limit exceeded. Airtime={airtime_ms:.1f}ms, "
                        f"wait={wait_time:.1f}s before retry"
                    )
            else:
                logger.debug(f"Packet rejected by repeater handler: {self.daemon.repeater_handler._last_drop_reason}")

    
    def _log_stats(self):
        """Log pipeline statistics."""
        avg_processing_time = 0
        if self.stats["processing_times"]:
            avg_processing_time = sum(self.stats["processing_times"]) / len(self.stats["processing_times"])
        
        logger.info(
            f"[Pipeline Stats] Enqueued: {self.stats['total_enqueued']}, "
            f"Processed: {self.stats['total_processed']}, "
            f"Errors: {self.stats['total_errors']}, "
            f"Queue: {self.stats['current_queue_size']}/{self.stats['max_queue_size']} (current/max), "
            f"Avg Time: {avg_processing_time:.2f}ms, "
            f"Forwarded: {self.stats['packets_forwarded']}, "
            f"Marked NoRetx: {self.stats['packets_marked_no_retransmit']}"
        )
        
        # Log packet type breakdown
        if self.stats["packets_by_type"]:
            type_breakdown = ", ".join([f"{k}: {v}" for k, v in sorted(self.stats["packets_by_type"].items())])
            logger.debug(f"[Pipeline Types] {type_breakdown}")
    
    def _log_final_stats(self):
        """Log final statistics on shutdown."""
        logger.info("=== Final Pipeline Statistics ===")
        self._log_stats()
        logger.info("================================")
    
    def get_stats(self):
        """Return current pipeline statistics."""
        stats_copy = self.stats.copy()
        if self.stats["processing_times"]:
            stats_copy["avg_processing_time_ms"] = sum(self.stats["processing_times"]) / len(self.stats["processing_times"])
        else:
            stats_copy["avg_processing_time_ms"] = 0
        # Don't include the deque in the return value
        del stats_copy["processing_times"]
        return stats_copy
