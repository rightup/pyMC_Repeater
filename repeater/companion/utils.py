"""Shared utilities for Companion (e.g. validation for config sync)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from openhop_core.companion.constants import DEFAULT_MAX_CONTACTS

logger = logging.getLogger(__name__)

_INVALID_NODE_NAME_CHARS = "\n\r\x00"

# Optional per-companion RepeaterCompanionBridge constructor settings (power-user).
COMPANION_BRIDGE_SETTING_KEYS = frozenset({"max_contacts", "offline_queue_size"})

# Settings that must not be applied from config (fixed at openhop_core defaults).
_COMPANION_IGNORED_BRIDGE_KEYS = frozenset({"max_channels", "adv_type"})

# Contact flag bit 0 marks a favourite (protected from forced-trim eviction).
_CONTACT_FLAG_FAVOURITE = 0x01


class CompanionContactCapacityError(Exception):
    """Persisted companion contacts exceed configured max_contacts."""

    def __init__(
        self,
        companion_hash: str,
        stored_count: int,
        max_contacts: int,
        companion_name: Optional[str] = None,
    ) -> None:
        self.companion_hash = companion_hash
        self.stored_count = stored_count
        self.max_contacts = max_contacts
        self.companion_name = companion_name
        label = f"'{companion_name}'" if companion_name else companion_hash
        super().__init__(
            f"Companion {label}: {stored_count} contacts in storage exceeds "
            f"max_contacts={max_contacts}. Increase max_contacts or remove contacts before starting."
        )


class CompanionStateLoadError(Exception):
    """Persisted companion state exists in SQLite but could not be loaded.

    Raised at companion init so the companion fails loudly instead of starting
    with an empty store (which would present to clients as wiped channels or
    contacts and let subsequent saves overwrite the persisted state)."""


def normalize_companion_identity_key(identity_key: str) -> str:
    """Strip whitespace and remove optional 0x prefix so fromhex() is consistent across installs."""
    s = identity_key.strip()
    if s.lower().startswith("0x"):
        s = s[2:].strip()
    return s


def validate_companion_node_name(value: str) -> str:
    """Validate node_name for config sync: non-empty, max 31 bytes UTF-8, no control chars."""
    if not isinstance(value, str):
        raise ValueError("node_name must be a string")
    s = value.strip()
    if not s:
        raise ValueError("node_name cannot be empty")
    if len(s.encode("utf-8")) > 31:
        raise ValueError("node_name too long (max 31 bytes UTF-8)")
    if any(c in s for c in _INVALID_NODE_NAME_CHARS):
        raise ValueError("node_name contains invalid characters")
    return s


def parse_positive_int(value: Any, field_name: str, *, minimum: int = 1) -> int:
    """Parse a positive integer from config or API input."""
    try:
        n = int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field_name} must be a positive integer") from e
    if n < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return n


def parse_companion_bridge_kwargs(settings: dict) -> Dict[str, int]:
    """Extract optional RepeaterCompanionBridge kwargs from companion settings.

    Only ``max_contacts`` and ``offline_queue_size`` are honored. ``max_channels`` and
    ``adv_type`` are ignored with a warning if present.
    """
    if not settings:
        return {}
    for key in _COMPANION_IGNORED_BRIDGE_KEYS:
        if key in settings:
            logger.warning(
                "Companion setting %r is not supported and will be ignored (fixed default)",
                key,
            )
    kwargs: Dict[str, int] = {}
    if "max_contacts" in settings:
        max_contacts = parse_positive_int(settings["max_contacts"], "max_contacts")
        kwargs["max_contacts"] = max_contacts
    if "offline_queue_size" in settings:
        # 0 is valid and means "off" (no offline message storage).
        kwargs["offline_queue_size"] = parse_positive_int(
            settings["offline_queue_size"], "offline_queue_size", minimum=0
        )
    return kwargs


def effective_max_contacts(bridge_kwargs: Dict[str, int]) -> int:
    """Return max_contacts from parsed kwargs or openhop_core default."""
    return bridge_kwargs.get("max_contacts", DEFAULT_MAX_CONTACTS)


def merge_companion_settings_update(current_settings: dict, patch: dict) -> Dict[str, Any]:
    """Merge a companion settings PATCH into current settings.

    Raises:
        ValueError: Unknown setting or invalid bridge setting value.
    """
    merged = dict(current_settings or {})
    for key, value in patch.items():
        if key not in COMPANION_SETTINGS_ALLOWLIST:
            raise ValueError(f"Unknown companion setting: {key}")
        if key in COMPANION_BRIDGE_SETTING_KEYS:
            parsed = parse_companion_bridge_kwargs({key: value})
            merged[key] = parsed[key]
        else:
            merged[key] = value
    return merged


def validate_companion_config_capacity(
    identity: dict,
    sqlite_handler: Any,
    *,
    companion_name: Optional[str] = None,
    settings: Optional[dict] = None,
) -> None:
    """Raise CompanionContactCapacityError if persisted contacts exceed configured max_contacts."""
    if sqlite_handler is None:
        return
    identity_key = identity.get("identity_key")
    if not identity_key:
        return
    merged_settings = settings if settings is not None else (identity.get("settings") or {})
    max_contacts = effective_max_contacts(parse_companion_bridge_kwargs(merged_settings))
    companion_hash = companion_hash_str_from_identity_key(identity_key)
    check_companion_contact_capacity(
        companion_hash,
        max_contacts,
        sqlite_handler,
        companion_name=companion_name,
    )


def check_companion_contact_capacity(
    companion_hash: str,
    max_contacts: int,
    sqlite_handler: Any,
    *,
    companion_name: Optional[str] = None,
) -> None:
    """Raise CompanionContactCapacityError if persisted contacts exceed max_contacts."""
    if sqlite_handler is None:
        return
    stored_count = sqlite_handler.companion_count_contacts(companion_hash)
    if stored_count > max_contacts:
        raise CompanionContactCapacityError(
            companion_hash, stored_count, max_contacts, companion_name=companion_name
        )


def select_companion_contacts_to_trim(contacts, max_contacts: int):
    """Select which persisted contacts to keep/remove to fit ``max_contacts``.

    Mirrors ``ContactStore.add_or_overwrite`` eviction: the oldest non-favourite
    contacts (by ``lastmod``) are removed first; favourites (flags bit 0) are
    never evicted.

    Returns:
        (keep, removed): lists of contact dicts.

    Raises:
        ValueError: favourites alone exceed ``max_contacts`` (cannot trim).
    """
    contacts = list(contacts)
    if len(contacts) <= max_contacts:
        return contacts, []
    favourites = [c for c in contacts if int(c.get("flags", 0)) & _CONTACT_FLAG_FAVOURITE]
    if len(favourites) > max_contacts:
        raise ValueError(
            f"Cannot trim to max_contacts={max_contacts}: "
            f"{len(favourites)} favourite contacts cannot be evicted"
        )
    non_favourites = [c for c in contacts if not int(c.get("flags", 0)) & _CONTACT_FLAG_FAVOURITE]
    # Keep the newest non-favourites by lastmod; evict the oldest.
    non_favourites.sort(key=lambda c: int(c.get("lastmod", 0)))
    keep_count = max_contacts - len(favourites)
    removed = non_favourites[: len(non_favourites) - keep_count]
    kept_non_favourites = non_favourites[len(non_favourites) - keep_count :]
    return favourites + kept_non_favourites, removed


def trim_companion_contacts_to_fit(
    sqlite_handler: Any, companion_hash: str, max_contacts: int
) -> int:
    """Trim persisted contacts (favourite-aware) down to ``max_contacts``.

    Loads the companion's contacts, evicts the oldest non-favourites per
    :func:`select_companion_contacts_to_trim`, persists the kept set, and returns
    the number removed (0 if already within the limit).

    Raises:
        ValueError: favourites alone exceed ``max_contacts`` (cannot trim).
        RuntimeError: loading or persisting the contact list failed.
    """
    if sqlite_handler is None:
        return 0
    contacts = sqlite_handler.companion_load_contacts(companion_hash)
    if contacts is None:
        raise RuntimeError(
            f"Failed to load persisted contacts for {companion_hash}; refusing to trim"
        )
    keep, removed = select_companion_contacts_to_trim(contacts, max_contacts)
    if not removed:
        return 0
    if not sqlite_handler.companion_save_contacts(companion_hash, keep):
        raise RuntimeError(f"Failed to persist trimmed contacts for {companion_hash}")
    return len(removed)


def enforce_companion_contact_capacity(
    companion_hash: str,
    max_contacts: int,
    sqlite_handler: Any,
    *,
    trim: bool = False,
    companion_name: Optional[str] = None,
) -> int:
    """Ensure persisted contacts fit ``max_contacts`` at load time.

    With ``trim=False`` (default) this is a guard: it raises
    :class:`CompanionContactCapacityError` when over capacity. With ``trim=True``
    (the ``trim_contacts_on_overflow`` policy) it trims favourite-aware to fit,
    persists, and returns the number of contacts removed.
    """
    if not trim:
        check_companion_contact_capacity(
            companion_hash, max_contacts, sqlite_handler, companion_name=companion_name
        )
        return 0
    return trim_companion_contacts_to_fit(sqlite_handler, companion_hash, max_contacts)


def format_companion_bridge_limits(bridge_kwargs: Dict[str, int]) -> str:
    """Format non-default bridge limits for log lines."""
    if not bridge_kwargs:
        return ""
    parts = [f"{k}={v}" for k, v in sorted(bridge_kwargs.items())]
    return ", " + ", ".join(parts)


def companion_hash_str_from_identity_key(identity_key: Any) -> str:
    """Derive companion_hash storage key (0xHH) from an identity_key config value."""
    from openhop_core import LocalIdentity

    if isinstance(identity_key, str):
        key_bytes = bytes.fromhex(normalize_companion_identity_key(identity_key))
    elif isinstance(identity_key, bytes):
        key_bytes = identity_key
    else:
        raise ValueError("identity_key has unknown type")
    pubkey_byte = LocalIdentity(seed=key_bytes).get_public_key()[0]
    return f"0x{pubkey_byte:02x}"


# All companion settings writable via identity API (tcp + bridge power-user keys).
COMPANION_SETTINGS_ALLOWLIST = frozenset(
    {
        "node_name",
        "tcp_port",
        "bind_address",
        "tcp_timeout",
        # Persistent opt-in: trim oldest non-favourite contacts to fit max_contacts
        # at load instead of refusing to start when over capacity.
        "trim_contacts_on_overflow",
        *COMPANION_BRIDGE_SETTING_KEYS,
    }
)
