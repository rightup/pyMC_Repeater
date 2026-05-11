"""Bundled MQTT broker presets.

Each sibling ``*.yaml`` file in this package defines a named preset - a
ready-to-use list of broker dicts for a known MeshCoreToMQTT (MC2MQTT)
network. Presets ship with the package, so a ``pip install -U`` is enough
to pick up new endpoints without editing user config.

Public API:
    get_preset(name)   -> dict (the parsed YAML, or {} if unknown)
    list_presets()     -> sorted list of preset names

The loader is lazy: nothing is read or parsed at import time. The first
call discovers sibling YAML files via ``importlib.resources`` and caches
the parsed dicts for the lifetime of the process.
"""

from __future__ import annotations

import logging
from importlib.resources import files
from pathlib import Path
from typing import Dict, List

import yaml

logger = logging.getLogger("Presets")

# Cache of parsed presets, keyed by name (e.g. "waev"). Populated on first
# call to _load_all(); never cleared.
_CACHE: Dict[str, dict] = {}
_LOADED: bool = False


def _load_all() -> Dict[str, dict]:
    """Discover and parse every bundled ``*.yaml`` file once."""
    global _LOADED
    if _LOADED:
        return _CACHE
    for resource in files(__package__).iterdir():
        # importlib.resources.Traversable: only consider real files ending in .yaml
        name = getattr(resource, "name", "")
        if not name.endswith(".yaml"):
            continue
        try:
            with resource.open("r", encoding="utf-8") as f:
                _CACHE[Path(name).stem] = yaml.safe_load(f) or {}
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"Failed to load preset '{name}': {e}")
    _LOADED = True
    return _CACHE


def get_preset(name: str) -> dict:
    """Return the parsed preset dict, or ``{}`` if no such preset exists."""
    return _load_all().get(name, {})


def list_presets() -> List[str]:
    """Return the sorted list of bundled preset names."""
    return sorted(_load_all().keys())
