import asyncio
import logging
import time

logger = logging.getLogger("PathHelper")


class PathHelper:
    def __init__(self, acl_dict=None, log_fn=None, ack_received_callback=None):

        self.acl_dict = acl_dict or {}
        self.log_fn = log_fn or logger.info
        self.ack_received_callback = ack_received_callback

    async def _register_ack_crc(self, ack_crc: int) -> None:
        """Propagate an ACK CRC to the configured callback."""
        if ack_crc is None:
            return
        callback = self.ack_received_callback
        if callback is None:
            return
        try:
            result = callback(ack_crc)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.debug(f"ACK callback failed for CRC {ack_crc:08X}: {e}")

    async def process_path_packet(self, packet):

        from openhop_core.protocol.constants import PAYLOAD_TYPE_ACK
        from openhop_core.protocol.crypto import CryptoUtils
        from openhop_core.protocol.packet_utils import PathUtils

        try:
            if len(packet.payload) < 2:
                return False

            dest_hash = packet.payload[0]
            src_hash = packet.payload[1]

            # Get the ACL for this destination identity
            identity_acl = self.acl_dict.get(dest_hash)
            if not identity_acl:
                logger.debug(f"No ACL for dest 0x{dest_hash:02X}, allowing forward")
                return False

            # Find the client by source hash
            client = None
            for client_info in identity_acl.get_all_clients():
                pubkey = client_info.id.get_public_key()
                if pubkey[0] == src_hash:
                    client = client_info
                    break

            if not client:
                logger.debug(f"PATH packet from unknown client 0x{src_hash:02X}, allowing forward")
                return False

            # Get shared secret for decryption
            shared_secret = client.shared_secret
            if not shared_secret or len(shared_secret) == 0:
                logger.debug(f"No shared secret for client 0x{src_hash:02X}, cannot decrypt PATH")
                return False

            # Decrypt the PATH packet payload
            # Payload format: dest_hash(1) + src_hash(1) + mac(2) + encrypted_data
            if len(packet.payload) < 4:
                logger.debug(f"PATH packet too short: {len(packet.payload)} bytes")
                return False

            mac_and_data = packet.payload[2:]  # Skip dest_hash and src_hash
            aes_key = shared_secret[:16]
            decrypted = CryptoUtils.mac_then_decrypt(aes_key, shared_secret, mac_and_data)

            if not decrypted:
                logger.debug(f"Failed to decrypt PATH packet from 0x{src_hash:02X}")
                return False

            # Parse decrypted PATH data
            # Format: path_len(1) + path[path_byte_len] + extra_type(1) + extra[...]
            if len(decrypted) < 1:
                logger.debug("Decrypted PATH data too short")
                return False
            path_len_byte = decrypted[0]
            if PathUtils.is_valid_path_len(path_len_byte):
                path_byte_len = PathUtils.get_path_byte_len(path_len_byte)
                path_hops = PathUtils.get_path_hash_count(path_len_byte)
            else:
                # Legacy fallback for malformed/old packets: treat first byte as raw path bytes.
                path_byte_len = path_len_byte
                path_hops = path_byte_len

            if len(decrypted) < 1 + path_byte_len:
                logger.debug(
                    f"PATH data truncated: need {1 + path_byte_len} bytes, got {len(decrypted)}"
                )
                return False

            path_data = decrypted[1 : 1 + path_byte_len]

            # Update client's out_path (same as C++ memcpy); out_path_len keeps
            # the encoded byte so direct sends put it on the wire as-is.
            client.out_path = bytearray(path_data)
            client.out_path_len = (
                path_len_byte if PathUtils.is_valid_path_len(path_len_byte) else path_byte_len
            )
            client.last_activity = int(time.time())

            logger.info(
                f"Updated out_path for client 0x{src_hash:02X} -> 0x{dest_hash:02X}: "
                f"path_len_byte=0x{path_len_byte:02X}, hops={path_hops}, "
                f"path={[hex(b) for b in path_data]}"
            )

            # Handle bundled ACK in PATH extra section.
            ack_crc = None
            extra_start = 1 + path_byte_len
            if len(decrypted) > extra_start:
                extra_type = decrypted[extra_start] & 0x0F
                extra_payload = decrypted[extra_start + 1 :]
                if extra_type == PAYLOAD_TYPE_ACK and len(extra_payload) >= 4:
                    ack_crc = int.from_bytes(extra_payload[:4], "little")
                    logger.info(
                        f"PATH bundled ACK extracted for client 0x{src_hash:02X}: CRC={ack_crc:08X}"
                    )

            if ack_crc is not None:
                await self._register_ack_crc(ack_crc)
            # Don't mark as do_not_retransmit - let it forward normally
            return False

        except Exception as e:
            logger.error(f"Error processing PATH packet: {e}", exc_info=True)
            return False
