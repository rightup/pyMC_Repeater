# Adding a New Sensor Plug-in

Sensors in openhop-repeater are self-contained modules that live in `repeater/sensors/`. The subsystem is plug-in based: adding a new sensor requires only one new file. The manager discovers and loads it automatically at runtime by importing the module named after the sensor type.

---

## How the sensor subsystem works

| Component | File | Role |
|-----------|------|------|
| `SensorBase` | `repeater/sensors/base.py` | Abstract base class all sensors inherit from |
| `SensorRegistry` | `repeater/sensors/registry.py` | Maps type strings → sensor classes via `@SensorRegistry.register` |
| `SensorManager` | `repeater/sensors/manager.py` | Reads config, imports sensor modules, polls sensors in background |

When `SensorManager` loads a sensor of type `"foo"`, it calls `importlib.import_module("repeater.sensors.foo")`. That import runs the `@SensorRegistry.register("foo")` decorator on your class, making it available. No changes to `__init__.py` or the manager are needed.

---

## Step-by-step guide

### 1. Create `repeater/sensors/<type>.py`

Name the file after the sensor type string (lowercase, underscores for hyphens). The type string is what operators write in `config.yaml`.

Minimal template:

```python
"""
<SensorName> sensor plug-in.

Requires: pip install <package>

Config example:
  - type: <type>
    name: "my-sensor"
    enabled: true
    auto_install_packages: false
    settings:
      some_option: value
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import SensorBase
from .registry import SensorRegistry


@SensorRegistry.register("<type>")
class MySensor(SensorBase):
    sensor_type = "<type>"

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None, log=None):
        super().__init__(name=name, config=config, log=log)

        # Read settings with safe defaults
        self.some_option = self.settings.get("some_option", "default")

        self.available = False
        if not self.ensure_python_modules(
            [
                ("import_name", "pip-package-name"),
            ]
        ):
            return  # logs a warning; sensor will report unavailable

        try:
            import import_name  # type: ignore[import-not-found]
            # Initialise hardware here
            self.device = import_name.Device(...)
            self.available = True
            self.log.info("MySensor initialized")
        except Exception as exc:
            self.log.warning("MySensor init failed: %s", exc)
            self.available = False

    def _read(self) -> Dict[str, Any]:
        if not self.available:
            raise RuntimeError("device not available")
        try:
            return {
                "field_one": ...,
                "field_two": ...,
            }
        except Exception as exc:
            raise RuntimeError(f"read failed: {exc}") from exc
```

Key rules:

- **`sensor_type`** class attribute must match the string passed to `@SensorRegistry.register`.
- **`self.settings`** is the `settings:` block from the sensor's config entry (a plain dict).
- **`ensure_python_modules`** handles missing dependencies gracefully. Pass a multi-line list of `(import_name, pip_package)` tuples. Returns `False` and logs a warning if any are missing and `auto_install_packages` is `false`; installs them via pip if `true`. Sensor-specific packages belong here — do **not** add them to `pyproject.toml`.
- **`_read`** must return a flat `dict[str, Any]`. The base class wraps it in a standard envelope (`name`, `type`, `ok`, `timestamp`, `data`, optional `error`).
- **`_read`** must raise `RuntimeError` on failure — the base class catches it, marks `ok=False`, and logs it without crashing the polling loop.
- All hardware initialisation belongs in `__init__`, not in `_read`. Keep `_read` fast.
- Lazy-import third-party packages inside `__init__` (after `ensure_python_modules` returns `True`) so the module can be imported on hosts that don't have the package installed.

### 2. Add a commented example to `config.yaml.example`

Find the `sensors.definitions` block and add your sensor alongside the existing examples:

```yaml
    # Example MySensor (commented out by default)
    # - type: <type>
    #   name: my-sensor
    #   enabled: true
    #   auto_install_packages: true
    #   settings:
    #     some_option: value
```

Use hex notation for I2C addresses (e.g. `0x43`) as this matches how addresses are listed in datasheets and tools like `i2cdetect`.

### 3. Test locally

Add a test to `tests/test_sensors.py` that:

1. Registers a lightweight mock of your sensor (or stubs the hardware import).
2. Verifies that `SensorManager` loads it and `read_all()` returns the expected structure.
3. Verifies that a hardware failure in `_read` produces an `ok=False` result rather than raising.

Example pattern from the existing test suite:

```python
class _MockMySensor(SensorBase):
    sensor_type = "<type>"

    def _read(self):
        return {"field_one": 42.0, "field_two": 55.0}

SensorRegistry.register("<type>", _MockMySensor)

def test_my_sensor_loads_and_reads():
    config = {
        "sensors": {
            "enabled": True,
            "definitions": [
                {"name": "test-sensor", "type": "<type>", "settings": {}},
            ],
        }
    }
    manager = SensorManager(config)
    readings = manager.read_all()
    assert readings[0]["ok"] is True
    assert readings[0]["data"]["field_one"] == 42.0
```

---

## Checklist

- [ ] `repeater/sensors/<type>.py` created
- [ ] `sensor_type` class attribute matches the `@SensorRegistry.register` key
- [ ] All settings read from `self.settings` with sensible defaults
- [ ] `ensure_python_modules` called before any third-party import
- [ ] Hardware initialised in `__init__`, not `_read`
- [ ] `_read` raises `RuntimeError` on failure (never returns `None` or partial data silently)
- [ ] Commented example added to `config.yaml.example`
- [ ] Unit test added to `tests/test_sensors.py`

---

## Existing sensors

| Type | File | Hardware |
|------|------|----------|
| `hardware_stats` | `repeater/sensors/hardware_stats.py` | Host CPU / memory / disk / network (via `psutil`) |
| `ina219` | `repeater/sensors/ina219.py` | INA219 I²C current/voltage/power monitor |
| `ens210` | `repeater/sensors/ens210.py` | ENS210 I²C relative humidity and temperature sensor |
| `bme280` | `repeater/sensors/bme280.py` | BME280 I²C temperature, humidity and barometric pressure sensor |
