import logging
import time
from typing import Dict, Optional

from pymc_core.protocol import Identity
from pymc_core.protocol.constants import PUB_KEY_SIZE

logger = logging.getLogger("ACL")

PERM_ACL_GUEST = 0x01
PERM_ACL_ADMIN = 0x02
PERM_ACL_READ_WRITE = 0x01
PERM_ACL_ROLE_MASK = 0x03


class ClientInfo:
    """Represents an authenticated client in the access control list."""

    def __init__(self, identity: Identity, permissions: int = 0):
        self.id = identity
        self.permissions = permissions
        self.shared_secret = b""
        self.last_timestamp = 0
        self.last_activity = 0
        self.last_login_success = 0
        self.out_path_len = -1
        self.out_path = bytearray()
        self.sync_since = 0  # For room servers - timestamp of last synced message

    def is_admin(self) -> bool:
        return (self.permissions & PERM_ACL_ROLE_MASK) == PERM_ACL_ADMIN

    def is_guest(self) -> bool:
        return (self.permissions & PERM_ACL_ROLE_MASK) == PERM_ACL_GUEST


class ACL:

    def __init__(
        self,
        max_clients: int = 50,
        admin_password: str = "admin123",
        guest_password: str = "guest123",
        allow_read_only: bool = True,
    ):
        self.max_clients = max_clients
        self.admin_password = admin_password
        self.guest_password = guest_password
        self.allow_read_only = allow_read_only
        self.clients: Dict[bytes, ClientInfo] = {}

    def authenticate_client(
        self, 
        client_identity: Identity, 
        shared_secret: bytes, 
        password: str, 
        timestamp: int,
        sync_since: int = None,
        target_identity_hash: int = None,
        target_identity_name: str = None,
        target_identity_config: dict = None
    ) -> tuple[bool, int]:

        target_identity_config = target_identity_config or {}
        
        # Check for identity-specific passwords (required for room servers)
        identity_settings = target_identity_config.get("settings", {})
        
        # Determine if this is a room server by checking the type field
        identity_type = target_identity_config.get("type", "")
        is_room_server = identity_type == "room_server"
        
        # Log sync_since if provided (room server format)
        if sync_since is not None:
            logger.debug(f"Client sync_since timestamp: {sync_since}")
        
        if is_room_server:
            # Room servers use passwords from their settings section only
            # Empty strings are treated as "not set"
            admin_pwd = identity_settings.get("admin_password") or None
            guest_pwd = identity_settings.get("guest_password") or None
            
            if not admin_pwd and not guest_pwd:
                logger.error(f"Room server '{target_identity_name}' has no passwords configured! Set admin_password and/or guest_password in settings.")
                return False, 0
        else:
            # Repeater uses global passwords from its own security section
            admin_pwd = self.admin_password
            guest_pwd = self.guest_password
            logger.debug(
                f"Repeater passwords - admin: {'SET' if admin_pwd else 'NONE'}, "
                f"guest: {'SET' if guest_pwd else 'NONE'}"
            )
        
        if target_identity_name:
            logger.debug(f"Authenticating for identity '{target_identity_name}' (room_server={is_room_server})")
 
        pub_key = client_identity.get_public_key()[:PUB_KEY_SIZE]

        if not password:
            client = self.clients.get(pub_key)
            if client is None:
                if self.allow_read_only:
                    logger.info("Blank password, allowing read-only guest access")
                    return True, PERM_ACL_GUEST
                else:
                    logger.info("Blank password, sender not in ACL and read-only disabled")
                    return False, 0
            logger.info(f"ACL-based login for {pub_key[:6].hex()}...")
            return True, client.permissions

        permissions = 0
        logger.debug(f"Comparing password (len={len(password)}) against admin/guest")
        logger.debug(f"Admin pwd len={len(admin_pwd) if admin_pwd else 0}, Guest pwd len={len(guest_pwd) if guest_pwd else 0}")
        logger.debug(f"Password comparison: '{password}' vs admin='{admin_pwd[:4]}...' ({len(admin_pwd)} chars)")
        if admin_pwd and password == admin_pwd:
            permissions = PERM_ACL_ADMIN
            logger.info(f"Admin password validated for '{target_identity_name or 'unknown'}'")
        elif guest_pwd and password == guest_pwd:
            permissions = PERM_ACL_READ_WRITE
            logger.info(f"Guest password validated for '{target_identity_name or 'unknown'}'")
        else:
            logger.info(f"Invalid password for '{target_identity_name or 'unknown'}'")
            return False, 0

        client = self.clients.get(pub_key)
        if client is None:
            if len(self.clients) >= self.max_clients:
                logger.warning("ACL full, cannot add client")
                return False, 0

            client = ClientInfo(client_identity, 0)
            self.clients[pub_key] = client
            logger.info(f"Added new client {pub_key[:6].hex()}...")

        if timestamp <= client.last_timestamp:
            logger.warning(
                f"Possible replay attack! timestamp={timestamp}, last={client.last_timestamp}"
            )
            return False, 0

        client.last_timestamp = timestamp
        client.last_activity = int(time.time())
        client.last_login_success = int(time.time())
        client.permissions &= ~PERM_ACL_ROLE_MASK
        client.permissions |= permissions
        client.shared_secret = shared_secret
        
        # Store sync_since for room server clients
        if sync_since is not None:
            client.sync_since = sync_since
            logger.debug(f"Stored sync_since={sync_since} for client")

        logger.info(f"Login success! Permissions: {'ADMIN' if client.is_admin() else 'GUEST'}")
        return True, client.permissions

    def get_client(self, pub_key: bytes) -> Optional[ClientInfo]:
        return self.clients.get(pub_key[:PUB_KEY_SIZE])

    def get_num_clients(self) -> int:
        return len(self.clients)

    def get_all_clients(self):
        return list(self.clients.values())

    def remove_client(self, pub_key: bytes) -> bool:
        key = pub_key[:PUB_KEY_SIZE]
        if key in self.clients:
            del self.clients[key]
            return True
        return False
