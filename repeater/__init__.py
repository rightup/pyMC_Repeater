import sys
import types

# Provide an RPi.GPIO shim on platforms without the real library.
try:
    import RPi.GPIO  # type: ignore
except Exception:
    try:
        from . import LuckfoxGPIO as _gpio  # type: ignore
        rpi_mod = types.ModuleType("RPi")
        rpi_mod.GPIO = _gpio
        sys.modules["RPi"] = rpi_mod
        sys.modules["RPi.GPIO"] = _gpio
    except Exception:
        # If loading the shim fails, let downstream imports raise naturally.
        pass

__version__ = "1.0.5-beta-1"
