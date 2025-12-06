import os
import time
import sys

# MOCK RPi.GPIO Metadata
RPI_INFO = {
    'P1_REVISION': 3,
    'RAM': '1024M',
    'REVISION': 'a02082',  # This code means "Raspberry Pi 3 Model B"
    'TYPE': 'Pi 3 Model B',
    'PROCESSOR': 'BCM2837',
    'MANUFACTURER': 'Sony'
}

RPI_REVISION = 3  # Deprecated but sometimes checked
VERSION = '0.7.0' # Fake RPi.GPIO version

BOARD = 10
BCM = 11
OUT = "out"
IN = "in"
HIGH = 1
LOW = 0
PUD_OFF = 0
PUD_DOWN = 1
PUD_UP = 2
# Add missing constants to satisfy RPi.GPIO checks
I2C = 42
SPI = 41
PWM = 40
SERIAL = 30
UNKNOWN = -1
HARD_PWM = 43
RISING = 31
FALLING = 32
BOTH = 33

def setmode(mode): pass
def setwarnings(flag): pass

def setup(pin, mode, pull_up_down=PUD_OFF, initial=None):
    path = f"/sys/class/gpio/gpio{pin}"
    if not os.path.exists(path):
        try:
            with open("/sys/class/gpio/export", "w") as f:
                f.write(str(pin))
            time.sleep(0.1)
        except OSError: pass

    try:
        with open(f"{path}/direction", "w") as f:
            f.write("out" if mode == OUT else "in")
    except OSError: pass

    if mode == OUT and initial is not None:
        output(pin, initial)

def output(pin, value):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "w") as f:
            f.write("1" if value else "0")
    except: pass

def input(pin):
    try:
        with open(f"/sys/class/gpio/gpio{pin}/value", "r") as f:
            return int(f.read().strip())
    except: return 0

def cleanup(channel=None): pass

sys.modules['RPi'] = type('RPi', (object,), {'GPIO': sys.modules[__name__]})
sys.modules['RPi.GPIO'] = sys.modules[__name__]
