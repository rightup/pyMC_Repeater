"""
Repeater CompanionBridge with SQLite-backed preference persistence.

Persists full NodePrefs as a JSON blob so companion settings (including
auto-add config) survive repeater restarts. Merge-on-load supports
schema evolution when NodePrefs gains or loses fields.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Mapping
from contextvars import ContextVar
from enum import Enum
from typing import Any, Callable, Optional

from openhop_core.companion import CompanionBridge
from openhop_core.protocol.constants import MAX_TEXT_LEN, PAYLOAD_TYPE_GRP_TXT, ROUTE_TYPE_FLOOD
from openhop_core.protocol.packet import Packet
from openhop_core.protocol.transport_keys import get_auto_key_for

logger = logging.getLogger("RepeaterCompanionBridge")

# One packet only: consumed before Core invokes the transport or any callbacks.
_channel_region_scope: ContextVar[Optional[tuple[object, bytes]]] = ContextVar(
    "companion_channel_region_scope", default=None
)


def normalize_region_name(value: str) -> str:
    """Normalize a public auto-hashtag region, never arbitrary key material."""
    if not isinstance(value, str):
        raise ValueError("region must be a string")
    region = value.strip().removeprefix("#")
    if not region.isascii():
        raise ValueError("region must contain only ASCII characters")
    region = region.lower()
    if re.fullmatch(r"[a-z0-9-]{1,30}", region, flags=re.ASCII) is None:
        raise ValueError("region must contain 1-30 ASCII letters, digits, or hyphens")
    return region


def _prefs_bytes_from_json(value: Any) -> bytes:
    """Restore a ``bytes`` NodePrefs field from JSON (hex string from :func:`_to_json_safe`)."""
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return b""
        try:
            return bytes.fromhex(s)
        except ValueError:
            logger.debug("Invalid hex for prefs bytes field (prefix %r)", s[:32])
            return b""
    return b""


def _to_json_safe(value: Any) -> Any:
    """Convert a value to a JSON-serializable form (avoids TypeError from enums, bytes, etc.)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_json_safe(v) for k, v in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_json_safe(getattr(value, f.name)) for f in dataclasses.fields(value)}
    return value


class RepeaterCompanionBridge(CompanionBridge):
    """CompanionBridge that persists and loads prefs (full NodePrefs) via SQLite JSON blob."""

    def __init__(
        self,
        identity,
        packet_injector: Callable[..., Any],
        node_name: str = "pyMC",
        adv_type: int = 1,
        max_contacts: int = 1000,
        max_channels: int = 40,
        offline_queue_size: int = 512,
        radio_config: Optional[dict] = None,
        authenticate_callback: Optional[Callable[..., tuple[bool, int]]] = None,
        initial_contacts: Optional[Any] = None,
        *,
        radio_settings_getter: Optional[Callable[[], Mapping[str, Any]]] = None,
        max_tx_power_getter: Optional[Callable[[], Optional[int]]] = None,
        sqlite_handler=None,
        companion_hash: str = "",
        on_prefs_saved: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._sqlite_handler = sqlite_handler
        self._companion_hash = companion_hash
        self._on_prefs_saved = on_prefs_saved
        super().__init__(
            identity=identity,
            packet_injector=packet_injector,
            node_name=node_name,
            adv_type=adv_type,
            max_contacts=max_contacts,
            max_channels=max_channels,
            offline_queue_size=offline_queue_size,
            radio_config=radio_config,
            authenticate_callback=authenticate_callback,
            initial_contacts=initial_contacts,
            radio_settings_getter=radio_settings_getter,
            max_tx_power_getter=max_tx_power_getter,
        )

    def _apply_flood_scope(self, pkt: Packet) -> None:
        pending = _channel_region_scope.get()
        if pending is None or pending[0] is not self:
            super()._apply_flood_scope(pkt)
            return
        # No shared scope mutation: nested sends and child tasks must not inherit
        # this intent when the packet injector or a Core callback runs.
        _channel_region_scope.set(None)
        if (
            pkt.get_payload_type() != PAYLOAD_TYPE_GRP_TXT
            or pkt.get_route_type() != ROUTE_TYPE_FLOOD
        ):
            raise ValueError("region override requires a flood channel text packet")
        self._scope_packet(pkt, pending[1])
        pkt._flood_scope_applied = True

    async def send_channel_message(
        self,
        channel_idx: int,
        text: str,
        timestamp: Optional[int] = None,
        *,
        region: Optional[str] = None,
    ) -> bool:
        """Optionally scope one channel message without changing radio defaults."""
        if region is None:
            return await super().send_channel_message(channel_idx, text, timestamp=timestamp)
        region_key = get_auto_key_for(normalize_region_name(region))
        if (
            not isinstance(channel_idx, int)
            or isinstance(channel_idx, bool)
            or not 0 <= channel_idx <= 255
        ):
            raise ValueError("invalid channel index")
        if not isinstance(text, str) or not text.strip() or "\x00" in text:
            raise ValueError("text must be nonempty and contain no NUL characters")
        if timestamp is not None and (
            not isinstance(timestamp, int)
            or isinstance(timestamp, bool)
            or not 0 <= timestamp <= 0xFFFFFFFF
        ):
            raise ValueError("invalid channel timestamp")
        # Keep this adjacent to Core's builder, with no await before it reads the
        # sender name. New scoped sends reject truncation; legacy sends keep it.
        max_bytes = max(0, MAX_TEXT_LEN - len(f"{self.prefs.node_name}: ".encode("utf-8")))
        if len(text.encode("utf-8")) > max_bytes:
            raise ValueError(f"text exceeds {max_bytes} UTF-8 bytes for the channel sender name")
        token = _channel_region_scope.set((self, region_key))
        try:
            # Retain Core's full channel-secret encryption and echo tracking.
            return await super().send_channel_message(channel_idx, text, timestamp=timestamp)
        finally:
            # Also clear intents when a channel is missing, construction fails,
            # or transport cancellation interrupts the send.
            _channel_region_scope.reset(token)

    def _save_prefs(self) -> None:
        """Persist full NodePrefs as JSON to SQLite."""
        if not self._sqlite_handler or not self._companion_hash:
            return
        try:
            prefs_dict = dataclasses.asdict(self.prefs)
            prefs_safe = _to_json_safe(prefs_dict)
            self._sqlite_handler.companion_save_prefs(str(self._companion_hash), prefs_safe)
            if self._on_prefs_saved:
                try:
                    self._on_prefs_saved(self.prefs.node_name)
                except Exception as e:
                    logger.warning("Failed to sync node_name to config: %s", e)
        except Exception as e:
            logger.warning("Failed to persist companion prefs: %s", e)

    def _load_prefs(self) -> None:
        """Load prefs from SQLite JSON and merge into self.prefs (only known keys)."""
        if not self._sqlite_handler or not self._companion_hash:
            return
        try:
            stored = self._sqlite_handler.companion_load_prefs(self._companion_hash)
            if not stored or not isinstance(stored, dict):
                return
            for key, value in stored.items():
                if not hasattr(self.prefs, key):
                    continue
                current = getattr(self.prefs, key)
                try:
                    if value is None:
                        continue
                    if isinstance(current, bytes):
                        setattr(self.prefs, key, _prefs_bytes_from_json(value))
                        continue
                    if isinstance(current, bool):
                        setattr(self.prefs, key, bool(value))
                    elif isinstance(current, int):
                        setattr(self.prefs, key, int(value))
                    elif isinstance(current, float):
                        setattr(self.prefs, key, float(value))
                    elif isinstance(current, str):
                        setattr(self.prefs, key, str(value))
                    else:
                        setattr(self.prefs, key, value)
                except (TypeError, ValueError) as e:
                    logger.debug("Skip prefs key %r: %s", key, e)
        except Exception as e:
            logger.warning("Failed to load companion prefs: %s", e)
