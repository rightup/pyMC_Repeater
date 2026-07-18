"""
Repeater CompanionBridge with SQLite-backed preference persistence.

Persists full NodePrefs as a JSON blob so companion settings (including
auto-add config) survive repeater restarts. Merge-on-load supports
schema evolution when NodePrefs gains or loses fields.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from enum import Enum
from typing import Any, Callable, Optional

from openhop_core.companion import CompanionBridge

from repeater.companion.correlation import outbound_send_capture

logger = logging.getLogger("RepeaterCompanionBridge")


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
        tracker=None,
    ) -> None:
        self._sqlite_handler = sqlite_handler
        self._companion_hash = companion_hash
        self._on_prefs_saved = on_prefs_saved
        # RF correlation tracker (design doc §10.4): registers each
        # outbound send so a later heard-repeat can be journaled as
        # message_send_state. Optional/None when correlation isn't wired up.
        self._tracker = tracker
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

    async def _send_packet(self, pkt, wait_for_ack: bool = False, expected_crc=None) -> bool:
        """Send via the packet_injector, then register/publish the outbound
        packet_hash for RF correlation (design doc §10.4).

        Registration and hash publication happen unconditionally (even for
        actions this bridge sends besides text/channel messages — logins,
        status/telemetry requests) since a hash-only registration that never
        gets a duplicate is just an inert, TTL-expiring tracker entry; the
        cost is one cheap dict write. The hash is computed once here and
        reused for both, so it matches exactly what ``packets.packet_hash``
        and ``companion_messages.packet_hash`` use as their correlation key.
        """
        if expected_crc is None:
            sent = await self._packet_injector(pkt, wait_for_ack=wait_for_ack)
        else:
            sent = await self._packet_injector(
                pkt, wait_for_ack=wait_for_ack, expected_crc=expected_crc
            )
        if sent:
            try:
                packet_hash = pkt.calculate_packet_hash().hex().upper()
            except Exception as e:
                logger.debug("Could not compute packet hash for correlation: %s", e)
                return sent
            if self._tracker is not None and self._companion_hash:
                self._tracker.register_outbound(packet_hash, self._companion_hash)
            holder = outbound_send_capture.get()
            if holder is not None:
                holder["hash"] = packet_hash
        return sent

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
