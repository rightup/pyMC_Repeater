"""
Repeater CompanionBridge with SQLite-backed preference persistence.

Persists full NodePrefs as a JSON blob so companion settings (including
auto-add config) survive repeater restarts. Merge-on-load supports
schema evolution when NodePrefs gains or loses fields.
"""

from __future__ import annotations

import dataclasses
import logging
from enum import Enum
from typing import Any, Callable, Optional

from pymc_core.companion import CompanionBridge

logger = logging.getLogger("RepeaterCompanionBridge")


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
        )
        # Load persisted prefs (e.g. node_name) from SQLite so matching uses last-saved name
        self._load_prefs()

    def _save_prefs(self) -> None:
        """Persist full NodePrefs as JSON to SQLite."""
        if not self._sqlite_handler or not self._companion_hash:
            return
        try:
            prefs_dict = dataclasses.asdict(self.prefs)
            prefs_safe = _to_json_safe(prefs_dict)
            self._sqlite_handler.companion_save_prefs(
                str(self._companion_hash), prefs_safe
            )
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
