import logging
import time
from typing import Dict, Optional

from openhop_core.protocol import Identity
from openhop_core.protocol.constants import PUB_KEY_SIZE

# ACL roles come from openhop_core, which mirrors firmware
# ``src/helpers/ClientACL.h``: the role is the LOW TWO BITS of the permissions
# byte and ADMIN is 3 — it is not "the 0x02 bit".
#
# This import is deliberately fail-closed. A core without these symbols still
# builds the login reply's is_admin byte from ``permissions & 0x02``, which
# also matches READ_WRITE (2); pairing it with this module would silently
# announce a room server's read-write clients as admins. Refusing to start is
# the safe failure.
try:
    from openhop_core.protocol.constants import (
        PERM_ACL_ADMIN,
        PERM_ACL_GUEST,
        PERM_ACL_READ_ONLY,
        PERM_ACL_READ_WRITE,
        PERM_ACL_ROLE_MASK,
    )
    from openhop_core.protocol.constants import acl_is_admin as is_admin_permissions
    from openhop_core.protocol.constants import acl_role as role_of
except ImportError as exc:  # pragma: no cover - exercised by the install, not tests
    raise ImportError(
        "openhop_core is too old: it does not export PERM_ACL_* / acl_is_admin. "
        "Install openhop_core with the ACL role fix (fix/login-perms or later) — "
        "an older core encodes admin as the 0x02 bit and would announce "
        "read-write clients as admins."
    ) from exc

logger = logging.getLogger("ACL")

_ROLE_NAMES = {
    PERM_ACL_GUEST: "guest",
    PERM_ACL_READ_ONLY: "read_only",
    PERM_ACL_READ_WRITE: "read_write",
    PERM_ACL_ADMIN: "admin",
}


def role_name(permissions: int) -> str:
    """Human-readable role name for logs and the web API."""
    return _ROLE_NAMES[role_of(permissions)]


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
        return is_admin_permissions(self.permissions)

    def is_guest(self) -> bool:
        return role_of(self.permissions) == PERM_ACL_GUEST

    def role_name(self) -> str:
        """Role name ("guest"/"read_only"/"read_write"/"admin") for logs and the API."""
        return role_name(self.permissions)


class ACL:
    def __init__(
        self,
        max_clients: int = 50,
        admin_password: Optional[str] = None,
        guest_password: Optional[str] = None,
        allow_read_only: bool = True,
    ):
        self.max_clients = max_clients
        self.admin_password = admin_password or ""
        self.guest_password = guest_password or ""
        self.allow_read_only = allow_read_only
        self.clients: Dict[bytes, ClientInfo] = {}

    def _is_replay(self, client: ClientInfo, timestamp: int) -> bool:
        if timestamp <= client.last_timestamp:
            logger.warning(
                f"Possible replay attack! timestamp={timestamp}, last={client.last_timestamp}"
            )
            return True
        return False

    def _touch_client_session(
        self,
        client: ClientInfo,
        shared_secret: bytes,
        timestamp: int,
        sync_since: int = None,
    ) -> None:
        now = int(time.time())
        # Monotonic: the replay watermark must never move backwards, even if
        # another accepted request advanced it between the replay check and
        # this write.
        client.last_timestamp = max(client.last_timestamp, timestamp)
        client.last_activity = now
        client.last_login_success = now
        client.shared_secret = shared_secret
        if sync_since is not None:
            client.sync_since = sync_since
            logger.debug(f"Stored sync_since={sync_since} for client")

    def authenticate_client(
        self,
        client_identity: Identity,
        shared_secret: bytes,
        password: str,
        timestamp: int,
        sync_since: int = None,
        target_identity_hash: int = None,
        target_identity_name: str = None,
        target_identity_config: dict = None,
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
                logger.error(
                    f"Room server '{target_identity_name}' has no passwords configured! Set admin_password and/or guest_password in settings."
                )
                return False, 0
        else:
            # Repeater uses global passwords from its own security section
            admin_pwd = self.admin_password
            guest_pwd = self.guest_password
            logger.debug(
                f"Repeater passwords - admin: {'SET' if admin_pwd else 'NONE'}, "
                f"guest: {'SET' if guest_pwd else 'NONE'}"
            )

        admin_pwd = admin_pwd or ""
        guest_pwd = guest_pwd or ""

        if target_identity_name:
            logger.debug(
                f"Authenticating for identity '{target_identity_name}' (room_server={is_room_server})"
            )

        pub_key = client_identity.get_public_key()[:PUB_KEY_SIZE]

        if not password:
            client = self.clients.get(pub_key)
            if client is None:
                if not self.allow_read_only:
                    logger.info("Blank password, sender not in ACL and read-only disabled")
                    return False, 0
                if len(self.clients) >= self.max_clients:
                    logger.warning("ACL full, cannot add client")
                    return False, 0
                client = ClientInfo(client_identity, PERM_ACL_GUEST)
                self.clients[pub_key] = client
                logger.info("Blank password, allowing read-only guest access")
            else:
                logger.info(f"ACL-based login for {pub_key[:6].hex()}...")

            if self._is_replay(client, timestamp):
                return False, 0
            self._touch_client_session(client, shared_secret, timestamp, sync_since=sync_since)
            # No role normalisation needed: PERM_ACL_GUEST *is* role 0, so a
            # client stored with no role bits already reads back as a guest.
            return True, client.permissions

        permissions = 0
        logger.debug(f"Comparing password (len={len(password)}) against admin/guest")
        logger.debug(
            f"Admin pwd len={len(admin_pwd) if admin_pwd else 0}, Guest pwd len={len(guest_pwd) if guest_pwd else 0}"
        )
        if admin_pwd and password == admin_pwd:
            permissions = PERM_ACL_ADMIN
            logger.info(f"Admin password validated for '{target_identity_name or 'unknown'}'")
        elif guest_pwd and password == guest_pwd:
            # Firmware splits the guest password by server type. simple_repeater
            # grants GUEST (may fetch base telemetry, may not change settings);
            # simple_room_server grants READ_WRITE (may post and read messages).
            permissions = PERM_ACL_READ_WRITE if is_room_server else PERM_ACL_GUEST
            logger.info(
                f"Guest password validated for '{target_identity_name or 'unknown'}' "
                f"(role={role_name(permissions)})"
            )
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

        if self._is_replay(client, timestamp):
            return False, 0
        self._touch_client_session(client, shared_secret, timestamp, sync_since=sync_since)
        client.permissions &= ~PERM_ACL_ROLE_MASK
        client.permissions |= permissions

        logger.info(f"Login success! Role: {client.role_name()}")
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
