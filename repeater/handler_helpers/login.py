"""
Login/ANON_REQ packet handling helper for pyMC Repeater.

This module processes login requests and manages authentication for all identities.
"""

import asyncio
import logging

from pymc_core.node.handlers.login_server import LoginServerHandler

logger = logging.getLogger("LoginHelper")


class LoginHelper:
    def __init__(self, identity_manager, packet_injector=None, log_fn=None):

        self.identity_manager = identity_manager
        self.packet_injector = packet_injector
        self.log_fn = log_fn or logger.info
        
        self.handlers = {}
        self.acls = {}  # Per-identity ACLs keyed by hash_byte

    def register_identity(self, name: str, identity, identity_type: str = "room_server", config: dict = None):
        config = config or {}
        
        hash_byte = identity.get_public_key()[0]
        
        # Create ACL for this identity
        from repeater.handler_helpers.acl import ACL
        
        # Get security config for this identity
        if identity_type == "room_server":
            # Room servers use passwords from their settings section only
            settings = config.get("settings", {})
            
            # Empty strings ('') are treated as "not set" by using 'or None'
            admin_password = settings.get("admin_password") or None
            guest_password = settings.get("guest_password") or None
            
            # Validate room servers have passwords configured
            if not admin_password and not guest_password:
                logger.error(
                    f"Room server '{name}' MUST have admin_password or guest_password configured. "
                    f"Add them to 'settings' section. Skipping registration."
                )
                return
            
            # Use configured passwords from settings
            final_security = {
                "max_clients": settings.get("max_clients", 50),
                "admin_password": admin_password,
                "guest_password": guest_password,
                "allow_read_only": settings.get("allow_read_only", True),
            }
        else:
            # Repeater uses security from repeater.security in config
            security = config.get("repeater", {}).get("security", {})
            final_security = {
                "max_clients": security.get("max_clients", 10),
                "admin_password": security.get("admin_password", "admin123"),
                "guest_password": security.get("guest_password", "guest123"),
                "allow_read_only": security.get("allow_read_only", True),
            }
            logger.debug(
                f"Repeater security config: admin_pw={'SET' if final_security['admin_password'] else 'NONE'}, "
                f"guest_pw={'SET' if final_security['guest_password'] else 'NONE'}, "
                f"max_clients={final_security['max_clients']}"
            )
        
        # Create ACL for this identity
        identity_acl = ACL(
            max_clients=final_security["max_clients"],
            admin_password=final_security["admin_password"],
            guest_password=final_security["guest_password"],
            allow_read_only=final_security["allow_read_only"],
        )
        
        self.acls[hash_byte] = identity_acl
        logger.info(f"Created ACL for {identity_type} '{name}': hash=0x{hash_byte:02X}")
        
        # Create auth callback that uses this identity's ACL
        def auth_callback_with_context(client_identity, shared_secret, password, timestamp, sync_since=None):
            return identity_acl.authenticate_client(
                client_identity=client_identity,
                shared_secret=shared_secret,
                password=password,
                timestamp=timestamp,
                sync_since=sync_since,
                target_identity_hash=hash_byte,
                target_identity_name=name,
                target_identity_config=config
            )
        
        handler = LoginServerHandler(
            local_identity=identity,
            log_fn=self.log_fn,
            authenticate_callback=auth_callback_with_context,
            is_room_server=(identity_type == "room_server"),
        )
        
        handler.set_send_packet_callback(self._send_packet_with_delay)
        
        self.handlers[hash_byte] = handler
        
        logger.info(f"Registered {identity_type} '{name}' login handler: hash=0x{hash_byte:02X}")



    async def process_login_packet(self, packet):

        try:
            if len(packet.payload) < 1:
                return False
            
            dest_hash = packet.payload[0]
            
            handler = self.handlers.get(dest_hash)
            if handler:
                logger.debug(f"Routing login to identity: hash=0x{dest_hash:02X}")
                await handler(packet)
                packet.mark_do_not_retransmit()
                return True
            else:
                logger.debug(f"No login handler registered for hash 0x{dest_hash:02X}, allowing forward")
                return False
                
        except Exception as e:
            logger.error(f"Error processing login packet: {e}")
            return False

    def _send_packet_with_delay(self, packet, delay_ms: int):
   
        if self.packet_injector:
            asyncio.create_task(self._delayed_send(packet, delay_ms))
        else:
            logger.error("No packet injector configured, cannot send login response")

    async def _delayed_send(self, packet, delay_ms: int):
 
        await asyncio.sleep(delay_ms / 1000.0)
        try:
            await self.packet_injector(packet, wait_for_ack=False)
            logger.debug(f"Sent login response after {delay_ms}ms delay")
        except Exception as e:
            logger.error(f"Error sending login response: {e}")

    def get_acl_dict(self):
        """Return dictionary of ACLs keyed by identity hash."""
        return self.acls
    
    def get_acl_for_identity(self, hash_byte: int):
        """Get ACL for a specific identity."""
        return self.acls.get(hash_byte)

    def list_authenticated_clients(self, hash_byte: int = None):
        """List authenticated clients for a specific identity or all identities."""
        if hash_byte is not None:
            acl = self.acls.get(hash_byte)
            return acl.get_all_clients() if acl else []
        
        # Return clients from all ACLs
        all_clients = []
        for acl in self.acls.values():
            all_clients.extend(acl.get_all_clients())
        return all_clients
