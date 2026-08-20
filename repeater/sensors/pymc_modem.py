"""Compatibility alias for the renamed openHop Modem sensor."""

from . import openhop_modem as _implementation
from .openhop_modem import OpenHopModemSensor
from .registry import SensorRegistry

# Keep the old module-level urllib seam used by downstream tests/integrations.
urllib = _implementation.urllib

SensorRegistry.register("pymc_modem", OpenHopModemSensor)

# Preserve the old import path/class name for existing integrations.
PymcModemSensor = OpenHopModemSensor

__all__ = ["PymcModemSensor"]
