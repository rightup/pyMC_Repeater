"""openHop Repeater plugin manager library.

Plugins are external processes/applications managed outside the main Repeater
process. This package handles install layout, lifecycle, and local IPC only.
"""

from .manifest import ManifestError, PluginManifest, parse_manifest
from .storage import PluginPaths, PluginStorage

__all__ = [
    "ManifestError",
    "PluginManifest",
    "parse_manifest",
    "PluginPaths",
    "PluginStorage",
]
