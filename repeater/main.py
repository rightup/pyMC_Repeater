import asyncio
import logging
import os
import sys

from repeater.config import get_radio_for_board, load_config
from repeater.engine import RepeaterHandler
from repeater.web.http_server import HTTPStatsServer, _log_buffer
from repeater.handler_helpers import TraceHelper, DiscoveryHelper, AdvertHelper
from repeater.packet_router import PacketRouter

logger = logging.getLogger("RepeaterDaemon")


class RepeaterDaemon:

    def __init__(self, config: dict, radio=None):

        self.config = config
        self.radio = radio
        self.dispatcher = None
        self.repeater_handler = None
        self.local_hash = None
        self.local_identity = None
        self.http_server = None
        self.trace_helper = None
        self.advert_helper = None
        self.discovery_helper = None
        self.router = None


        log_level = config.get("logging", {}).get("level", "INFO")
        logging.basicConfig(
            level=getattr(logging, log_level),
            format=config.get("logging", {}).get("format"),
        )

        root_logger = logging.getLogger()
        _log_buffer.setLevel(getattr(logging, log_level))
        root_logger.addHandler(_log_buffer)

    async def initialize(self):

        logger.info(f"Initializing repeater: {self.config['repeater']['node_name']}")

        if self.radio is None:
            logger.info("Initializing radio hardware...")
            try:
                self.radio = get_radio_for_board(self.config)
                
                if hasattr(self.radio, 'set_custom_cad_thresholds'):
                    # Load CAD settings from config, with defaults
                    cad_config = self.config.get("radio", {}).get("cad", {})
                    peak_threshold = cad_config.get("peak_threshold", 23)
                    min_threshold = cad_config.get("min_threshold", 11)
                    
                    self.radio.set_custom_cad_thresholds(peak=peak_threshold, min_val=min_threshold)
                    logger.info(f"CAD thresholds set from config: peak={peak_threshold}, min={min_threshold}")
                else:
                    logger.warning("Radio does not support CAD configuration")
                

                if hasattr(self.radio, 'get_frequency'):
                    logger.info(f"Radio config - Freq: {self.radio.get_frequency():.1f}MHz")
                if hasattr(self.radio, 'get_spreading_factor'):
                    logger.info(f"Radio config - SF: {self.radio.get_spreading_factor()}")
                if hasattr(self.radio, 'get_bandwidth'):
                    logger.info(f"Radio config - BW: {self.radio.get_bandwidth()}kHz")
                if hasattr(self.radio, 'get_coding_rate'):
                    logger.info(f"Radio config - CR: {self.radio.get_coding_rate()}")
                if hasattr(self.radio, 'get_tx_power'):
                    logger.info(f"Radio config - TX Power: {self.radio.get_tx_power()}dBm")
                
                logger.info("Radio hardware initialized")
            except Exception as e:
                logger.error(f"Failed to initialize radio hardware: {e}")
                raise RuntimeError("Repeater requires real LoRa hardware") from e


        try:
            from pymc_core import LocalIdentity
            from pymc_core.node.dispatcher import Dispatcher

            self.dispatcher = Dispatcher(self.radio)
            logger.info("Dispatcher initialized")

            identity_key = self.config.get("mesh", {}).get("identity_key")
            if not identity_key:
                logger.error("No identity key found in configuration. Cannot init repeater.")
                raise RuntimeError("Identity key is required for repeater operation")

            local_identity = LocalIdentity(seed=identity_key)
            self.local_identity = local_identity
            self.dispatcher.local_identity = local_identity

            pubkey = local_identity.get_public_key()
            self.local_hash = pubkey[0]
            logger.info(f"Local identity set: {local_identity.get_address_bytes().hex()}")
            local_hash_hex = f"0x{self.local_hash: 02x}"
            logger.info(f"Local node hash (from identity): {local_hash_hex}")


            self.dispatcher._is_own_packet = lambda pkt: False

            self.repeater_handler = RepeaterHandler(
                self.config, self.dispatcher, self.local_hash, send_advert_func=self.send_advert
            )

            # Create router
            self.router = PacketRouter(self)
            await self.router.start()
            
            # Register router as entry point for ALL packets via fallback handler
            # All received packets flow through router → helpers → repeater engine
            self.dispatcher.register_fallback_handler(self._router_callback)
            logger.info("Packet router registered as fallback (catches all packets)")

            # Create processing helpers (handlers created internally)
            self.trace_helper = TraceHelper(
                local_hash=self.local_hash,
                repeater_handler=self.repeater_handler,
                dispatcher=self.dispatcher,
                log_fn=logger.info,
            )
            logger.info("Trace processing helper initialized")
            
            # Create advert helper for neighbor tracking
            self.advert_helper = AdvertHelper(
                local_identity=self.local_identity,
                storage=self.repeater_handler.storage if self.repeater_handler else None,
                log_fn=logger.info,
            )
            logger.info("Advert processing helper initialized")

            # Set up discovery handler if enabled
            allow_discovery = self.config.get("repeater", {}).get("allow_discovery", True)
            if allow_discovery:
                self.discovery_helper = DiscoveryHelper(
                    local_identity=self.local_identity,
                    dispatcher=self.dispatcher,
                    node_type=2,
                    log_fn=logger.info,
                )
                logger.info("Discovery processing helper initialized")
            else:
                logger.info("Discovery response handler disabled")

        except Exception as e:
            logger.error(f"Failed to initialize dispatcher: {e}")
            raise

    async def _router_callback(self, packet):
        """
        Single entry point for ALL packets.
        Enqueues packets for router processing.
        """
        if self.router:
            await self.router.enqueue(packet)

    def get_stats(self) -> dict:
        stats = {}
        
        if self.repeater_handler:
            stats = self.repeater_handler.get_stats()
            # Add public key if available
            if self.local_identity:
                try:
                    pubkey = self.local_identity.get_public_key()
                    stats["public_key"] = pubkey.hex()
                except Exception:
                    stats["public_key"] = None
        
        return stats

    async def send_advert(self) -> bool:

        if not self.dispatcher or not self.local_identity:
            logger.error("Cannot send advert: dispatcher or identity not initialized")
            return False

        try:
            from pymc_core.protocol import PacketBuilder
            from pymc_core.protocol.constants import ADVERT_FLAG_HAS_NAME, ADVERT_FLAG_IS_REPEATER

            # Get node name and location from config
            repeater_config = self.config.get("repeater", {})
            node_name = repeater_config.get("node_name", "Repeater")
            latitude = repeater_config.get("latitude", 0.0)
            longitude = repeater_config.get("longitude", 0.0)

            flags = ADVERT_FLAG_IS_REPEATER | ADVERT_FLAG_HAS_NAME

            packet = PacketBuilder.create_advert(
                local_identity=self.local_identity,
                name=node_name,
                lat=latitude,
                lon=longitude,
                feature1=0,
                feature2=0,
                flags=flags,
                route_type="flood",
            )

            # Send via dispatcher
            await self.dispatcher.send_packet(packet, wait_for_ack=False)

            # Mark our own advert as seen to prevent re-forwarding it
            if self.repeater_handler:
                self.repeater_handler.mark_seen(packet)
                logger.debug("Marked own advert as seen in duplicate cache")

            logger.info(f"Sent flood advert '{node_name}' at ({latitude: .6f}, {longitude: .6f})")
            return True

        except Exception as e:
            logger.error(f"Failed to send advert: {e}", exc_info=True)
            return False

    async def run(self):

        logger.info("Repeater daemon started")

        await self.initialize()

        # Start HTTP stats server
        http_port = self.config.get("http", {}).get("port", 8000)
        http_host = self.config.get("http", {}).get("host", "0.0.0.0")

        node_name = self.config.get("repeater", {}).get("node_name", "Repeater")

        # Format public key for display
        pub_key_formatted = ""
        if self.local_identity:
            pub_key_hex = self.local_identity.get_public_key().hex()
            # Format as <first8...last8>
            if len(pub_key_hex) >= 16:
                pub_key_formatted = f"{pub_key_hex[:8]}...{pub_key_hex[-8:]}"
            else:
                pub_key_formatted = pub_key_hex

        current_loop = asyncio.get_event_loop()

        self.http_server = HTTPStatsServer(
            host=http_host,
            port=http_port,
            stats_getter=self.get_stats,
            node_name=node_name,
            pub_key=pub_key_formatted,
            send_advert_func=self.send_advert,
            config=self.config, 
            event_loop=current_loop, 
            daemon_instance=self,  
            config_path=getattr(self, 'config_path', '/etc/pymc_repeater/config.yaml'),
        )

        try:
            self.http_server.start()
        except Exception as e:
            logger.error(f"Failed to start HTTP server: {e}")

        # Run dispatcher (handles RX/TX via pymc_core)
        try:
            await self.dispatcher.run_forever()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            if self.router:
                await self.router.stop()
            if self.http_server:
                self.http_server.stop()


def main():

    import argparse

    parser = argparse.ArgumentParser(description="pyMC Repeater Daemon")
    parser.add_argument(
        "--config",
        help="Path to config file (default: /etc/pymc_repeater/config.yaml)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )

    args = parser.parse_args()

    # Load configuration
    config = load_config(args.config)
    config_path = args.config if args.config else '/etc/pymc_repeater/config.yaml'

    if args.log_level:
        config["logging"]["level"] = args.log_level

    # Don't initialize radio here - it will be done inside the async event loop
    daemon = RepeaterDaemon(config, radio=None)
    daemon.config_path = config_path

    # Run
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        logger.info("Repeater stopped")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
