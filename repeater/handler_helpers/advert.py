"""
Advertisement packet handling helper for pyMC Repeater.

This module processes advertisement packets for neighbor tracking and discovery.
"""

import logging
import time

from pymc_core.node.handlers.advert import AdvertHandler

logger = logging.getLogger("AdvertHelper")


class AdvertHelper:
    """Helper class for processing advertisement packets in the repeater."""

    def __init__(self, local_identity, storage, log_fn=None):
        """
        Initialize the advert helper.

        Args:
            local_identity: The LocalIdentity instance for this repeater
            storage: StorageCollector instance for persisting advert data
            log_fn: Optional logging function for AdvertHandler
        """
        self.local_identity = local_identity
        self.storage = storage
        
        # Create AdvertHandler internally as a parsing utility
        self.advert_handler = AdvertHandler(log_fn=log_fn or logger.info)
        
        # Cache for tracking known neighbors (avoid repeated database queries)
        self._known_neighbors = set()

    async def process_advert_packet(self, packet, rssi: int, snr: float) -> None:
        """
        Process an incoming advertisement packet.

        This method uses AdvertHandler to parse the packet, then stores
        the neighbor information for tracking and discovery.

        Args:
            packet: The advertisement packet to process
            rssi: Received signal strength indicator
            snr: Signal-to-noise ratio
        """
        try:
            # Set signal metrics on packet for handler to use
            packet._snr = snr
            packet._rssi = rssi
            
            # Use AdvertHandler to parse the packet - it now returns parsed data
            advert_data = await self.advert_handler(packet)
            
            if not advert_data or not advert_data.get("valid"):
                logger.warning(f"Invalid advert packet from {packet.from_id}")
                packet.mark_do_not_retransmit()
                packet.drop_reason = "Invalid advert packet"
                return
            
            # Extract data from parsed advert
            pubkey = advert_data["public_key"]
            node_name = advert_data["name"]
            contact_type = advert_data["contact_type"]
            
            # Skip our own adverts
            if self.local_identity:
                local_pubkey = self.local_identity.get_public_key().hex()
                if pubkey == local_pubkey:
                    logger.debug("Ignoring own advert in neighbor tracking")
                    return
            
            # Get route type from packet header
            from pymc_core.protocol.constants import PH_ROUTE_MASK
            route_type = packet.header & PH_ROUTE_MASK
            
            # Check if this is a new neighbor
            current_time = time.time()
            if pubkey not in self._known_neighbors:
                # Only check database if not in cache
                current_neighbors = self.storage.get_neighbors() if self.storage else {}
                is_new_neighbor = pubkey not in current_neighbors
                
                if is_new_neighbor:
                    self._known_neighbors.add(pubkey)
                    logger.info(f"Discovered new neighbor: {node_name} ({pubkey[:16]}...)")
            else:
                is_new_neighbor = False
            
            # Build advert record
            advert_record = {
                "timestamp": current_time,
                "pubkey": pubkey,
                "node_name": node_name,
                "is_repeater": "REPEATER" in contact_type.upper(),
                "route_type": route_type,
                "contact_type": contact_type,
                "latitude": advert_data["latitude"],
                "longitude": advert_data["longitude"],
                "rssi": rssi,
                "snr": snr,
                "is_new_neighbor": is_new_neighbor,
            }
            
            # Store to database
            if self.storage:
                try:
                    self.storage.record_advert(advert_record)
                except Exception as e:
                    logger.error(f"Failed to store advert record: {e}")
        
        except Exception as e:
            logger.error(f"Error processing advert packet: {e}", exc_info=True)
