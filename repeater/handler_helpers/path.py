import logging
import time

logger = logging.getLogger("PathHelper")


class PathHelper:
    def __init__(self, acl_dict=None, log_fn=None):

        self.acl_dict = acl_dict or {}
        self.log_fn = log_fn or logger.info

    async def process_path_packet(self, packet):

        from pymc_core.protocol.crypto import CryptoUtils
        
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
            # Format: path_len(1) + path[path_len] + extra_type(1) + extra[...]
            if len(decrypted) < 1:
                logger.debug(f"Decrypted PATH data too short")
                return False
            
            path_len = decrypted[0]
            if len(decrypted) < 1 + path_len:
                logger.debug(f"PATH data truncated: need {1 + path_len} bytes, got {len(decrypted)}")
                return False
            
            path_data = decrypted[1:1 + path_len]
            
            # Update client's out_path (same as C++ memcpy)
            client.out_path = bytearray(path_data)
            client.out_path_len = path_len
            client.last_activity = int(time.time())
            
            logger.info(
                f"Updated out_path for client 0x{src_hash:02X} -> 0x{dest_hash:02X}: "
                f"path_len={path_len}, path={[hex(b) for b in path_data]}"
            )
            
            # Don't mark as do_not_retransmit - let it forward normally
            return False
            
        except Exception as e:
            logger.error(f"Error processing PATH packet: {e}", exc_info=True)
            return False
