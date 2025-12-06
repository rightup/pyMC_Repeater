import sys
import os
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)

try:
    import LuckfoxGPIO
    sys.modules['RPi'] = type('RPi', (object,), {'GPIO': LuckfoxGPIO})
    sys.modules['RPi.GPIO'] = LuckfoxGPIO
    print("Luckfox GPIO Shim applied successfully!")
except ImportError as e:
    print(f"CRITICAL: Could not load LuckfoxGPIO shim: {e}")

__version__ = "1.0.5-beta-1"
