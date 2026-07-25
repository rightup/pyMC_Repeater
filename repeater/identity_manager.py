import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

logger = logging.getLogger("IdentityManager")


class IdentityConfigurationError(RuntimeError):
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
        self._lock = threading.RLock()
        self.identities: Dict[int, Tuple[Any, dict, str]] = {}
        self.named_identities: Dict[str, Tuple[Any, dict, str]] = {}
        self.registered_hashes: Dict[int, str] = {}

    def registration_error(self, name: str, identity) -> Optional[str]:
        """Return a reason this identity cannot be registered, or ``None``.

        Local protocol routing and companion persistence are keyed by the first
        byte of the public key.  A collision therefore cannot be represented
        safely, even though the full public keys differ.  Names must also be
        unique because callers use them to locate the configured service.
        """
        hash_byte = identity.get_public_key()[0]

        with self._lock:
            if hash_byte in self.identities:
                existing_name = self.registered_hashes.get(hash_byte, "unknown")
                return (
                    f"Identity '{name}' (hash=0x{hash_byte:02X}) conflicts with "
                    f"existing identity '{existing_name}'"
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
        without mutating any state. Local protocol routing and companion
        persistence are keyed by the first public-key byte, so a one-byte
        prefix collision cannot be represented even though the full keys
        differ, and names must be unique because callers use them to locate
        the configured service.
        """
        with self._lock:
            batch_hashes: Dict[int, IdentitySpec] = {}
            batch_names: Dict[str, IdentitySpec] = {}

            for spec in specs:
                error = self.registration_error(spec.name, spec.identity)
                if error:
                    raise IdentityConfigurationError(error)
                existing = batch_names.get(spec.name)
                if existing is not None:
                    raise IdentityConfigurationError(
                        f"Local identity name '{spec.name}' conflicts with existing "
                        f"identity '{existing.label}'"
                    )
                existing = batch_hashes.get(spec.hash_byte)
                if existing is not None:
                    raise IdentityConfigurationError(
                        f"Local identity '{spec.label}' (hash=0x{spec.hash_byte:02X}) "
                        f"conflicts with '{existing.label}'; local identities must "
                        "have unique one-byte public-key prefixes"
                    )
                batch_names[spec.name] = spec
                batch_hashes[spec.hash_byte] = spec

    def validate_identity(self, name: str, identity) -> bool:
        """Log and report whether an identity can be registered without mutation."""
        error = self.registration_error(name, identity)
        if error:
            logger.error("Identity registration rejected: %s", error)
            return False
        return True

    def register_identity(self, name: str, identity, config: dict, identity_type: str):
        with self._lock:
            if not self.validate_identity(name, identity):
                return False

            hash_byte = identity.get_public_key()[0]

            self.identities[hash_byte] = (identity, config, identity_type)
            self.named_identities[name] = (identity, config, identity_type)
            self.registered_hashes[hash_byte] = f"{identity_type}:{name}"

        logger.info(
            f"Identity registered: name={name}, hash=0x{hash_byte:02X}, type={identity_type}"
        )
        return True

    def unregister_identity(self, name: str) -> bool:
        """Remove one identity from every manager index.

        Registration is all-or-nothing across these three maps; removal must
        preserve the same invariant so a deleted companion cannot continue to
        reserve its routing hash or appear under a stale lookup.
        """
        with self._lock:
            registered = self.named_identities.pop(name, None)
            if registered is None:
                return False
            identity, _config, identity_type = registered
            hash_byte = identity.get_public_key()[0]
            self.identities.pop(hash_byte, None)
            self.registered_hashes.pop(hash_byte, None)
        logger.info(
            "Identity unregistered: name=%s, hash=0x%02X, type=%s",
            name,
            hash_byte,
            identity_type,
        )
        return True

    def get_identity_by_hash(self, hash_byte: int) -> Optional[Tuple[Any, dict, str]]:
        with self._lock:
            return self.identities.get(hash_byte)

    def get_identity_by_name(self, name: str) -> Optional[Tuple[Any, dict, str]]:
        with self._lock:
            return self.named_identities.get(name)

    def has_identity(self, hash_byte: int) -> bool:
        with self._lock:
            return hash_byte in self.identities

    def list_identities(self) -> list:
        with self._lock:
            identities = []
            for hash_byte, (identity, config, id_type) in self.identities.items():
                name = self.registered_hashes.get(hash_byte, "unknown")
                identities.append(
                    {
                        "hash": f"0x{hash_byte:02X}",
                        "name": name,
                        "type": id_type,
                        "address": (
                            identity.get_address_bytes().hex()
                            if identity
                            else "N/A"
                        ),
                        "public_key": (
                            identity.get_public_key().hex()
                            if identity
                            else None
                        ),
                    }
                )
            return identities

    def has_identity_type(self, identity_type: str) -> bool:
        with self._lock:
            return any(
                id_type == identity_type
                for _, _, id_type in self.identities.values()
            )

    def get_identities_by_type(self, identity_type: str) -> list:
        with self._lock:
            return [
                (name, identity, config)
                for name, (identity, config, id_type)
                in self.named_identities.items()
                if id_type == identity_type
            ]
