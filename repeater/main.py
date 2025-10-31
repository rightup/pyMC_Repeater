import asyncio
import logging
import os
import sys

from repeater.config import get_radio_for_board, load_config
from repeater.engine import RepeaterHandler
from repeater.http_server import HTTPStatsServer, _log_buffer
from pymc_core.node.handlers.trace import TraceHandler
from pymc_core.protocol.constants import MAX_PATH_SIZE, ROUTE_TYPE_DIRECT

logger = logging.getLogger("RepeaterDaemon")





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
        self.trace_handler = None


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
                    self.radio.set_custom_cad_thresholds(peak=23, min_val=11)
                    logger.info("CAD thresholds set: peak=23, min=11")
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

            self.dispatcher.register_fallback_handler(self._repeater_callback)
            logger.info("Repeater handler registered (forwarder mode)")

            self.trace_handler = TraceHandler(log_fn=logger.info)
            
            self.dispatcher.register_handler(
                TraceHandler.payload_type(),
                self._trace_callback,
            )
            logger.info("Trace handler registered for network diagnostics")

            

        except Exception as e:
            logger.error(f"Failed to initialize dispatcher: {e}")
            raise

    async def _repeater_callback(self, packet):

        if self.repeater_handler:

            metadata = {
                "rssi": getattr(packet, "rssi", 0),
                "snr": getattr(packet, "snr", 0.0),
                "timestamp": getattr(packet, "timestamp", 0),
            }
            await self.repeater_handler(packet, metadata)

    async def _trace_callback(self, packet):

        try:
            # Only process direct route trace packets
            if packet.get_route_type() != ROUTE_TYPE_DIRECT or packet.path_len >= MAX_PATH_SIZE:
                return

         
            parsed_data = self.trace_handler._parse_trace_payload(packet.payload)
            
            if not parsed_data.get("valid", False):
                logger.warning(f"[TraceHandler] Invalid trace packet: {parsed_data.get('error', 'Unknown error')}")
                return
            
            trace_path = parsed_data["trace_path"]
            trace_path_len = len(trace_path)
            
          
            if self.repeater_handler:
                import time
                
                trace_path_bytes = [f"{h:02X}" for h in trace_path[:8]]
                if len(trace_path) > 8:
                    trace_path_bytes.append("...")
                path_hash = "[" + ", ".join(trace_path_bytes) + "]"
                
                path_snrs = []
                path_snr_details = []
                for i in range(packet.path_len):
                    if i < len(packet.path):
                        snr_val = packet.path[i]
                   
                        snr_signed = snr_val if snr_val < 128 else snr_val - 256
                        snr_db = snr_signed / 4.0
                        path_snrs.append(f"{snr_val}({snr_db:.1f}dB)")
                 
                        if i < len(trace_path):
                            path_snr_details.append({
                                "hash": f"{trace_path[i]:02X}",
                                "snr_raw": snr_val,
                                "snr_db": snr_db
                            })
                
                packet_record = {
                    "timestamp": time.time(),
                    "type": packet.get_payload_type(),  # 0x09 for trace
                    "route": packet.get_route_type(),   # Should be direct (1)
                    "length": len(packet.payload or b""),
                    "rssi": getattr(packet, "rssi", 0),
                    "snr": getattr(packet, "snr", 0.0),
                    "score": self.repeater_handler.calculate_packet_score(
                        getattr(packet, "snr", 0.0), 
                        len(packet.payload or b""), 
                        self.repeater_handler.radio_config.get("spreading_factor", 8)
                    ),
                    "tx_delay_ms": 0,  
                    "transmitted": False,  
                    "is_duplicate": False,  
                    "packet_hash": packet.calculate_packet_hash().hex()[:16],
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
                }
                self.repeater_handler.log_trace_record(packet_record)
    
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
            
       
            parsed_data["snr"] = packet.get_snr()
            parsed_data["rssi"] = getattr(packet, "rssi", 0)
            formatted_response = self.trace_handler._format_trace_response(parsed_data)
            
            logger.info(f"[TraceHandler] {formatted_response}")
            logger.info(f"[TraceHandler] Path SNRs: [{', '.join(path_snrs)}], Hashes: [{', '.join(path_hashes)}]")
            
     
            if (packet.path_len < trace_path_len and 
                len(trace_path) > packet.path_len and
                trace_path[packet.path_len] == self.local_hash and
                self.repeater_handler and not self.repeater_handler.is_duplicate(packet)):
                
                if self.repeater_handler and hasattr(self.repeater_handler, 'recent_packets'):
                    packet_hash = packet.calculate_packet_hash().hex()[:16]
                    for record in reversed(self.repeater_handler.recent_packets):
                        if record.get("packet_hash") == packet_hash:
                            record["transmitted"] = True
                            record["drop_reason"] = "trace_forwarded"
                            break
   
                current_snr = packet.get_snr()
                
    
                snr_scaled = int(current_snr * 4)
       
                if snr_scaled > 127:
                    snr_scaled = 127
                elif snr_scaled < -128:
                    snr_scaled = -128

                snr_byte = snr_scaled if snr_scaled >= 0 else (256 + snr_scaled)
        
                while len(packet.path) <= packet.path_len:
                    packet.path.append(0)
                    
                packet.path[packet.path_len] = snr_byte
                packet.path_len += 1
                
                logger.info(f"[TraceHandler] Forwarding trace, stored SNR {current_snr:.1f}dB at position {packet.path_len-1}")
                
                # Mark as seen and forward directly (bypass normal routing, no ACK required)
                self.repeater_handler.mark_seen(packet)
                if self.dispatcher:
                    await self.dispatcher.send_packet(packet, wait_for_ack=False)
            else:
                # Show why we didn't forward
                if packet.path_len >= trace_path_len:
                    logger.info(f"[TraceHandler] Trace completed (reached end of path)")
                elif len(trace_path) <= packet.path_len:
                    logger.info(f"[TraceHandler] Path index out of bounds")
                elif trace_path[packet.path_len] != self.local_hash:
                    expected_hash = trace_path[packet.path_len] if packet.path_len < len(trace_path) else None
                    logger.info(f"[TraceHandler] Not our turn (next hop: 0x{expected_hash:02x})")
                elif self.repeater_handler and self.repeater_handler.is_duplicate(packet):
                    logger.info(f"[TraceHandler] Duplicate packet, ignoring")

        except Exception as e:
            logger.error(f"[TraceHandler] Error processing trace packet: {e}")



    def get_stats(self) -> dict:

        if self.repeater_handler:
            stats = self.repeater_handler.get_stats()
            # Add public key if available
            if self.local_identity:
                try:
                    pubkey = self.local_identity.get_public_key()
                    stats["public_key"] = pubkey.hex()
                except Exception:
                    stats["public_key"] = None
            if self.radio:
                stats["radio_instance"] = self.radio
            return stats
        return {}

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

        template_dir = os.path.join(os.path.dirname(__file__), "templates")
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

        # Get the current event loop (the main loop where the radio was initialized)
        current_loop = asyncio.get_event_loop()

        self.http_server = HTTPStatsServer(
            host=http_host,
            port=http_port,
            stats_getter=self.get_stats,
            template_dir=template_dir,
            node_name=node_name,
            pub_key=pub_key_formatted,
            send_advert_func=self.send_advert,
            config=self.config,  # Pass the config reference
            event_loop=current_loop,  # Pass the main event loop
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

    if args.log_level:
        config["logging"]["level"] = args.log_level

    # Don't initialize radio here - it will be done inside the async event loop
    daemon = RepeaterDaemon(config, radio=None)

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
