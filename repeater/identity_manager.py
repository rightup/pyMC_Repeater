import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

from repeater.exceptions import ConfigurationError

logger = logging.getLogger("IdentityManager")


def _namespace_for(identity_type: str) -> str:
    """Routing/persistence namespace a local identity occupies.

    A one-byte dest-hash collision is only unrepresentable when both
    identities share a namespace.  Companions key their runtime bridge
    (``daemon.companion_bridges[hash]``) and their SQLite state
    (``companion_prefs`` PRIMARY KEY, plus the contacts/channels/messages
    tables) by the hash byte.  The repeater and every room server instead
    share the login/text/protocol helper ``handlers[hash]`` slot and the
    ``room_*`` tables, so they form one "server" namespace.  A companion and
    a server-side identity live in physically separate stores, and the packet
    router (``_consume_via_local_candidates``) offers a colliding packet to
    both and lets HMAC pick the owner, so they may safely share a prefix.
    """
    return "companion" if identity_type == "companion" else "server"


class IdentityConfigurationError(ConfigurationError):
    """A configured local identity cannot be represented safely."""


@dataclass(frozen=True)
class IdentitySpec:
    """A parsed-but-unregistered local identity from configuration."""

    name: str
    identity: Any  # openhop_core LocalIdentity (or compatible)
    config: dict
    identity_type: str  # "repeater" | "room_server" | "companion"

    @property
    def label(self) -> str:
        return f"{self.identity_type}:{self.name}"

    @property
    def hash_byte(self) -> int:
        return self.identity.get_public_key()[0]


class IdentityManager:
    def __init__(self, config: dict):
        self.config = config
        self.identities: Dict[Tuple[int, str], Tuple[Any, dict, str]] = {}
        self.named_identities: Dict[str, Tuple[Any, dict, str]] = {}
        self.registered_hashes: Dict[Tuple[int, str], str] = {}

    def registration_error(self, name: str, identity, identity_type: str) -> Optional[str]:
        """Return a reason this identity cannot be registered, or ``None``.

        Local protocol routing and per-identity persistence are keyed by the
        first public-key byte *within a namespace* (see :func:`_namespace_for`).
        Two identities that share both the hash byte and the namespace cannot
        be represented safely even though their full keys differ; a companion
        and a server-side identity may share a prefix because they live in
        separate stores that the packet router disambiguates by HMAC.  Names
        must also be unique because callers use them to locate the service.
        """
        hash_byte = identity.get_public_key()[0]
        key = (hash_byte, _namespace_for(identity_type))

        if key in self.identities:
            existing_name = self.registered_hashes.get(key, "unknown")
            return (
                f"Identity '{name}' (hash=0x{hash_byte:02X}) conflicts with "
                f"existing identity '{existing_name}'; identities of the same "
                f"class must have unique one-byte public-key prefixes"
            )

        if name in self.named_identities:
            existing_identity, _, existing_type = self.named_identities[name]
            existing_hash = existing_identity.get_public_key()[0]
            return (
                f"Identity name '{name}' is already registered for "
                f"{existing_type} (hash=0x{existing_hash:02X})"
            )

        return None

    def validate_specs(self, specs: Iterable[IdentitySpec]) -> None:
        """Raise ``IdentityConfigurationError`` on any name or hash collision.

        Each spec is checked against the currently registered identities (via
        :meth:`registration_error`) and against the other specs in the batch,
        without mutating any state. A one-byte prefix collision is only
        rejected when both identities share a namespace (see
        :func:`_namespace_for`); a companion may share a prefix with a
        server-side identity. Names must be unique because callers use them to
        locate the configured service.
        """
        batch_keys: Dict[Tuple[int, str], IdentitySpec] = {}
        batch_names: Dict[str, IdentitySpec] = {}

        for spec in specs:
            error = self.registration_error(spec.name, spec.identity, spec.identity_type)
            if error:
                raise IdentityConfigurationError(error)
            existing = batch_names.get(spec.name)
            if existing is not None:
                raise IdentityConfigurationError(
                    f"Local identity name '{spec.name}' conflicts with existing "
                    f"identity '{existing.label}'"
                )
            key = (spec.hash_byte, _namespace_for(spec.identity_type))
            existing = batch_keys.get(key)
            if existing is not None:
                raise IdentityConfigurationError(
                    f"Local identity '{spec.label}' (hash=0x{spec.hash_byte:02X}) "
                    f"conflicts with '{existing.label}'; identities of the same "
                    "class must have unique one-byte public-key prefixes"
                )
            batch_names[spec.name] = spec
            batch_keys[key] = spec

    def validate_identity(self, name: str, identity, identity_type: str) -> bool:
        """Log and report whether an identity can be registered without mutation."""
        error = self.registration_error(name, identity, identity_type)
        if error:
            logger.error("Identity registration rejected: %s", error)
            return False
        return True

    def register_identity(self, name: str, identity, config: dict, identity_type: str):
        if not self.validate_identity(name, identity, identity_type):
            return False

        hash_byte = identity.get_public_key()[0]
        key = (hash_byte, _namespace_for(identity_type))

        self.identities[key] = (identity, config, identity_type)
        self.named_identities[name] = (identity, config, identity_type)
        self.registered_hashes[key] = f"{identity_type}:{name}"

        logger.info(
            f"Identity registered: name={name}, hash=0x{hash_byte:02X}, type={identity_type}"
        )
        return True

    def get_identity_by_hash(
        self, hash_byte: int, namespace: Optional[str] = None
    ) -> Optional[Tuple[Any, dict, str]]:
        """Return the identity registered at ``hash_byte``.

        With ``namespace`` given, returns that namespace's identity; otherwise
        returns the first identity at the hash (a companion and a server-side
        identity may both be registered there since they no longer collide).
        """
        if namespace is not None:
            return self.identities.get((hash_byte, namespace))
        for (registered_hash, _ns), value in self.identities.items():
            if registered_hash == hash_byte:
                return value
        return None

    def get_identity_by_name(self, name: str) -> Optional[Tuple[Any, dict, str]]:
        return self.named_identities.get(name)

    def has_identity(self, hash_byte: int, namespace: Optional[str] = None) -> bool:
        if namespace is not None:
            return (hash_byte, namespace) in self.identities
        return any(registered_hash == hash_byte for registered_hash, _ns in self.identities)

    def list_identities(self) -> list:
        identities = []
        for (hash_byte, namespace), (identity, config, id_type) in self.identities.items():
            name = self.registered_hashes.get((hash_byte, namespace), "unknown")
            identities.append(
                {
                    "hash": f"0x{hash_byte:02X}",
                    "name": name,
                    "type": id_type,
                    "address": identity.get_address_bytes().hex() if identity else "N/A",
                    "public_key": identity.get_public_key().hex() if identity else None,
                }
            )
        return identities

    def has_identity_type(self, identity_type: str) -> bool:
        return any(id_type == identity_type for _, _, id_type in self.identities.values())

    def get_identities_by_type(self, identity_type: str) -> list:
        results = []
        for name, (identity, config, id_type) in self.named_identities.items():
            if id_type == identity_type:
                results.append((name, identity, config))
        return results
