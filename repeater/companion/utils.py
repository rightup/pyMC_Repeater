"""Shared utilities for Companion (e.g. validation for config sync)."""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Dict, Iterable, Mapping, Optional

from openhop_core.companion.constants import DEFAULT_MAX_CONTACTS

logger = logging.getLogger(__name__)

_COMPANION_REGISTRATION_NAME_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}",
    re.ASCII,
)

# Optional per-companion RepeaterCompanionBridge constructor settings (power-user).
COMPANION_BRIDGE_SETTING_KEYS = frozenset({"max_contacts", "offline_queue_size"})

# Settings that must not be applied from config (fixed at openhop_core defaults).
_COMPANION_IGNORED_BRIDGE_KEYS = frozenset({"max_channels", "adv_type"})

# Contact flag bit 0 marks a favourite (protected from forced-trim eviction).
CONTACT_FLAG_FAVOURITE = 0x01
# Back-compat alias: this module used the private name before the flag became
# part of the v1 API surface (`favorite` on contacts).
_CONTACT_FLAG_FAVOURITE = CONTACT_FLAG_FAVOURITE

DEFAULT_COMPANION_TCP_PORT = 5000
DEFAULT_COMPANION_TCP_TIMEOUT_SEC = 8 * 60 * 60
MAX_COMPANION_TCP_TIMEOUT_SEC = 2_147_483_647
MAX_COMPANION_PUSH_MIN_INTERVAL_SEC = 86_400.0
MAX_COMPANION_PUSH_REQUEST_TIMEOUT_SEC = 300.0
# The upstream Frame transport has a fixed 2048-response writer queue. A
# 2000-contact cap leaves room for the dump's framing responses without making
# a power-user setting capable of silently overflowing that queue.
MAX_COMPANION_CONTACTS = 2_000
# Each pending entry carries a bounded protocol message. Keep this tunable but
# prevent a config typo from creating an effectively unbounded memory queue.
MAX_COMPANION_OFFLINE_QUEUE_SIZE = 4_096
_SQLITE_ROW_ID_MAX = (1 << 63) - 1
_UINT32_MAX = (1 << 32) - 1
_PACKET_HASH_RE = re.compile(r"[0-9A-F]{16}", re.ASCII)


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


def _reject_non_finite_json_constant(value: str) -> None:
    raise ValueError(f"{value} is not a finite JSON number")


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{value} is not a finite JSON number")
    return parsed


def _json_object_without_duplicates(pairs) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def strict_json_loads(value: Any) -> Any:
    """Parse interoperable RFC JSON.

    Python's default decoder accepts non-standard NaN/Infinity values and
    silently keeps the last duplicate object field. Neither behavior is safe
    for durable API state, where every human and agent must read one
    unambiguous value. Float overflow such as ``1e400`` is rejected too.
    """

    return json.loads(
        value,
        parse_constant=_reject_non_finite_json_constant,
        parse_float=_parse_finite_json_float,
        object_pairs_hook=_json_object_without_duplicates,
    )


def parse_companion_send_response(value: Any) -> Dict[str, Any]:
    """Parse and validate one durable Mobile Companion send response."""

    response = strict_json_loads(value)
    if not isinstance(response, dict):
        raise ValueError("stored send response must be a JSON object")
    if response.get("success") is not True:
        raise ValueError("stored send response must be a success envelope")
    data = response.get("data")
    if not isinstance(data, dict):
        raise ValueError("stored send response data must be an object")

    message_id = data.get("message_id")
    if (
        type(message_id) is not int
        or message_id < 1
        or message_id > _SQLITE_ROW_ID_MAX
    ):
        raise ValueError("stored send response has an invalid message_id")
    if type(data.get("sent")) is not bool:
        raise ValueError("stored send response has an invalid sent flag")
    if data.get("state") not in {"transmitted", "failed"}:
        raise ValueError("stored send response has an invalid state")
    if data["sent"] != (data["state"] == "transmitted"):
        raise ValueError("stored send response state conflicts with sent")

    packet_hash = data.get("packet_hash")
    if packet_hash is not None and (
        not isinstance(packet_hash, str)
        or _PACKET_HASH_RE.fullmatch(packet_hash) is None
    ):
        raise ValueError("stored send response has an invalid packet_hash")
    if (
        "expected_ack" in data
        and data["expected_ack"] is not None
        and (
            type(data["expected_ack"]) is not int
            or not 0 <= data["expected_ack"] <= _UINT32_MAX
        )
    ):
        raise ValueError("stored send response has an invalid expected_ack")
    if "is_flood" in data and type(data["is_flood"]) is not bool:
        raise ValueError("stored send response has an invalid is_flood flag")
    reason = data.get("reason")
    if (data["state"] == "failed" and reason is None) or (
        reason is not None and (not isinstance(reason, str) or not reason)
    ):
        raise ValueError("stored send response has an invalid reason")
    return response


def normalize_companion_identity_key(identity_key: str) -> str:
    """Strip whitespace and remove optional 0x prefix so fromhex() is consistent across installs."""
    s = identity_key.strip()
    if s.lower().startswith("0x"):
        s = s[2:].strip()
    return s


def companion_device_principal_id(
    companion_identity: object,
    companion_hash: object,
    device_id: object,
) -> str:
    """Return the stable, identity-qualified principal for one paired device."""
    namespace = str(companion_identity or companion_hash or "").strip().lower()
    stable_device_id = str(device_id or "")
    if not namespace or not stable_device_id:
        raise ValueError("paired device principal requires identity and device_id")
    return f"{namespace}:{stable_device_id}"


def validate_companion_node_name(value: str) -> str:
    """Validate node_name for config sync: non-empty, max 31 bytes UTF-8, no control chars."""
    if not isinstance(value, str):
        raise ValueError("node_name must be a string")
    s = value.strip()
    if not s:
        raise ValueError("node_name cannot be empty")
    try:
        encoded_size = len(s.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("node_name must be valid UTF-8") from None
    if encoded_size > 31:
        raise ValueError("node_name too long (max 31 bytes UTF-8)")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in s):
        raise ValueError("node_name contains invalid characters")
    return s


def validate_companion_registration_name(value: str) -> str:
    """Validate the stable URL/scope identifier for one companion.

    Human-facing spaces and Unicode belong in ``settings.node_name``. Keeping
    this identifier to one readable ASCII slug makes paths and
    ``companion:{name}`` authorization scopes unambiguous.
    """
    if not isinstance(value, str):
        raise ValueError("companion name must be a string")
    if _COMPANION_REGISTRATION_NAME_RE.fullmatch(value) is None:
        raise ValueError(
            "companion name must be 1-64 ASCII characters, start with a "
            "letter or digit, and contain only letters, digits, '.', '_', or '-'"
        )
    return value


def validate_companion_tcp_port(value: Any) -> int:
    """Return a valid TCP listener port without coercing ambiguous JSON types."""
    if type(value) is not int or not 1 <= value <= 65_535:
        raise ValueError("tcp_port must be an integer between 1 and 65535")
    return value


def validate_companion_tcp_timeout(value: Any) -> int:
    """Return a valid idle timeout in seconds; zero explicitly disables it."""
    if type(value) is not int or not 0 <= value <= MAX_COMPANION_TCP_TIMEOUT_SEC:
        raise ValueError(
            "tcp_timeout must be an integer between 0 and "
            f"{MAX_COMPANION_TCP_TIMEOUT_SEC} seconds (0 disables it)"
        )
    return value


def validate_companion_legacy_adoption(value: Any) -> bool:
    """Require an explicit JSON/YAML boolean for legacy namespace adoption."""

    if type(value) is not bool:
        raise ValueError("adopt_legacy_namespace must be a boolean")
    return value


def validate_companion_boolean_setting(value: Any, setting_name: str) -> bool:
    """Require a real JSON/YAML boolean for one named companion setting."""

    if type(value) is not bool:
        raise ValueError(f"{setting_name} must be a boolean")
    return value


def validate_companion_seconds_setting(
    value: Any,
    setting_name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    """Return one finite, bounded seconds setting without string coercion."""

    if type(value) not in (int, float):
        raise ValueError(f"{setting_name} must be a number")
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(
            f"{setting_name} must be between {minimum:g} and {maximum:g} seconds"
        ) from None
    if not math.isfinite(seconds) or not minimum <= seconds <= maximum:
        raise ValueError(
            f"{setting_name} must be between {minimum:g} and {maximum:g} seconds"
        )
    return seconds


def validate_companion_bind_address(value: Any) -> str:
    """Return one explicit TCP bind host suitable for ``asyncio.start_server``.

    Resolution remains the operating system's job so hostnames, IPv4, IPv6,
    and scoped IPv6 addresses all keep working.  This check only rejects
    ambiguous or hostile configuration values before any stateful companion
    setup begins.
    """
    if not isinstance(value, str):
        raise ValueError("bind_address must be a string")
    address = value.strip()
    try:
        encoded_size = len(address.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("bind_address must be valid UTF-8") from None
    if (
        not address
        or encoded_size > 255
        or any(
            character.isspace()
            or ord(character) < 0x20
            or ord(character) == 0x7F
            for character in address
        )
    ):
        raise ValueError(
            "bind_address must be a non-empty host or IP address without whitespace "
            "or control characters (max 255 bytes)"
        )
    return address


def _listener_enabled(value: Any) -> bool:
    """Match the daemon's small, human-readable boolean config convention."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def validate_companion_listener_config(
    companions: Iterable[Mapping[str, Any]],
    http_config: Optional[Mapping[str, Any]] = None,
) -> None:
    """Require one unambiguous process-wide TCP port per local listener.

    A globally unique port is intentionally stricter than reasoning about
    wildcard and interface-specific bind overlap.  It is easier for operators,
    clients, and automation to understand, and it prevents an interface change
    from turning a previously valid configuration into an outage.
    """
    owners: Dict[int, str] = {}
    http_settings = http_config or {}
    if _listener_enabled(http_settings.get("enabled", True)):
        http_port = http_settings.get("port", 8000)
        if type(http_port) is not int or not 1 <= http_port <= 65_535:
            raise ValueError("http.port must be an integer between 1 and 65535")
        owners[http_port] = "Repeater HTTP API"

    for companion in companions:
        name = str(companion.get("name") or "<unnamed>")
        raw_settings = companion.get("settings")
        settings = {} if raw_settings is None else raw_settings
        if not isinstance(settings, Mapping):
            raise ValueError(f"companion '{name}' settings must be an object")
        frame_enabled = validate_companion_boolean_setting(
            settings.get("frame_enabled", True),
            "frame_enabled",
        )
        if not frame_enabled:
            continue
        port = validate_companion_tcp_port(
            settings.get("tcp_port", DEFAULT_COMPANION_TCP_PORT)
        )
        validate_companion_bind_address(
            settings.get("bind_address", "127.0.0.1")
        )
        owner = owners.get(port)
        if owner is not None:
            raise ValueError(
                f"tcp_port {port} for companion '{name}' conflicts with {owner}; "
                "choose a unique TCP port"
            )
        owners[port] = f"companion '{name}'"


def parse_positive_int(
    value: Any,
    field_name: str,
    *,
    minimum: int = 1,
    maximum: Optional[int] = None,
) -> int:
    """Parse an integer from config without coercing JSON booleans/floats."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field_name} must be a positive integer")
    try:
        n = int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field_name} must be a positive integer") from e
    if n < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    if maximum is not None and n > maximum:
        raise ValueError(f"{field_name} must be <= {maximum}")
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
        max_contacts = parse_positive_int(
            settings["max_contacts"],
            "max_contacts",
            maximum=MAX_COMPANION_CONTACTS,
        )
        kwargs["max_contacts"] = max_contacts
    if "offline_queue_size" in settings:
        # 0 is valid and means "off" (no offline message storage).
        kwargs["offline_queue_size"] = parse_positive_int(
            settings["offline_queue_size"],
            "offline_queue_size",
            minimum=0,
            maximum=MAX_COMPANION_OFFLINE_QUEUE_SIZE,
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
        elif key == "tcp_port":
            merged[key] = validate_companion_tcp_port(value)
        elif key == "tcp_timeout":
            merged[key] = validate_companion_tcp_timeout(value)
        elif key == "bind_address":
            merged[key] = validate_companion_bind_address(value)
        elif key == "node_name":
            merged[key] = validate_companion_node_name(value)
        elif key == "adopt_legacy_namespace":
            merged[key] = validate_companion_legacy_adoption(value)
        elif key in {
            "frame_enabled",
            "trim_contacts_on_overflow",
            "rf_reception_events",
        }:
            merged[key] = validate_companion_boolean_setting(value, key)
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
    strict_count = getattr(
        type(sqlite_handler),
        "companion_count_contacts_strict",
        None,
    )
    if callable(strict_count):
        stored_count = strict_count(sqlite_handler, companion_hash)
    else:
        # Compatibility for small storage doubles and older external storage
        # adapters. The bundled SQLite handler always takes the strict path.
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
    :func:`select_companion_contacts_to_trim`, atomically removes the selected
    rows with one journal event each, and returns the number removed (0 if
    already within the limit).

    Raises:
        ValueError: favourites alone exceed ``max_contacts`` (cannot trim).
        RuntimeError: loading or persisting the contact list failed.
    """
    if sqlite_handler is None:
        return 0
    strict_load = getattr(
        type(sqlite_handler),
        "companion_load_contacts_strict",
        None,
    )
    if callable(strict_load):
        contacts = strict_load(sqlite_handler, companion_hash)
    else:
        contacts = sqlite_handler.companion_load_contacts(companion_hash)
    if contacts is None:
        raise RuntimeError(
            f"Failed to load persisted contacts for {companion_hash}; refusing to trim"
        )
    _kept, removed = select_companion_contacts_to_trim(contacts, max_contacts)
    if not removed:
        return 0
    sqlite_handler.companion_apply_contact_changes(
        companion_hash,
        [{"change": "remove", "contact": contact} for contact in removed],
    )
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
        # REST/SSE remains available when the unauthenticated Frame listener
        # is unnecessary or its port is owned by another local application.
        "frame_enabled",
        # One-time, explicit ownership claim for companion rows created before
        # immutable full-public-key namespace bindings existed.
        "adopt_legacy_namespace",
        # Persistent opt-in: trim oldest non-favourite contacts to fit max_contacts
        # at load instead of refusing to start when over capacity.
        "trim_contacts_on_overflow",
        # Opt-in: journal an `rf_reception` event (design doc §9) to this
        # companion for every genuine OTA duplicate, not just correlated ones.
        # Default off — at ~50k packets/day it would otherwise dominate the
        # journal (§9 "Correlated vs. uncorrelated receptions").
        "rf_reception_events",
        *COMPANION_BRIDGE_SETTING_KEYS,
    }
)
